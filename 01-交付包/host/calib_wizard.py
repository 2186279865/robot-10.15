"""X/Y 与视觉 u/v 标定的“无界面逻辑”（可在无相机、无硬件、无窗口时测试）。

顶视识别给的是托盘归一化坐标 (u, v)（0~1），机器人要的是 X/Y 脉冲：

    PX = a*u + b*v + tx
    PY = c*u + d*v + ty

三个不同的 (u, v) 对应点就能解出 6 个未知数（a/b/c/d/tx/ty），
反解与残差在 `new_eye/tray_calib.py`；本模块负责流程与检查：

1. `list_targets()` / `read_target()`  取最新视觉结果（可指定编号）；
2. `measure_points()`                  把“基准 / X 走 N 脉冲 / Y 走 N 脉冲”组成标定点；
3. `check_points()`                    检查三个点是否真的分开了（视觉没动/只沿一条线动要拒绝）；
4. `solve()`                           反解仿射、算残差与“每脉冲对应多少 u/v”，可写文件并备份旧标定；
5. `report()`                          生成给界面看的多行报告（含方向检查提示）。

Tk 对话框在 `calib_dialog.py`，只负责发运动命令和把它们按顺序串起来。
"""
from __future__ import annotations

import importlib.util
import json
import math
import shutil
import time
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent
from vision_paths import (  # noqa: E402  （放在常量定义处，保持原有变量名不变）
    CALIB_MARKS as MARKS_PATH,
    LATEST_TARGETS as VISION_JSON,
    TRAY_CALIB as CALIB_PATH,
    TRAY_CALIB_PY as _TRAY_CALIB_PY,
)

#: 视觉约 1 fps；标定用的点也应该是刚拍的，太旧就不要（工件可能已被碰走）
MAX_AGE_S = 15.0

#: 没有指定编号时，取离托盘中心最近的目标
CENTER = (0.5, 0.5)

#: 三个点至少要在画面里分开这么多（归一化距离），否则解出来的方向不可信
MIN_UV_DELTA = 0.008

#: 编码器回读单位：驱动器 65536 计数/圈；位置指令单位：3200 脉冲/圈
COUNTS_PER_REV = 65536.0
PULSES_PER_REV = 3200.0


def jog_direction(sign, flip=False):
    """点动方向：sign>0 为“正”（命令 dir=0），flip=True 时反过来。

    固件 `motor_pos(axis, dir 0|1, rpm, acc, pulses, mode)` 里脉冲只表大小，
    方向是独立的 dir 字段，所以这里只算 dir。
    """
    return 1 if (sign < 0) != bool(flip) else 0


def counts_to_pulses(counts, counts_per_rev=COUNTS_PER_REV, pulses_per_rev=PULSES_PER_REV):
    """编码器计数 → 位置指令脉冲（3200/圈）。手推模式用它把回读换算成“脉冲坐标”。"""
    return float(counts) / float(counts_per_rev) * float(pulses_per_rev)


#: 回读轴号：X=4、Y=5（与 `protocol.VALID_AXES` 一致）
READBACK_AXES = {'x': 4, 'y': 5}
#: 回读值多久内算“新鲜”（毫秒）；超时就用软位置
READBACK_MAX_AGE_MS = 2500


def pick_pulses(soft, readback, now_ms, max_age_ms=READBACK_MAX_AGE_MS,
                counts_per_rev=COUNTS_PER_REV, pulses_per_rev=PULSES_PER_REV):
    """选“记录爪位”用的脉冲来源。

    `soft`：(px, py) 软位置（相对走累加，没有回读时靠它）。
    `readback`：{'x': {'counts': int, 'ts': ms}|None, 'y': ...}（来自 `motor_query pos`）。
    返回 {'px','py','source'}；`source` 为 'readback'（回读新鲜、两轴都有）或 'soft'。
    """
    px, py = float(soft[0]), float(soft[1])
    rb = readback or {}
    pair = (rb.get('x') or {}, rb.get('y') or {})
    ok = True
    for item in pair:
        counts = item.get('counts')
        ts = item.get('ts')
        if counts is None or ts is None or now_ms - int(ts) > max_age_ms:
            ok = False
            break
    if ok:
        px = counts_to_pulses(pair[0]['counts'], counts_per_rev, pulses_per_rev)
        py = counts_to_pulses(pair[1]['counts'], counts_per_rev, pulses_per_rev)
        return {'px': px, 'py': py, 'source': 'readback'}
    return {'px': px, 'py': py, 'source': 'soft'}


