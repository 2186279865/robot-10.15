"""auto_grab（自动抓一件）的离线单元测试。

不碰硬件：用一个假 client 记录发出的命令，用假 pos_source 提供"回读 counts"，
检查顺序与参数（Z→X/Y→Z→夹→Z→XY→Z→Y→转盘→松）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HOST = Path(__file__).resolve().parent.parent
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

import auto_grab as AG          # noqa: E402


class FakeClient:
    online = True

    def __init__(self):
        self.calls = []
        self._done = {}
        self._n = 0
        #: 模拟"回读 counts"：goto 一次就累加，让 move_abs 每一步都能算出正确增量
        self.counts = {1: 0, 3: 0, 4: 0, 5: 0}

    def now_ms(self):
        return 1000

    def send(self, name, *args, timeout_ms=None):
        self._n += 1
        self.calls.append((name, args))
        self._done[self._n] = {'kind': 'reply', 'code': '',
                               'pairs': {'ok': '1'}}
        return self._n

    def goto(self, axis, pulses, tol=None, rpm=None, timeout_ms=None):
        self._n += 1
        self.calls.append(('goto', (int(axis), int(pulses), int(rpm or 0))))
        self.counts[int(axis)] = self.counts.get(int(axis), 0) + int(pulses) * 20.48
        self._done[self._n] = {'kind': 'reply', 'code': '', 'pairs': {'ok': '1'}}
        return self._n

    def angle(self, axis, deg, rpm=None, timeout_ms=None):
        self._n += 1
        self.calls.append(('angle', (int(axis), float(deg))))
        self._done[self._n] = {'kind': 'reply', 'code': '', 'pairs': {'ok': '1'}}
        return self._n

    def done_of(self, req_id):
        return self._done.get(int(req_id))

    def gotos(self):
        return [c for c in self.calls if c[0] == 'goto']


def _calib():
    return {'affine': {'a': 6000.0, 'b': 0.0, 'c': 0.0,
                       'd': 130000.0, 'tx': -1000.0, 'ty': -69000.0}}


class TargetMathTest(unittest.TestCase):
    def test_target_xy_uses_affine(self):
        target = {'center_tray': [0.5, 0.5]}
        x, y = AG.target_xy(target, calib=_calib())
        self.assertAlmostEqual(x, 6000.0 * 0.5 - 1000.0)
        self.assertAlmostEqual(y, 130000.0 * 0.5 - 69000.0)

    def test_target_xy_rejects_bad_calib(self):
        with self.assertRaises(AG.GrabError):
            AG.target_xy({'center_tray': [0.1, 0.2]}, calib={'affine': {'a': 1}})

    def test_pick_target_prefers_confidence_then_area(self):
        low = {'center_tray': [0.1, 0.1], 'confidence': 0.5, 'area_px': 5000}
        high_small = {'center_tray': [0.2, 0.2], 'confidence': 0.9, 'area_px': 1000}
        high_big = {'center_tray': [0.3, 0.3], 'confidence': 0.9, 'area_px': 9000}
        self.assertIs(AG.pick_target([low, high_small, high_big]), high_big)

    def test_pick_target_empty_raises(self):
        with self.assertRaises(AG.GrabError):
            AG.pick_target([])

    def test_describe_contains_center(self):
        text = AG.describe({'center_tray': [0.5, 0.25], 'class_name': '正方体',
                            'color': '绿'})
        self.assertIn('u=0.50', text)
        self.assertIn('v=0.25', text)
        self.assertIn('正方体', text)

    def test_load_targets_skips_entries_without_center(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'latest_targets.json'
            path.write_text(json.dumps({'targets': [
                {'center_tray': [0.2, 0.3]},
                {'center_tray': None},
                {'no_center': 1},
            ]}), encoding='utf-8')
            self.assertEqual(len(AG.load_targets(path)), 1)


class SequenceTest(unittest.TestCase):
    def test_run_once_order_and_commands(self):
        client = FakeClient()
        target = {'center_tray': [0.5, 0.5], 'confidence': 1.0}
        # 回读 counts 跟着假 client 的 goto 走（模拟真实回读）
        pos_source = lambda: ({}, {}, dict(client.counts))   # noqa: E731
        result = AG.run_once(client, target, log=lambda _t: None,
                             wait=lambda rid, ms: client.done_of(rid),
                             calib=_calib(), pos_source=pos_source)
        self.assertTrue(result['ok'])
        self.assertAlmostEqual(result['elapsed'] >= 0, True)

        axes = [c[1][0] for c in client.gotos() if c[1][0] != AG.AXIS_TRAY]  # 转盘单独校验
        # 顺序（固定路线硬规矩：穿通道时 Z 必须在底部）：
        # Y 进通道 → Z 抬顶 → [X/Y 并行 motor_pos] → Z 降底 → Z 抬顶 → [X/Y 并行] → Z 降底 → Y 退 P0
        # 注意：X/Y 现在是 **一步并行**（motor_pos），不再逐个 goto；假 client 不更新编码器，
        # 所以残差保护会判定"读数滞后"而不补走 → gotos 里只剩 Y/Z。
        self.assertEqual(axes, [5, 1, 1, 1, 1, 5])
        _mp = [c[1][0] for c in client.calls
               if c[0] == 'motor_pos' and int(c[1][0]) in (AG.AXIS_X, AG.AXIS_Y)]
        self.assertIn(AG.AXIS_X, _mp)
        self.assertIn(AG.AXIS_Y, _mp)

        z_gotos = [c[1][1] for c in client.gotos() if c[1][0] == 1]
        self.assertEqual(z_gotos[0], AG.Z_UP)      # 到通道末端后抬到顶
        self.assertEqual(z_gotos[1], AG.Z_DOWN - AG.Z_UP)   # 降到抓取位（增量）
        grippers = [c[1][0] for c in client.calls if c[0] == 'gripper']
        self.assertEqual(grippers, ['grab', 'open', 'ready'])
        # 落货转盘：速度模式匀速（rpm=TRAY_RPM），到点前刹车，残差大才补一次
        # 转盘改为「位置模式 + acc=1（驱动器慢斜坡）+ 一次算好脉冲」
        tray = [c for c in client.calls
                if c[0] == 'motor_pos' and int(c[1][0]) == AG.AXIS_TRAY]
        self.assertGreaterEqual(len(tray), 1)
        args = tray[0][1]                              # (addr, dir, rpm, acc, pulses, mode)
        self.assertEqual(int(args[1]), 0)              # 正向
        self.assertEqual(int(args[3]), AG.TRAY_ACC)    # acc=1 慢斜坡
        self.assertEqual(str(args[5]), 'rel')          # 相对脉冲
        self.assertGreater(int(args[4]), 0)
        # 位置模式一次到位：不再需要 motor_stop（驱动器自己减速停在目标点）

    def test_run_once_refuses_when_offline(self):
        client = FakeClient()
        client.online = False
        with self.assertRaises(AG.GrabError):
            AG.run_once(client, {'center_tray': [0.5, 0.5]}, log=lambda _t: None,
                        calib=_calib())

    def test_run_once_refuses_absurd_target(self):
        client = FakeClient()
        calib = {'affine': {'a': 900000.0, 'b': 0.0, 'c': 0.0,
                            'd': 900000.0, 'tx': 0.0, 'ty': 0.0}}
        with self.assertRaises(AG.GrabError):
            AG.run_once(client, {'center_tray': [0.5, 0.5]}, log=lambda _t: None,
                        calib=calib, pos_source=lambda: ({}, {}, {}))


class RunManyEventTest(unittest.TestCase):
    """run_many 必须走同一套 run_once，并把事件回调给界面用。"""

    def test_events_and_single_item(self):
        client = FakeClient()
        pos_source = lambda: ({}, {}, dict(client.counts))   # noqa: E731
        events = []
        orig_load = AG.load_targets
        orig_pick = AG.pick_target
        AG.load_targets = lambda *a, **k: [{'center_tray': [0.5, 0.5],
                                            'confidence': 1.0,
                                            'shape': '正方体/正方形',
                                            'color': '绿'}]
        AG.pick_target = lambda targets, **k: targets[0]
        try:
            res = AG.run_many(client, 1, lambda: None, log=lambda _t: None,
                              wait=lambda rid, ms: client.done_of(rid),
                              pos_source=pos_source, drop=False,
                              on_event=events.append)
        finally:
            AG.load_targets = orig_load
            AG.pick_target = orig_pick
        self.assertEqual(res['done'], 1)
        kinds = [e['kind'] for e in events]
        self.assertIn('item_start', kinds)
        self.assertIn('target', kinds)
        self.assertIn('item_ok', kinds)
        self.assertEqual(kinds[-1], 'finished')
        self.assertEqual(events[0]['n'], 1)


if __name__ == '__main__':
    unittest.main()
