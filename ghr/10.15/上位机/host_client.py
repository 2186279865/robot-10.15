"""PC 侧协议客户端：握手、请求 id 管理、超时、事件分派与失联保护。

设计要点（依据 `上位机方案-v3-TypeC-CDC.md` 7 节）：
- `ok` 只表示“命令被接纳”，**不等于到位**；到位要靠 `event task ... state=reached`。
- 同一会话缓存请求内容：重试必须复用同一个 id 和同一份字节，固件才能去重。
- 查询/心跳可重试；运动命令超时后进入“结果未知”，只允许查状态或停止，不盲目重发。
- 心跳丢失 3s 即判定失联，控件必须回到安全状态（不允许带着未知状态继续操作）。

本模块不依赖 tkinter、不依赖 pyserial，可单独在测试中驱动（`service=False`）。
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

import protocol as P

#: 心跳周期与失联判定阈值（毫秒）。
HEARTBEAT_MS = 1000
LINK_TIMEOUT_MS = 3000
#: 默认请求超时；查询类较紧，运动类较松（等的是“被接纳”，不是到位）。
TIMEOUT_QUERY_MS = 300
TIMEOUT_DEFAULT_MS = 600
#: 可安全重试的命令（幂等）。
RETRYABLE = {"ping", "status", "motor_query", "param_get", "hello"}

ERROR_TEXT: Dict[str, str] = {
    "E01": "未知命令",
    "E02": "参数错误",
    "E03": "地址错误",
    "E04": "查询超时",
    "E05": "回包错误",
    "E06": "缓冲溢出/行过长",
    "E07": "校验错误",
    "E08": "使能未确认",
    "E09": "忙/队列满",
    "E10": "停止锁存",
    "E11": "无回读能力",
    "E12": "请求 id 冲突",
    "E13": "位置基准无效",
}

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_HANDSHAKING = "handshaking"
STATE_ONLINE = "online"
STATE_LINK_LOST = "link_lost"

STATE_TEXT: Dict[str, str] = {
    STATE_DISCONNECTED: "未连接",
    STATE_CONNECTING: "连接中",
    STATE_HANDSHAKING: "握手中",
    STATE_ONLINE: "在线",
    STATE_LINK_LOST: "失联",
}

RESULT_WAITING = "waiting"
RESULT_OK = "ok"
RESULT_ERR = "err"
RESULT_TIMEOUT = "timeout"
RESULT_UNKNOWN = "unknown"  # 运动类超时：结果未知，禁止盲目重发


@dataclass
class UiEvent:
    """推给界面的消息。界面只消费这个，不直接碰协议层。"""

    kind: str  # state / rx / tx / reply / err / event / tele / note / timeout
    text: str
    data: dict = field(default_factory=dict)
    ts_ms: int = 0


@dataclass
class PendingRequest:
    req_id: int
    cmd: str
    raw: bytes
    body: str
    deadline_ms: int
    attempts: int = 1
    result: str = RESULT_WAITING
    frames: List[P.Frame] = field(default_factory=list)


class HostClient:
    """把“链路字节”变成“界面可用的状态”。"""

    def __init__(
        self,
        link=None,
        clock: Optional[Callable[[], float]] = None,
        service: bool = False,
    ) -> None:
        self._clock = clock or time.monotonic
        self.link = link
        self.framing = P.FramingBuffer()
        self.ids = P.RequestIdPool()
        self.pending: Dict[int, PendingRequest] = {}
        self.session_id = random.randint(1, 0xFFFF)
        self.state = STATE_DISCONNECTED
        self.fw_version = ""
        self.boot_id = ""
        self.axis_list: Tuple[int, ...] = P.VALID_AXES
        self.readback_axes: Tuple[int, ...] = ()
        self.latched = False
        self.last_rx_ms = 0
        self.rx_count = 0
        self.tx_count = 0
        self.bad_frame_count = 0
        #: 超长/坏帧统计是“超长恢复”验收的证据之一。
        self.oversize_count = 0
        self._events: Deque[UiEvent] = deque(maxlen=1000)
        self._service_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.auto_reconnect = True
        self.link_factory: Optional[Callable[[], object]] = None
        #: 运动命令观察钩子：`hook(cmd, args)`，界面用它累计“软位置”（没有回读时也能回 0）。
        self.motion_hook: Optional[Callable[[str, tuple], None]] = None
        if service:
            self.start_service()

    # -- 时钟与事件 ---------------------------------------------------------
    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    def emit(self, kind: str, text: str, **data: object) -> None:
        with self._lock:
            self._events.append(UiEvent(kind=kind, text=text, data=dict(data), ts_ms=self.now_ms()))

    def poll_events(self, limit: int = 200) -> List[UiEvent]:
        out: List[UiEvent] = []
        with self._lock:
            while self._events and len(out) < limit:
                out.append(self._events.popleft())
        return out

    # -- 连接 ---------------------------------------------------------------
    def attach(self, link) -> None:
        self.link = link

    def connect(self) -> bool:
        """打开链路并发出握手。返回是否成功进入握手流程。"""
        if self.link is None:
            self.emit("note", "未选择链路")
            return False
        try:
            self.link.open()
        except Exception as exc:  # 打开失败必须让界面看见原因
            self.emit("note", f"打开链路失败：{exc}")
            self.set_state(STATE_DISCONNECTED)
            return False
        self.framing.reset()
        self.pending.clear()
        self.session_id = random.randint(1, 0xFFFF)
        self.set_state(STATE_HANDSHAKING)
        self.emit("note", f"链路已打开：{self.link.describe()}")
        self.begin_handshake()
        return True

    def begin_handshake(self) -> int:
        self.boot_id = ""
        self.readback_axes = ()
        self.ids = P.RequestIdPool()
        self.session_id = random.randint(1, 0xFFFF)
        return self.send("hello", P.PROTOCOL_VERSION, self.session_id, timeout_ms=1000)

    def disconnect(self, reason: str = "主动断开") -> None:
        for pending in self.pending.values():
            if pending.result == RESULT_WAITING:
                pending.result = RESULT_UNKNOWN
        self.pending.clear()
        self.ids = P.RequestIdPool()
        if self.link is not None:
            try:
                self.link.close()
            except Exception:
                pass
        self.set_state(STATE_DISCONNECTED)
        self.emit("note", reason)

    def set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        self.emit("state", STATE_TEXT.get(state, state), state=state)

    @property
    def online(self) -> bool:
        return self.state == STATE_ONLINE

    # -- 发送 ---------------------------------------------------------------
    def send(
        self,
        cmd: str,
        *args: object,
        timeout_ms: int = TIMEOUT_DEFAULT_MS,
        req_id: Optional[int] = None,
    ) -> int:
        rid = self.ids.acquire() if req_id is None else req_id
        try:
            raw = P.encode_request(rid, cmd, *args)
        except Exception:
            self.ids.release(rid)
            raise
        body = ",".join(str(x) for x in (rid, cmd) + args if x is not None)
        self.pending[rid] = PendingRequest(
            req_id=rid,
            cmd=cmd,
            raw=raw,
            body=body,
            deadline_ms=self.now_ms() + timeout_ms,
        )
        self._write(raw, f"TX {rid} {cmd}")
        hook = self.motion_hook
        if hook is not None and cmd in ("motor_pos", "motor_speed", "motor_zero",
                                        "stop_all", "stop_clear"):
            try:
                hook(cmd, args)
            except Exception as exc:  # 钩子只做界面记账，绝不能影响下发
                self.emit("note", f"运动记账钩子异常：{exc}")
        return rid

    def resend(self, req_id: int) -> bool:
        """幂等重试：复用同一 id 与同一份字节，让固件走会话去重。"""
        pending = self.pending.get(req_id)
        if pending is None or pending.result != RESULT_WAITING:
            return False
        if pending.cmd not in RETRYABLE:
            self.emit("note", f"{pending.cmd} 超时进入结果未知，不自动重发", req_id=req_id)
            pending.result = RESULT_UNKNOWN
            return False
        pending.attempts += 1
        pending.deadline_ms = self.now_ms() + TIMEOUT_QUERY_MS
        self._write(pending.raw, f"重试 {req_id} {pending.cmd}")
        return True

    def send_raw(self, text: str) -> None:
        """调试模式：按十六进制文本发送原始字节（避开 `!` 停止字符）。"""
        try:
            raw = bytes.fromhex(text.replace(" ", ""))
        except ValueError as exc:
            self.emit("note", f"十六进制格式错误：{exc}")
            return
        self._write(raw, "TX raw")

    def stop_all(self) -> int:
        """停止按钮：立即下发 stop_all，并置本地锁存提示。"""
        return self.send("stop_all", timeout_ms=TIMEOUT_QUERY_MS)

    def _write(self, raw: bytes, label: str) -> None:
        if self.link is None:
            self.emit("note", f"{label} 失败：链路为空")
            return
        try:
            self.link.write(raw)
        except Exception as exc:
            self.emit("note", f"{label} 失败：{exc}")
            self.handle_link_error(str(exc))
            return
        self.tx_count += 1
        self.emit("tx", raw.decode("ascii", "replace").rstrip("\n"), raw=raw)

    # -- 接收 ---------------------------------------------------------------
    def feed_bytes(self, data: bytes) -> None:
        if not data:
            return
        self.last_rx_ms = self.now_ms()
        for inbound in self.framing.feed(data):
            if not inbound.ok or inbound.frame is None:
                code = inbound.error.code if inbound.error else "E05"
                self.bad_frame_count += 1
                if code == "E06":
                    self.oversize_count += 1
                self.emit("note", f"坏帧 {code}：{inbound.error}", code=code)
                continue
            self._dispatch(inbound.frame)

    def _dispatch(self, frame: P.Frame) -> None:
        self.emit("rx", frame.raw, frame=frame)
        if frame.kind == "event":
            self._on_event(frame)
        elif frame.kind == "tele":
            self.emit("tele", frame.raw, pairs=frame.pairs)
        elif frame.kind == "request":
            # 固件不应回送请求帧；记录以便排查接线/回环问题。
            self.emit("note", f"收到意外请求帧：{frame.raw}")
        else:
            self._on_reply(frame)

    def _on_reply(self, frame: P.Frame) -> None:
        pending = self.pending.pop(frame.req_id, None) if frame.req_id != P.ID_EVENT else None
        if pending is not None:
            self.ids.release(pending.req_id)
            if frame.kind == "err":
                pending.result = RESULT_ERR
                code = frame.error_code or "E05"
                self.emit(
                    "err",
                    f"{pending.cmd} 被拒绝：{code} {ERROR_TEXT.get(code, '')}".strip(),
                    code=code,
                    cmd=pending.cmd,
                    req_id=pending.req_id,
                    pairs=frame.pairs,
                )
            else:
                pending.result = RESULT_OK
                self._apply_ok(pending, frame)
                self.emit(
                    "reply",
                    f"{pending.cmd} ok",
                    cmd=pending.cmd,
                    req_id=pending.req_id,
                    pairs=frame.pairs,
                )
        elif frame.req_id != P.ID_EVENT:
            self.emit("note", f"收到无对应请求的应答 id={frame.req_id}：{frame.raw}")

    def _apply_ok(self, pending: PendingRequest, frame: P.Frame) -> None:
        pairs = frame.pairs
        if pending.cmd == "hello":
            self.fw_version = pairs.get("fw", "")
            self.boot_id = pairs.get("boot", "")
            axes_raw = pairs.get("axes", "")
            axes = tuple(int(x) for x in axes_raw.split("/") if x.isdigit())
            if axes:
                self.axis_list = axes
            rb = pairs.get("readback", "")
            self.readback_axes = tuple(int(x) for x in rb.split("/") if x.isdigit())
            self.set_state(STATE_ONLINE)
            self.emit(
                "note",
                f"握手完成：固件 {self.fw_version}，启动标识 {self.boot_id}，"
                f"轴 {'/'.join(str(a) for a in self.axis_list)}，"
                f"回读 {'/'.join(str(a) for a in self.readback_axes) or '无'}",
            )
        if pending.cmd == "stop_all":
            self.latched = True
        if pending.cmd == "stop_clear" and pairs.get("latch") == "0":
            self.latched = False

    def _on_event(self, frame: P.Frame) -> None:
        pairs = frame.pairs
        etype = frame.fields[1] if len(frame.fields) > 1 else "?"
        if etype == "task":
            state = pairs.get("state", "")
            note = {
                P.EVIDENCE_SENT: "已下发驱动器（不代表到位）",
                P.EVIDENCE_DRIVER_ACK: "驱动器已应答",
                P.EVIDENCE_REACHED: "已到位",
                P.EVIDENCE_SETTLE_ELAPSED: "等待时间已到",
            }.get(state, state)
            self.emit("event", f"任务 {pairs.get('id', '?')} {state}：{note}", type=etype, **pairs)
        elif etype == "latch":
            self.latched = pairs.get("state") == "set"
            self.emit("event", f"停止锁存 {'置位' if self.latched else '解除'}", type=etype, **pairs)
        else:
            self.emit("event", frame.raw, type=etype, **pairs)

    # -- 定时任务 -----------------------------------------------------------
    def tick(self) -> None:
        """由服务线程或界面定时调用：超时判定 + 心跳。"""
        now = self.now_ms()
        for pending in list(self.pending.values()):
            if pending.result != RESULT_WAITING or now < pending.deadline_ms:
                continue
            if pending.cmd in RETRYABLE and pending.attempts < 3:
                self.resend(pending.req_id)
                continue
            self.pending.pop(pending.req_id, None)
            self.ids.release(pending.req_id)
            if pending.cmd in RETRYABLE:
                pending.result = RESULT_TIMEOUT
                self.emit("timeout", f"{pending.cmd} 无应答（已重试）", cmd=pending.cmd)
            else:
                pending.result = RESULT_UNKNOWN
                self.emit(
                    "timeout",
                    f"{pending.cmd} 超时：结果未知，请查状态或停止，不要盲目重发",
                    cmd=pending.cmd,
                    req_id=pending.req_id,
                )
        if self.state == STATE_ONLINE and self.last_rx_ms:
            if now - self.last_rx_ms > LINK_TIMEOUT_MS:
                self.handle_link_lost("心跳超时")
                return
            if now - getattr(self, "_last_ping_ms", 0) >= HEARTBEAT_MS:
                self._last_ping_ms = now
                self.ping()

    def ping(self) -> int:
        return self.send("ping", timeout_ms=TIMEOUT_QUERY_MS)

    # -- 故障处理 -----------------------------------------------------------
    def handle_link_error(self, reason: str) -> None:
        self.handle_link_lost(reason)

    def handle_link_lost(self, reason: str) -> None:
        """失联保护：状态未知、禁止继续下发、提示需要停止或重连。"""
        if self.state == STATE_LINK_LOST:
            return
        for pending in self.pending.values():
            if pending.result == RESULT_WAITING:
                pending.result = RESULT_UNKNOWN
        self.pending.clear()
        self.ids = P.RequestIdPool()
        self.set_state(STATE_LINK_LOST)
        self.emit("note", f"链路异常：{reason}；运动状态未知，已禁止继续下发")
        if self.link is not None:
            try:
                self.link.close()
            except Exception:
                pass

    # -- 服务线程 -----------------------------------------------------------
    def start_service(self) -> None:
        if self._service_thread is not None:
            return
        self._stop.clear()
        self._service_thread = threading.Thread(target=self._service_loop, daemon=True)
        self._service_thread.start()

    def stop_service(self) -> None:
        self._stop.set()
        thread = self._service_thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._service_thread = None

    def _service_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.link is not None and getattr(self.link, "connected", False):
                    poll = getattr(self.link, "poll", None)
                    if poll is not None:
                        # 模拟后端靠 poll() 冒充"固件主动上报"的事件与遥测；
                        # 真实串口链路没有这个方法，不影响。
                        try:
                            poll()
                        except Exception as exc:
                            self.emit("note", f"链路 poll 异常：{exc}")
                    data = self.link.read()
                    if data:
                        self.feed_bytes(data)
                elif self.state not in (STATE_DISCONNECTED, STATE_LINK_LOST):
                    self.handle_link_lost("链路已断开")
                self.tick()
            except Exception as exc:  # 后台线程不允许把异常抛给解释器
                self.emit("note", f"服务线程异常：{exc}")
                self.handle_link_lost(str(exc))
            time.sleep(0.02)
