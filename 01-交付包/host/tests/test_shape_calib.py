"""形状标定 + 侧视融合判定的单测（vision/shape_calib.py、shape_features.refine_with_side）。"""
import sys
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[2]
VISION = PKG / 'vision'
if str(VISION) not in sys.path:
    sys.path.insert(0, str(VISION))

import shape_calib as SC          # noqa: E402
import shape_features as SF       # noqa: E402


class CalibMeasureTest(unittest.TestCase):
    MMP = SC.mm_per_px(160.0, 700.0)          # 0.2286 mm/px

    def test_cube_25mm(self):
        w_px = 25.0 / self.MMP                    # 宽度方向未变形 → 109.4px
        depth_px = 31.0 / self.MMP                # 前面露出来 → 深度被撑到 31mm
        side_h_px = 26.0 / self.MMP               # 副摄 45° 投影后略失真
        res = SC.measure((10, 10, w_px, depth_px), (10, 10, w_px, side_h_px),
                         tray_mm=160.0, known_mm=25.0)
        self.assertTrue(res['ok'], msg=str(res.get('warn')))
        self.assertAlmostEqual(res['mm_per_px'], 0.2286, places=3)
        self.assertAlmostEqual(res['w_mm'], 25.0, delta=0.5)
        self.assertAlmostEqual(res['k_front'], 25.0 / 31.0, delta=0.02)
        self.assertAlmostEqual(res['k_h'], 25.0 / 26.0, delta=0.02)

    def test_missing_side(self):
        res = SC.measure((0, 0, 100, 120), None, tray_mm=160.0, known_mm=25.0)
        self.assertFalse(res['ok'])
        self.assertTrue(res['warn'])

    def test_tiny_target_warns(self):
        res = SC.measure((0, 0, 3, 3), (0, 0, 3, 3), tray_mm=160.0, known_mm=25.0)
        self.assertFalse(res['ok'])

    def test_wrong_tray_mm_warns(self):
        # tray_mm 填成 200（外框其实是 160）→ 量出的宽度明显偏大，应给警告
        res = SC.measure((0, 0, 109.4, 109.4), (0, 0, 109.4, 109.4),
                         tray_mm=200.0, known_mm=25.0)
        self.assertFalse(res['ok'])
        self.assertTrue(any('tray_mm' in w for w in res['warn']))

    def test_fmt_has_all_fields(self):
        res = SC.measure((0, 0, 110, 130), (0, 0, 110, 115), tray_mm=160.0, known_mm=25.0)
        text = SC.fmt(res)
        for key in ('mm/px', 'k_front', 'k_h'):
            self.assertIn(key, text)


class RefineWithSideTest(unittest.TestCase):
    def test_round_with_arc_is_sphere(self):
        feats = {'w_mm': 30, 'h_mm': 30}
        shape, conf = SF.refine_with_side(SF.SHAPE_ROUND, 0.9, feats,
                                          {'height_mm': 29, 'kind': 'arc', 'curv': 0.62})
        self.assertEqual(shape, SF.SHAPE_SPHERE)

    def test_round_with_flat_top_is_cylinder(self):
        feats = {'w_mm': 30, 'h_mm': 30}
        shape, _ = SF.refine_with_side(SF.SHAPE_ROUND, 0.9, feats,
                                       {'height_mm': 29, 'kind': 'rect', 'curv': 0.96})
        self.assertEqual(shape, SF.SHAPE_ROUND)

    def test_cube_with_low_height_is_box(self):
        feats = {'w_mm': 40, 'h_mm': 40}
        shape, _ = SF.refine_with_side(SF.SHAPE_CUBE, 0.85, feats,
                                       {'height_mm': 15, 'kind': 'rect'})
        self.assertEqual(shape, SF.SHAPE_BOX)

    def test_cube_with_equal_height_stays_cube(self):
        feats = {'w_mm': 40, 'h_mm': 40}
        shape, _ = SF.refine_with_side(SF.SHAPE_CUBE, 0.85, feats,
                                       {'height_mm': 39, 'kind': 'rect'})
        self.assertEqual(shape, SF.SHAPE_CUBE)

    def test_rhombus_with_side_face(self):
        feats = {'w_mm': 36, 'h_mm': 22}
        shape, _ = SF.refine_with_side(SF.SHAPE_RHOMBUS, 0.8, feats,
                                       {'height_mm': 25, 'kind': 'rect'})
        self.assertEqual(shape, SF.SHAPE_RHOMBUS)

    def test_no_side_keeps_shape(self):
        shape, conf = SF.refine_with_side(SF.SHAPE_CUBE, 0.85, {'w_mm': 40, 'h_mm': 40}, None)
        self.assertEqual(shape, SF.SHAPE_CUBE)
        self.assertAlmostEqual(conf, 0.85)


class SideInfoTest(unittest.TestCase):
    def test_none_inputs(self):
        out = SF.side_info_of(None, None, 0.2286, 1.0)
        self.assertIsNone(out['height_mm'])

    def test_height_and_kind(self):
        import cv2
        import numpy as np
        mask = np.zeros((120, 80), np.uint8)
        cv2.rectangle(mask, (10, 20), (70, 110), 255, -1)      # 平顶矩形侧影
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contour = max(cnts, key=cv2.contourArea)
        out = SF.side_info_of(mask, contour, mm_per_px=0.2286, k_h=1.0)
        self.assertAlmostEqual(out['height_mm'], 90 * 0.2286, delta=0.5)
        self.assertEqual(out['kind'], 'rect')


if __name__ == '__main__':
    unittest.main()
