"""形状标定：用已知尺寸的方块，标出"毫米/像素"和两个视角的修正系数。

为什么需要：
  - 主摄（顶视）能看到物块"一点点前面" → 上下方向（深度）被撑大，直接用 bbox 高度
    会把正方体判成长方体；
  - 副摄是 45° 斜视 → 竖直方向被投影压缩，量高度要乘系数；
  - 托盘四点框框的是**外框**，所以 tray_mm 就是外框实际边长（本项目 160mm）。
所以现场放一个已知边长的方块，量一次就能把三个数钉死。

纯函数、不依赖相机/GUI，便于单测：
    res = measure(top_bbox, side_bbox, tray_mm=160, known_mm=25)
其中 top_bbox/side_bbox 是 **(x, y, w, h)**，且必须位于 700×700 的托盘校正图坐标系。
"""
import math

PACK_PX = 700.0        # 视觉管线把托盘校正成 700×700


def mm_per_px(tray_mm=PACK_PX * 0.2286, pack_px=PACK_PX):
    """校正图上每像素多少毫米。"""
    return float(tray_mm) / float(pack_px)


def measure(top_bbox, side_bbox, tray_mm=160.0, known_mm=25.0, pack_px=PACK_PX):
    """算出 mm_per_px、k_front（顶视深度修正）、k_h（副摄高度修正）。

    返回 dict：{'mm_per_px', 'k_front', 'k_h', 'w_mm', 'depth_mm', 'height_mm', 'ok', 'warn'}
    """
    mmp = mm_per_px(tray_mm, pack_px)
    out = {'mm_per_px': round(mmp, 5), 'k_front': None, 'k_h': None,
           'w_mm': None, 'depth_mm': None, 'height_mm': None, 'ok': False, 'warn': []}
    if not top_bbox or not side_bbox:
        out['warn'].append('缺少分割结果（顶视或副摄没看到目标）')
        return out

    _, _, tw, th = [float(v) for v in top_bbox]
    _, _, sw, sh = [float(v) for v in side_bbox]
    if tw <= 2 or th <= 2 or sh <= 2:
        out['warn'].append('目标太小，换个大点的物块再标（建议 ≥ 20mm）')
        return out

    # 顶视：宽度方向没有遮挡，用它反推真实毫米/像素，再用高度算深度修正
    w_mm_measured = tw * mmp
    k_front = float(known_mm) / max(1e-6, th * mmp)
    k_h = float(known_mm) / max(1e-6, sh * mmp)

    out.update({'w_mm': round(w_mm_measured, 2), 'depth_mm': round(th * mmp, 2),
                'height_mm': round(sh * mmp, 2),
                'k_front': round(k_front, 3), 'k_h': round(k_h, 3)})

    # 合理性检查：系数应接近 1（0.5~2.0），宽度毫米应与已知边长同量级
    if not (0.5 <= k_front <= 2.0):
        out['warn'].append('k_front=%.2f 超出合理范围（0.5~2.0）' % k_front)
    if not (0.5 <= k_h <= 2.0):
        out['warn'].append('k_h=%.2f 超出合理范围（0.5~2.0）' % k_h)
    if abs(w_mm_measured - known_mm) > max(6.0, 0.25 * known_mm):
        out['warn'].append('顶视量到的宽度 %.0fmm 与已知 %.0fmm 差得多，检查 tray_mm 是否填对（外框边长）'
                           % (w_mm_measured, known_mm))
    out['ok'] = not out['warn']
    return out


def fmt(res):
    if not res:
        return '标定失败'
    return ('mm/px=%.4f｜顶视宽 %.1fmm、深度 %.1fmm → k_front=%.3f｜'
            '副摄高 %.1fmm → k_h=%.3f'
            % (res['mm_per_px'] or 0, res['w_mm'] or 0, res['depth_mm'] or 0,
               res['k_front'] or 0, res['height_mm'] or 0, res['k_h'] or 0))


def is_known_mm_ok(known_mm):
    try:
        v = float(known_mm)
    except Exception:              # noqa: BLE001
        return False
    return 5.0 <= v <= 200.0 and not math.isnan(v)
