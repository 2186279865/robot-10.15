"""只读探针：经派端串口桥问真 STM32 要状态，判断新固件是否已烧进去。

用法：
    python .survey/probe_bridge2.py [PI_IP] [--ready]

默认只发 hello / status / param_get（不使能、不运动、不停止）。
加 --ready 才额外发一次 gripper ready（夹爪空载时安全，会真的转到 150°）。
"""
import pathlib
import sys
import time

PKG = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / '10.21' / 'common'))
sys.path.insert(0, str(PKG / '10.21' / 'host'))
import serial_link            # noqa: E402
import host_client            # noqa: E402

PI = next((a for a in sys.argv[1:] if not a.startswith('--')), '192.168.100.198')
DO_READY = '--ready' in sys.argv


def pump(hc, link, seconds):
    end = time.time() + seconds
    while time.time() < end:
        for _ in range(200):
            raw = link.read()
            if not raw:
                break
            hc.feed_bytes(raw)
        hc.tick()
        time.sleep(0.02)


def drain(hc):
    out = []
    for ev in hc.poll_events():
        data = getattr(ev, 'data', None) or {}
        pairs = data.get('pairs') if isinstance(data, dict) else None
        out.append((getattr(ev, 'kind', '?'), getattr(ev, 'text', ''), pairs))
    return out


def main():
    link = serial_link.open_best_link('tcp://%s:5058' % PI, 115200)
    hc = host_client.HostClient(link)
    ok = hc.connect()
    print('connect() ->', ok)
    pump(hc, link, 2.5)
    print('online=%s fw=%s boot=%s axes=%s readback=%s' % (
        hc.online, hc.fw_version, hc.boot_id, getattr(hc, 'axis_list', None),
        getattr(hc, 'readback_axes', None)))
    for kind, text, pairs in drain(hc):
        if kind in ('reply', 'event', 'err'):
            print('  [%s] %s %s' % (kind, text, pairs or ''))
    if not hc.online:
        print('❌ 握手未完成')
        link.close()
        return 1

    rid = hc.send('status')
    pump(hc, link, 0.8)
    print('status id=%s latched=%s' % (rid, hc.latched))
    for kind, text, pairs in drain(hc):
        if kind in ('reply', 'err'):
            print('  [%s] %s %s' % (kind, text, pairs or ''))

    for name in ('gripper_ready_deg', 'gripper_hold_max_ms',
                 'gripper_open_deg', 'readback_mask', 'speed_rpm'):
        rid = hc.send('param_get', name)
        pump(hc, link, 0.45)
        got = [p for k, t, p in drain(hc) if k in ('reply', 'err')]
        print('param_get %-20s id=%s -> %s' % (name, rid, got))

    if DO_READY:
        rid = hc.send('gripper', 'ready')
        pump(hc, link, 1.2)
        got = [p for k, t, p in drain(hc) if k in ('reply', 'err')]
        print('gripper ready id=%s -> %s' % (rid, got))
        print('  （回 action=ready 说明已烧新固件；回 E02/E01 说明还是旧固件）')

    link.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
