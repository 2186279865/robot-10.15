"""Unknown-object instance segmentation backend.

SAM (Ultralytics) is used as a boundary refiner instead of a blind prompt grid:
a colour / empty-background foreground pass first proposes candidate blobs, and
only those blobs contribute point prompts.  Every prompt therefore lands on a
real foreground pixel, so empty tray regions no longer produce phantom masks
(the old 5x5 grid version did exactly that).

Put sam_b.pt (or sam2_b.pt) in new_eye/models/.
"""
from pathlib import Path
import os
import cv2

try:
    import shape_features as _SF
except Exception:            # noqa: BLE001
    _SF = None
import numpy as np

ROOT = Path(__file__).resolve().parent

# Ultralytics 默认把设置/缓存写到 %APPDATA%\Ultralytics（可能没写权限，
# 启动时会报 "Error writing to ...settings.json"）。统一指到 new_eye/runtime/，
# 让整个工具链只依赖 final/ 这一棵树。
os.environ.setdefault('YOLO_CONFIG_DIR', str(ROOT / 'runtime' / 'ultralytics'))


class AutoSegmenter:
    def __init__(self, model_path=None):
        from ultralytics import SAM
        candidates = [Path(model_path)] if model_path else [ROOT/'models'/'sam_b.pt', ROOT/'models'/'sam2_b.pt']
        # config.json 里写的是相对 new_eye 目录的路径（如 models/sam_b.pt），
        # 而 GUI 可能从任意工作目录启动，所以这里同时按 cwd 和 new_eye 目录解析。
        self.path = None
        for p in candidates:
            p = Path(p)
            search = [p] if p.is_absolute() else [p, ROOT / p]
            for q in search:
                if q.exists():
                    self.path = q
                    break
            if self.path is not None:
                break
        if self.path is None:
            raise FileNotFoundError('未找到SAM权重，请把sam_b.pt放入new_eye/models/')
        self.model = SAM(str(self.path))

    # ------------------------------------------------------------------ 前处理
    @staticmethod
    def _foregrounds(frame, background, cfg):
        """返回 (候选颜色掩膜, 灰度差分掩膜或None, 差分图, HSV 图)。

        默认只用颜色（饱和度）找候选物体：空托盘本身是灰色的，产生不了候选，
        阴影也不会（阴影饱和度同样很低），所以不会再往空的地方打点。
        灰/黑物料与灰色托盘区分不开，需要“采集空托盘”并在 config 里设
        "gray_objects": true 才启用背景差分分支；若背景图与当前画面差异过大
        （换托盘/换光照/没重新采集），自动放弃差分分支，避免整幅图都变成前景。
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        sat, val = hsv[:, :, 1], hsv[:, :, 2]
        color = ((sat > 45) & (val > 35)).astype(np.uint8) * 255
        color = cv2.morphologyEx(color, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        color = cv2.morphologyEx(color, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        diff = np.zeros_like(gray)
        gray_obj = None
        if background is not None and background.ndim == 3:
            # 允许调用方直接传彩色空托盘图（GUI 传的是灰度，命令行工具可能传彩色）。
            background = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
        if cfg.get('gray_objects') and background is not None and background.shape[:2] == gray.shape:
            diff = cv2.absdiff(gray, background)
            h, w = gray.shape[:2]
            inner = diff[int(h * .12):int(h * .88), int(w * .12):int(w * .88)]
            # 背景过期保护：空托盘背景应是“同机位、同光照”下拍的。整幅差异偏大说明
            # 换过托盘/光照，此时灰度差分不可信，宁可不启用也不要把整幅图当物体。
            if inner.size and float(np.median(inner)) <= 18:
                gray_obj = ((diff > max(30, float(cfg.get('background_diff', 26))))
                            & (sat < 55)).astype(np.uint8) * 255
                gray_obj = cv2.morphologyEx(gray_obj, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        return color, gray_obj, diff, hsv

    # 兼容旧调用名
    _foreground = _foregrounds


    @staticmethod
    def _blobs(mask, strength=None, min_strength=0.0):
        h, w = mask.shape[:2]
        min_a = max(600, int(h * w * .0025))
        max_a = int(h * w * .30)
        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        blobs = []
        for lab in range(1, n):
            x, y, bw, bh, a = stats[lab]
            if not (min_a <= a <= max_a) or bw < 12 or bh < 12:
                continue
            if a / max(1, bw * bh) < .30:
                continue
            if max(bw, bh) / max(1, min(bw, bh)) > 6:
                continue
            if x <= 2 or y <= 2 or x + bw >= w - 2 or y + bh >= h - 2:
                # 贴边的大块一律视为托盘/背景；贴边小块（阴影带/托盘边缘）也不要。
                continue
            if strength is not None:
                # 灰度候选必须“确实和空托盘不一样”，排除背景图过时留下的弱差异。
                if float(strength[labels == lab].mean()) < min_strength:
                    continue
            blobs.append({'bbox': (int(x), int(y), int(bw), int(bh)), 'area': int(a),
                          'center': (float(cents[lab][0]), float(cents[lab][1]))})
        return blobs

    @staticmethod
    def _prompts(blobs, mask):
        """每个候选块撒一组内部点：相邻物体在颜色掩膜里可能连成一块，
        均匀铺点才能保证每个物体都至少有一个提示。"""
        h, w = mask.shape[:2]
        per = []
        for b in blobs:
            x, y, bw, bh = b['bbox']
            cand = [(b['center'][0], b['center'][1])]
            for fy in (.2, .4, .6, .8):
                for fx in (.2, .4, .6, .8):
                    cand.append((x + bw * fx, y + bh * fy))
            keep = []
            for px, py in cand:
                p = (int(round(px)), int(round(py)))
                if not (0 <= p[0] < w and 0 <= p[1] < h) or mask[p[1], p[0]] == 0:
                    continue
                if all(abs(p[0] - q[0]) >= 8 or abs(p[1] - q[1]) >= 8 for q in keep):
                    keep.append(p)
            if keep:
                per.append(keep)
        pts = []
        while len(pts) < 40 and any(per):
            for keep in per:
                if keep and len(pts) < 40:
                    pts.append(keep.pop(0))
        return [list(p) for p in pts]

    # ------------------------------------------------------------------ 主入口
    def segment(self, frame, background=None, cfg=None):
        """对已透视校正的托盘图做实例分割。

        background: 与 frame 同尺寸的空托盘灰度图（可选），用于灰度物体差分。
        """
        cfg = cfg or {}
        h, w = frame.shape[:2]
        color, gray_obj, diff, hsv = self._foregrounds(frame, background, cfg)
        blobs = self._blobs(color)
        if gray_obj is not None:
            # 颜色和差分各自成块，再做重叠合并：差分有残留时不会把彩色物体连成一片。
            for b in self._blobs(gray_obj, diff, 45.0):
                if all(self._bbox_iou(b['bbox'], k['bbox']) < .30 for k in blobs):
                    blobs.append(b)
        fg = color if gray_obj is None else cv2.bitwise_or(color, gray_obj)
        if not blobs:
            return []
        pts = self._prompts(blobs, fg)
        if not pts:
            return []
        # 注意：ultralytics 8.3 的 SAM.predict() 不接受 multimask_output（它只属于内部
        # prompt_inference），传入会被 get_cfg() 当成非法参数直接抛 SyntaxError。
        result = self.model.predict(frame, points=pts, labels=[1] * len(pts), verbose=False)[0]
        if result.masks is None:
            return []
        masks = result.masks.data.cpu().numpy()
        scores = result.boxes.conf.cpu().numpy() if result.boxes is not None else np.full(len(masks), .9)
        out = []
        for mask, score in zip(masks, scores):
            m = (mask > 0.5).astype(np.uint8) * 255
            if m.shape[:2] != (h, w):
                m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
            # 掩膜加固：反光/贴纸会把掩膜打出洞，闭运算+填洞后再算形状，宽高比与圆度才稳。
            if _SF is not None:
                try:
                    m = _SF.clean_mask(m, close_ksize=3)
                except Exception:            # noqa: BLE001
                    pass
            contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            c = max(contours, key=cv2.contourArea)
            area = cv2.contourArea(c)
            if area < max(600.0, h * w * .004) or area > h * w * .45:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            # 托盘/背景掩膜：贴边且面积大，直接丢弃（托盘本体就是最大的背景块）。
            if (x <= 3 or y <= 3 or x + bw >= w - 3 or y + bh >= h - 3) and area > h * w * .15:
                continue
            # 掩膜必须确实落在候选前景上，否则就是空托盘/阴影被 SAM“补”出来的假目标。
            if fg[y:y + bh, x:x + bw].size == 0:
                continue
            if (fg[y:y + bh, x:x + bw] > 0).mean() < .30:
                continue
            sel = m > 0
            fg_ratio = float(np.logical_and(sel, fg > 0).sum()) / max(1, int(sel.sum()))
            if fg_ratio < .35:
                continue
            # 物体性：掩膜内部至少要有明显颜色或与空托盘的亮度差。
            sat_mean = float(hsv[:, :, 1][sel].mean()) if sel.any() else 0.0
            diff_mean = float(diff[sel].mean()) if sel.any() else 0.0
            if sat_mean < 32 and diff_mean < 20:
                continue
            M = cv2.moments(c)
            cx = M['m10'] / M['m00'] if M['m00'] else x + bw / 2
            cy = M['m01'] / M['m00'] if M['m00'] else y + bh / 2
            out.append({'mask': m, 'contour': c, 'bbox': [x, y, bw, bh],
                        'center_px': [round(cx, 2), round(cy, 2)],
                        'center_tray': [round(cx / w, 4), round(cy / h, 4)],
                        'area_px': round(area, 1), 'confidence': round(float(score), 3)})
        # 去重：两路提示常常对同一个物体返回“顶面”和“整体”两张掩膜，它们可能只有
        # 部分重叠（IoU 不到 .45），所以除了 IoU/包含，还按中心距离合并同位置目标。
        keep = []
        for item in sorted(out, key=lambda z: z['area_px'], reverse=True):
            dup = False
            for k in keep:
                if (self._iou(item['mask'], k['mask']) >= .45
                        or self._contain(item['mask'], k['mask']) >= .72
                        or self._near(item, k)):
                    dup = True
                    break
            if not dup:
                keep.append(item)
        keep.sort(key=lambda z: (z['center_tray'][1], z['center_tray'][0]))
        return keep

    @staticmethod
    def _bbox_iou(a, b):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        x0, y0 = max(ax, bx), max(ay, by)
        x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        inter = float((x1 - x0) * (y1 - y0))
        return inter / max(1.0, aw * ah + bw * bh - inter)

    @staticmethod
    def _iou(a, b):
        inter = np.logical_and(a > 0, b > 0).sum()
        union = np.logical_or(a > 0, b > 0).sum()
        return float(inter / union) if union else 0.0

    @staticmethod
    def _contain(a, b):
        inter = np.logical_and(a > 0, b > 0).sum()
        small = min((a > 0).sum(), (b > 0).sum())
        return float(inter / small) if small else 0.0

    @staticmethod
    def _near(item, other):
        """两个掩膜的中心离得很近（相对自身尺寸）→ 认为是同一物体重复检出。

        用中心距 + 尺寸比判断，不依赖掩膜重叠：同一物体的“顶面掩膜”和“整体
        掩膜”常常只有一部分重合，IoU 可能只有 .2~.4，但中心几乎重合。
        """
        ax, ay, aw, ah = item['bbox']
        bx, by, bw, bh = other['bbox']
        dx = (ax + aw / 2) - (bx + bw / 2)
        dy = (ay + ah / 2) - (by + bh / 2)
        dist = (dx * dx + dy * dy) ** .5
        size = min(max(aw, ah), max(bw, bh))
        return dist < .35 * size and min(aw * ah, bw * bh) / max(1.0, aw * ah, bw * bh) > .25
