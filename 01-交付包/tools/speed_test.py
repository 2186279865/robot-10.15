"""轴速测试：给定 rpm 走固定脉冲，测“实际耗时”和“是否丢步”（命令 vs 编码器）。

用法（在 01-交付包 目录下）：
    .venv\\Scripts\\python.exe tools\\speed_test.py --axis 5 --rpm 600 800 1200 --pulses 2000
    .venv\\Scripts\\python.exe tools\\speed_test.py --port tcp://192.168.100.198:5058 --axis 1 --rpm 1500 2000 2200 --pulses 20000

判读：
    实测脉冲 ≈ 命令脉冲（误差 <2%）→ 这个转速安全；
    误差 >3% 或报 E14/stall → 太快，退回上一档。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent
for _p in (PKG / 'host', PKG / 'common'):
    sys.path.insert(0, str(_p))

import host_client          # noqa: E402
import protocol as P        # noqa: E402
import serial_link          # noqa: E402

AXIS_NAME = {1: 'Z', 3: '托盘', 4: 'X', 5: 'Y'}


def pump(hc, link, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data = link.read()
        except Exception:                       # noqa: BLE001
            time.sleep(0.05)
            continue
        if data:
            hc.feed_bytes(data)
        else:
            time.sleep(0.01)
        hc.tick()


def counts_of(hc, axis: int):
    item = (hc.last_counts or {}).get(int(axis))
    if not item:
        return None
    try:
        return float(item.get('counts'))
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description='轴速/丢步测试')
    ap.add_argument('--port', default='tcp://192.168.100.198:5058',
                    help='链路：tcp://<派IP>:5058 或 COM6')
    ap.add_argument('--axis', type=int, default=5, help='1=Z 3=托盘 4=X 5=Y（默认 5=Y）')
    ap.add_argument('--rpm', type=float, nargs='+', default=[600, 800, 1200])
    ap.add_argument('--pulses', type=int, default=2000, help='每档走多少脉冲')
    ap.add_argument('--acc', type=int, default=0)
    args = ap.parse_args()

    link = serial_link.open_best_link(args.port, 115200)
    hc = host_client.HostClient(link)
    if not hc.connect():
        print(f'打不开链路：{hc.last_error}')
        return 1
    pump(hc, link, 2.5)
    print(f'链路={link.describe()}  在线={hc.online}  固件={hc.fw_version}')

    hc.send('stop_clear')
    pump(hc, link, 0.3)
    hc.send('tele_on', args.axis, 200)
    pump(hc, link, 1.2)
    c0 = counts_of(hc, args.axis)
    print(f'{AXIS_NAME.get(args.axis, args.axis)} 轴起始 counts = {c0}')
    if c0 is None:
        print('⚠ 没有遥测回读：先确认 tele_on 生效 / 该轴是否接了编码器')
        return 1

    print(f'\n{"rpm":>6} {"用时s":>8} {"命令":>8} {"实测":>9} {"误差":>7}  应答')
    for rpm in args.rpm:
        before = counts_of(hc, args.axis) or 0.0
        t0 = time.monotonic()
        rid = hc.send('motor_pos', args.axis, 1, int(rpm), int(args.acc),
                      int(args.pulses), 'rel', timeout_ms=20000)
        pump(hc, link, 0.2)
        # 等这条应答（最多 20 s）
        deadline = time.monotonic() + 20.0
        res = None
        while time.monotonic() < deadline:
            pump(hc, link, 0.1)
            res = hc.done_of(rid) if hasattr(hc, 'done_of') else None
            if res is not None:
                break
        dt = time.monotonic() - t0
        pump(hc, link, 0.6)
        after = counts_of(hc, args.axis) or 0.0
        d_pulses = (after - before) / 20.48
        err = abs(abs(d_pulses) - args.pulses) / max(1.0, args.pulses) * 100.0
        code = (res or {}).get('kind') or (res or {}).get('code') or '?'
        print(f'{rpm:6.0f} {dt:8.2f} {args.pulses:8d} {d_pulses:9.0f} {err:6.1f}%  {code}')
        if err > 3.0:
            print(f'   ⚠ 误差 {err:.1f}% > 3%：{rpm:.0f} rpm 可能丢步，建议退回上一档')
        pump(hc, link, 0.8)

    hc.send('stop_clear')
    pump(hc, link, 0.3)
    print('\n完成。若某档误差大，把该轴 rpm 调回上一档（改 host/protocol.py 的 GOTO_RPM_BY_AXIS）。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
