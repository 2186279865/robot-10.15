#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""10.21/pi/tests/selftest_client.py —— pi_agent 端到端自测（Windows 也能跑）。

先起服务（另开一个窗口）::

    python 10.21/pi/pi_agent.py --fake-camera --fake-serial \
        --img-port 15057 --ser-port 15058 --no-discovery

再跑自测::

    python 10.21/pi/tests/selftest_client.py --img-port 15057 --ser-port 15058

检查项：
  A 图传：请求 640x480 拉一帧 → JPEG 头 0xFFD8、cv2.imdecode 成功、尺寸=640x480
  B 图传：请求 w=h=0 拉一帧 → 尺寸=派端配置（默认 1280x720）
  C 图传：请求不存在的相机 → 收到 ER 错误包（E_NO_CAMERA），且连接不断
  D 串口桥：`$1,hello,1*CS` → `#1,ok,fw=...,boot=...,axes=1/3/4/5,readback=...`
  E 串口桥：`$2,ping*CS` → `#2,ok,ms=...`
  F 串口桥：`$3,motor_query,4,pos*CS` → `#3,ok,addr=4,counts=...,what=pos`
  G 串口桥：未知命令 → `#4,err,E01`
  H（可选，给 --discovery-port 才测）UDP 发现：发 ROBOTPI? 收 ROBOTPI! 应答

退出码：0 全部通过；1 有失败。
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]      # 老布局 10.21/；交付包里是 01-交付包/orangepi/
# 交付包布局：01-交付包/{orangepi/pi/tests, common} → common 在上一层
for _cand in (BASE / 'common', BASE.parent / 'common'):
    if _cand.is_dir():
        sys.path.insert(0, str(_cand))
        break

import wire  # noqa: E402

try:
    import cv2
except Exception:                                # pragma: no cover
    cv2 = None
try:
    import numpy as np
except Exception:                                # pragma: no cover
    np = None

FAILS = []
PASSES = []


def check(name, ok, detail=''):
    tag = 'PASS' if ok else 'FAIL'
    line = '[%s] %s' % (tag, name)
    if detail:
        line += '   (%s)' % detail
    print(line, flush=True)
    (PASSES if ok else FAILS).append(name)


def xor_text(payload):
    value = 0
    for ch in payload:
        value ^= ord(ch)
    return '%02X' % (value & 0xFF)


def encode_request(req_id, cmd, *args):
    """自己算校验和，不依赖 host/protocol.py —— 独立验证派端的帧格式。"""
    body = ','.join(str(x) for x in (req_id, cmd) + args)
    return ('$%s*%s\n' % (body, xor_text(body))).encode('ascii')


# ---------------------------------------------------------------- 图传
def fetch_frame(sock, cam, width, height, quality, timeout=5.0):
    """发一个请求、读一个应答。返回 ('image', hdr, jpeg) 或 ('error', hdr, text)。"""
    sock.settimeout(timeout)
    sock.sendall(wire.pack_request(cam, width, height, quality))
    magic = wire.recv_exact(sock, 2)
    if magic == wire.MAGIC_IMG:
        head = magic + wire.recv_exact(sock, wire.IMG_HDR_LEN - 2)
        hdr = wire.parse_image_header(head)
        jpeg = wire.recv_exact(sock, hdr['n'])
        return 'image', hdr, jpeg
    if magic == wire.MAGIC_ERR:
        head = magic + wire.recv_exact(sock, wire.ERR_HDR_LEN - 2)
        hdr = wire.parse_error_header(head)
        text = ''
        if hdr['n']:
            text = wire.recv_exact(sock, hdr['n']).decode('utf-8', 'replace')
        return 'error', hdr, text
    raise RuntimeError('未知 magic %r' % (magic,))


