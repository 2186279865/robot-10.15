import cv2
import numpy as np
from collections import Counter

DEFAULT_CLASS_NAMES = {
    "1": "类别1", "2": "类别2", "3": "类别3",
    "4": "类别4", "5": "类别5", "6": "类别6"
}

def crop_roi(frame, roi):
    if not roi:
        return frame, (0, 0)
    x, y, w, h = roi
    return frame[y:y+h, x:x+w], (x, y)

def _shape(contour):
    area = cv2.contourArea(contour)
    peri = cv2.arcLength(contour, True)
    if area <= 0 or peri <= 0:
        return "未知", 0.0
    circularity = 4 * np.pi * area / (peri * peri)
    approx = cv2.approxPolyDP(contour, 0.04 * peri, True)
    x, y, w, h = cv2.boundingRect(contour)
    ratio = max(w, h) / max(1, min(w, h))
    if circularity > 0.78 and ratio < 1.25:
        return "圆柱/圆形", min(1.0, circularity)
    if len(approx) == 3:
        return "三棱体/三角形", 0.82
    if len(approx) == 4:
        return ("正方体/正方形" if ratio < 1.25 else "长方体/长方形"), 0.80
    if len(approx) == 5:
        return "五棱体", 0.76
    return "其他几何体", 0.55

def _color(roi):
    if roi.size == 0:
        return "未知", 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # 去掉轮廓边缘影响，只统计中心区域
    h, w = hsv.shape[:2]
    y0, y1 = h // 5, max(h // 5 + 1, 4 * h // 5)
    x0, x1 = w // 5, max(w // 5 + 1, 4 * w // 5)
    p = hsv[y0:y1, x0:x1].reshape(-1, 3)
    sat = p[:, 1]; val = p[:, 2]
    med_s, med_v = float(np.median(sat)), float(np.median(val))
    if med_v < 55: return "黑", 0.86
    if med_s < 35 and med_v > 180: return "浅色/白", 0.55
    hue = float(np.median(p[:, 0]))
    if hue < 10 or hue >= 170: return "红", 0.78
    if hue < 25: return "黄/橙", 0.76
    if hue < 45: return "绿", 0.72
    if hue < 100: return "蓝/浅蓝", 0.72
    return "紫/其他", 0.55

def segment(frame, cfg):
    roi, (ox, oy) = crop_roi(frame, cfg.get("top_roi"))
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # 自动估计背景：取边缘像素的中位数，避免把浅色托盘整体当成一个物体。
    border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
    bg_gray = float(np.median(border)) if border.size else float(np.median(gray))
    diff = cv2.absdiff(gray, np.full_like(gray, int(bg_gray)))
    bg_path = cfg.get("empty_background")
    if bg_path:
        bg = None
        try:
            bg = cv2.imread(str(bg_path), cv2.IMREAD_GRAYSCALE)
        except Exception:
            bg = None
        if bg is not None and bg.shape == gray.shape:
            diff = cv2.absdiff(gray, bg)
    # 彩色物料优先用饱和度分离；灰黑物料用与边缘背景的亮度差分离。
    color_mask = ((hsv[:, :, 1] > 45) & (hsv[:, :, 2] > 35)).astype(np.uint8) * 255
    diff_mask = (diff > max(24, float(cfg.get("background_diff", 26)))).astype(np.uint8) * 255
    diff_mask = cv2.bitwise_and(diff_mask, (hsv[:, :, 1] < 55).astype(np.uint8) * 255)
    mask = cv2.bitwise_or(color_mask, diff_mask)
    # 明亮托盘上，低饱和度的底板差分容易形成大片区域，剔除靠边的大背景连通域。
    if cfg.get("tray_background") == "dark":
        _, legacy = cv2.threshold(gray, 45, 255, cv2.THRESH_BINARY)
        mask = cv2.bitwise_and(mask, legacy)
    # 删除与图像边缘连通的背景区域，避免托盘底板成为一个大轮廓。
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    clean = np.zeros_like(mask)
    hh, ww = mask.shape[:2]
    for label in range(1, n):
        x0, y0, cw, ch, a0 = stats[label]
        touches = x0 <= 0 or y0 <= 0 or x0 + cw >= ww or y0 + ch >= hh
        if touches and a0 > ww * hh * 0.01:
            continue
        clean[labels == label] = 255
    mask = clean
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5,5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        area = cv2.contourArea(c)
        if not (cfg["min_area_px"] <= area <= cfg["max_area_px"]):
            continue
        x, y, w, h = cv2.boundingRect(c)
        if w < 8 or h < 8: continue
        # 去掉贴着识别图边缘的背景连通域；真实货物通常完整位于四点区域内部。
        border_margin = int(cfg.get("border_margin_px", 3))
        if x <= border_margin or y <= border_margin or x+w >= roi.shape[1]-border_margin or y+h >= roi.shape[0]-border_margin:
            if area > roi.shape[0] * roi.shape[1] * 0.08:
                continue
        M = cv2.moments(c)
        cx = (M["m10"] / M["m00"] if M["m00"] else x+w/2) + ox
        cy = (M["m01"] / M["m00"] if M["m00"] else y+h/2) + oy
        local = roi[max(y,0):y+h, max(x,0):x+w]
        color, cc = _color(local)
        shape, sc = _shape(c)
        box = cv2.minAreaRect(c)
        angle = float(box[2])
        if box[1][0] < box[1][1]: angle += 90
        out.append({"center_px": [round(cx, 2), round(cy, 2)], "bbox": [x+ox,y+oy,w,h],
                    "area_px": round(area, 1), "color": color, "shape": shape,
                    "angle_deg": round(angle, 2), "confidence": round((cc+sc)/2, 3),
                    "needs_review": cc < .65 or sc < .65})
    return sorted(out, key=lambda x: (x["center_px"][1], x["center_px"][0]))

def vote(detections, class_map=None):
    if not detections: return []
    # 当前版本按稳定的目标序号投票；托盘静止时适用，避免单帧抖动改变类别。
    n = min(len(x) for x in detections)
    result = []
    for i in range(n):
        rows = [x[i] for x in detections]
        row = dict(rows[-1])
        row["color"] = Counter(x["color"] for x in rows).most_common(1)[0][0]
        row["shape"] = Counter(x["shape"] for x in rows).most_common(1)[0][0]
        row["confidence"] = round(float(np.mean([x["confidence"] for x in rows])), 3)
        row["needs_review"] = row["confidence"] < .68
        row["class_key"] = f"{row['shape']}|{row['color']}"
        result.append(row)
    # 类别由稳定的“形状+颜色”键确定，最多六类；不依赖大模型猜测。
    key_to_id = dict(class_map or {})
    for key in sorted({x["class_key"] for x in result}):
        if key not in key_to_id and len(key_to_id) < 6:
            key_to_id[key] = str(len(key_to_id) + 1)
    for row in result:
        cid = key_to_id.get(row["class_key"])
        row["class_id"] = cid
        row["class_name"] = DEFAULT_CLASS_NAMES.get(cid, "未分类") if cid else "未分类"
        if cid is None:
            row["needs_review"] = True
            row["confidence"] = min(row["confidence"], 0.40)
    return result
