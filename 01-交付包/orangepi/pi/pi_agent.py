#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pi_agent.py —— 树莓派端统一服务（图传 + 串口桥 + UDP 发现）。

三种服务，全部线程实现，日志带时间戳：

1) 图传 TCP（默认 5057 / wire.IMG_PORT，拉模式）
   笔记本连上来后反复发 9 字节请求（wire.REQ），派端抓**最新一帧**、压 JPEG、
   用 wire.IMG_HDR 回包。请求里的 w/h/q 传 0 表示“用派端配置”。
   抓不到图回 wire.ERR（E_NO_CAMERA 等）但**不断连接**。

2) 串口桥 TCP（默认 5058 / wire.SER_PORT，字节级透明）
   笔记本发来的上位机协议行原样写进 STM32 的 USB CDC 串口；单片机回包原样回传。
   **看门狗**：连续 wire.SERIAL_WATCHDOG_MS(1500ms) 没收到笔记本字节，就向串口
   写一次 wire.STOP_FRAMES（四轴停车帧），状态标记“已护栏”，收到新字节后复位；
   每个空闲期只发一次，避免刷屏。
   多客户端策略：**允许并发连接**（不拒绝、不排队）。
     - 笔记本→串口方向：加写锁串行化，字节流不会被两个客户端交织；
     - 串口→笔记本方向：广播给所有已连接客户端（多个上位机看到同一份回包）。
   对“单上位机”这个真实用法与拒绝策略等价，但方便现场用两个工具同时看回包。

3) UDP 发现（默认 5056，`--no-discovery` 关闭）
   收到 wire.DISCOVERY_PROBE 就回 wire.pack_discovery_reply(本机局域网 IP, 端口, 名字)。

调试开关：
   `--fake-camera`  用 numpy 画 1280x720 合成画面（浅色台面 + 3 个缓慢移动的彩色
                    方块 + 左上角时间戳），笔记本没有相机时联调用；
   `--fake-serial`  进程内假 STM32：按 `10.21/host/protocol.py` 的真实帧格式
                    （`$<id>,<cmd>,<args>*<CS>\\n`，CS = 逐字节异或两位大写十六进制）
                    解析并发 `#<id>,ok,...\\n` / `#<id>,err,E0x,...\\n`。
                    **不依赖 pyserial**，Windows 上可直接跑。

依赖：标准库 + cv2 + numpy + pyserial（pyserial 只是真串口模式需要，可选导入）。
运行（Windows 联调）::

    python 10.21/pi/pi_agent.py --fake-camera --fake-serial \
        --img-port 5057 --ser-port 5058 --name robot-pi

运行（树莓派）::

    python3 pi_agent.py                 # 读同目录 config.json
    python3 pi_agent.py --cam-index 0 --cam-index 2 --width 1280 --height 720
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

# ----------------------------------------------------------------- 路径与依赖
BASE_DIR = Path(__file__).resolve().parent
# 交付包里的布局是 01-交付包/{orangepi/pi, common}，所以 common/ 在上一级；
# 老布局（10.21/{pi, common}）里它在父目录的父目录。两种都试，找不到不报错。
_COMMON_CANDIDATES = (
    BASE_DIR.parent.parent / 'common',   # 01-交付包/common
    BASE_DIR.parent / 'common',          # 10.21/common（同目录布局）
)
COMMON_DIR = next((p for p in _COMMON_CANDIDATES if p.is_dir()), _COMMON_CANDIDATES[0])
sys.path.insert(0, str(COMMON_DIR))

import wire  # noqa: E402  （10.21/common/wire.py，只读，禁止修改）

try:  # 图像处理：树莓派上装了 opencv-python-headless 或 apt 的 python3-opencv
    import cv2
    HAVE_CV2 = True
except Exception:  # pragma: no cover - 只在没装 opencv 的机器上走
    cv2 = None
    HAVE_CV2 = False

try:
    import numpy as np
    HAVE_NUMPY = True
except Exception:  # pragma: no cover
    np = None
    HAVE_NUMPY = False

try:  # pyserial 只在“真串口”模式下需要；--fake-serial 不需要它
    import serial
    HAVE_PYSERIAL = True
except Exception:
    serial = None
    HAVE_PYSERIAL = False


DEFAULTS = {
    'host': '0.0.0.0',
    'img_port': wire.IMG_PORT,
    'ser_port': wire.SER_PORT,
    'discovery_port': wire.DISCOVERY_PORT,
    'serial': '/dev/ttyACM0',
    'baud': 115200,
    'cameras': [0],
    'width': 1280,
    'height': 720,
    'fps': 30,
    'quality': 80,
    'name': 'robot-pi',
}

#: 请求里允许的最大宽高，防止笔记本发来离谱数字把内存吃光。
MAX_DIM = 4096
MIN_DIM = 16


# ----------------------------------------------------------------- 日志
class Logger:
    """带时间戳的线程安全日志：`[2026-10-21 21:52:41.123] [tag] ...`。"""

    def __init__(self, path=None):
        self._lock = threading.Lock()
        self._fh = None
        self._console_encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
        if path:
            p = Path(path).expanduser()
            if not p.is_absolute():
                # 相对路径按“启动时的工作目录”解释（systemd 下就是 WorkingDirectory）
                p = Path.cwd() / p
            p.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(p, 'a', encoding='utf-8')
            self.path = str(p)
        else:
            self.path = ''

    def __call__(self, tag, msg, *fmt):
        if fmt:                       # 允许 log('fw', '未知命令 %s', cmd) 这种写法
            try:
                msg = msg % fmt
            except (TypeError, ValueError):
                msg = ' '.join([str(msg)] + [str(x) for x in fmt])
        now = time.time()
        stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
        line = '[%s.%03d] [%s] %s' % (stamp, int(now * 1000) % 1000, tag, msg)
        with self._lock:
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                # 老式 Windows 控制台是 GBK，遇到不认识的字符宁可替换也别崩
                try:
                    sys.stdout.write(
                        line.encode(self._console_encoding, 'replace')
                        .decode(self._console_encoding, 'replace') + '\n')
                    sys.stdout.flush()
                except Exception:
                    pass
            except Exception:
                pass
            if self._fh is not None:
                self._fh.write(line + '\n')
                self._fh.flush()

    def close(self):
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


# ----------------------------------------------------------------- 小工具
def local_ip(dst_ip=None):
    """取本机局域网 IPv4：连 8.8.8.8 看本端地址，失败退回 127.0.0.1。

    给了 dst_ip 就先试“连这个对端”，这样多网卡时回给探测方的是可达的那个地址。
    """
    targets = []
    if dst_ip:
        targets.append((dst_ip, 80))
    targets.append(('8.8.8.8', 80))
    for target in targets:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect(target)
            ip = s.getsockname()[0]
            if ip and not ip.startswith('127.'):
                return ip
        except OSError:
            continue
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
    return '127.0.0.1'


#: 启动期致命错误（配置不对/缺依赖）：打印原因后以退出码 2 结束，
#: 让 systemd 的 journal 里第一屏就能看到真正原因，而不是一个"活着但没串口"的空壳。
class StartupError(RuntimeError):
    pass


def fmt_bytes(data, limit=72):
    """把字节流格式化成日志友好的形式：能打印的按 ASCII，其余按十六进制。"""
    if data is None:
        return ''
    head = data[:limit]
    try:
        text = head.decode('ascii')
        if all(32 <= ord(ch) < 127 or ch in '\r\n\t' for ch in text):
            return repr(text) + ('…' if len(data) > limit else '')
    except UnicodeDecodeError:
        pass
    return head.hex(' ') + (' …%dB' % len(data) if len(data) > limit else '')


