"""对比「快速定位(locate_fast)」与「SAM 分割」在同一张干净校正帧上的结果。

用法：
    python tools/locate_check.py                     # 用 vision/runtime/last_top_warp.jpg
    python tools/locate_check.py 路径/某帧.jpg
输出：两者目标数、逐个 (u,v) 偏差（像素与毫米）、耗时。
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / 'vision'))
sys.path.insert(0, str(PKG / 'common'))

import cv2                      # noqa: E402
import numpy as np              # noqa: E402

import locate_fast              # noqa: E402
import auto_segment             # noqa: E402

CFG = json.loads((PKG / 'vision/config.json').read_text(encoding='utf-8'))
SIZE, TRAY_MM = 700, float((CFG.get('shape_calib') or {}).get('tray_mm') or 160.0)
MM_PER_PX = TRAY_MM / SIZE


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else (PKG / 'vision/runtime/last_top_warp.jpg')
    if not path.is_file():
        print(f'缺帧：{path}\n→ 先在 GUI 里点一次「单次识别」（新版会自动存 last_top_warp.jpg）')
        return 1
    # cv2.imread 对中文路径会失败，用 fromfile + imdecode
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print(f'读不出：{path}')
        return 1
    print(f'帧：{path.name}  {img.shape[1]}x{img.shape[0]}')

    t0 = time.perf_counter()
    fast = locate_fast.locate(img)
    tf = (time.perf_counter() - t0) * 1000

    seg = auto_segment.AutoSegmenter(CFG.get('sam_model'))
    t0 = time.perf_counter()
    sam = [s for s in seg.segment(img, None, CFG)
           if 2000 <= s.get('area_px', 0) <= 48000]
    ts = (time.perf_counter() - t0) * 1000

    print(f'\n快速定位：{len(fast)} 个，{tf:.1f} ms')
    for d in fast:
        print(f"   u={d['u']:.3f} v={d['v']:.3f} area={float(d['area_px']):8.0f}")
    print(f'SAM     ：{len(sam)} 个，{ts:.0f} ms')
    for s in sam:
        u, v = s['center_tray']
        print(f'   u={u:.3f} v={v:.3f} area={float(s["area_px"]):8.0f}')

    if not fast or not sam:
        print('\n⚠ 有一边没目标，无法配对（可能是空托盘）')
        return 0
    print('\n配对偏差（快速定位 → 最近 SAM）：')
    worst = 0.0
    for d in fast:
        best, bd = None, 9.9
        for s in sam:
            u, v = s['center_tray']
            dd = math.hypot(d['u'] - u, d['v'] - v)
            if dd < bd:
                bd, best = dd, s
        worst = max(worst, bd)
        u, v = best['center_tray']
        print(f"   ({d['u']:.3f},{d['v']:.3f}) → ({u:.3f},{v:.3f})  "
              f"Δ={bd * SIZE:5.1f}px ≈ {bd * SIZE * MM_PER_PX:4.1f}mm  "
              f"面积 {d['area_px']} vs {best['area_px']}")
    print(f'\n最大偏差 {worst * SIZE:.1f}px ≈ {worst * SIZE * MM_PER_PX:.1f}mm '
          f'（抓取允许 ±5mm；>15mm 需调阈值）')
    print(f'速度比：快速定位 {tf:.0f} ms vs SAM {ts:.0f} ms → {ts / max(tf, 1e-6):.0f}×')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
