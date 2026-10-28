"""轴速测试：给定 rpm 走固定脉冲，测实际耗时与丢步（命令 vs 编码器）。

用法（需连上派，GUI 可以同时开着；本工具走只读观察口或独占口均可）：
    python tools/speed_test.py --axis 5 --rpm 300 600 900 --pulses 2000
    python tools/speed_test.py --axis 5 --rpm 800 --pulses 3000 --host 192.168.100.198

判读：
    * 实测脉冲 ≈ 命令脉冲（差 <2%）→ 不丢步，可以放心用这个转速；
    * 差 >3% 或报 E14/stall → 太快，退回上一个档位。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / 'host'))
sys.path.insert(0, str(PKG / 'common'))

import host_client as HC          # noqa: E402
import protocol as P              # noqa: E402


def _counts(client, axis: int, want_after: float) -> int | None:
    """取某轴编码器 counts（从遥测事件里捞）。"""
    t_end = time.time() + want_after
    last = None
    while time.time() < t_end:
        client.tick()
        for ev in client.poll_events():
            if getattr(ev, 'kind', '') == 'tele':
                addr = str(ev.data.get('pairs', {}).get('addr'))
                if addr == str(axis):
                    last = int(float(ev.data['pairs'].get('counts') or 0))
        if last is not None:
            return last
        time.sleep(0.02)
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='192.168.100.198')
    ap.add_argument('--axis', type=int, default=5)
    ap.add_argument('--rpm', type=float, nargs='+', default=[300, 600, 900])
    ap.add_argument('--pulses', type=int, default=2000)
    ap.add_argument('--acc', type=int, default=0)
    args = ap.parse_args()

    client = HC.HostClient(HC.SocketLink(args.host, 5058)) if hasattr(HC, 'SocketLink') else None
    if client is None:
        print('HostClient/SocketLink 接口变了，请检查 host_client')
        return 1
    try:
        print('连接 %s …' % args.host)
        client.start()
        client.wait_online(timeout=8)
        client.send('stop_clear')
        client.send('tele_on', args.axis, 100)
        time.sleep(0.4)
        c0 = _counts(client, args.axis, 1.5)
        print('起始 counts =', c0)

        for rpm in args.rpm:
            c_before = _counts(client, args.axis, 1.0) or 0
            t0 = time.time()
            r = client.send('motor_pos', args.axis, 0, int(rpm), int(args.acc),
                            int(args.pulses), 'rel', timeout_ms=15000)
            dt = time.time() - t0
            time.sleep(0.25)
            c_after = _counts(client, args.axis, 1.2) or 0
            d_counts = c_after - c_before
            d_pulses = d_counts / 20.48
            err = abs(d_pulses - args.pulses) / max(1.0, args.pulses) * 100
            print('rpm=%-5s 用时 %.2f s  实测 %+d 脉冲（命令 %+d）  误差 %.1f%%  应答 %s'
                  % (rpm, dt, round(d_pulses), args.pulses, err,
                     (r or {}).get('ok', '?')))
            time.sleep(0.6)
        client.send('stop_clear')
    finally:
        try:
            client.stop()
        except Exception:               # noqa: BLE001
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
