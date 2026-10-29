"""转盘 60° 比例实测：转 N 次、每次读 counts 算实际角度，给出现有比例误差与建议 scale。

用法（先在上位机点「断开」——5058 是独占的）：
    python tools\\tray_calib.py --host 192.168.100.198 --steps 6 --deg 60
注意：会真的转动转盘（请空盘、别放货）。
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

from host_client import HostClient     # noqa: E402

COUNTS_PER_PULSE = 20.48
AXIS_TRAY = 3


def _wait_online(c: HostClient, timeout=5.0) -> bool:
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        c.tick()
        if c.online:
            return True
        time.sleep(0.05)
    return False


def _counts(c: HostClient):
    info = getattr(c, 'last_counts', {}).get(AXIS_TRAY) or {}
    v = info.get('counts') if isinstance(info, dict) else info
    return None if v is None else float(v)


def _move_rel(c: HostClient, pulses: int, rpm: int, timeout_ms: int = 3000) -> bool:
    rid = c.send('motor_pos', AXIS_TRAY, 0 if pulses > 0 else 1, int(rpm), 0,
                 abs(int(pulses)), 'rel', timeout_ms=timeout_ms)
    rep = c.wait_reply(rid, timeout_ms=timeout_ms)
    return bool(rep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='192.168.100.198')
    ap.add_argument('--port', type=int, default=5058)
    ap.add_argument('--steps', type=int, default=6)
    ap.add_argument('--deg', type=float, default=60.0)
    ap.add_argument('--rpm', type=int, default=6)
    ap.add_argument('--write', action='store_true', help='把建议 scale 写进 runtime/tray_scale.json')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划，不动电机')
    a = ap.parse_args()

    scale_file = PKG / 'runtime' / 'tray_scale.json'
    try:
        scale = float(json.loads(scale_file.read_text(encoding='utf-8')).get('scale', 1.0))
    except Exception:
        scale = 1.0
    want = int(round(a.deg / 360.0 * 3200.0))
    pulses = max(1, int(round(want * scale)))
    print('scale=%.4f | %.0f deg -> want %d pulses -> send %d pulses'
          % (scale, a.deg, want, pulses))
    if a.dry_run:
        print('[dry-run] plan: %d steps x %d pulses (%d rpm), no motion'
              % (a.steps, pulses, a.rpm))
        return 0

    c = HostClient()
    try:
        c.connect('tcp://%s:%d' % (a.host, a.port))
    except Exception as exc:                        # noqa: BLE001
        print('connect failed: %s' % exc)
        return 2
    try:
        if not _wait_online(c):
            print('not online (GUI still holding 5058? click Disconnect first)')
            return 3
        c.send('stop_clear')
        time.sleep(0.2)
        c.tick()
        c.send('tele_on', AXIS_TRAY, 100)
        time.sleep(0.4)
        for _ in range(6):
            c.tick()
            time.sleep(0.05)
        print('start counts =', _counts(c))

        per = []
        start = _counts(c)
        for i in range(a.steps):
            before = _counts(c)
            ok = _move_rel(c, pulses, a.rpm)
            if not ok:
                print('step %d: no reply from motor_pos' % (i + 1))
            t_end = time.monotonic() + 4.0
            last, stable = None, 0
            while time.monotonic() < t_end:
                c.tick()
                time.sleep(0.08)
                now = _counts(c)
                if now is not None and last is not None and abs(now - last) <= 24:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                last = now
            after = _counts(c)
            if before is None or after is None:
                print('step %d: no counts readback' % (i + 1))
                continue
            d_cnt = after - before
            d_pulses = d_cnt / COUNTS_PER_PULSE
            d_deg = d_pulses / 3200.0 * 360.0
            per.append(d_deg)
            print('step %d: counts %+.0f -> actual %+.1f deg, target %.1f deg (err %+.1f)'
                  % (i + 1, d_cnt, d_deg, a.deg, d_deg - a.deg))

        if per:
            avg = sum(per) / len(per)
            sug = scale * (a.deg / avg) if avg else scale
            print('\navg %.2f deg/step (target %.1f) -> suggested scale = %.4f' % (avg, a.deg, sug))
            if start is not None and _counts(c) is not None:
                total = (_counts(c) - start) / COUNTS_PER_PULSE / 3200.0 * 360.0
                print('total %+.1f deg (target %+.1f, err %+.1f)'
                      % (total, a.deg * len(per), total - a.deg * len(per)))
            if a.write:
                scale_file.write_text(json.dumps({'scale': round(sug, 4)}), encoding='utf-8')
                print('written to %s' % scale_file)
            else:
                print('(add --write to save into %s)' % scale_file)
        return 0
    finally:
        try:
            c.disconnect()
        except Exception:                           # noqa: BLE001
            pass


if __name__ == '__main__':
    raise SystemExit(main())
