from __future__ import annotations

import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'
CONFIG = ROOT / 'config.json'
DEFAULTS = {
    'camera': {'mode': 'remote', 'host': '192.168.100.198', 'port': 5057,
               'top': 1, 'side': 0, 'width': 1280, 'height': 720,
               'top_points': [], 'side_points': []},
    'vision': {'backend': 'geometry', 'segmentation': 'contour',
               'sam_model': str(ROOT.parent / '01-交付包/vision/models/sam_b.pt'),
               'min_area': 350, 'interval': 1.0, 'stale_seconds': 5.0,
               'side_pairing': False, 'side_axis': 'u', 'side_flip': False,
               'local_url': 'http://127.0.0.1:11434', 'local_model': 'qwen3-vl:4b-instruct',
               'cloud_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
               'cloud_model': 'qwen3-vl-plus', 'key_env': 'DASHSCOPE_API_KEY',
               'model_timeout': 180, 'cache_seconds': 10,
               'shapes': ['正方体', '长方体', '三棱柱', '五棱柱', '六棱柱',
                          '圆柱', '球', '圆锥', '棱锥', '菱面体', '其他几何体', '不确定']},
    'robot': {'endpoint': 'tcp://192.168.100.198:5058', 'simulated': True,
              'counts_per_pulse': 20.48, 'position_age': 2.0,
              'limits': {}, 'rpm': {'1': 300, '3': 120, '4': 120, '5': 120},
              'tolerance_counts': 200, 'move_timeout': 120,
              'reference_confirmed': False, 'calibration_confirmed': False,
              'affine': None,
              'route': {'p0_x': 0, 'p0_y': 0, 'p1_y': -13144,
                        'z_low': 0, 'z_high': -152000, 'z_pick': 0},
              'settle_seconds': 0.7, 'verify_pick': True, 'max_items': 12,
              'display_required': True, 'display_host': '', 'display_port': 5059},
}


def merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in extra.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load(path=CONFIG):
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('配置必须是 JSON 对象')
    return merge(DEFAULTS, data)


def save(data, path=CONFIG):
    path = Path(path)
    # Reference and arming are session facts, never restored after restarting.
    data = copy.deepcopy(data)
    data['robot']['reference_confirmed'] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def demo_robot_config():
    cfg = copy.deepcopy(DEFAULTS['robot'])
    cfg.update(simulated=True, reference_confirmed=True, calibration_confirmed=True,
               simulation_only=True,
               display_required=False,
               limits={'1': [-160000, 500], '3': [-4000, 4000],
                       '4': [-1000, 12000], '5': [-55000, 500]},
               affine={'a': 8000, 'b': 0, 'c': 0, 'd': -30000, 'tx': 1000, 'ty': -18000})
    return cfg
