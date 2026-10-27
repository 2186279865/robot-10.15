"""classify._lookup 的回归测试：feature 为 None 时必须判"待登记"，不能抛异常。

真实触发场景：副摄单独看到的目标 `feature` 是 None，融合后投票阶段把它的
class_key 又拿去查一次近邻 → 老代码 `np.linalg.norm(None)` 直接把整次识别打成
`TypeError: unsupported operand type(s) for *: 'NoneType' and 'NoneType'`。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[2]      # 01-交付包
VISION = PKG / 'vision'
for p in (VISION, PKG / 'host'):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import classify          # noqa: E402


class LookupFeatureGuardTest(unittest.TestCase):
    def test_none_feature_returns_pending(self):
        result = classify._lookup('正方体/正方形|绿', '正方体/正方形', '绿',
                                  {}, {'classes': {}}, None)
        self.assertIsNone(result['class_id'])
        self.assertEqual(result['class_name'], '待登记')
        self.assertTrue(result['need_review'])

    def test_empty_array_feature_returns_pending(self):
        import numpy as np
        result = classify._lookup('X|Y', 'X', 'Y', {}, {}, np.zeros((0,), 'float32'))
        self.assertEqual(result['class_name'], '待登记')

    def test_rule_map_still_wins(self):
        cfg = {'class_map': {'正方体/正方形|绿': 3},
               'class_names': {'3': '绿正方体'}}
        result = classify._lookup('正方体/正方形|绿', '正方体/正方形', '绿',
                                  cfg, {'classes': {}}, None)
        self.assertEqual(str(result['class_id']), '3')
        self.assertEqual(result['class_source'], 'rule')


if __name__ == '__main__':
    unittest.main()
