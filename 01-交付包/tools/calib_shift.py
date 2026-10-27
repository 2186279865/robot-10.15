"""把 tray_calib.json 的 Y 基准整体平移（零点对齐用）。

用法：
    python tools/calib_shift.py --dy -43045            # 只看结果（不写盘）
    python tools/calib_shift.py --dy -43045 --write    # 写盘（自动备份）

背景：位置清零 / 断电重连会让软位置零点变，标定记录的是"旧零点"下的脉冲；
用一次"手动抓到物块"的实测 (u,v) 与真实脉冲就能算出零点差，整体平移即可，
不用重标（方向与比例不变）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
CAL = PKG / 'vision' / 'tray_calib.json'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='平移 tray_calib 的 Y 基准')
    ap.add_argument('--dy', type=float, required=True, help='Y 平移量（脉冲），负=基准变小')
    ap.add_argument('--dx', type=float, default=0.0, help='X 平移量（脉冲），默认 0')
    ap.add_argument('--u', type=float, help='锚点 u（用于打印反算校验）')
    ap.add_argument('--v', type=float, help='锚点 v')
    ap.add_argument('--write', action='store_true', help='写盘（自动备份）')
    args = ap.parse_args(argv)

    d = json.loads(CAL.read_text(encoding='utf-8'))
    af = d['affine']
    before = (af['tx'], af['ty'])

    def predict(u, v):
        return (af['a'] * u + af['b'] * v + af['tx'],
                af['c'] * u + af['d'] * v + af['ty'])

    if args.u is not None and args.v is not None:
        x0, y0 = predict(args.u, args.v)
        print('锚点 (u=%.4f, v=%.4f) 平移前预测： X=%.0f  Y=%.0f' % (args.u, args.v, x0, y0))

    af['tx'] += args.dx
    af['ty'] += args.dy
    d['affine'] = af
    d['updated_at'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    if args.u is not None and args.v is not None:
        x1, y1 = predict(args.u, args.v)
        print('平移后预测： X=%.0f  Y=%.0f   （Δx=%+.0f, Δy=%+.0f）' % (x1, y1, args.dx, args.dy))

    print('tx: %.1f → %.1f   ty: %.1f → %.1f' % (before[0], af['tx'], before[1], af['ty']))

    if not args.write:
        print('（干跑，未写盘；加 --write 落地）')
        return 0

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    bak = CAL.with_name('tray_calib.%s.bak.json' % stamp)
    shutil.copy2(CAL, bak)
    CAL.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    print('已写入 %s（备份 %s）' % (CAL, bak.name))
    return 0


if __name__ == '__main__':
    sys.exit(main())
