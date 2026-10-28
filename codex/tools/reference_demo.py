"""Reproducible evaluation of the user supplied illustration; no robot connections."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main  # sets the writable Ultralytics config location
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from workbench.cameras import read_image, save_image
from workbench.settings import ROOT, load
from workbench.vision import VisionPipeline


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default=str(ROOT / 'datasets/user_geometry_reference/source.jpg'))
    parser.add_argument('--backend', choices=('geometry', 'local', 'cloud'), default='geometry')
    parser.add_argument('--segment', choices=('contour', 'sam_auto'), default='sam_auto')
    args = parser.parse_args()
    folder = ROOT / 'datasets/user_geometry_reference'
    folder.mkdir(parents=True, exist_ok=True)
    frame = read_image(args.source)[:320].copy()  # Exclude the supplied English class captions.
    save_image(folder / 'without_captions.png', frame)
    cfg = load()
    cfg['camera']['top_points'] = []
    cfg['vision'].update(backend='geometry', segmentation=args.segment)
    pipeline = VisionPipeline(cfg)
    start = time.perf_counter()
    items, ms = pipeline.recognize(frame)
    items.sort(key=lambda t: (int(t.center[0] // 320), t.center[0]))
    labels = []
    for i, item in enumerate(items, 1):
        x, y, w, h = item.bbox
        pad = 8
        x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(frame.shape[1], x + w + pad), min(320, y + h + pad)
        crop = frame[y0:y1, x0:x1].copy()
        mask = item.mask[y0:y1, x0:x1]
        # Remove only bright green annotation near the detected silhouette. Preserve body color.
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        border = cv2.dilate(mask, np.ones((7, 7), np.uint8)) - cv2.erode(mask, np.ones((7, 7), np.uint8))
        green_excess = crop[:, :, 1].astype(int) - np.maximum(crop[:, :, 0], crop[:, :, 2]).astype(int)
        ink = (((hsv[:, :, 0] >= 50) & (hsv[:, :, 0] <= 85) &
                (hsv[:, :, 1] > 50) & (green_excess > 25)) & (border > 0)).astype(np.uint8) * 255
        ink = cv2.dilate(ink, np.ones((3, 3), np.uint8))
        clean = cv2.inpaint(crop, ink, 3, cv2.INPAINT_TELEA)
        save_image(folder / f'object-{i}.png', clean)
        save_image(folder / f'object-{i}-mask.png', mask)
        save_image(folder / f'object-{i}-original.png', crop)
        group = int(item.center[0] // 320)
        labels.append(dict(id=f'object-{i}', top=f'object-{i}.png', mask=f'object-{i}-mask.png',
                           expected_shape=['三棱柱', '六棱柱', '菱面体'][group],
                           expected_color='', expected_hollow='',
                           note='形状来自用户示意图的分组标签；颜色、实心/镂空未提供独立真值。绿色轮廓已经边界修复，仍有渲染和修复误差。'))
    manifest = folder / 'labels.json'
    manifest.write_text(json.dumps({'provenance': '用户提供的几何体示意图，仅供功能测试，不能代表实拍准确率。',
                                   'cases': labels}, ensure_ascii=False, indent=2), encoding='utf-8')
    data = dict(backend=args.backend, segmentation=args.segment, segmentation_ms=ms,
                count=len(items), observations=[t.public() for t in items])
    if args.backend != 'geometry':
        from workbench.dataset import benchmark, write_report
        rows = benchmark(cfg, manifest, [args.backend])
        data['model_summary'] = write_report(rows, ROOT / f'runtime/reference/{args.backend}-benchmark.json')
        for item, row in zip(items, rows):
            item.shape = row.get('shape', '接口失败')
            item.note = row.get('error', '')
            item.model_ms = row['elapsed_ms']
    canvas = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize((1440, 480))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 17)
    for i, item in enumerate(items, 1):
        x, y, w, h = item.bbox
        draw.rectangle((x*1.5, y*1.5, (x+w)*1.5, (y+h)*1.5), outline='#08aaf0', width=2)
        text = f'{i}: {item.color} / {item.shape}'
        left = min(x*1.5, 1440 - draw.textlength(text, font=font) - 4)
        top = max(1, y*1.5-25)
        box = draw.textbbox((left, top), text, font=font)
        draw.rectangle((box[0]-2, box[1]-2, box[2]+2, box[3]+2), fill='white')
        draw.text((left, top), text, fill='#103047', font=font)
    output = ROOT / 'runtime/reference'
    output.mkdir(parents=True, exist_ok=True)
    canvas.save(output / f'{args.backend}-annotated.png')
    data['total_seconds'] = time.perf_counter() - start
    (output / f'{args.backend}-observations.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    run()
