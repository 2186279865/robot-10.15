"""抓取点标定（grasp_calib）的单元测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HOST = Path(__file__).resolve().parents[1]
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

import grasp_calib as GC      # noqa: E402

TRUTH = {'a': 6200.0, 'b': 180.0, 'c': 120.0, 'd': 7800.0,
         'tx': 2400.0, 'ty': -2600.0}


def truth_pulse(u, v):
    return (TRUTH['a'] * u + TRUTH['b'] * v + TRUTH['tx'],
            TRUTH['c'] * u + TRUTH['d'] * v + TRUTH['ty'])


def make_pairs(corners=((0.08, 0.12), (0.9, 0.16), (0.88, 0.9), (0.1, 0.88),
                        (0.5, 0.5))):
    return [{'u': u, 'v': v, 'px': truth_pulse(u, v)[0], 'py': truth_pulse(u, v)[1],
             'ts': 0.0} for u, v in corners]


class PairsIoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='grasp_io_'))
        self.path = self.tmp / 'pairs.json'

    def test_add_load_clear(self):
        self.assertEqual(GC.load_pairs(self.path), [])
        GC.add_pair(0.2, 0.3, 1000, 2000, path=self.path)
        GC.add_pair(0.8, 0.7, 3000, 4000, path=self.path)
        pairs = GC.load_pairs(self.path)
        self.assertEqual(len(pairs), 2)
        self.assertAlmostEqual(pairs[1]['px'], 3000.0)
        GC.clear(self.path)
        self.assertEqual(GC.load_pairs(self.path), [])

    def test_bad_uv_rejected(self):
        with self.assertRaises(ValueError):
            GC.add_pair(1.4, 0.3, 0, 0, path=self.path)

    def test_broken_file_is_tolerated(self):
        self.path.write_text('{not json', encoding='utf-8')
        self.assertEqual(GC.load_pairs(self.path), [])

    def test_file_keeps_unit_marker(self):
        GC.add_pair(0.2, 0.3, 1000, 2000, path=self.path)
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(data['unit'], 'pulse')
        self.assertEqual(len(data['pairs']), 1)


class SolveTest(unittest.TestCase):
    def test_exact_pairs_recover_affine(self):
        result = GC.solve(make_pairs(), write=False)
        a = result['affine']
        for key in ('a', 'b', 'c', 'd', 'tx', 'ty'):
            self.assertAlmostEqual(a[key], TRUTH[key], places=2)
        self.assertLess(result['worst_pulse'], 1.0)

    def test_noisy_pairs_residual_small(self):
        import random
        random.seed(7)
        pairs = []
        for u, v in ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)):
            px, py = truth_pulse(u, v)
            pairs.append({'u': u, 'v': v,
                          'px': px + random.uniform(-30, 30),
                          'py': py + random.uniform(-30, 30), 'ts': 0.0})
        result = GC.solve(pairs, write=False)
        self.assertLess(result['worst_pulse'], GC.MAX_RESIDUAL)

    def test_too_few_pairs_rejected(self):
        with self.assertRaises(ValueError):
            GC.solve(make_pairs()[:2], write=False)

    def test_clustered_pairs_rejected(self):
        pairs = [{'u': 0.50, 'v': 0.50, 'px': 0, 'py': 0, 'ts': 0.0},
                 {'u': 0.52, 'v': 0.51, 'px': 10, 'py': 10, 'ts': 0.0},
                 {'u': 0.51, 'v': 0.53, 'px': 5, 'py': 20, 'ts': 0.0}]
        with self.assertRaises(ValueError):
            GC.solve(pairs, write=False)


if __name__ == '__main__':
    unittest.main()
