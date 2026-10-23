"""approach_route 单元测试：接近段示教的数据与校验。"""
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = HERE.parent
sys.path.insert(0, str(HOST))

import approach_route as AR  # noqa: E402


def _route(*steps):
    r = AR.empty()
    for axis, direction, rpm, acc, pulses in steps:
        AR.add_step(r, axis, direction, rpm, acc, pulses)
    return r


class TestAddStep(unittest.TestCase):
    def test_axes_and_names(self):
        self.assertEqual(AR.AXES, (4, 5, 1))
        self.assertEqual([AR.AXIS_NAMES[a] for a in AR.AXES], ['X', 'Y', 'Z'])

    def test_add_ok(self):
        r = _route((4, 0, 60, 1, 3200), (5, 1, 60, 1, 1600), (1, 0, 30, 1, 800))
        self.assertEqual(len(r['steps']), 3)
        self.assertEqual(r['steps'][0]['axis'], 4)
        self.assertEqual(r['steps'][1]['dir'], 1)

    def test_reject_tray_axis(self):
        r = AR.empty()
        with self.assertRaises(ValueError):
            AR.add_step(r, 3, 0, 60, 1, 100)

    def test_reject_bad_dir(self):
        r = AR.empty()
        with self.assertRaises(ValueError):
            AR.add_step(r, 4, 2, 60, 1, 100)

    def test_reject_zero_and_negative_pulses(self):
        r = AR.empty()
        for bad in (0, -100):
            with self.assertRaises(ValueError):
                AR.add_step(r, 4, 0, 60, 1, bad)

    def test_reject_too_many_pulses(self):
        r = AR.empty()
        with self.assertRaises(ValueError):
            AR.add_step(r, 4, 0, 60, 1, AR.MAX_PULSES + 1)

    def test_reject_too_many_steps(self):
        r = AR.empty()
        for _ in range(AR.MAX_STEPS):
            AR.add_step(r, 4, 0, 60, 1, 10)
        with self.assertRaises(ValueError):
            AR.add_step(r, 4, 0, 60, 1, 10)

    def test_undo_and_clear(self):
        r = _route((4, 0, 60, 1, 3200), (5, 0, 60, 1, 3200))
        AR.undo(r)
        self.assertEqual(len(r['steps']), 1)
        AR.undo(r)
        self.assertEqual(len(r['steps']), 0)
        AR.undo(r)          # 空的时候撤销不报错
        self.assertEqual(len(r['steps']), 0)
        r2 = _route((4, 0, 60, 1, 100))
        AR.clear(r2)
        self.assertEqual(r2['steps'], [])


class TestTotalsDescribe(unittest.TestCase):
    def test_totals_net(self):
        r = _route((4, 0, 60, 1, 3200), (4, 1, 60, 1, 1200), (5, 1, 60, 1, 1600))
        t = AR.totals(r)
        self.assertEqual(t[4], 2000)     # X 正向 3200 - 负向 1200
        self.assertEqual(t[5], -1600)    # Y 负向
        self.assertEqual(t[1], 0)        # Z 没动
        self.assertNotIn(3, t)           # 托盘不参与（根本没有这个键）

    def test_describe_empty(self):
        self.assertIn('0 段', AR.describe(AR.empty()))

    def test_describe_lists_all_axes(self):
        r = _route((4, 0, 60, 1, 3200), (5, 1, 60, 1, 1600), (1, 0, 30, 1, 800))
        text = AR.describe(r)
        self.assertIn('3 段', text)
        self.assertIn('X +3200', text)
        self.assertIn('Y -1600', text)
        self.assertIn('Z +800', text)


class TestTiming(unittest.TestCase):
    def test_step_ms_positive_and_grows(self):
        small = AR.step_ms({'pulses': 100, 'rpm': 60})
        big = AR.step_ms({'pulses': 10000, 'rpm': 60})
        self.assertGreater(small, 0)
        self.assertGreater(big, small)

    def test_estimate_ms_sums_steps(self):
        r = _route((4, 0, 60, 1, 3200), (5, 0, 60, 1, 3200))
        one = AR.step_ms(r['steps'][0])
        self.assertAlmostEqual(AR.estimate_ms(r), one * 2, delta=1)

    def test_build_steps_override_speed(self):
        r = _route((4, 0, 60, 1, 3200), (5, 0, 60, 1, 1600))
        steps = AR.build_steps(r)
        self.assertEqual([s['rpm'] for s in steps], [60, 60])
        fast = AR.build_steps(r, speed_rpm=120)
        self.assertEqual([s['rpm'] for s in fast], [120, 120])
        self.assertLess(AR.step_ms(fast[0]), AR.step_ms(steps[0]))


class TestValidate(unittest.TestCase):
    def test_empty_is_problem(self):
        problems = AR.validate(AR.empty())
        self.assertTrue(problems)

    def test_ok_route_no_problems(self):
        self.assertEqual(AR.validate(_route((4, 0, 60, 1, 3200))), [])

    def test_bad_pulses_reported(self):
        r = AR.empty()
        r['steps'].append({'axis': 4, 'dir': 0, 'rpm': 60, 'acc': 1, 'pulses': 0})
        self.assertTrue(AR.validate(r))


class TestLoadSave(unittest.TestCase):
    def setUp(self):
        self.tmp = HERE / ('_tmp_ar_%d' % os.getpid())
        self.tmp.mkdir(exist_ok=True)

        def _clean():
            for p in self.tmp.iterdir():
                p.unlink(missing_ok=True)
            self.tmp.rmdir()
        self.addCleanup(_clean)

    def test_load_missing_is_empty(self):
        r = AR.load(self.tmp / 'nope.json')
        self.assertEqual(r['steps'], [])

    def test_load_broken_is_empty(self):
        path = self.tmp / 'broken.json'
        path.write_text('{oops', encoding='utf-8')
        self.assertEqual(AR.load(path)['steps'], [])

    def test_load_filters_bad_steps(self):
        path = self.tmp / 'bad.json'
        path.write_text(json.dumps({'version': AR.VERSION, 'steps': [
            {'axis': 3, 'dir': 0, 'rpm': 60, 'acc': 1, 'pulses': 100},     # 托盘轴，丢
            {'axis': 4, 'dir': 0, 'rpm': 60, 'acc': 1, 'pulses': 0},       # 0 脉冲，丢
            {'axis': 5, 'dir': 1, 'rpm': 60, 'acc': 1, 'pulses': 1600},    # 保留
        ]}), encoding='utf-8')
        steps = AR.load(path)['steps']
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['axis'], 5)

    def test_roundtrip(self):
        path = self.tmp / 'approach.json'
        r = _route((4, 0, 60, 1, 3200), (5, 1, 60, 1, 1600))
        AR.save(r, path)
        got = AR.load(path)
        self.assertEqual(len(got['steps']), 2)
        self.assertEqual(got['steps'][1]['dir'], 1)
        raw = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(raw['version'], AR.VERSION)
        self.assertIn('saved_at', raw)

    def test_load_wrong_version_is_empty(self):
        path = self.tmp / 'v99.json'
        path.write_text(json.dumps({'version': 99, 'steps': [
            {'axis': 4, 'dir': 0, 'rpm': 60, 'acc': 1, 'pulses': 100}]}), encoding='utf-8')
        self.assertEqual(AR.load(path)['steps'], [])


if __name__ == '__main__':
    unittest.main()