def normalize_points(points, travel=None, labels=('X', 'Y')):
    """把"混了单位"的标定点自动归一化到脉冲。

    实测里最常见的错：一根轴记的是软位置（脉冲），另一根记的是编码器
    counts（1 脉冲 = 20.48 counts）。判据：某轴记录跨度超过该轴实测行程
    的 1.5 倍，就把它按 counts 处理（除以 20.48）。

    返回 (points2, notes)；notes 是给界面显示的中文说明。
    """
    pts = [dict(p) for p in points]
    notes = []
    if not travel:
        return pts, notes
    for key, axis, name in (('px', 4, labels[0]), ('py', 5, labels[1])):
        lo, hi = travel.get(axis, (None, None))
        if lo is None or hi is None:
            continue
        vals = [float(p.get(key, 0.0)) for p in pts]
        span = max(vals) - min(vals)
        allow = abs(hi - lo) * 1.5 + 200.0
        if span > allow:
            k = float(COUNTS_PER_REV) / float(PULSES_PER_REV)
            for pt in pts:
                pt[key] = float(pt.get(key, 0.0)) / k
            notes.append('%s 的记录跨度 %.0f 超过行程 %.0f 的 1.5 倍，已按编码器 counts 换算成脉冲（除以 %.2f）'
                         % (name, span, abs(hi - lo), k))
    return pts, notes


def wrap_delta(prev_counts, now_counts, counts_per_rev=COUNTS_PER_REV):
    """单圈绝对编码器：两次回读之差，按“最短路径”折算到 (-半圈, +半圈]。

    驱动器回读是单圈计数时，跨零点要用这个；如果一次推动超过半圈，
    结果是歧义的，交给 `hand_points()` 报错，让人中途多记一个点。
    """
    full = float(counts_per_rev)
    delta = float(now_counts) - float(prev_counts)
    return (delta + full / 2.0) % full - full / 2.0


def hand_points(samples, counts_per_rev=COUNTS_PER_REV, pulses_per_rev=PULSES_PER_REV,
                max_delta_ratio=0.45):
    """手动推动的采样 → 标定点。

    samples: [{'u','v','counts_x','counts_y'}, ...]，至少 3 个；第一个当基准(0,0)。
    每个采样记录：视觉 u/v + X/Y 两个轴的编码器计数。
    返回的 points 里 px/py 是以第一个采样为原点的“位置指令脉冲”坐标（可为负）。
    """
    if len(samples) < 3:
        raise ValueError(f'至少要 3 个采样点（现在 {len(samples)} 个）')
    for index, item in enumerate(samples):
        for key in ('u', 'v', 'counts_x', 'counts_y'):
            if key not in item:
                raise ValueError(f'第 {index + 1} 个采样缺少 {key}')
    full = float(counts_per_rev)
    points = [{'u': float(samples[0]['u']), 'v': float(samples[0]['v']),
               'px': 0.0, 'py': 0.0}]
    px = py = 0.0
    prev = samples[0]
    for index, sample in enumerate(samples[1:], start=2):
        dx = wrap_delta(prev['counts_x'], sample['counts_x'], counts_per_rev)
        dy = wrap_delta(prev['counts_y'], sample['counts_y'], counts_per_rev)
        for name, delta in (('X', dx), ('Y', dy)):
            if abs(delta) > full * max_delta_ratio:
                raise ValueError(
                    f'{name} 轴从第 {index - 1} 点到第 {index} 点转了将近半圈（'
                    f'{delta:+.0f} counts）：中途多记一个点，或分两次短距离推动')
        px += counts_to_pulses(dx, counts_per_rev, pulses_per_rev)
        py += counts_to_pulses(dy, counts_per_rev, pulses_per_rev)
        points.append({'u': float(sample['u']), 'v': float(sample['v']),
                       'px': px, 'py': py})
        prev = sample
    return points


