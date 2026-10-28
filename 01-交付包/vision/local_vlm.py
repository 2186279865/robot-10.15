"""本地千问（Ollama qwen3-vl）形状第二意见。

用法（在 vision/ 目录下）::

    import local_vlm
    if local_vlm.available():
        r = local_vlm.ask_shape(crop_bgr, ['正方体/正方形', '长方体/长方形'])
        # r -> {'shape': '正方体/正方形', 'conf': 0.8, 'reason': '...', 'ms': 1290}

约定：
- 只做“在给定候选里挑一个”的选择题，避免它自由发挥编形状名；
- 服务没起 / 超时 / 解析失败 → 返回 None，调用方保持几何判据结果（绝不抛异常）；
- 结果按 (图像指纹, 候选集合) 缓存 10 s，连续识别同一件时不会重复推理。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

BASE = os.environ.get('ROBOT_VLM_URL', 'http://127.0.0.1:11434').rstrip('/')
MODEL = os.environ.get('ROBOT_VLM_MODEL', 'qwen3-vl:4b-instruct')

# 允许模型输出的形状名（与 classify / config.class_map 里用的名字保持一致）
SHAPES = ['正方体/正方形', '长方体/长方形', '菱体/菱形', '三棱体/三角形',
          '五棱体', '六棱体/多棱', '圆柱/圆形', '球', '其他几何体']

_CACHE = {}
_CACHE_TTL = 10.0

PROMPT = (
    '你是几何形状判别器。图里是一个放在灰色托盘上的小物块，相机从 60° 斜上方俯拍，'
    '所以你能同时看到它的“顶面”和“一到两个侧面”。\n'
    '请只根据可见的面判断整体是什么立体形状，规则：\n'
    '1. 顶面是圆/椭圆、侧面是竖直的曲面 → 圆柱；顶面是圆且侧面顶部向内收窄、看不到平顶 → 球；\n'
    '2. 顶面是正方形或长方形、四个角是直角 → 正方体（三边等长）或长方体（某一边明显更长）；\n'
    '3. 顶面是菱形（四边等长但角不是直角）→ 菱体；\n'
    '4. 顶面是 3/5/6 条边的多边形 → 三棱体 / 五棱体 / 六棱体；\n'
    '5. 注意：60° 俯视会让纵深方向压缩，正方形会被看成略扁的长方形，不要因此判成长方体；\n'
    '6. 看不清关键面时，选最可能的，但 confidence 给低值（<0.5）。\n'
    '只输出 JSON：{"shape": "...", "confidence": 0-1, "reason": "一句话中文依据"}'
)


def available(timeout: float = 0.8) -> bool:
    try:
        with urllib.request.urlopen(BASE + '/api/tags', timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        names = [m.get('name', '') for m in data.get('models', [])]
        return MODEL in names or any(n.split(':')[0] == MODEL.split(':')[0] for n in names)
    except Exception:                                   # noqa: BLE001
        return False


def _encode(img_bgr, max_side: int = 720, quality: int = 88) -> str:
    h, w = img_bgr.shape[:2]
    s = min(1.0, float(max_side) / max(h, w))
    if s < 1.0:
        img_bgr = cv2.resize(img_bgr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return ''
    return base64.b64encode(buf.tobytes()).decode('ascii')


def _fingerprint(b64: str, candidates) -> str:
    h = hashlib.md5(b64.encode('ascii'))
    h.update(('|'.join(candidates)).encode('utf-8'))
    return h.hexdigest()


def ask_shape(img_bgr, candidates=None, timeout: float = 8.0, model: str = None):
    """在候选形状里挑一个。返回 dict 或 None（服务不可用/解析失败）。"""
    try:
        cands = [c for c in (candidates or SHAPES) if c in SHAPES] or SHAPES
        b64 = _encode(img_bgr)
        if not b64:
            return None
        key = _fingerprint(b64, cands)
        now = time.time()
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]

        prompt = PROMPT + '\n候选形状（必须从中选一个，不要输出候选外的名字）：' + '、'.join(cands)
        body = {
            'model': model or MODEL,
            'messages': [{'role': 'user', 'content': prompt, 'images': [b64]}],
            'stream': False,
            'format': 'json',
            'think': False,
            'keep_alive': os.environ.get('ROBOT_VLM_KEEP_ALIVE', '120s'),
            'options': {'temperature': 0, 'num_predict': 220,
                        # 默认走 CPU：SAM 已占显存，4B 模型同时上 GPU 会 OOM
                        'num_gpu': int(os.environ.get('ROBOT_VLM_GPU', '99'))},
        }
        req = urllib.request.Request(
            BASE + '/api/chat', data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
        ms = int((time.time() - t0) * 1000)
        text = ((raw.get('message') or {}).get('content') or '').strip()
        # 有的版本会把 JSON 包在 ```json ... ``` 里
        if text.startswith('```'):
            text = text.strip('`')
            text = text.split('\n', 1)[-1].rsplit('\n', 1)[0]
        data = json.loads(text)
        shape = str(data.get('shape', '')).strip()
        if shape not in cands:
            # 允许“圆柱/圆形”这类带斜杠的宽松匹配
            for c in cands:
                if shape and (shape in c or c in shape):
                    shape = c
                    break
            else:
                shape = ''
        conf = data.get('confidence')
        try:
            conf = max(0.0, min(1.0, float(conf)))
        except Exception:                               # noqa: BLE001
            conf = 0.5
        out = {'shape': shape or None, 'conf': conf,
               'reason': str(data.get('reason', ''))[:120], 'ms': ms}
        _CACHE[key] = (now, out)
        return out
    except Exception:                                   # noqa: BLE001
        return None


def warmup(timeout: float = 180.0) -> bool:
    """预热：第一次推理要把模型读进内存（实测 ~85 s），提前跑一次避免首次识别卡住。"""
    try:
        import numpy as _np
        dummy = _np.full((240, 320, 3), 128, _np.uint8)
        return ask_shape(dummy, ['正方体/正方形', '圆柱/圆形'], timeout=timeout) is not None
    except Exception:                                   # noqa: BLE001
        return False
