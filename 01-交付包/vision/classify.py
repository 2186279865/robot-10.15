"""规则特征 + 现场样本登记分类器（不训练模型）。

思路（与 new_eye/vision.py 的旧规则一脉相承，但基于 SAM 掩膜）：
1. 颜色：掩膜内部腐蚀后取 HSV 统计，避开高光/阴影/托盘边，得到颜色名与纯度；
2. 形状：校正后掩膜轮廓的圆度、多边形顶点数、长宽比、实心度；
   圆形顶面若副摄侧影顶部明显收窄 → 判为“球”，否则“圆柱/圆形”；
3. 类别 = "形状|颜色" 键 → class_map / 现场登记样本；
   未知组合先在已登记样本里做余弦最近邻，仍不可信就标“待登记”，不硬猜。

现场登记：GUI 里点“登记样本”再点目标，输入类别编号/名称，特征存 samples.json，
同时把 "形状|颜色" 写进 config.json 的 class_map，重启后规则映射仍然有效。
"""
import json
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / 'samples.json'

# 与 vision.py / config.class_map 对齐的颜色桶
COLOR_NAMES = ['红', '黄/橙', '绿', '蓝/浅蓝', '紫/其他', '黑', '浅色/白']


# --------------------------------------------------------------------- 样本库
def load_samples(path=None):
    p = Path(path or SAMPLES)
    if not p.exists():
        return {'classes': {}}
    try:
        data = json.loads(p.read_text(encoding='utf8'))
        if isinstance(data, dict) and isinstance(data.get('classes'), dict):
            return data
    except Exception:
        pass
    return {'classes': {}}


def save_samples(data, path=None):
    p = Path(path or SAMPLES)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
    tmp.replace(p)


# --------------------------------------------------------------------- 颜色
def _color_stats(warp, mask):
    """返回 (颜色名, 纯度置信度, HSV 直方图特征)。"""
    h, w = mask.shape[:2]
    k = max(3, int(min(h, w) * .12))
    k += 1 - k % 2
    inner = cv2.erode(mask, np.ones((k, k), np.uint8))
    if cv2.countNonZero(inner) < 200:
        inner = mask
    hsv = cv2.cvtColor(warp, cv2.COLOR_BGR2HSV)
    p = hsv[inner > 0]
    if p.size == 0:
        return '未知', 0.0, np.zeros(12, np.float32)
    hue = p[:, 0].astype(np.float32)
    sat = p[:, 1].astype(np.float32)
    val = p[:, 2].astype(np.float32)
    # 高光(V≈255)和暗边(V 很低)不代表物体本色，统计时剔除
    keep = (val > 35) & (val < 250)
    if keep.sum() < 40:
        keep = np.ones_like(keep, dtype=bool)
    hs, ss, vs = hue[keep], sat[keep], val[keep]
    med_s, med_v = float(np.median(ss)), float(np.median(vs))
    colorful = ss > 40
    med_h = float(np.median(hs[colorful])) if colorful.sum() >= 25 else float(np.median(hs))
    # 颜色判定
    if med_v < 55:
        return '黑', min(.95, .55 + .4 * (55 - med_v) / 55), _hist(hs, ss, vs)
    if med_s < 32 and med_v > 165:
        return '浅色/白', min(.8, .5 + .3 * (med_v - 165) / 90), _hist(hs, ss, vs)
    if med_h < 10 or med_h >= 168:
        name, lo, hi = '红', 168, 180
        name2, lo2, hi2 = '红', 0, 10
    elif med_h < 34:
        name, lo, hi = '黄/橙', 10, 34
        name2, lo2, hi2 = '', 0, 0
    elif med_h < 85:
        name, lo, hi = '绿', 34, 85
        name2, lo2, hi2 = '', 0, 0
    elif med_h < 128:
        name, lo, hi = '蓝/浅蓝', 85, 128
        name2, lo2, hi2 = '', 0, 0
    else:
        name, lo, hi = '紫/其他', 128, 168
        name2, lo2, hi2 = '', 0, 0
    sel = (hs >= lo) & (hs < hi)
    if name2:
        sel = sel | ((hs >= lo2) & (hs < hi2))
    purity = float(sel.mean()) if hs.size else 0.0
    return name, min(.95, .5 + .5 * purity), _hist(hs, ss, vs)


