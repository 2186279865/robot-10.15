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
import os

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
        white = None
        if cfg.get('white_objects', True):
            white = AutoSegmenter._white_mask(hsv, cfg)
        return color, gray_obj, diff, hsv, white

    @staticmethod
    def _white_ok(bbox, area, cfg):
        """白色候选的块级过滤（面积 + 填充率）。

        现场实测（2026-10-31，托盘里放两个白六棱锥的一帧，700×700 校正图）：
          白棱锥 A bbox(212,537,115,124) area=7262 fill=0.52
          白棱锥 B bbox(502,532,139,129) area=7778 fill=0.44
          托盘内壁反光 bbox(560,430,98,108) area=3800 fill=0.37  ← 假目标，必须滤掉
        内壁是一条斜向亮带（底部没有阴影、面积只有白棱锥一半），
        所以用「面积下限 5000 + 填充率 ≥0.35」这两条把它挡在外面。
        真白块比这大得多；万一现场改用更小的白块，把 `white_min_area` 调小即可。
        """
        bw = max(1, int(bbox[2]))
        bh = max(1, int(bbox[3]))
        fill = float(area) / (bw * bh)
        if float(area) < float(cfg.get('white_min_area', 5000)):
            return False
        return fill >= float(cfg.get('white_min_fill', 0.35))

    @staticmethod
    def _white_mask(hsv, cfg):
        """浅色/白色物块的候选（2026-10-31 新增）。

        现场问题：两个**白色六棱锥**一直识别不出来。原因是颜色分支判据是
        `饱和度 > 45`，而白块在画面里 S 只有 11~15（现场实帧实测），
        候选前景里一个点都落不到它们身上 → SAM 没有提示点 → 永远分不出来。
        白块和灰色托盘的区别不在饱和度，而在**亮度**：白块 V 比周围表面高 10%~20%。

        所以这里用「V ÷ 大核高斯模糊(V)」这个**局部亮度比**来找：
        - 比值与整体光照无关（整幅变亮/变暗，比值不变），比固定阈值稳；
        - 核比物块大、比光照渐变尺幅小，托盘本身和阴影都是平的、比值≈1；
        - 仍然要求低饱和 + 足够亮，避免把彩色块的亮面也当成"白块"。

        cfg 可调：`white_ratio`(1.10) / `white_sat_max`(55) / `white_val_min`(80)。
        关掉整条分支：config 里写 `"white_objects": false`。
        """
        h, w = hsv.shape[:2]
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        v = val.astype(np.float32)
        sigma = max(15.0, min(h, w) * 0.04)      # 700 宽时约 28 px
        surf = cv2.GaussianBlur(v, (0, 0), sigma)
        ratio = v / np.maximum(surf, 1.0)
        mul = float(cfg.get('white_ratio', 1.10))
        sat_max = int(cfg.get('white_sat_max', 55))
        val_min = int(cfg.get('white_val_min', 80))
        m = ((ratio > mul) & (sat < sat_max) & (val > val_min)).astype(np.uint8) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
        return m

    # 兼容旧调用名
    _foreground = _foregrounds


    @staticmethod
    def _blobs(mask, strength=None, min_strength=0.0, min_area=None):
        h, w = mask.shape[:2]
        min_a = max(600, int(h * w * .0025)) if min_area is None else int(min_area)
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
    def segment(self, frame, background=None, cfg=None, stats=None):
        """对已透视校正的托盘图做实例分割。

        background: 与 frame 同尺寸的空托盘灰度图（可选），用于灰度物体差分。
        stats:      可选 dict；会写入 `white_blobs`（白色/浅色候选块数），便于现场排查。
        """
        cfg = cfg or {}
        h, w = frame.shape[:2]
        color, gray_obj, diff, hsv, white = self._foregrounds(frame, background, cfg)
        blobs = self._blobs(color)
        fg = color
        white_blobs = 0
        if white is not None:
            # 白色/浅色物块候选：另起一块，和颜色块重叠的不要（避免同一物体打两次点）。
            # 面积下限单独给（白块常被高光切成几块，太小的是反光，不要浪费提示点）。
            for b in self._blobs(white, None, 0.0, int(cfg.get('white_min_area', 5000) * 0.6)):
                if not self._white_ok(b['bbox'], b['area'], cfg):
                    continue
                if all(self._bbox_iou(b['bbox'], k['bbox']) < .30 for k in blobs):
                    blobs.append(b)
                    white_blobs += 1
            fg = cv2.bitwise_or(fg, white)
        if stats is not None:
            stats['white_blobs'] = white_blobs
        if gray_obj is not None:
            # 颜色和差分各自成块，再做重叠合并：差分有残留时不会把彩色物体连成一片。
            for b in self._blobs(gray_obj, diff, 45.0):
                if all(self._bbox_iou(b['bbox'], k['bbox']) < .30 for k in blobs):
                    blobs.append(b)
        fg = fg if gray_obj is None else cv2.bitwise_or(fg, gray_obj)
        if not blobs:
            return []
        pts = self._prompts(blobs, fg)
        if not pts:
            return []
        # 注意：ultralytics 8.3 的 SAM.predict() 不接受 multimask_output（它只属于内部
        # prompt_inference），传入会被 get_cfg() 当成非法参数直接抛 SyntaxError。
        _dev = (cfg.get('device') if cfg else None) or os.environ.get('ROBOT_SAM_DEVICE') or 'cpu'
        result = self.model.predict(frame, points=pts, labels=[1] * len(pts),
                                   verbose=False, device=_dev)[0]
        if result.masks is None:
            return []
        masks = result.masks.data.cpu().numpy()
        scores = result.boxes.conf.cpu().numpy() if result.boxes is not None else np.full(len(masks), .9)
        out = []
        for mask, score in zip(masks, scores):
            m = (mask > 0.5).astype(np.uint8) * 255
            if m.shape[:2] != (h, w):
                m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
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
            # 配置化的目标过滤：托盘外的杯子/夹爪常被当成“物块”。
            _tf = cfg.get('target_filter') or {}
            _amax = int(_tf.get('area_max') or 0)
            if _amax and area > _amax:
                continue
            _edge = int(_tf.get('edge_px') or 0)
            if _edge and _tf.get('drop_edge', True):
                if x <= _edge or y <= _edge or x + bw >= w - _edge or y + bh >= h - _edge:
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
            # 白色/浅色物体在同一道门上会被误杀：白六棱锥 S≈10，现场又没开
            # gray_objects（没有空托盘差分）→ diff_mean=0，两条都不满足。
            # 所以再加一条：掩膜大部分落在白色候选上就认（white 分支自己已经做过
            # 「比周围表面亮 10% 以上 + 低饱和」的判断）。
            _white_hit = 0.0
            if white is not None and sel.any():
                _white_hit = float(np.logical_and(sel, white > 0).sum()) / max(1, int(sel.sum()))
            if sat_mean < 32 and diff_mean < 20 and _white_hit < .35:
                continue
            M = cv2.moments(c)
            cx = M['m10'] / M['m00'] if M['m00'] else x + bw / 2
            cy = M['m01'] / M['m00'] if M['m00'] else y + bh / 2
            _mean_bgr = cv2.mean(frame, mask=(m > 0).astype('uint8'))[:3]
            _hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            _mean_hsv = cv2.mean(_hsv, mask=(m > 0).astype('uint8'))[:3]
            out.append({'mask': m, 'contour': c, 'bbox': [x, y, bw, bh],
                        'center_px': [round(cx, 2), round(cy, 2)],
                        'center_tray': [round(cx / w, 4), round(cy / h, 4)],
                        'mean_bgr': [round(v, 1) for v in _mean_bgr],
                        'mean_hsv': [round(v, 1) for v in _mean_hsv],
                        'area_px': round(area, 1), 'confidence': round(float(score), 3)})
        # 去重：两路提示常常对同一个物体返回“顶面”和“整体”两张掩膜，它们可能只有
        # 部分重叠（IoU 不到 .45），所以除了 IoU/包含，还按中心距离合并同位置目标。
        keep = []
        for item in sorted(out, key=lambda z: z['area_px'], reverse=True):
            dup = False
            for k in keep:
                if self._faces_merge(item, k):
                    # 同一物块的“顶面 + 侧面”两张掩膜：合并成一个整体，
                    # 这样后面的形状/中心/面积才是整块物块的（避免被切成两半）。
                    self._merge_into(k, item, w, h)
                    dup = True
                    break
                if (self._iou(item['mask'], k['mask']) >= .45
                        or self._contain(item['mask'], k['mask']) >= .72
                        or self._near(item, k)):
                    dup = True
                    break
            if not dup:
                keep.append(item)
        keep.sort(key=lambda z: (z['center_tray'][1], z['center_tray'][0]))
        return keep

    def warmup(self, cfg=None, size: int = 700):
        """预热到「第一次真实推理也已经跑过」的程度。

        现场实测（2026-10-31，RTX 5060 / torch 2.7.1+cu128）：
        SAM(...) 只是加载权重，**第一次 predict 还要再花约 8 s**（建算子/传权重）；
        光构造不推理，用户第一次点「连续抓」仍然要等这 8 s。
        这里造一张 700×700 的假场景（灰底 + 一个饱和色块）走完整 segment()，
        把提示点、SAM 推理、掩膜后处理都跑到。失败不影响主流程。

        返回 True 表示这次预热真的跑完了推理。
        """
        cfg = dict(cfg or {})
        # 注意：**不能剥掉 device** —— 真实识别时 segment() 会用 cfg['device']（现场是 cuda），
        # 预热必须跑在同一个设备上，否则预热的是 CPU、真实识别第一次还是要在 CUDA 上重建算子。
        s = max(64, int(size or 700))
        img = np.full((s, s, 3), 110, dtype=np.uint8)     # 灰底：饱和度低，不会被当成候选
        c = int(s * 0.34)
        cv2.rectangle(img, (c, c), (c + s // 6, c + s // 6), (40, 40, 200), -1)  # 饱和红块
        try:
            self.segment(img, None, cfg)
            return True
        except Exception:                              # noqa: BLE001 —— 预热失败只影响速度
            return False

    @staticmethod
    def _faces_merge(item, other, gap=6):
        """两个掩膜是否像块状物体的两个相邻面"顶面+前面"。

        判据：bbox 贴得很近（缝隙 <= gap 像素），且并起来不至于变成两倍大
        （并集宽 <= 1.4 倍较大宽、并集高 <= 1.75 倍较大高）。这样相邻摆放的
        两个独立物块（并集宽/高约 2 倍）不会被误合并。
        """
        ax, ay, aw, ah = item['bbox']
        bx, by, bw, bh = other['bbox']
        if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
            return False
        dx = max(0, max(ax, bx) - min(ax + aw, bx + bw))
        dy = max(0, max(ay, by) - min(ay + ah, by + bh))
        if dx > gap or dy > gap:
            return False
        # 同色系判定：优先看 HSV 色调（曲面物块上下明度差很大，但色调相同）
        ha, hb = item.get('mean_hsv'), other.get('mean_hsv')
        if ha and hb:
            dh = abs(float(ha[0]) - float(hb[0]))
            dh = min(dh, 180.0 - dh)          # OpenCV 色相 0~180，环形距离
            sat_ok = max(float(ha[1]), float(hb[1])) >= 55.0
            if sat_ok:
                if dh > 14.0:                 # 色调差太大 → 不同色物块，不合并
                    return False
            else:
                ca, cb = item.get('mean_bgr'), other.get('mean_bgr')
                if ca and cb:
                    import math as _math
                    d = _math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(ca, cb)))
                    if d > 42.0:
                        return False
        else:
            ca, cb = item.get('mean_bgr'), other.get('mean_bgr')
            if ca and cb:
                import math as _math
                d = _math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(ca, cb)))
                if d > 42.0:
                    return False
        uw = max(ax + aw, bx + bw) - min(ax, bx)
        uh = max(ay + ah, by + bh) - min(ay, by)
        return uw <= 1.40 * max(aw, bw) and uh <= 1.75 * max(ah, bh)

    @staticmethod
    def _merge_into(k, item, w, h):
        """把 item 的掩膜并进 k（并重算轮廓/bbox/中心/面积/置信度）。"""
        import numpy as _np
        u = _np.logical_or(k['mask'] > 0, item['mask'] > 0).astype(_np.uint8) * 255
        cs, _ = cv2.findContours(u, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cs:
            return
        c = max(cs, key=cv2.contourArea)
        area = cv2.contourArea(c)
        x, y, bw, bh = cv2.boundingRect(c)
        M = cv2.moments(c)
        cx = M['m10'] / M['m00'] if M['m00'] else x + bw / 2
        cy = M['m01'] / M['m00'] if M['m00'] else y + bh / 2
        k.update(mask=u, contour=c, bbox=[x, y, bw, bh],
                 center_px=[round(cx, 2), round(cy, 2)],
                 center_tray=[round(cx / w, 4), round(cy / h, 4)],
                 area_px=round(area, 1),
                 confidence=max(float(k['confidence']), float(item['confidence'])))

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