def _tray_calib():
    """优先用 sys.path 里的 tray_calib（测试会把 vision/ 加进 sys.path）；否则按文件路径加载。"""
    try:
        import tray_calib  # noqa: F401  (测试环境)
        return tray_calib
    except ImportError:
        path = _TRAY_CALIB_PY
        spec = importlib.util.spec_from_file_location('tray_calib_wizard', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _load(path, max_age_s, now):
    path = Path(path)
    if not path.exists():
        raise ValueError(f'还没有视觉结果：{path}\n先在 new_eye 里点“单次识别”')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') not in (4, 5):
        raise ValueError('视觉数据格式不对（需要 new_eye 的 schema 4/5）')
    if data.get('motion_authorized') is not False:
        raise ValueError('只接受未授权运动的目标（motion_authorized 必须为 false）')
    ts = float(data.get('ts') or 0.0)
    if ts <= 0:
        raise ValueError('视觉数据缺少时间戳 ts')
    now = time.time() if now is None else float(now)
    age = now - ts
    if age > max_age_s:
        raise ValueError(f'视觉数据已过期 {age:.0f}s（阈值 {max_age_s:.0f}s），请重新识别')
    return data, age


def list_targets(path=VISION_JSON, max_age_s=MAX_AGE_S, now=None):
    """列出当前视觉结果里可用的点：[{'id','label','u','v','class_name','views'}]。"""
    data, _age = _load(path, max_age_s, now)
    targets = data.get('targets') or []
    if not targets:
        raise ValueError('视觉结果里没有目标：先把物块放到画面里再识别')
    out = []
    for target in targets:
        uv = target.get('center_tray')
        if not (isinstance(uv, (list, tuple)) and len(uv) == 2):
            continue
        u, v = float(uv[0]), float(uv[1])
        if not (math.isfinite(u) and math.isfinite(v)):
            continue
        out.append({'id': target.get('id'), 'label': _label(target), 'u': u, 'v': v,
                    'class_name': target.get('class_name'), 'views': target.get('views')})
    if not out:
        raise ValueError('视觉结果里的目标都缺 center_tray，无法标定')
    return out


def read_target(path=VISION_JSON, prefer=None, max_age_s=MAX_AGE_S, now=None):
    """读最新视觉结果里的一个点，返回 (u, v, label)。

    prefer: 目标 id（如 3 或 '3'），优先取它；否则取离托盘中心 (0.5, 0.5) 最近的。
    """
    targets = list_targets(path, max_age_s=max_age_s, now=now)
    if prefer is not None and str(prefer).strip() != '':
        for item in targets:
            if str(item['id']) == str(prefer).strip():
                return item['u'], item['v'], item['label']
        raise ValueError(f'视觉结果里没有编号 {prefer} 的目标（当前有：'
                         + ', '.join(str(item['id']) for item in targets) + '）')
    best = min(targets, key=lambda item: (item['u'] - CENTER[0]) ** 2
               + (item['v'] - CENTER[1]) ** 2)
    return best['u'], best['v'], best['label']


def _label(target):
    name = target.get('class_name') or target.get('class_id') or '未知'
    return f"#{target.get('id')} {name}"


def measure_points(base, x, y, pulses):
    """组三组标定点：基准(0,0) / X 走 pulses / Y 走 pulses。

    base/x/y 都是 (u, v)，pulses 是正整数（相对移动量）。
    """
    pulses = int(pulses)
    if pulses <= 0:
        raise ValueError('标定脉冲数必须是正整数')
    u0, v0 = (float(val) for val in base)
    u1, v1 = (float(val) for val in x)
    u2, v2 = (float(val) for val in y)
    return [
        {'u': u0, 'v': v0, 'px': 0.0, 'py': 0.0},
        {'u': u1, 'v': v1, 'px': float(pulses), 'py': 0.0},
        {'u': u2, 'v': v2, 'px': 0.0, 'py': float(pulses)},
    ]



def _lsq_affine(points):
    """多点最小二乘拟合 u,v = A*[px,py] + c，返回 (cu, cv) 每组 3 个系数。"""
    A = [[float(p['px']), float(p['py']), 1.0] for p in points]
    n = len(A)
    M = [[sum(A[k][i] * A[k][j] for k in range(n)) for j in range(3)] for i in range(3)]

    def det(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))

    D = det(M)
    if abs(D) < 1e-12:
        return None
    out = []
    for col in (0, 1):
        target = [float(p['u' if col == 0 else 'v']) for p in points]
        b = [sum(A[k][i] * target[k] for k in range(n)) for i in range(3)]
        coef = []
        for i in range(3):
            Mi = [row[:] for row in M]
            for r in range(3):
                Mi[r][i] = b[r]
            coef.append(det(Mi) / D)
        out.append(coef)
    return out