# =========================================================================
# 相机
# =========================================================================
class CameraWorker:
    """一路相机 = 一个采集线程 + “最新一帧”缓存。

    采集线程一直抓图并覆盖缓存，图传线程永远读**最新一帧**，天然丢掉积压。
    同一相机被多个连接并发请求时靠 `_lock` 保护缓存。
    """

    def __init__(self, cam_id, index, width, height, fps, fake, log):
        self.cam_id = cam_id
        self.index = index
        self.width = width
        self.height = height
        self.fps = max(1, int(fps))
        self.fake = fake
        self.log = log
        self._lock = threading.Lock()
        self._latest = None
        self._latest_ts_us = 0
        self._frames = 0
        self._stop = threading.Event()
        self._thread = None
        self.opened = False
        self.last_error = ''

    # -- 生命周期 ---------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name='cam%d' % self.cam_id,
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    # -- 取帧 -------------------------------------------------------------
    def get_latest(self):
        """返回 (frame, ts_us)；还没有出图时返回 (None, 0)。"""
        with self._lock:
            return self._latest, self._latest_ts_us

    def available(self):
        with self._lock:
            return self._latest is not None

    def _store(self, frame):
        with self._lock:
            self._latest = frame
            self._latest_ts_us = time.time_ns() // 1000
            self._frames += 1

    # -- 采集线程 ---------------------------------------------------------
    def _run(self):
        if self.fake:
            self._run_fake()
        else:
            self._run_real()

    def _run_fake(self):
        if not (HAVE_CV2 and HAVE_NUMPY):
            self.last_error = '缺少 cv2/numpy，无法生成假画面'
            self.log('cam', '相机 %d：%s' % (self.cam_id, self.last_error))
            return
        period = 1.0 / float(self.fps)
        t0 = time.time()
        self.opened = True
        self.log('cam', '相机 %d 使用假画面（合成 %dx%d@%dfps）'
                 % (self.cam_id, self.width, self.height, self.fps))
        while not self._stop.is_set():
            begin = time.time()
            frame = synthesize_frame(self.cam_id, begin - t0, self.width, self.height)
            self._store(frame)
            rest = period - (time.time() - begin)
            if rest > 0:
                self._stop.wait(rest)

    def _run_real(self):
        if not HAVE_CV2:
            self.last_error = '未安装 opencv（cv2）'
            self.log('cam', '相机 %d：%s' % (self.cam_id, self.last_error))
            return
        cap = None
        failures = 0
        period = 1.0 / float(self.fps)
        while not self._stop.is_set():
            if cap is None:
                cap = self._open_capture()
                if cap is None:
                    self.last_error = '打开设备 %d 失败' % self.index
                    self.log('cam', '相机 %d（设备 %d）打开失败，1s 后重试'
                             % (self.cam_id, self.index))
                    self._stop.wait(1.0)
                    continue
                self.opened = True
                self.last_error = ''
                self.log('cam', '相机 %d 已打开：设备 %d %dx%d@%dfps'
                         % (self.cam_id, self.index, self.width, self.height, self.fps))
            begin = time.time()
            try:
                ok, frame = cap.read()
            except Exception as exc:  # 有的后端会直接抛
                ok, frame = False, None
                self.log('cam', '相机 %d 读帧异常：%s' % (self.cam_id, exc))
            if not ok or frame is None:
                failures += 1
                if failures >= 5:
                    self.log('cam', '相机 %d 连续抓帧失败，重开设备' % self.cam_id)
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None
                    self.opened = False
                    failures = 0
                    self._stop.wait(0.8)
                else:
                    self._stop.wait(0.05)
                continue
            failures = 0
            self._store(frame)
            rest = period - (time.time() - begin)
            if rest > 0:
                self._stop.wait(rest)

    def _open_capture(self):
        """Linux 用 V4L2；Windows 依次回退 DSHOW / MSMF / 任意后端。"""
        if sys.platform.startswith('linux'):
            apis = [cv2.CAP_V4L2]
        elif os.name == 'nt':
            apis = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        else:
            apis = [cv2.CAP_ANY]
        for api in apis:
            cap = None
            try:
                cap = cv2.VideoCapture(self.index, api)
                if not cap.isOpened():
                    cap.release()
                    continue
                # 两台相机同时跑 720p 时必须走 MJPG：YUYV 单路就要 ~55 MB/s，
                # 两个 USB2 相机挂在同一个 hub 上供不起，表现就是"连续抓帧失败"。
                if sys.platform.startswith('linux'):
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
                cap.set(cv2.CAP_PROP_FPS, float(self.fps))
                try:  # 缓冲 1 帧：配合“抓最新一帧”，减少延迟
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass
                real_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                real_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                if real_w and real_h and (real_w, real_h) != (self.width, self.height):
                    self.log('cam', '相机 %d 实际输出 %dx%d（请求 %dx%d）'
                             % (self.cam_id, real_w, real_h, self.width, self.height))
                return cap
            except Exception as exc:
                self.log('cam', '相机 %d 后端 %s 打开异常：%s' % (self.cam_id, api, exc))
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
        return None


