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
    "gripper_grab_deg": "夹爪夹紧角度",
    "gripper_step_deg": "夹爪单步角度",
    "gripper_settle_ms": "夹爪等待 ms",
    "heartbeat_ms": "心跳周期 ms",
    "link_timeout_ms": "失联停车 ms",
    "query_timeout_ms": "查询超时 ms",
    "telemetry_ms": "遥测周期 ms",
    "frame_gap_ms": "电机帧间隔 ms",
}
KNOWN_PARAMS = [(name, PARAM_LABELS[name]) for name in P.PARAM_DEFAULTS if name in PARAM_LABELS]


class AxisPanel:
    """单轴手动控制面板。"""

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
        self.pulses = tk.IntVar(value=3200)
        self.mode = tk.StringVar(value="rel")
        self.tele = tk.BooleanVar(value=False)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text="方向").pack(side="left")
        ttk.Radiobutton(row, text="正", value=0, variable=self.direction).pack(side="left")
        ttk.Radiobutton(row, text="负", value=1, variable=self.direction).pack(side="left")
        ttk.Label(row, text="转速").pack(side="left", padx=(6, 0))
        ttk.Spinbox(row, from_=1, to=300, width=5, textvariable=self.rpm).pack(side="left")
        ttk.Label(row, text="加速度").pack(side="left", padx=(6, 0))
        ttk.Spinbox(row, from_=0, to=255, width=4, textvariable=self.acc).pack(side="left")

        row2 = ttk.Frame(frame)
        row2.pack(fill="x", padx=4, pady=2)
        ttk.Label(row2, text="脉冲").pack(side="left")
        ttk.Spinbox(row2, from_=1, to=1000000, width=8, textvariable=self.pulses).pack(side="left")
        ttk.Label(row2, text="模式").pack(side="left", padx=(6, 0))
        ttk.Combobox(
            row2, width=5, state="readonly", values=("rel", "abs"), textvariable=self.mode
        ).pack(side="left")
        ttk.Checkbutton(
            row2, text="订阅遥测", variable=self.tele, command=self.toggle_telemetry
        ).pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=4, pady=2)
        for text, command in (
            ("速度模式", self.send_speed),
            ("位置模式", self.send_pos),
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

        self.readback = ttk.Label(frame, text="回读：—（未接回读）", foreground="#666666")
        self.readback.pack(fill="x", padx=4, pady=(2, 4))

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

    def send_pos(self) -> None:
        if not self._guard():
            return
        self.app.client.send(
            "motor_pos",
            self.addr,
            self.direction.get(),
            self.rpm.get(),
            self.acc.get(),
            self.pulses.get(),
            self.mode.get(),
        )

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
        if not messagebox.askyesno("位置清零", f"{AXIS_LABELS[self.addr]} 当前位置将被当成零点，继续？"):
            return
        self.app.client.send("motor_zero", self.addr)

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

        log_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(log_tab, text="原始日志")
        self._build_log(log_tab)

    def _build_gripper(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="夹爪：0°=夹紧、180°=松开，每次 45°（GRIPPER_* 宏为准，方向不要反转）",
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
        ttk.Label(angle_row, text="指定角度 0~270：").pack(side="left")
        self.servo_angle = tk.IntVar(value=180)
        ttk.Spinbox(angle_row, from_=0, to=270, width=6, textvariable=self.servo_angle).pack(side="left")
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
            if event.data.get("cmd") == "param_get" and "name" in pairs:
                name = pairs.get("name")
                if name in self.param_vars:
                    self.param_vars[name].set(pairs.get("value", ""))
                self.custom_value.set(pairs.get("value", ""))
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
