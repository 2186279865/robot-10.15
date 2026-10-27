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
    'rhombus_rect_min': 0.55,  # 菱形的最小外接矩形填充率下限
    'rhombus_rect_max': 0.92,
    'rhombus_extent_max': 0.68,  # 菱形 bbox 填充上限（五棱≈0.69、圆/六棱≈0.77 不会被吃到）  # 上限（方形/长方形 rect_fit≈1.0，不会被误判）
    'prism_rect_min': 0.72,      # 多棱柱的最小外接矩形填充率下限
    'prism_extent_min': 0.66,    # 多棱柱的 bbox 填充率下限（排除菱形/三角形）
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
SHAPE_RHOMBUS = '菱体/菱形'
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


def features(mask, contour, mm_per_px=1.0, cfg=None, depth_scale=1.0):
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
        'h_mm': round(float(h) * mm_per_px * depth_scale, 1),   # 深度方向按 k_front 修正
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

    # 菱形顶面：bbox 填充率只有约 0.5（方形=1.0、圆=0.79、五棱≈0.69、六棱≈0.75），
    # 且最小外接矩形填充率也低。SAM 边缘毛刺常把顶点数算成 6~8，所以这里**不依赖 nv**
    # （只要 nv>=4，排除三角形）。
    extent = float(f.get('extent') or 0.0)
    # SAM 边缘毛刺会把菱形顶点算成 5~6 个，所以放宽到 nv>=4；
    # 真正的区分靠两条：bbox 填充率低（菱形≈0.5）+ 最小外接矩形也填不满（<0.92）。
    # 旋转 45° 的正方形 rect_fit≈1.0、extent≈0.5，靠 rect_fit 上限排除。
    if (nv >= 4 and solid >= 0.90
            and 0.40 <= rect_fit <= float(c['rhombus_rect_max'])
            and extent <= float(c['rhombus_extent_max'])):
        why.append('bbox 填充 %.2f、最小外接矩形填充 %.2f（菱形/风筝形）' % (extent, rect_fit))
        return SHAPE_RHOMBUS, 0.80, why

    # 先判圆：圆度够高就是圆（球/圆柱），不要被"顶点数=8"骗成多棱柱
    if circ >= float(c['circ_round']) and solid >= float(c['solidity_round']):
        why.append('圆度 %.2f' % circ)
        if side_curv is not None and float(side_curv) < float(c['sphere_curv']):
            why.append('副摄顶部收窄 %.2f<%.2f' % (side_curv, c['sphere_curv']))
            return SHAPE_SPHERE, 0.88, why
        return SHAPE_ROUND, min(0.95, 0.60 + 0.35 * min(1.0, circ)), why

    # 圆度不够但顶点多、实心度高、bbox 填充率也高 → 六棱/多棱柱
    # （多棱柱 extent≈0.75、rect_fit≈0.87；菱形 extent≈0.5 已在上面被拿走）
    if (5 <= nv <= 8 and solid >= 0.93
            and rect_fit >= float(c['prism_rect_min'])
            and extent >= float(c['prism_extent_min'])):
        why.append('顶点=%d 圆度 %.2f bbox 填充 %.2f' % (nv, circ, extent))
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


# ---------------------------------------------------------------- 侧视融合
def side_info_of(side_mask, side_contour, mm_per_px=1.0, k_h=1.0):
    """副摄：量物体高度 mm，并判侧影是"平顶(矩形)"还是"弧顶"。

    返回 {'height_mm': float|None, 'curv': float|None, 'kind': 'rect'|'arc'|None}
    """
    out = {'height_mm': None, 'curv': None, 'kind': None}
    if side_mask is None or side_contour is None:
        return out
    x, y, w, h = cv2.boundingRect(side_contour)
    if h <= 2 or w <= 2:
        return out
    out['height_mm'] = round(float(h) * mm_per_px * k_h, 1)
    curv = _side_curv(side_mask, side_contour)
    out['curv'] = None if curv is None else round(curv, 3)
    if curv is not None:
        out['kind'] = 'arc' if curv < 0.80 else 'rect'
    return out


def _side_curv(mask, contour):
    """侧影顶部相对中部收窄程度：弧顶(球) <0.80，平顶(柱/方) ≈1。"""
    x, y, w, h = cv2.boundingRect(contour)
    if h < 15 or w < 8:
        return None
    prof = []
    for f in (.12, .22, .32, .55, .75):
        row = min(h - 1, int(h * f))
        seg = mask[y + row:y + row + 1, x:x + w]
        prof.append(float((seg > 0).mean()) if seg.size else 0.0)
    mid = max(prof[3], prof[4], 1e-6)
    return float(min(prof[0], prof[1], prof[2]) / mid)


def refine_with_side(shape, conf, feats, side, cfg=None):
    """用副摄的高度/侧影形状做二次判定（球/圆柱、正方体/扁长方体）。"""
    c = cfg_of(cfg)
    if not side or side.get('height_mm') is None:
        return shape, conf
    h_mm = float(side['height_mm'])
    kind = side.get('kind')
    curv = side.get('curv')
    w_mm = float((feats or {}).get('w_mm') or 0.0)
    d_mm = float((feats or {}).get('h_mm') or 0.0)

    if shape == SHAPE_ROUND:
        if kind == 'arc' or (curv is not None and curv < float(c['sphere_curv'])):
            return SHAPE_SPHERE, max(conf, 0.85)
        return SHAPE_ROUND, max(conf, 0.80)

    if shape in (SHAPE_CUBE, SHAPE_BOX) and w_mm > 1:
        if h_mm <= 0.70 * w_mm:
            return SHAPE_BOX, max(conf, 0.80)           # 扁平 → 长方体
        near_w = abs(h_mm - w_mm) <= 0.28 * w_mm
        near_d = d_mm <= 0 or abs(d_mm - w_mm) <= 0.30 * w_mm
        if near_w and near_d:
            return SHAPE_CUBE, max(conf, 0.85)
        return SHAPE_BOX, max(conf, 0.75)

    if shape == SHAPE_RHOMBUS and kind == 'rect':
        return SHAPE_RHOMBUS, max(conf, 0.82)           # 菱柱（有侧面）
    return shape, conf