def synthesize_frame(cam_id, t, width, height):
    """合成假画面：浅色台面 + 网格 + 3 个缓慢移动的彩色方块 + 左上角时间戳。"""
    img = np.zeros((height, width, 3), np.uint8)
    img[:, :] = (196, 202, 210)          # BGR 浅灰蓝台面
    grid = (176, 182, 194)
    for x in range(0, width, 80):
        img[:, x:x + 1] = grid
    for y in range(0, height, 80):
        img[y:y + 1, :] = grid
    colors = ((60, 120, 255), (80, 200, 90), (255, 150, 60))   # 红 / 绿 / 蓝
    half = max(10, min(width, height) // 12)
    for i, color in enumerate(colors[:3]):
        cx = int(width * (0.25 + 0.2 * i) + width * 0.28 * math.sin(t * 0.6 + i * 2.1))
        cy = int(height * 0.5 + height * 0.30 * math.cos(t * 0.45 + i * 1.7))
        cx = max(half + 2, min(width - half - 2, cx))
        cy = max(half + 2, min(height - half - 2, cy))
        cv2.rectangle(img, (cx - half, cy - half), (cx + half, cy + half), color, -1)
        cv2.putText(img, str(i + 1), (cx - 8, cy + 8), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (255, 255, 255), 2, cv2.LINE_AA)
    now = time.time()
    stamps = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
    cv2.putText(img, 'FAKE CAM %d  %s.%03d' % (cam_id, stamps, int(now * 1000) % 1000),
                (12, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (24, 24, 24), 2, cv2.LINE_AA)
    cv2.putText(img, '%dx%d  t=%.1fs' % (width, height, t),
                (12, 68), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 60, 60), 2, cv2.LINE_AA)
    return img


# =========================================================================
# 图传 TCP 服务
# =========================================================================
class ImageServer:
    """图传服务：每连接一个线程，循环“读 9 字节请求 → 回一帧 JPEG/错误”。"""

    def __init__(self, bind_host, port, cameras, quality, log, stop_event):
        self.bind_host = bind_host
        self.port = int(port)
        self.cameras = cameras          # {cam_id: CameraWorker}
        self.quality = int(quality)
        self.log = log
        self.stop_event = stop_event
        self.sock = None
        self._thread = None
        self._lock = threading.Lock()
        self._seq = 0
        self.frames_served = 0
        self.errors_sent = 0
        self.bytes_sent = 0
        self.connections = 0

    # -- 生命周期 ---------------------------------------------------------
    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind_host, self.port))
        s.listen(8)
        s.settimeout(0.5)
        self.sock = s
        self._thread = threading.Thread(target=self._accept_loop, name='img-accept',
                                        daemon=True)
        self._thread.start()
        self.log('img', '图传服务已监听 %s:%d（相机 %s）'
                 % (self.bind_host, self.port,
                    ', '.join(str(c) for c in sorted(self.cameras)) or '无'))

    def stop(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def _accept_loop(self):
        while not self.stop_event.is_set():
            sock = self.sock
            if sock is None:
                return
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            self.log('img', '笔记本已连接 %s:%d' % (addr[0], addr[1]))
            threading.Thread(target=self._serve, args=(conn, addr), name='img-conn',
                             daemon=True).start()

    # -- 单连接 -----------------------------------------------------------
    def _serve(self, conn, addr):
        peer = '%s:%d' % (addr[0], addr[1])
        try:
            conn.settimeout(30.0)      # 半分钟没请求就当掉线，防止线程泄漏
            while not self.stop_event.is_set():
                try:
                    head = wire.recv_exact(conn, wire.REQ_LEN)
                except socket.timeout:
                    continue
                except (ConnectionError, OSError):
                    break
                try:
                    req = wire.parse_request(head)
                except (ValueError, TypeError) as exc:
                    self._error(conn, wire.E_BAD_REQUEST, '请求解析失败: %s' % exc)
                    continue
                self._handle_request(conn, req)
        except (ConnectionError, OSError) as exc:
            if not self.stop_event.is_set():
                self.log('img', '%s 连接异常：%s' % (peer, exc))
        except Exception as exc:  # 单连接出问题不能拖垮服务
            self.log('img', '%s 处理异常：%r' % (peer, exc))
        finally:
            self._safe_close(conn)
            self.log('img', '%s 已断开' % peer)

    def _handle_request(self, conn, req):
        cam_id = int(req['cam'])
        worker = self.cameras.get(cam_id)
        if worker is None:
            self._error(conn, wire.E_NO_CAMERA,
                        '相机 %d 未配置（可选：%s）'
                        % (cam_id, ', '.join(str(c) for c in sorted(self.cameras)) or '无'))
            return
        frame, _ts_us = worker.get_latest()
        if frame is None:
            self._error(conn, wire.E_NO_CAMERA,
                        '相机 %d 还没出图（%s）'
                        % (cam_id, worker.last_error or '采集中'))
            return
        try:
            out = self._encode(frame, req, worker)
        except Exception as exc:
            self._error(conn, wire.E_ENCODE_FAIL, '编码异常: %r' % (exc,))
            return
        if out is None:
            self._error(conn, wire.E_NO_CAMERA if not HAVE_CV2 else wire.E_ENCODE_FAIL,
                        '派端无法编码 JPEG' if not HAVE_CV2 else 'cv2.imencode 失败')
            return
        jpeg, out_w, out_h = out
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            seq = self._seq
        packet = wire.pack_image(cam_id, out_w, out_h, seq,
                                 time.time_ns() // 1000, jpeg)
        if self._send(conn, packet):
            with self._lock:
                self.frames_served += 1
                self.bytes_sent += len(packet)

    def _encode(self, frame, req, worker):
        """按请求的 w/h/q 缩放并压 JPEG；w/h/q 为 0 用派端配置。返回 (jpeg,w,h)。"""
        if not HAVE_CV2:
            return None
        frame_h, frame_w = frame.shape[:2]
        want_w = int(req['width']) or worker.width or frame_w
        want_h = int(req['height']) or worker.height or frame_h
        want_w = max(MIN_DIM, min(MAX_DIM, want_w))
        want_h = max(MIN_DIM, min(MAX_DIM, want_h))
        img = frame
        if (want_w, want_h) != (frame_w, frame_h):
            interp = cv2.INTER_AREA if (want_w <= frame_w and want_h <= frame_h) \
                else cv2.INTER_LINEAR
            img = cv2.resize(frame, (want_w, want_h), interpolation=interp)
        quality = int(req['quality']) or self.quality
        quality = max(1, min(100, quality))
        ok, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return None
        return buf.tobytes(), img.shape[1], img.shape[0]

    def _send(self, conn, data):
        try:
            conn.sendall(data)
            return True
        except (ConnectionError, OSError):
            return False

    def _error(self, conn, code, msg):
        with self._lock:
            self.errors_sent += 1
        self._send(conn, wire.pack_error(code, msg))

    def _safe_close(self, conn):
        try:
            conn.close()
        except OSError:
            pass


# =========================================================================
# 假 STM32（--fake-serial）
# =========================================================================
#: 与 10.21/host/protocol.py 保持一致（本模块只读，运行时不再依赖它）。
VALID_AXES = (1, 3, 4, 5)
COUNTS_PER_REV = 65536
FAKE_FW = 'pi-sim-0.1'
PROTOCOL_VERSION = 1
PARAM_DEFAULTS = {
    'speed_rpm': 60,
    'acc': 1,
    'speed_rpm_max': 300,
    'gripper_open_deg': 180,
    'gripper_ready_deg': 150,
    'gripper_grab_deg': 135,
    'gripper_step_deg': 45,
    'gripper_settle_ms': 200,
    #: 固件调试兜底：非夹紧状态最多保持多久（uint16，0=不卸力）
    'gripper_hold_max_ms': 0,
    'heartbeat_ms': 500,
    'link_timeout_ms': 3000,
    'query_timeout_ms': 100,
    'telemetry_ms': 200,
    'frame_gap_ms': 10,
    'readback_mask': 15,
}
ERROR_CODES = {
    'E01': '未知命令', 'E02': '参数错误', 'E03': '地址错误', 'E04': '查询超时',
    'E05': '回包错误', 'E06': '缓冲溢出/行过长', 'E07': '校验错误',
    'E08': '使能未确认', 'E09': '忙/队列满', 'E10': '停止锁存',
    'E11': '无回读能力', 'E12': '请求id冲突', 'E13': '位置基准无效',
}


class FakeError(Exception):
    """假固件内部的协议错误，带一个 E0x 码。"""

    def __init__(self, code, *pairs):
        self.code = code
        self.pairs = pairs
        super().__init__(code)


def xor_checksum(payload):
    """payload 逐字节异或（两位大写十六进制由 checksum_text 给出）。"""
    value = 0
    for ch in payload:
        value ^= ord(ch)
    return value


def checksum_text(payload):
    return '%02X' % (xor_checksum(payload) & 0xFF)


def make_frame(start, body):
    """组装一帧 ASCII 文本：`<start><body>*<CS>\\n`。"""
    return ('%s%s*%s\n' % (start, body, checksum_text(body))).encode('ascii')


class FakeSTM32:
    """进程内“假 STM32”：`feed(bytes)` 进字节、返回要回传的字节。

    行为与 `10.21/host/simulator.py` 对齐，但自带分帧/校验实现，只依赖标准库，
    这样树莓派上不需要拷 host/ 目录也能 `--fake-serial` 联调。
    """

    _MAX_FRAME = 96

    def __init__(self, log=None):
        self.log = log or (lambda *a: None)
        self._lock = threading.RLock()
        self.axes = {
            addr: {'pos': 0, 'target': 0, 'rpm': 0, 'dir': 0, 'acc': 1,
                   'mode': 'stop', 'enabled': False, 'stall': False,
                   'last_rb': self._now_ms()}
            for addr in VALID_AXES
        }
        self.gripper = {'angle': 180, 'open': 180, 'ready': 150, 'grab': 135,
                        'step': 45, 'settle': 200, 'hold_max_ms': 0,
                        'busy_until': 0}
        self.params = dict(PARAM_DEFAULTS)
        self.latch = False
        self.latch_reason = ''
        self.tele = {}
        self._tele_last = {}
        self._session = {}
        self._order = []
        self._next_task = 1
        self._tasks = []
        self._events = []
        self._buf = bytearray()
        self._oversize = False
        self._boot = 0x5A5A0000
        self._last_ms = self._now_ms()
        self.commands = 0
        self._apply_readback_mask()
        #: 命令名 → (方法名, 参数个数下限, 上限)；别名见方法注释
        self._table = {
            'hello': (self._cmd_hello, 2, 2),
            'ping': (self._cmd_ping, 0, 0),
            'status': (self._cmd_status, 0, 0),
            'motor_enable': (self._cmd_motor_enable, 2, 2),
            'motor_speed': (self._cmd_motor_speed, 4, 4),
            'motor_pos': (self._cmd_motor_pos, 6, 6),
            'motor_stop': (self._cmd_motor_stop, 1, 1),
            'stop_all': (self._cmd_stop_all, 0, 0),
            'motor_stall_clear': (self._cmd_motor_stall_clear, 1, 1),
            'motor_zero': (self._cmd_motor_zero, 1, 1),
            'motor_query': (self._cmd_motor_query, 2, 2),
            'gripper': (self._cmd_gripper, 1, 1),
            'servo_angle': (self._cmd_servo_angle, 1, 1),
            'param_get': (self._cmd_param_get, 1, 1),
            'param_set': (self._cmd_param_set, 2, 2),
            'tele_on': (self._cmd_tele_on, 2, 2),
            'tele_off': (self._cmd_tele_off, 0, 0),
            'stop_clear': (self._cmd_stop_clear, 0, 0),
            # 兼容旧文档/别的分支里出现过的命令名（真实上位机不会发）
            'motor_move': (self._cmd_motor_move, 5, 5),
            'motor_abs': (self._cmd_motor_abs, 5, 5),
            'gripper_angle': (self._cmd_gripper_angle, 1, 1),
            'gripper_home': (self._cmd_gripper_home, 0, 0),
            'estop': (self._cmd_estop, 0, 0),
            'estop_clear': (self._cmd_estop_clear, 0, 0),
            'task_status': (self._cmd_task_status, 0, 0),
            'task_query': (self._cmd_task_status, 0, 0),
            'task_cancel': (self._cmd_task_cancel, 0, 0),
        }

    # -- 时间/参数 --------------------------------------------------------
    @staticmethod
    def _now_ms():
        return int(time.monotonic() * 1000)

    def _apply_readback_mask(self):
        self.readback = [addr for i, addr in enumerate(VALID_AXES)
                         if (int(self.params.get('readback_mask', 15)) >> i) & 1]

    def _readback_capable(self, addr):
        return addr in self.readback

    # -- 对外 -------------------------------------------------------------
    def feed(self, data):
        """收字节 → 处理完整行 → 返回回包（应答 + 事件 + 遥测）。"""
        if not data:
            return b''
        with self._lock:
            if b'!' in data:                     # 固件层的“急停字符”
                self._latch_stop('pc_stop_char')
                data = data.replace(b'!', b'')
            self._advance()
            out = []
            for byte in data:
                if byte == 0x0A:                 # LF
                    line = self._buf.decode('ascii', 'replace').rstrip('\r')
                    self._buf.clear()
                    if self._oversize:
                        self._oversize = False
                        self.log('fw', '丢弃超长行（>%d 字节）', self._MAX_FRAME)
                        continue
                    if line:
                        out.extend(self._handle_line(line))
                    continue
                if self._oversize:
                    continue
                if not self._buf and byte not in (0x24, 0x23):   # 只认 '$' / '#'
                    continue
                self._buf.append(byte)
                if len(self._buf) > self._MAX_FRAME - 1:
                    self._oversize = True
                    self._buf.clear()
            out.extend(self._drain())
            return b''.join(out)

    def poll(self):
        """让“随时间发生”的事件/遥测流出来（桥的串口→笔记本方向周期性调用）。"""
        with self._lock:
            self._advance()
            return b''.join(self._drain())

    # -- 分帧/校验 --------------------------------------------------------
    def _handle_line(self, line):
        if len(line) < 4 or line[0] != '$':
            return []
        if '*' not in line:
            return []
        body, _, cs = line[1:].rpartition('*')
        if checksum_text(body) != cs.strip().upper():
            return []
        parts = body.split(',')
        if len(parts) < 2 or not parts[0].isdigit():
            return []
        rid = int(parts[0])
        if not 1 <= rid <= 65535:
            return []
        cmd = parts[1]
        args = parts[2:]
        self.commands += 1
        if cmd != 'hello':
            verdict, cached = self._session_check(rid, body)
            if verdict == 'dup':
                return [cached] if cached else []
            if verdict == 'conflict':
                return [self._err(rid, 'E12')]
        entry = self._table.get(cmd)
        if entry is None:
            self.log('fw', '未知命令 %s（id=%d）', cmd, rid)
            return [self._err(rid, 'E01')]
        handler, lo, hi = entry
        if not lo <= len(args) <= hi:
            return [self._err(rid, 'E02', 'need=%d..%d' % (lo, hi), 'got=%d' % len(args))]
        try:
            responses = handler(rid, args)
        except FakeError as exc:
            return [self._err(rid, exc.code, *exc.pairs)]
        if cmd != 'hello' and responses:
            self._session_record(rid, body, responses[0])
        return responses

    def _session_check(self, rid, body):
        entry = self._session.get(rid)
        if entry is None:
            return 'new', None
        if entry[0] == body:
            return 'dup', entry[1]
        return 'conflict', None

    def _session_record(self, rid, body, response):
        if rid in self._session:
            try:
                self._order.remove(rid)
            except ValueError:
                pass
        self._session[rid] = (body, response)
        self._order.append(rid)
        while len(self._order) > 32:
            self._session.pop(self._order.pop(0), None)

    # -- 回包 -------------------------------------------------------------
    @staticmethod
    def _ok(rid, *fields):
        body = ','.join(['%d' % rid, 'ok'] + [str(f) for f in fields])
        return make_frame('#', body)

    @staticmethod
    def _err(rid, code, *pairs):
        body = ','.join(['%d' % rid, 'err', code] + [str(p) for p in pairs])
        return make_frame('#', body)

    @staticmethod
    def _emit(*fields):
        body = ','.join(['0', 'event'] + [str(f) for f in fields])
        return make_frame('#', body)

    def _drain(self):
        out = self._events
        self._events = []
        return out

    # -- 参数校验小工具 ---------------------------------------------------
    @staticmethod
    def _int(token, lo=None, hi=None):
        text = token.strip()
        if not text.lstrip('-').isdigit():
            raise FakeError('E02', 'bad_int=%s' % token)
        value = int(text)
        if lo is not None and value < lo:
            raise FakeError('E02', 'below_min=%s' % token)
        if hi is not None and value > hi:
            raise FakeError('E02', 'above_max=%s' % token)
        return value

    def _axis(self, token):
        value = self._int(token)
        if value not in VALID_AXES:
            raise FakeError('E03', 'addr=%s' % token)
        return value

    def _latched(self):
        return self.latch

    # -- 命令实现 ---------------------------------------------------------
    def _cmd_hello(self, rid, args):
        version = self._int(args[0], 1, 65535)
        session = self._int(args[1], 0, 0xFFFFFFFF)
        if version != PROTOCOL_VERSION:
            raise FakeError('E02', 'want=%d' % PROTOCOL_VERSION, 'got=%d' % version)
        self._session.clear()
        self._order.clear()
        self._boot = (self._boot + 1) & 0xFFFFFFFF
        readback = '/'.join(str(a) for a in self.readback) or '-'
        self.log('fw', 'hello：会话 %d，启动标识 %08X', session, self._boot)
        return [self._ok(rid, 'fw=%s' % FAKE_FW, 'proto=%d' % PROTOCOL_VERSION,
                         'boot=%08X' % self._boot, 'axes=1/3/4/5',
                         'readback=%s' % readback)]

    def _cmd_ping(self, rid, args):
        return [self._ok(rid, 'ms=%d' % self._now_ms())]

    def _cmd_status(self, rid, args):
        running = '/'.join(str(a) for a in VALID_AXES
                           if self.axes[a]['mode'] != 'stop' and self.axes[a]['rpm']) or '-'
        queued = sum(1 for t in self._tasks if t['state'] == 'sent')
        return [self._ok(rid, 'latch=%d' % (1 if self.latch else 0), 'owner=pc',
                         'running=%s' % running, 'queued=%d' % queued,
                         'gripper=%d' % self.gripper['angle'])]

    def _cmd_motor_enable(self, rid, args):
        addr = self._axis(args[0])
        on = self._int(args[1], 0, 1)
        if self._latched():
            raise FakeError('E10')
        axis = self.axes[addr]
        axis['enabled'] = bool(on)
        if not on:
            axis['rpm'] = 0
            axis['mode'] = 'stop'
        return [self._ok(rid, 'addr=%d' % addr, 'enabled=%d' % (1 if on else 0))]

    def _cmd_motor_speed(self, rid, args):
        addr = self._axis(args[0])
        direction = self._int(args[1], 0, 1)
        rpm = self._int(args[2], 1, 6000)
        acc = self._int(args[3], 0, 255)
        if self._latched():
            raise FakeError('E10')
        axis = self.axes[addr]
        axis.update(enabled=True, dir=direction, rpm=rpm, acc=acc, mode='speed')
        task = self._new_task(addr, 'speed')
        return [self._ok(rid, 'addr=%d' % addr, 'rpm=%d' % rpm, 'task=%d' % task)]

    def _cmd_motor_pos(self, rid, args):
        addr = self._axis(args[0])
        direction = self._int(args[1], 0, 1)
        rpm = self._int(args[2], 1, 6000)
        acc = self._int(args[3], 0, 255)
        pulses = self._int(args[4], 0, 0x7FFFFFFF)
        mode = args[5]
        if mode not in ('rel', 'abs'):
            raise FakeError('E02', 'mode=%s' % mode)
        if self._latched():
            raise FakeError('E10')
        if mode == 'abs' and not self._readback_capable(addr):
            raise FakeError('E13', 'reason=no_position_reference')
        axis = self.axes[addr]
        axis.update(enabled=True, dir=direction, rpm=rpm, acc=acc, mode='pos')
        delta = pulses if direction else -pulses
        axis['target'] = (pulses if mode == 'abs' else axis['pos'] + delta)
        task = self._new_task(addr, 'pos')
        return [self._ok(rid, 'addr=%d' % addr, 'pulses=%d' % pulses,
                         'mode=%s' % mode, 'task=%d' % task)]

    def _cmd_motor_stop(self, rid, args):
        addr = self._axis(args[0])
        axis = self.axes[addr]
        axis['rpm'] = 0
        axis['mode'] = 'stop'
        return [self._ok(rid, 'addr=%d' % addr)]

    def _cmd_stop_all(self, rid, args):
        for axis in self.axes.values():
            axis['rpm'] = 0
            axis['mode'] = 'stop'
        self._latch_stop('stop_all')
        return [self._ok(rid, 'axes=1/3/4/5')]

    def _cmd_motor_stall_clear(self, rid, args):
        addr = self._axis(args[0])
        self.axes[addr]['stall'] = False
        return [self._ok(rid, 'addr=%d' % addr)]

    def _cmd_motor_zero(self, rid, args):
        addr = self._axis(args[0])
        axis = self.axes[addr]
        if axis['mode'] != 'stop' and axis['rpm']:
            raise FakeError('E09', 'reason=moving')
        axis['pos'] = 0
        axis['target'] = 0
        axis['last_rb'] = self._now_ms()
        return [self._ok(rid, 'addr=%d' % addr, 'position=0')]

    def _cmd_motor_query(self, rid, args):
        addr = self._axis(args[0])
        what = args[1]
        if what not in ('vel', 'pos', 'flag'):
            raise FakeError('E02', 'what=%s' % what)
        axis = self.axes[addr]
        if not self._readback_capable(addr):
            raise FakeError('E11', 'addr=%d' % addr)
        age = self._now_ms() - axis['last_rb']
        axis['last_rb'] = self._now_ms()
        if what == 'vel':
            return [self._ok(rid, 'addr=%d' % addr, 'rpm=%d' % axis['rpm'],
                             'what=vel', 'age_ms=%d' % age)]
        if what == 'pos':
            deg = axis['pos'] * 360.0 / COUNTS_PER_REV
            return [self._ok(rid, 'addr=%d' % addr, 'counts=%d' % axis['pos'],
                             'deg=%.1f' % deg, 'what=pos', 'age_ms=%d' % age)]
        return [self._ok(rid, 'addr=%d' % addr, 'flag=0', 'what=flag',
                         'age_ms=%d' % age)]

    def _cmd_gripper(self, rid, args):
        action = args[0]
        grip = self.gripper
        if action == 'open':
            grip['angle'] = grip['open']
        elif action == 'ready':
            grip['angle'] = grip['ready']
        elif action == 'grab':
            grip['angle'] = grip['grab']
        elif action == 'step_grab':
            grip['angle'] = max(grip['grab'], grip['angle'] - grip['step'])
        elif action == 'step_release':
            grip['angle'] = min(grip['open'], grip['angle'] + grip['step'])
        else:
            raise FakeError('E02', 'action=%s' % action)
        grip['busy_until'] = self._now_ms() + int(grip['settle'])
        return [self._ok(rid, 'action=%s' % action, 'angle=%d' % grip['angle'],
                         'settle_ms=%d' % grip['settle'])]

    def _cmd_servo_angle(self, rid, args):
        deg = self._int(args[0], 0, 270)
        grip = self.gripper
        if not grip['grab'] <= deg <= grip['open']:
            raise FakeError('E02', 'reason=out_of_range')
        grip['angle'] = deg
        grip['busy_until'] = self._now_ms() + int(grip['settle'])
        return [self._ok(rid, 'deg=%d' % deg, 'settle_ms=%d' % grip['settle'])]

    def _cmd_param_get(self, rid, args):
        name = args[0]
        if name not in self.params:
            raise FakeError('E02', 'reason=unknown_param')
        return [self._ok(rid, 'name=%s' % name, 'value=%s' % self.params[name])]

    def _cmd_param_set(self, rid, args):
        name = args[0]
        if name not in self.params:
            raise FakeError('E02', 'reason=unknown_param')
        value = self._int(args[1], -1000000, 1000000)
        if name == 'speed_rpm_max' and value < 1:
            raise FakeError('E02', 'reason=range')
        if name == 'readback_mask' and not 1 <= value <= 15:
            raise FakeError('E02', 'reason=range')
        if name == 'gripper_ready_deg' and not 0 <= value <= 270:
            raise FakeError('E02', 'reason=range')
        if name == 'gripper_hold_max_ms' and not 0 <= value <= 60000:
            raise FakeError('E02', 'reason=range')
        before = self.params[name]
        self.params[name] = value
        if name == 'gripper_open_deg':
            self.gripper['open'] = value
        elif name == 'gripper_ready_deg':
            self.gripper['ready'] = value
        elif name == 'gripper_grab_deg':
            self.gripper['grab'] = value
        elif name == 'gripper_step_deg':
            self.gripper['step'] = value
        elif name == 'gripper_settle_ms':
            self.gripper['settle'] = value
        elif name == 'gripper_hold_max_ms':
            self.gripper['hold_max_ms'] = value
        elif name == 'readback_mask':
            self._apply_readback_mask()
        return [self._ok(rid, 'name=%s' % name, 'before=%s' % before, 'value=%d' % value)]

    def _cmd_tele_on(self, rid, args):
        addr = self._axis(args[0])
        interval = self._int(args[1], 20, 60000)
        self.tele[addr] = interval
        self._tele_last[addr] = 0
        return [self._ok(rid, 'addr=%d' % addr, 'interval_ms=%d' % interval)]

    def _cmd_tele_off(self, rid, args):
        self.tele.clear()
        self._tele_last.clear()
        return [self._ok(rid, 'subs=0')]

    def _cmd_stop_clear(self, rid, args):
        self.latch = False
        self.latch_reason = ''
        self._emit('latch', 'state=clear')
        return [self._ok(rid, 'latch=0')]

    # -- 旧命令名别名 -----------------------------------------------------
    def _cmd_motor_move(self, rid, args):
        return self._cmd_motor_pos(rid, list(args) + ['rel'])

    def _cmd_motor_abs(self, rid, args):
        return self._cmd_motor_pos(rid, list(args) + ['abs'])

    def _cmd_gripper_angle(self, rid, args):
        return self._cmd_servo_angle(rid, args)

    def _cmd_gripper_home(self, rid, args):
        return self._cmd_gripper(rid, ['open'])

    def _cmd_estop(self, rid, args):
        return self._cmd_stop_all(rid, args)

    def _cmd_estop_clear(self, rid, args):
        return self._cmd_stop_clear(rid, args)

    def _cmd_task_status(self, rid, args):
        queued = sum(1 for t in self._tasks if t['state'] == 'sent')
        return [self._ok(rid, 'tasks=%d' % len(self._tasks), 'queued=%d' % queued)]

    def _cmd_task_cancel(self, rid, args):
        for task in self._tasks:
            if task['state'] == 'sent':
                task['state'] = 'cancelled'
        return [self._ok(rid, 'cancelled=1')]

    # -- 内部状态推进 -----------------------------------------------------
    def _new_task(self, addr, kind):
        task_id = self._next_task
        self._next_task += 1
        self._tasks.append({'id': task_id, 'addr': addr, 'kind': kind, 'state': 'sent'})
        self._events.append(self._emit('task', 'id=%d' % task_id, 'state=sent'))
        return task_id

    def _latch_stop(self, reason):
        for axis in self.axes.values():
            axis['rpm'] = 0
            axis['mode'] = 'stop'
        if not self.latch:
            self.latch = True
            self.latch_reason = reason
            self._events.append(self._emit('latch', 'state=set',
                                           'reason=%s' % reason.replace(' ', '_')))

    def _advance(self):
        now = self._now_ms()
        dt = max(0, now - self._last_ms)
        self._last_ms = now
        if dt:
            for addr, axis in self.axes.items():
                if not axis['enabled'] or not axis['rpm']:
                    continue
                if axis['mode'] == 'speed':
                    axis['pos'] += self._delta(axis, dt)
                elif axis['mode'] == 'pos':
                    remaining = axis['target'] - axis['pos']
                    step = self._delta(axis, dt)
                    if abs(remaining) <= abs(step):
                        axis['pos'] = axis['target']
                        axis['rpm'] = 0
                        axis['mode'] = 'stop'
                        for task in self._tasks:
                            if task['addr'] == addr and task['state'] == 'sent':
                                task['state'] = 'reached'
                                self._events.append(
                                    self._emit('task', 'id=%d' % task['id'],
                                               'state=reached'))
                    else:
                        axis['pos'] += step
        if self.gripper['busy_until'] and now >= self.gripper['busy_until']:
            self.gripper['busy_until'] = 0
            self._events.append(self._emit('task', 'id=0', 'state=settle_elapsed',
                                           'what=gripper'))
        for addr, interval in list(self.tele.items()):
            if now - self._tele_last.get(addr, 0) < max(1, int(interval)):
                continue
            self._tele_last[addr] = now
            axis = self.axes[addr]
            if self._readback_capable(addr):
                deg = axis['pos'] * 360.0 / COUNTS_PER_REV
                body = '0,tele,addr=%d,rpm=%d,counts=%d,deg=%.1f' % (
                    addr, axis['rpm'], axis['pos'], deg)
            else:
                body = '0,tele,addr=%d,rpm=%d,readback=0' % (addr, axis['rpm'])
            self._events.append(make_frame('#', body))

    @staticmethod
    def _delta(axis, dt_ms):
        counts = int(axis['rpm'] * COUNTS_PER_REV / 60.0 * dt_ms / 1000.0)
        return counts if axis['dir'] else -counts


# =========================================================================
# 串口传输层（真串口 / 假固件，同一套接口）
# =========================================================================
class FakeSerialTransport:
    """--fake-serial：进程内假固件，不需要任何串口硬件。"""

    is_fake = True

    def __init__(self, log):
        self.log = log
        self.device = FakeSTM32(log)
        self._rx = bytearray()
        self._lock = threading.Lock()
        self._wr_lock = threading.Lock()
        self.connected = False
        self.last_error = ''

    def start(self):
        self.connected = True
        self.log('ser', '串口使用假固件 --fake-serial（不加任何真实串口）')
        self.write(wire.STOP_FRAMES)      # 与真串口“打开即停车”保持一致

    def stop(self):
        self.connected = False

    def write(self, data):
        if not data:
            return True
        with self._wr_lock:
            try:
                response = self.device.feed(bytes(data))
            except Exception as exc:       # 假固件自己出问题也不能让桥崩
                self.log('ser', '假固件异常：%r', exc)
                return False
            if response:
                with self._lock:
                    self._rx.extend(response)
            return True

    def read(self):
        with self._lock:
            data = bytes(self._rx)
            self._rx.clear()
        tail = self.device.poll()
        if tail:
            data += tail
        return data


class RealSerialTransport:
    """真串口：后台线程维护连接，掉线按 0.5→3s 退避重连，连上先发停车帧。"""

    is_fake = False

    def __init__(self, path, baud, log):
        self.path = path
        self.baud = int(baud)
        self.log = log
        self._rx = bytearray()
        self._rx_lock = threading.Lock()
        self._wr_lock = threading.Lock()
        self._stop = threading.Event()
        self._ser = None
        self._thread = None
        self.connected = False
        self.last_error = ''
        self.reconnects = 0

    def start(self):
        if not HAVE_PYSERIAL:
            self.last_error = '未安装 pyserial'
            self.log('ser', '真串口需要 pyserial：pip install pyserial '
                            '（或 sudo apt install python3-serial）；现在串口服务空转')
            return
        self._thread = threading.Thread(target=self._run, name='serial', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        ser = self._ser
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        self.connected = False

    def _run(self):
        backoff = 0.5
        while not self._stop.is_set():
            ser = None
            try:
                ser = serial.Serial(self.path, self.baud, timeout=0.05,
                                    write_timeout=1.0)
            except Exception as exc:
                self.last_error = '打开 %s 失败: %s' % (self.path, exc)
                self.log('ser', '%s（%.1fs 后重试）' % (self.last_error, backoff))
                self._stop.wait(backoff)
                backoff = min(3.0, backoff * 2)
                continue
            with self._wr_lock:
                self._ser = ser
            self.connected = True
            self.reconnects += 1
            backoff = 0.5
            self.log('ser', '串口已打开 %s @%d 8N1' % (self.path, self.baud))
            self.write(wire.STOP_FRAMES)      # 上电兜底：四轴停车
            try:
                while not self._stop.is_set():
                    data = ser.read(4096)
                    if data:
                        with self._rx_lock:
                            self._rx.extend(data)
                    else:
                        time.sleep(0.002)
            except Exception as exc:
                self.last_error = '串口读失败: %s' % exc
                self.log('ser', self.last_error)
            finally:
                self.connected = False
                with self._wr_lock:
                    self._ser = None
                try:
                    ser.close()
                except Exception:
                    pass
            if not self._stop.is_set():
                self.log('ser', '串口断开，%.1fs 后重连' % backoff)
                self._stop.wait(backoff)
                backoff = min(3.0, backoff * 2)
        self.connected = False

    def write(self, data):
        if not data:
            return True
        with self._wr_lock:
            ser = self._ser
            if ser is None:
                return False
            try:
                ser.write(data)
                return True
            except Exception as exc:
                self.last_error = '写串口失败: %s' % exc
                self.log('ser', self.last_error)
                return False

    def read(self):
        with self._rx_lock:
            if not self._rx:
                return b''
            data = bytes(self._rx)
            self._rx.clear()
            return data


# =========================================================================
# 串口桥 TCP 服务
# =========================================================================
class SerialBridge:
    """串口桥：TCP 字节流 ↔ 串口字节流，双向透明。

    多客户端策略：允许并发。笔记本→串口方向加锁串行；串口→笔记本方向广播。
    """

    def __init__(self, bind_host, port, transport, log, stop_event):
        self.bind_host = bind_host
        self.port = int(port)
        self.transport = transport
        self.log = log
        self.stop_event = stop_event
        self.sock = None
        self._accept_thread = None
        self._pump_thread = None
        self._wd_thread = None
        self._clients = []
        self._clients_lock = threading.Lock()
        self.last_laptop_rx = time.monotonic()
        self.guarded = False
        self._guard_lock = threading.Lock()
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.stop_frames_sent = 0
        self.lockdown_count = 0

    # -- 生命周期 ---------------------------------------------------------
    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind_host, self.port))
        s.listen(8)
        s.settimeout(0.5)
        self.sock = s
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               name='ser-accept', daemon=True)
        self._accept_thread.start()
        self._pump_thread = threading.Thread(target=self._pump_loop,
                                             name='ser-pump', daemon=True)
        self._pump_thread.start()
        self._wd_thread = threading.Thread(target=self._watchdog_loop,
                                           name='ser-watchdog', daemon=True)
        self._wd_thread.start()
        self.log('ser', '串口桥已监听 %s:%d（多客户端允许并发，串口回包广播）'
                 % (self.bind_host, self.port))

    def stop(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for conn, _addr in clients:
            try:
                conn.close()
            except OSError:
                pass
        for thread in (self._accept_thread, self._pump_thread, self._wd_thread):
            if thread is not None:
                thread.join(timeout=2.0)
        self._accept_thread = self._pump_thread = self._wd_thread = None

    # -- 接入 -------------------------------------------------------------
    def _accept_loop(self):
        while not self.stop_event.is_set():
            sock = self.sock
            if sock is None:
                return
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(0.5)
            with self._clients_lock:
                self._clients.append((conn, addr))
            self.log('ser', '笔记本已连接串口桥 %s:%d（当前 %d 个客户端）'
                     % (addr[0], addr[1], self._client_count()))
            threading.Thread(target=self._client_loop, args=(conn, addr),
                             name='ser-client', daemon=True).start()

    def _client_count(self):
        with self._clients_lock:
            return len(self._clients)

    def _client_loop(self, conn, addr):
        peer = '%s:%d' % (addr[0], addr[1])
        try:
            while not self.stop_event.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                self.last_laptop_rx = time.monotonic()
                self._reset_guard()
                if not self.transport.write(data):
                    self.log('ser', '串口不可用，丢弃笔记本 %d 字节' % len(data))
                    continue
                self.tx_bytes += len(data)
                self.log('ser', '笔记本→串口 %dB %s' % (len(data), fmt_bytes(data)))
        except Exception as exc:
            self.log('ser', '%s 桥接异常：%r' % (peer, exc))
        finally:
            self._drop_client(conn)
            self.log('ser', '%s 已断开（剩余 %d 个客户端）'
                     % (peer, self._client_count()))

    def _drop_client(self, conn):
        with self._clients_lock:
            self._clients = [(c, a) for (c, a) in self._clients if c is not conn]
        try:
            conn.close()
        except OSError:
            pass

    # -- 串口 → 笔记本 ----------------------------------------------------
    def _pump_loop(self):
        while not self.stop_event.is_set():
            try:
                data = self.transport.read()
            except Exception as exc:
                self.log('ser', '串口读取异常：%r' % exc)
                data = b''
            if data:
                self.rx_bytes += len(data)
                self._broadcast(data)
            else:
                time.sleep(0.004)

    def _broadcast(self, data):
        with self._clients_lock:
            clients = list(self._clients)
        if not clients:
            return
        dead = []
        for conn, _addr in clients:
            try:
                conn.sendall(data)
            except OSError:
                dead.append(conn)
        for conn in dead:
            self._drop_client(conn)
        self.log('ser', '串口→笔记本 %dB %s' % (len(data), fmt_bytes(data)))

    # -- 看门狗 -----------------------------------------------------------
    def _reset_guard(self):
        with self._guard_lock:
            if self.guarded:
                self.guarded = False
                self.log('ser', '看门狗：收到笔记本新字节，解除护栏')

    def _watchdog_loop(self):
        window = wire.SERIAL_WATCHDOG_MS / 1000.0
        while not self.stop_event.is_set():
            time.sleep(0.05)
            if not self.transport.connected:
                self.last_laptop_rx = time.monotonic()
                continue
            with self._guard_lock:
                if self.guarded:
                    continue
                silent = time.monotonic() - self.last_laptop_rx
                if silent < window:
                    continue
                self.guarded = True
            ok = self.transport.write(wire.STOP_FRAMES)
            self.stop_frames_sent += 1
            self.lockdown_count += 1
            self.log('ser', '看门狗：%.1fs 没收到笔记本字节，已向串口发停车帧%s'
                     % (window, '' if ok else '（串口不可用，仅标记）'))


# =========================================================================
# UDP 发现服务
# =========================================================================
class DiscoveryServer:
    """监听 UDP 广播：收到 DISCOVERY_PROBE 就回自己的 IP 与端口。"""

    def __init__(self, port, img_port, ser_port, name, log, stop_event):
        self.port = int(port)
        self.img_port = int(img_port)
        self.ser_port = int(ser_port)
        self.name = name
        self.log = log
        self.stop_event = stop_event
        self.sock = None
        self._thread = None
        self.replies = 0

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        try:
            s.bind(('', self.port))
        except OSError as exc:
            self.log('disc', 'UDP %d 绑定失败（%s），发现服务关闭' % (self.port, exc))
            try:
                s.close()
            except OSError:
                pass
            return
        s.settimeout(0.5)
        self.sock = s
        self._thread = threading.Thread(target=self._loop, name='discovery', daemon=True)
        self._thread.start()
        self.log('disc', 'UDP 发现已监听 :%d（图传 %d / 串口桥 %d / 名字 %s）'
                 % (self.port, self.img_port, self.ser_port, self.name))

    def stop(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def _loop(self):
        while not self.stop_event.is_set():
            sock = self.sock
            if sock is None:
                return
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if wire.DISCOVERY_PROBE not in data:
                continue
            ip = local_ip(addr[0])
            reply = wire.pack_discovery_reply(ip, self.img_port, self.ser_port, self.name)
            try:
                sock.sendto(reply, addr)
                self.replies += 1
                self.log('disc', '应答探测 %s:%d → %s:%d/%d'
                         % (addr[0], addr[1], ip, self.img_port, self.ser_port))
            except OSError as exc:
                self.log('disc', '应答 %s:%d 失败：%s' % (addr[0], addr[1], exc))


# =========================================================================
# 配置与命令行
# =========================================================================
def load_config(path):
    p = Path(path)
    if not p.is_absolute():
        p = BASE_DIR / p
    try:
        with open(p, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print('[config] 读取 %s 失败：%s（改用默认值）' % (p, exc), flush=True)
        return {}


def normalize_cameras(items, fallback):
    """把配置里的 cameras 规范成 [{'index':N, ...}]；支持 int 或 dict。"""
    out = []
    for item in items or []:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            out.append({'index': int(item)})
        elif isinstance(item, dict):
            try:
                out.append({'index': int(item.get('index', 0)),
                            'width': item.get('width'),
                            'height': item.get('height'),
                            'fps': item.get('fps')})
            except (TypeError, ValueError):
                continue
        elif isinstance(item, str) and item.strip().lstrip('-').isdigit():
            out.append({'index': int(item)})
    return out or [{'index': int(fallback)}]


def build_arg_parser():
    ap = argparse.ArgumentParser(
        description='树莓派端服务：图传 TCP + 串口桥 TCP + UDP 发现（附假相机/假固件）',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument('--host', default=None, help='监听地址（0.0.0.0 = 所有网卡）')
    ap.add_argument('--img-port', type=int, default=None, help='图传 TCP 端口')
    ap.add_argument('--ser-port', type=int, default=None, help='串口桥 TCP 端口')
    ap.add_argument('--discovery-port', type=int, default=None, help='UDP 发现端口')
    ap.add_argument('--serial', default=None,
                    help='STM32 串口路径（Linux /dev/ttyACM0；Windows 必须 COMx）')
    ap.add_argument('--baud', type=int, default=None, help='串口波特率（8N1）')
    ap.add_argument('--cam-index', type=int, action='append', default=None,
                    metavar='N', help='相机设备号，可给多次；cam id 就是它出现的序号')
    ap.add_argument('--width', type=int, default=None, help='相机采集宽')
    ap.add_argument('--height', type=int, default=None, help='相机采集高')
    ap.add_argument('--fps', type=int, default=None, help='相机采集帧率')
    ap.add_argument('--quality', type=int, default=None, help='JPEG 质量 1..100')
    ap.add_argument('--name', default=None, help='发现应答里的机器名')
    ap.add_argument('--fake-camera', action='store_true', help='用合成画面代替真相机')
    ap.add_argument('--fake-serial', action='store_true', help='用进程内假 STM32 代替串口')
    ap.add_argument('--no-camera', action='store_true', help='不启动图传服务')
    ap.add_argument('--no-serial', action='store_true', help='不启动串口桥')
    ap.add_argument('--no-discovery', action='store_true', help='不启动 UDP 发现')
    ap.add_argument('--log-file', default=None, help='同时把日志写入该文件（追加）')
    ap.add_argument('--exit-after', type=float, default=None, metavar='SECONDS',
                    help='联调/CI 用：启动 N 秒后自动优雅退出（0=一直跑）')
    ap.add_argument('--config', default='config.json', help='配置文件路径')
    return ap


class Settings:
    """合并后的运行配置（CLI > config.json > 内置默认）。"""

    def __init__(self, args, cfg):
        pick = lambda cli, key, fallback: (
            cli if cli is not None else cfg.get(key, fallback))
        self.host = pick(args.host, 'host', DEFAULTS['host'])
        self.img_port = int(pick(args.img_port, 'img_port', DEFAULTS['img_port']))
        self.ser_port = int(pick(args.ser_port, 'ser_port', DEFAULTS['ser_port']))
        self.discovery_port = int(pick(args.discovery_port, 'discovery_port',
                                       DEFAULTS['discovery_port']))
        self.baud = int(pick(args.baud, 'baud', DEFAULTS['baud']))
        self.width = int(pick(args.width, 'width', DEFAULTS['width']))
        self.height = int(pick(args.height, 'height', DEFAULTS['height']))
        self.fps = int(pick(args.fps, 'fps', DEFAULTS['fps']))
        self.quality = int(pick(args.quality, 'quality', DEFAULTS['quality']))
        self.name = str(pick(args.name, 'name', DEFAULTS['name']))
        cam_list = args.cam_index if args.cam_index else cfg.get('cameras',
                                                                 DEFAULTS['cameras'])
        self.cameras = normalize_cameras(cam_list, DEFAULTS['cameras'][0])
        self.serial = args.serial or cfg.get('serial')
        self.fake_camera = bool(args.fake_camera)
        self.fake_serial = bool(args.fake_serial)
        self.run_camera = not args.no_camera
        self.run_serial = not args.no_serial
        self.run_discovery = not args.no_discovery
        self.exit_after = float(getattr(args, 'exit_after', 0.0) or 0.0)

    def resolve_serial(self):
        """返回 (path, error)。Windows 上串口必须显式给 COMx。"""
        if self.fake_serial:
            return None, ''
        path = self.serial
        if os.name == 'nt':
            if not path:
                return None, ('Windows 下真串口必须用 --serial COMx 指定'
                              '（或加 --fake-serial 联调）')
            if not path.upper().startswith('COM'):
                return None, ('Windows 串口名应形如 COM3，当前 %r 来自 config.json 默认'
                              '（Linux 用）；用 --serial COMx 覆盖，或加 --fake-serial 联调'
                              % path)
        elif not path:
            return None, '未配置串口路径（默认 /dev/ttyACM0，可用 --serial 指定）'
        return path, ''

    def describe(self):
        return ('host=%s img=%d ser=%d disc=%d cameras=%s %dx%d@%d q=%d '
                'fake_camera=%s fake_serial=%s' % (
                    self.host, self.img_port, self.ser_port, self.discovery_port,
                    [c['index'] for c in self.cameras], self.width, self.height,
                    self.fps, self.quality, self.fake_camera, self.fake_serial))


# =========================================================================
# 主程序
# =========================================================================
class App:
    def __init__(self, args, log):
        self.args = args
        self.log = log
        cfg = load_config(args.config)
        self.cfg = cfg
        self.settings = Settings(args, cfg)
        self.stop_event = threading.Event()
        self.cameras = {}
        self.image_server = None
        self.transport = None
        self.serial_bridge = None
        self.discovery = None

    # -- 启动 -------------------------------------------------------------
    def build(self):
        st = self.settings
        self.log('main', '配置：%s' % st.describe())

        if st.run_camera:
            for cam_id, item in enumerate(st.cameras):
                width = item.get('width') or st.width
                height = item.get('height') or st.height
                fps = item.get('fps') or st.fps
                worker = CameraWorker(cam_id, item['index'], int(width), int(height),
                                      int(fps), st.fake_camera, self.log)
                self.cameras[cam_id] = worker
                worker.start()

        if st.run_serial:
            if st.fake_serial:
                self.transport = FakeSerialTransport(self.log)
            else:
                path, error = st.resolve_serial()
                if error:
                    raise StartupError('串口配置错误：%s' % error)
                if not HAVE_PYSERIAL:
                    raise StartupError(
                        '未安装 pyserial，真串口模式不可用：'
                        'sudo apt install python3-serial'
                        '（只想联调就加 --fake-serial）')
                self.transport = RealSerialTransport(path, st.baud, self.log)
            self.transport.start()

        return self

    def start_services(self):
        st = self.settings
        if st.run_camera:
            if not (HAVE_CV2 and HAVE_NUMPY):
                self.log('img', '未安装 cv2/numpy，图传服务不启动'
                                '（pip install opencv-python-headless numpy）')
            else:
                self.image_server = ImageServer(st.host, st.img_port, self.cameras,
                                                st.quality, self.log, self.stop_event)
                self.image_server.start()
        if st.run_serial and self.transport is not None:
            self.serial_bridge = SerialBridge(st.host, st.ser_port, self.transport,
                                             self.log, self.stop_event)
            self.serial_bridge.start()
        if st.run_discovery:
            self.discovery = DiscoveryServer(st.discovery_port, st.img_port,
                                             st.ser_port, st.name, self.log,
                                             self.stop_event)
            self.discovery.start()
        return self

    def ready_line(self):
        st = self.settings
        ip = local_ip()
        return 'pi ready: img=%s:%d ser=%s:%d name=%s' % (
            ip, st.img_port, ip, st.ser_port, st.name)

    def install_signals(self):
        def handler(signum, frame):
            self.log('main', '收到信号 %s，正在退出…' % signum)
            self.stop_event.set()
        for name in ('SIGINT', 'SIGTERM'):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def run(self):
        try:
            self.build()
        except StartupError as exc:
            self.log('main', '启动失败：%s' % exc)
            self.shutdown()
            return 2
        self.start_services()
        self.log('ready', self.ready_line())
        deadline = None
        if self.settings.exit_after > 0:
            deadline = time.time() + self.settings.exit_after
            self.log('main', '--exit-after=%.1fs：到点自动优雅退出'
                     % self.settings.exit_after)
        try:
            while not self.stop_event.wait(0.5):
                if deadline is not None and time.time() >= deadline:
                    self.log('main', '--exit-after 时间到，正在退出…')
                    self.stop_event.set()
                    break
        except KeyboardInterrupt:
            self.log('main', 'Ctrl+C，正在退出…')
            self.stop_event.set()
        finally:
            self.shutdown()
        return 0

    def shutdown(self):
        self.log('main', '正在关闭服务…')
        if self.image_server is not None:
            self.image_server.stop()
        if self.serial_bridge is not None:
            self.serial_bridge.stop()
        if self.discovery is not None:
            self.discovery.stop()
        if self.transport is not None:
            try:
                # 退出前再兜底停一次车（真实硬件上等价于四轴停车）
                self.transport.write(wire.STOP_FRAMES)
            except Exception:
                pass
            self.transport.stop()
        for worker in self.cameras.values():
            worker.stop()
        snap = []
        if self.image_server is not None:
            snap.append('img frames=%d conns=%d errors=%d'
                        % (self.image_server.frames_served,
                           self.image_server.connections,
                           self.image_server.errors_sent))
        if self.serial_bridge is not None:
            snap.append('ser tx=%dB rx=%dB stopframes=%d'
                        % (self.serial_bridge.tx_bytes,
                           self.serial_bridge.rx_bytes,
                           self.serial_bridge.stop_frames_sent))
        if self.discovery is not None:
            snap.append('disc replies=%d' % self.discovery.replies)
        for worker in self.cameras.values():
            snap.append('cam%d frames=%d' % (worker.cam_id, worker._frames))
        if snap:
            self.log('main', '统计：' + '；'.join(snap))
        self.log('main', '已退出')


def _setup_stdout():
    """输出被重定向到管道/文件时强制 UTF-8。

    Windows 管道默认是 GBK，日志被别的工具按 UTF-8 读会变乱码；tty（cmd/PowerShell
    窗口）保持系统编码，中文照常显示。
    """
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def main(argv=None):
    _setup_stdout()
    args = build_arg_parser().parse_args(argv)
    log = Logger(args.log_file)
    if log.path:
        log('main', '日志文件：%s' % log.path)
    log('main', 'pi_agent 启动，Python %s / %s'
        % (sys.version.split()[0], sys.platform))
    if not HAVE_CV2:
        log('main', '警告：未安装 cv2，图传不可用（pip install opencv-python-headless）')
    if not HAVE_PYSERIAL:
        log('main', '提示：未安装 pyserial，仅 --fake-serial 可用')
    app = App(args, log)
    try:
        code = app.run()
    except Exception:
        import traceback
        log('main', '致命错误：\n' + traceback.format_exc())
        code = 1
    finally:
        log.close()
    return code


if __name__ == '__main__':
    sys.exit(main())