def _hist(hs, ss, vs):
    hh, _ = np.histogram(hs, bins=12, range=(0, 180))
    sh, _ = np.histogram(ss, bins=4, range=(0, 256))
    vh, _ = np.histogram(vs, bins=4, range=(0, 256))
    hh = hh / max(1, hh.sum())
    sh = sh / max(1, sh.sum())
    vh = vh / max(1, vh.sum())
    return np.concatenate([hh, sh, vh]).astype(np.float32)


# --------------------------------------------------------------------- 形状

def _sharp_corners(contour, peri):
    """数"真正的角"：精细近似后，内角 < 130° 的顶点个数。
    圆/椭圆/圆柱侧影的转折是钝的（≈180°），不会计数；多边形会。"""
    try:
        fine = cv2.approxPolyDP(contour, .02 * max(1.0, peri), True).reshape(-1, 2)
        n = len(fine)
        if n < 3:
            return 0
        cnt = 0
        for i in range(n):
            a = fine[i - 1].astype(np.float64)
            b = fine[i].astype(np.float64)
            c = fine[(i + 1) % n].astype(np.float64)
            v1, v2 = b - a, c - b
            n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
            if n1 < 1e-6 or n2 < 1e-6:
                continue
            cosv = float(np.dot(v1, v2) / (n1 * n2))
            turn = float(np.degrees(np.arccos(max(-1.0, min(1.0, cosv)))))
            # v1/v2 是相邻两条边的方向 → turn 是"转向角"：多边形大，圆弧小
            if turn > 55.0:
                cnt += 1
        return cnt
    except Exception:                              # noqa: BLE001
        return 0


def _shape_of(mask, contour):
    area = float(cv2.contourArea(contour))
    peri = float(cv2.arcLength(contour, True))
    if area <= 0 or peri <= 0:
        return '未知', 0.0, {'circ': 0.0, 'ratio': 1.0, 'nv': 0, 'solidity': 0.0, 'extent': 0.0, 'aspect': 1.0}
    circ = 4 * np.pi * area / (peri * peri)
    approx = cv2.approxPolyDP(contour, .035 * peri, True)
    nv = len(approx)
    x, y, w, h = cv2.boundingRect(contour)
    ratio = max(w, h) / max(1, min(w, h))
    hull = cv2.convexHull(contour)
    solidity = area / max(1.0, float(cv2.contourArea(hull)))
    extent = area / max(1.0, float(w * h))
    aspect = h / max(1.0, float(w))
    # ---- 旋转不敏感的最小外接矩形 ----
    try:
        (_rc, (rw, rh), _ra) = cv2.minAreaRect(contour)
        rw, rh = float(rw), float(rh)
        if rw < rh:
            rw, rh = rh, rw
        rect_ratio = rw / max(1.0, rh)
        rect_fill = area / max(1.0, rw * rh)
    except Exception:                                   # noqa: BLE001
        rect_ratio, rect_fill = 1.0, 0.0
    info = {'circ': round(circ, 3), 'ratio': round(ratio, 3), 'nv': int(nv),
            'solidity': round(solidity, 3), 'extent': round(extent, 3), 'aspect': round(aspect, 3),
            'rect_ratio': round(rect_ratio, 3), 'rect_fill': round(rect_fill, 3),
            'rw': round(rw, 1), 'rh': round(rh, 1)}

    # 顶点数用更紧的 epsilon（0.03 周长）重算：旋转不敏感，能分开 4/5/6/8+
    nv = len(cv2.approxPolyDP(contour, .03 * peri, True))
    info['nv'] = int(nv)
    # ---- 角点锐度：真正的多边形才有点数，圆/椭圆/圆柱侧影的"角"是钝的 ----
    sharp = _sharp_corners(contour, peri)
    info['sharp'] = int(sharp)

    # ① 方形族：4 个锐角 + 几乎填满最小外接矩形（旋转 45° 也≈1.0）
    if sharp == 4 and rect_fill >= .93 and solidity >= .90:
        return ('正方体/正方形' if rect_ratio < 1.16 else '长方体/长方形'), .84, info
    # ② 菱体：4 个锐角但没填满外接矩形（菱形 rect_fill≈.78~.85，旋转正方形≈1.0）
    if sharp == 4 and .62 <= rect_fill < .93 and solidity >= .90:
        return '菱体/菱形', .80, info
    # ③ 三角形
    if sharp == 3:
        return '三棱体/三角形', .82, info
    # ④ 五棱
    if sharp == 5:
        return '五棱体', .78, info
    # ⑤ 圆/椭圆：几乎没有锐角（圆柱侧影=圆角矩形也走这里）
    if sharp <= 2 and rect_fill <= .93 and circ >= .74:
        return '圆柱/圆形', min(.95, .58 + .40 * min(1.0, max(0.0, circ - .74) / .20)), info
    if sharp <= 2 and rect_fill <= .93:
        return '圆柱/圆形', .62, info
    # ⑥ 六边形：可能是六棱柱，也可能是"立方体整体侧影"（顶面+前面=6 角）
    #    后者 rect_fill 更高；由 shape_of_item 用顶面复核
    if sharp == 6 or nv == 6:
        if rect_fill >= .85 and solidity >= .93:
            return '六棱体/多棱', .55, info      # 低置信 → 走顶面复核
        return '六棱体/多棱', .78, info
    # ⑦ 兜底
    return '其他几何体', .55, info


