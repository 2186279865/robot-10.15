from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field

import cv2
import numpy as np

from .models import VisualModel


@dataclass
class Detection:
    bbox: list
    center: list
    mask: np.ndarray
    crop: np.ndarray
    color: str
    shape: str
    hollow: str
    confidence: float
    source: str = 'geometry'
    track_id: int = 0
    stable: bool = False
    uv: list | None = None
    model_ms: float = 0
    note: str = ''
    side_crop: np.ndarray | None = None
    measured_at: float = field(default_factory=time.time)
    hole_count: int = 0
    visibility: str = '不确定'

    def public(self):
        return {key: getattr(self, key) for key in ('bbox', 'center', 'color', 'shape', 'hollow',
                'confidence', 'source', 'track_id', 'stable', 'uv', 'model_ms', 'note',
                'measured_at', 'hole_count', 'visibility')}

    @property
    def label(self):
        hole = ' · 镂空' if self.hollow == '镂空' else ''
        return f'{self.color} · {self.shape}{hole}'

    @property
    def class_key(self):
        return f'{self.shape}|{self.color}|{self.hollow}'


def color_name(frame, mask):
    """Dominant foreground color; red uses circular hue rather than scalar median."""
    inner = cv2.erode(mask, np.ones((5, 5), np.uint8))
    if cv2.countNonZero(inner) < 30:
        inner = mask
    pixels = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[inner > 0]
    if len(pixels) < 10:
        return '不确定', 0.0
    h, s, v = pixels.T.astype(float)
    names = np.full(len(h), '不确定', dtype=object)
    names[v < 48] = '黑'
    neutral = (s < 35) & (v >= 48)
    names[neutral & (v > 185)] = '白'
    names[neutral & (v <= 185)] = '灰'
    chromatic = (s >= 35) & (v >= 48)
    for name, lo, hi in [('红', 0, 11), ('橙', 11, 23), ('黄', 23, 36),
                          ('绿', 36, 86), ('浅蓝', 86, 103), ('蓝', 103, 132),
                          ('紫', 132, 165), ('红', 165, 180)]:
        names[chromatic & (h >= lo) & (h < hi)] = name
    # Small black lettering must not override a colored object's body.
    body = names[chromatic]
    pool = body if len(body) >= len(names) * .30 else names
    winner, count = Counter(pool).most_common(1)[0]
    return str(winner), round(count / len(pool), 3)


def geometric_shape(mask):
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if not contours or hierarchy is None:
        return '不确定', '不确定', 0.0, 0
    hierarchy = hierarchy[0]
    external = [i for i, h in enumerate(hierarchy) if h[3] == -1]
    idx = max(external, key=lambda i: cv2.contourArea(contours[i]))
    contour = contours[idx]
    area, perimeter = cv2.contourArea(contour), cv2.arcLength(contour, True)
    if area < 20 or perimeter == 0:
        return '不确定', '不确定', 0.0, 0
    holes = [i for i, h in enumerate(hierarchy) if h[3] == idx
             and cv2.contourArea(contours[i]) > max(80, area * .035)]
    hollow = '镂空' if holes else '不确定'
    hull = cv2.convexHull(contour)
    solidity = area / max(1, cv2.contourArea(hull))
    rw, rh = cv2.minAreaRect(contour)[1]
    ratio = max(rw, rh) / max(1, min(rw, rh))
    fill = area / max(1, rw * rh)
    circularity = 4 * np.pi * area / perimeter ** 2
    # Several approximation scales must agree; a single threshold's vertex count is brittle.
    votes = [len(cv2.approxPolyDP(contour, perimeter * eps, True)) for eps in (.015, .02, .025, .03)]
    vertices, n = Counter(votes).most_common(1)[0]
    if circularity > .84 and ratio < 1.2:
        return '圆形截面体', hollow, .72, len(holes)
    if fill > .91:
        return ('方形截面体' if ratio < 1.18 else '矩形截面体'), hollow, .74, len(holes)
    if vertices in (3, 5, 6) and n >= 2 and solidity > .92:
        return {3: '三角形截面体', 5: '五边形截面体', 6: '六边形截面体'}[vertices], hollow, .70, len(holes)
    return '不确定', hollow, .35, len(holes)


def perspective_points(points, frame):
    if len(points) != 4:
        return None
    h, w = frame.shape[:2]
    pts = np.asarray(points, np.float32)
    if pts.shape != (4, 2) or not np.isfinite(pts).all() or np.any(pts < 0) or np.any(pts > 1):
        raise ValueError('托盘四点必须为 0~1 的归一化坐标')
    pts = pts * [w, h]
    contour = pts.astype(np.float32).reshape(-1, 1, 2)
    if not cv2.isContourConvex(contour) or abs(cv2.contourArea(contour)) < w * h * .01:
        raise ValueError('四点顺序或区域无效，请按左上、右上、右下、左下选择')
    return pts.astype(np.float32)


