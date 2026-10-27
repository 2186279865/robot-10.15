"""形状几何基元判定（vision/shape_features.py）单元测试。

这些用例用"合成掩膜"锁住判定链，防止以后改阈值把六棱柱判成圆柱之类的回归。
"""
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

PKG = Path(__file__).resolve().parents[2]          # 01-交付包
VISION = PKG / 'vision'
if str(VISION) not in sys.path:
    sys.path.insert(0, str(VISION))

import shape_features as SF                        # noqa: E402


def _mask_of(kind, size=240):
    img = np.zeros((size, size), np.uint8)
    if kind == 'square':
        cv2.rectangle(img, (40, 40), (200, 200), 255, -1)
    elif kind == 'rect':
        cv2.rectangle(img, (40, 90), (200, 150), 255, -1)
    elif kind == 'circle':
        cv2.circle(img, (120, 120), 80, 255, -1)
    elif kind == 'tri':
        cv2.fillPoly(img, [np.array([[120, 50], [200, 190], [40, 190]])], 255)
    elif kind == 'penta':
        cv2.fillPoly(img, [cv2.convexHull(np.array(
            [[120, 40], [200, 110], [175, 200], [65, 200], [40, 110]]))], 255)
    elif kind == 'rhombus':
        cv2.fillPoly(img, [np.array([[130, 60], [215, 130], [130, 200], [45, 130]])], 255)
    elif kind == 'square45':
        cv2.fillPoly(img, [cv2.convexHull(np.array(
            [[130, 40], [220, 130], [130, 220], [40, 130]]))], 255)
    elif kind == 'hexa':
        cv2.fillPoly(img, [cv2.convexHull(np.array(
            [[70, 60], [170, 60], [210, 120], [170, 180], [70, 180], [30, 120]]))], 255)
    else:
        raise ValueError(kind)
    cnts, _ = cv2.findContours(img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return img, max(cnts, key=cv2.contourArea)


class ShapeChainTest(unittest.TestCase):
    def _classify(self, kind, side_curv=None, mm_per_px=0.3):
        mask, contour = _mask_of(kind)
        feats = SF.features(mask, contour, mm_per_px=mm_per_px)
        name, conf, why = SF.classify_shape(feats, side_curv=side_curv)
        return name, conf, feats, why

    def test_square_is_cube(self):
        name, conf, _, _ = self._classify('square')
        self.assertEqual(name, SF.SHAPE_CUBE)
        self.assertGreaterEqual(conf, 0.8)

    def test_rect_is_box(self):
        name, _, feats, _ = self._classify('rect')
        self.assertEqual(name, SF.SHAPE_BOX)
        self.assertGreater(feats['ratio_mm'], 2.0)

    def test_circle_is_round(self):
        name, conf, _, _ = self._classify('circle')
        self.assertEqual(name, SF.SHAPE_ROUND)
        self.assertGreaterEqual(conf, 0.85)

    def test_circle_with_curved_side_is_sphere(self):
        name, _, _, why = self._classify('circle', side_curv=0.70)
        self.assertEqual(name, SF.SHAPE_SPHERE)
        self.assertTrue(any('收窄' in w for w in why))

    def test_triangle(self):
        name, _, _, _ = self._classify('tri')
        self.assertEqual(name, SF.SHAPE_TRI)

    def test_pentagon(self):
        name, _, _, _ = self._classify('penta')
        self.assertEqual(name, SF.SHAPE_PENTA)

    def test_hexagon_is_prism_not_round(self):
        """六棱柱最容易和圆柱混，这里锁死：必须判六棱体。"""
        name, conf, feats, _ = self._classify('hexa')
        self.assertEqual(name, SF.SHAPE_PRISM, msg='feats=%s' % SF.describe(feats))
        self.assertLess(feats['circ'], 0.85)

    def test_rhombus(self):
        """菱形顶面：bbox 填充率≈0.5、rect_fit≈0.8，必须判菱体而不是六棱/其他。"""
        name, conf, feats, _ = self._classify('rhombus')
        self.assertEqual(name, SF.SHAPE_RHOMBUS, msg=SF.describe(feats))
        self.assertLessEqual(feats['extent'], 0.66)

    def test_rotated_square_is_still_cube(self):
        """正方形转 45° 看起来像菱形，但 rect_fit≈1.0，必须仍判正方体。"""
        name, _, feats, _ = self._classify('square45')
        self.assertEqual(name, SF.SHAPE_CUBE, msg=SF.describe(feats))
        self.assertGreater(feats['rect_fit'], 0.95)

    def test_tiny_mask_is_other(self):
        mask = np.zeros((60, 60), np.uint8)
        cv2.circle(mask, (30, 30), 6, 255, -1)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contour = max(cnts, key=cv2.contourArea)
        feats = SF.features(mask, contour, mm_per_px=0.3)
        name, conf, _ = SF.classify_shape(feats)
        self.assertEqual(name, SF.SHAPE_OTHER)
        self.assertLess(conf, 0.5)

    def test_clean_mask_fills_hole(self):
        mask = np.zeros((200, 200), np.uint8)
        cv2.rectangle(mask, (40, 40), (160, 160), 255, -1)
        cv2.circle(mask, (100, 100), 25, 0, -1)          # 反光造成的洞
        cleaned = SF.clean_mask(mask)
        cnts, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contour = max(cnts, key=cv2.contourArea)
        feats = SF.features(cleaned, contour, mm_per_px=0.3)
        self.assertGreater(feats['solidity'], 0.99)
        self.assertEqual(SF.classify_shape(feats)[0], SF.SHAPE_CUBE)

    def test_features_have_unit_fields(self):
        mask, contour = _mask_of('square')
        feats = SF.features(mask, contour, mm_per_px=0.5)
        for key in ('circ', 'solidity', 'extent', 'rect_fit', 'nv',
                    'w_mm', 'h_mm', 'ratio_mm', 'hu'):
            self.assertIn(key, feats)
        self.assertAlmostEqual(feats['w_mm'], feats['h_mm'], delta=1.0)
        self.assertAlmostEqual(feats['ratio_mm'], 1.0, delta=0.02)


if __name__ == '__main__':
    unittest.main()