def _side_curvature(mask, contour):
    """副摄侧影顶部收窄程度：圆顶（球）<0.78，平顶（柱体）≈1。"""
    x, y, w, h = cv2.boundingRect(contour)
    if h < 20 or w < 10:
        return None
    prof = []
    for f in (.12, .22, .32, .55, .75):
        row = min(h - 1, int(h * f))
        seg = mask[y + row:y + row + 1, x:x + w]
        prof.append(float((seg > 0).mean()) if seg.size else 0.0)
    mid = max(prof[3], prof[4], 1e-6)
    return float(min(prof[0], prof[1], prof[2]) / mid)


# --------------------------------------------------------------------- 特征向量
def _feature(mask, hist, shape_info, side_curv):
    m = mask
    hu = cv2.HuMoments(cv2.moments(m)).flatten().astype(np.float32)
    hu = -np.sign(hu) * np.log10(np.abs(hu) + 1e-12)
    vec = np.concatenate([hist,
                          hu / 10.0,
                          np.float32([shape_info['circ'], shape_info['ratio'],
                                      shape_info['nv'] / 6.0, shape_info['solidity'],
                                      shape_info['extent'],
                                      -1.0 if side_curv is None else side_curv])])
    n = float(np.linalg.norm(vec))
    return (vec / max(1e-9, n)).astype(np.float32)



# ---------------------------------------------------------------- 顶面提取
SAVE_SPLIT = True           # 调试期打开：每次判形状落一张 dbg_split_last.jpg（远程诊断用）
_DBG_DIR = None


