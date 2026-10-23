"""Measure SAM-B point-prompt inference on CPU only (proxy for SBC performance).

Used to estimate how the new_eye segmentation stage would behave on an
8-core ARM board (Orange Pi 4 Pro, A733: 2xA76 + 6xA55) with no CUDA.
"""
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
NE = ROOT / 'final' / 'new_eye'
MODEL = NE / 'models' / 'sam_b.pt'
IMG = ROOT / 'final' / 'docs' / 'crop_test.jpg'

threads = int(sys.argv[1]) if len(sys.argv) > 1 else 8
torch.set_num_threads(threads)
os.environ.setdefault('YOLO_CONFIG_DIR', str(NE / 'runtime' / 'ultralytics'))

from ultralytics import SAM  # noqa: E402

frame = cv2.imread(str(IMG))
print(f'image: {IMG.name} {frame.shape[1]}x{frame.shape[0]}')
print(f'torch {torch.__version__}, threads={threads}, cuda={torch.cuda.is_available()}')

t0 = time.time()
model = SAM(str(MODEL))
model.to('cpu')
print(f'load model: {time.time() - t0:.2f}s')

h, w = frame.shape[:2]
pts = [[w * 0.5, h * 0.5], [w * 0.4, h * 0.4], [w * 0.6, h * 0.4],
       [w * 0.4, h * 0.6], [w * 0.6, h * 0.6]]

for n in (1, 5):
    p = np.array(pts[:n], dtype=np.float32)
    t = time.time()
    res = model.predict(frame, points=p, labels=[1] * n, device='cpu', verbose=False)
    dt = time.time() - t
    masks = res[0].masks
    nm = 0 if masks is None else len(masks.data)
    print(f'predict {n} point(s): {dt:.2f}s  masks={nm}')

# second frame (steady state: encoder cache warm-ish, new image content)
frame2 = cv2.flip(frame, 1)
t = time.time()
model.predict(frame2, points=np.array(pts, dtype=np.float32), labels=[1] * 5, device='cpu', verbose=False)
print(f'predict 5 points (2nd image): {time.time() - t:.2f}s')
