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
#: 判失联需要“完全收不到任何字节”这么久。原来 8 s，现场图传一开就把无线链路挤满，
#: 派端串口桥在 1.5 s 没收到字节就会注入停车帧（E10），8 s 判死太急：
#: 断开一次要重连+重新推参数，比多等几秒贵得多。15 s 仍留“链路变慢”的预警。
LINK_TIMEOUT_MS = 15000
#: 超过这个时长没收到任何数据就先告警（不判失联），让人别乱点。
SLOW_LINK_MS = 5000
#: 默认请求超时；查询类较紧，运动类较松（等的是“被接纳”，不是到位）。
TIMEOUT_QUERY_MS = 1500
TIMEOUT_DEFAULT_MS = 600
#: 运动类命令“被接纳”的回执超时。原来走 1500 ms，链路一拥塞（>1.5 s 才回）就会被
#: 判成超时/结果未知，进而重发或中止本件；现场实测回执迟到 1~3 s 是常态。
TIMEOUT_MOTION_MS = 3000
#: 可安全重试的命令（幂等）。
RETRYABLE = {"ping", "status", "motor_query", "param_get", "hello"}
#: 收到 E10（停止锁存）时值得“先 stop_clear 再原样重发”的命令。
#: 派端看门狗会在线路静默 1.5 s 时注入停车帧，于是运动命令被 E10 拒；
#: 这类拒绝是“环境打嗝”而不是指令错，自动清锁存重发即可，不必整件失败。
E10_RECOVERABLE = {"motor_pos", "motor_speed", "motor_goto", "motor_zero", "gripper", "angle"}
#: 同一条命令最多自动恢复几次（超过就老老实实报错，避免掩盖真故障）。
E10_MAX_RETRIES = 2

