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
# 赛题 6 类（形状裸名；颜色单独由 color 字段给出，class_key = 颜色|形状）
SHAPES = ['圆锥', '四棱锥', '六棱锥', '椭圆柱', '长方体', '其他几何体']
#: 允许的颜色（闭集）；蓝色物块在暖光下会偏紫，故保留“蓝/紫”双写提示
COLORS = ['黄', '紫', '蓝', '红', '白', '粉']

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


COLORS = ('红', '橙', '黄', '绿', '蓝', '紫', '黑', '白', '浅蓝')


def normalize_color(raw):
    """把模型给的颜色词归一到项目 6 色；无法判断返回 None。"""
    if not raw:
        return None
    t = str(raw).strip()
    # 具体优先：浅蓝(青蓝) > 紫 > 橙 > 粉(归红) > 单色
    if any(k in t for k in ('浅蓝', '淡蓝', '天蓝', '青蓝')):
        return '浅蓝'
    if any(k in t for k in ('紫', '藕荷', '蓝紫')):
        return '紫'
    if any(k in t for k in ('橙', '橘')):
        return '橙'
    if any(k in t for k in ('粉', '品红', '玫红')):
        return '红'
    if any(k in t for k in ('红',)):
        return '红'
    if any(k in t for k in ('黄', '金', '米')):
        return '黄'
    if any(k in t for k in ('蓝', '青')):
        return '蓝'
    if any(k in t for k in ('绿',)):
        return '绿'
    if any(k in t for k in ('白',)):
        return '白'
    if any(k in t for k in ('黑', '灰')):
        return '黑'
    return None


def normalize_shape(raw: str) -> str:
    """把模型给的形状名归一到赛题 6 类（裸名）；认不出返回“其他几何体”。"""
    s = (raw or '').strip()
    if not s:
        return '其他几何体'
    for name in SHAPES:
        if s == name:
            return name
    # 先判“锥”，再判“柱”，最后判“方/长”
    if '圆锥' in s:
        return '圆锥'
    if '六棱锥' in s or ('六' in s and '锥' in s):
        return '六棱锥'
    if '四棱锥' in s or '方锥' in s or ('四' in s and '锥' in s) or s == '棱锥':
        return '四棱锥'
    if '锥' in s or '金字塔' in s:
        return '四棱锥'
    if '椭' in s:
        return '椭圆柱'
    if '圆柱' in s or '圆形' in s or s == '圆':
        return '椭圆柱'
    if any(k in s for k in ('长方体', '正方形', '正方体', '长方形', '矩形', '菱形', '方体', '立方')):
        return '长方体'
    # 赛题只有“六棱锥”，几何侧可能给出“六棱柱/多棱柱”的轮廓 → 归入六棱锥
    if '六棱' in s or '多棱' in s or '八棱' in s:
        return '六棱锥'
    return '其他几何体'
def _post(body: dict, key: str, timeout: float) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        API_URL, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8', 'ignore'))


_RULE = (
    '【判别步骤（严格按顺序，先答“顶面内部有没有棱线”）】\n'
    '第 0 步｜顶面内部：是一整片均匀平面，还是被棱线/明暗切成 2~6 块？\n'
    '  · 整片均匀、内部无分界线 → 柱体（椭圆柱 / 长方体）\n'
    '  · 内部有 1 条以上棱线、越靠中心越尖 → 锥体（圆锥 / 四棱锥 / 六棱锥）\n'
    '第 1 步｜柱体：看顶面外轮廓\n'
    '  · 边缘全圆滑、无尖角（椭圆） → 椭圆柱\n'
    '  · 有 4 个尖角、两组近似平行的边（矩形/平行四边形，朝向任意） → 长方体\n'
    '  · 60° 俯视会把高度压扁，“看起来矮”不代表不是长方体，以顶面形状为准\n'
    '第 2 步｜锥体：看外轮廓边数 + 内部斜棱\n'
    '  · 外轮廓是圆、中心收成一点 → 圆锥\n'
    '  · 外轮廓四边形（4 边），内部有 1~4 条斜棱把顶面分成三角面 → 四棱锥\n'
    '  · 外轮廓六边形（6 边），内部有 6 条斜棱汇聚到中心 → 六棱锥\n'
    '第 3 步｜顶/侧交界（侧面窄带只做辅助）：\n'
    '  · 柱体：顶面与侧面之间是一条“直棱”，侧面上下等宽\n'
    '  · 锥体：顶面与侧面之间是“从顶点向外的多条斜棱”，侧面从边缘向外张开\n'
)

