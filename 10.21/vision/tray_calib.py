"""托盘归一化坐标 (u,v) ↔ X/Y 轴脉冲 的二维仿射标定。

视觉给出的是托盘平面归一化坐标（0~1，左上为原点，u 沿四点第 1→2 点方向，
v 沿第 1→4 点方向）。机器人动的是 X/Y 两个直线轴，二者的关系是一个仿射变换：

    px = a*u + b*v + tx
    py = c*u + d*v + ty

做法：让机械臂分别沿 X、Y 走已知脉冲，记录同一个物理点在视觉里的 (u,v) 位移，
用最小二乘解出 a,b,c,d,tx,ty。至少 3 个不同位置的对应点（含 1 个基准点），
Z 轴高度、夹爪角度、放料位置这些"示教量"不在这里，见 `vision_grasp.json`。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = ROOT / 'tray_calib.json'

KEYS = ('a', 'b', 'c', 'd', 'tx', 'ty')


class CalibError(ValueError):
    pass


def _solve3(m, rhs):
    """3x3 线性方程组，高斯消元（纯 Python，不依赖 numpy）。"""
    n = 3
    a = [[float(m[i][j]) for j in range(n)] + [float(rhs[i])] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise CalibError('标定点共线或重合，无法解出变换（请换更分散的三个点）')
        a[col], a[piv] = a[piv], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for r in range(n):
            if r != col and a[r][col]:
                f = a[r][col]
                a[r] = [av - f * cv for av, cv in zip(a[r], a[col])]
    return [a[i][n] for i in range(n)]


def _f(u, v, row):
    return (u, v, 1.0)[row]


def solve(points):
    """points: [{'u','v','px','py'}, ...]，≥3 个点；返回仿射参数 dict。

    模型对 px 和 py 各是一次线性回归，特征为 [u, v, 1]。
    """
    pts = [{'u': float(p['u']), 'v': float(p['v']),
            'px': float(p['px']), 'py': float(p['py'])} for p in points]
    if len(pts) < 3:
        raise CalibError('至少要 3 个标定点（建议 1 个基准 + X/Y 各走一次）')
    us = [p['u'] for p in pts]
    vs = [p['v'] for p in pts]
    if max(us) - min(us) < 1e-6 or max(vs) - min(vs) < 1e-6:
        raise CalibError('标定点在 u 或 v 方向没有展开（视觉里点没动或只沿一条线动）')
    # 法方程 (A^T A) x = A^T b
    ata = [[sum(_f(us[i], vs[i], r) * _f(us[i], vs[i], col) for i in range(len(pts)))
            for col in range(3)] for r in range(3)]
    atb_x = [sum(_f(us[i], vs[i], r) * pts[i]['px'] for i in range(len(pts))) for r in range(3)]
    atb_y = [sum(_f(us[i], vs[i], r) * pts[i]['py'] for i in range(len(pts))) for r in range(3)]
    a, b, tx = _solve3(ata, atb_x)
    c, d, ty = _solve3(ata, atb_y)
    if abs(a * d - b * c) < 1e-9:
        raise CalibError('变换矩阵接近奇异，标定点可能共线')
    return {'a': a, 'b': b, 'c': c, 'd': d, 'tx': tx, 'ty': ty}


def residuals(points, calib):
    out = []
    for p in points:
        px, py = uv_to_pulse(p['u'], p['v'], calib)
        out.append({'u': p['u'], 'v': p['v'],
                    'dx_pulse': px - float(p['px']), 'dy_pulse': py - float(p['py'])})
    return out


def uv_to_pulse(u, v, calib):
    """把视觉归一化坐标换算成 X/Y 脉冲。calib 可以是 dict 或文件内容。"""
    k = calib.get('affine', calib)
    try:
        a, b, c, d, tx, ty = (float(k[key]) for key in KEYS)
    except (KeyError, TypeError) as exc:
        raise CalibError(f'标定数据缺少字段：{exc}') from exc
    px = a * float(u) + b * float(v) + tx
    py = c * float(u) + d * float(v) + ty
    return px, py


def save(calib, path=DEFAULT_PATH, points=None):
    data = {'version': 1, 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'affine': {key: float(calib[key]) for key in KEYS}}
    if points:
        data['points'] = [{'u': float(p['u']), 'v': float(p['v']),
                           'px': float(p['px']), 'py': float(p['py'])} for p in points]
        data['residual'] = residuals(points, calib)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def load(path=DEFAULT_PATH):
    path = Path(path)
    if not path.exists():
        raise CalibError(f'还没有标定文件：{path}（先按联调与通讯方案.md 第 4 节做一次标定）')
    data = json.loads(path.read_text(encoding='utf-8'))
    if 'affine' not in data:
        raise CalibError(f'{path} 不是有效的标定文件（缺 affine）')
    return data


if __name__ == '__main__':
    # 自检：已知变换 → 造点 → 反解，误差应接近 0
    truth = {'a': 1600.0, 'b': 210.0, 'c': -180.0, 'd': 1480.0, 'tx': 3200.0, 'ty': -900.0}
    pts = []
    for u, v in ((0.2, 0.2), (0.8, 0.25), (0.3, 0.75), (0.75, 0.7)):
        px, py = uv_to_pulse(u, v, truth)
        pts.append({'u': u, 'v': v, 'px': px, 'py': py})
    got = solve(pts)
    err = max(abs(got[k] - truth[k]) for k in KEYS)
    print('反解误差', err)
    print('残差', residuals(pts, got)[:2])
    assert err < 1e-6, err
    print('tray_calib 自检通过')
