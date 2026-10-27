"""形状几何基元特征 + 容错链判定（与 GUI/相机解耦，可单测）。

为什么单独一层：
  旧实现把"像素长宽比 1.25"当正方体/长方体分界，但相机有俯角 → 同一物块在
  托盘不同位置比例能差 10~20%，边缘处必错。这里改成：
    1) 先在托盘坐标（700×700）里把像素换成毫米（用四点框 + 托盘实际尺寸）；
    2) 用**与位置/尺度无关**的几何基元特征（圆度/实心度/矩形填充率/顶点数）；
    3) 判定走"容错链"，每级阈值都写在配置里，现场可调；
    4) 认不出就返回「其他几何体」，绝不硬猜成某个具体类别。

对外只依赖 cv2 / numpy：
    feats = features(mask, contour, mm_per_px=0.28)
    name, conf, why = classify_shape(feats, side_curv=None)
"""
import math

import cv2
import numpy as np

#: 默认阈值（可被 cfg['shape_cfg'] 覆盖）
DEFAULTS = {
    'nv_eps': 0.02,          # approxPolyDP 的相对精度（× 周长）
    'min_area_px': 400.0,    # 小于这个面积直接不判
    'circ_round': 0.85,      # 圆度 ≥ 此值 → 圆形
    'solidity_round': 0.93,  # 圆形还要求实心度
    'rect_fit': 0.95,        # 最小外接矩形填充率 ≥ → 方形/长条形
    'solidity_rect': 0.97,
    'cube_tol': 0.12,        # 边长差 ≤ 12% → 正方体
    'ratio_long': 1.30,      # 毫米长宽比 ≥ → 长方体
    'sphere_curv': 0.80,     # 副摄顶部收窄率 < → 球
    'tray_mm': 200.0,        # 托盘内框边长（mm），用于 px→mm；700px 对应该值
}

#: 形状名（与旧版保持一致，便于 class_key/class_map 复用）
SHAPE_TRI = '三棱体/三角形'
SHAPE_PENTA = '五棱体'
SHAPE_PRISM = '六棱体/多棱'
SHAPE_ROUND = '圆柱/圆形'
SHAPE_SPHERE = '球'
SHAPE_CUBE = '正方体/正方形'
SHAPE_BOX = '长方体/长方形'
SHAPE_OTHER = '其他几何体'


def cfg_of(cfg):
    """合并用户配置。"""
    out = dict(DEFAULTS)
    out.update((cfg or {}).get('shape_cfg') or {})
    return out


def _fill_holes(mask):
    """把掩膜内部的黑洞填上（反光/贴纸造成的缺口）。"""
    m = (mask > 0).astype(np.uint8) * 255
    h, w = m.shape[:2]
    ff = m.copy()
    cv2.floodFill(ff, np.zeros((h + 2, w + 2), np.uint8), (0, 0), 255)
    holes = cv2.bitwise_not(ff)
    return cv2.bitwise_or(m, holes)


def clean_mask(mask, close_ksize=3):
    """分割后处理：闭运算 + 填洞。返回 uint8 0/255。"""
    if mask is None:
        return None
    m = (mask > 0).astype(np.uint8) * 255
    if close_ksize and close_ksize >= 3:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_ksize, close_ksize))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _fill_holes(m)


