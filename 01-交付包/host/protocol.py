"""10.15 上位机与固件之间的文本行协议（PC 侧实现）。

依据 `ghr/10.15/上位机方案-v3-TypeC-CDC.md` 第 7 节与
`ghr/10.15/交接文档-上位机开工-TypeC-20261016.md` 第 8.2 节。

帧格式（ASCII，LF 结束，允许结尾带 CR）::

    请求：$<id>,<cmd>[,<arg>...]*<CS>\\n
    应答：#<id>,ok[,<key=value>...]*<CS>\\n
    错误：#<id>,err,<code>*<CS>\\n
    事件：#0,event,<type>[,<key=value>...]*<CS>\\n
    遥测：#0,tele,<key=value>...*<CS>\\n

CS 为起始符之后、星号之前全部 ASCII 字节的异或，两位大写十六进制。
完整帧含起始符、校验和与 LF 最多 96 字节，因此 payload 最多 91 字节。

本模块不依赖任何第三方库，可单独导入用于 PC 侧与测试；固件侧
`host_protocol_inline.h` 必须与本文件的校验/长度/错误码保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 协议常量
# --------------------------------------------------------------------------

PROTOCOL_VERSION = 1

START_REQUEST = "$"
START_REPLY = "#"

CHECKSUM_SEP = "*"
TERMINATOR = "\n"

MAX_FRAME_BYTES = 96
MAX_PAYLOAD_BYTES = MAX_FRAME_BYTES - 5  # 起始符 + '*' + 2位CS + LF

ID_MIN = 1
ID_MAX = 65535
ID_EVENT = 0  # 事件与遥测专用，请求不得使用

#: 轴上电后由固件在 hello 应答中回报，PC 侧也必须做一次本地白名单检查。
VALID_AXES: Tuple[int, ...] = (1, 3, 4, 5)
AXIS_NAMES: Dict[int, str] = {1: "Z", 3: "托盘", 4: "X", 5: "Y"}
AXIS_LABELS: Dict[int, str] = {a: f"{AXIS_NAMES[a]}({a})" for a in VALID_AXES}

#: 建议错误码，见方案 7.2 节。
ERROR_CODES: Dict[str, str] = {
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
    "E12": "请求id冲突",
    "E13": "位置基准无效",
    "E14": "闭环未到位",
}

#: 任务完成证据等级；`ok` 只代表命令被接纳，不代表到位。
EVIDENCE_SENT = "sent"
EVIDENCE_DRIVER_ACK = "driver_ack"
EVIDENCE_REACHED = "reached"
EVIDENCE_SETTLE_ELAPSED = "settle_elapsed"

EVENT_TYPES = ("task", "latch", "link", "reset")

#: 运行参数默认值（方案 5 节）。界面默认速度上限 300RPM。
#: 本版使用的「预备位」角度（比 150° 再张开一点：下探时爪尖不蹭物块）。
#: 固件参数 gripper_ready_deg 出厂默认仍是 150，上位机连上时会 param_set 成这个值。
GRIPPER_READY_DEG = 158

#: 闭环 goto 的默认转速（圈/分，按轴分）。实测 rpm 就是速度，
#: 驱动器自己封顶约 274 圈/分。Z 要快（满行程 3 s），X/Y 慢一点不得不快不震。
# Z=1500 已验证；Y 从 300 提到 800（现场可再调）；X=120 偏保守，提到 300；托盘仍 300
GOTO_RPM_BY_AXIS = {1: 2200, 3: 300, 4: 300, 5: 2000}

PARAM_DEFAULTS: Dict[str, object] = {
    "speed_rpm": 60,
    "acc": 1,
    "speed_rpm_max": 300,
    "gripper_open_deg": 180,
    "gripper_ready_deg": 150,
    "gripper_grab_deg": 135,
    "gripper_step_deg": 45,
    "gripper_settle_ms": 200,
    #: 夹爪在非夹紧状态最多保持多久（固件调试兜底，0=不卸力）。
    "gripper_hold_max_ms": 0,
    "heartbeat_ms": 500,
    "link_timeout_ms": 3000,
    "query_timeout_ms": 100,
    "telemetry_ms": 200,
    "frame_gap_ms": 10,
    #: 哪些轴接了驱动器回读线：bit0=Z(1)、bit1=托盘(3)、bit2=X(4)、bit3=Y(5)。
    #: 默认 15 = 四轴都接；某个轴还没接就把对应位去掉，不用重烧固件。
    "readback_mask": 15,
    #: 闭环 motor_goto 用：编码器每圈 counts（EMM V5 标称 65536，必须实测标定）。
    "counts_per_rev": 65536,
    #: 闭环到位容差（编码器 counts），200 counts ≈ 1.1°。
    "goto_tol_counts": 350,
    #: 闭环首段速度（RPM）。
    "goto_rpm": 300,
    #: 闭环补差段速度（RPM）。
    "goto_final_rpm": 150,
    #: 闭环最多补几轮（含首段后）。
    "goto_max_passes": 2,
    #: 闭环最长耗时（ms，0=按距离自动估算）。
    "goto_max_ms": 0,
    "angle_tol_counts": 90,          # 转盘角度模式容差 counts
}


class ProtocolError(Exception):
    """协议层错误，`code` 为 E01～E13 之一。"""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"未知错误码: {code}")
        self.code = code
        self.message = message or ERROR_CODES[code]
        super().__init__(f"{code} {self.message}")


# --------------------------------------------------------------------------
# 校验和
# --------------------------------------------------------------------------


def xor_checksum(payload: str) -> int:
    """返回 payload 中 ASCII 字节的异或值。payload 必须为纯 ASCII。"""
    value = 0
    for ch in payload:
        code = ord(ch)
        if code > 0x7F:
            raise ProtocolError("E02", f"非 ASCII 字符: {ch!r}")
        value ^= code
    return value


def checksum_text(payload: str) -> str:
    """返回两位大写十六进制校验和文本。"""
    return f"{xor_checksum(payload):02X}"


def verify_checksum(payload: str, expected: str) -> bool:
    expected = expected.strip().upper()
    if len(expected) != 2:
        return False
    try:
        int(expected, 16)
    except ValueError:
        return False
    return checksum_text(payload) == expected


# --------------------------------------------------------------------------
# 组帧
# --------------------------------------------------------------------------


def _join_body(req_id: int, head: str, extra: Sequence[str]) -> str:
    parts = [head]
    parts.extend(str(x) for x in extra)
    return f"{req_id}," + ",".join(parts)


def build_line(start: str, body: str) -> str:
    """由起始符与 payload 组装出不含 LF 的完整帧文本。"""
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ProtocolError("E06", f"payload {len(body)} 字节超过 {MAX_PAYLOAD_BYTES}")
    return f"{start}{body}{CHECKSUM_SEP}{checksum_text(body)}"


def encode_request(req_id: int, cmd: str, *args: object) -> bytes:
    _check_request_id(req_id)
    return (build_line(START_REQUEST, _join_body(req_id, cmd, args)) + TERMINATOR).encode("ascii")


def encode_ok(req_id: int, **pairs: object) -> bytes:
    extra = [f"{k}={_fmt(v)}" for k, v in pairs.items()]
    return (build_line(START_REPLY, _join_body(req_id, "ok", extra)) + TERMINATOR).encode("ascii")


def encode_err(req_id: int, code: str, **pairs: object) -> bytes:
    if code not in ERROR_CODES:
        raise ValueError(f"未知错误码: {code}")
    extra = [code] + [f"{k}={_fmt(v)}" for k, v in pairs.items()]
    return (build_line(START_REPLY, _join_body(req_id, "err", extra)) + TERMINATOR).encode("ascii")


def encode_event(event_type: str, **pairs: object) -> bytes:
    extra = [event_type] + [f"{k}={_fmt(v)}" for k, v in pairs.items()]
    return (build_line(START_REPLY, _join_body(ID_EVENT, "event", extra)) + TERMINATOR).encode("ascii")


def encode_telemetry(**pairs: object) -> bytes:
    extra = [f"{k}={_fmt(v)}" for k, v in pairs.items()]
    return (build_line(START_REPLY, _join_body(ID_EVENT, "tele", extra)) + TERMINATOR).encode("ascii")


def _fmt(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _check_request_id(req_id: int) -> None:
    if not isinstance(req_id, int) or isinstance(req_id, bool):
        raise ProtocolError("E02", "id 必须为整数")
    if not ID_MIN <= req_id <= ID_MAX:
        raise ProtocolError("E02", f"id 超出范围 {ID_MIN}～{ID_MAX}: {req_id}")


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """一条已通过校验的帧。"""

    start: str
    req_id: int
    fields: Tuple[str, ...]
    checksum: str
    raw: str

    @property
    def is_request(self) -> bool:
        return self.start == START_REQUEST

    @property
    def kind(self) -> str:
        """请求帧返回 'request'，应答帧返回 ok/err/event/tele。"""
        return "request" if self.is_request else self.fields[0]

    @property
    def cmd(self) -> str:
        return self.fields[0]

    @property
    def args(self) -> Tuple[str, ...]:
        return self.fields[1:]

    @property
    def pairs(self) -> Dict[str, str]:
        """把 `key=value` 字段解析为字典，忽略不合规字段。"""
        out: Dict[str, str] = {}
        for item in self.fields[1:]:
            if "=" in item:
                key, _, value = item.partition("=")
                out[key] = value
        return out

    @property
    def error_code(self) -> Optional[str]:
        if self.kind == "err" and self.fields[1:]:
            return self.fields[1]
        return None

    @property
    def body(self) -> str:
        """payload 部分（id 与字段），用于会话去重比较。"""
        return ",".join(str(x) for x in (self.req_id,) + self.fields)


def parse_line(line: str) -> Frame:
    """解析单行（不含 LF）。结构、校验或字段非法时抛 ProtocolError。"""
    raw = line
    line = line.rstrip("\r")
    if not line:
        raise ProtocolError("E06", "空行")
    start = line[0]
    if start not in (START_REQUEST, START_REPLY):
        raise ProtocolError("E07", f"非法起始符: {start!r}")
    if CHECKSUM_SEP not in line:
        raise ProtocolError("E07", "缺少校验和分隔符 '*'")
    payload, _, checksum = line[1:].rpartition(CHECKSUM_SEP)
    if not verify_checksum(payload, checksum):
        raise ProtocolError("E07", f"校验和不匹配: {line!r}")
    if len(line) + 1 > MAX_FRAME_BYTES:
        raise ProtocolError("E06", f"帧长 {len(line) + 1} 超过 {MAX_FRAME_BYTES}")

    parts = payload.split(",")
    if len(parts) < 2:
        raise ProtocolError("E02", f"字段不足: {payload!r}")
    raw_id = parts[0]
    if not raw_id.isdigit():
        raise ProtocolError("E02", f"id 非十进制整数: {raw_id!r}")
    req_id = int(raw_id)
    if req_id > ID_MAX:
        raise ProtocolError("E02", f"id 超出范围: {req_id}")
    if start == START_REQUEST and req_id == ID_EVENT:
        raise ProtocolError("E02", "请求 id 不得为 0")
    if start == START_REPLY and req_id != ID_EVENT and not ID_MIN <= req_id <= ID_MAX:
        raise ProtocolError("E02", f"id 超出范围: {req_id}")

    fields = tuple(parts[1:])
    if start == START_REPLY:
        kind = fields[0]
        if kind == "err" and (len(fields) < 2 or fields[1] not in ERROR_CODES):
            raise ProtocolError("E05", f"未知错误码: {fields[1:2]}")
        if req_id == ID_EVENT and kind not in ("event", "tele"):
            raise ProtocolError("E05", f"id=0 只用于事件/遥测，收到 {kind!r}")
    return Frame(start=start, req_id=req_id, fields=fields, checksum=checksum.upper(), raw=raw)


# --------------------------------------------------------------------------
# 接收分帧（USB CDC 逐字节 / 任意分包与粘包）
# --------------------------------------------------------------------------


@dataclass
class Inbound:
    """一行的解析结果：成功则 `frame` 非空，失败则 `error` 非空。"""

    line: str
    frame: Optional[Frame] = None
    error: Optional[ProtocolError] = None

    @property
    def ok(self) -> bool:
        return self.frame is not None


class FramingBuffer:
    """逐字节分帧缓冲。

    处理 USB CDC 的分包/粘包：一个 64 字节 Bulk 包可能只有半条命令，
    一条 96 字节的帧也会跨包。超长时置 E06 并丢弃到 LF，不执行残缺行。
    """

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        self.max_frame_bytes = max_frame_bytes
        self._buf = bytearray()
        self._oversize = False
        self._pending_error: Optional[ProtocolError] = None
        self.dropped_oversize = 0
        self.dropped_junk = 0

    def reset(self) -> None:
        self._buf.clear()
        self._oversize = False
        self._pending_error = None

    @property
    def buffered(self) -> bytes:
        return bytes(self._buf)

    def feed(self, data: bytes) -> List[Inbound]:
        results: List[Inbound] = []
        for byte in data:
            if byte == 0x0A:  # LF
                line = self._buf.decode("ascii", errors="replace").rstrip("\r")
                oversize = self._oversize
                pending = self._pending_error
                self._buf.clear()
                self._oversize = False
                self._pending_error = None
                if oversize:
                    self.dropped_oversize += 1
                    results.append(Inbound(line=line, error=pending or ProtocolError("E06", "行过长")))
                    continue
                if not line:
                    continue
                try:
                    results.append(Inbound(line=line, frame=parse_line(line)))
                except ProtocolError as exc:
                    results.append(Inbound(line=line, error=exc))
                continue

            if self._oversize:
                continue

            if not self._buf:
                if byte not in (ord(START_REQUEST), ord(START_REPLY)):
                    self.dropped_junk += 1
                    continue
            self._buf.append(byte)
            if len(self._buf) > self.max_frame_bytes - 1:  # LF 也计入 96 字节上限
                self._oversize = True
                self._pending_error = ProtocolError("E06", "行过长，丢弃至 LF")
                self._buf.clear()
        return results


# --------------------------------------------------------------------------
# 请求校验表
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArgSpec:
    """单个位置参数的约束。"""

    name: str
    kind: str  # int / str / enum
    required: bool = True
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    choices: Tuple[str, ...] = ()
    axis: bool = False


def _axis(name: str = "addr") -> ArgSpec:
    return ArgSpec(name=name, kind="int", axis=True)


#: 命令 → 位置参数表。见方案 7.2 节命令清单。
COMMAND_SPECS: Dict[str, Tuple[ArgSpec, ...]] = {
    "hello": (
        ArgSpec("protocol_version", "int", minimum=1, maximum=65535),
        ArgSpec("session", "int", minimum=0, maximum=0xFFFFFFFF),
    ),
    "ping": (),
    "motor_enable": (_axis(), ArgSpec("on", "int", minimum=0, maximum=1)),
    "motor_speed": (
        _axis(),
        ArgSpec("dir", "int", minimum=0, maximum=1),
        ArgSpec("rpm", "int", minimum=1, maximum=6000),
        ArgSpec("acc", "int", minimum=0, maximum=255),
    ),
    "motor_pos": (
        _axis(),
        ArgSpec("dir", "int", minimum=0, maximum=1),
        ArgSpec("rpm", "int", minimum=1, maximum=6000),
        ArgSpec("acc", "int", minimum=0, maximum=255),
        ArgSpec("pulses", "int", minimum=0, maximum=0x7FFFFFFF),
        ArgSpec("mode", "enum", choices=("rel", "abs")),
    ),
    "motor_stop": (_axis(),),
    "stop_all": (),
    "motor_stall_clear": (_axis(),),
    "motor_zero": (_axis(),),
    "motor_query": (_axis(), ArgSpec("what", "enum", choices=("vel", "pos", "flag"))),
    "status": (),
    "gripper": (
        ArgSpec(
            "action",
            "enum",
            choices=("open", "grab", "ready", "step_grab", "step_release"),
        ),
    ),
    "servo_angle": (ArgSpec("deg", "int", minimum=0, maximum=270),),
    "param_get": (ArgSpec("name", "str"),),
    "param_set": (
        ArgSpec("name", "str"),
        ArgSpec("value", "int", minimum=-1000000, maximum=1000000),
    ),
    "tele_on": (_axis(), ArgSpec("interval_ms", "int", minimum=20, maximum=60000)),
    "tele_off": (),
    "stop_clear": (),
}


@dataclass
class ParsedRequest:
    """通过校验的请求。"""

    req_id: int
    cmd: str
    args: Tuple[str, ...]
    values: Dict[str, object]
    frame: Frame

    @property
    def addr(self) -> Optional[int]:
        return self.values.get("addr")  # type: ignore[return-value]


def validate_request(frame: Frame) -> ParsedRequest:
    """校验请求的字段数、整数格式、范围与轴白名单。"""
    if not frame.is_request:
        raise ProtocolError("E05", "不是请求帧")
    cmd = frame.cmd
    if cmd not in COMMAND_SPECS:
        raise ProtocolError("E01", f"未知命令: {cmd}")
    specs = COMMAND_SPECS[cmd]
    args = frame.args
    if len(args) != len(specs):
        raise ProtocolError("E02", f"{cmd} 需要 {len(specs)} 个参数，收到 {len(args)}")
    values: Dict[str, object] = {}
    for spec, token in zip(specs, args):
        if spec.kind == "int":
            if not token.lstrip("-").isdigit():
                raise ProtocolError("E02", f"{spec.name} 必须为整数: {token!r}")
            number = int(token)
            if spec.minimum is not None and number < spec.minimum:
                raise ProtocolError("E02", f"{spec.name}={number} 小于下限 {spec.minimum}")
            if spec.maximum is not None and number > spec.maximum:
                raise ProtocolError("E02", f"{spec.name}={number} 超过上限 {spec.maximum}")
            if spec.axis and number not in VALID_AXES:
                raise ProtocolError("E03", f"非法轴地址 {number}，仅允许 1/3/4/5")
            values[spec.name] = number
        elif spec.kind == "enum":
            if token not in spec.choices:
                raise ProtocolError("E02", f"{spec.name} 必须为 {'/'.join(spec.choices)}: {token!r}")
            values[spec.name] = token
        else:
            if not token or "," in token or "=" in token:
                raise ProtocolError("E02", f"{spec.name} 非法: {token!r}")
            values[spec.name] = token
    return ParsedRequest(req_id=frame.req_id, cmd=cmd, args=args, values=values, frame=frame)


# --------------------------------------------------------------------------
# 会话与请求去重
# --------------------------------------------------------------------------


class SessionCache:
    """缓存最近完成的请求，实现 `相同 id + 相同内容 → 重发应答`。"""

    NEW = "new"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"

    def __init__(self, capacity: int = 16) -> None:
        self.capacity = capacity
        self._entries: "Dict[int, Tuple[str, bytes]]" = {}
        self._order: List[int] = []

    def clear(self) -> None:
        self._entries.clear()
        self._order.clear()

    def check(self, req_id: int, body: str) -> str:
        entry = self._entries.get(req_id)
        if entry is None:
            return self.NEW
        return self.DUPLICATE if entry[0] == body else self.CONFLICT

    def cached_response(self, req_id: int) -> Optional[bytes]:
        entry = self._entries.get(req_id)
        return entry[1] if entry else None

    def record(self, req_id: int, body: str, response: bytes) -> None:
        if req_id in self._entries:
            self._order.remove(req_id)
        self._entries[req_id] = (body, response)
        self._order.append(req_id)
        while len(self._order) > self.capacity:
            oldest = self._order.pop(0)
            self._entries.pop(oldest, None)

    def __len__(self) -> int:
        return len(self._entries)


class RequestIdPool:
    """1～65535 的请求 id 分配器，避免复用在途 id。"""

    def __init__(self, start: int = 1) -> None:
        self._next = start
        self._in_flight: set = set()

    def acquire(self) -> int:
        for _ in range(ID_MAX):
            candidate = self._next
            self._next = ID_MIN if self._next >= ID_MAX else self._next + 1
            if candidate not in self._in_flight:
                self._in_flight.add(candidate)
                return candidate
        raise ProtocolError("E09", "无可用请求 id")

    def release(self, req_id: int) -> None:
        self._in_flight.discard(req_id)

    @property
    def in_flight(self) -> int:
        return len(self._in_flight)
