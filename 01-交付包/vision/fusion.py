"""双摄融合：顶视 + 副摄综合检测。

两路相机各自用四点透视把画面校正到 700x700 的托盘坐标系。顶视负责俯视位置
(u, v)、轮廓与数量；副摄给出水平位置与高度/侧影信息，用来印证顶视结果、并
补上顶视被遮挡/漏掉的目标。

关于"副摄水平方向对应托盘的哪根轴"：
副摄是从侧面水平看托盘的，它画面的水平方向只可能对应托盘的 u（左右）或 v
（前后），取决于它装在托盘哪一条边外面。所以融合先假设两种对齐轴（u / v）
各配一次，取代价小的，也可以在界面里手动指定（"对齐轴"）。

融合策略（不假设两路像素一一对应）：
1. 顶视目标与副摄侧影按 |Δ坐标| + 颜色惩罚做**一对一**配对（每块侧影最多配
   一个顶视目标），且两路颜色都识别出来但明显不同时绝不配对。副摄只能看到
   一个水平方向，同列前后重叠物体会叠成一块侧影；若允许共用，侧影上的类别
   标签就会挑错，出现"同一物体两路两个类"。没配上的顶视目标标为"副摄未印证"。
2. 副摄看到、但没有配上任何顶视目标的侧影，作为 side-only 目标追加进列表
   （通常是顶视被遮挡或漏检的物体）。
3. 副摄画面左右方向可能与顶视相反，翻转与不翻转都试一次，取代价小的。

坐标系约定：center_tray = [u, v]，u 为 700 宽图的比例坐标 0~1。
"""
import math

MAX_DIST = .18     # 顶视与副摄同一目标允许的最大水平差（700px 图约 126px）
DUP_SIDE_DIST = .10  # 副摄把同一物块拆成多块时，距已配对目标这么近的"多余块"丢弃
AXIS_NAMES = {'u': '左右(u)', 'v': '前后(v)'}


def _color_penalty(t, s):
    """两路颜色一致时加 0 分，未知略加，明显不同加大（只做偏好，不硬否决）。"""
    tc, sc = t.get('color'), s.get('color')
    if not tc or not sc or tc == '未知' or sc == '未知':
        return .03
    return 0.0 if tc == sc else .10


def _top_val(t, axis):
    """顶视目标在给定对齐轴上的坐标（u 用 x，v 用 y）。"""
    return t['center_tray'][0 if axis == 'u' else 1]


def _side_val(s, flip):
    """副摄目标的水平坐标（副摄画面只有水平方向能对应托盘的某根轴）。"""
    su = s['center_tray'][0]
    return (1 - su) if flip else su


def _match(top, side, flip, max_dist, axis='u'):
    """一对一配对：每块副摄侧影最多配一个顶视目标。

    副摄只能看到一个水平方向，同列前后重叠的物体会叠成一块侧影。若允许共用，
    这块侧影的类别标签就只能在几个顶视目标里随便挑一个，导致用户看到
    “同一个物体在两个画面里是两个类别”。因此这里严格一对一：没配上的
    顶视目标标为“副摄未印证”，没配上的侧影作为 side-only 追加。
    """
    cand = []
    for i, t in enumerate(top):
        tv = _top_val(t, axis)
        for j, s in enumerate(side):
            d = abs(tv - _side_val(s, flip))
            if d > max_dist:
                continue
            pen = _color_penalty(t, s)
            # 两路颜色都识别出来但明显不同 → 一定不是同一个物体，直接不配：
            # 这样副摄画面上的类别标签永远和顶视一致，不会再出现“同一物体两路两类”。
            if pen >= .10:
                continue
            cand.append((d + pen, d, i, j))
    cand.sort()
    matches, used, cost = {}, set(), 0.0
    for score, d, i, j in cand:
        if i in matches or j in used:
            continue
        matches[i] = j
        used.add(j)
        cost += score
    return matches, used, cost, flip


