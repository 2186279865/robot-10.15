"""home_route 单元测试。"""
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = HERE.parent
sys.path.insert(0, str(HOST))

import home_route as HR  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_defaults(self):
        cfg = HR.normalize(None)
        self.assertTrue(cfg['enabled'])
        self.assertEqual([s['axis'] for s in cfg['steps']], [4, 5, 1])
        self.assertEqual(cfg['zero_axes'], [4, 5])

    def test_bad_axis(self):
        with self.assertRaises(HR.RouteError):
            HR.normalize({'steps': [{'axis': 9, 'pulses': 10}]})

    def test_negative_pulses(self):
        with self.assertRaises(HR.RouteError):
            HR.normalize({'steps': [{'axis': 4, 'pulses': -5}]})

    def test_bad_dir(self):
        with self.assertRaises(HR.RouteError):
            HR.normalize({'steps': [{'axis': 4, 'pulses': 5, 'dir': 2}]})

    def test_rpm_range(self):
        with self.assertRaises(HR.RouteError):
            HR.normalize({'steps': [{'axis': 4, 'pulses': 5, 'rpm': 0}]})

    def test_enabled_but_all_zero(self):
        with self.assertRaises(HR.RouteError):
            HR.normalize({'enabled': True,
                          'steps': [{'axis': 4, 'pulses': 0}]})

    def test_disabled_allows_empty(self):
        cfg = HR.normalize({'enabled': False, 'steps': [{'axis': 4, 'pulses': 0}]})
        self.assertFalse(cfg['enabled'])


class TestDescribeAndTiming(unittest.TestCase):
    def test_describe_skips_zero_steps(self):
        cfg = HR.normalize({'steps': [{'axis': 4, 'pulses': 0},
                                      {'axis': 5, 'pulses': 1200, 'dir': 1}]})
        lines = HR.describe(cfg)
        self.assertEqual(len(lines), 1)
        self.assertIn('Y(5)', lines[0])
        self.assertIn('负', lines[0])

    def test_step_ms_grows_with_pulses(self):
        small = HR.step_ms({'pulses': 100, 'rpm': 60, 'acc': 100})
        big = HR.step_ms({'pulses': 10000, 'rpm': 60, 'acc': 100})
        self.assertGreater(big, small)
        self.assertGreater(small, 600)


class TestLoadSaveState(unittest.TestCase):
    def setUp(self):
        self.tmp = HERE / ('_tmp_hr_%d' % os.getpid())
        self.tmp.mkdir(exist_ok=True)

        def _clean():
            for p in self.tmp.iterdir():
                p.unlink(missing_ok=True)
            self.tmp.rmdir()
        self.addCleanup(_clean)

    def test_route_roundtrip(self):
        path = self.tmp / 'home_route.json'
        cfg = HR.normalize({'steps': [{'axis': 3, 'pulses': 640, 'rpm': 30}],
                            'zero_axes': [3]})
        HR.save(cfg, path)
        got = HR.load(path)
        self.assertEqual(got['steps'][0]['axis'], 3)
        self.assertEqual(got['zero_axes'], [3])
        self.assertIn('updated_at', json.loads(path.read_text(encoding='utf-8')))

    def test_load_missing_returns_defaults(self):
        cfg = HR.load(self.tmp / 'nope.json')
        self.assertEqual(cfg['zero_axes'], [4, 5])

    def test_load_broken_returns_defaults(self):
        path = self.tmp / 'broken.json'
        path.write_text('{oops', encoding='utf-8')
        cfg = HR.load(path)
        self.assertEqual([s['axis'] for s in cfg['steps']], [4, 5, 1])

    def test_state_roundtrip(self):
        path = self.tmp / 'home_state.json'
        self.assertEqual(HR.load_state(path), {})
        self.assertIn('未建立', HR.state_text(HR.load_state(path)))
        HR.save_state(True, [4, 5], 3, path)
        state = HR.load_state(path)
        self.assertTrue(state['ok'])
        self.assertEqual(state['zero_axes'], [4, 5])
        self.assertEqual(state['steps_done'], 3)
        self.assertIn('已建立', HR.state_text(state))

    def test_state_broken(self):
        path = self.tmp / 'home_state.json'
        path.write_text('nope', encoding='utf-8')
        self.assertEqual(HR.load_state(path), {})


if __name__ == '__main__':
    unittest.main()