def _check_multi(points, min_uv=MIN_UV_DELTA, travel=None, labels=('X', 'Y')):
    """多点（>=4）标定点的工程检查：拟合出一对方向向量再判。"""
    problems = []
    xs = [float(p['px']) for p in points]
    ys = [float(p['py']) for p in points]
    fit = _lsq_affine(points)
    if fit is None:
        return ['脉冲坐标退化（所有点在同一条线上）：X/Y 要分别动出两个方向']
    cu, cv = fit
    jx = (cu[0], cv[0])
    jy = (cu[1], cv[1])
    nx = math.hypot(*jx)
    ny = math.hypot(*jy)
    if nx < 1e-9 or ny < 1e-9:
        problems.append('有一根轴在画面里几乎不动：检查是不是只动了一个轴，或某个轴没记录到移动')
    else:
        cos = (jx[0] * jy[0] + jx[1] * jy[1]) / (nx * ny)
        if abs(cos) > 0.985:
            problems.append(f'X 与 Y 在画面里几乎同方向（夹角 {math.degrees(math.acos(max(-1, min(1, cos)))):.1f}°）：'
                            '检查是不是两个轴都被同一个方向影响，或点没分开')
    # 行程合理性：超过实测行程太多，多半是把编码器 counts 当成脉冲记了
    if travel:
        for axis, name, vals in ((4, labels[0], xs), (5, labels[1], ys)):
            lo, hi = travel.get(axis, (None, None))
            if lo is None or hi is None:
                continue
            span = max(vals) - min(vals)
            allow = abs(hi - lo) * 1.5 + 200
            if span > allow:
                problems.append(
                    f'{name} 记录到的跨度 {span:.0f} 超过实测行程 {abs(hi - lo):.0f} 脉冲的 1.5 倍：'
                    f'像是把编码器 counts（1 脉冲=20.48 counts）当脉冲记了。请用「相对走」小步重记，'
                    f'并确认「记录爪位」日志里的来源是「软位置」或「回读」')
    if not problems:
        # 画面跨度过小（几乎没动）也挡掉
        if max(xs) - min(xs) < 1 and max(ys) - min(ys) < 1:
            problems.append('所有点的脉冲都一样（没记录到移动）')
    return problems

