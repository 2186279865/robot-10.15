"""链路层：真实串口（pyserial 优先）、无 pyserial 时的应急回退、自动重连。

约定：本模块只负责“字节进、字节出”，不理解协议；协议语义在 `host_client.py`。
"失联保护"因此分成两层：
- 链路层（本模块）：发现读失败/断开 → 关闭链路并告知上层；
- 协议层（`HostClient`）：心跳超时 → 判定失联、禁止继续下发。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

try:  # pragma: no cover - 取决于运行环境
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore

    HAVE_PYSERIAL = True
except Exception:  # noqa: BLE001
    serial = None  # type: ignore
    list_ports = None  # type: ignore
    HAVE_PYSERIAL = False

#: STMicroelectronics 的 USB VID；CDC 虚拟串口通常为 0483:5740。
STM32_VID = 0x0483
STREAM_TIMEOUT_S = 0.05


class LinkError(RuntimeError):
    pass


@dataclass
class PortInfo:
    device: str
    description: str = ""
    hwid: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None

    @property
    def is_stm32(self) -> bool:
        return self.vid == STM32_VID or "0483" in self.hwid.upper()

    def label(self) -> str:
        tag = "STM32" if self.is_stm32 else "其他"
        return f"{self.device} [{tag}] {self.description}".strip()


def list_serial_ports() -> List[PortInfo]:
    """枚举串口；没有 pyserial 时退化为“只列出常见的 COM 名”。"""
    if HAVE_PYSERIAL and list_ports is not None:
        out: List[PortInfo] = []
        for port in list_ports.comports():
            out.append(
                PortInfo(
                    device=port.device,
                    description=port.description or "",
                    hwid=port.hwid or "",
                    vid=getattr(port, "vid", None),
                    pid=getattr(port, "pid", None),
                )
            )
        # STM32 虚拟串口排前面，方便现场一眼选中。
        out.sort(key=lambda p: (not p.is_stm32, p.device))
        return out
    return [PortInfo(device=f"COM{i}", description="（未安装 pyserial，名称需手填）") for i in range(1, 21)]


def sort_ports(ports: Sequence[PortInfo]) -> List[PortInfo]:
    return sorted(ports, key=lambda p: (not p.is_stm32, p.device))


# --------------------------------------------------------------------------
# 真实链路
# --------------------------------------------------------------------------


class SerialLink:
    """pyserial 实现。`read()` 超时返回空字节，不阻塞界面。"""

    is_simulated = False

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = STREAM_TIMEOUT_S) -> None:
        if not HAVE_PYSERIAL:
            raise LinkError("未安装 pyserial，无法使用真实串口；请先 pip install pyserial")
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._ser = None

    def open(self) -> None:
        try:
            self._ser = serial.Serial(  # type: ignore[union-attr]
                self.port, self.baudrate, timeout=self.timeout, write_timeout=1.0
            )
        except Exception as exc:  # noqa: BLE001
            self._ser = None
            raise LinkError(f"打开 {self.port} 失败：{exc}") from exc

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    @property
    def connected(self) -> bool:
        return self._ser is not None and bool(getattr(self._ser, "is_open", False))

    def write(self, data: bytes) -> int:
        if self._ser is None:
            raise LinkError("串口未打开")
        try:
            return int(self._ser.write(data))
        except Exception as exc:  # noqa: BLE001
            raise LinkError(f"写入 {self.port} 失败：{exc}") from exc

    def read(self) -> bytes:
        if self._ser is None:
            return b""
        try:
            waiting = getattr(self._ser, "in_waiting", 0)
            return bytes(self._ser.read(waiting or 1))
        except Exception as exc:  # noqa: BLE001
            raise LinkError(f"读取 {self.port} 失败：{exc}") from exc

    def describe(self) -> str:
        return f"{self.port} @{self.baudrate}（USB CDC 线路编码，不代表实际速率）"


class WinFileLink:
    """没有 pyserial 时的应急回退：直接打开 `\\\\.\\COMx`。

    能力有限，必须如实说明：不做 COMMTIMEOUTS 配置、不做流控、
    不保证写出长度与超时行为。仅用于“装不上 pyserial”的现场自救。
    """

    is_simulated = False
    limitations = "应急模式：无超时/流控配置，行为不如 pyserial 可控"

    def __init__(self, port: str, timeout: float = STREAM_TIMEOUT_S) -> None:
        self.port = port if port.startswith("\\\\.\\") else f"\\\\.\\{port}"
        self.timeout = timeout
        self._fd: Optional[int] = None

    def open(self) -> None:
        try:
            self._fd = os.open(self.port, os.O_RDWR | getattr(os, "O_BINARY", 0) | os.O_NOINHERIT)
        except OSError as exc:
            self._fd = None
            raise LinkError(f"打开 {self.port} 失败：{exc}") from exc

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
            time.sleep(0.05)

    @property
    def connected(self) -> bool:
        return self._fd is not None

    def write(self, data: bytes) -> int:
        if self._fd is None:
            raise LinkError("串口未打开")
        return os.write(self._fd, data)

    def read(self) -> bytes:
        if self._fd is None:
            return b""
        try:
            return os.read(self._fd, 4096)
        except OSError:
            # Windows 下无数据时可能直接报错，视作“暂无数据”。
            return b""

    def describe(self) -> str:
        return f"{self.port}（{self.limitations}）"


# --------------------------------------------------------------------------
# TCP 链路（WiFi 连树莓派上的串口桥 5058）
# --------------------------------------------------------------------------

#: 显式 TCP 前缀；只认 `tcp://host:port`，不会把 `COM5` 之类误判成 TCP。
TCP_PREFIX = "tcp://"


def is_tcp_port(port: str) -> bool:
    return isinstance(port, str) and port.strip().lower().startswith(TCP_PREFIX)


def parse_tcp_port(port: str):
    """把 `tcp://192.168.137.50:5058` 解析成 `(host, port)`；格式不对抛 LinkError。"""
    text = (port or "").strip()
    if not text.lower().startswith(TCP_PREFIX):
        raise LinkError(f"不是 TCP 端口：{port!r}（应为 tcp://IP:PORT）")
    body = text[len(TCP_PREFIX):].strip().strip("/")
    if not body:
        raise LinkError(f"TCP 端口缺少地址：{port!r}（应为 tcp://IP:PORT）")
    host, sep, raw_port = body.rpartition(":")
    if not sep or not host:
        raise LinkError(f"TCP 端口缺少端口号：{port!r}（应为 tcp://IP:PORT）")
    try:
        number = int(raw_port)
    except ValueError as exc:
        raise LinkError(f"TCP 端口号非法：{port!r}") from exc
    if not (0 < number < 65536):
        raise LinkError(f"TCP 端口号超出范围：{port!r}")
    return host, number


class SocketLink:
    """TCP 实现：连树莓派的串口桥，字节级透明，接口与 :class:`SerialLink` 完全一致。

    后台线程持续收字节塞进 `bytearray`；`read()` 只是取走当前已收到的字节，
    不阻塞界面。断线时 `connected` 变为 False 并记录原因（不抛到后台线程），
    需要重连时由上层（`HostClient` / `AutoReconnector`）重新构造。
    """

    is_simulated = False

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float = STREAM_TIMEOUT_S,
        connect_timeout: float = 3.0,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.baudrate = 115200  # 仅为与 SerialLink 的 describe 习惯保持一致的占位
        self._sock: Optional[socket.socket] = None
        self._rx = bytearray()
        self._lock = threading.Lock()
        self._alive = False
        self._thread: Optional[threading.Thread] = None
        self.last_error = ""
        self.rx_bytes = 0
        self.tx_bytes = 0

    def open(self) -> None:
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
        except OSError as exc:
            self.last_error = f"连接 {self.host}:{self.port} 失败：{exc}"
            raise LinkError(self.last_error) from exc
        sock.settimeout(self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        with self._lock:
            self._rx.clear()
            self._sock = sock
            self._alive = True
        self.last_error = ""
        thread = threading.Thread(
            target=self._rx_loop, name=f"SocketLink-{self.host}:{self.port}", daemon=True
        )
        self._thread = thread
        thread.start()

    def _rx_loop(self) -> None:
        while True:
            with self._lock:
                if not self._alive:
                    return
                sock = self._sock
            if sock is None:
                return
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                self._mark_lost(f"读取 {self.host}:{self.port} 失败：{exc}")
                return
            if not chunk:
                self._mark_lost("对端关闭连接")
                return
            with self._lock:
                self._rx += chunk
                self.rx_bytes += len(chunk)

    def _mark_lost(self, reason: str) -> None:
        self.last_error = reason
        with self._lock:
            self._alive = False
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        # 后台线程里只记录，不抛异常：断线由 connected=False 告知上层。

    def close(self) -> None:
        with self._lock:
            self._alive = False
            sock, self._sock = self._sock, None
            self._rx.clear()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._sock is not None and self._alive

    def write(self, data: bytes) -> int:
        with self._lock:
            sock = self._sock
        if sock is None or not self._alive:
            raise LinkError(self.last_error or "TCP 链路未连接")
        try:
            sock.sendall(data)
        except OSError as exc:
            self._mark_lost(f"写入 {self.host}:{self.port} 失败：{exc}")
            raise LinkError(self.last_error) from exc
        self.tx_bytes += len(data)
        return len(data)

    def read(self) -> bytes:
        with self._lock:
            if not self._rx:
                return b""
            data = bytes(self._rx)
            self._rx.clear()
        return data

    def describe(self) -> str:
        return f"TCP {self.host}:{self.port}"


def open_best_link(port: str, baudrate: int = 115200):
    """优先 pyserial，其次应急文件读写；`tcp://IP:PORT` 走 WiFi。"""
    if is_tcp_port(port):
        host, number = parse_tcp_port(port)
        return SocketLink(host, number)
    if HAVE_PYSERIAL:
        return SerialLink(port, baudrate=baudrate)
    return WinFileLink(port)


# --------------------------------------------------------------------------
# 自动重连
# --------------------------------------------------------------------------


class AutoReconnector:
    """断线后按退避重连，直到用户主动断开。

    只是“重新打开链路 + 重新握手”；握手拿到新的启动标识后，旧请求全部失效，
    因此重连后必须清除在途请求和界面上的“结果未知”标记（由 HostClient 负责）。
    """

    def __init__(
        self,
        client,
        factory: Callable[[], object],
        clock: Optional[Callable[[], float]] = None,
        base_ms: int = 1000,
        max_ms: int = 8000,
    ) -> None:
        self.client = client
        self.factory = factory
        self._clock = clock or time.monotonic
        self.base_ms = base_ms
        self.max_ms = max_ms
        self.armed = False
        self.attempts = 0
        self.next_try_ms = 0

    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    def arm(self) -> None:
        self.armed = True
        self.attempts = 0
        self.next_try_ms = 0

    def disarm(self) -> None:
        self.armed = False

    def tick(self) -> bool:
        """到点就重连一次；返回本次是否真的发起了重连。"""
        if not self.armed or self.client.state != "link_lost":
            return False
        now = self.now_ms()
        if now < self.next_try_ms:
            return False
        delay = min(self.max_ms, self.base_ms * (2 ** min(self.attempts, 3)))
        self.next_try_ms = now + delay
        self.attempts += 1
        try:
            link = self.factory()
        except Exception as exc:  # noqa: BLE001
            self.client.emit("note", f"重连构造链路失败：{exc}")
            return False
        self.client.attach(link)
        self.client.emit("note", f"第 {self.attempts} 次重连（退避 {delay} ms）")
        return self.client.connect()
