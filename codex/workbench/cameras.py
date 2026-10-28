from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path

import cv2
import numpy as np

REQUEST = struct.Struct('<2sHHHB')
HEADER = struct.Struct('<2sHHHIQI')


def read_image(path):
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f'无法读取图片：{path}')
    return image


def save_image(path, frame):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(path.suffix or '.png', frame)
    if not ok:
        raise ValueError('图片编码失败')
    encoded.tofile(str(path))


def recv_exact(sock, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError('图传连接已关闭')
        buf.extend(chunk)
    return bytes(buf)


class RemoteCamera:
    def __init__(self, host, port, index, width=0, height=0):
        self.address = (host, int(port))
        self.index, self.width, self.height = int(index), int(width), int(height)
        self.sock = None

    def read(self):
        if self.sock is None:
            self.sock = socket.create_connection(self.address, timeout=2)
            self.sock.settimeout(2)
        try:
            self.sock.sendall(REQUEST.pack(b'RQ', self.index, self.width, self.height, 80))
            magic = recv_exact(self.sock, 2)
            if magic == b'ER':
                code, size = struct.unpack('<HH', recv_exact(self.sock, 4))
                raise RuntimeError(f'派端相机错误 {code}：' + recv_exact(self.sock, size).decode('utf-8', 'replace'))
            if magic != b'IM':
                raise ValueError('未知图传报文')
            fields = HEADER.unpack(magic + recv_exact(self.sock, HEADER.size - 2))
            size = fields[-1]
            if not 0 < size <= 20 * 1024 * 1024:
                raise ValueError('图传长度超限')
            frame = cv2.imdecode(np.frombuffer(recv_exact(self.sock, size), np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError('JPEG 解码失败')
            return frame
        except Exception:
            self.close()
            raise

    def close(self):
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class LocalCamera:
    def __init__(self, index, width, height):
        self.index, self.width, self.height = int(index), int(width), int(height)
        self.cap = None

    def read(self):
        if self.cap is None:
            self.cap = cv2.VideoCapture(self.index)
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        ok, frame = self.cap.read()
        if not ok:
            self.close()
            raise RuntimeError(f'本地相机 {self.index} 无画面')
        return frame

    def close(self):
        cap, self.cap = self.cap, None
        if cap:
            cap.release()


def demo_frame():
    frame = np.full((720, 960, 3), 155, np.uint8)
    cv2.circle(frame, (200, 230), 65, (220, 70, 30), -1)
    cv2.rectangle(frame, (400, 160), (535, 295), (35, 175, 45), -1)
    pts = np.array([[720 + 75 * np.cos(t), 235 + 75 * np.sin(t)]
                    for t in np.linspace(0, 2 * np.pi, 6)[:-1]], np.int32)
    cv2.fillPoly(frame, [pts], (30, 40, 220))
    cv2.circle(frame, (300, 510), 75, (20, 190, 230), -1)
    cv2.circle(frame, (300, 510), 34, (155, 155, 155), -1)
    cv2.rectangle(frame, (580, 450), (780, 555), (155, 40, 150), -1)
    return frame


class CameraStream:
    def __init__(self, source):
        self.source = source
        self.frame = None
        self.stamp = 0.0
        self.error = ''
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                frame = self.source.read()
                with self.lock:
                    self.frame, self.stamp, self.error = frame, time.monotonic(), ''
            except Exception as exc:
                self.error = str(exc)
                self.stop_event.wait(0.5)
            self.stop_event.wait(0.035)
        self.source.close()

    def get(self, max_age=2.0):
        with self.lock:
            if self.frame is None or time.monotonic() - self.stamp > max_age:
                return None
            return self.frame.copy()

    def stop(self):
        self.stop_event.set()
        # Socket shutdown wakes a blocked reader. USB capture is closed by its own thread.
        if isinstance(self.source, RemoteCamera):
            self.source.close()


def make_stream(cfg, view):
    cls = RemoteCamera if cfg['mode'] == 'remote' else LocalCamera
    args = [cfg[view], cfg['width'], cfg['height']]
    if cls is RemoteCamera:
        args = [cfg['host'], cfg['port']] + args
    stream = CameraStream(cls(*args))
    stream.start()
    return stream