def check_points(points, min_uv=MIN_UV_DELTA, labels=('X', 'Y'), travel=None):
    """检查三个点够不够“张开”：返回问题列表（空=可以用）。

    labels: (第二个点的名字, 第三个点的名字)，自动走标定时是 ('X','Y')，
            手推标定时传 ('第 2 点','第 3 点')，只影响提示文字。
    """
    problems = []
    if len(points) < 3:
        return ['至少要 3 个点（基准 + X + Y）']
    if len(points) > 3:
        # 手推/多点模式：点不是「基准 + 只动 X + 只动 Y」，要用全部点拟合。
        # 先做单位归一化（把误记成 counts 的轴换算回脉冲），再做几何检查。
        pts, _notes = normalize_points(points, travel=travel, labels=labels)
        return _check_multi(pts, min_uv=min_uv, travel=None, labels=labels)
    auto = tuple(labels) == ('X', 'Y')
    base, x, y = points[0], points[1], points[2]
    dx_u, dx_v = x['u'] - base['u'], x['v'] - base['v']
    dy_u, dy_v = y['u'] - base['u'], y['v'] - base['v']
    if abs(dx_u) + abs(dx_v) < min_uv:
        if auto:
            head = f'X 走完后视觉几乎没动（Δu={dx_u:+.3f} Δv={dx_v:+.3f}）：'
        else:
            head = (f'{labels[0]} 相对基准视觉几乎没动（Δu={dx_u:+.3f} Δv={dx_v:+.3f}）：')
        problems.append(head + '检查物块是否被碰走、轴是否真动了、目标编号是否选对')
    if abs(dy_u) + abs(dy_v) < min_uv:
        if auto:
            head = f'Y 走完后视觉几乎没动（Δu={dy_u:+.3f} Δv={dy_v:+.3f}）：'
        else:
            head = (f'{labels[1]} 相对基准视觉几乎没动（Δu={dy_u:+.3f} Δv={dy_v:+.3f}）：')
        problems.append(head + '检查轴是否真动了、目标编号是否选对')
    # 两个方向在画面里的夹角太小 → 仿射矩阵病态（tray_calib.solve 也会拒绝）
    norm_x = math.hypot(dx_u, dx_v)
    norm_y = math.hypot(dy_u, dy_v)
    if norm_x > 1e-6 and norm_y > 1e-6:
        cos = (dx_u * dy_u + dx_v * dy_v) / (norm_x * norm_y)
        if abs(cos) > 0.985:
            problems.append(f'X 与 Y 在画面里几乎同方向（夹角 {math.degrees(math.acos(max(-1, min(1, cos)))):.1f}°）：'
                            f'检查是不是两个轴都被同一个方向影响，或点没分开')
    # 脉冲是否真的分成了两个方向（最常见的手误：全程只动了一个轴，或根本没记录到移动）
    coords = {(round(item['px'], 1), round(item['py'], 1)) for item in points}
    if len(coords) == 1:
        problems.append('所有点的脉冲都一样（没有记录到移动）：确认用的是单轴页「▶ 相对走」，'
                        '并且是移动之后才点「② 记录坐标」；可点「读取当前脉冲」看软位置是否在变')
    elif not problems:
        sx = len({round(item['px'], 1) for item in points}) == 1
        sy = len({round(item['py'], 1) for item in points}) == 1
        if sx and not sy:
            problems.append('只有 Y 动过（X 一直是同一个值）：再走一段 X 并记一个点')
        if sy and not sx:
            problems.append('只有 X 动过（Y 一直是同一个值）：再走一段 Y 并记一个点')
    return problems


def solve(points, path=CALIB_PATH, write=False, backup=True, min_uv=MIN_UV_DELTA,
          labels=('X', 'Y'), travel=None):
    """反解仿射，返回报告用 dict；write=True 时写标定文件（默认备份旧文件）。

    travel: {轴号: (min,max)} 软限位（脉冲），用于抓「把 counts 当脉冲记」这种错。
    """
    pts, notes = normalize_points(points, travel=travel, labels=labels)
    problems = check_points(pts, min_uv=min_uv, labels=labels, travel=None)
    if problems:
        raise ValueError('标定点不合格：\n· ' + '\n· '.join(problems))
    points = pts
    tray = _tray_calib()
    affine = tray.solve(points)
    res = tray.residuals(points, affine)
    max_dx = max(abs(item['dx_pulse']) for item in res)
    max_dy = max(abs(item['dy_pulse']) for item in res)
    # 反解矩阵：把“脉冲”换回 u/v 的变化率，便于在界面上显示方向
    a, b, c, d = (affine[key] for key in ('a', 'b', 'c', 'd'))
    det = a * d - b * c
    per_pulse = {
        'x': {'du': d / det, 'dv': -c / det},
        'y': {'du': -b / det, 'dv': a / det},
    }
    pred = []
    for point in points:
        px, py = _tray_calib().uv_to_pulse(point['u'], point['v'], affine)
        pred.append({'u': point['u'], 'v': point['v'],
                     'px': float(point['px']), 'py': float(point['py']),
                     'px_pred': px, 'py_pred': py})
    result = {'affine': affine, 'residuals': res, 'predict': pred,
              'notes': notes,
              'max_abs_pulse': {'dx': max_dx, 'dy': max_dy},
              'per_pulse': per_pulse, 'path': None, 'backup': None,
              'points': points}
    if write:
        if backup:
            result['backup'] = str(_backup(path))
        saved = tray.save(affine, path=path, points=points)
        result['path'] = str(saved)
    return result


def _backup(path):
    path = Path(path)
    if not path.exists():
        return None
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dst = path.with_name(f'{path.stem}.{stamp}.bak.json')
    shutil.copyfile(path, dst)
    return dst


def solve_hand(samples, path=CALIB_PATH, write=False, backup=True, min_uv=MIN_UV_DELTA):
    """手推标定：采样 → 标定点 → 反解。samples 见 `hand_points()`。"""
    points = hand_points(samples)
    return solve(points, path=path, write=write, backup=backup, min_uv=min_uv,
                 labels=('第 2 点', '第 3 点'))


def vision_ts(path=VISION_JSON):
    """最新视觉结果的时间戳；文件不存在/解析失败返回 0。

    电机带动模式靠它判断“画面有没有更新”：每走完一步，要等一个比走之前
    **更新**的帧才记录 u/v，否则记到的还是走之前的位置。
    """
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return float(data.get('ts') or 0.0)
    except (OSError, ValueError, TypeError):
        return 0.0


def pulse_points(samples):
    """电机带动模式的采样 → 标定点：samples 里已带 px/py（相对第一个点的指令脉冲）。"""
    if len(samples) < 3:
        raise ValueError(f'至少要 3 个采样点（现在 {len(samples)} 个）')
    points = []
    for index, item in enumerate(samples):
        for key in ('u', 'v', 'px', 'py'):
            if key not in item:
                raise ValueError(f'第 {index + 1} 个采样缺少 {key}')
        points.append({'u': float(item['u']), 'v': float(item['v']),
                       'px': float(item['px']), 'py': float(item['py'])})
    return points


def solve_pulse(samples, path=CALIB_PATH, write=False, backup=True, min_uv=MIN_UV_DELTA):
    """电机带动模式：采样(带 px/py) → 标定点 → 反解。"""
    points = pulse_points(samples)
    return solve(points, path=path, write=write, backup=backup, min_uv=min_uv,
                 labels=('第 2 点', '第 3 点'))