def test_image(host, port, expect_w, expect_h):
    print('\n--- A/B/C 图传 (img %s:%d) ---' % (host, port), flush=True)
    try:
        sock = wire.open_tcp(host, port, timeout=5.0)
    except OSError as exc:
        check('连接图传端口 %d' % port, False, str(exc))
        return
    try:
        # A：让派端按请求缩放
        kind, hdr, body = fetch_frame(sock, 0, 640, 480, 70)
        if kind != 'image':
            check('A 请求 640x480 拉帧', False, '收到错误 %s %s' % (hdr, body))
        else:
            check('A 拉帧成功（seq=%s, %d 字节）' % (hdr['seq'], hdr['n']), True)
            check('A JPEG 头 0xFFD8', body[:2] == b'\xff\xd8', body[:2].hex().upper())
            check('A 应答宽高 = 640x480',
                  (hdr['width'], hdr['height']) == (640, 480),
                  '%dx%d' % (hdr['width'], hdr['height']))
            if cv2 is not None and np is not None:
                img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
                ok = img is not None
                check('A cv2.imdecode 成功', ok)
                if ok:
                    check('A 解码尺寸 = (480,640)',
                          img.shape[:2] == (480, 640), str(img.shape))
                    # 假画面应该不是纯色：算出像素标准差
                    check('A 画面非纯色（std>8）', float(img.std()) > 8.0,
                          'std=%.1f' % float(img.std()))
            else:
                print('  （本机没有 cv2/numpy，跳过解码检查）')

        # B：w/h=0 → 用派端配置
        kind, hdr, body = fetch_frame(sock, 0, 0, 0, 0)
        if kind != 'image':
            check('B 请求 w=h=0 拉帧', False, '收到错误 %s' % (hdr,))
        else:
            size = (hdr['width'], hdr['height'])
            ok = size == (expect_w, expect_h)
            check('B w/h=0 → 派端配置尺寸 %dx%d' % (expect_w, expect_h), ok,
                  '实际 %dx%d' % size)
            check('B JPEG 头 0xFFD8', body[:2] == b'\xff\xd8', body[:2].hex().upper())

        # C：不存在的相机 → 错误包，连接必须还在
        kind, hdr, text = fetch_frame(sock, 9, 0, 0, 0)
        check('C 不存在的相机回 ER 错误包', kind == 'error', str(hdr))
        if kind == 'error':
            check('C 错误码 = E_NO_CAMERA(%d)' % wire.E_NO_CAMERA,
                  hdr['code'] == wire.E_NO_CAMERA, 'code=%s text=%s' % (hdr['code'], text))
        kind, hdr, body = fetch_frame(sock, 0, 320, 240, 80)
        check('C 出错后连接仍可用（继续拉 320x240）',
              kind == 'image' and (hdr['width'], hdr['height']) == (320, 240),
              '%s %dx%d' % (kind, hdr.get('width', 0), hdr.get('height', 0)))
    except (ConnectionError, OSError) as exc:
        check('图传收发', False, repr(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- 串口桥
def read_reply(conn, req_id, timeout=3.0):
    """在字节流里等 req_id 对应的应答行，返回 (kind, raw_line)；超时返回 (None, '')。"""
    buf = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn.settimeout(max(0.05, deadline - time.time()))
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b'\n' in buf:
            line, _, buf = buf.partition(b'\n')
            text = line.decode('ascii', 'replace').strip()
            if not text.startswith('#'):
                continue
            payload = text[1:].rpartition('*')[0]
            parts = payload.split(',')
            if len(parts) >= 2 and parts[0] == str(req_id):
                return parts[1], text
    return None, ''


def read_raw(conn, timeout=0.5):
    """读一段原始字节（用于确认“坏帧不该有应答”）。"""
    buf = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn.settimeout(max(0.05, deadline - time.time()))
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def test_serial(host, port):
    print('\n--- D/E/F/G 串口桥 (ser %s:%d) ---' % (host, port), flush=True)
    try:
        conn = wire.open_tcp(host, port, timeout=5.0)
    except OSError as exc:
        check('连接串口桥端口 %d' % port, False, str(exc))
        return
    try:
        # D：hello（上位机每次连上发的第一条）
        conn.sendall(encode_request(1, 'hello', 1, 4242))
        kind, line = read_reply(conn, 1)
        print('  回包: %s' % (line or '<无>'))
        check('D hello 回 ok', kind == 'ok', line)
        check('D hello 含 fw=', 'fw=' in line)
        check('D hello 含 axes=1/3/4/5', 'axes=1/3/4/5' in line)
        check('D hello 含 readback=', 'readback=' in line)
        check('D hello 含 boot=', 'boot=' in line)

        # E：ping
        conn.sendall(encode_request(2, 'ping'))
        kind, line = read_reply(conn, 2)
        print('  回包: %s' % (line or '<无>'))
        check('E ping 回 ok 且带 ms=', kind == 'ok' and 'ms=' in line, line)

        # F：motor_query（四轴地址 4 = X 轴，位置查询）
        conn.sendall(encode_request(3, 'motor_query', 4, 'pos'))
        kind, line = read_reply(conn, 3)
        print('  回包: %s' % (line or '<无>'))
        check('F motor_query 回 ok', kind == 'ok', line)
        check('F 含 addr=4', 'addr=4' in line)
        check('F 含 counts=', 'counts=' in line)
        check('F 含 what=pos', 'what=pos' in line)

        # G：未知命令 → E01
        conn.sendall(encode_request(4, 'no_such_cmd'))
        kind, line = read_reply(conn, 4)
        print('  回包: %s' % (line or '<无>'))
        check('G 未知命令回 err E01', kind == 'err' and ',E01' in line, line)

        # 校验和错的帧应被静默丢弃（固件不执行、不应答）
        conn.sendall(b'$5,ping*00\n')
        noise = read_raw(conn, 0.4)
        check('G 坏校验帧不应答', b'#5' not in noise, repr(noise[:60]))
        # 客户端还能继续正常说话
        conn.sendall(encode_request(6, 'ping'))
        kind, line = read_reply(conn, 6)
        check('G 坏帧之后连接仍可用', kind == 'ok', line)
    except (ConnectionError, OSError) as exc:
        check('串口桥收发', False, repr(exc))
    finally:
        try:
            conn.close()
        except OSError:
            pass


# ---------------------------------------------------------------- 发现
def test_discovery(port, timeout=2.0):
    print('\n--- H UDP 发现 (udp :%d) ---' % port, flush=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(wire.DISCOVERY_PROBE, ('127.0.0.1', port))
        deadline = time.time() + timeout
        data = b''
        addr = None
        while time.time() < deadline and not data:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
        if not data:
            check('H 收到发现应答', False, '超时（服务是否 --no-discovery？）')
            return
        info = wire.parse_discovery_reply(data)
        check('H 收到发现应答', True, '%s ← %s' % (info, addr))
        check('H 应答含合法 IP', bool(info['ip']), info['ip'])
        check('H 应答端口非 0', info['img_port'] > 0 and info['ser_port'] > 0,
              'img=%d ser=%d name=%s' % (info['img_port'], info['ser_port'], info['name']))
    finally:
        sock.close()


def main(argv=None):
    # 重定向到管道/文件时强制 UTF-8（Windows 管道默认 GBK，会让中文和 ✓ 乱码/报错）
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser(description='pi_agent 端到端自测客户端')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--img-port', type=int, default=15057)
    ap.add_argument('--ser-port', type=int, default=15058)
    ap.add_argument('--discovery-port', type=int, default=0,
                    help='给了才测 UDP 发现（0 = 跳过）')
    ap.add_argument('--expect-width', type=int, default=1280,
                    help='服务端配置的采集宽（用于校验 w/h=0 的应答）')
    ap.add_argument('--expect-height', type=int, default=720)
    args = ap.parse_args(argv)

    print('pi_agent 自测客户端：host=%s img=%d ser=%d disc=%s'
          % (args.host, args.img_port, args.ser_port,
             args.discovery_port or '跳过'))
    test_image(args.host, args.img_port, args.expect_width, args.expect_height)
    test_serial(args.host, args.ser_port)
    if args.discovery_port:
        test_discovery(args.discovery_port)

    print('\n================ 汇总 ================')
    print('通过 %d 项，失败 %d 项' % (len(PASSES), len(FAILS)))
    if FAILS:
        for name in FAILS:
            print('  [FAIL] %s' % name)
        return 1
    print('[OK] selftest 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
