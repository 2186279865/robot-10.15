"""抓取点标定：手动把夹爪对准物块，抓成功后记录 (u,v) ↔ (X,Y) 脉冲。

为什么比"画面点标点、再把爪子移过去"更准：

* 画面点的 (u,v) 是**物块**的位置；爪子移过去靠肉眼对正，误差直接进标定；
* 抓取点的 (u,v) 也是物块的位置，但 (X,Y) 是**抓成功时爪子的真实位置**——
  这个位置已经用"抓起来没有"验证过，误差只剩机械对准精度；
* 而且记录时的 Z 高度就是抓取高度，没有视差问题。

流程（GUI 里点两下）：摆一件 → 单次识别 → 手动点动对准并抓成功 →
「记录抓取点」→ 换个位置再来 3~5 次 → 「抓取点求解」。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

PKG_ROOT = Path(__file__).resolve().parents[1]
PAIRS_PATH = PKG_ROOT / 'vision' / 'runtime' / 'grasp_pairs.json'

MIN_PAIRS = 3          # 3 点能解仿射，4 点以上才有冗余、能看残差
GOOD_PAIRS = 5         # 推荐 5~6 点，覆盖托盘四个角
MAX_RESIDUAL = 120.0   # 超过这个残差（脉冲）说明哪个点记错了
OUTLIER_PULSE = 60.0   # 单点残差超过它 → 求解时自动剔除（可回退）
DUP_UV = 0.02          # u/v 都差这么小的两点视为同一位置（会用新点替换旧点）


def _path(path=None) -> Path:
    return Path(path) if path else PAIRS_PATH


def load_pairs(path=None) -> List[Dict[str, float]]:
    """读已记录的抓取点；文件不存在/坏掉都返回空列表。"""
    p = _path(path)
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    pairs = data.get('pairs') if isinstance(data, dict) else data
    out: List[Dict[str, float]] = []
    for item in pairs or []:
        try:
            out.append({'u': float(item['u']), 'v': float(item['v']),
                        'px': float(item['px']), 'py': float(item['py']),
                        'ts': float(item.get('ts') or 0.0)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_pairs(pairs, path=None) -> Path:
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {'schema_version': 1, 'unit': 'pulse',
               'updated': time.strftime('%Y-%m-%dT%H:%M:%S'),
               'pairs': list(pairs)}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    return p


def add_pair(u: float, v: float, px: float, py: float,
             path=None, note: str = '') -> List[Dict[str, float]]:
    """追加一个抓取点（px/py 是脉冲），返回新的全部点。"""
    u, v = float(u), float(v)
    if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
        raise ValueError('u/v 应该在 0~1 之间（现在是 %.3f / %.3f）' % (u, v))
    pairs = load_pairs(path)
    item = {'u': u, 'v': v, 'px': float(px), 'py': float(py),
            'ts': time.time(), 'note': note}
    # 同一位置重复记录 → 替换旧点（避免把“手抖记两次”当成两个独立点）
    for i, old in enumerate(pairs):
        if abs(old['u'] - u) <= DUP_UV and abs(old['v'] - v) <= DUP_UV:
            pairs[i] = item
            save_pairs(pairs, path)
            return pairs
    pairs.append(item)
    save_pairs(pairs, path)
    return pairs


def clear(path=None) -> None:
    save_pairs([], path)


def remove_last(path=None) -> List[Dict[str, float]]:
    """删掉最后一个抓取点（记错了用）。"""
    pairs = load_pairs(path)
    if pairs:
        pairs.pop()
        save_pairs(pairs, path)
    return pairs


def preview(pairs, path=None) -> Dict[str, object]:
    """只试算不写盘：返回每个点的残差（按从大到小），用于“记完立刻知道准不准”。"""
    import calib_wizard as CW

    ok, why = spread_ok(pairs)
    if not ok:
        raise ValueError(why)
    res = CW.solve(to_points(pairs), write=False)
    rows = []
    for i, r in enumerate(res.get('residuals') or []):
        dx = float(r.get('dx_pulse') or 0.0)
        dy = float(r.get('dy_pulse') or 0.0)
        rows.append({'idx': i + 1, 'u': r.get('u'), 'v': r.get('v'),
                     'dx': dx, 'dy': dy, 'dist': (dx * dx + dy * dy) ** 0.5})
    rows.sort(key=lambda d: -d['dist'])
    return {'n': len(pairs), 'rows': rows,
            'max': rows[0]['dist'] if rows else 0.0,
            'affine': res.get('affine')}


def to_points(pairs) -> List[Dict[str, float]]:
    return [{'u': p['u'], 'v': p['v'], 'px': p['px'], 'py': p['py']} for p in pairs]


def describe(pairs) -> str:
    if not pairs:
        return '还没有记录抓取点'
    us = [p['u'] for p in pairs]
    vs = [p['v'] for p in pairs]
    xs = [p['px'] for p in pairs]
    ys = [p['py'] for p in pairs]
    return ('记录 %d 个点：u %.2f~%.2f、v %.2f~%.2f；X %+.0f~%+.0f、Y %+.0f~%+.0f 脉冲'
            % (len(pairs), min(us), max(us), min(vs), max(vs),
               min(xs), max(xs), min(ys), max(ys)))


def spread_ok(pairs) -> tuple:
    """点要分散：u、v 各自要有一定跨度，否则解出来的仿射外推会很飘。"""
    if len(pairs) < MIN_PAIRS:
        return False, '至少要 %d 个点（现在 %d 个）' % (MIN_PAIRS, len(pairs))
    du = max(p['u'] for p in pairs) - min(p['u'] for p in pairs)
    dv = max(p['v'] for p in pairs) - min(p['v'] for p in pairs)
    if du < 0.25 or dv < 0.25:
        return False, ('点太集中了（u 跨度 %.2f、v 跨度 %.2f）：'
                       '要把物块摆到托盘的两头/两侧再记' % (du, dv))
    return True, ''


def solve(pairs, path=None, write: bool = False) -> Dict[str, object]:
    """用抓取点拟合 (u,v)→(X,Y) 仿射。write=True 写 tray_calib.json（自动备份）。"""
    import calib_wizard as CW

    ok, why = spread_ok(pairs)
    if not ok:
        raise ValueError(why)
    pts = to_points(pairs)
    dropped = []
    # 先试算；若某点残差明显异常且冗余足够，剔除后重解（最多剔 2 个）
    for _ in range(2):
        if len(pts) <= 4:
            break
        try:
            probe = CW.solve(pts, write=False)
        except ValueError:
            break
        rows = []
        for i, r in enumerate(probe.get('residuals') or []):
            dx = float(r.get('dx_pulse') or 0.0)
            dy = float(r.get('dy_pulse') or 0.0)
            rows.append((((dx * dx + dy * dy) ** 0.5), i))
        if not rows:
            break
        worst, idx = max(rows)
        if worst <= OUTLIER_PULSE:
            break
        dropped.append({'u': pts[idx]['u'], 'v': pts[idx]['v'],
                        'px': pts[idx]['px'], 'py': pts[idx]['py'],
                        'residual': round(worst, 1)})
        pts.pop(idx)
    result = CW.solve(pts, write=write)
    result['dropped'] = dropped
    result['used'] = len(pts)
    result['pairs'] = len(pairs)
    result['max_abs'] = result.get('max_abs_pulse') or {}
    worst = max(result['max_abs'].get('dx', 0.0), result['max_abs'].get('dy', 0.0))
    result['worst_pulse'] = worst
    if worst > MAX_RESIDUAL:
        result['warning'] = ('残差有点大（%.0f 脉冲）：挑那个残差最大的点重记一次' % worst)
    return result
