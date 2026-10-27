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
    info = {'circ': round(circ, 3), 'ratio': round(ratio, 3), 'nv': int(nv),
            'solidity': round(solidity, 3), 'extent': round(extent, 3), 'aspect': round(aspect, 3)}
    if nv == 3:
        return '三棱体/三角形', .82, info
    if nv == 4:
        return ('正方体/正方形' if ratio < 1.25 else '长方体/长方形'), .80, info
    if nv == 5:
        return '五棱体', .78, info
    # 圆/椭圆顶面：透视会让圆变成椭圆，所以不能只靠长宽比；
    # 圆度>0.80 直接判圆，圆度稍低时用"顶点数≥6 + 实心度高 + 面积占比<0.84"兜底
    # （六棱柱有被误判成圆柱的风险，现场登记时核对一下）。
    if (circ > .80 and solidity > .93) or (nv >= 6 and circ > .74 and solidity > .93 and extent < .84):
        return '圆柱/圆形', min(.95, .55 + .4 * min(1.0, max(0.0, circ - .74) / .22)), info
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
    shape, shape_conf, info = _shape_of(mask, contour)
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