def features(mask, contour, mm_per_px=1.0, cfg=None):
    """算出一组几何基元特征（全部与位置无关）。"""
    c = cfg_of(cfg)
    out = {
        'area_px': 0.0, 'circ': 0.0, 'solidity': 0.0, 'extent': 0.0,
        'rect_fit': 0.0, 'nv': 0, 'w_mm': 0.0, 'h_mm': 0.0,
        'ratio_mm': 1.0, 'hu': [0.0, 0.0, 0.0],
    }
    if mask is None or contour is None:
        return out
    m = (mask > 0).astype(np.uint8) * 255
    area = float(cv2.contourArea(contour))
    peri = float(cv2.arcLength(contour, True))
    if area <= 0:
        return out
    x, y, w, h = cv2.boundingRect(contour)
    hull = cv2.convexHull(contour)
    hull_area = float(cv2.contourArea(hull))
    rect = cv2.minAreaRect(contour)
    (rw, rh) = rect[1]
    rect_area = float(max(1.0, rw * rh))
    approx = cv2.approxPolyDP(contour, c['nv_eps'] * max(1.0, peri), True)

    side_mm = {}
    try:
        side_mm[0] = float(w) * mm_per_px
        side_mm[1] = float(h) * mm_per_px
        long_mm, short_mm = max(side_mm.values()), max(1e-6, min(side_mm.values()))
    except Exception:                                  # noqa: BLE001
        long_mm, short_mm = 1.0, 1.0

    hu_raw = cv2.HuMoments(cv2.moments(m)).flatten()
    hu_list = []
    for value in hu_raw[:3]:
        v = float(value)
        if abs(v) < 1e-12:
            hu_list.append(0.0)
        else:
            hu_list.append(-math.copysign(1.0, v) * math.log10(abs(v)) / 10.0)

    out.update({
        'area_px': round(area, 1),
        'circ': round(4.0 * math.pi * area / max(1e-9, peri * peri), 3),
        'solidity': round(area / max(1e-9, hull_area), 3),
        'extent': round(area / max(1.0, float(w * h)), 3),
        'rect_fit': round(area / rect_area, 3),
        'nv': int(len(approx)),
        'w_mm': round(float(w) * mm_per_px, 1),
        'h_mm': round(float(h) * mm_per_px, 1),
        'ratio_mm': round(long_mm / short_mm, 3),
        'hu': [round(x, 3) for x in hu_list],
    })
    return out


def classify_shape(feats, side_curv=None, cfg=None):
    """容错链判定，返回 (形状名, 置信度, 判据说明)。"""
    c = cfg_of(cfg)
    f = feats or {}
    if float(f.get('area_px') or 0) < float(c['min_area_px']):
        return SHAPE_OTHER, 0.30, ['面积过小']
    nv = int(f.get('nv') or 0)
    circ = float(f.get('circ') or 0)
    solid = float(f.get('solidity') or 0)
    rect_fit = float(f.get('rect_fit') or 0)
    ratio_mm = float(f.get('ratio_mm') or 1.0)
    w_mm = float(f.get('w_mm') or 0)
    h_mm = float(f.get('h_mm') or 0)
    why = []

    if nv == 3:
        why.append('顶点=3')
        return SHAPE_TRI, 0.85, why
    if nv == 5 and solid >= 0.90:
        why.append('顶点=5')
        return SHAPE_PENTA, 0.82, why

    # 先判圆：圆度够高就是圆（球/圆柱），不要被"顶点数=8"骗成多棱柱
    if circ >= float(c['circ_round']) and solid >= float(c['solidity_round']):
        why.append('圆度 %.2f' % circ)
        if side_curv is not None and float(side_curv) < float(c['sphere_curv']):
            why.append('副摄顶部收窄 %.2f<%.2f' % (side_curv, c['sphere_curv']))
            return SHAPE_SPHERE, 0.88, why
        return SHAPE_ROUND, min(0.95, 0.60 + 0.35 * min(1.0, circ)), why

    # 圆度不够但顶点多、实心度高 → 六棱/多棱柱
    if nv >= 6 and solid >= 0.90:
        why.append('顶点=%d 圆度 %.2f（不足圆）' % (nv, circ))
        return SHAPE_PRISM, 0.78, why

    if rect_fit >= float(c['rect_fit']) and solid >= float(c['solidity_rect']):
        diff = abs(w_mm - h_mm) / max(1e-6, max(w_mm, h_mm))
        why.append('矩形填充 %.2f，边长 %.0f/%.0fmm 差 %.0f%%'
                   % (rect_fit, w_mm, h_mm, diff * 100))
        if diff <= float(c['cube_tol']):
            return SHAPE_CUBE, 0.85, why
        if ratio_mm >= float(c['ratio_long']):
            return SHAPE_BOX, 0.85, why
        return SHAPE_BOX, 0.70, why

    why.append('未命中任何基元（圆度 %.2f 实心度 %.2f 矩形填充 %.2f 顶点 %d）'
               % (circ, solid, rect_fit, nv))
    return SHAPE_OTHER, 0.45, why


def describe(feats):
    """一行文字，方便界面/日志里看判据。"""
    f = feats or {}
    return ('circ=%.2f solid=%.2f rectfit=%.2f nv=%s %.0fx%.0fmm (%.2f:1)'
            % (f.get('circ') or 0, f.get('solidity') or 0, f.get('rect_fit') or 0,
               f.get('nv') or 0, f.get('w_mm') or 0, f.get('h_mm') or 0,
               f.get('ratio_mm') or 1.0))
