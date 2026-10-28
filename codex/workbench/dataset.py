from __future__ import annotations

import copy
import csv
import json
import statistics
import time
from pathlib import Path

from .cameras import read_image, save_image
from .settings import RUNTIME
from .vision import VisionPipeline


def capture_sample(top, side, items, root=None):
    root = Path(root or RUNTIME / 'samples')
    folder = root / (time.strftime('%Y%m%d-%H%M%S') + f'-{time.time_ns() % 1000000:06d}')
    folder.mkdir(parents=True, exist_ok=True)
    save_image(folder / 'top.png', top)
    if side is not None:
        save_image(folder / 'side.png', side)
    for i, item in enumerate(items, 1):
        save_image(folder / f'object-{i}.png', item.crop)
    (folder / 'observed.json').write_text(json.dumps(
        {'captured_at': time.time(), 'targets': [item.public() for item in items]},
        ensure_ascii=False, indent=2), encoding='utf-8')
    # Each benchmark row is a tight object crop. Ground truth must be supplied by a human.
    manifest = {'cases': [dict(id=f'object-{i}', top=f'object-{i}.png',
                              expected_shape='', expected_color='', expected_hollow='')
                          for i in range(1, len(items) + 1)]}
    (folder / 'labels.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return folder


def benchmark(cfg, manifest, backends=('geometry', 'local', 'cloud'), repeats=1):
    """Same immutable crop for each backend; no control, no model-produced ground truth."""
    from .models import VisualModel
    from .vision import color_name, geometric_shape
    import cv2
    import numpy as np

    manifest = Path(manifest)
    cases = json.loads(manifest.read_text(encoding='utf-8'))['cases']
    reports = []
    for backend in backends:
        model = VisualModel(cfg['vision'])
        for case in cases:
            top = read_image(manifest.parent / case['top'])
            side = read_image(manifest.parent / case['side']) if case.get('side') else None
            for repeat in range(repeats):
                row = {'backend': backend, 'id': case['id'], 'repeat': repeat + 1,
                       'expected_shape': case.get('expected_shape', ''),
                       'expected_color': case.get('expected_color', ''),
                       'expected_hollow': case.get('expected_hollow', ''), 'error': ''}
                start = time.perf_counter()
                try:
                    if case.get('mask'):
                        mask = cv2.cvtColor(read_image(manifest.parent / case['mask']), cv2.COLOR_BGR2GRAY)
                        if mask.shape != top.shape[:2]:
                            raise ValueError('评测 mask 与图片尺寸不一致')
                        mask = (mask > 127).astype(np.uint8) * 255
                    else:
                        hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
                        mask = ((hsv[:, :, 1] > 38) & (hsv[:, :, 2] > 35)).astype(np.uint8) * 255
                        if cv2.countNonZero(mask) < 30:
                            raise ValueError('低饱和度物体的几何/颜色评测需要人工 mask')
                    color, _ = color_name(top, mask)
                    if backend == 'geometry':
                        shape, hollow, _, _ = geometric_shape(mask)
                    else:
                        result = model.classify(top, side, backend=backend)
                        shape, hollow = result['shape'], result['hollow']
                    row.update(shape=shape, color=color, hollow=hollow)
                    for field in ('shape', 'color', 'hollow'):
                        expected = case.get('expected_' + field)
                        row[field + '_correct'] = (row[field] == expected) if expected else ''
                except Exception as exc:
                    row['error'] = str(exc)
                row['elapsed_ms'] = round((time.perf_counter() - start) * 1000, 1)
                reports.append(row)
                print(f'[{backend}] {case["id"]} {row["elapsed_ms"]:.0f} ms ' +
                      (row['error'] or str(row.get('shape', ''))), flush=True)
    return reports


def write_report(rows, output):
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    columns = sorted(set().union(*(row.keys() for row in rows))) if rows else ['backend']
    with path.with_suffix('.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    summary = {}
    for backend in sorted({r['backend'] for r in rows}):
        subset = [r for r in rows if r['backend'] == backend]
        passed = [r for r in subset if not r['error']]
        times = sorted(r['elapsed_ms'] for r in passed)
        scores = {}
        for field in ('shape', 'color', 'hollow'):
            labeled = [r for r in subset if r.get('expected_' + field)]
            scores[field + '_accuracy'] = (sum(r.get(field + '_correct') is True for r in labeled)
                                            / len(labeled)) if labeled else None
        summary[backend] = dict(calls=len(subset), errors=len(subset) - len(passed),
                                median_ms=statistics.median(times) if times else None, **scores)
    path.with_suffix('.summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary
