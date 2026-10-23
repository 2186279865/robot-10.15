"""找派 + 连通性体检（笔记本端，纯标准库）。

用法：
    python 10.21/net/find_pi.py                # UDP 广播找派
    python 10.21/net/find_pi.py 192.168.137.50 # 直接体检指定 IP
    python 10.21/net/find_pi.py 192.168.137.50 --full   # 再加取帧测速
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'common'))
import wire  # noqa: E402


def discover(timeout: float = 1.5) -> list:
    """UDP 广播找派，返回 [{'ip':..,'img_port':..,'ser_port':..,'name':..}]。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    found, seen = [], set()
    try:
        for _ in range(2):
            s.sendto(wire.DISCOVERY_PROBE, ('255.255.255.255', wire.DISCOVERY_PORT))
            t0 = time.time()
            while time.time() - t0 < timeout:
                try:
                    data, addr = s.recvfrom(512)
                except socket.timeout:
                    break
                if not data.startswith(wire.DISCOVERY_REPLY):
                    continue
                try:
                    info = wire.parse_discovery_reply(data)
                except Exception:
                    continue
                info.setdefault('ip', addr[0])
                if info['ip'] in seen:
                    continue
                seen.add(info['ip'])
                found.append(info)
    finally:
        s.close()
    return found


def check_ports(host: str, img_port: int, ser_port: int) -> dict:
    """TCP 端口连通性 + 建连耗时。"""
    out = {}
    for name, port in (('图传', img_port), ('串口桥', ser_port)):
        t0 = time.time()
        try:
            sock = socket.create_connection((host, port), timeout=2.0)
            sock.close()
            out[name] = {'ok': True, 'ms': round((time.time() - t0) * 1000, 1)}
        except Exception as e:  # noqa: BLE001
            out[name] = {'ok': False, 'err': str(e)}
    return out


def grab_one(host: str, img_port: int, cam: int = 0) -> dict:
    """拉一帧，返回 {ok,ms,bytes,width,height,seq}。"""
    import cv2
    import numpy as np

    t0 = time.time()
    try:
        sock = socket.create_connection((host, img_port), timeout=3.0)
        sock.settimeout(5.0)
        sock.sendall(wire.pack_request(cam))
        hdr = wire.parse_image_header(wire.recv_exact(sock, wire.IMG_HDR_LEN))
        body = wire.recv_exact(sock, hdr['n'])
        sock.close()
        arr = np.frombuffer(body, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        ok = img is not None
        return {'ok': ok, 'ms': round((time.time() - t0) * 1000, 1),
                'bytes': len(body), 'width': hdr['width'], 'height': hdr['height'],
                'seq': hdr['seq'], 'decoded': ok}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'err': str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description='找派 + 连通性体检')
    ap.add_argument('host', nargs='?', help='派 IP（省略则自动发现）')
    ap.add_argument('--img-port', type=int, default=wire.IMG_PORT)
    ap.add_argument('--ser-port', type=int, default=wire.SER_PORT)
    ap.add_argument('--cam', type=int, default=0, help='0=顶视 1=侧视')
    ap.add_argument('--full', action='store_true', help='额外取一帧并测速')
    ap.add_argument('--timeout', type=float, default=1.5)
    a = ap.parse_args()

    host = a.host
    if not host:
        print('正在 UDP 广播发现派 ...')
        found = discover(a.timeout)
        if not found:
            print('[失败] 没收到派的发现应答。')
            print('  · 确认派已启动 pi_agent.py 且两端在同一网段')
            print('  · 确认 Windows 防火墙允许 UDP 5056')
            print('  · 也可以直接指定 IP：python find_pi.py 192.168.137.50')
            return 2
        for f in found:
            print('  发现：%-16s img=%d ser=%d name=%s' %
                  (f['ip'], f['img_port'], f['ser_port'], f.get('name', '')))
        host = found[0]['ip']
        a.img_port = found[0]['img_port']
        a.ser_port = found[0]['ser_port']

    print('\n目标：%s' % host)
    ports = check_ports(host, a.img_port, a.ser_port)
    for k, v in ports.items():
        print('  %-4s %s' % (k, ('连接 %.0f ms' % v['ms']) if v['ok'] else ('失败：' + v['err'])))

    if a.full:
        r = grab_one(host, a.img_port, a.cam)
        if r['ok']:
            print('  取帧：%d×%d，%d KB，%.0f ms，seq=%d' %
                  (r['width'], r['height'], r['bytes'] // 1024, r['ms'], r['seq']))
        else:
            print('  取帧失败：%s' % r.get('err'))

    ok = ports.get('图传', {}).get('ok') and ports.get('串口桥', {}).get('ok')
    print('\n结论：%s' % ('链路可用，可以启动 gui：python 10.21/main.py --pi %s' % host
                        if ok else '链路不通，按上面提示排查'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
