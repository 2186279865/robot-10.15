"""无硬件模拟后端：在 PC 侧复现固件对协议的处理与状态机。

用途（见方案 10 节第 1 步）：在没有板子的情况下验证分帧、校验、超长恢复、
请求去重、会话与 GUI 连接，并让 PC 侧开发不必等 USB 枚举完成。

**这是模拟数据，不是实测数据。** 界面必须显示“模拟”。模拟器只承诺
“与协议一致”，不承诺与真实驱动器的时间特性一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import protocol as P

FIRMWARE_VERSION = "sim-0.1"
BOOT_ID = 0x5A5A0001

#: 只有 Z 轴按方案 8 节先接回读（驱动器 TXD → PA3）。
READBACK_AXES = (1,)

#: 编码器计数/圈（方案 8 节）。
COUNTS_PER_REV = 65536


@dataclass
class AxisState:
    addr: int
    enabled: bool = False
    direction: int = 0
    rpm: int = 0
    acc: int = 1
    mode: str = "stop"  # stop / speed / pos
    target_counts: int = 0
    position_counts: int = 0
    stall: bool = False
    last_readback_ms: int = 0

    @property
    def readback_capable(self) -> bool:
        return self.addr in READBACK_AXES

    @property
    def running(self) -> bool:
        return self.mode != "stop" and self.rpm != 0 and self.enabled


@dataclass
class GripperState:
    open_deg: int = P.PARAM_DEFAULTS["gripper_open_deg"]  # type: ignore[assignment]
    grab_deg: int = P.PARAM_DEFAULTS["gripper_grab_deg"]  # type: ignore[assignment]
    step_deg: int = P.PARAM_DEFAULTS["gripper_step_deg"]  # type: ignore[assignment]
    settle_ms: int = P.PARAM_DEFAULTS["gripper_settle_ms"]  # type: ignore[assignment]
    angle: int = 180
    busy_until_ms: int = 0


@dataclass
class TaskRecord:
    task_id: int
    addr: int
    kind: str
    state: str = P.EVIDENCE_SENT


class SimulatedDevice:
    """固件行为的软件替身。`feed()` 进字节、出字节。"""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self.framing = P.FramingBuffer()
        self.session = P.SessionCache(capacity=32)
        self.axes: Dict[int, AxisState] = {addr: AxisState(addr=addr) for addr in P.VALID_AXES}
        self.gripper = GripperState()
        self.params: Dict[str, object] = dict(P.PARAM_DEFAULTS)
        self.stop_latched = False
        self.stop_latch_reason = ""
        self.control_owner = "pc"
        self.telemetry: Dict[int, int] = {}
        self.events_out: List[bytes] = []
        self._task_list: List[TaskRecord] = []
        self._next_task = 1
        self._tele_last: Dict[int, int] = {}
        self._next_boot = BOOT_ID
        self.started_ms = self.now_ms()
        self._last_ms = self.started_ms
        self.command_log: List[str] = []

    # -- 时钟 ---------------------------------------------------------------
    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    # -- 对外接口 -----------------------------------------------------------
    def feed(self, data: bytes) -> bytes:
        """接收 PC 字节流，返回需要回送的字节流（应答 + 事件 + 遥测）。"""
        if not data:
            return b""
        # 固件的 USB OUT 回调只扫描停止字符，不做行解析（方案 5 节）。
        if b"!" in data:
            self.latch_stop("PC 停止字符 !")
            data = data.replace(b"!", b"")
        self.advance_to(self.now_ms())
        out: List[bytes] = []
        for inbound in self.framing.feed(data):
            if not inbound.ok or inbound.frame is None:
                code = inbound.error.code if inbound.error else "E05"
                out.append(P.encode_err(0, code))
                continue
            out.extend(self.handle(inbound.frame))
        out.extend(self.drain_events())
        return b"".join(out)

    def handle(self, frame: P.Frame) -> List[bytes]:
        if not frame.is_request:
            return []
        self.command_log.append(frame.raw)
        if frame.req_id != P.ID_EVENT:
            verdict = self.session.check(frame.req_id, self._request_key(frame))
            if verdict == P.SessionCache.DUPLICATE:
                cached = self.session.cached_response(frame.req_id)
                return [cached] if cached else []
            if verdict == P.SessionCache.CONFLICT:
                return [P.encode_err(frame.req_id, "E12")]
        try:
            parsed = P.validate_request(frame)
        except P.ProtocolError as exc:
            return [P.encode_err(frame.req_id, exc.code)]
        responses = self.execute(parsed)
        if frame.req_id != P.ID_EVENT and responses:
            self.session.record(frame.req_id, self._request_key(frame), responses[0])
        return responses

    @staticmethod
    def _request_key(frame: P.Frame) -> str:
        return frame.body

    # -- 命令实现 -----------------------------------------------------------
    def execute(self, req: P.ParsedRequest) -> List[bytes]:
        cmd = req.cmd
        rid = req.req_id
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            return [P.encode_err(rid, "E01")]
        try:
            return handler(rid, **req.values)
        except P.ProtocolError as exc:
            return [P.encode_err(rid, exc.code)]

    # 需要忽略锁存的控制类命令（其余运动命令在锁存期间返回 E10）
    _LATCH_EXEMPT = {"hello", "ping", "status", "stop_clear"}

    def _cmd_hello(self, rid: int, protocol_version: int, session: int) -> List[bytes]:
        if protocol_version != P.PROTOCOL_VERSION:
            return [P.encode_err(rid, "E02", want=P.PROTOCOL_VERSION, got=protocol_version)]
        self.session.clear()
        self._next_boot += 1
        return [
            P.encode_ok(
                rid,
                fw=FIRMWARE_VERSION,
                proto=P.PROTOCOL_VERSION,
                boot=f"{self._next_boot:08X}",
                axes="1/3/4/5",
                readback="1",
            )
        ]

    def _cmd_ping(self, rid: int, **_: object) -> List[bytes]:
        return [P.encode_ok(rid, ms=self.now_ms())]

    def _cmd_status(self, rid: int, **_: object) -> List[bytes]:
        running = "/".join(str(a) for a in P.VALID_AXES if self.axes[a].running) or "-"
        queued = sum(1 for t in self._tasks if t.state == P.EVIDENCE_SENT)
        return [
            P.encode_ok(
                rid,
                latch=1 if self.stop_latched else 0,
                owner=self.control_owner,
                running=running,
                queued=queued,
                gripper=self.gripper.angle,
            )
        ]

    def _cmd_motor_enable(self, rid: int, addr: int, on: int) -> List[bytes]:
        if self._latched(addr):
            return [P.encode_err(rid, "E10")]
        axis = self.axes[addr]
        axis.enabled = bool(on)
        if not on:
            axis.rpm = 0
            axis.mode = "stop"
        return [P.encode_ok(rid, addr=addr, enabled=1 if axis.enabled else 0)]

    def _cmd_motor_speed(self, rid: int, addr: int, dir: int, rpm: int, acc: int) -> List[bytes]:
        if self._latched(addr):
            return [P.encode_err(rid, "E10")]
        axis = self.axes[addr]
        axis.enabled = True
        axis.direction = dir
        axis.rpm = rpm
        axis.acc = acc
        axis.mode = "speed"
        task = self._new_task(addr, "speed")
        return [P.encode_ok(rid, addr=addr, rpm=rpm, task=task.task_id)]

    def _cmd_motor_pos(
        self, rid: int, addr: int, dir: int, rpm: int, acc: int, pulses: int, mode: str
    ) -> List[bytes]:
        if self._latched(addr):
            return [P.encode_err(rid, "E10")]
        axis = self.axes[addr]
        if mode == "abs" and not axis.readback_capable:
            return [P.encode_err(rid, "E13", reason="no_position_reference")]
        axis.enabled = True
        axis.direction = dir
        axis.rpm = rpm
        axis.acc = acc
        axis.mode = "pos"
        delta = pulses if dir else -pulses
        axis.target_counts = axis.position_counts + delta
        task = self._new_task(addr, "pos")
        return [P.encode_ok(rid, addr=addr, pulses=pulses, mode=mode, task=task.task_id)]

    def _cmd_motor_stop(self, rid: int, addr: int) -> List[bytes]:
        axis = self.axes[addr]
        axis.rpm = 0
        axis.mode = "stop"
        return [P.encode_ok(rid, addr=addr)]

    def _cmd_stop_all(self, rid: int) -> List[bytes]:
        for axis in self.axes.values():
            axis.rpm = 0
            axis.mode = "stop"
        self.latch_stop("stop_all")
        return [P.encode_ok(rid, axes="1/3/4/5")]

    def _cmd_motor_stall_clear(self, rid: int, addr: int) -> List[bytes]:
        self.axes[addr].stall = False
        return [P.encode_ok(rid, addr=addr)]

    def _cmd_motor_zero(self, rid: int, addr: int) -> List[bytes]:
        axis = self.axes[addr]
        if axis.running:
            return [P.encode_err(rid, "E09", reason="moving")]
        axis.position_counts = 0
        axis.last_readback_ms = self.now_ms()
        return [P.encode_ok(rid, addr=addr, position=0)]

    def _cmd_motor_query(self, rid: int, addr: int, what: str) -> List[bytes]:
        axis = self.axes[addr]
        if not axis.readback_capable:
            return [P.encode_err(rid, "E11", addr=addr)]
        age = self.now_ms() - axis.last_readback_ms
        axis.last_readback_ms = self.now_ms()
        if what == "vel":
            return [P.encode_ok(rid, addr=addr, rpm=axis.rpm, age_ms=age)]
        if what == "pos":
            deg = axis.position_counts * 360.0 / COUNTS_PER_REV
            return [
                P.encode_ok(
                    rid,
                    addr=addr,
                    counts=axis.position_counts,
                    deg=f"{deg:.1f}",
                    age_ms=age,
                )
            ]
        return [P.encode_ok(rid, addr=addr, flag=0x00, age_ms=age)]

    def _cmd_gripper(self, rid: int, action: str) -> List[bytes]:
        g = self.gripper
        if action == "open":
            g.angle = g.open_deg
        elif action == "grab":
            g.angle = g.grab_deg
        elif action == "step_grab":
            g.angle = max(g.grab_deg, g.angle - g.step_deg)
        elif action == "step_release":
            g.angle = min(g.open_deg, g.angle + g.step_deg)
        g.busy_until_ms = self.now_ms() + int(g.settle_ms)
        return [P.encode_ok(rid, action=action, angle=g.angle, settle_ms=g.settle_ms)]

    def _cmd_servo_angle(self, rid: int, deg: int) -> List[bytes]:
        if not self.gripper.grab_deg <= deg <= self.gripper.open_deg:
            return [P.encode_err(rid, "E02", reason="out_of_range")]
        self.gripper.angle = deg
        self.gripper.busy_until_ms = self.now_ms() + int(self.gripper.settle_ms)
        return [P.encode_ok(rid, deg=deg, settle_ms=self.gripper.settle_ms)]

    def _cmd_param_get(self, rid: int, name: str) -> List[bytes]:
        if name not in self.params:
            return [P.encode_err(rid, "E02", reason="unknown_param")]
        return [P.encode_ok(rid, name=name, value=self.params[name])]

    def _cmd_param_set(self, rid: int, name: str, value: int) -> List[bytes]:
        if name not in self.params:
            return [P.encode_err(rid, "E02", reason="unknown_param")]
        if name in ("speed_rpm_max",) and value < 1:
            return [P.encode_err(rid, "E02", reason="range")]
        before = self.params[name]
        self.params[name] = value
        if name == "gripper_open_deg":
            self.gripper.open_deg = value
        elif name == "gripper_grab_deg":
            self.gripper.grab_deg = value
        elif name == "gripper_step_deg":
            self.gripper.step_deg = value
        elif name == "gripper_settle_ms":
            self.gripper.settle_ms = value
        return [P.encode_ok(rid, name=name, before=before, value=value)]

    def _cmd_tele_on(self, rid: int, addr: int, interval_ms: int) -> List[bytes]:
        self.telemetry[addr] = interval_ms
        return [P.encode_ok(rid, addr=addr, interval_ms=interval_ms)]

    def _cmd_tele_off(self, rid: int) -> List[bytes]:
        self.telemetry.clear()
        return [P.encode_ok(rid, subs=0)]

    def _cmd_stop_clear(self, rid: int) -> List[bytes]:
        """只解锁，不恢复旧运动（方案 5 节）。"""
        self.stop_latched = False
        self.stop_latch_reason = ""
        return [P.encode_ok(rid, latch=0)]

    # -- 内部 ---------------------------------------------------------------
    @property
    def _tasks(self) -> List[TaskRecord]:
        return self._task_list

    def _new_task(self, addr: int, kind: str) -> TaskRecord:
        task = TaskRecord(task_id=self._next_task, addr=addr, kind=kind)
        self._next_task += 1
        self._tasks.append(task)
        self.events_out.append(P.encode_event("task", id=task.task_id, state=P.EVIDENCE_SENT))
        return task

    def _latched(self, addr: int) -> bool:
        if not self.stop_latched:
            return False
        # 锁存期间允许停止类命令通过，避免“锁死后无法停车”。
        return True

    def latch_stop(self, reason: str) -> None:
        for axis in self.axes.values():
            axis.rpm = 0
            axis.mode = "stop"
        if not self.stop_latched:
            self.stop_latched = True
            self.stop_latch_reason = reason
            self.events_out.append(P.encode_event("latch", state="set", reason=reason.replace(" ", "_")))

    def drain_events(self) -> List[bytes]:
        out = list(self.events_out)
        self.events_out.clear()
        return out

    def advance_to(self, now_ms: int) -> None:
        """按经过时间推进模拟运动与到位事件。"""
        last = getattr(self, "_last_ms", now_ms)
        dt = max(0, now_ms - last)
        self._last_ms = now_ms
        if dt:
            for axis in self.axes.values():
                if axis.mode == "speed" and axis.rpm:
                    axis.position_counts += self._delta(axis, dt)
                elif axis.mode == "pos" and axis.rpm:
                    remaining = axis.target_counts - axis.position_counts
                    step = self._delta(axis, dt)
                    if abs(remaining) <= abs(step):
                        axis.position_counts = axis.target_counts
                        axis.rpm = 0
                        axis.mode = "stop"
                        for task in self._tasks:
                            if task.addr == axis.addr and task.state == P.EVIDENCE_SENT:
                                task.state = P.EVIDENCE_REACHED
                                self.events_out.append(
                                    P.encode_event("task", id=task.task_id, state=P.EVIDENCE_REACHED)
                                )
                    else:
                        axis.position_counts += step
        if self.gripper.busy_until_ms and now_ms >= self.gripper.busy_until_ms:
            self.gripper.busy_until_ms = 0
            self.events_out.append(
                P.encode_event("task", id=0, state=P.EVIDENCE_SETTLE_ELAPSED, what="gripper")
            )
        self._emit_telemetry(now_ms)

    def _emit_telemetry(self, now_ms: int) -> None:
        """按订阅周期产生遥测帧；未接回读的轴只报速度并显式标记 readback=0。"""
        for addr, interval in list(self.telemetry.items()):
            if now_ms - self._tele_last.get(addr, 0) < max(1, int(interval)):
                continue
            self._tele_last[addr] = now_ms
            axis = self.axes[addr]
            if axis.readback_capable:
                deg = axis.position_counts * 360.0 / COUNTS_PER_REV
                self.events_out.append(
                    P.encode_telemetry(
                        addr=addr, rpm=axis.rpm, counts=axis.position_counts, deg=f"{deg:.1f}"
                    )
                )
            else:
                self.events_out.append(
                    P.encode_telemetry(addr=addr, rpm=axis.rpm, readback=0)
                )

    @staticmethod
    def _delta(axis: AxisState, dt_ms: int) -> int:
        counts = int(axis.rpm * COUNTS_PER_REV / 60.0 * dt_ms / 1000.0)
        return counts if axis.direction else -counts

    def gripper_busy(self) -> bool:
        return self.now_ms() < self.gripper.busy_until_ms

    def reset(self) -> None:
        """模拟 MCU 复位：运动清空、会话与缓冲清空。"""
        self.framing.reset()
        self.session.clear()
        for axis in self.axes.values():
            axis.enabled = False
            axis.rpm = 0
            axis.mode = "stop"
            axis.position_counts = 0
        self.events_out.clear()
        self._task_list = []
        self._next_task = 1
        self._tele_last.clear()
        self._last_ms = self.now_ms()


# --------------------------------------------------------------------------
# 与真实链路同接口的模拟链路
# --------------------------------------------------------------------------


class SimulatedLink:
    """实现与 `serial_link.SerialLink` 相同的业务接口，供 GUI 切换使用。"""

    is_simulated = True

    def __init__(self, device: Optional[SimulatedDevice] = None) -> None:
        self.device = device or SimulatedDevice()
        self._rx = bytearray()
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def connected(self) -> bool:
        return self._open

    def write(self, data: bytes) -> int:
        if not self._open:
            raise RuntimeError("链路未打开")
        response = self.device.feed(bytes(data))
        self._rx.extend(response)
        return len(data)

    def read(self) -> bytes:
        chunk = bytes(self._rx)
        self._rx.clear()
        return chunk

    def poll(self) -> bytes:
        """让模拟设备把“随时间产生”的字节送出来（到位事件、遥测）。

        真实链路里这些字节由固件主动发；模拟后端必须显式调用本方法，
        否则 `read()` 永远只能拿到应答 —— 这一步也是测试判别“到位≠收到 ok”的关键。
        """
        self.device.advance_to(self.device.now_ms())
        data = b"".join(self.device.drain_events())
        self._rx.extend(data)
        return data

    def simulate_disconnect(self) -> None:
        """模拟拔线：链路断开但不复位设备（用于验证失联保护）。"""
        self._open = False

    def simulate_mcu_reset(self) -> None:
        self.device.reset()

    def describe(self) -> str:
        return "模拟后端（无硬件）"