def _dbg_dump(tag, warp, mask, extra=None):
    """把当前 warp + mask（+ 顶面区域）拼一张图存盘，供远程诊断。"""
    global _DBG_DIR
    if not SAVE_SPLIT:
        return
    try:
        import os
        from pathlib import Path as _P
        if _DBG_DIR is None:
            _DBG_DIR = _P(__file__).resolve().parent / 'runtime'
        _DBG_DIR.mkdir(parents=True, exist_ok=True)
        vis = warp.copy() if warp is not None else np.zeros((700, 700, 3), np.uint8)
        if mask is not None:
            over = vis.copy()
            over[mask > 0] = (0, 255, 0)
            vis = cv2.addWeighted(vis, 0.65, over, 0.35, 0)
        if extra is not None and extra[0] is not None:
            tm = extra[0]
            o2 = vis.copy()
            o2[tm > 0] = (0, 200, 255)
            vis = cv2.addWeighted(vis, 0.55, o2, 0.45, 0)
        for i, line in enumerate((tag or '').split('|')):
            if line:
                cv2.putText(vis, line, (8, 24 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX,
                            .6, (255, 255, 255), 2)
        cv2.imwrite(str(_DBG_DIR / 'dbg_split_last.jpg'), vis)
    except Exception:
        pass



def _clean_mask(mask):
    """去毛刺：只保留最大连通域，开运算 3x3 + 闭运算 5x5。（形状判据前统一调用）"""
    try:
        m = (mask > 0).astype(np.uint8)
        if int(m.sum()) < 100:
            return mask
        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        if n <= 1:
            return mask
        idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        keep = (lab == idx).astype(np.uint8) * 255
        keep = cv2.morphologyEx(keep, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        keep = cv2.morphologyEx(keep, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        if int((keep > 0).sum()) < 100:            # 清没了就退回原掩膜
            return mask
        return keep
    except Exception:                              # noqa: BLE001
        return mask


def _split_top_face(warp, mask):
    """把一块物体的掩膜按亮度/位置拆成"顶面"和"侧面"。

    物理依据：相机是 60° 俯瞰，块状物体在画面里呈"顶面（上面）+ 侧面（下面）"
    两块；顶面朝向光源通常更亮，而且一定在**上方**。整块轮廓是六边形，
    直接判形状会把正方体判成六棱柱，所以先取顶面再判。

    返回 (top_mask, top_contour) 或 None（拆不出来）。
    """
    try:
        if warp is None or mask is None:
            return None
        sel = mask > 0
        if sel.sum() < 300:
            return None
        gray = cv2.cvtColor(warp, cv2.COLOR_BGR2GRAY) if warp.ndim == 3 else warp
        vals = gray[sel]
        if vals.size < 300:
            return None
        # 用"掩膜内亮度中值"当分界（Otsu 在只有两级亮度时会退化成边界值，
        # 导致一侧一个像素都没有）；顶面/侧面亮度差通常 > 10。
        lo_v, hi_v = float(np.percentile(vals, 10)), float(np.percentile(vals, 90))
        thr = 0.5 * (lo_v + hi_v)
        if hi_v - lo_v < 8:                       # 亮度几乎均匀 → 看不清面
            return None
        hi = np.logical_and(sel, gray > thr)
        lo = np.logical_and(sel, gray <= thr)
        if hi.sum() < 200 or lo.sum() < 200:
            return None
        ys, _xs = np.nonzero(sel)
        yy = np.arange(gray.shape[0])[:, None]
        hi_y = float(np.average(np.broadcast_to(yy, gray.shape)[hi]))
        lo_y = float(np.average(np.broadcast_to(yy, gray.shape)[lo]))
        cand = hi if hi_y < lo_y else lo          # 顶面一定在上方
        frac = cand.sum() / max(1.0, sel.sum())
        if frac < .28 or frac > .85:              # 太小=反光；太大=把整块都算进来了
            return None
        m = (cand.astype(np.uint8)) * 255
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cs:
            return None
        c = max(cs, key=cv2.contourArea)
        if cv2.contourArea(c) < 200:
            return None
        _dbg_dump('thr=%.0f hi_y=%.0f lo_y=%.0f use=%s hi_n=%d lo_n=%d'
                  % (thr, hi_y, lo_y, 'hi' if cand is hi else 'lo',
                     int(hi.sum()), int(lo.sum())), warp, mask, (m, c))
        return m, c
    except Exception:                              # noqa: BLE001
        return None


# 60° 俯瞰下，顶面在图像里沿"纵深"方向被压缩约 sin60≈0.87，
# 所以同一个正方体的顶面长宽比会量成 ~1.15；长方形会 >=1.4。用 1.30 分界。
SQUARE_RATIO_MAX = 1.30


def shape_of_item(warp, mask, contour):
    """整块轮廓判一次；非"方正"时再取顶面重判，取置信度更高者。"""
    mask = _clean_mask(mask)
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if cs:
        contour = max(cs, key=cv2.contourArea)
    shape, conf, info = _shape_of(mask, contour)
    solid_ok = (info.get('rect_fill', 0) >= .88 and info.get('nv') == 4)
    if solid_ok:
        return shape, conf, info
    tf = _split_top_face(warp, mask)
    if tf is not None:
        tmask, tcont = tf
        _c0 = cv2.contourArea(tcont)
        if _c0 >= .22 * max(1.0, cv2.contourArea(contour)):
            s2, c2, i2 = _shape_of(tmask, tcont)
            i2 = dict(i2)
            i2['top_face'] = True
            if i2.get('nv') == 4 and i2.get('rect_fill', 0) >= .85:
                # 顶面是四边形 → 用长宽比判 正方/长方（阈值见上）
                s2 = ('正方体/正方形' if i2['rect_ratio'] < SQUARE_RATIO_MAX
                      else '长方体/长方形')
                c2 = .84
            if c2 >= conf:
                return s2, c2, i2
    return shape, conf, info


# --------------------------------------------------------------------- 分类
def _class_name(cid, cfg, samples):
    cls = ((samples or {}).get('classes') or {}).get(str(cid)) or {}
    if cls.get('name'):
        return cls['name']
    names = (cfg or {}).get('class_names') or {}
    return names.get(str(cid)) or (f'类别{cid}' if str(cid).isdigit() else str(cid))


def _result(cid, name, key, conf, source, cfg):
    return {'class_id': str(cid), 'class_name': name, 'class_key': key,
            'class_conf': round(float(conf), 3), 'class_source': source,
            'need_review': False}


def classify_item(warp, item, cfg=None, samples=None, side_warp=None, side_item=None, side_only=False):
    """给一个检测项补全颜色/形状/类别字段，返回原 item（原地更新）。"""
    cfg = cfg or {}
    mask = item.get('mask')
    contour = item.get('contour')
    if mask is None or contour is None:
        item.setdefault('class_name', '未知')
        item['need_review'] = True
        return item
    if side_only:
        # 副摄单独看到的目标：颜色可用，形状/类别不可靠，交给顶视确认。
        color, color_conf, hist = _color_stats(warp, mask)
        key = f'?|{color}'
        item.update({'color': color, 'color_conf': round(color_conf, 2), 'shape': None,
                     'shape_conf': 0.0, 'class_id': None, 'class_name': '副摄待确认',
                     'class_key': key, 'class_conf': 0.0, 'class_source': 'side-only',
                     'need_review': True, 'feature': None})
        return item
    color, color_conf, hist = _color_stats(warp, mask)
    shape, shape_conf, info = shape_of_item(warp, mask, contour)
    side_curv = None
    if (side_item is not None and side_item.get('mask') is not None
            and cfg.get('side_shape_refine', True) and shape in ('圆柱/圆形', '其他几何体')):
        side_curv = _side_curvature(side_item['mask'], side_item['contour'])
        if side_curv is not None and shape == '圆柱/圆形':
            if side_curv < .78:
                shape, shape_conf = '球', max(.6, shape_conf)
            else:
                shape_conf = max(shape_conf, .62)
    key = f'{shape}|{color}'
    feat = _feature(mask, hist, info, side_curv)
    cls = _lookup(key, shape, color, cfg, samples, feat)
    item.update({'color': color, 'color_conf': round(color_conf, 2),
                 'shape': shape, 'shape_conf': round(shape_conf, 2),
                 'side_curv': None if side_curv is None else round(side_curv, 2),
                 'feature': feat, **cls})
    return item


def _lookup(key, shape, color, cfg, samples, feat):
    cmap = cfg.get('class_map') or {}
    if key in cmap:
        cid = str(cmap[key])
        return _result(cid, _class_name(cid, cfg, samples), key, .95, 'rule', cfg)
    classes = (samples or {}).get('classes') or {}
    for cid, c in classes.items():
        if key in (c.get('keys') or []):
            return _result(cid, _class_name(cid, cfg, samples), key, .9, 'sample-key', cfg)
    # 没有特征向量就没法做近邻匹配：直接判"待登记"，不要拿 None 去算范数
    # （副摄单独看到的目标 feature 就是 None，之前会把整次识别打成 TypeError）
    if feat is None:
        return {'class_id': None, 'class_name': '待登记', 'class_key': key,
                'class_conf': 0.0, 'class_source': 'unknown', 'need_review': True}
    feat = np.asarray(feat, dtype=np.float32).reshape(-1)
    if feat.size == 0:
        return {'class_id': None, 'class_name': '待登记', 'class_key': key,
                'class_conf': 0.0, 'class_source': 'unknown', 'need_review': True}
    best = None
    fn = float(np.linalg.norm(feat)) + 1e-9
    for cid, c in classes.items():
        for s in c.get('samples') or []:
            v = np.asarray(s.get('feature') or [], dtype=np.float32)
            if v.size != feat.size:
                continue
            sim = float(np.dot(v, feat) / (np.linalg.norm(v) * fn))
            if best is None or sim > best[0]:
                best = (sim, cid)
    thr = float(cfg.get('sample_similarity', .88))
    if best is not None and best[0] >= thr:
        return _result(best[1], _class_name(best[1], cfg, samples), key,
                       min(.9, best[0]), 'nearest', cfg)
    return {'class_id': None, 'class_name': '待登记', 'class_key': key,
            'class_conf': 0.0, 'class_source': 'unknown', 'need_review': True}


# --------------------------------------------------------------------- 登记
def register(item, class_id, class_name=None, cfg=None, samples=None, path=None):
    """把当前目标的特征登记为某类别，并记忆 "形状|颜色" 规则。"""
    samples = samples if samples is not None else load_samples(path)
    cid = str(class_id).strip()
    if not cid:
        return None
    classes = samples.setdefault('classes', {})
    c = classes.setdefault(cid, {'name': class_name or _class_name(cid, cfg or {}, samples),
                                 'keys': [], 'samples': []})
    if class_name:
        c['name'] = class_name
    key = item.get('class_key') or f"{item.get('shape') or '?'}|{item.get('color') or '?'}"
    if key not in c['keys']:
        c['keys'].append(key)
    feat = item.get('feature')
    if feat is not None:
        arr = np.asarray(feat, dtype=np.float32).tolist()
        fn = float(np.linalg.norm(np.asarray(arr, np.float32))) + 1e-9
        dup = False
        for s in c['samples']:
            v = np.asarray(s.get('feature') or [], np.float32)
            if v.size == len(arr) and float(np.dot(v, np.asarray(arr, np.float32)) / (np.linalg.norm(v) * fn + 1e-9)) > .985:
                dup = True
                break
        if not dup:
            c['samples'].append({'feature': arr, 'shape': item.get('shape'),
                                 'color': item.get('color'),
                                 'ts': time.strftime('%Y-%m-%d %H:%M:%S')})
    save_samples(samples, path)
    if cfg is not None:
        cmap = cfg.setdefault('class_map', {})
        cmap[key] = cid
        cfg['class_names'] = cfg.get('class_names') or {}
        cfg['class_names'][cid] = c['name']
    item.update({'class_id': cid, 'class_name': c['name'], 'class_key': key,
                 'class_conf': 1.0, 'class_source': 'registered', 'need_review': False})
    return c


def class_summary(fused):
    """统计各类别数量，给状态栏/JSON 用。"""
    out = {}
    for f in fused:
        it = f.get('top') or f.get('side') or {}
        name = it.get('class_name') or '未知'
        out[name] = out.get(name, 0) + 1
    return out