def fuse(top, side, max_dist=MAX_DIST, flip_mode=0, axis_mode=0, info=None,
         dup_dist=DUP_SIDE_DIST):
    """合并两路检测，返回带 id 的融合目标列表。

    flip_mode：副摄画面左右方向。0=自动判断，1=同向（不翻转），2=镜像（翻转）。
    axis_mode：副摄水平方向对应托盘的哪根轴。0=自动（u/v 都试），1=左右(u)，2=前后(v)。
    info：可选 dict，填入本次融合的诊断信息
    {'axis','flip','flip_mode','axis_mode','n_top','n_side','matched','mean_dist',
     'unmatched_top','unmatched_side','auto'}

    每项: {'id', 'views'('top+side'/'top'/'side'), 'top': item|None,
           'side': item|None, 'confidence'}
    """
    top = sorted(top, key=lambda z: (z['center_tray'][0], z['center_tray'][1]))
    side = sorted(side, key=lambda z: (z['center_tray'][0], z['center_tray'][1]))
    matches, used, flip, axis = {}, set(), False, 'u'
    if top and side:
        axes = ('u', 'v') if axis_mode in (0, None) else (('u',) if axis_mode == 1 else ('v',))
        flips = [False, True] if flip_mode in (0, None) else [flip_mode == 2]
        best = None
        for ax in axes:
            for f in flips:
                m, u, cost, fl = _match(top, side, f, max_dist, ax)
                # 先比配上的目标数，再比代价（|Δ| 越小越好）；平手时保留先试的
                # 轴/方向（轴顺序 u 优先），避免自动模式在数据少时来回跳。
                key = (len(m), -cost)
                if best is None or key > best[0]:
                    best = (key, m, u, fl, ax)
        matches, used, flip, axis = best[1], best[2], best[3], best[4]

    fused = []
    dists = []
    for i, t in enumerate(top):
        j = matches.get(i)
        s = side[j] if j is not None else None
        item = {'top': t, 'side': s,
                'views': 'top+side' if s is not None else 'top'}
        if s is not None:
            item['dist'] = abs(_top_val(t, axis) - _side_val(s, flip))
            dists.append(item['dist'])
            tc, sc = t.get('color'), s.get('color')
            item['color_mismatch'] = bool(tc and sc and tc != '未知' and sc != '未知' and tc != sc)
        fused.append(item)
    side_only = 0
    dup_side = 0
    used_sides = [side[j] for j in matches.values() if j is not None]

    def _uv(it):
        c = it.get('center_tray') or (0.0, 0.0)
        return float(c[0]), float(c[1])

    for j, s in enumerate(side):
        if j in used:
            continue
        su, sv = _uv(s)
        near = any(math.hypot(su - mu, sv - mv) <= dup_dist for mu, mv in map(_uv, used_sides))
        if near:
            dup_side += 1          # 副摄把同一物块拆成多块：丢弃碎块
            continue
        fused.append({'top': None, 'side': s, 'views': 'side'})
        side_only += 1

    def key(f):
        it = f['top'] or f['side']
        return (it['center_tray'][1], it['center_tray'][0])
    fused.sort(key=key)
    for n, f in enumerate(fused, 1):
        f['id'] = n
        f['confidence'] = max((z['confidence'] for z in (f['top'], f['side']) if z), default=0.0)

    if info is not None:
        info.update(axis=axis, axis_name=AXIS_NAMES[axis],
                    flip=flip, flip_mode=int(flip_mode or 0),
                    axis_mode=int(axis_mode or 0), n_top=len(top), n_side=len(side),
                    matched=len(matches),
                    mean_dist=(sum(dists) / len(dists)) if dists else None,
                    duplicate_side=dup_side,
                    unmatched_top=len(top) - len(matches),
                    unmatched_side=side_only,
                    auto=(flip_mode in (0, None) and axis_mode in (0, None)))
    return fused