def solve_ortho(points, path=CALIB_PATH, write=False, backup=True, min_uv=MIN_UV_DELTA):
    """简化标定：假设「X 只影响 u、Y 只影响 v」（相机装正、平台正交）。

    好处：不会出现“矩阵奇异/点共线”。只需要两个动作——一次只走 X、一次只走 Y。
    返回结构与 `solve()` 一致，另外带 'mode'='ortho' 与 'coupling' 旋转检查。
    """
    if len(points) < 3:
        raise ValueError(f'至少要 3 个点（现在 {len(points)} 个）')
    base = points[0]
    xp = max(points[1:], key=lambda item: abs(item['px'] - base['px']))
    yp = max(points[1:], key=lambda item: abs(item['py'] - base['py']))
    dpx, du = xp['px'] - base['px'], xp['u'] - base['u']
    dpy, dv = yp['py'] - base['py'], yp['v'] - base['v']
    if abs(dpx) < 1:
        raise ValueError('所有点的 X 脉冲都一样：先用单轴页走一段 X，再记一个点')
    if abs(dpy) < 1:
        raise ValueError('所有点的 Y 脉冲都一样：先用单轴页走一段 Y，再记一个点')
    if abs(du) < min_uv:
        raise ValueError(f'走 X 后 u 几乎没变（Δu={du:+.3f}）：检查物块是否被碰、相机是否装反、'
                         f'目标编号是否选对')
    if abs(dv) < min_uv:
        raise ValueError(f'走 Y 后 v 几乎没变（Δv={dv:+.3f}）：检查物块是否被碰、相机是否装反、'
                         f'目标编号是否选对')
    a = dpx / du
    d = dpy / dv
    affine = {'a': a, 'b': 0.0, 'c': 0.0, 'd': d,
              'tx': base['px'] - a * base['u'],
              'ty': base['py'] - d * base['v']}
    tray = _tray_calib()
    res = tray.residuals(points, affine)
    pred = []
    for point in points:
        px, py = tray.uv_to_pulse(point['u'], point['v'], affine)
        pred.append({'u': point['u'], 'v': point['v'],
                     'px': float(point['px']), 'py': float(point['py']),
                     'px_pred': px, 'py_pred': py})
    coupling = {'x_du': du, 'x_dv': xp['v'] - base['v'],
                'y_dv': dv, 'y_du': yp['u'] - base['u']}
    result = {'affine': affine, 'residuals': res, 'predict': pred,
              'max_abs_pulse': {
                  'dx': max(abs(item['dx_pulse']) for item in res),
                  'dy': max(abs(item['dy_pulse']) for item in res)},
              'per_pulse': {'x': {'du': 1.0 / a, 'dv': 0.0},
                            'y': {'du': 0.0, 'dv': 1.0 / d}},
              'path': None, 'backup': None, 'points': points,
              'mode': 'ortho', 'coupling': coupling}
    if write:
        if backup:
            result['backup'] = str(_backup(path))
        saved = tray.save(affine, path=path, points=points)
        result['path'] = str(saved)
    return result


def report(result, pulses):
    """生成给界面看的多行报告：方向、每脉冲变化率、走 N 脉冲的实际位移、残差。"""
    per = result['per_pulse']
    lines = []
    if result.get('mode') == 'ortho':
        lines.append('简化标定结果（假设 X 只影响左右 u、Y 只影响前后 v）')
    lines.append('换算率（每 1 脉冲）与方向：')
    for axis, key in (('X 轴', 'x'), ('Y 轴', 'y')):
        du, dv = per[key]['du'], per[key]['dv']
        if abs(du) >= abs(dv):
            trend = 'u 增大' if du > 0 else 'u 减小'
        else:
            trend = 'v 增大' if dv > 0 else 'v 减小'
        lines.append(f'  {axis}正脉冲 → Δu {du:+.5f}、Δv {dv:+.5f}  （主要让 {trend}）')
        lines.append(f'      走 {pulses} 脉冲 ≈ Δu {du * pulses:+.3f}、Δv {dv * pulses:+.3f}')
    lines.append('方向检查：')
    problems = []
    if abs(per['x']['dv']) > abs(per['x']['du']):
        problems.append('X 正脉冲对 v 的影响大于 u：检查 X 轴是否装反/轴号是否对调')
    if abs(per['y']['du']) > abs(per['y']['dv']):
        problems.append('Y 正脉冲对 u 的影响大于 v：检查 Y 轴是否装反/轴号是否对调')
    if problems:
        lines.extend('  ⚠ ' + item for item in problems)
    else:
        lines.append('  ✓ 两轴各自主要影响一个画面方向，符合“X→左右、Y→前后”的预期')
    lines.append('残差（模型预测脉冲 - 实测脉冲）：')
    for item, res in zip(result['points'], result['residuals']):
        lines.append(f"  u={item['u']:.3f} v={item['v']:.3f} → "
                     f"ΔX {res['dx_pulse']:+.1f}、ΔY {res['dy_pulse']:+.1f} 脉冲")
    lines.append(f"  最大残差 |dx|={result['max_abs_pulse']['dx']:.1f}、"
                 f"|dy|={result['max_abs_pulse']['dy']:.1f} 脉冲")
    if result['max_abs_pulse']['dx'] > 300 or result['max_abs_pulse']['dy'] > 300:
        lines.append('  ⚠ 残差偏大（>300 脉冲）：检查视觉中心是否晃动、物块是否被夹爪影子干扰，'
                     '或脉冲数太小导致噪声占比高（可把相对脉冲提高到 5000）')
    coupling = result.get('coupling')
    if coupling:
        ratio_x = coupling['x_dv'] / coupling['x_du']
        ratio_y = coupling['y_du'] / coupling['y_dv']
        lines.append('旋转/耦合检查（简化法假设它们接近 0）：')
        lines.append(f'  走 X 时 Δv/Δu = {ratio_x:+.2f}；走 Y 时 Δu/Δv = {ratio_y:+.2f}')
        if abs(ratio_x) > 0.30 or abs(ratio_y) > 0.30:
            lines.append('  ⚠ 两轴明显不垂直或相机装歪：抓取会有固定偏移，建议改用“通用 3 点”'
                         '（把点走到斜对角再求解），或把相机转正')
        else:
            lines.append('  ✓ 两轴基本正交，简化法够用')
    return '\n'.join(lines)


