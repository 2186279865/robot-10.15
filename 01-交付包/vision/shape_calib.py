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
    out = {'mm_per_px': None, 'k_front': None, 'k_h': None,
           'w_mm': None, 'depth_mm': None, 'height_mm': None,
           'tray_mm_effective': None, 'scale_source': None, 'ok': False, 'warn': []}
    if not top_bbox or not side_bbox:
        out['warn'].append('缺少分割结果（顶视或副摄没看到目标）')
        return out

    _, _, tw, th = [float(v) for v in top_bbox]
    _, _, sw, sh = [float(v) for v in side_bbox]
    if tw <= 2 or th <= 2 or sh <= 2:
        out['warn'].append('目标太小，换个大点的物块再标（建议 ≥ 20mm）')
        return out

    # 关键：**用已知方块的实际宽度反推 mm/px**，而不是相信 tray_mm。
    # 因为四点框往往点在托盘内框（比外框小一圈），tray_mm 填外框会让所有毫米偏大。
    mmp_measured = float(known_mm) / max(1e-6, tw)
    mmp_nominal = mm_per_px(tray_mm, pack_px)
    mmp = mmp_measured
    out['scale_source'] = 'known_width'

    # 顶视深度（含露出的前面）与副摄高度都按实测 mm/px 换算后，再算修正系数
    depth_mm = th * mmp
    height_mm = sh * mmp
    k_front = float(known_mm) / max(1e-6, depth_mm)
    k_h = float(known_mm) / max(1e-6, height_mm)

    out.update({'mm_per_px': round(mmp, 5), 'tray_mm_effective': round(mmp * pack_px, 2),
                'w_mm': round(tw * mmp, 2), 'depth_mm': round(depth_mm, 2),
                'height_mm': round(height_mm, 2),
                'k_front': round(k_front, 3), 'k_h': round(k_h, 3)})

    if not (0.45 <= k_front <= 2.2):
        out['warn'].append('k_front=%.2f 超出合理范围（0.45~2.2）' % k_front)
    if not (0.45 <= k_h <= 2.2):
        out['warn'].append('k_h=%.2f 超出合理范围（0.45~2.2）' % k_h)
    # 实测 mm/px 与"按 tray_mm 名义值"差太多时提示：多半是四点框框的是内框
    ratio = mmp_measured / max(1e-9, mmp_nominal)
    if ratio < 0.75 or ratio > 1.35:
        out['warn'].append('实测 mm/px=%.4f，按 tray_mm=%.0f 应为 %.4f（差 %.0f%%）'
                           '——四点框大概框的是托盘内框，已按实测值标定，可忽略此提示'
                           % (mmp_measured, tray_mm, mmp_nominal, (ratio - 1) * 100))
    out['ok'] = not out['warn']
    return out


def fmt(res):
    if not res:
        return '标定失败'
    return ('mm/px=%.4f（等效托盘 %.0fmm）｜顶视宽 %.1fmm、深度 %.1fmm → k_front=%.3f｜'
            '副摄高 %.1fmm → k_h=%.3f'
            % (res['mm_per_px'] or 0, res.get('tray_mm_effective') or 0,
               res['w_mm'] or 0, res['depth_mm'] or 0, res['k_front'] or 0,
               res['height_mm'] or 0, res['k_h'] or 0))


def is_known_mm_ok(known_mm):
    try:
        v = float(known_mm)
    except Exception:              # noqa: BLE001
        return False
    return 5.0 <= v <= 200.0 and not math.isnan(v)
