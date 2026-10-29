"""motion_guard 单测：软限位与基准一致性（2026-10-29 事故后的护栏）。"""
import sys
from pathlib import Path
import unittest

HOST = Path(__file__).resolve().parents[1]
for p in (str(HOST), str(HOST / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import motion_guard as MG


class LimitsTest(unittest.TestCase):
    def test_default_limits_loaded(self):
        lim = MG.load_limits()
        self.assertIn(1, lim)
        self.assertLess(lim[1][0], lim[1][1])

    def test_check_absolute_inside_and_outside(self):
        lim = {1: (-152501, 500)}
        self.assertTrue(MG.check_absolute(lim, 1, -152000)[0])
        self.assertTrue(MG.check_absolute(lim, 1, 0)[0])
        ok, msg = MG.check_absolute(lim, 1, -305612)
        self.assertFalse(ok)
        self.assertIn('超出软限位', msg)
        # 事故里出现过的取值：+153,612 脉冲（正方向越界）
        self.assertFalse(MG.check_absolute(lim, 1, 153612)[0])


class ConsistencyTest(unittest.TestCase):
    def test_counts_to_pulses(self):
        self.assertAlmostEqual(MG.counts_to_pulses(20480), 1000.0)
        self.assertAlmostEqual(MG.counts_to_pulses(-269189), -13143.99, places=1)

    def test_consistent_passes(self):
        soft = {1: -152000.0, 4: 5597.0, 5: -13144.0}
        counts = {1: -152000 * 20.48, 4: 5597 * 20.48, 5: -13144 * 20.48}
        self.assertEqual(MG.consistency(soft, counts), [])

    def test_partial_zero_detected(self):
        # 事故形态：Z 被清零而软位置仍是 -152000 → 回读 0 counts
        soft = {1: -152000.0, 4: 0.0, 5: -13144.0}
        counts = {1: 0.0, 4: 0.0, 5: -269189.0}
        bad = MG.consistency(soft, counts)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0][0], 1)
        self.assertIn('Z', MG.describe_consistency(bad))

    def test_empty_inputs_are_ignored(self):
        self.assertEqual(MG.consistency({}, {1: 123}), [])
        self.assertEqual(MG.consistency(None, None), [])


if __name__ == '__main__':
    unittest.main()
