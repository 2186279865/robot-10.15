"""Optional visual models. No key is stored and no network call happens in geometry mode."""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

import cv2


def image_b64(frame):
    h, w = frame.shape[:2]
    if max(h, w) > 900:
        frame = cv2.resize(frame, (round(w * 900 / max(h, w)), round(h * 900 / max(h, w))))
    ok, data = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError('无法编码物块图片')
    return base64.b64encode(data).decode('ascii')


def parse_result(raw, allowed):
    if not isinstance(raw, str):
        raise ValueError('视觉模型没有返回文本')
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r'\{.*\}', text, re.S)
        if not match:
            raise ValueError('视觉模型没有返回 JSON') from None
        data = json.loads(match.group())
    if not isinstance(data, dict):
        raise ValueError('模型结果必须是对象')
    shape = data.get('shape')
    if shape not in allowed:
        raise ValueError(f'模型返回范围外的形状：{shape}')
    hollow = data.get('hollow', '不确定')
    if hollow not in ('实心', '镂空', '不确定'):
        hollow = '不确定'
    visible = data.get('visibility', '不确定')
    if visible not in ('完整', '遮挡', '不确定'):
        visible = '不确定'
    color = str(data.get('color') or '不确定')[:20]
    return {'shape': shape, 'hollow': hollow, 'visibility': visible,
            'model_color': color, 'reason': str(data.get('reason') or '')[:200]}


class VisualModel:
    def __init__(self, cfg):
        self.cfg = cfg

    def classify(self, top, side=None, backend=None):
        backend = backend or self.cfg['backend']
        allowed = self.cfg['shapes']
        prompt = ('判断图中居中的单个物块，第一张为主视图。若有第二张，是已经匹配的同一物块另一视角。'
                  '物块可能旋转、带印字、带孔。印字只是纹理，不是判断形状的依据。'
                  '绿色细描边可能是标注，不代表物块主体颜色。'
                  '三棱柱有两个三角端面和三个侧面，端视投影可能像房子或五边形；'
                  '六棱柱有六边形端面；菱面体的面是倾斜菱形，不能只凭投影六边形认作六棱柱。'
                  '区分三维实体和二维截面；看不到关键面时返回不确定，不要猜。'
                  '镂空指真实开孔或中空，不要把黑色印字和阴影当孔。'
                  '只输出 JSON，不输出 Markdown：'
                  '{"shape":"候选名","hollow":"实心/镂空/不确定",'
                  '"color":"主体颜色","visibility":"完整/遮挡/不确定","reason":"简短依据"}。'
                  'shape 候选：' + '、'.join(allowed))
        images = [image_b64(top)]
        if side is not None:
            images.append(image_b64(side))
        if backend == 'local':
            url = self.cfg['local_url'].rstrip('/') + '/api/chat'
            schema = {'type': 'object', 'properties': {
                'shape': {'type': 'string', 'enum': allowed},
                'hollow': {'type': 'string', 'enum': ['实心', '镂空', '不确定']},
                'color': {'type': 'string'},
                'visibility': {'type': 'string', 'enum': ['完整', '遮挡', '不确定']},
                'reason': {'type': 'string'}},
                'required': ['shape', 'hollow', 'color', 'visibility', 'reason'], 'additionalProperties': False}
            payload = {'model': self.cfg['local_model'], 'stream': False, 'format': schema,
                       'think': False, 'options': {'temperature': 0, 'num_predict': 400},
                       'messages': [{'role': 'user', 'content': prompt, 'images': images}]}
            headers = {}
        elif backend == 'cloud':
            key = os.environ.get(self.cfg['key_env'], '').strip()
            if not key:
                raise RuntimeError('未配置环境变量 ' + self.cfg['key_env'])
            url = self.cfg['cloud_url'].rstrip('/') + '/chat/completions'
            if not url.startswith('https://'):
                raise ValueError('云端接口必须使用 HTTPS')
            content = [{'type': 'text', 'text': prompt}] + [
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + im}}
                for im in images]
            payload = {'model': self.cfg['cloud_model'], 'temperature': 0, 'max_tokens': 300,
                       'enable_thinking': False,
                       'messages': [{'role': 'user', 'content': content}]}
            headers = {'Authorization': 'Bearer ' + key}
        else:
            raise ValueError('请选择 local 或 cloud')
        headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=float(self.cfg['model_timeout'])) as response:
                raw = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            # Provider error bodies can contain request metadata; never log the request/key.
            raise RuntimeError(f'视觉服务 HTTP {exc.code}；检查模型、区域地址和账号权限') from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f'视觉服务连接失败：{type(exc).__name__}') from None
        data = json.loads(raw)
        if backend == 'local':
            answer = data.get('message', {}).get('content', '')
        else:
            choices = data.get('choices') or []
            if not choices:
                raise ValueError('视觉服务返回空结果')
            answer = choices[0]['message']['content']
        if not answer and data.get('done_reason') == 'length':
            raise ValueError('模型输出预算耗尽；请选择 instruct 版本，避免 Thinking 版本没有最终答案')
        result = parse_result(answer, allowed)
        result.update(source=backend, model_ms=round((time.perf_counter() - start) * 1000, 1))
        return result
