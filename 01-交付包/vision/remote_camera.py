"""远程相机：笔记本经 WiFi 从树莓派「拉」一帧（图传 5057，见 common/wire.py）。

为什么按需拉
------------
树莓派同时接两路相机，WiFi 带宽有限。如果让派主动推流，旧帧会积压、笔记本
看到的画面永远延迟。这里改成「笔记本要一帧 → 发 9 字节请求 → 派拍+压 JPEG →
回一包」，所以永远只有当前帧，不会有队列。

接口与 vision/camera.py 的 Camera / cv2.VideoCapture 保持一致：
    cam = RemoteCamera(index, w, h, fps, host, port)
    cam.open(); cam.cap.grab(); cam.cap.retrieve(); cam.close()
这样 vision/gui.py 的 Stream 一行都不用改（只换 make_camera）。

约定：index 就是请求里的 cam id（0=顶视，1=侧视）。width/height 传 0 表示
「用派端配置」，否则派按请求的分辨率出图。
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import cv2

# 10.21/common/wire.py 是两端唯一的协议实现；这里把它加进 sys.path。
_HERE = Path(__file__).resolve().parent          # 10.21/vision
_PKG_ROOT = _HERE.parent                         # 10.21
_COMMON = _PKG_ROOT / 'common'
for _p in (str(_COMMON), str(_PKG_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # 正常路径：common/wire.py
    import wire  # type: ignore
except Exception:  # pragma: no cover - 部署时 common/ 缺失的兜底
    wire = None  # type: ignore

#: 连接超时与单次收包超时（秒）。5 s 是验收要求：WiFi 抖动允许慢，但不许挂死。
CONNECT_TIMEOUT_S = 3.0
SOCKET_TIMEOUT_S = 5.0
#: 断线重连退避：0.5 → 1 → 2 → 3（封顶），连上后复位。
BACKOFF_MIN_S = 0.5
BACKOFF_MAX_S = 3.0


def _wire():
    if wire is None:
        raise RuntimeError('找不到 10.21/common/wire.py，无法使用远程相机')
    return wire


class RemoteCap:
    """模仿 cv2.VideoCapture 的远程版本（只实现 Stream 用到的子集）。

    线程安全：所有对 socket/frame 的访问都在 `self._lock` 内；GUI 线程调
    `release()` 时会先 shutdown 唤醒阻塞中的 recv，最多等 0.3 s。
    """

    def __init__(self, cam_id: int, width: int, height: int, fps: int,
                 host: str, port: int = None, quality: int = 0,
                 timeout: float = SOCKET_TIMEOUT_S,
                 connect_timeout: float = CONNECT_TIMEOUT_S) -> None:
        w = _wire()
        self.cam_id = int(cam_id)
        self.width = int(width or 0)
        self.height = int(height or 0)
        self.fps = int(fps or 0)
        self.quality = int(quality or 0)
        self.host = str(host)
        self.port = int(port if port is not None else w.IMG_PORT)
        self.timeout = float(timeout)
        self.connect_timeout = float(connect_timeout)
        self._lock = threading.RLock()
        self._sock = None
        self._frame = None
        self._closed = False
        self._backoff = BACKOFF_MIN_S
        self._next_try = 0.0
        self._last_log = 0.0
        self.last_error = ''
        self.last_meta = {}
        self.frames = 0

    # -- 连接管理 -----------------------------------------------------------
    def _connect_locked(self) -> bool:
        """在持锁状态下建立连接（失败返回 False，不抛给后台线程）。"""
        if self._closed:
            return False
        if self._sock is not None:
            return True
        now = time.monotonic()
        if now < self._next_try:
            return False
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
        except OSError as exc:
            self._note_error(f'连接 {self.host}:{self.port} 失败：{exc}')
            self._schedule_retry()
            return False
        sock.settimeout(self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._sock = sock
        self._backoff = BACKOFF_MIN_S
        self._next_try = 0.0
        self.last_error = ''
        print(f'[remote_camera] 已连接图传 {self.host}:{self.port}（cam {self.cam_id}）',
              flush=True)
        return True

    def _schedule_retry(self) -> None:
        self._next_try = time.monotonic() + self._backoff
        self._backoff = min(BACKOFF_MAX_S, self._backoff * 2.0)

    def _note_error(self, text: str) -> None:
        """记录错误；同一错误最多每 2 s 打一次，避免刷屏。"""
        self.last_error = text
        now = time.monotonic()
        if now - self._last_log >= 2.0:
            self._last_log = now
            print(f'[remote_camera] {text}', flush=True)

    def _drop_socket_locked(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # -- cv2 风格接口 -------------------------------------------------------
    def open(self) -> bool:
        with self._lock:
            return self._connect_locked()

    def grab(self) -> bool:
        """拉一帧：发请求 → 收 JPEG → imdecode 存进 self._frame。成功返回 True。

        断线时按 0.5→3 s 退避重连；任何网络异常都只记错返回 False，
        绝不把异常抛给 GUI 后台线程（否则整个预览线程直接退出）。
        """
        with self._lock:
            if self._closed:
                return False
            if not self._connect_locked():
                return False
            sock = self._sock
            try:
                sock.sendall(_wire().pack_request(
                    self.cam_id, self.width, self.height, self.quality))
                # 先只读 2 字节 magic：图传头 24B（IM）和错误头 6B（ER）长度不同，
                # 若一律按 24B 读，错误包（6B 头 + 文本体）会被多吞掉一部分文本，
                # 再按 body 长度读就会一直等到超时（表现为 timed out 而不是报错）。
                magic = _wire().recv_exact(sock, 2)
                if magic == _wire().MAGIC_ERR:
                    head = magic + _wire().recv_exact(sock, _wire().ERR_HDR_LEN - 2)
                    err = _wire().parse_error_header(head)
                    body = _wire().recv_exact(sock, err['n']) if err['n'] else b''
                    msg = err['text'] + ('：' + body.decode('utf-8', 'replace') if body else '')
                    self._note_error(f'派端报错（cam {self.cam_id}）：{msg}')
                    return False
                if magic != _wire().MAGIC_IMG:
                    raise ValueError(f'未知应答 magic={magic!r}')
                head = magic + _wire().recv_exact(sock, _wire().IMG_HDR_LEN - 2)
                info = _wire().parse_image_header(head)
                jpeg = _wire().recv_exact(sock, info['n'])
                frame = cv2.imdecode(_from_bytes(jpeg), cv2.IMREAD_COLOR)
                if frame is None or frame.size == 0:
                    self._note_error(f'JPEG 解码失败（{info["n"]} 字节）')
                    return False
                self._frame = frame
                self.last_meta = info
                self.frames += 1
                self.last_error = ''
                self._backoff = BACKOFF_MIN_S
                return True
            except (OSError, ConnectionError, socket.timeout) as exc:
                self._note_error(f'取帧失败：{exc}')
                self._drop_socket_locked()
                self._schedule_retry()
                return False
            except Exception as exc:  # noqa: BLE001 - 协议解析异常同样不能炸线程
                self._note_error(f'取帧异常：{exc}')
                self._drop_socket_locked()
                self._schedule_retry()
                return False

    def retrieve(self):
        with self._lock:
            if self._frame is None:
                return False, None
            return True, self._frame.copy()

    def read(self):
        if self.grab():
            with self._lock:
                return True, (None if self._frame is None else self._frame.copy())
        return False, None

    def isOpened(self) -> bool:
        with self._lock:
            return self._sock is not None and not self._closed

    def release(self) -> None:
        # 先 shutdown 再抢锁：grab() 可能正卡在 recv 里持锁，shutdown 能立刻把它
        # 唤醒（收包抛 OSError），GUI 线程就不会被一次 5 s 超时冻住。
        self._closed = True
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        got = self._lock.acquire(timeout=0.5)
        try:
            if got:   # 抢不到锁说明 grab 正在收尾，它自己会把 socket 关掉
                self._drop_socket_locked()
            self._frame = None
        finally:
            if got:
                self._lock.release()
        self._next_try = 0.0


def _from_bytes(data: bytes):
    import numpy as np
    return np.frombuffer(data, dtype=np.uint8)


class RemoteCamera:
    """与 vision/camera.py 的 Camera 同接口的远程相机。

    `read()` 失败时抛 RuntimeError（和本地 Camera 一致），`close()` 可重复调用。
    """

    def __init__(self, index, width, height, fps, host, port=None, quality=0) -> None:
        self.index = int(index)
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.host = str(host)
        self.port = int(port) if port is not None else _wire().IMG_PORT
        self.quality = int(quality or 0)
        self.cap = None

    def open(self) -> None:
        if self.cap is None:
            self.cap = RemoteCap(self.index, self.width, self.height, self.fps,
                                 self.host, self.port, quality=self.quality)
        ok = self.cap.open()
        if not ok:
            if self.cap._closed:
                raise RuntimeError(f'远程相机已关闭 index={self.index}')
            print(f'[remote_camera] 暂时连不上 {self.host}:{self.port}，'
                  f'后台会按 {BACKOFF_MIN_S}→{BACKOFF_MAX_S} s 退避重试', flush=True)

    def read(self):
        if self.cap is None:
            self.open()
        ok, frame = self.cap.read()
        if not ok or frame is None:
            raise RuntimeError(
                f'远程相机读取失败 index={self.index} {self.host}:{self.port}：'
                f'{self.cap.last_error or "无数据"}')
        return frame

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()

    def isOpened(self) -> bool:
        return self.cap is not None and self.cap.isOpened()

    def describe(self) -> str:
        return f'远程相机 {self.host}:{self.port} cam{self.index}'

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
