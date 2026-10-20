"""Reusable YOLO-World detector; all model caches remain inside Qwen."""
import os
import time
from pathlib import Path

import cv2
import numpy as np
from object_segmentation import region_mask

ROOT = Path(__file__).resolve().parent
DEFAULT_PROMPTS = 'toy block, geometric solid, cube, pyramid, cylinder, prism, bottle, cup, ball'
for key, folder in [('YOLO_CONFIG_DIR', 'config/ultralytics'), ('TORCH_HOME', 'cache/torch'),
                    ('XDG_CACHE_HOME', 'cache'), ('HF_HOME', 'cache/huggingface')]:
    (ROOT / folder).mkdir(parents=True, exist_ok=True)
    os.environ[key] = str(ROOT / folder)
os.environ['YOLO_AUTOINSTALL'] = 'false'


class WorldDetector:
    def __init__(self):
        self.model = None
        self.prompts = None

    def detect(self, frame, polygon=None, prompts=DEFAULT_PROMPTS, confidence=.15):
        names = tuple(x.strip() for x in prompts.replace('，', ',').split(',') if x.strip())
        if not names or len(names) > 80 or not .01 <= confidence <= .95:
            raise ValueError('请输入1~80个英文检测提示词；置信度范围0.01~0.95。')
        started = time.perf_counter()
        import torch
        from ultralytics import YOLOWorld
        if self.model is None:
            weights = ROOT / 'models/yolo/yolov8s-worldv2.pt'
            if not weights.exists():
                raise FileNotFoundError(f'缺少检测权重：{weights}')
            self.model = YOLOWorld(str(weights))
            self.model.to('cuda:0' if torch.cuda.is_available() else 'cpu')
        if names != self.prompts:
            # Explicit path avoids CLIP's default cache outside this project.
            from ultralytics.nn.text_model import CLIP
            self.model.model.clip_model = CLIP(str(ROOT / 'cache/clip/ViT-B-32.pt'),
                                              device=torch.device('cpu'))
            try:
                self.model.set_classes(list(names))
            finally:
                # Embeddings are cached by YOLO; free the text encoder before
                # predictor device conversion and leave GPU room for Qwen.
                self.model.model.clip_model = None
            self.prompts = names
        allowed = region_mask(frame.shape, polygon)
        source = frame.copy()
        source[allowed == 0] = 255
        # Use the selected work area at full detector resolution, then restore
        # box coordinates to the rotated full image (never ROI-local pixels).
        offset_x, offset_y, crop_w, crop_h = cv2.boundingRect(allowed)
        if crop_w == 0 or crop_h == 0:
            raise ValueError('选区为空。')
        source = source[offset_y:offset_y+crop_h, offset_x:offset_x+crop_w]
        result = self.model.predict(source, imgsz=640, conf=confidence, iou=.45,
                                    agnostic_nms=True, max_det=21, verbose=False)[0]
        objects = []
        mask = np.zeros(frame.shape[:2], np.uint8)
        height, width = frame.shape[:2]
        for box, score, cls in zip(result.boxes.xyxy.cpu().tolist(),
                                   result.boxes.conf.cpu().tolist(), result.boxes.cls.cpu().tolist()):
            box = [box[0]+offset_x, box[1]+offset_y, box[2]+offset_x, box[3]+offset_y]
            x, y = max(0, int(box[0])), max(0, int(box[1]))
            right, bottom = min(width, int(np.ceil(box[2]))), min(height, int(np.ceil(box[3])))
            if right <= x or bottom <= y:
                continue
            cx, cy = (x+right-1)/2, (y+bottom-1)/2
            if not allowed[int(cy), int(cx)]:
                continue
            contour = np.array([[[x,y]], [[right-1,y]], [[right-1,bottom-1]], [[x,bottom-1]]], np.int32)
            touches = (x == 0 or y == 0 or right == width or bottom == height
                       or bool(np.any(allowed[y:bottom, x:right] == 0)))
            objects.append(dict(bbox=[x,y,right-x,bottom-y], contour=contour,
                                center_px=[cx,cy], area=(right-x)*(bottom-y), touches_roi=touches,
                                detector_label=names[int(cls)], detection_confidence=round(score,4)))
            mask[y:bottom,x:right] = allowed[y:bottom,x:right]
        if len(objects) > 20:
            raise ValueError('候选超过20个，请缩小区域或提高置信度。')
        objects.sort(key=lambda obj: obj['bbox'][:2])
        for index, obj in enumerate(objects, 1):
            obj['object_id'] = index
        return objects, mask, dict(detection_seconds=round(time.perf_counter()-started,3),
                                  detector_device=str(self.model.device), prompts=list(names),
                                  confidence=confidence)
