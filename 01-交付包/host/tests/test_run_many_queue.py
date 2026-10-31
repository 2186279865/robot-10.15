"""连续抓（run_many）行为测试：一次识别→队列逐件，且每件都调用 run_once。"""
import sys
from pathlib import Path
import unittest
from unittest import mock

HOST = Path(__file__).resolve().parents[1]
for p in (str(HOST), str(HOST / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import auto_grab as AG


class _FakeClient:
    online = True

    def __init__(self):
        self.counts = {1: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}
        self.sent = []

    @property
    def last_counts(self):                 # 开批校准（全开环账本）要 X/Y 起点
        return {4: {'counts': 0.0},
                5: {'counts': float(AG.P1_Y) * 20.48}}

    def tick(self):
        return None

    def send(self, cmd, *args, **kw):
        self.sent.append((cmd,) + args)
        return 1

    def done_of(self, rid):
        return {'id': rid, 'ok': True, 'pairs': {}}

    def goto(self, *a, **kw):
        return {'ok': True}


def _targets(n):
    out = []
    for i in range(n):
        out.append({'center_tray': [0.2 + 0.1 * i, 0.3 + 0.1 * i],
                    'confidence': 0.9, 'area_px': 10000 + i,
                    'shape': '正方体/正方形', 'color': '红'})
    return out


class RunManyQueueTest(unittest.TestCase):
    def test_recognize_once_then_queue(self):
        """3 件只应识别 1 次，且 run_once 被调用 3 次（每件流程与单件相同）。"""
        calls = {'recognize': 0, 'run_once': 0}

        def fake_recognize():
            calls['recognize'] += 1

        def fake_run_once(client, target, **kw):
            calls['run_once'] += 1
            return {'ok': True, 'target': AG.describe(target)}

        with mock.patch.object(AG, 'load_targets', lambda *a, **k: _targets(3)), \
             mock.patch.object(AG, 'run_once', fake_run_once):
            res = AG.run_many(_FakeClient(), 3, fake_recognize, log=lambda _t: None,
                              wait=lambda rid, ms: {'id': rid, 'ok': True, 'pairs': {}},
                              pos_source=lambda: ({}, {}, {}), drop=False)

        self.assertEqual(res['done'], 3)
        self.assertEqual(res['failed'], 0)
        self.assertEqual(calls['recognize'], 1, '队列模式只识别一次')
        self.assertEqual(calls['run_once'], 3, '每件都必须走一次 run_once')

    def test_recognize_each_mode(self):
        """recognize_each=True 时退回“每件都识别”。"""
        calls = {'recognize': 0}

        def fake_recognize():
            calls['recognize'] += 1

        with mock.patch.object(AG, 'load_targets', lambda *a, **k: _targets(2)), \
             mock.patch.object(AG, 'run_once', lambda *a, **k: {'ok': True}):
            res = AG.run_many(_FakeClient(), 2, fake_recognize, log=lambda _t: None,
                              wait=lambda rid, ms: {'id': rid, 'ok': True, 'pairs': {}},
                              pos_source=lambda: ({}, {}, {}), drop=False,
                              recognize_each=True)
        self.assertEqual(res['done'], 2)
        self.assertEqual(calls['recognize'], 2)

    def test_failure_retries_once_then_stops(self):
        """单件失败 → 重试一次；仍失败则整条结束（done=0, failed=1）。"""
        calls = {'run_once': 0}

        def boom(*a, **k):
            calls['run_once'] += 1
            raise AG.GrabError('E09 busy')

        with mock.patch.object(AG, 'load_targets', lambda *a, **k: _targets(2)), \
             mock.patch.object(AG, 'run_once', boom), \
             mock.patch.object(AG.time, 'sleep', lambda s: None):
            res = AG.run_many(_FakeClient(), 2, lambda: None, log=lambda _t: None,
                              wait=lambda rid, ms: {'id': rid, 'ok': True, 'pairs': {}},
                              pos_source=lambda: ({}, {}, {}), drop=False)
        self.assertEqual(res['done'], 0)
        self.assertEqual(res['failed'], 1)
        self.assertEqual(calls['run_once'], 2, '首次 + 一次重试')


if __name__ == '__main__':
    unittest.main()
