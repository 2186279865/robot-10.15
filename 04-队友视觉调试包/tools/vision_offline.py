#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线跑一次分割/识别预览（不连机器人）。

用法：
    python 04-队友视觉调试包/tools/vision_offline.py
    python 04-队友视觉调试包/tools/vision_offline.py --image samples/real_cam0.jpg
    python 04-队友视觉调试包/tools/vision_offline.py --image a.jpg b.jpg --out out

输出：
    <out>/<图片名>_seg.jpg   画了框/中心的预览图
    <out>/<图片名>_seg.json  每个目标的字段（mask 不落盘）

说明：
  - 这里**不做四点单应（700×700）**，直接用原图跑。所以 center_tray 的归一化基准和真机不同；
    要验证"真机 u/v 是否一致"，必须在视觉页的 700×700 校正图上比对。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG4 = HERE.parent
ROOT = PKG4.parent
PKG1 = ROOT / "01-交付包"
VISION = PKG1 / "vision"
MODEL = VISION / "models" / "sam_b.pt"
SAMPLES = PKG4 / "samples"


def default_image() -> Path:
    for name in ("real_cam0.jpg", "real_new0.jpg", "real_cam1.jpg", "real_new1.jpg"):
        p = SAMPLES / name
        if p.is_file():
            return p
    return SAMPLES / "real_cam0.jpg"


def overlay(img, items, out_path: Path) -> None:
    import cv2

    vis = img.copy()
    for i, it in enumerate(items, 1):
        x, y, w, h = [int(v) for v in it["bbox"]]
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cx, cy = [int(v) for v in it["center_px"]]
        cv2.circle(vis, (cx, cy), 4, (0, 0, 255), -1)
        cv2.putText(vis, f"#{i} {it['confidence']:.2f}", (x, max(14, y - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), vis)
    print(f"  预览图：{out_path}")


def strip_item(it: dict) -> dict:
    keep = ("bbox", "center_px", "center_tray", "area_px", "confidence")
    out = {k: it.get(k) for k in keep}
    mask = it.get("mask")
    if mask is not None:
        try:
            out["mask_px"] = int((mask > 0).sum())
        except Exception:                              # noqa: BLE001
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="离线跑一帧分割（不连机器人）")
    ap.add_argument("--image", nargs="+", default=[str(default_image())],
                    help="图片路径（可多张），默认用 samples/real_cam0.jpg")
    ap.add_argument("--model", default=str(MODEL), help="模型路径，默认 01-交付包/vision/models/sam_b.pt")
    ap.add_argument("--out", default=str(PKG4 / "out"), help="输出目录")
    ap.add_argument("--no-save", action="store_true", help="只打印，不落盘")
    args = ap.parse_args()

    model = Path(args.model)
    if not model.is_file():
        print(f"[FAIL] 模型不存在：{model}", file=sys.stderr)
        print("       请从 A 机拷贝 sam_b.pt（375,042,383 字节）到 01-交付包/vision/models/", file=sys.stderr)
        return 1

    try:
        import cv2
        sys.path.insert(0, str(VISION))
        from auto_segment import AutoSegmenter      # noqa: WPS433
    except Exception as exc:                        # noqa: BLE001
        print(f"[FAIL] 导入失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("       先跑 04-队友视觉调试包/启动自检.bat 看缺什么", file=sys.stderr)
        return 1

    print(f"模型：{model}（{model.stat().st_size:,} B）")
    t0 = None
    import time

    t0 = time.time()
    seg = AutoSegmenter(str(model))
    print(f"加载耗时：{time.time() - t0:.2f} s")

    out_dir = Path(args.out)
    rc = 0
    for path_s in args.image:
        path = Path(path_s)
        if not path.is_absolute():
            cand = (Path.cwd() / path)
            path = cand if cand.is_file() else (SAMPLES / path.name)
        if not path.is_file():
            print(f"[FAIL] 图片不存在：{path_s}", file=sys.stderr)
            rc = 1
            continue
        img = cv2.imread(str(path))
        if img is None:
            print(f"[FAIL] 读不出图片：{path}", file=sys.stderr)
            rc = 1
            continue
        t = time.time()
        items = seg.segment(img)
        dt = time.time() - t
        h, w = img.shape[:2]
        print(f"\n{path.name}  {w}×{h}  耗时 {dt:.2f} s  目标 {len(items)} 个")
        for i, it in enumerate(items, 1):
            print(f"  #{i} bbox={it['bbox']} center_px={it['center_px']} "
                  f"center_tray={it['center_tray']} area={it['area_px']:.0f}px conf={it['confidence']:.3f}")
        if not args.no_save:
            stem = path.stem
            overlay(img, items, out_dir / f"{stem}_seg.jpg")
            summary = {
                "image": str(path), "width": w, "height": h, "seconds": round(dt, 3),
                "count": len(items), "items": [strip_item(it) for it in items],
                "note": "离线预览未做 700×700 单应，center_tray 基准与真机不同",
            }
            (out_dir / f"{stem}_seg.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  JSON：{out_dir / (stem + '_seg.json')}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
