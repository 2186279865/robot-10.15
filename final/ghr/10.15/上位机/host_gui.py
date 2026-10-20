"""上位机主界面：tkinter + 串口/模拟双后端。

分工（与 `上位机方案-v3-TypeC-CDC.md` 第 5 节一致）：

* `serial_link` 只做字节收发；`HostClient` 把字节变成状态与事件（可在后台线程跑）。
* 界面线程只做两件事：`after()` 轮询事件、把操作翻译成 `client.send()`。
  绝不从后台线程直接碰控件。
* 序列执行器 `SequenceRunner` 由本文件驱动：事件先给界面、再转发给它。

命令行：
    python host_gui.py                 # 自动列串口，手动连接
    python host_gui.py --sim           # 直接进模拟模式（无硬件）
    python host_gui.py --port COM5
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import protocol as P  # noqa: E402
import home_route as HR  # noqa: E402
import sequence as S  # noqa: E402
from host_client import (  # noqa: E402
    STATE_DISCONNECTED,
    STATE_LINK_LOST,
    STATE_ONLINE,
    STATE_TEXT,
    HostClient,
)
from serial_link import AutoReconnector, list_serial_ports, open_best_link  # noqa: E402
from simulator import SimulatedDevice, SimulatedLink  # noqa: E402

#: 界面里的轴名字，必须与固件 `stepper.h` 的地址宏一致：Z=1、托盘=3、X=4、Y=5
AXIS_LABELS = {1: "Z (1)", 3: "托盘 (3)", 4: "X (4)", 5: "Y (5)"}


def home_order(axes=None):
    """回 0 的轴顺序：Z（先抬）→ X → Y → 转盘；axes 给定时只保留这些轴。"""
    order = [1, 4, 5, 3]
    if axes is None:
        return order
    want = set(int(a) for a in axes)
    return [addr for addr in order if addr in want]

BAUDRATE = 115200
POLL_MS = 30
LOG_MAX_LINES = 2000
#: 参数名以 `protocol.PARAM_DEFAULTS`（即模拟固件的契约）为准；
#: 这里只补中文说明，名称对不上就在测试里直接暴露出来。
PARAM_LABELS = {
    "speed_rpm": "默认转速 RPM",
    "acc": "默认加速度档",
    "speed_rpm_max": "速度上限 RPM",
    "gripper_open_deg": "夹爪松开角度",
    "gripper_grab_deg": "夹爪夹紧角度",
    "gripper_step_deg": "夹爪单步角度",
    "gripper_settle_ms": "夹爪等待 ms",
    "heartbeat_ms": "心跳周期 ms",
    "link_timeout_ms": "失联停车 ms",
    "query_timeout_ms": "查询超时 ms",
    "telemetry_ms": "遥测周期 ms",
    "frame_gap_ms": "电机帧间隔 ms",
    "readback_mask": "回读轴掩码(1/3/4/5=1/2/4/8)",
}
KNOWN_PARAMS = [(name, PARAM_LABELS[name]) for name in P.PARAM_DEFAULTS if name in PARAM_LABELS]


class AxisPanel:
    """单轴手动控制面板。"""

    # 与固件保持一致：STEPPER_PULSES_PER_REV=3200（位置指令单位），
    # EMM 回读编码器 65536 计数/圈（0x36 回包）。
    PULSES_PER_REV = 3200
    COUNTS_PER_REV = 65536.0

    def __init__(self, master, app, addr: int) -> None:
        self.app = app
        self.addr = addr
        self.enabled = False
        self.last_counts = None
        self.last_counts_ms = 0
        self.last_rpm = None

        frame = ttk.LabelFrame(master, text=AXIS_LABELS[addr])
        self.frame = frame

        self.direction = tk.IntVar(value=0)
        self.rpm = tk.IntVar(value=60)
        self.acc = tk.IntVar(value=1)
        # 位置手动输入 = 圈数 + 度数（两者相加），脉冲数由它们算出来
        self.turns = tk.DoubleVar(value=1.0)
        self.degrees = tk.DoubleVar(value=0.0)
        self.mode = tk.StringVar(value="rel")
        self.tele = tk.BooleanVar(value=False)
        self.reference_valid = False

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text="方向").pack(side="left")
        ttk.Radiobutton(row, text="正", value=0, variable=self.direction).pack(side="left")
        ttk.Radiobutton(row, text="负", value=1, variable=self.direction).pack(side="left")
        ttk.Label(row, text="转速").pack(side="left", padx=(6, 0))
        ttk.Spinbox(row, from_=1, to=300, width=5, textvariable=self.rpm).pack(side="left")
        ttk.Label(row, text="加速度").pack(side="left", padx=(6, 0))
        ttk.Spinbox(row, from_=0, to=255, width=4, textvariable=self.acc).pack(side="left")

        # 位置模式：手动输入"转几圈"和"转几度"，两者相加，脉冲自动算
        row2 = ttk.Frame(frame)
        row2.pack(fill="x", padx=4, pady=2)
        ttk.Label(row2, text="位置：圈").pack(side="left")
        ttk.Spinbox(
            row2,
            from_=0.0,
            to=10000.0,
            increment=0.1,
            width=7,
            format="%.2f",
            textvariable=self.turns,
        ).pack(side="left")
        ttk.Label(row2, text="度").pack(side="left", padx=(6, 0))
        ttk.Spinbox(
            row2,
            from_=0.0,
            to=9999.0,
            increment=5.0,
            width=7,
            format="%.1f",
            textvariable=self.degrees,
        ).pack(side="left")
        ttk.Label(row2, text="→").pack(side="left", padx=(6, 0))
        self.pulse_label = ttk.Label(row2, text="—", foreground="#0a3d91")
        self.pulse_label.pack(side="left")
        ttk.Label(row2, text="（1圈=3200脉冲）", foreground="#666666").pack(side="left", padx=(4, 0))
        self.turns.trace_add("write", lambda *_a: self._refresh_target())
        self.degrees.trace_add("write", lambda *_a: self._refresh_target())
        self.mode.trace_add("write", lambda *_a: self._refresh_target())

        row3 = ttk.Frame(frame)
        row3.pack(fill="x", padx=4, pady=2)
        ttk.Label(row3, text="下发方式").pack(side="left")
        ttk.Combobox(
            row3, width=5, state="readonly", values=("rel", "abs"), textvariable=self.mode
        ).pack(side="left")
        ttk.Button(row3, text="▶ 相对走", width=9, command=lambda: self.send_pos("rel")).pack(
            side="left", padx=(6, 2)
        )
        ttk.Button(row3, text="▶ 绝对走到此", width=12, command=lambda: self.send_pos("abs")).pack(
            side="left", padx=2
        )
        ttk.Checkbutton(
            row3, text="订阅遥测", variable=self.tele, command=self.toggle_telemetry
        ).pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=4, pady=2)
        for text, command in (
            ("速度模式", self.send_speed),
            ("停止", self.send_stop),
            ("使能", lambda: self.send_enable(1)),
            ("取消使能", lambda: self.send_enable(0)),
            ("解堵转", self.send_stall_clear),
            ("位置清零", self.send_zero),
        ):
            ttk.Button(buttons, text=text, width=8, command=command).pack(side="left", padx=1)

        buttons2 = ttk.Frame(frame)
        buttons2.pack(fill="x", padx=4, pady=2)
        for text, command in (
            ("查速度", lambda: self.query("vel")),
            ("查位置", lambda: self.query("pos")),
            ("查状态", lambda: self.query("flag")),
        ):
            ttk.Button(buttons2, text=text, width=8, command=command).pack(side="left", padx=1)

        self.target_label = ttk.Label(frame, text="目标位置：—", foreground="#444444")
        self.target_label.pack(fill="x", padx=4, pady=(2, 0))
        self.ref_label = ttk.Label(frame, text="位置基准：未建立", foreground="#8a5a00")
        self.ref_label.pack(fill="x", padx=4)
        self.readback = ttk.Label(frame, text="回读：—（未接回读）", foreground="#666666")
        self.readback.pack(fill="x", padx=4, pady=(0, 4))
        self._refresh_target()
        self._refresh_reference()

    # -- 圈/度 → 脉冲 + 位置基准 --------------------------------------------
    def total_pulses(self):
        """圈数 + 度数 → 脉冲数（1 圈 = 3200 脉冲，1° = 3200/360 脉冲）。

        输入非法（空、非数字、负数）时返回 None，由调用方提示。
        """
        try:
            turns = float(self.turns.get())
            degrees = float(self.degrees.get())
        except (tk.TclError, ValueError):
            return None
        if turns < 0.0 or degrees < 0.0:
            return None
        return int(round(turns * self.PULSES_PER_REV + degrees * (self.PULSES_PER_REV / 360.0)))

    def _refresh_target(self) -> None:
        pulses = self.total_pulses()
        mode = "相对走" if self.mode.get() == "rel" else "绝对走到此"
        if pulses is None:
            self.pulse_label.configure(text="—（请输入非负的圈数/度数）")
            self.target_label.configure(text=f"目标位置：输入无效　{mode}")
            return
        actual_deg = pulses * 360.0 / float(self.PULSES_PER_REV)
        self.pulse_label.configure(text=f"{pulses} 脉冲")
        self.target_label.configure(
            text=(
                f"目标位置：{float(self.turns.get()):.2f} 圈 + {float(self.degrees.get()):.1f}° "
                f"= {pulses} 脉冲（{actual_deg:.1f}°）　{mode}"
            )
        )

    def _refresh_reference(self) -> None:
        if self.reference_valid:
            self.ref_label.configure(
                text="位置基准：已建立（最近一次『位置清零』/已确认）", foreground="#0a6b0a"
            )
        else:
            self.ref_label.configure(
                text="位置基准：未建立 —— 用『绝对走到此』前先点『位置清零』", foreground="#8a5a00"
            )

    def reset_reference(self) -> None:
        """新会话：驱动器是否掉过电未知，绝对位置基准一律作废。"""
        self.reference_valid = False
        self._refresh_reference()

    def mark_zeroed(self) -> None:
        self.reference_valid = True
        self._refresh_reference()

    # -- 发送 ---------------------------------------------------------------
    def _guard(self) -> bool:
        if not self.app.client.online:
            self.app.note("未连接：请先在顶部连接固件（模拟模式也可以）")
            return False
        return True

    def send_speed(self) -> None:
        if not self._guard():
            return
        self.app.client.send("motor_speed", self.addr, self.direction.get(), self.rpm.get(), self.acc.get())

    def send_pos(self, mode: str = "rel") -> None:
        """按相对/绝对方式下发目标位置（目标 = 圈数 + 度数，换算成脉冲）。"""
        if not self._guard():
            return
        self.mode.set(mode)
        pulses = self.total_pulses()
        if pulses is None:
            self.app.note("圈数/度数必须是非负数字")
            return
        if pulses <= 0:
            self.app.note("圈数和度数不能同时为 0")
            return
        if mode == "abs" and not self.reference_valid:
            if not messagebox.askyesno(
                "绝对位置基准未建立",
                f"{AXIS_LABELS[self.addr]} 还没有建立位置基准。\n\n"
                "绝对位置以驱动器上电位置或最近一次『位置清零』为原点，\n"
                "基准不对时电机会按错误的目标位置行走。\n\n"
                "建议先点『位置清零』，再走绝对位置。\n\n仍要按现在的脉冲数下发吗？",
            ):
                return
            self.mark_zeroed()
        self.app.client.send(
            "motor_pos",
            self.addr,
            self.direction.get(),
            self.rpm.get(),
            self.acc.get(),
            pulses,
            mode,
        )
        self._refresh_target()

    def send_stop(self) -> None:
        if not self._guard():
            return
        self.app.client.send("motor_stop", self.addr)

    def send_enable(self, on: int) -> None:
        if not self._guard():
            return
        self.enabled = bool(on)
        self.app.client.send("motor_enable", self.addr, int(on))

    def send_stall_clear(self) -> None:
        if not self._guard():
            return
        self.app.client.send("motor_stall_clear", self.addr)

    def send_zero(self) -> None:
        if not self._guard():
            return
        if not messagebox.askyesno(
            "位置清零",
            f"把 {AXIS_LABELS[self.addr]} 的当前位置当成零点？\n\n"
            "固件只在轴停止时接受清零；清零后该轴的位置基准才算建立。",
        ):
            return
        self.app.client.send("motor_zero", self.addr)
        self.mark_zeroed()

    def query(self, what: str) -> None:
        if not self._guard():
            return
        self.app.client.send("motor_query", self.addr, what, timeout_ms=300)

    def toggle_telemetry(self) -> None:
        if not self._guard():
            self.tele.set(False)
            return
        period = 200
        if self.tele.get():
            self.app.client.send("tele_on", self.addr, period)
        else:
            self.app.client.send("tele_off", self.addr)

    # -- 显示 ---------------------------------------------------------------
    def update_telemetry(self, pairs: dict) -> None:
        if pairs.get("readback") == "0":
            self.no_readback = True
            self.refresh_readback()
            return
        rpm = pairs.get("rpm")
        if rpm is not None:
            self.last_rpm = rpm
        if "counts" in pairs:
            try:
                self.last_counts = int(pairs["counts"])
                self.last_counts_ms = self.app.client.now_ms()
            except ValueError:
                pass
        self.refresh_readback()

    def refresh_readback(self) -> None:
        # 固件在 hello 里声明了哪些轴有回读能力；没有的轴永远不要显示成"实测值"。
        if self.app.client.online and self.addr not in self.app.client.readback_axes:
            self.readback.configure(text="回读：—（该轴未接回读）", foreground="#999999")
            return
        if self.last_counts is None:
            self.readback.configure(text="回读：—（等遥测/查询）", foreground="#666666")
            return
        age = max(0, self.app.client.now_ms() - self.last_counts_ms)
        deg = self.last_counts * 360.0 / 65536.0
        rpm = f"，{self.last_rpm} RPM" if self.last_rpm is not None else ""
        stale = "（数据已过期）" if age > 3000 else ""
        self.readback.configure(
            text=f"回读：{self.last_counts} 脉冲 / {deg:.1f}°{rpm}，{age / 1000:.1f}s 前{stale}",
            foreground="#0a6b0a" if age <= 3000 else "#8a5a00",
        )


class App:
    def __init__(self, root: tk.Tk, args) -> None:
        self.root = root
        self.args = args
        self.client = HostClient(clock=time.monotonic, service=True)
        self.reconnector = AutoReconnector(self.client, self._make_link, time.monotonic)
        self.client.link_factory = self._make_link
        self.axes = {}
        self.sim_device = None
        self.sequence = S.Sequence()
        self.runner = S.SequenceRunner(self.client, self.client.now_ms, self._runner_message)
        self._stop_requested = False
        self._log_lines = 0
        # 软位置：没有回读也能算“一键回 0”。motor_pos(rel) 累加；
        # 速度模式/绝对模式/断线后标记未知，不做猜测。
        self.soft_pos = {addr: 0 for addr in AXIS_LABELS}
        self.pos_known = {addr: True for addr in AXIS_LABELS}
        self._home_queue = []
        self._route_queue = []
        self._route_done = 0
        self.home_state = HR.load_state()
        self._last_motion_s = 0.0
        self.client.motion_hook = self._on_motion

        root.title("机器人抓取系统 上位机（Type-C USB CDC）")
        try:
            root.geometry("1080x760")
        except tk.TclError:  # pragma: no cover - 无显示环境
            pass
        self._build_top()
        self._build_tabs()
        self._build_status()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_ports()
        if args.sim:
            self.sim.set(True)
        if args.port:
            self.port_text.set(args.port)
        root.after(POLL_MS, self._pump)
        if args.autoconnect:
            root.after(200, self.toggle_connect)

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_top(self) -> None:
        bar = ttk.Frame(self.root, padding=6)
        bar.pack(fill="x")

        self.sim = tk.BooleanVar(value=False)
        ttk.Label(bar, text="串口").pack(side="left")
        self.port_text = tk.StringVar()
        self.port_box = ttk.Combobox(bar, width=34, textvariable=self.port_text)
        self.port_box.pack(side="left", padx=4)
        ttk.Button(bar, text="刷新", width=6, command=self.refresh_ports).pack(side="left")
        ttk.Checkbutton(bar, text="模拟后端", variable=self.sim).pack(side="left", padx=6)
        self.connect_button = ttk.Button(bar, text="连接", width=8, command=self.toggle_connect)
        self.connect_button.pack(side="left", padx=4)
        self.home_button = ttk.Button(bar, text="一键回0", width=9, command=self.home_all)
        self.home_button.pack(side="left", padx=(6, 4))
        self.route_button = ttk.Button(bar, text="开机回基准", width=10, command=self.home_route)
        self.route_button.pack(side="left", padx=(0, 4))
        self.state_label = ttk.Label(bar, text="未连接", foreground="#888888", width=12)
        self.state_label.pack(side="left", padx=6)
        self.fw_label = ttk.Label(bar, text="固件 —")
        self.fw_label.pack(side="left", padx=6)

        self.stop_button = tk.Button(
            bar,
            text="停止全部 (Esc)",
            command=self.emergency_stop,
            bg="#c62828",
            fg="white",
            activebackground="#8e0000",
            activeforeground="white",
            padx=12,
            pady=2,
        )
        self.stop_button.pack(side="right", padx=6)
        self.clear_latch_button = ttk.Button(bar, text="清除停止锁存", command=self.clear_latch)
        self.clear_latch_button.pack(side="right", padx=4)
        self.last_rx_label = ttk.Label(bar, text="最后通信 —")
        self.last_rx_label.pack(side="right", padx=6)
        self.root.bind("<Escape>", lambda _event: self.emergency_stop())

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=4)

        axes_tab = ttk.Frame(self.notebook)
        self.notebook.add(axes_tab, text="四轴控制")
        for column in range(2):
            axes_tab.columnconfigure(column, weight=1)
        for index, addr in enumerate(AXIS_LABELS):
            panel = AxisPanel(axes_tab, self, addr)
            panel.frame.grid(row=index // 2, column=index % 2, sticky="nsew", padx=4, pady=4)
            self.axes[addr] = panel
        gripper_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(gripper_tab, text="舵机夹爪")
        self._build_gripper(gripper_tab)

        param_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(param_tab, text="参数")
        self._build_params(param_tab)

        seq_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(seq_tab, text="动作序列")
        self._build_sequence(seq_tab)

        from vision_import import build_tab
        # 传 self：生成抓取序列时要填入“动作序列”页并写日志
        build_tab(self.notebook, self)

        log_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(log_tab, text="原始日志")
        self._build_log(log_tab)

    def _build_gripper(self, parent: ttk.Frame) -> None:
        grab_default = int(P.PARAM_DEFAULTS["gripper_grab_deg"])
        open_default = int(P.PARAM_DEFAULTS["gripper_open_deg"])
        ttk.Label(
            parent,
            text=(
                "夹爪：%d°=夹紧（180-45，不是 0 度）、%d°=松开，每次 %d°"
                "（以固件 GRIPPER_* / 参数表为准，方向不要反转）"
                % (grab_default, open_default, int(P.PARAM_DEFAULTS["gripper_step_deg"]))
            ),
            foreground="#444444",
        ).pack(anchor="w")
        buttons = ttk.Frame(parent)
        buttons.pack(anchor="w", pady=6)
        for text, command in (
            ("夹紧", lambda: self.send_gripper("grab")),
            ("松开", lambda: self.send_gripper("open")),
            ("收紧一步", lambda: self.send_gripper("step_grab")),
            ("松开一步", lambda: self.send_gripper("step_release")),
        ):
            ttk.Button(buttons, text=text, width=10, command=command).pack(side="left", padx=2)

        angle_row = ttk.Frame(parent)
        angle_row.pack(anchor="w", pady=6)
        ttk.Label(angle_row, text="指定角度 %d~%d：" % (grab_default, open_default)).pack(side="left")
        self.servo_angle = tk.IntVar(value=open_default)
        ttk.Spinbox(
            angle_row, from_=grab_default, to=open_default, width=6, textvariable=self.servo_angle
        ).pack(side="left")
        ttk.Button(angle_row, text="下发角度", command=self.send_servo_angle).pack(side="left", padx=6)
        ttk.Label(
            angle_row, text="（直接下发角度会绕过夹爪内部角度，仅调试用）", foreground="#888888"
        ).pack(side="left")

        self.gripper_status = ttk.Label(parent, text="状态：未知（软件目标值，无到位传感器）")
        self.gripper_status.pack(anchor="w", pady=6)

    def _build_params(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="参数先存 RAM，掉电丢失；参数名要与固件 param_get/param_set 的实现一致。",
            foreground="#444444",
        ).pack(anchor="w")
        self.param_vars = {}
        grid = ttk.Frame(parent)
        grid.pack(anchor="w", pady=6)
        for row, (name, desc) in enumerate(KNOWN_PARAMS):
            ttk.Label(grid, text=f"{name}（{desc}）").grid(row=row, column=0, sticky="w", pady=1)
            var = tk.StringVar(value="")
            self.param_vars[name] = var
            ttk.Entry(grid, width=10, textvariable=var).grid(row=row, column=1, sticky="w", padx=6)
            ttk.Button(
                grid, text="读取", width=6, command=lambda n=name: self.send_param_get(n)
            ).grid(row=row, column=2, padx=1)
            ttk.Button(
                grid, text="写入", width=6, command=lambda n=name: self.send_param_set(n)
            ).grid(row=row, column=3, padx=1)

        custom = ttk.Frame(parent)
        custom.pack(anchor="w", pady=8)
        ttk.Label(custom, text="自定义参数名").pack(side="left")
        self.custom_param = tk.StringVar()
        ttk.Entry(custom, width=16, textvariable=self.custom_param).pack(side="left", padx=4)
        self.custom_value = tk.StringVar()
        ttk.Entry(custom, width=10, textvariable=self.custom_value).pack(side="left", padx=4)
        ttk.Button(custom, text="读取", width=6, command=lambda: self.send_param_get(self.custom_param.get())).pack(side="left")
        ttk.Button(custom, text="写入", width=6, command=lambda: self.send_param_set(self.custom_param.get())).pack(side="left", padx=2)

    def _build_sequence(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        form = ttk.Frame(parent)
        form.grid(row=0, column=0, sticky="ew")
        ttk.Label(form, text="动作").pack(side="left")
        self.seq_kind = tk.StringVar(value="motor_speed")
        ttk.Combobox(
            form,
            width=14,
            state="readonly",
            values=list(S.STEP_KINDS),
            textvariable=self.seq_kind,
        ).pack(side="left", padx=4)
        ttk.Label(form, text="参数").pack(side="left")
        self.seq_args = tk.StringVar(value="1,0,60,1")
        ttk.Entry(form, width=24, textvariable=self.seq_args).pack(side="left", padx=4)
        ttk.Label(form, text="完成条件").pack(side="left")
        self.seq_wait = tk.StringVar(value="ok")
        ttk.Combobox(
            form, width=8, state="readonly", values=("ok", "ms", "reached"), textvariable=self.seq_wait
        ).pack(side="left", padx=4)
        ttk.Label(form, text="等待ms").pack(side="left")
        self.seq_wait_ms = tk.IntVar(value=200)
        ttk.Spinbox(form, from_=0, to=60000, width=6, textvariable=self.seq_wait_ms).pack(side="left")
        ttk.Label(form, text="超时ms").pack(side="left")
        self.seq_timeout = tk.IntVar(value=5000)
        ttk.Spinbox(form, from_=100, to=120000, width=7, textvariable=self.seq_timeout).pack(side="left")
        self.seq_continue = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="超时跳过", variable=self.seq_continue).pack(side="left", padx=4)
        ttk.Button(form, text="添加", width=6, command=self.seq_add).pack(side="left", padx=2)

        self.seq_hint = ttk.Label(parent, text="参数按逗号分隔，顺序见下方提示", foreground="#888888")
        self.seq_hint.grid(row=1, column=0, sticky="w")
        self.seq_kind.trace_add("write", lambda *_: self.seq_hint.configure(
            text=f"{self.seq_kind.get()} 参数：{S.STEP_KINDS.get(self.seq_kind.get(), ('', ''))[1] or '无'}"
        ))

        columns = ("no", "kind", "args", "wait", "timeout")
        self.seq_tree = ttk.Treeview(parent, columns=columns, show="headings", height=10)
        for key, title, width in (
            ("no", "#", 40),
            ("kind", "动作", 140),
            ("args", "参数", 260),
            ("wait", "完成条件", 110),
            ("timeout", "超时ms", 80),
        ):
            self.seq_tree.heading(key, text=title)
            self.seq_tree.column(key, width=width, anchor="w")
        self.seq_tree.grid(row=2, column=0, sticky="nsew", pady=6)

        tools = ttk.Frame(parent)
        tools.grid(row=3, column=0, sticky="ew")
        for text, command in (
            ("删除", self.seq_remove),
            ("上移", lambda: self.seq_move(-1)),
            ("下移", lambda: self.seq_move(1)),
            ("清空", self.seq_clear),
            ("保存", self.seq_save),
            ("加载", self.seq_load),
        ):
            ttk.Button(tools, text=text, width=6, command=command).pack(side="left", padx=2)
        ttk.Separator(tools, orient="vertical").pack(side="left", fill="y", padx=6)
        for text, command in (
            ("运行", self.seq_run),
            ("暂停", self.seq_pause),
            ("继续", self.seq_resume),
            ("中止", self.seq_abort),
        ):
            ttk.Button(tools, text=text, width=6, command=command).pack(side="left", padx=2)
        self.seq_status = ttk.Label(parent, text="序列：空闲", foreground="#444444")
        self.seq_status.grid(row=4, column=0, sticky="w", pady=4)

    def _build_log(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)
        tools = ttk.Frame(parent)
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.auto_scroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(tools, text="自动滚动", variable=self.auto_scroll).pack(side="left")
        ttk.Button(tools, text="清空", width=6, command=self.clear_log).pack(side="left", padx=4)
        ttk.Button(tools, text="保存", width=6, command=self.save_log).pack(side="left")
        self.debug_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="调试模式（按十六进制发原始字节）", variable=self.debug_mode).pack(side="left", padx=8)
        self.raw_text = tk.StringVar()
        ttk.Entry(tools, width=28, textvariable=self.raw_text).pack(side="left")
        ttk.Button(tools, text="发送", width=6, command=self.send_raw).pack(side="left", padx=4)

        self.log_text = tk.Text(parent, wrap="none", height=18, font=("Consolas", 9))
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        for tag, color in (
            ("tx", "#0a4a8a"),
            ("rx", "#444444"),
            ("note", "#8a5a00"),
            ("err", "#b00020"),
            ("event", "#0a6b0a"),
            ("tele", "#666666"),
        ):
            self.log_text.tag_configure(tag, foreground=color)

    def _build_status(self) -> None:
        bar = ttk.Frame(self.root, padding=(6, 2))
        bar.pack(fill="x")
        self.status_label = ttk.Label(bar, text="就绪")
        self.status_label.pack(side="left")
        self.home_label = ttk.Label(bar, text=HR.state_text(self.home_state),
                                    foreground="#888888")
        self.home_label.pack(side="right")

    # ------------------------------------------------------------------
    # 链路
    # ------------------------------------------------------------------
    def refresh_ports(self) -> None:
        ports = list_serial_ports()
        self._ports = ports
        self.port_box.configure(values=[p.label() for p in ports])
        if ports and not self.port_text.get():
            self.port_text.set(ports[0].label())
        self.note(f"发现 {len(ports)} 个串口")

    def _selected_port(self) -> str:
        text = self.port_text.get().strip()
        for port in getattr(self, "_ports", []):
            if port.label() == text:
                return port.device
        return text.split(" ")[0]

    def _make_link(self):
        if self.sim.get():
            self.sim_device = SimulatedDevice(clock=time.monotonic)
            return SimulatedLink(self.sim_device)
        port = self._selected_port()
        if not port:
            raise RuntimeError("没有选择串口")
        return open_best_link(port, BAUDRATE)

    def toggle_connect(self) -> None:
        if self.client.state in (STATE_DISCONNECTED, STATE_LINK_LOST):
            try:
                link = self._make_link()
            except Exception as exc:
                self.note(f"打开链路失败：{exc}")
                return
            self.client.attach(link)
            if self.client.connect():
                self.reconnector.arm()
        else:
            self.reconnector.disarm()
            self.client.disconnect()

    def emergency_stop(self) -> None:
        self.runner.abort("用户按下停止")
        self.client.stop_all()
        self._stop_requested = True
        self.note("已下发停止全部；请确认机械确实停下")

    def clear_latch(self) -> None:
        """解除固件的停止锁存：按过停止（或发过 '!'）之后，必须显式解除才能继续跑运动。"""
        if not self.client.online:
            self.note("未连接：无法清除停止锁存")
            return
        self.client.send("stop_clear")
        self._stop_requested = False
        self.note("已请求解除停止锁存；确认机械停稳、人员撤离后再下发运动")

    # -- 软位置记账 + 一键回 0 ----------------------------------------------
    def _on_motion(self, cmd: str, args: tuple) -> None:
        """累计每条轴的“软位置”，用于没有回读时的“一键回 0”。

        规则：
        - `motor_pos` rel：按方向把脉冲数累加；
        - `motor_pos` abs / `motor_speed` / 急停 / 解除锁存：位置不再确定，标记未知
          （固件把 abs 也按相对下发，速度模式没有终点，所以都不能信）；
        - `motor_zero`（位置清零）：当前位置即新原点，软位置归 0。
        """
        try:
            if cmd == "stop_all":
                # 急停/停止：只有“正在动”的轴位置才会变不确定，静止时保持记账不变。
                if time.monotonic() - getattr(self, "_last_motion_s", 0.0) <= 5.0:
                    for addr in self.pos_known:
                        self.pos_known[addr] = False
                return
            if cmd == "stop_clear":
                return
            addr = int(args[0])
            if addr not in self.soft_pos:
                return
            if cmd == "motor_zero":
                self.soft_pos[addr] = 0
                self.pos_known[addr] = True
            elif cmd == "motor_pos":
                mode = str(args[5]) if len(args) > 5 else "rel"
                if mode != "rel":
                    self.pos_known[addr] = False
                    return
                pulses = abs(int(args[4]))
                negative = bool(int(args[1]))
                self.soft_pos[addr] += (-pulses if negative else pulses)
                self._last_motion_s = time.monotonic()
            elif cmd == "motor_speed":
                self.pos_known[addr] = False
                self._last_motion_s = time.monotonic()
        except (IndexError, TypeError, ValueError):
            pass

    def home_all(self, axes=None) -> None:
        """一键回 0 基准点：按“位置清零”时的原点，把 Z→X→Y→转盘 依次开回来。

        :param axes: 只回其中的轴（如 (4, 5) 只回 X/Y）；None 表示全部四轴。
        """
        if not self.client.online:
            self.note("未连接：无法回 0")
            return
        order = home_order(axes)
        moves = []
        skipped = []
        for addr in order:
            if not self.pos_known.get(addr, False):
                skipped.append(AXIS_LABELS[addr])
                continue
            delta = -self.soft_pos.get(addr, 0)
            if delta:
                moves.append((addr, delta))
        for name in skipped:
            self.note(f"{name} 位置未知（用过速度/绝对模式或重连过），本次跳过；"
                      "请手动挪到原点后点该轴『位置清零』")
        if not moves:
            self.note("所有轴都已在 0 基准点（或位置未知），没有需要回 0 的轴")
            return
        text = "\n".join(f"  {AXIS_LABELS[addr]}：{delta:+d} 脉冲" for addr, delta in moves)
        if not messagebox.askyesno(
            "一键回 0 基准点",
            "将按下面顺序移回 0 基准点：\n\n" + text +
            "\n\n顺序：Z（先抬）→ X → Y → 转盘。\n"
            "0 基准点 = 该轴最近一次『位置清零』的位置；如果从没清零过，"
            "就是上位机启动时的位置。\n"
            "依据是本机记录的相对脉冲累加（没有回读）：中途手动推过轴或步进丢步会有偏差。\n\n"
            "确认运动路径上没有人/物再继续。",
        ):
            return
        self._home_queue = list(moves)
        self.note("开始一键回 0：" + "、".join(AXIS_LABELS[a] for a, _ in moves))
        self._home_step()

    def _home_step(self) -> None:
        if not self._home_queue:
            self.note("一键回 0 完成（各轴软位置已置 0）")
            return
        addr, delta = self._home_queue.pop(0)
        panel = self.axes.get(addr)
        rpm = int(panel.rpm.get()) if panel is not None else 60
        acc = int(panel.acc.get()) if panel is not None else 100
        pulses = abs(delta)
        direction = 0 if delta > 0 else 1
        self.client.send("motor_enable", addr, 1)
        self.client.send("motor_pos", addr, direction, rpm, acc, pulses, "rel")
        self.soft_pos[addr] = 0
        self.note(f"回 0：{AXIS_LABELS[addr]} {delta:+d} 脉冲"
                  f"（方向 {'正' if direction == 0 else '负'}，{rpm}rpm）")
        per_second = max(1, rpm) * AxisPanel.PULSES_PER_REV / 60.0
        wait_ms = int(pulses / per_second * 1000) + 900
        self.root.after(max(800, min(wait_ms, 30000)), self._home_step)

    # -- 开机回基准：固定路线 → 终点清零 -------------------------------------
    def home_route(self) -> None:
        """开机回基准：先人工放到固定开机位，再按 home_route.json 走一遍，终点清零。"""
        if not self.client.online:
            self.note("未连接：无法执行开机回基准")
            return
        cfg = HR.load()
        if not cfg.get('enabled'):
            self.note("开机回基准已在 home_route.json 里关闭（enabled=false）")
            return
        lines = HR.describe(cfg)
        if not lines:
            self.note("开机回基准：路线里没有要走的步（pulses 全为 0），请改 home_route.json")
            return
        zero_names = "、".join(AXIS_LABELS.get(a, str(a)) for a in cfg['zero_axes']) or "（不清零）"
        if not messagebox.askyesno(
            "开机回基准",
            (cfg.get('boot_tip') or HR.DEFAULTS['boot_tip']) + "\n\n"
            "将按固定路线执行：\n" + "\n".join(lines) +
            f"\n\n走到终点后清零：{zero_names}\n"
            "（终点即视觉标定/抓取使用的 0 基准）\n\n"
            "确认路径上没人、没物，且龙门确实在开机位，再继续。",
        ):
            return
        self._route_queue = [s for s in cfg['steps'] if s['pulses'] > 0]
        self._route_cfg = cfg
        self._route_done = 0
        self.note("开始开机回基准：" + "、".join(AXIS_LABELS.get(s['axis'], str(s['axis']))
                                             for s in self._route_queue))
        self._home_route_step()

    def _home_route_step(self) -> None:
        if not self._route_queue:
            self._home_route_finish()
            return
        step = self._route_queue.pop(0)
        addr = step['axis']
        self.client.send("motor_enable", addr, 1)
        self.client.send("motor_pos", addr, step['dir'], step['rpm'], step['acc'],
                         step['pulses'], "rel")
        self._route_done += 1
        self.note("回基准 %d/%d：%s %s %d 脉冲（%drpm）"
                  % (self._route_done,
                     self._route_done + len(self._route_queue),
                     AXIS_LABELS.get(addr, addr),
                     "正" if step['dir'] == 0 else "负",
                     step['pulses'], step['rpm']))
        self.root.after(HR.step_ms(step, self._route_cfg.get('settle_ms', 0)),
                        self._home_route_step)

    def _home_route_finish(self) -> None:
        cfg = getattr(self, '_route_cfg', HR.load())
        zero_axes = list(cfg.get('zero_axes') or [])
        for addr in zero_axes:
            self.client.send("motor_zero", addr)
            self.soft_pos[addr] = 0
            self.pos_known[addr] = True
        HR.save_state(True, zero_axes, self._route_done)
        self.home_state = HR.load_state()
        try:
            self.home_label.configure(text=HR.state_text(self.home_state),
                                      foreground="#0a6b0a")
        except (AttributeError, tk.TclError):
            pass
        names = "、".join(AXIS_LABELS.get(a, str(a)) for a in zero_axes) or "（无）"
        self.note(f"开机回基准完成：{self._route_done} 步，已把 {names} 清零；"
                  "现在可以开始视觉标定/抓取")

    def send_gripper(self, action: str) -> None:
        self.client.send("gripper", action, timeout_ms=2000)
        self.gripper_status.configure(
            text=f"状态：已请求 {action}（无到位传感器，'等待结束'只代表 200ms 等待走完）"
        )

    def send_servo_angle(self) -> None:
        self.client.send("servo_angle", self.servo_angle.get())

    def send_param_get(self, name: str) -> None:
        if not name:
            return
        self.client.send("param_get", name)

    def send_param_set(self, name: str) -> None:
        if not name:
            return
        value = self.param_vars.get(name)
        text = value.get() if value is not None else self.custom_value.get()
        if text == "":
            self.note(f"参数 {name} 没有填值")
            return
        self.client.send("param_set", name, text)
        if name == "readback_mask":
            # 回读轴名单是 hello 里带回来的，改完掩码要重新握手界面才会刷新
            self.note("readback_mask 已下发，重新握手刷新回读轴名单")
            self.client.begin_handshake()

    def send_raw(self) -> None:
        if not self.debug_mode.get():
            self.note("先勾选调试模式才会发送原始字节")
            return
        text = self.raw_text.get()
        if "!" in text:
            self.note("原始模式不允许发送停止字符 '!'，请用停止按钮")
            return
        self.client.send_raw(text)

    # ------------------------------------------------------------------
    # 序列
    # ------------------------------------------------------------------
    def seq_add(self) -> None:
        kind = self.seq_kind.get()
        raw = self.seq_args.get().strip()
        args = []
        if raw:
            for item in raw.split(","):
                item = item.strip()
                if item == "":
                    continue
                try:
                    args.append(int(item))
                except ValueError:
                    args.append(item)
        step = S.Step(
            kind=kind,
            args=args,
            wait=self.seq_wait.get(),
            wait_ms=self.seq_wait_ms.get(),
            timeout_ms=self.seq_timeout.get(),
            on_timeout=S.ON_TIMEOUT_CONTINUE if self.seq_continue.get() else S.ON_TIMEOUT_ABORT,
        )
        problems = step.validate(self.client.readback_axes)
        if problems:
            self.note("这一步有问题：" + "；".join(problems))
            return
        self.sequence.steps.append(step)
        self.seq_refresh()

    def seq_remove(self) -> None:
        selection = self.seq_tree.selection()
        if not selection:
            return
        index = int(self.seq_tree.item(selection[0], "values")[0]) - 1
        if 0 <= index < len(self.sequence.steps):
            self.sequence.steps.pop(index)
            self.seq_refresh()

    def seq_move(self, delta: int) -> None:
        selection = self.seq_tree.selection()
        if not selection:
            return
        index = int(self.seq_tree.item(selection[0], "values")[0]) - 1
        target = index + delta
        if 0 <= index < len(self.sequence.steps) and 0 <= target < len(self.sequence.steps):
            steps = self.sequence.steps
            steps[index], steps[target] = steps[target], steps[index]
            self.seq_refresh()

    def seq_clear(self) -> None:
        self.sequence.steps.clear()
        self.seq_refresh()

    def seq_save(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("序列 JSON", "*.json")], initialfile="sequence.json"
        )
        if not path:
            return
        self.sequence.save(path)
        self.note(f"序列已保存：{path}")

    def seq_load(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("序列 JSON", "*.json")])
        if not path:
            return
        try:
            self.sequence = S.Sequence.load(path)
        except Exception as exc:
            self.note(f"加载失败：{exc}")
            return
        self.seq_refresh()
        self.note(f"序列已加载：{path}")

    def seq_refresh(self) -> None:
        self.seq_tree.delete(*self.seq_tree.get_children())
        for index, step in enumerate(self.sequence.steps, 1):
            wait = step.wait
            if wait == S.WAIT_TIME:
                wait = f"ms:{step.wait_ms}"
            self.seq_tree.insert(
                "",
                "end",
                values=(index, step.kind, ",".join(str(a) for a in step.args), wait, step.timeout_ms),
            )

    def seq_run(self) -> None:
        problems = self.runner.start(self.sequence)
        if problems:
            self.note("序列无法运行：" + "；".join(problems))
            return
        self.seq_status.configure(text=f"序列：{self.runner.message}")

    def seq_pause(self) -> None:
        self.runner.pause()

    def seq_resume(self) -> None:
        self.runner.resume()

    def seq_abort(self) -> None:
        self.runner.abort("用户中止")
        self.client.stop_all()

    def _runner_message(self, message: str) -> None:
        self.seq_status.configure(text=f"序列：{message}")
        self.note(f"序列 {message}")

    # ------------------------------------------------------------------
    # 事件与刷新
    # ------------------------------------------------------------------
    def _pump(self) -> None:
        self.reconnector.tick()
        for event in self.client.poll_events(300):
            self._on_event(event)
            if event.kind in ("reply", "err", "timeout", "event"):
                self.runner.notify(event)
        self.runner.tick()
        self._refresh_state()
        for panel in self.axes.values():
            panel.refresh_readback()
        self.root.after(POLL_MS, self._pump)

    def _on_event(self, event) -> None:
        if event.kind == "tele":
            pairs = event.data.get("pairs", {})
            try:
                addr = int(pairs.get("addr", "0"))
            except ValueError:
                addr = 0
            panel = self.axes.get(addr)
            if panel is not None:
                panel.update_telemetry(pairs)
            self._log(event.text, "tele")
            return
        if event.kind == "reply":
            pairs = event.data.get("pairs", {})
            cmd = event.data.get("cmd")
            if cmd == "hello":
                # 新会话：驱动器可能已掉电，绝对位置基准一律作废。
                for panel in self.axes.values():
                    panel.reset_reference()
            if cmd == "param_get" and "name" in pairs:
                name = pairs.get("name")
                if name in self.param_vars:
                    self.param_vars[name].set(pairs.get("value", ""))
                self.custom_value.set(pairs.get("value", ""))
        elif event.kind == "err":
            code = event.data.get("code")
            if code == "E13":
                self._log("绝对位置被拒：该轴位置基准无效，先点该轴『位置清零』建立基准", "warn")
            elif code == "E04":
                self._log("查询超时：该轴没有接回读线（TXD→PA3），或驱动器没回包", "warn")
        self._log(event.text, event.kind)

    def _refresh_state(self) -> None:
        state = self.client.state
        text = STATE_TEXT.get(state, state)
        color = {
            STATE_ONLINE: "#0a6b0a",
            STATE_LINK_LOST: "#b00020",
            STATE_DISCONNECTED: "#888888",
        }.get(state, "#8a5a00")
        self.state_label.configure(text=text, foreground=color)
        self.connect_button.configure(
            text="断开" if state not in (STATE_DISCONNECTED, STATE_LINK_LOST) else "连接"
        )
        self.fw_label.configure(
            text=f"固件 {self.client.fw_version or '—'} 启动 {self.client.boot_id or '—'}"
        )
        if self.client.last_rx_ms:
            age = max(0, self.client.now_ms() - self.client.last_rx_ms)
            self.last_rx_label.configure(text=f"最后通信 {age / 1000:.1f}s 前")
        else:
            self.last_rx_label.configure(text="最后通信 —")
        self.status_label.configure(
            text=(
                f"状态 {text}｜发送 {self.client.tx_count} 帧｜接收 {self.client.rx_count} 帧"
                f"｜坏帧 {self.client.bad_frame_count}｜超长 {self.client.oversize_count}"
                f"{'｜已停止锁存' if self.client.latched else ''}"
            )
        )
        self.seq_status.configure(text=f"序列：{self.runner.message or self.runner.state}")

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _log(self, text: str, tag: str = "note") -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        self.log_text.insert("end", line + "\n", tag)
        self._log_lines += 1
        if self._log_lines > LOG_MAX_LINES:
            self.log_text.delete("1.0", "200.0")
            self._log_lines -= 200
        if self.auto_scroll.get():
            self.log_text.see("end")

    def note(self, text: str) -> None:
        self._log(text, "note")

    def clear_log(self) -> None:
        self.log_text.delete("1.0", "end")
        self._log_lines = 0

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="host_log.txt")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.log_text.get("1.0", "end"))
        self.note(f"日志已保存：{path}")

    def _on_close(self) -> None:
        try:
            if self.client.online:
                self.client.stop_all()
                time.sleep(0.1)
            self.reconnector.disarm()
            self.client.disconnect("关闭界面")
            self.client.stop_service()
        finally:
            self.root.destroy()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="机器人抓取系统上位机")
    parser.add_argument("--port", help="串口设备名，例如 COM5")
    parser.add_argument("--sim", action="store_true", help="启动即用模拟后端")
    parser.add_argument("--connect", action="store_true", dest="autoconnect", help="启动后自动连接")
    parser.add_argument("--selftest", action="store_true", help="只构建界面、跑几轮事件循环后退出")
    args = parser.parse_args(argv)

    root = tk.Tk()
    app = App(root, args)
    if args.selftest:
        root.after(600, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