class Segmenter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sam = None

    def foreground(self, frame, points, background=None):
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        color = ((hsv[:, :, 1] > 38) & (hsv[:, :, 2] > 35)).astype(np.uint8) * 255
        if background is not None:
            if frame.shape != background.shape:
                raise ValueError('空背景分辨率与当前相机不同，请重新采集')
            diff = cv2.absdiff(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                               cv2.cvtColor(background, cv2.COLOR_BGR2GRAY))
            gray_objects = ((diff > 32) & (hsv[:, :, 1] < 55)).astype(np.uint8) * 255
            color |= gray_objects
        color = cv2.morphologyEx(color, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        roi = np.full(color.shape, 255, np.uint8)
        pts = perspective_points(points, frame)
        if pts is not None:
            roi[:] = 0
            cv2.fillConvexPoly(roi, pts.astype(np.int32), 255)
        color &= roi
        return color, pts

    def detect(self, frame, points=(), background=None):
        fg, pts = self.foreground(frame, points, background)
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        h, w = fg.shape
        candidates = [c for c in contours if self.cfg['min_area'] <= cv2.contourArea(c) < h * w * .45]
        masks = []
        if self.cfg['segmentation'] in ('sam', 'sam_auto') and (candidates or self.cfg['segmentation'] == 'sam_auto'):
            if self.sam is None:
                from ultralytics import SAM
                self.sam = SAM(self.cfg['sam_model'])
            boxes = [list(cv2.boundingRect(c)) for c in candidates]
            xyxy = [[x, y, x + bw, y + bh] for x, y, bw, bh in boxes]
            kwargs = {} if self.cfg['segmentation'] == 'sam_auto' else {'bboxes': xyxy}
            results = self.sam.predict(frame, imgsz=640, verbose=False, **kwargs)[0]
            if results.masks is not None:
                for raw in results.masks.data.cpu().numpy():
                    m = cv2.resize((raw > .5).astype(np.uint8) * 255, (w, h), interpolation=cv2.INTER_NEAREST)
                    masks.append(m)
            if self.cfg['segmentation'] == 'sam_auto':
                # Whole-image SAM also finds the background and individual faces.
                # Reject image-spanning regions, then remove masks contained in a larger object.
                selected = []
                for m in sorted(masks, key=cv2.countNonZero, reverse=True):
                    yy, xx = np.where(m > 0)
                    if not len(xx) or cv2.countNonZero(m) > w * h * .25:
                        continue
                    if xx.max() - xx.min() > w * .9 or yy.max() - yy.min() > h * .95:
                        continue
                    if any(cv2.countNonZero(m & old) / max(1, cv2.countNonZero(m)) > .8 for old in selected):
                        continue
                    if pts is not None and cv2.pointPolygonTest(pts, (float(xx.mean()), float(yy.mean())), False) < 0:
                        continue
                    selected.append(m)
                masks = selected
        else:
            for c in candidates:
                outer = np.zeros_like(fg)
                cv2.drawContours(outer, [c], -1, 255, -1)
                # Keep inner background holes. Do not globally fill holes as in the old implementation.
                masks.append(cv2.bitwise_and(outer, fg))
        detections = []
        transform = cv2.getPerspectiveTransform(pts, np.float32([[0, 0], [1, 0], [1, 1], [0, 1]])) if pts is not None else None
        for mask in masks:
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not cnts:
                continue
            c = max(cnts, key=cv2.contourArea)
            if cv2.contourArea(c) < self.cfg['min_area']:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            if bw < 10 or bh < 10:
                continue
            moments = cv2.moments(c)
            cx = moments['m10'] / max(1, moments['m00'])
            cy = moments['m01'] / max(1, moments['m00'])
            if any(np.linalg.norm(np.array([cx, cy]) - d.center) < min(bw, bh) * .35 for d in detections):
                continue
            color, cc = color_name(frame, mask)
            # Color silhouette is supplementary evidence for visible holes even if SAM fills them.
            hole_mask = mask & fg
            shape, hollow, sc, holes = geometric_shape(hole_mask)
            pad = max(8, round(max(bw, bh) * .12))
            crop = frame[max(0, y - pad):min(h, y + bh + pad), max(0, x - pad):min(w, x + bw + pad)].copy()
            uv = None
            if transform is not None:
                uv = cv2.perspectiveTransform(np.float32([[[cx, cy]]]), transform)[0, 0].tolist()
            detection = Detection([x, y, bw, bh], [cx, cy], mask, crop, color, shape,
                                  hollow, min(cc, sc), uv=uv, hole_count=holes)
            detection.note = '二维外形；需模型确认三维名称。孔洞仅为可见证据，印字也可能形成暗区。'
            detections.append(detection)
        return detections


class Tracker:
    def __init__(self):
        self.tracks = {}
        self.next_id = 1

    def update(self, items, size):
        now = time.monotonic()
        self.tracks = {k: v for k, v in self.tracks.items() if now - v['seen'] < 10}
        used = set()
        max_dist = max(size) * .07
        for item in items:
            choices = [(np.linalg.norm(np.array(item.center) - old['center']), tid)
                       for tid, old in self.tracks.items() if tid not in used]
            distance, tid = min(choices) if choices else (float('inf'), None)
            if distance > max_dist:
                tid = self.next_id
                self.next_id += 1
                self.tracks[tid] = {'history': deque(maxlen=4)}
            old = self.tracks[tid]
            old.update(center=item.center, seen=now)
            old['history'].append((item.shape, item.color, item.hollow))
            key, votes = Counter(old['history']).most_common(1)[0]
            item.track_id = tid
            item.stable = votes >= 2 and key == (item.shape, item.color, item.hollow)
            used.add(tid)


class VisionPipeline:
    def __init__(self, cfg):
        self.cfg = copy.deepcopy(cfg)
        self.segmenter = Segmenter(self.cfg['vision'])
        self.model = VisualModel(self.cfg['vision'])
        self.tracker = Tracker()
        self.backgrounds = {}
        self.cache = {}
        self.lock = threading.Lock()

    def recognize(self, top, side=None, on_partial=None):
        with self.lock:
            captured_at = time.time()
            start = time.perf_counter()
            vc, cc = self.cfg['vision'], self.cfg['camera']
            items = self.segmenter.detect(top, cc['top_points'], self.backgrounds.get('top'))
            for item in items:
                item.measured_at = captured_at
            if on_partial and vc['backend'] != 'geometry':
                on_partial([copy.copy(t) for t in items], (time.perf_counter() - start) * 1000)
            side_items = []
            if side is not None and vc['side_pairing'] and cc['side_points']:
                side_items = self.segmenter.detect(side, cc['side_points'], self.backgrounds.get('side'))
            used_side = set()
            for item in items:
                if item.uv is not None and side_items:
                    coordinate = item.uv[0 if vc['side_axis'] == 'u' else 1]
                    options = []
                    for i, s in enumerate(side_items):
                        if i in used_side or s.uv is None or s.color != item.color:
                            continue
                        value = 1 - s.uv[0] if vc['side_flip'] else s.uv[0]
                        if abs(value - coordinate) < .12:
                            options.append((abs(value - coordinate), i))
                    # Same-color overlapping columns remain ambiguous: do not invent a pairing.
                    if len(options) == 1:
                        index = options[0][1]
                        item.side_crop = side_items[index].crop
                        used_side.add(index)
                if vc['backend'] != 'geometry':
                    signature = hashlib.sha256(item.crop.tobytes() + str(item.crop.shape).encode()).hexdigest()
                    side_signature = hashlib.sha256(item.side_crop.tobytes()).hexdigest() if item.side_crop is not None else ''
                    key = (vc['backend'], signature, side_signature)
                    cached = self.cache.get(key)
                    if cached and time.monotonic() - cached[0] < vc['cache_seconds']:
                        result = cached[1]
                    else:
                        try:
                            result = self.model.classify(item.crop, item.side_crop)
                            self.cache[key] = (time.monotonic(), result)
                        except Exception as exc:
                            item.shape = '不确定'
                            item.source = vc['backend'] + '-error'
                            item.confidence = 0
                            item.note = str(exc)
                            if on_partial:
                                on_partial([copy.copy(t) for t in items], (time.perf_counter() - start) * 1000)
                            continue
                    item.shape, item.hollow, item.source = result['shape'], result['hollow'], result['source']
                    item.model_ms = result['model_ms']
                    item.visibility = result['visibility']
                    item.note = result['reason']
                    if result['visibility'] != '完整' or item.shape == '不确定':
                        item.confidence = min(item.confidence, .4)
                    if result['model_color'] not in (item.color, '不确定'):
                        item.note += f'；模型颜色={result["model_color"]}，主体统计={item.color}'
                    if on_partial:
                        on_partial([copy.copy(t) for t in items], (time.perf_counter() - start) * 1000)
            self.cache = {k: v for k, v in self.cache.items() if time.monotonic() - v[0] < vc['cache_seconds']}
            self.tracker.update(items, top.shape[:2])
            return items, round((time.perf_counter() - start) * 1000, 1)
