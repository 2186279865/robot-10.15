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
        '判断几何形状与颜色。\n'
        f'形状只能从这些里选：{cand}。\n'
        '注意：俯视会让纵深压缩，正方体可能看起来略扁，不要因此判成长方体；'
        '顶面是圆但中心收尖的是圆锥；方底/多边形底、顶部收拢成尖点的是棱锥；'
        '看不清就给低置信度。\n'
        '只输出 JSON：{"objects":[{"shape":"...","color":"...","confidence":0.0,'
        '"reason":"...","center":[x,y]}]}；center 用归一化坐标 0~1（左上角为原点，'
        'x 向右、y 向下）。'
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


def match(warp, mask, min_dist: float = 0.18, candidates: Optional[List[str]] = None,
          timeout: float = DEFAULT_TIMEOUT, model: Optional[str] = None
          ) -> Optional[Dict[str, Any]]:
    """把整幅图批量结果里的某个物块，按归一化中心距离匹配到当前掩膜。

    返回 {'shape','raw_shape','conf','color','reason','ms','dist'}；匹配不上返回 None。
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
    best, best_d = None, 1e9
    for o in r['objects']:
        c = o.get('center') or None
        if not c or len(c) < 2:
            continue
        try:
            ox, oy = float(c[0]), float(c[1])
        except (TypeError, ValueError):
            continue
        # 模型可能给 0..1 归一化，也可能给像素（按 1000 归一或 0..w 像素）
        if ox > 1.5 or oy > 1.5:
            if ox > 1.5 and ox <= w and oy <= h and max(ox, oy) > 2:
                ox, oy = ox / w, oy / h
            else:
                ox, oy = ox / 1000.0, oy / 1000.0
        d = ((ox - cu) ** 2 + (oy - cv_) ** 2) ** 0.5
        if d < best_d:
            best, best_d = o, d
    if best is None or best_d > min_dist:
        return None
    out = dict(best)
    out['dist'] = round(best_d, 3)
    out['ms'] = r.get('ms', 0)
    return out



def describe(res: Optional[Dict[str, Any]]) -> str:
    if not res:
        return '云端复核不可用'
    return '%s + %s（conf %.2f，%d ms）%s' % (
        res.get('shape'), res.get('color') or '?', float(res.get('conf') or 0),
        int(res.get('ms') or 0), (' · ' + res['reason']) if res.get('reason') else '')