def summary(result):
    """给界面用的一行摘要：方向 + 残差。"""
    per = result['per_pulse']
    rx, ry = result['max_abs_pulse']['dx'], result['max_abs_pulse']['dy']
    return (f"X 正脉冲 → u {per['x']['du']:+.2e}/脉冲、v {per['x']['dv']:+.2e}/脉冲；"
            f"Y 正脉冲 → u {per['y']['du']:+.2e}/脉冲、v {per['y']['dv']:+.2e}/脉冲；"
            f"残差 max |dx|={rx:.0f}、|dy|={ry:.0f} 脉冲")


# ---------------- 标注点法（new_eye 里点位置 → 上位机记录爪位） ----------------
def load_marks(path=None):
    """读 new_eye“标注点”文件：返回 [{u, v, x?, y?, px?, py?}, ...]，坏文件/缺失返回 []。"""
    path = MARKS_PATH if path is None else path
    try:
        data = json.loads(Path(path).read_text(encoding='utf8'))
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get('points') or []
    out = []
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict):
            continue
        try:
            rec = {'u': float(m['u']), 'v': float(m['v'])}
        except Exception:
            continue
        for key in ('x', 'y', 'px', 'py'):
            if m.get(key) is not None:
                try:
                    rec[key] = float(m[key])
                except Exception:
                    pass
        out.append(rec)
    return out


def save_marks(marks, path=None):
    """把（可能已补上 px/py 的）标注点写回文件，下次打开还能继续记录。"""
    p = Path(MARKS_PATH if path is None else path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'points': marks}
    tmp = p.with_name(p.stem + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf8')
    tmp.replace(p)
    return p


def marks_points(marks):
    """标注点 → 标定点；要求每个点都记录过爪位脉冲（px/py）。"""
    pts = []
    for i, m in enumerate(marks, 1):
        if m.get('px') is None or m.get('py') is None:
            raise ValueError('标注点 P%d 还没有记录爪位：把 X/Y 开到该点上方，再点“记录爪位”' % i)
        pts.append({'u': float(m['u']), 'v': float(m['v']),
                    'px': float(m['px']), 'py': float(m['py'])})
    if len(pts) < 3:
        raise ValueError('至少要 3 个标注点（现在 %d 个）；点要分散开，别挤在一条线上' % len(pts))
    return pts
