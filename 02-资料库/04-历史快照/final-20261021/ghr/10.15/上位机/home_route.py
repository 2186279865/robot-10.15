"""开机回基准（固定路线 → 终点清零）。

现场用法：
    1) 断电重启后，先把龙门**手动**推到固定的“开机位”（地面/机架上的标记，X/Y 都对齐）；
    2) 打开上位机 → 连接 → 点顶栏「开机回基准」；
    3) 机器按 `home_route.json` 里的 steps 依次相对走（默认 X 走、Y 走、Z 抬一点）；
    4) 走完后对上 `zero_axes` 里的轴发 `motor_zero`，并把软位置置 0、标记为“已建立基准”；
    5) 之后再做视觉标定/抓取——所有 u/v↔脉冲、抓取坐标都以这个终点为 0。

为什么这样做：固件的 `motor_pos(abs)` 实际是相对移动（`control_inline.h` 已知缺陷），
没有限位开关/编码器回读，所以“可重复的 0 点”只能靠“每次开机都放到同一个物理位置 + 固定路线”
来保证。路线是相对脉冲，和标定/抓取用的是同一套软位置记账。

文件：
    home_route.json   路线配置（steps / zero_axes / settle_ms / boot_tip）
    home_state.json   最近一次回基准的结果（成功时间、步数、终点清零的轴）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH = ROOT / 'home_route.json'
STATE_PATH = ROOT / 'home_state.json'

PULSES_PER_REV = 3200.0
AXES = (1, 3, 4, 5)

DEFAULTS = {
    'enabled': True,
    'boot_tip': '先把龙门手动推到固定“开机位”（X/Y 都对上标记），再点「开机回基准」',
    'settle_ms': 800,
    'steps': [
        {'axis': 4, 'dir': 0, 'pulses': 3000, 'rpm': 60, 'acc': 100},
        {'axis': 5, 'dir': 0, 'pulses': 3000, 'rpm': 60, 'acc': 100},
        {'axis': 1, 'dir': 1, 'pulses': 1000, 'rpm': 40, 'acc': 100},
    ],
    # 走到终点后，把这些轴清零（软位置置 0 + motor_zero）
    'zero_axes': [4, 5],
}


class RouteError(ValueError):
    """路线配置不合法。"""


def _merge(base: dict, override: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def normalize(cfg: dict | None) -> dict:
    cfg = _merge(DEFAULTS, cfg or {})
    steps = []
    for i, raw in enumerate(cfg.get('steps') or []):
        if not isinstance(raw, dict):
            raise RouteError('第 %d 步不是对象' % (i + 1))
        try:
            axis = int(raw.get('axis'))
            direction = int(raw.get('dir', 0))
            pulses = int(raw.get('pulses', 0))
            rpm = int(raw.get('rpm', 60))
            acc = int(raw.get('acc', 100))
        except (TypeError, ValueError):
            raise RouteError('第 %d 步参数不是整数' % (i + 1)) from None
        if axis not in AXES:
            raise RouteError('第 %d 步轴号 %s 不合法（只能是 1/3/4/5）' % (i + 1, axis))
        if direction not in (0, 1):
            raise RouteError('第 %d 步方向只能是 0/1' % (i + 1))
        if pulses < 0:
            raise RouteError('第 %d 步脉冲数不能为负（方向单独给）' % (i + 1))
        if not 1 <= rpm <= 6000:
            raise RouteError('第 %d 步转速要在 1~6000 rpm' % (i + 1))
        if not 0 <= acc <= 255:
            raise RouteError('第 %d 步加速度要在 0~255' % (i + 1))
        steps.append({'axis': axis, 'dir': direction, 'pulses': pulses,
                      'rpm': rpm, 'acc': acc})
    if cfg['enabled'] and not [s for s in steps if s['pulses'] > 0]:
        raise RouteError('启用了开机回基准，但 steps 里没有任何要走的步')
    zero = []
    for raw in cfg.get('zero_axes') or []:
        try:
            axis = int(raw)
        except (TypeError, ValueError):
            raise RouteError('zero_axes 里 %r 不是轴号' % (raw,)) from None
        if axis not in AXES:
            raise RouteError('zero_axes 里 %s 不合法' % axis)
        if axis not in zero:
            zero.append(axis)
    cfg['steps'] = steps
    cfg['zero_axes'] = zero
    cfg['settle_ms'] = max(0, int(cfg.get('settle_ms', 800)))
    return cfg


def load(path=None) -> dict:
    """读配置；文件不存在时返回默认值（不写盘）。"""
    path = Path(path) if path else PATH
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return normalize(None)
    except (OSError, ValueError):
        return normalize(None)
    return normalize(raw if isinstance(raw, dict) else None)


def save(cfg: dict, path=None) -> Path:
    path = Path(path) if path else PATH
    data = normalize(cfg)
    data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)
    return path


def write_default_if_missing(path=None) -> Path:
    path = Path(path) if path else PATH
    if not path.exists():
        save(DEFAULTS, path)
    return path


def step_ms(step: dict, settle_ms: int = 0) -> int:
    """估算一步要走多久（匀速 + 加速 + 收尾冗余）。"""
    per_s = max(1, int(step.get('rpm', 60))) * PULSES_PER_REV / 60.0
    travel = int(step.get('pulses', 0)) / per_s * 1000.0
    accel = 250 + 2 * int(step.get('acc', 100))
    return int(max(600, travel + accel + 600 + settle_ms))


def describe(cfg: dict) -> list[str]:
    """给确认框/日志用的可读路线描述。"""
    names = {1: 'Z(1)', 3: '托盘(3)', 4: 'X(4)', 5: 'Y(5)'}
    lines = []
    for i, s in enumerate(cfg.get('steps') or [], 1):
        if s['pulses'] <= 0:
            continue
        lines.append('  %d) %s %s %d 脉冲（%drpm，加速度 %d）'
                     % (i, names.get(s['axis'], s['axis']),
                        '正' if s['dir'] == 0 else '负', s['pulses'], s['rpm'], s['acc']))
    return lines


def load_state(path=None) -> dict:
    path = Path(path) if path else STATE_PATH
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def save_state(ok: bool, zero_axes, steps_done: int, path=None, note: str = '') -> Path:
    path = Path(path) if path else STATE_PATH
    data = {
        'ok': bool(ok),
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'ts': time.time(),
        'zero_axes': [int(a) for a in (zero_axes or [])],
        'steps_done': int(steps_done),
        'note': note,
    }
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)
    return path


def state_text(state: dict | None) -> str:
    if not state or not state.get('ok'):
        return '基准：未建立（先放开机位，点「开机回基准」）'
    return '基准：已建立 %s' % (state.get('updated_at') or '')
