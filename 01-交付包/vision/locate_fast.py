"""快速定位（不分类）：颜色/背景差分 + 连通域 + 分水岭拆分。

目标：10–30 ms 给出托盘内每个物块的 (u, v) 与面积，供 auto_grab 直接换算 X/Y 脉冲。
与 SAM 路径输出同样的坐标语义（700×700 校正图上的归一化 u,v）。

用法::
    from locate_fast import locate
    items = locate(warp, bg=bg_gray_700)      # bg 可选（空托盘灰度图，同样 700×700）
    # -> [{'u':0.31,'v':0.72,'area_px':18000,'bbox_warped':[..],'mask':..,'center_tray':(0.31,0.72)}, ...]
"""
from __future__ import annotations

import cv2
import numpy as np

SIZE = 700


def _bg_from_border(warp: np.ndarray, border: int = 14) -> tuple[np.ndarray, np.ndarray]:
    """用四周边缘像素估计背景色（托盘是均匀灰），返回 (背景图, 背景掩膜)。"""
    h, w = warp.shape[:2]
    m = np.zeros((h, w), np.uint8)
    m[:border, :] = 255
    m[-border:, :] = 255
    m[:, :border] = 255
    m[:, -border:] = 255
    med = np.median(warp[m > 0].reshape(-1, 3), axis=0)
    bg = np.dstack([np.full((h, w), med[0], np.float32),
                    np.full((h, w), med[1], np.float32),
                    np.full((h, w), med[2], np.float32)]).astype(np.uint8)
    return bg, m


def _split_blob(mask: np.ndarray, expect: float) -> list[np.ndarray]:
    """把粘连的大块按距离变换分水岭拆成若干块；拆不动就原样返回。"""
    n_guess = int(round(float(mask.sum()) / max(1.0, expect)))
    if n_guess < 2:
        return [mask]
    # 先腐蚀切掉细小连接桥（相邻物块的窄缝），再在腐蚀图上找种子
    er = cv2.erode(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)), iterations=1)
    base = er if int(er.sum()) > 4000 * 255 else mask
    dist = cv2.distanceTransform(base, cv2.DIST_L2, 3)
    if dist.max() <= 0:
        return [mask]
    # 局部极大值作为种子（核随块尺寸放大）
    ks = int(max(9, min(35, (mask.shape[0] * 0.02) + 9)))
    kern = np.ones((ks, ks), np.uint8)
    peaks = (dist >= cv2.dilate(dist, kern) - 1e-3) & (dist > 0.30 * dist.max())
    seeds = (peaks.astype(np.uint8)) * 255
    seeds = cv2.morphologyEx(seeds, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n_seed, markers = cv2.connectedComponents(seeds)
    if n_seed - 1 < 2:
        return [mask]
    markers = markers + 1
    markers[mask == 0] = 0
    color = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
    cv2.watershed(color, markers)
    outs = []
    for k in range(2, n_seed + 1):
        part = np.where(markers == k, 255, 0).astype(np.uint8)
        if part.sum() / 255.0 < 300:
            continue
        outs.append(part)
    return outs if len(outs) >= 2 else [mask]




def _split_by_erosion(mask: np.ndarray, k_er: int = 5, it: int = 2,
                      min_area: int = 700) -> list:
    """用腐蚀切桥再连通域拆分：物块之间有窄缝时最稳。失败返回 [mask]。"""
    # 从小到大试腐蚀核，直到把一块切成多块（窄缝宽度未知，固定核不可靠）
    for (k_er, it) in ((5, 1), (5, 2), (7, 2), (9, 2), (11, 2), (13, 2)):
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_er, k_er))
        er = cv2.erode(mask, ker, iterations=it)
        if int((er > 0).sum()) < min_area * 2:
            continue
        n, lab, st, _ = cv2.connectedComponentsWithStats(er, 8)
        comps = [i for i in range(1, n) if int(st[i, 4]) >= min_area]
        if len(comps) < 2:
            continue
        outs = []
        for i in comps:
            part = ((lab == i).astype(np.uint8)) * 255
            part = cv2.dilate(part, ker, iterations=it) & mask
            if int((part > 0).sum()) >= min_area:
                outs.append(part)
        if len(outs) >= 2:
            return outs
    return [mask]