#: goto / angle 不传 rpm 时的兜底转速（圈/分）。固件默认 60，Z 满行程要 72 s，
#: 现场实测 1500 才能把 152,000 脉冲压到 3 s 内（见 10-27 日志）。
GOTO_DEFAULT_RPM = 1500

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
    #: 原始参数：E10 恢复要换新 id 重发同一条命令，得能原样重建（见 `_recover_e10`）。
    args: Tuple[object, ...] = ()


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
        #: E10 自动恢复表：重发用的新 id → 原请求 id（应答回来按原 id 结算）
        self._e10_alias: Dict[int, int] = {}
        #: True = 这次锁存是人按「停止全部」造成的（E10 自动恢复不去清它，安全优先）
        self._user_stop = False
        #: 已完成请求的结果缓存（req_id -> {'kind','code','pairs'}），给"后台线程等某条应答"用：
        #: 服务线程会先把 pending 弹掉，光看 pending 会错过应答。
        self.done: Dict[int, dict] = {}
        self.session_id = random.randint(1, 0xFFFF)
        self.state = STATE_DISCONNECTED
        self.fw_version = ""
        self.boot_id = ""
        self.axis_list: Tuple[int, ...] = P.VALID_AXES
        self.readback_axes: Tuple[int, ...] = ()
        self.latched = False
        #: 最近一次连接/打开失败的原因（界面弹窗要用，不能只留在日志里）
        self.last_error = ""
        self.last_rx_ms = 0
        #: 「链路变慢」只告警一次，收到数据后复位（见 tick()）
        self._slow_link_warned = False
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
        #: 最近一次应答/拒绝，供“点动”这类一次命令的对话框显示结果（不改变事件流）。
        self.last_ok: Optional[dict] = None
        self.last_err: Optional[dict] = None
        #: `motor_query pos` 的驱动器编码器计数（只对有回读的轴有效）：{axis: {'counts','ts','what'}}
        self.last_counts: Dict[int, dict] = {}
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
            self.last_error = "未选择链路"
            self.emit("note", "未选择链路")
            return False
        self.last_error = ""
        try:
            self.link.open()
        except Exception as exc:  # 打开失败必须让界面看见原因
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.emit("note", f"打开链路失败：{exc}")
            self.set_state(STATE_DISCONNECTED)
            return False
        self.framing.reset()
        self.pending.clear()
        self._e10_alias.clear()
        # 新连接：固件锁存状态未知，按“未锁存”起算；人工停止标记也复位
        self.latched = False
        self._user_stop = False
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
        self._e10_alias.clear()
        self.last_counts.clear()
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
            args=tuple(args),
        )
        self._write(raw, f"TX {rid} {cmd}")
        hook = self.motion_hook
        if hook is not None and cmd in ("motor_pos", "motor_speed", "motor_stop",
                                        "motor_zero", "stop_all", "stop_clear",
                                        "motor_goto"):
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
        """停止按钮：立即下发 stop_all，并置本地锁存提示。

        `_user_stop` 标记「这次锁存是人按的」，E10 自动恢复不会去清它 ——
        安全动作必须由人来解除（点「清锁存」）。
        """
        self._user_stop = True
        return self.send("stop_all", timeout_ms=TIMEOUT_QUERY_MS)

    def goto(
        self,
        axis: int,
        pulses: int,
        tol: Optional[int] = None,
        rpm: Optional[int] = None,
        timeout_ms: Optional[int] = None,
    ) -> int:
        """闭环相对移动：固件用遥测 counts 自己闭环，ok 回来时已经“走完并校验过”。

        与 `motor_pos` 的区别：`motor_pos` 只保证“指令被接纳”，不保证真的走到；
        `motor_goto` 的应答里带 `err`（剩余 counts）、`passes`（补了几轮）、
        `elapsed_ms`，失败回 `E14`。通道段这类“必须走到位”的路径用它。
        """
        # 速度：实测 rpm 直接决定速度（驱动器自己封顶约 274 圈/分）。
        # 不传 rpm 时以前走固件默认 60，Z 满行程要 72 s；这里统一给 GOTO_DEFAULT_RPM。
        if rpm is None:
            speed = int(P.GOTO_RPM_BY_AXIS.get(int(axis), GOTO_DEFAULT_RPM))
        else:
            speed = int(rpm)
        speed = speed or 1
        args: List[object] = [
            int(axis),
            int(pulses),
            int(P.PARAM_DEFAULTS["goto_tol_counts"] if tol is None else tol),
            speed,
        ]
        if timeout_ms is None:
            speed = min(speed, 274)  # 驱动器实际上限，超了也快不了，避免超时估太短
            # 闭环要先走一段、等稳、再补差（最多 goto_max_passes 次），所以客户端超时
            # 要按 3 倍行程时间 + 8 s 余量算，否则会把“固件还在跑”误判成请求超时。
            timeout_ms = int(abs(int(pulses)) / 3200.0 / speed * 60000 * 3) + 8000
        return self.send("motor_goto", *args, timeout_ms=timeout_ms)

    def angle_zero(self, axis: int, timeout_ms: int = 3000) -> int:
        """把转盘当前位置记为 0°（人工对准 1 号盒后发一次）。掉电失效。"""
        return self.send("motor_angle_zero", int(axis), timeout_ms=timeout_ms)

    def angle(
        self,
        axis: int,
        deg: float,
        rpm: Optional[int] = None,
        timeout_ms: Optional[int] = None,
    ) -> int:
        """转到“基准 + deg 度”（内部是闭环 goto，重复下发同一角度幂等）。

        deg 会被换成立方百分之几度（deg100），负角度按最短路径走。
        超时默认按 0.6 圈/秒估，最少 8 s。
        """
        deg100 = int(round(float(deg) * 100.0)) % 36000
        args: List[object] = [int(axis), deg100]
        if rpm is None:
            rpm = P.GOTO_RPM_BY_AXIS.get(int(axis), GOTO_DEFAULT_RPM)
        args.append(int(rpm))
        if timeout_ms is None:
            timeout_ms = 8000
        return self.send("motor_angle", *args, timeout_ms=timeout_ms)


    def wait_reply(self, req_id: int, timeout_ms: int = 8000) -> Optional[Dict[str, object]]:
        """阻塞等待某条请求的应答（只给脚本/自测用；GUI 走事件队列，别调它）。

        返回 `{"kind": reply|err|timeout, "text", "code", "cmd", "pairs"}`；超时返回 None。
        期间会把心跳等其它事件从队列里取走 —— 所以不能在 GUI 线程里用。
        """
        deadline = self.now_ms() + timeout_ms
        while self.now_ms() < deadline:
            if self.link is not None:
                try:
                    data = self.link.read()
                except Exception:
                    data = b""
                if data:
                    self.feed_bytes(data)
            self.tick()
            for ev in self.poll_events(500):
                if ev.data.get("req_id") == req_id and ev.kind in ("reply", "err", "timeout"):
                    return {
                        "kind": ev.kind,
                        "text": ev.text,
                        "code": ev.data.get("code"),
                        "cmd": ev.data.get("cmd"),
                        "pairs": ev.data.get("pairs") or {},
                    }
            time.sleep(0.005)
        return None

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

    def _remember_done(self, req_id: int, kind: str, code: str = "", pairs: Optional[dict] = None) -> None:
        """记下某条请求的最终结果（最多留 64 条，供后台线程轮询）。"""
        self.done[int(req_id)] = {'kind': kind, 'code': code, 'pairs': dict(pairs or {})}
        while len(self.done) > 64:
            try:
                self.done.pop(next(iter(self.done)))
            except (StopIteration, KeyError):
                break

    def done_of(self, req_id: int) -> Optional[dict]:
        """查一条请求的最终结果；还没完成返回 None。"""
        return self.done.get(int(req_id))

    def _recover_e10(self, pending: "PendingRequest") -> bool:
        """E10（停止锁存）自动恢复：先 `stop_clear`，再用**新 id** 重发同一条命令。

        为什么值得自动做：派端串口桥看门狗在笔记本 1.5 s 没发字节时会注入停车帧，
        于是紧随其后的运动命令被 E10 拒。这不是指令错，清锁存后重发通常立刻成功；
        以前这里直接报错，整件就失败了（现场 10-31 03:14 那件就是这么丢的）。

        为什么必须换新 id：固件（和模拟器）对重复 req_id 走"重传"分支，直接回放上一次
        的缓存应答 —— 也就是那条 E10；同 id 重发等于没发。新 id 的应答回来时按原请求
        结算（`_e10_alias`），调用方仍在等原 id，完全无感。
        """
        if self.state != STATE_ONLINE or pending.cmd not in E10_RECOVERABLE:
            return False
        if getattr(self, "_user_stop", False):
            # 人按「停止全部」造成的锁存绝不自动清：那是安全动作，要人来解。
            # 只有派端看门狗/链路抖动造成的锁存才值得自动恢复。
            return False
        if pending.attempts > E10_MAX_RETRIES:
            return False
        pending.attempts += 1
        try:
            # 1) 先清锁存：另起一条正常等回执的请求（stop_clear 不受锁存限制）
            self.send("stop_clear", timeout_ms=TIMEOUT_MOTION_MS)
        except Exception as exc:  # noqa: BLE001 —— 链路半断时别把恢复流程也炸掉
            self.emit("note", f"E10 自动恢复失败（stop_clear 下发异常）：{exc}")
            return False
        try:
            # 2) 换新 id 重发同一条命令
            new_rid = self.ids.acquire()
            raw = P.encode_request(new_rid, pending.cmd, *pending.args)
        except Exception as exc:  # noqa: BLE001
            self.emit("note", f"E10 自动恢复失败（重发编码异常）：{exc}")
            return False
        self.pending[new_rid] = PendingRequest(
            req_id=new_rid,
            cmd=pending.cmd,
            raw=raw,
            body=",".join(str(x) for x in (new_rid, pending.cmd) + tuple(pending.args)),
            deadline_ms=self.now_ms() + TIMEOUT_MOTION_MS,
            attempts=pending.attempts,
            args=tuple(pending.args),
        )
        self._e10_alias[new_rid] = pending.req_id
        pending.result = RESULT_WAITING
        pending.deadline_ms = self.now_ms() + TIMEOUT_MOTION_MS * 2
        self._write(raw, f"E10 重发 {pending.req_id}→{new_rid} {pending.cmd}")
        self.emit(
            "note",
            f"{pending.cmd} 被 E10 停止锁存拒绝 → 自动 stop_clear 后重发"
            f"（第 {pending.attempts - 1} 次，id {pending.req_id}→{new_rid}）",
            req_id=pending.req_id,
        )
        return True

    def _resolve(self, pending: PendingRequest, frame: P.Frame) -> None:
        """给一条请求结算（ok / err）。事件里的 req_id 一律用原请求 id。"""
        self.pending.pop(pending.req_id, None)
        self.ids.release(pending.req_id)
        if frame.kind == "err":
            pending.result = RESULT_ERR
            code = frame.error_code or "E05"
            text = f"{pending.cmd} 被拒绝：{code} {ERROR_TEXT.get(code, '')}".strip()
            self.last_err = {"ts": self.now_ms(), "cmd": pending.cmd, "code": code, "text": text}
            self._remember_done(pending.req_id, 'err', code=code, pairs=frame.pairs)
            self.emit(
                "err",
                text,
                code=code,
                cmd=pending.cmd,
                req_id=pending.req_id,
                pairs=frame.pairs,
            )
        else:
            pending.result = RESULT_OK
            self.last_ok = {"ts": self.now_ms(), "cmd": pending.cmd, "req_id": pending.req_id}
            self._apply_ok(pending, frame)
            self._remember_done(pending.req_id, 'reply', pairs=frame.pairs)
            self.emit(
                "reply",
                f"{pending.cmd} ok",
                cmd=pending.cmd,
                req_id=pending.req_id,
                pairs=frame.pairs,
            )

    def _on_reply(self, frame: P.Frame) -> None:
        rid = frame.req_id
        if rid == P.ID_EVENT:
            return
        # E10 恢复用的新 id → 原请求 id；先把这点查清，再做任何 pop/release。
        root_id = self._e10_alias.pop(rid, rid)
        if root_id != rid:
            self.pending.pop(rid, None)
            self.ids.release(rid)
        root = self.pending.get(root_id)
        if root is not None and frame.kind == "err" and frame.error_code == "E10":
            if self._recover_e10(root):
                return
        if root is not None:
            self._resolve(root, frame)
        else:
            self.emit("note", f"收到无对应请求的应答 id={rid}：{frame.raw}")

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
        if pending.cmd == "motor_query":
            try:
                addr = int(pairs.get("addr", 0))
                counts = int(pairs.get("counts", ""))
            except (TypeError, ValueError):
                return
            if addr > 0:
                self.last_counts[addr] = {
                    "counts": counts,
                    "ts": self.now_ms(),
                    "what": pairs.get("what", ""),
                }

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
            if not self.latched:
                # 锁存被解除（人工清锁存 / stop_clear 成功）→ 解除"人按停止"的标记
                self._user_stop = False
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
                self._remember_done(pending.req_id, 'timeout')
                self.emit("timeout", f"{pending.cmd} 无应答（已重试）", cmd=pending.cmd)
            else:
                pending.result = RESULT_UNKNOWN
                self._remember_done(pending.req_id, 'timeout', code='UNKNOWN')
                self.emit(
                    "timeout",
                    f"{pending.cmd} 超时：结果未知，请查状态或停止，不要盲目重发",
                    cmd=pending.cmd,
                    req_id=pending.req_id,
                )
        if self.state == STATE_ONLINE and self.last_rx_ms:
            silence = now - self.last_rx_ms
            if silence > LINK_TIMEOUT_MS:
                self.handle_link_lost("心跳超时")
                return
            if silence > SLOW_LINK_MS and not self._slow_link_warned:
                # 只告警不判死：现场图传一开就容易出现 5~10 s 的静默，
                # 判失联要重连+重推参数，比多等几秒贵得多。
                self._slow_link_warned = True
                self.emit(
                    "note",
                    f"链路变慢：已经 {silence / 1000:.1f} s 没收到任何数据"
                    f"（{LINK_TIMEOUT_MS / 1000:.0f} s 才判失联），先别动，等它恢复",
                )
            elif silence <= SLOW_LINK_MS and self._slow_link_warned:
                self._slow_link_warned = False
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
        self._e10_alias.clear()
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
