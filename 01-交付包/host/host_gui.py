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
import approach_route as AR
import journey as J  # noqa: E402
import pi_link  # noqa: E402
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
    "gripper_ready_deg": "夹爪预备角度(150=待抓)",
    "gripper_grab_deg": "夹爪夹紧角度",
    "gripper_step_deg": "夹爪单步角度",
    "gripper_settle_ms": "夹爪等待 ms",
    "gripper_hold_max_ms": "夹爪最多保持 ms(0=不卸力)",
    "heartbeat_ms": "心跳周期 ms",
    "link_timeout_ms": "失联停车 ms",
    "query_timeout_ms": "查询超时 ms",
    "telemetry_ms": "遥测周期 ms",
    "frame_gap_ms": "电机帧间隔 ms",
    "readback_mask": "回读轴掩码(1/3/4/5=1/2/4/8)",
    "counts_per_rev": "编码器每圈 counts(标定)",
    "goto_tol_counts": "闭环容差 counts",
    "goto_rpm": "闭环首段 RPM",
    "goto_final_rpm": "闭环补差 RPM",
    "goto_max_passes": "闭环最多补几轮",
    "goto_max_ms": "闭环超时 ms(0=自动)",
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
        #: 勾上后「相对走」改用 motor_goto：固件用遥测 counts 自己闭环，ok 回来时已校验到位。
        self.loop = tk.BooleanVar(value=False)
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
            row3, text="闭环", variable=self.loop
        ).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            row3, text="订阅遥测", variable=self.tele, command=self.toggle_telemetry
        ).pack(side="left", padx=(4, 0))

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
        if mode == "rel" and self.loop.get():
            # 闭环 goto：固件自己读遥测 counts 补差，回复里带 err/passes/elapsed_ms。
            signed = pulses if not self.direction.get() else -pulses
            self.app.client.goto(self.addr, signed)
            self.app.note(
                "%s 闭环 goto %+d 脉冲（%s）；到位后应答里会给 err/passes"
                % (AXIS_LABELS.get(self.addr, self.addr), signed, "负向" if signed < 0 else "正向")
            )
            return
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
        # 软位置：没有回读时用相对脉冲累加记账（标定记录爪位、绝对位置换算都靠它）。motor_pos(rel) 累加；
        # 速度模式/绝对模式/断线后标记未知，不做猜测。
        self.soft_pos = {addr: 0 for addr in AXIS_LABELS}
        self.pos_known = {addr: True for addr in AXIS_LABELS}
        self._last_motion_s = 0.0
        self.client.motion_hook = self._on_motion
        # 视觉页（由 main.py 挂上来）：连到派时自动开双画面，像连串口一样省事
        self.vision_app = None
        self._vision_auto_started = False
        self._pi_entries = pi_link.entries()      # 上次连过的派，先垫上
        self._pi_remote = bool(os.environ.get("ROBOT_PI"))   # --pi 启动时也是 True
        # 接近段示教：整机上电起点 → 抓取位，记 X/Y/Z 三轴的相对位移，之后自动重放
        self.approach = AR.load()
        self._approach_recording = False
        self._approach_running = False
        self._approach_queue: list[dict] = []
        self._approach_cb = None
        self._approach_rpm_text = tk.StringVar(value="0")
        self._approach_auto = tk.BooleanVar(value=True)
        self._approach_status = None
        self._speed_t0: dict = {}      # 速度模式点动的开始时刻：(t0, dir, rpm, acc)
        # 回上电初始位：自动记录本次上电后的“所有移动”，点一下按钮就倒序反向走回去
        self.journey = J.load()
        self._journey_boot = self.journey.get("boot")
        self._journey_running = False
        self._journey_queue: list[dict] = []
        self._journey_status = None

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
        self._scan_pi_async()                 # 后台找派，找到就并入下拉框
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
        self._build_approach(axes_tab, row=(len(AXIS_LABELS) + 1) // 2)
        self._build_journey(axes_tab, row=(len(AXIS_LABELS) + 1) // 2 + 1)
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

    # -- 接近段示教（X/Y/Z：整机上电起点 → 抓取位） ---------------------------
    def _build_approach(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(
            parent, text="接近段示教（X/Y/Z：整机上电起点 → 抓取位，托盘不参与）")
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", padx=4, pady=(6, 8))
        line1 = ttk.Frame(frame)
        line1.pack(fill="x", padx=6, pady=4)
        ttk.Button(line1, text="开始示教", width=9,
                   command=self.approach_begin).pack(side="left", padx=2)
        self.approach_end_button = ttk.Button(
            line1, text="结束并保存", width=11, command=self.approach_end)
        self.approach_end_button.pack(side="left", padx=2)
        ttk.Button(line1, text="撤销上一段", width=10,
                   command=self.approach_undo).pack(side="left", padx=2)
        ttk.Button(line1, text="清空", width=6,
                   command=self.approach_clear).pack(side="left", padx=2)
        ttk.Button(line1, text="执行接近段", width=11,
                   command=self.approach_run).pack(side="left", padx=(14, 2))
        line2 = ttk.Frame(frame)
        line2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Checkbutton(line2, text="运行序列/抓取前自动执行接近段",
                        variable=self._approach_auto).pack(side="left")
        ttk.Label(line2, text="重放转速（0=沿用示教）").pack(side="left", padx=(14, 4))
        ttk.Entry(line2, textvariable=self._approach_rpm_text, width=6).pack(side="left")
        self._approach_status = ttk.Label(line2, text="", foreground="#555555")
        self._approach_status.pack(side="left", padx=12)
        self.approach_refresh()

    def approach_refresh(self) -> None:
        if self._approach_status is None:
            return
        text = AR.describe(self.approach)
        if self.approach.get("saved_at"):
            text += f"｜已保存 {self.approach['saved_at']}"
        if self._approach_recording:
            text += "｜● 记录中"
        if self._approach_running:
            text += "｜▶ 正在执行"
        try:
            self._approach_status.configure(
                text=text,
                foreground="#c62828" if (self._approach_recording or self._approach_running)
                else "#555555")
        except tk.TclError:      # pragma: no cover - 无显示环境
            pass

    def approach_begin(self) -> None:
        """开始示教：之后每一次点动（X/Y/Z）都会被按顺序记成一段。"""
        if not self.client.online:
            self.note("未连接：先连接控制板再示教")
            return
        if self._approach_running:
            self.note("接近段正在执行，等它跑完再示教")
            return
        steps = self.approach.get("steps") or []
        if steps and not messagebox.askyesno(
                "开始示教",
                f"已有 {len(steps)} 段路线。\n\n"
                "继续示教会把新动作**追加**在后面（想从头来先点『清空』）。\n\n继续吗？"):
            return
        self._approach_recording = True
        try:
            self.approach_end_button.configure(text="结束并保存 ●")
        except tk.TclError:      # pragma: no cover
            pass
        self.approach_refresh()
        self.note("开始示教：用四轴控制页的点动按钮把 X/Y/Z 从整机上电的初始位挪到抓取位。"
                  "『相对走』每点一次记一段；『绝对走到此』按软位置换算成一段；"
                  "『常用走』要等停下（停止/急停）才结算成一段。托盘的点动不记。")

    def approach_end(self) -> None:
        """结束示教并落盘。"""
        if not self._approach_recording:
            self.note("当前没在示教（先点『开始示教』）")
            return
        self._approach_recording = False
        try:
            self.approach_end_button.configure(text="结束并保存")
        except tk.TclError:      # pragma: no cover
            pass
        steps = self.approach.get("steps") or []
        if not steps:
            self.approach_refresh()
            self.note("示教结束，但一段都没记到：示教期间没有出现过 X/Y/Z 的相对点动")
            return
        try:
            path = AR.save(self.approach)
        except Exception as exc:             # noqa: BLE001
            self.note(f"保存接近段失败：{exc}")
            return
        self.approach_refresh()
        self.note(f"已保存接近段：{path.name}（{AR.describe(self.approach)}，"
                  f"预计 {AR.estimate_ms(self.approach) / 1000:.1f}s 走完）。"
                  "以后运行序列/抓取前会自动先走这一段（可在勾选框关掉）")

    def _approach_record(self, axis: int, direction: int, rpm: int, acc: int, pulses: int) -> None:
        """示教期间每发生一次 X/Y/Z 相对点动就记一段（由 _on_motion 调用）。"""
        try:
            AR.add_step(self.approach, axis, direction, rpm, acc, abs(int(pulses)))
        except ValueError as exc:
            self.note(f"这一段没记进接近段：{exc}")
            return
        self.approach_refresh()
        # 每记一段都给可见反馈：以前只更新状态行，用户以为“没记上”
        name = AR.AXIS_NAMES.get(int(axis), str(axis))
        sign = "-" if int(direction) else "+"
        self.note("已记录第 %d 段：%s %s%d 脉冲（%d RPM）"
                  % (len(self.approach.get("steps") or []), name, sign,
                     abs(int(pulses)), int(rpm)))

    def approach_undo(self) -> None:
        AR.undo(self.approach)
        self.approach_refresh()
        self.note(f"已撤销最后一段（{AR.describe(self.approach)}）")

    def approach_clear(self) -> None:
        if not (self.approach.get("steps") or []):
            self.note("接近段本来就是空的")
            return
        if not messagebox.askyesno("清空接近段", "把当前示教的段全清掉？（已保存的文件不动，"
                                   "再点『结束并保存』才会覆盖）"):
            return
        AR.clear(self.approach)
        self.approach_refresh()
        self.note("接近段已清空（还没保存）")

    def approach_run(self) -> None:
        """手动执行一次接近段。"""
        if not self._approach_start_replay(None):
            return
        self.note("手动执行接近段")

    def _approach_should_auto(self) -> bool:
        """运行序列/抓取前是否要先走接近段。"""
        try:
            auto = bool(self._approach_auto.get())
        except tk.TclError:      # pragma: no cover
            auto = False
        if not auto or self._approach_running or self._approach_recording:
            return False
        return bool(self.approach.get("steps") or [])

    def _approach_replay_rpm(self) -> int:
        try:
            return max(0, int(float(self._approach_rpm_text.get() or 0)))
        except (ValueError, tk.TclError):
            return 0

    def _approach_start_replay(self, callback) -> bool:
        """执行接近段；callback 在跑完后调用（用于「跑完再开始序列」）。"""
        if self._approach_running:
            self.note("接近段已经在执行了")
            return False
        if not self.client.online:
            self.note("未连接：无法执行接近段")
            return False
        problems = AR.validate(self.approach)
        if problems:
            self.note("接近段不能执行：" + "；".join(problems))
            return False
        self._approach_queue = AR.build_steps(self.approach, self._approach_replay_rpm())
        self._approach_cb = callback
        self._approach_running = True
        self.approach_refresh()
        if self.client.latched:
            self.client.send("stop_clear", timeout_ms=1000)
        self.note(f"开始执行接近段：{AR.describe(self.approach)}，"
                  f"约 {AR.estimate_ms(self.approach) / 1000:.1f}s（开环，确认路径上没人没物）")
        self.root.after(100, self._approach_step)
        return True

    def _approach_step(self) -> None:
        if not self._approach_running:
            return
        if not self.client.online:
            self.note("接近段中断：链路断了")
            self._approach_finish()
            return
        if not self._approach_queue:
            self._approach_finish()
            return
        step = self._approach_queue.pop(0)
        axis = int(step["axis"])
        self.client.send("motor_enable", axis, 1)
        self.client.send("motor_pos", axis, int(step["dir"]), int(step["rpm"]),
                         int(step["acc"]), int(step["pulses"]), "rel")
        self.root.after(AR.step_ms(step), self._approach_step)

    def _approach_finish(self) -> None:
        self._approach_running = False
        self._approach_queue = []
        callback, self._approach_cb = self._approach_cb, None
        self.approach_refresh()
        self.note("接近段执行完毕（现在应在抓取位）")
        if callback is not None:
            self.root.after(200, callback)


    def _build_gripper(self, parent: ttk.Frame) -> None:
        grab_default = int(P.PARAM_DEFAULTS["gripper_grab_deg"])
        open_default = int(P.PARAM_DEFAULTS["gripper_open_deg"])
        ready_default = int(P.PARAM_DEFAULTS["gripper_ready_deg"])
        ttk.Label(
            parent,
            text=(
                "夹爪三档：%d°=夹紧、%d°=预备位（连上后自动打到这里）、%d°=完全张开（上电/松开）"
                "；每次单步 %d°（以固件 GRIPPER_* / 参数表为准，方向不要反转）"
                % (
                    grab_default,
                    ready_default,
                    open_default,
                    int(P.PARAM_DEFAULTS["gripper_step_deg"]),
                )
            ),
            foreground="#444444",
        ).pack(anchor="w")
        buttons = ttk.Frame(parent)
        buttons.pack(anchor="w", pady=6)
        self.gripper_buttons = {}
        for text, action in (
            ("夹紧", "grab"),
            ("预备(%d)" % ready_default, "ready"),
            ("松开", "open"),
            ("收紧一步", "step_grab"),
            ("松开一步", "step_release"),
        ):
            button = ttk.Button(
                buttons, text=text, width=10, command=lambda a=action: self.send_gripper(a)
            )
            button.pack(side="left", padx=2)
            self.gripper_buttons[action] = button

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

    # ------------------------------------------------------------------
    # 链路
    # ------------------------------------------------------------------
    def refresh_ports(self) -> None:
        """串口 + 派一起列出来；派条目形如「派 robot-pi 192.168.100.198（…）」。

        派先放前面：插着派时用户最想选的往往是它，而不是蓝牙虚拟串口。
        """
        pi_list = list(getattr(self, "_pi_entries", [])) or pi_link.entries()
        com_ports = list_serial_ports()
        ports = pi_list + com_ports
        self._ports = ports
        self.port_box.configure(values=[p.label() for p in ports])
        if ports and not self.port_text.get():
            self.port_text.set(ports[0].label())
        self.note(f"发现 {len(pi_list)} 个派、{len(com_ports)} 个串口")

    def _scan_pi_async(self) -> None:
        """后台 UDP 发现派，回来后并入下拉框（不阻塞界面）。"""

        def _done(found):
            try:
                self.root.after(0, lambda: self._apply_pi_scan(found))
            except Exception:                    # 界面已关闭
                pass

        pi_link.discover_async(_done, timeout=1.2)

    def _apply_pi_scan(self, found) -> None:
        merged = pi_link.merge(found, pi_link.load_last())
        if merged:
            self._pi_entries = merged
            self.refresh_ports()
            self.note("发现派：" + "、".join(ep.host for ep in merged))
        else:
            self.note("没发现派（UDP 广播可能被 AP 隔离）；可在端口框手填 tcp://IP:5058")

    def _selected_endpoint(self):
        """选中的对象（可能是 PortInfo 也可能是 PiEndpoint）；手填文本返回 None。"""
        text = self.port_text.get().strip()
        for port in getattr(self, "_ports", []):
            if port.label() == text:
                return port
        return None

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
        # 选的是派（或手填 tcp://）→ 视觉也要跟着切到派端相机，
        # 否则会出现「控制连上派、相机还在笔记本」这种半连状态。
        ep = self._selected_endpoint()
        if not isinstance(ep, pi_link.PiEndpoint):
            ep = pi_link.endpoint_from_text(port)
        if ep is not None:
            pi_link.apply_env(ep)
            self._pi_remote = True
            self._vision_auto_started = False
            self.note(f"端口切换为派 {ep.host}：相机走 {ep.host}:{ep.img_port}，"
                      f"串口走 {ep.device}")
        else:
            pi_link.clear_env()
            self._pi_remote = False
        return open_best_link(port, BAUDRATE)

    def toggle_connect(self) -> None:
        if self.client.state in (STATE_DISCONNECTED, STATE_LINK_LOST):
            self._last_fail_msg = ""          # 重新尝试，清掉上一次的失败提示
            try:
                link = self._make_link()
            except Exception as exc:
                self.note(f"打开链路失败：{exc}")
                self._warn_link_failed(f"{type(exc).__name__}: {exc}")
                return
            self.client.attach(link)
            if self.client.connect():
                self.reconnector.arm()
            else:
                # 以前这里只写一条 note，用户在大窗口里看不到，会以为「点了没反应」
                self._warn_link_failed(self.client.last_error or "未知原因（链路打不开）")
        else:
            self.reconnector.disarm()
            self.client.disconnect()

    def _warn_link_failed(self, reason: str) -> None:
        """连接失败时给出看得见的反馈：状态栏红字 + 提示音 + 弹窗 + 排查建议。"""
        self._last_fail_msg = reason
        try:
            self.state_label.configure(text="连接失败", foreground="#c62828")
        except Exception:
            pass
        try:
            self.bell()
        except Exception:
            pass
        port = ""
        try:
            port = self._selected_port()
        except Exception:
            pass
        hints = []
        if port.startswith("tcp://") or "派" in port:
            hints.append("① 派端 agent 是否在跑：派上执行 `systemctl status robotpi`"
                         "（没装就 `~/robot1021/start_agent.sh`）")
            hints.append("② 电脑和派是否同一 WiFi；`ping <派IP>`、"
                         f"`Test-NetConnection <派IP> -Port 5058`")
            hints.append("③ 端口被防火墙挡：管理员跑 `10.21\\net\\firewall-allow.ps1`")
        else:
            hints.append("① 串口是否被别的程序占用（MobaXterm / Keil / 串口助手）")
            hints.append("② 设备是否插好、下拉框里的 COM 号是否选对")
        messagebox.showwarning(
            "连接失败",
            f"打不开链路：{reason}\n\n所选端口：{port or '（空）'}\n\n" + "\n".join(hints),
        )
        self.note(f"连接失败：{reason}")

    def emergency_stop(self) -> None:
        self.runner.abort("用户按下停止")
        self.client.stop_all()
        self._stop_requested = True
        # 接近段重放是 root.after 驱动的，必须一起取消，否则停完还会继续动
        self._approach_queue = []
        self._approach_running = False
        self._approach_cb = None
        self.approach_refresh()
        self._journey_queue = []
        self._journey_running = False
        self.journey_refresh()
        self.note("已下发停止全部；请确认机械确实停下")

    def clear_latch(self) -> None:
        """解除固件的停止锁存：按过停止（或发过 '!'）之后，必须显式解除才能继续跑运动。"""
        if not self.client.online:
            self.note("未连接：无法清除停止锁存")
            return
        self.client.send("stop_clear")
        self._stop_requested = False
        self.note("已请求解除停止锁存；确认机械停稳、人员撤离后再下发运动")

    # -- 软位置记账（无回读时用相对脉冲累加） --------------------------------
    def _on_motion(self, cmd: str, args: tuple) -> None:
        """累计每条轴的“软位置”：没有回读时用它做标定记录与绝对位移换算。

        规则：
        - `motor_pos` rel：按方向把脉冲数累加；
        - `motor_pos` abs / `motor_speed` / 急停 / 解除锁存：位置不再确定，标记未知
          （固件把 abs 也按相对下发，速度模式没有终点，所以都不能信）；
        - `motor_zero`（位置清零）：当前位置即新原点，软位置归 0。
        """
        try:
            if cmd == "stop_all":
                # 先给速度模式点动收尾（它没有“终点”，只能拿运行时长折算脉冲）
                self._approach_flush_speed(None)
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
                pulses = abs(int(args[4]))
                direction = int(args[1])
                rpm = int(args[2])
                acc = int(args[3])
                negative = bool(direction)
                self.soft_pos[addr] += (-pulses if negative else pulses)
                self.pos_known[addr] = True
                self._last_motion_s = time.monotonic()
                if mode != "rel":
                    # 固件把「绝对位置」也按相对移动执行（control_apply_position(..., absolute=0)），
                    # 所以实际走的就是 pulses；记账必须按真实动作，不能按目标位置差。
                    self.note("%s『绝对走到此』：固件按相对执行，实际走 %d 脉冲（%s向）"
                              % (AXIS_LABELS.get(addr, addr), pulses, "负" if negative else "正"))
                self._journey_add(addr, direction, rpm, acc, pulses)
                if self._approach_recording and not self._approach_running and addr in AR.AXES:
                    self._approach_record(addr, direction, rpm, acc, pulses)
            elif cmd == "motor_goto":
                # args = (axis, pulses[, tol[, rpm]])，pulses 带符号
                signed = int(args[1])
                pulses = abs(signed)
                direction = 1 if signed < 0 else 0
                rpm = int(args[3]) if len(args) > 3 else int(P.PARAM_DEFAULTS["goto_rpm"])
                acc = 0
                self.soft_pos[addr] += (-pulses if direction else pulses)
                self.pos_known[addr] = True
                self._last_motion_s = time.monotonic()
                self._journey_add(addr, direction, rpm, acc, pulses)
                if self._approach_recording and not self._approach_running and addr in AR.AXES:
                    self._approach_record(addr, direction, rpm, acc, pulses)
            elif cmd == "motor_speed":
                # 「常用走 / 连续走」：没有终点，记下开始时间，等停下（motor_stop / stop_all）再折算
                self.pos_known[addr] = False
                self._last_motion_s = time.monotonic()
                self._speed_t0[addr] = (time.monotonic(), int(args[1]), int(args[2]), int(args[3]))
            elif cmd == "motor_stop":
                self._approach_flush_speed(addr)
                self._last_motion_s = time.monotonic()
        except (IndexError, TypeError, ValueError):
            pass

    def _approach_flush_speed(self, addr) -> None:
        """把「速度模式走了多久」折算成一段（addr=None 表示所有轴一起收尾）。"""
        if not self._speed_t0:
            return
        targets = list(self._speed_t0) if addr is None else [addr]
        for axis in targets:
            started = self._speed_t0.pop(axis, None)
            if started is None:
                continue
            t0, direction, rpm, acc = started
            pulses = int(max(1, rpm) * AR.PULSES_PER_REV / 60.0 * (time.monotonic() - t0))
            if pulses <= 0:
                continue
            self._journey_add(axis, direction, rpm, acc, pulses)
            if self._approach_recording and not self._approach_running and axis in AR.AXES:
                self._approach_record(axis, direction, rpm, acc, pulses)

    # -- 回上电初始位：自动记录本次上电后的全部移动 ---------------------------
    def _build_journey(self, parent: ttk.Frame, row: int) -> None:
        box = ttk.LabelFrame(
            parent, text="回上电初始位（自动记录本次上电后的全部移动，四轴都记）")
        box.grid(row=row, column=0, columnspan=2, sticky="we", padx=6, pady=(0, 6))
        line = ttk.Frame(box)
        line.pack(fill="x", padx=6, pady=(4, 2))
        self.journey_status = ttk.Label(line, text=J.describe(self.journey))
        self.journey_status.pack(side="left")
        ttk.Button(line, text="清空记录", command=self.journey_clear).pack(side="right")
        ttk.Button(line, text="原路返回初始位",
                   command=self.journey_return).pack(side="right", padx=6)
        ttk.Label(
            box,
            text="按记录倒序、方向取反重放一遍。开环无回读，返回后请目视确认确实回到上电位置。",
            foreground="#666666").pack(fill="x", padx=6, pady=(0, 6))
        self.journey_refresh()

    def journey_refresh(self) -> None:
        try:
            self.journey_status.configure(text=J.describe(self.journey))
        except (AttributeError, tk.TclError):      # pragma: no cover - 无显示环境
            pass

    def _journey_add(self, axis: int, direction: int, rpm: int, acc: int, pulses: int) -> None:
        """记一段真实发生的位移；返回动作自己引起的位移不记（否则会越滚越多）。"""
        if self._journey_running:
            return
        try:
            J.append(self.journey, axis, direction, rpm, acc, pulses)
        except ValueError:
            return
        try:
            J.save(self.journey)
        except OSError as exc:
            self.note(f"移动记录写盘失败（不影响运动）：{exc}")
        self.journey_refresh()

    def journey_clear(self) -> None:
        if not (self.journey.get("steps") or []):
            self.note("移动记录本来就是空的")
            return
        if not messagebox.askyesno(
                "清空移动记录",
                "清空本次上电后的移动记录？\n\n"
                "清空后就只能回到“现在这个位置”，不能再原路返回更早的位置。"):
            return
        J.clear(self.journey)
        try:
            J.save(self.journey)
        except OSError:
            pass
        self.journey_refresh()
        self.note("已清空移动记录（从当前位置重新开始记）")

    def journey_return(self) -> None:
        """原路返回：把记录倒序 + 方向取反，逐段走回去。"""
        if not self.client.online:
            self.note("未连接：先连上控制板")
            return
        if self._journey_running or self._approach_running:
            self.note("还有动作在跑，等它跑完再返回")
            return
        problems = J.validate(self.journey)
        if problems:
            self.note("不能返回：" + "；".join(problems))
            return
        steps = J.reverse_steps(self.journey)
        if not messagebox.askyesno(
                "原路返回初始位",
                f"将按记录倒序走 {len(steps)} 段，回到本次上电的初始位置。\n\n"
                f"预计 {J.estimate_ms(self.journey) / 1000:.1f} 秒。\n\n"
                "确认路径上无人无障碍、急停可达吗？"):
            return
        if self.client.latched:
            try:
                self.client.send("stop_clear", timeout_ms=1000)
            except Exception:                     # noqa: BLE001 —— 尽力而为
                pass
        self._journey_queue = list(steps)
        self._journey_running = True
        self.journey_refresh()
        self.note(f"开始原路返回：{len(steps)} 段（倒序、方向取反）")
        self.root.after(120, self._journey_step)

    def _journey_step(self) -> None:
        if not self._journey_running:
            return
        if not self.client.online:
            self._journey_finish("连接断了，原路返回中断")
            return
        if not self._journey_queue:
            self._journey_finish("已按原路返回（现在应在整机上电的初始位）", clear=True)
            return
        step = self._journey_queue.pop(0)
        try:
            self.client.send("motor_enable", step["axis"], 1, timeout_ms=1000)
            self.client.send("motor_pos", step["axis"], step["dir"], step["rpm"],
                             step["acc"], step["pulses"], "rel", timeout_ms=1000)
        except Exception as exc:                  # noqa: BLE001
            self._journey_finish(f"发送失败，原路返回中断：{exc}")
            return
        self.journey_refresh()
        self.root.after(J.step_ms(step), self._journey_step)

    def _journey_finish(self, message: str, clear: bool = False) -> None:
        self._journey_running = False
        self._journey_queue = []
        if clear:
            # 回到起点了，记录已用完：清空，避免再点一次又往反方向走
            J.clear(self.journey)
            try:
                J.save(self.journey)
            except OSError:
                pass
        self.journey_refresh()
        self.note(message)

    def send_gripper(self, action: str) -> None:
        self.client.send("gripper", action, timeout_ms=2000)
        if action == "ready":
            text = ("状态：已请求 ready 预备位 %d°（无到位传感器，"
                    "'等待结束'只代表 %dms 等待走完）"
                    % (int(P.PARAM_DEFAULTS["gripper_ready_deg"]),
                       int(P.PARAM_DEFAULTS["gripper_settle_ms"])))
        else:
            text = (f"状态：已请求 {action}（无到位传感器，"
                    "'等待结束'只代表 200ms 等待走完）")
        self.gripper_status.configure(text=text)

    def _auto_gripper_ready(self) -> None:
        """连接成功（hello → online）后自动把夹爪打到预备位。

        只发一次、失败只写日志：这里在界面线程的事件回调之后执行，
        所以超时/断线不会卡住界面，也不会弹错误框。
        """
        if not self.client.online:
            return
        # 上一次会话断开时，固件 3 秒收不到上位机字节会自动锁存停止；
        # 重连后运动命令会被 E10 拒绝。这里自动清一次（只解锁存、不产生运动），
        # 否则用户每次重连都得先手动点「清除停止锁存」。
        if self.client.latched:
            try:
                self.client.send("stop_clear", timeout_ms=1000)
                self.note("连接成功：上次断开时固件锁存了停止，已自动清除（不产生任何运动）")
            except Exception as exc:             # noqa: BLE001 —— 尽力而为
                self.note(f"自动清除停止锁存失败（可手动点『清除停止锁存』）：{exc}")
        ready_deg = int(P.PARAM_DEFAULTS["gripper_ready_deg"])
        try:
            self.client.send("gripper", "ready", timeout_ms=1000)
        except Exception as exc:             # noqa: BLE001 —— 自动动作用尽力而为
            self.note(f"自动回预备位失败（忽略，可手动点『预备(%d)』）：{exc}" % ready_deg)
            return
        self.note(f"连接成功：已自动请求夹爪回预备位 {ready_deg}°")
        try:
            self.gripper_status.configure(text=f"状态：已自动请求 ready（预备位 {ready_deg}°）")
        except tk.TclError:
            pass

    def attach_vision(self, vision_app) -> None:
        """main.py 把视觉页挂进来：连到派时本类会自动开双画面。"""
        self.vision_app = vision_app

    def _auto_start_vision(self) -> None:
        """连上派之后自动开双画面（等价于点「打开双画面」）。只做一次。"""
        if self._vision_auto_started or self.vision_app is None:
            return
        if not self._pi_remote:
            return
        # 只有真的连上了才记住这个派，避免手填错的 IP 也进下拉框
        try:
            ep = self._selected_endpoint()
            if isinstance(ep, pi_link.PiEndpoint):
                pi_link.save_last(ep)
        except Exception:                        # noqa: BLE001 —— 记住 IP 只是锦上添花
            pass
        try:
            self.vision_app.start()
            self._vision_auto_started = True
            self.note("视觉已切到派端相机并自动开始取帧（1500ms 内出图）")
        except Exception as exc:                 # noqa: BLE001 —— 视觉失败不影响控制
            self.note(f"自动打开双画面失败（可在视觉页手动点『打开双画面』）：{exc}")

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
        # 先走接近段（整机上电起点 → 抓取位），跑完再开始序列
        if self._approach_should_auto() and self._approach_start_replay(self._seq_run_now):
            return
        self._seq_run_now()

    def _seq_run_now(self) -> None:
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
                # 连上后把夹爪摆到预备位（150°）：放到下一轮 after 里发，
                # 重连/握手期间卡住也只记日志，不弹框、不阻塞界面。
                try:
                    self.root.after(10, self._auto_gripper_ready)
                except tk.TclError:          # 界面正在关闭
                    pass
                # 连的是派 → 视觉自动切远程相机并开双画面（跟连串口一样一键到位）
                if self._pi_remote:
                    try:
                        self.root.after(500, self._auto_start_vision)
                    except tk.TclError:
                        pass
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
        # 上一次连接尝试失败 → 别让状态栏退回「未连接」把失败信息盖掉
        if state == STATE_ONLINE:
            self._last_fail_msg = ""
        if state == STATE_DISCONNECTED and getattr(self, "_last_fail_msg", ""):
            text = "连接失败"
            color = "#c62828"
        self.state_label.configure(text=text, foreground=color)
        self.connect_button.configure(
            text="断开" if state not in (STATE_DISCONNECTED, STATE_LINK_LOST) else "连接"
        )
        self.fw_label.configure(
            text=f"固件 {self.client.fw_version or '—'} 启动 {self.client.boot_id or '—'}"
        )
        # 固件重启（boot_id 变了）说明这是新的一次上电：移动记录从头开始
        boot = self.client.boot_id
        if state == STATE_ONLINE and boot and boot != self._journey_boot:
            self._journey_boot = boot
            self.journey = J.empty(boot)
            try:
                J.save(self.journey)
            except OSError:
                pass
            self.journey_refresh()
            self.note(f"检测到固件启动号 {boot}：移动记录已清零，从现在开始记")
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