def _split_by_hue(warp: np.ndarray, mask: np.ndarray, k: int = 2,
                  min_area: int = 900) -> list:
    """按色相把粘连的一团拆成 k 块（橙色圆柱 + 黄色圆锥这类）。失败返回 [mask]。"""
    hsv = cv2.cvtColor(warp, cv2.COLOR_BGR2HSV)
    idx = np.where(mask > 0)
    if idx[0].size < min_area * k:
        return [mask]
    hue = hsv[..., 0][idx].astype(np.float32).reshape(-1, 1)
    try:
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
        _, labels, centers = cv2.kmeans(hue, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    except Exception:                                   # noqa: BLE001
        return [mask]
    labels = labels.ravel()
    # 只有两簇色相差异明显才拆（>12 个色相单位）
    cs = sorted(float(c[0]) for c in centers)
    if len(cs) < 2 or (cs[1] - cs[0]) < 12:
        return [mask]
    outs = []
    for kk in range(k):
        sel = labels == kk
        if int(sel.sum()) < min_area:
            continue
        part = np.zeros(mask.shape, np.uint8)
        part[idx[0][sel], idx[1][sel]] = 255
        part = cv2.morphologyEx(part, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        part = cv2.morphologyEx(part, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=1)
        if int((part > 0).sum()) >= min_area:
            outs.append(part)
    return outs if len(outs) >= 2 else [mask]


def locate(warp: np.ndarray,
           bg: np.ndarray | None = None,
           area_min: int = 1200,
           area_max: int = 60000,
           sat_min: int = 40,
           val_min: int = 25,
           diff_min: int = 16,
           border: int = 14,
           edge_px: int = 4,
           max_targets: int = 16,
           use_border_bg: bool = False,
           split: bool = True,
           debug: dict | None = None) -> list[dict]:
    """返回托盘内目标列表（按面积从大到小）。"""
    if warp is None:
        return []
    if warp.shape[0] != SIZE or warp.shape[1] != SIZE:
        warp = cv2.resize(warp, (SIZE, SIZE), interpolation=cv2.INTER_AREA)

    hsv = cv2.cvtColor(warp, cv2.COLOR_BGR2HSV)
    sat, val = hsv[..., 1], hsv[..., 2]
    fg = ((sat >= sat_min) & (val >= val_min))

    if bg is not None:
        try:
            b = bg if bg.ndim == 2 else cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
            if b.shape[:2] != (SIZE, SIZE):
                b = cv2.resize(b, (SIZE, SIZE), interpolation=cv2.INTER_AREA)
            g = cv2.cvtColor(warp, cv2.COLOR_BGR2GRAY)
            fg = fg | (cv2.absdiff(g, b) >= diff_min)
        except Exception:                       # noqa: BLE001
            pass
    elif use_border_bg:
        # 没有空托盘图时（可选）：与边框背景色做差分，抓白色/浅色物块
        bgc, _ = _bg_from_border(warp, border)
        _d = np.linalg.norm(warp.astype(np.int16) - bgc.astype(np.int16), axis=2)
        fg = fg | (_d >= 28)

    m = (fg.astype(np.uint8)) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)

    n, lab, stats, cents = cv2.connectedComponentsWithStats(m, 8)
    blobs = []
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        if a < area_min:
            continue
        blobs.append((a, i, x, y, w, h))
    blobs.sort(reverse=True)

    items: list[dict] = []
    for a, i, x, y, w, h in blobs:
        blob = ((lab == i).astype(np.uint8)) * 255
        if split and a > area_max * 0.56:
            parts = _split_by_erosion(blob, 5, 2, max(700, int(area_min * 0.4)))
            if len(parts) < 2:
                parts = _split_by_hue(warp, blob, 2, max(900, int(area_min * 0.5)))
            if len(parts) < 2:
                parts = _split_blob(blob, expect=area_max * 0.35)
        else:
            parts = [blob]
        for p in parts:
            cnts, _ = cv2.findContours(p, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not cnts:
                continue
            c = max(cnts, key=cv2.contourArea)
            ar = float(cv2.contourArea(c))
            if ar < area_min or ar > area_max:
                continue
            bx, by, bw, bh = cv2.boundingRect(c)
            if bx <= edge_px or by <= edge_px or bx + bw >= SIZE - edge_px or by + bh >= SIZE - edge_px:
                continue
            mtx = cv2.moments(c)
            if mtx['m00'] <= 0:
                continue
            cu = float(mtx['m10'] / mtx['m00']) / SIZE
            cv = float(mtx['m01'] / mtx['m00']) / SIZE
            items.append({'u': round(cu, 4), 'v': round(cv, 4),
                          'center_tray': (round(cu, 4), round(cv, 4)),
                          'area_px': int(round(ar)),
                          'bbox_warped': [int(bx), int(by), int(bw), int(bh)],
                          'mask': p, 'contour': c,
                          'center_px': (cu * SIZE, cv * SIZE)})
        if len(items) >= max_targets:
            break

    items.sort(key=lambda d: -d['area_px'])
    if debug is not None:
        debug['n_items'] = len(items)
        debug['bg_used'] = bg is not None
    return items
