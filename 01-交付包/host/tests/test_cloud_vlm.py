"""云端复核适配层的离线单测（不打真实网络）。"""
import sys
import unittest
from pathlib import Path

import numpy as np

VISION = Path(__file__).resolve().parents[2] / 'vision'
sys.path.insert(0, str(VISION))

import cloud_vlm as CV  # noqa: E402


class NormalizeShapeTest(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(CV.normalize_shape('正方体/正方形'), '长方体')
        self.assertEqual(CV.normalize_shape('棱锥'), '四棱锥')

    def test_cone_before_cylinder(self):
        # “圆锥”里也含“圆”，必须优先判成圆锥，不能落到圆柱
        self.assertEqual(CV.normalize_shape('圆锥'), '圆锥')
        self.assertEqual(CV.normalize_shape('圆形金字塔'), '四棱锥')
        self.assertEqual(CV.normalize_shape('圆柱'), '椭圆柱')

    def test_keywords(self):
        # 赛题只有 6 类：三棱柱/球不在其中 → 归“其他几何体”；六棱柱 → 六棱锥分支
        self.assertEqual(CV.normalize_shape('三棱柱'), '其他几何体')
        self.assertEqual(CV.normalize_shape('三棱体'), '其他几何体')
        self.assertEqual(CV.normalize_shape('六棱柱'), '六棱锥')
        self.assertEqual(CV.normalize_shape('正方形'), '长方体')
        self.assertEqual(CV.normalize_shape('长方形'), '长方体')
        self.assertEqual(CV.normalize_shape('菱形'), '长方体')
        self.assertEqual(CV.normalize_shape('球体'), '其他几何体')

    def test_unknown(self):
        self.assertEqual(CV.normalize_shape(''), '其他几何体')
        self.assertEqual(CV.normalize_shape('某种奇怪的东西'), '其他几何体')


class MatchTest(unittest.TestCase):
    def setUp(self):
        self._orig = CV.ask_objects_cached
        self.warp = np.zeros((700, 700, 3), np.uint8)
        self.mask = np.zeros((700, 700), np.uint8)
        self.mask[300:400, 300:400] = 255          # 中心 ≈ (0.5, 0.5)
        CV._CACHE.clear()

    def tearDown(self):
        CV.ask_objects_cached = self._orig

    def test_match_nearest(self):
        CV.ask_objects_cached = lambda *a, **k: {
            'objects': [
                {'shape': '正方体/正方形', 'conf': 0.9, 'center': [0.5, 0.52], 'color': '红'},
                {'shape': '圆锥', 'conf': 0.8, 'center': [0.1, 0.1], 'color': '绿'},
            ], 'ms': 100}
        r = CV.match(self.warp, self.mask)
        self.assertIsNotNone(r)
        self.assertEqual(r['shape'], '正方体/正方形')
        self.assertLess(r['dist'], 0.05)

    def test_match_too_far(self):
        CV.ask_objects_cached = lambda *a, **k: {
            'objects': [{'shape': '圆锥', 'conf': 0.9, 'center': [0.05, 0.05]}], 'ms': 1}
        self.assertIsNone(CV.match(self.warp, self.mask))

    def test_match_pixel_center(self):
        # 模型偶尔给像素坐标（0..700），应被归一化后匹配
        CV.ask_objects_cached = lambda *a, **k: {
            'objects': [{'shape': '棱锥', 'conf': 0.8, 'center': [350, 350]}], 'ms': 1}
        r = CV.match(self.warp, self.mask)
        self.assertIsNotNone(r)
        self.assertEqual(r['shape'], '棱锥')


if __name__ == '__main__':
    unittest.main()