_SCENE = (
    '【场景与数量】这是机械臂分拣台（灰色托盘，俯视约 60°）。本赛题只有 6 种物品，'
    '每种最多 2 个、合计最多 12 个；允许更少（少件/空盘请如实返回）。'
    '严禁为凑数编造、严禁把两个物块合并成一条、严禁同一物块输出两次。\n'
    '【类别白名单】shape 只能取（写裸形状名，颜色单独写在 color）：'
    '圆锥 / 四棱锥 / 六棱锥 / 椭圆柱 / 长方体；明显不符则填“其他几何体”。\n'
    '【颜色】color 只能取：黄、紫、蓝、红、白、粉。'
    '蓝=冷色、B 明显强于 R；紫=带明显品红味（R 与 B 都不低）——蓝色在暖光下会偏紫，'
    '若介于两者之间请报“蓝”并给 confidence ≤0.6 且 reason 写“偏紫”；'
    '白=低饱和高亮（别把反光当白）；粉=浅红偏灰（别判成白或红）；红=R 明显最高。\n'
)

_JSON_SPEC = (
    '只输出 JSON，不要多余文字：'
    '{"objects":[{"id":1,"shape":"圆锥/四棱锥/六棱锥/椭圆柱/长方体/其他几何体",'
    '"color":"黄/紫/蓝/红/白/粉","confidence":0.0,'
    '"inner_edges":0,"outline_corners":0,"edge_style":"straight","apex":0,'
    '"top_aspect":1.00,"reason":"≤20字","center":[x,y]}]}\n'
    '字段含义：inner_edges=顶面内部棱线条数(0/1/2/4/6，0=整片均匀)；'
    'outline_corners=顶面外轮廓尖角数(0=圆/椭圆)；edge_style=顶/侧交界“straight(直棱)”或“radial(辐射斜棱)”；'
    'apex=顶面中心是否尖点(0/1)；center 是物块中心归一化坐标 0~1。'
)


def _prompt_objects(candidates: List[str]) -> str:
    return (
        '这是一张从上方约 60° 俯视拍的灰色托盘照片，物块用彩色轮廓+数字标注。\n'
        '只回答落在灰色托盘内部的物块；托盘外的桌面、杯子、白色圆球一律不要回答。\n'
        + _SCENE + _RULE + _JSON_SPEC
    )


def _prompt_two_views(candidates: List[str]) -> str:
    return (
        '你收到两张图，拍的是同一个灰色托盘、同一时刻（物块用彩色轮廓+数字标注，编号一致）：\n'
        '图1 = 主摄（俯视约 60°，顶面正对镜头，只带出一条很窄的侧面）；\n'
        '图2 = 副摄（另一个方位，同样约 60° 俯视，带出的是另一侧的窄侧面）。\n'
        '两图的顶面都被压扁，侧面信息很少 → **以两图的顶面为唯一主判据**，侧面只用于确认“顶/侧之间是直棱还是辐射斜棱”。\n'
        '只回答落在灰色托盘内部的物块；托盘外的桌面、杯子、白色圆球一律不要回答。\n'
        + _SCENE + _RULE +
        '若两张图判断冲突，在 reason 里写明，并给较低 confidence。\n'
        + _JSON_SPEC.replace('"center":[x,y]', '"center":[x,y]') +
        '\ncenter 必须给 **图1（顶视）** 中的位置，每个物块都要给，不能省略/为 null。'
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
    return (
        f'这是一张拼图，共 {n} 个编号格子（每格左上角有数字 1..{n}），每格是一个物块的放大裁剪。\n'
        + _SCENE + _RULE + _JSON_SPEC
    )


def _prompt_crops(n: int, candidates) -> str:
    return _prompt_numbered(n, candidates)


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
            # 模型有时给像素坐标（0..700），统一归一到 0..1
            if len(c) >= 2:
                try:
                    cx, cy = float(c[0]), float(c[1])
                    if cx > 1.5 or cy > 1.5:
                        hh, ww = top_img.shape[:2]
                        cx, cy = cx / max(1.0, float(ww)), cy / max(1.0, float(hh))
                    c = [max(0.0, min(1.0, cx)), max(0.0, min(1.0, cy))]
                except Exception:                      # noqa: BLE001
                    c = []
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
