"""笔记本 ↔ 树莓派 线路协议（两端共用的唯一实现，只依赖标准库）。

设计目标
--------
1. 图传：笔记本"拉"一帧，派"拍 + 压 JPEG"回传。永不积压旧帧。
2. 串口桥：笔记本把上位机协议行原样发过来，派原样写进 /dev/ttyACM0；
   派把单片机回包原样回传。桥不解析，字节级透明。
3. 发现：笔记本 UDP 广播探测，派回自己的 IP 与端口（可选，静态 IP 也能用）。

所有多字节整数一律小端（x86/ARM 一致，无需关心字节序）。
"""
from __future__ import annotations

import struct

# ---------------------------------------------------------------- 端口约定
IMG_PORT = 5057          # 图传（TCP，拉模式）
SER_PORT = 5058          # 串口桥（TCP，全双工字节流；同一时刻只允许一个控制方）
OBSERVER_PORT = 5054     # 串口观察口（TCP，只读：能看回包，发来的字节被丢弃）
DISCOVERY_PORT = 5056    # 发现（UDP 广播）

# ---------------------------------------------------------------- 图传帧
MAGIC_REQ = b'RQ'
MAGIC_IMG = b'IM'
MAGIC_ERR = b'ER'

# 请求：magic(2) + cam(2) + w(2) + h(2) + q(1)
REQ = struct.Struct('<2sHHHB')
REQ_LEN = REQ.size                     # 9

# 响应头：magic(2) + cam(2) + w(2) + h(2) + seq(4) + ts_us(8) + len(4)
IMG_HDR = struct.Struct('<2sHHHIQI')
IMG_HDR_LEN = IMG_HDR.size             # 24

# 错误：magic(2) + code(2) + len(2) + msg
ERR_HDR = struct.Struct('<2sHH')
ERR_HDR_LEN = ERR_HDR.size             # 6

# 错误码
E_NO_CAMERA = 1        # 相机没打开 / 掉了
E_CAPTURE_FAIL = 2     # 抓帧失败
E_BAD_REQUEST = 3      # 请求格式不对
E_ENCODE_FAIL = 4      # JPEG 编码失败
E_BUSY = 5             # 服务内部错误

ERROR_TEXT = {
    E_NO_CAMERA: '相机未打开',
    E_CAPTURE_FAIL: '抓帧失败',
    E_BAD_REQUEST: '请求格式错误',
    E_ENCODE_FAIL: 'JPEG 编码失败',
    E_BUSY: '服务忙/内部错误',
}


def pack_request(cam_id: int, width: int = 0, height: int = 0, quality: int = 0) -> bytes:
    """width/height/quality 传 0 表示"用派端配置"。"""
    return REQ.pack(MAGIC_REQ, int(cam_id) & 0xFFFF, int(width) & 0xFFFF,
                    int(height) & 0xFFFF, int(quality) & 0xFF)


def parse_request(buf: bytes) -> dict:
    magic, cam, w, h, q = REQ.unpack(buf[:REQ_LEN])
    if magic != MAGIC_REQ:
        raise ValueError('bad magic %r' % (magic,))
    return {'cam': cam, 'width': w, 'height': h, 'quality': q}


def pack_image(cam_id: int, width: int, height: int, seq: int, ts_us: int,
               jpeg: bytes) -> bytes:
    return IMG_HDR.pack(MAGIC_IMG, int(cam_id) & 0xFFFF, int(width) & 0xFFFF,
                        int(height) & 0xFFFF, int(seq) & 0xFFFFFFFF,
                        int(ts_us) & 0xFFFFFFFFFFFFFFFF, len(jpeg)) + jpeg


def parse_image_header(buf: bytes) -> dict:
    magic, cam, w, h, seq, ts_us, n = IMG_HDR.unpack(buf[:IMG_HDR_LEN])
    if magic != MAGIC_IMG:
        raise ValueError('bad magic %r' % (magic,))
    return {'cam': cam, 'width': w, 'height': h, 'seq': seq, 'ts_us': ts_us, 'n': n}


def pack_error(code: int, msg: str = '') -> bytes:
    body = (msg or ERROR_TEXT.get(code, '')).encode('utf-8', 'replace')
    return ERR_HDR.pack(MAGIC_ERR, int(code) & 0xFFFF, len(body)) + body


def parse_error_header(buf: bytes) -> dict:
    magic, code, n = ERR_HDR.unpack(buf[:ERR_HDR_LEN])
    if magic != MAGIC_ERR:
        raise ValueError('bad magic %r' % (magic,))
    return {'code': code, 'n': n,
            'text': ERROR_TEXT.get(code, '错误 %d' % code)}


def peek_magic(buf: bytes) -> bytes:
    """判断缓冲开头是 IM / ER（不足 2 字节返回 b''）。"""
    return buf[:2] if len(buf) >= 2 else b''


# ---------------------------------------------------------------- 发现
DISCOVERY_PROBE = b'ROBOTPI?'
DISCOVERY_REPLY = b'ROBOTPI!'


def pack_discovery_reply(ip: str, img_port: int = IMG_PORT,
                         ser_port: int = SER_PORT, name: str = 'robot-pi') -> bytes:
    return (DISCOVERY_REPLY +
            ('%s|%d|%d|%s' % (ip, img_port, ser_port, name)).encode('utf-8'))


def parse_discovery_reply(data: bytes) -> dict:
    if not data.startswith(DISCOVERY_REPLY):
        raise ValueError('not a discovery reply')
    body = data[len(DISCOVERY_REPLY):].decode('utf-8', 'replace')
    parts = body.split('|')
    return {'ip': parts[0],
            'img_port': int(parts[1]) if len(parts) > 1 else IMG_PORT,
            'ser_port': int(parts[2]) if len(parts) > 2 else SER_PORT,
            'name': parts[3] if len(parts) > 3 else 'robot-pi'}


# ---------------------------------------------------------------- 安全兜底
# EMM V5 停车帧：[地址] FE 98 [snF] 6B —— 上电/异常时四轴各发一次
STOP_FRAMES = b''.join(bytes([addr, 0xFE, 0x98, 0x00, 0x6B]) for addr in (1, 3, 4, 5))
SERIAL_WATCHDOG_MS = 1500      # 笔记本 1.5 s 不发字节 → 派主动停车


# ---------------------------------------------------------------- 小工具
def recv_exact(sock, n: int) -> bytes:
    """读满 n 字节；对端正常关闭时抛 ConnectionError。"""
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('对端关闭连接')
        buf += chunk
    return buf


def open_tcp(host: str, port: int, timeout: float = 3.0):
    """建一个带超时的 TCP 连接（连上后把超时留给调用方决定）。"""
    import socket
    s = socket.create_connection((host, int(port)), timeout=timeout)
    s.settimeout(None)
    return s
