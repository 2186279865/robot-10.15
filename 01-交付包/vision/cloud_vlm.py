"""云端视觉复核：调用 DeepSeek（deepseek-flash，支持图像）判断物块形状/颜色。

设计原则：
- 只用标准库 + cv2/numpy，不引入额外依赖；
- 任何异常（断网、超时、无 key、解析失败）一律返回 None，调用方自动退回几何判据；
- 结果按图片指纹缓存 10 s，避免 5 帧投票里重复请求同一张图；
- key 查找顺序：环境变量 DEEPSEEK_API_KEY → vision/deepseek_key.txt → ~/.dsh/.credentials.yaml。

用法：
    import cloud_vlm
    if cloud_vlm.available():
        r = cloud_vlm.ask_shape(crop)         # {'shape','conf','color','reason','ms','raw_shape'}
        objs = cloud_vlm.ask_objects(frame)   # {'objects':[{...}], 'ms': 1234}
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2

API_URL = 'https://api.deepseek.com/chat/completions'
MODELS_URL = 'https://api.deepseek.com/models'
DEFAULT_MODEL = 'deepseek-flash'
DEFAULT_TIMEOUT = 12.0

# 与视觉主流程一致的类别名（含新增的圆锥/棱锥）
SHAPES = [
    '正方体/正方形', '长方体/长方形', '菱体/菱形', '三棱体/三角形',
    '五棱体', '六棱体/多棱', '圆柱/圆形', '球', '圆锥', '棱锥', '其他几何体',
]

_CACHE: Dict[str, tuple] = {}
_CACHE_TTL = 10.0
_AVAILABLE: Dict[str, Any] = {'ts': 0.0, 'ok': None}


# ---------------------------------------------------------------- key / 可用性
def _key_from_credentials() -> str:
    cred = Path.home() / '.dsh' / '.credentials.yaml'
    if not cred.exists():
        return ''
    try:
        text = cred.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return ''
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^(?:-\s*)?([A-Za-z0-9_\-]+)\s*:\s*(.+)$', line)
        if not m:
            continue
        name, val = m.group(1).lower(), m.group(2).strip().strip('"\'')
        if ('key' in name or 'token' in name) and val and not val.startswith('<'):
            return val
    return ''


def api_key() -> str:
    key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    if key:
        return key
    local = Path(__file__).resolve().parent / 'deepseek_key.txt'
    if local.exists():
        try:
            key = local.read_text(encoding='utf-8').strip()
            if key and not key.startswith('#'):
                return key
        except OSError:
            pass
    return _key_from_credentials()


def available(timeout: float = 1.5, force: bool = False) -> bool:
    """能否使用云端复核（有 key 且网络可达）。结果缓存 60 s。"""
    now = time.time()
    if not force and _AVAILABLE['ok'] is not None and now - _AVAILABLE['ts'] < 60:
        return bool(_AVAILABLE['ok'])
    key = api_key()
    ok = False
    if key:
        try:
            req = urllib.request.Request(MODELS_URL, headers={'Authorization': f'Bearer {key}'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ok = 200 <= resp.status < 300
        except Exception:                                  # noqa: BLE001
            ok = False
    _AVAILABLE.update({'ts': now, 'ok': ok})
    return ok


# ---------------------------------------------------------------- 编码 / 规范化
def _encode(img, max_side: int = 1024, quality: int = 88) -> str:
    h, w = img.shape[:2]
    scale = min(1.0, float(max_side) / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise ValueError('jpeg encode failed')
    return base64.b64encode(buf.tobytes()).decode('ascii')


def _fingerprint(img) -> str:
    small = cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA)
    return hashlib.md5(small.tobytes()).hexdigest()


COLORS = ('红', '黄', '蓝', '绿', '黑', '浅蓝')


def normalize_color(raw):
    """把模型给的颜色词归一到项目 6 色；无法判断返回 None。"""
    if not raw:
        return None
    t = str(raw).strip()
    if any(k in t for k in ('浅蓝', '淡蓝', '天蓝')):
        return '浅蓝'
    if any(k in t for k in ('红', '橙', '粉')):
        return '红'
    if any(k in t for k in ('黄', '金')):
        return '黄'
    if any(k in t for k in ('蓝', '青')):
        return '蓝'
    if any(k in t for k in ('绿',)):
        return '绿'
    if any(k in t for k in ('黑', '灰')):
        return '黑'
    return None


def normalize_shape(raw: str) -> str:
    """把模型给的形状名归一到主流程的类别名；认不出就返回 '其他几何体'。"""
    s = (raw or '').strip()
    if not s:
        return '其他几何体'
    for name in SHAPES:
        if s == name:
            return name
    # 关键词优先（注意“圆锥”要先于“圆柱/圆”判断）
    if '圆锥' in s:
        return '圆锥'
    if '棱锥' in s or '金字塔' in s or '锥' in s:
        return '棱锥'
    if '球' in s:
        return '球'
    if '圆柱' in s or ('柱' in s and '圆' in s):
        return '圆柱/圆形'
    if '菱' in s:
        return '菱体/菱形'
    if '三棱' in s or '三角' in s:
        return '三棱体/三角形'
    if '五棱' in s:
        return '五棱体'
    if '六棱' in s or '多棱' in s or '八棱' in s:
        return '六棱体/多棱'
    if '正方' in s or ('方形' in s and '长' not in s):
        return '正方体/正方形'
    if '长方' in s:
        return '长方体/长方形'
    if '圆' in s:
        return '圆柱/圆形'
    return '其他几何体'


# ---------------------------------------------------------------- 请求
def _post(body: dict, key: str, timeout: float) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        API_URL, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8', 'ignore'))


def _prompt_objects(candidates: List[str]) -> str:
    cand = '/'.join(candidates or SHAPES)
    return (
        '这是一个从上方约 60 度俯视拍摄的灰色托盘。请逐个识别托盘里的每个物块，'
        '**只回答落在灰色托盘内部的物块**；托盘外面的东西（桌面、杯子、白色圆球等）一律不要回答。\n'
        '判断它的几何形状与颜色。\n'
        f'形状只能从这个列表里选：{cand}。\n'
        '注意：60 度俯视会让纵深压缩，正方体可能看起来像略扁的长方形，不要因此判成长方体；'
        '顶面是圆但有中心尖点的通常是圆锥；方底尖顶的是棱锥。\n'
        '只输出 JSON，不要多余文字，格式：'
        '{"objects":[{"shape":"...","color":"...","confidence":0.0,"reason":"...",'
        '"center":[x,y]}]}；center 是物块中心的归一化坐标 0~1（左上为原点，x 右 y 下）。'
    )


def ask_objects(img, candidates: Optional[List[str]] = None, timeout: float = DEFAULT_TIMEOUT,
                model: Optional[str] = None, detail: str = 'high') -> Optional[Dict[str, Any]]:
    """识别整幅图里的所有物块。失败返回 None。"""
    key = api_key()
    if not key:
        return None
    t0 = time.time()
    try:
        body = {
            'model': model or DEFAULT_MODEL,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': _prompt_objects(candidates or SHAPES)},
                    {'type': 'image_url', 'image_url': {
                        'url': 'data:image/jpeg;base64,' + _encode(img), 'detail': detail}},
                ],
            }],
            'max_tokens': 900,
            'thinking': {'type': 'disabled'},
            'temperature': 0,
            'response_format': {'type': 'json_object'},
        }
        payload = _post(body, key, timeout)
        text = (payload.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        obj = json.loads(text) if text.strip() else {}
        out = []
        for it in (obj.get('objects') or []):
            raw = str(it.get('shape') or '')
            out.append({
                'shape': normalize_shape(raw),
                'raw_shape': raw,
                'color': str(it.get('color') or ''),
                'conf': float(it.get('confidence') or 0.0),
                'reason': str(it.get('reason') or '')[:120],
                'center': it.get('center'),
            })
        return {'objects': out, 'ms': int((time.time() - t0) * 1000), 'model': body['model']}
    except Exception:                                      # noqa: BLE001
        return None


def ask_shape(img, candidates: Optional[List[str]] = None, timeout: float = DEFAULT_TIMEOUT,
              model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """识别单个物块（传裁剪图）。失败返回 None；10 s 内同图直接复用结果。"""
    fp = _fingerprint(img)
    hit = _CACHE.get(fp)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return dict(hit[1])
    r = ask_objects(img, candidates=candidates, timeout=timeout, model=model)
    if not r or not r.get('objects'):
        return None
    best = max(r['objects'], key=lambda o: o.get('conf') or 0)
    res = {
        'shape': best['shape'], 'raw_shape': best.get('raw_shape'), 'conf': best.get('conf', 0.0),
        'color': best.get('color'), 'reason': best.get('reason'), 'ms': r.get('ms', 0),
    }
    _CACHE[fp] = (time.time(), dict(res))
    return res


def ask_objects_cached(img, candidates: Optional[List[str]] = None,
                      timeout: float = DEFAULT_TIMEOUT, model: Optional[str] = None
                      ) -> Optional[Dict[str, Any]]:
    """整幅图批量识别（同一幅图 10 s 内只请求一次）。"""
    fp = _fingerprint(img)
    hit = _CACHE.get(fp)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    r = ask_objects(img, candidates=candidates, timeout=timeout, model=model)
    if r is not None:
        _CACHE[fp] = (time.time(), r)
    return r


def _prompt_numbered(n: int, candidates) -> str:
    names = '、'.join(candidates)
    return (
        f'图中有 {n} 个物体，已用彩色轮廓和数字 1..{n} 标出编号。\n'
        f'请对每个编号判断“形状”和“颜色”。形状只能从：{names}。\n'
        '注意：这是 60° 左右俯视拍摄，纵深会被压缩（正方体可能看起来略扁，不要因此判成长方体）；'
        '顶面是圆且中心收成一个尖 = 圆锥；底面是方/多边形而顶部收成尖 = 棱锥；'
        '看不到关键面时给低置信。\n'
        '只输出 JSON：{"objects":[{"id":1,"shape":"...","color":"...","confidence":0.0,"reason":"..."}]}'
    )


def _prompt_crops(n: int, candidates) -> str:
    names = '、'.join(candidates)
    return (
        f'这是一张拼图，共 {n} 个编号格子（左上角有数字 1..{n}），每个格子是一个物块的放大裁剪。\n'
        f'请对每个编号判断“形状”和“颜色”。形状只能从：{names}。\n'
        '注意：拍摄角约 60° 俯视（能看到顶面 + 一小部分侧面），纵深被压缩；'
        '顶面是圆、往上收成一个尖 = 圆锥；底面是多边形、往上收成尖 = 棱锥；'
        '圆面但侧壁竖直 = 圆柱；看不到关键面时给低置信。颜色写主色（红/黄/蓝/绿/浅蓝/黑/白）。\n'
        '只输出 JSON：{"objects":[{"id":1,"shape":"...","color":"...","confidence":0.0,"reason":"..."}]}'
    )


_CROP_CACHE = {}


def ask_crops(warp, items, candidates=None, timeout=DEFAULT_TIMEOUT, model=None,
              detail='high', cell=300, pad=0.18):
    """把每个物块的 bbox 裁剪放大，拼成一张带编号的图，一次请求返回 {id: {...}}。"""
    import cv2
    import numpy as np

    key = api_key()
    if not key or not items:
        return None

    crops = []
    for it in items:
        m = it.get('mask')
        if m is None:
            return None
        ys, xs = np.nonzero(m)
        if ys.size == 0:
            return None
        y0, y1 = int(ys.min()), int(ys.max())
        x0, x1 = int(xs.min()), int(xs.max())
        pw = int((x1 - x0) * pad) + 4
        ph = int((y1 - y0) * pad) + 4
        x0, x1 = max(0, x0 - pw), min(warp.shape[1], x1 + pw)
        y0, y1 = max(0, y0 - ph), min(warp.shape[0], y1 + ph)
        c = warp[y0:y1, x0:x1]
        if c.size == 0:
            return None
        c = cv2.resize(c, (cell - 24, cell - 24), interpolation=cv2.INTER_LINEAR)
        crops.append(c)

    n = len(crops)
    cols = min(3, n)
    rows = (n + cols - 1) // cols
    sheet = np.full((rows * cell, cols * cell, 3), 245, np.uint8)
    for i, c in enumerate(crops):
        r, q = divmod(i, cols)
        y, x = r * cell + 12, q * cell + 12
        sheet[y:y + c.shape[0], x:x + c.shape[1]] = c
        cv2.putText(sheet, str(i + 1), (q * cell + 6, r * cell + 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 5)
        cv2.putText(sheet, str(i + 1), (q * cell + 6, r * cell + 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)

    fp = _fingerprint(sheet) + '|c%d' % n
    now = time.time()
    if _CROP_CACHE.get(fp) and now - _CROP_CACHE[fp][0] < CACHE_TTL:
        return _CROP_CACHE[fp][1]

    t0 = time.time()
    try:
        body = {
            'model': model or DEFAULT_MODEL,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': _prompt_crops(n, candidates or SHAPES)},
                    {'type': 'image_url', 'image_url': {
                        'url': 'data:image/jpeg;base64,' + _encode(sheet, max_side=1024),
                        'detail': detail}},
                ],
            }],
            'max_tokens': 900,
            'temperature': 0,
            'thinking': {'type': 'disabled'},
            'response_format': {'type': 'json_object'},
        }
        payload = _post(body, key, timeout)
        text = (payload.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        obj = json.loads(text) if text.strip() else {}
        by_id = {}
        for it in (obj.get('objects') or []):
            try:
                i = int(it.get('id'))
            except (TypeError, ValueError):
                continue
            raw = str(it.get('shape') or '')
            by_id[i] = {
                'shape': normalize_shape(raw), 'raw_shape': raw,
                'color': normalize_color(it.get('color')),
                'conf': float(it.get('confidence') or 0.0),
                'reason': str(it.get('reason') or '')[:120],
            }
        res = {'by_id': by_id, 'ms': int((time.time() - t0) * 1000)}
        _CROP_CACHE[fp] = (now, res)
        return res
    except Exception:                                    # noqa: BLE001
        return None


def ask_numbered(warp, items, candidates=None, timeout=DEFAULT_TIMEOUT, model=None,
                 detail='high'):
    """对整幅图做一次请求，按叠加的编号返回 {id: {...}}（id 从 1 开始）。

    items 为包含 'mask' 的物块列表；编号顺序与 items 顺序一致。
    返回 {'by_id': {...}, 'ms': int} 或 None。
    """
    import base64
    import cv2
    import numpy as np

    key = api_key()
    if not key or not items:
        return None
    fp = _fingerprint(warp) + '|n%d' % len(items)
    now = time.time()
    if _CACHE.get(fp) and now - _CACHE[fp][0] < CACHE_TTL:
        return _CACHE[fp][1]

    vis = warp.copy()
    for i, it in enumerate(items, 1):
        m = it.get('mask')
        if m is None:
            continue
        cnts = cv2.findContours((m > 0).astype(np.uint8), cv2.RETR_EXTERNAL,
                                cv2.CHAIN_APPROX_SIMPLE)[0]
        if not cnts:
            continue
        c = max(cnts, key=cv2.contourArea)
        cv2.drawContours(vis, [c], -1, (0, 255, 255), 3)
        x, y, w, h = cv2.boundingRect(c)
        cv2.putText(vis, str(i), (x + 4, max(18, y - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.9, (0, 0, 0), 5)
        cv2.putText(vis, str(i), (x + 4, max(18, y - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.9, (0, 255, 255), 2)

    t0 = time.time()
    try:
        body = {
            'model': model or DEFAULT_MODEL,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': _prompt_numbered(len(items), candidates or SHAPES)},
                    {'type': 'image_url', 'image_url': {
                        'url': 'data:image/jpeg;base64,' + _encode(vis), 'detail': detail}},
                ],
            }],
            'max_tokens': 900,
            'temperature': 0,
            'thinking': {'type': 'disabled'},
            'response_format': {'type': 'json_object'},
        }
        payload = _post(body, key, timeout)
        text = (payload.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        obj = json.loads(text) if text.strip() else {}
        by_id = {}
        for it in (obj.get('objects') or []):
            try:
                i = int(it.get('id'))
            except (TypeError, ValueError):
                continue
            raw = str(it.get('shape') or '')
            by_id[i] = {
                'shape': normalize_shape(raw), 'raw_shape': raw,
                'color': normalize_color(it.get('color')),
                'conf': float(it.get('confidence') or 0.0),
                'reason': str(it.get('reason') or '')[:120],
            }
        res = {'by_id': by_id, 'ms': int((time.time() - t0) * 1000)}
        _CACHE[fp] = (now, res)
        return res
    except Exception:                                    # noqa: BLE001
        return None


def match(warp, mask, min_dist: float = 0.18, color=None, candidates=None,
          timeout: float = DEFAULT_TIMEOUT, model=None):
    """把整幅图批量结果里的某个物块匹配到当前掩膜。

    匹配优先级：①颜色相同（LLM 的中心坐标不稳，颜色更可靠）→ ②中心最近（距离 <= min_dist）。
    返回 {'shape','raw_shape','conf','color','reason','ms','dist','by'}；匹配不上返回 None。
    """
    import numpy as np  # 局部导入，避免模块级强依赖

    r = ask_objects_cached(warp, candidates=candidates, timeout=timeout, model=model)
    if not r or not r.get('objects'):
        return None
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    h, w = mask.shape[:2]
    cu, cv_ = float(xs.mean()) / w, float(ys.mean()) / h

    def _center(o):
        c = o.get('center') or None
        if not c or len(c) < 2:
            return None
        try:
            ox, oy = float(c[0]), float(c[1])
        except (TypeError, ValueError):
            return None
        if ox > 1.5 or oy > 1.5:
            if ox > 1.5 and ox <= w and oy <= h:
                ox, oy = ox / w, oy / h
            else:
                ox, oy = ox / 1000.0, oy / 1000.0
        return ox, oy

    want = normalize_color(color) if color else None
    best, best_key, best_d = None, None, 1e9
    for o in r['objects']:
        c = _center(o)
        if c is None:
            continue
        d = ((c[0] - cu) ** 2 + (c[1] - cv_) ** 2) ** 0.5
        same = bool(want) and normalize_color(o.get('color')) == want
        # 颜色相同：即使距离远一点也优先；否则要求距离足够近
        if same:
            key = (0, d)
        elif d <= min_dist:
            key = (1, d)
        else:
            continue
        if key < (best_key if best_key else (9, 9e9)):
            best, best_key, best_d = o, key, d
    if best is None:
        return None
    out = dict(best)
    out['dist'] = round(best_d, 3)
    out['by'] = 'color' if best_key[0] == 0 else 'center'
    out['ms'] = r.get('ms', 0)
    return out


def describe(res: Optional[Dict[str, Any]]) -> str:
    if not res:
        return '云端复核不可用'
    return '%s + %s（conf %.2f，%d ms）%s' % (
        res.get('shape'), res.get('color') or '?', float(res.get('conf') or 0),
        int(res.get('ms') or 0), (' · ' + res['reason']) if res.get('reason') else '')


# --------------------------------------------------------------------------
# 两视角联合识别：图1=顶视，图2=副摄（可选）。两图一起请求，让模型交叉校验。
# --------------------------------------------------------------------------
def _prompt_two_views(candidates: List[str]) -> str:
    cand = '/'.join(candidates or SHAPES)
    return (
        '你收到两张图，拍的是**同一个灰色托盘**：\n'
        '图1 = 顶视相机（从上方约 60° 俯视）；图2 = 副摄（另一个方位，同样约 60° 俯视，能看到物块的另一个侧面）。\n'
        '请结合两张图，逐个判断托盘里每个物块的几何形状与颜色。'
        '**只回答落在灰色托盘内部的物块**；托盘外的桌面、杯子、白色圆球一律不要回答。\n'
        f'形状只能从这个列表里选：{cand}。\n'
        '两图互相校验的要点：\n'
        '- 正方体：两图顶面都接近正方形，且侧高约等于边长；长方体：某一方向明显更长。\n'
        '- 菱体/菱形：顶面是菱形或平行四边形（四边接近等长但角不是 90°），侧面是斜的。\n'
        '- 六棱体：顶面六边形；三棱体：顶面三角形；五棱体：顶面五边形。\n'
        '- 圆柱：顶面圆、侧面竖直；球：两图轮廓都圆、看不到平面；棱锥/圆锥：顶面有中心尖点、侧面收成三角。\n'
        '若两张图给你的判断冲突，请在 reason 里说明，并给较低的 confidence。'
        '只输出 JSON，不要多余文字，格式：'
        '{"objects":[{"shape":"...","color":"...","confidence":0.0,"reason":"...","center":[x,y]}]}；'
        'center 是**图1（顶视）**中的归一化中心坐标 0~1（左上为原点，x 向右 y 向下），**每个物体都必须给出 center，即使不确定也要给最接近的估计值，不能省略、不能为 null**。'
    )


def ask_objects2(top_img, side_img=None, candidates: Optional[List[str]] = None,
                 timeout: float = DEFAULT_TIMEOUT, model: Optional[str] = None,
                 detail: str = 'high') -> Optional[Dict[str, Any]]:
    """顶视(+副摄)联合识别；返回结构与 ask_objects 相同，多一个 views 字段。"""
    key = api_key()
    if not key:
        return None
    t0 = time.time()
    try:
        text_prompt = (_prompt_two_views(candidates or SHAPES) if side_img is not None
                       else _prompt_objects(candidates or SHAPES))
        content = [{'type': 'text', 'text': text_prompt}]
        for im in ([top_img] + ([side_img] if side_img is not None else [])):
            content.append({'type': 'image_url', 'image_url': {
                'url': 'data:image/jpeg;base64,' + _encode(im), 'detail': detail}})
        body = {
            'model': model or DEFAULT_MODEL,
            'messages': [{'role': 'user', 'content': content}],
            'max_tokens': 900,
            'thinking': {'type': 'disabled'},
            'temperature': 0,
            'response_format': {'type': 'json_object'},
        }
        payload = _post(body, key, timeout)
        text = (payload.get('choices') or [{}])[0].get('message', {}).get('content') or ''
        obj = json.loads(text) if text.strip() else {}
        out = []
        for it in (obj.get('objects') or []):
            raw = str(it.get('shape') or '')
            c = it.get('center') or it.get('centre') or []
            out.append({
                'shape': normalize_shape(raw),
                'raw_shape': raw,
                'color': str(it.get('color') or ''),
                'conf': float(it.get('confidence') or it.get('conf') or 0.0),
                'reason': str(it.get('reason') or '')[:160],
                'center': [float(c[0]), float(c[1])] if len(c) >= 2 else None,
                'views': 'top+side' if side_img is not None else 'top',
            })
        return {'objects': out, 'ms': int((time.time() - t0) * 1000), 'raw': text,
                'views': 'top+side' if side_img is not None else 'top'}
    except Exception:                                   # noqa: BLE001
        return None


def ask_objects2_cached(top_img, side_img=None, candidates: Optional[List[str]] = None,
                        timeout: float = DEFAULT_TIMEOUT, model: Optional[str] = None
                        ) -> Optional[Dict[str, Any]]:
    """两视角识别缓存版（10 s 内同一对图只请求一次）。"""
    fp = _fingerprint(top_img) + '|' + (_fingerprint(side_img) if side_img is not None else '-')
    hit = _CACHE.get(fp)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    r = ask_objects2(top_img, side_img, candidates=candidates, timeout=timeout, model=model)
    if r is not None:
        _CACHE[fp] = (time.time(), r)
    return r
