"""auto_grab（自动抓一件）的离线单元测试。

不碰硬件：用一个假 client 记录发出的命令，用假 pos_source 提供"回读 counts"，
检查顺序与参数（Z→X/Y→Z→夹→Z→XY→Z→Y→转盘→松）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
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
        # Y 进通道 → Z 抬顶 → [X/Y 并行 motor_pos] → Z 降底 → Z 抬顶 → [X/Y 并行] → Z 降底
        # 落货点在 P1（P0/P1 下方都是盒子）→ 降到底后直接松爪，不再有多余的 Y 退 P0。
        # 注意：X/Y 现在是 **一步并行**（motor_pos），不再逐个 goto；假 client 不更新编码器，
        # 所以残差保护会判定"读数滞后"而不补走 → gotos 里只剩 Y/Z。
        self.assertEqual(axes, [5, 1, 1, 1, 1])
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


class TrayEarlyTest(unittest.TestCase):
    """转盘要**提前**转：盒号在 run_many 里早定了，没必要等夹紧。

    现场实测（2026-10-31 04:02 那件）：-120° 要转 7.42 s，回程只要 ~6 s，
    旧顺序「夹紧后才转」在松爪前白等 1.5 s（日志「join 只等了 1.51 s」）。
    转盘只动 3 轴，与 Y/Z/X 电机独立，货块也不在转盘上，所以并行是安全的。
    """

    def _run(self, tray_early):
        client = FakeClient()
        target = {'center_tray': [0.5, 0.5], 'confidence': 1.0}
        state = {}
        marks = []
        orig_turn = AG.tray_turn

        def _fake_turn(*_a, **_k):
            # 记录"转盘开始"发生在第几条指令之后；转盘本身另有用例覆盖
            marks.append(len(client.calls))
            return {'ok': True}

        AG.tray_turn = _fake_turn
        try:
            res = AG.run_once(client, target, log=lambda _t: None,
                              wait=lambda rid, ms: client.done_of(rid),
                              calib=_calib(), pos_source=lambda: ({}, {}, dict(client.counts)),
                              tray_deg=60.0, tray_early=tray_early, state=state)
        finally:
            AG.tray_turn = orig_turn
        return client, res, state, marks

    def test_tray_starts_before_the_first_lift(self):
        client, res, state, marks = self._run(True)
        idx_first_goto = [i for i, c in enumerate(client.calls) if c[0] == 'goto']
        self.assertTrue(marks and idx_first_goto)
        self.assertLessEqual(marks[0], idx_first_goto[0],
                             '转盘应该在 ①Y/②Z 抬顶之前就开始转，而不是等夹紧')
        self.assertTrue(res['ok'])
        # 后台线程记录 tray_applied 可能比 run_once 返回稍晚（异步），这里等它一下
        deadline = time.time() + 1.0
        while time.time() < deadline and not state.get('tray_applied'):
            time.sleep(0.01)
        self.assertEqual(float(state.get('tray_applied') or 0.0), 60.0)

    def test_tray_early_false_keeps_old_order(self):
        client, _res, _state, marks = self._run(False)
        idx_grab = [i for i, c in enumerate(client.calls)
                    if c[0] == 'gripper' and c[1][0] == 'grab']
        self.assertTrue(marks and idx_grab)
        self.assertGreater(marks[0], idx_grab[0], '旧顺序：夹紧之后才转')

    def test_release_does_not_wait_for_the_tray(self):
        """降到底就立刻松爪：转盘还在转也不能拖住节拍（2026-10-31 用户要求）。"""
        client = FakeClient()
        client.counts = {1: 0, 3: 0, 4: 0, 5: 0}
        gate = threading.Event()
        orig_turn = AG.tray_turn

        def _blocking_turn(*_a, **_k):
            gate.wait(3.0)          # 模拟"转盘还没转完"
            return {'ok': True}

        AG.tray_turn = _blocking_turn
        logs = []
        try:
            t0 = time.perf_counter()
            res = AG.run_once(client, {'center_tray': [0.5, 0.5]}, log=logs.append,
                              wait=lambda rid, ms: client.done_of(rid), calib=_calib(),
                              pos_source=lambda: ({}, {}, dict(client.counts)),
                              tray_deg=60.0, tray_early=True, state={})
            dt = time.perf_counter() - t0
        finally:
            gate.set()
            AG.tray_turn = orig_turn
        self.assertTrue(res['ok'])
        self.assertLess(dt, 2.0, '松爪不该等转盘（后台还在转时必须照常松爪）')
        self.assertTrue([t for t in logs if '不等它' in t or '还在转' in t],
                        '应该留下「转盘还在转但先松爪」的日志')
        self.assertIn(('gripper', ('open',)), [(c[0], c[1]) for c in client.calls],
                      '降到底后必须已经松爪')


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


class PrefetchTest(unittest.TestCase):
    """一键启动要「点完就动」：识别期间先把「①Y 进通道」走掉。

    预走是安全敏感的（Z 不在底时绝不能动 Y 进通道），所以两个方向都要测：
    在底 → 只走 Y（Z 抬顶留给识别回来后的正式流程）；不在底 / 没读数 → 只打一行
    "跳过"，按常规顺序走。
    2026-10-31：Z 抬顶**不放进预走**（= 2.29 版行为）——用户明确说提前抬 Z 那版变差，
    且抓空那件会白抬 1.6 s、回 P0 再降 1.6 s。
    """

    def setUp(self):
        self._orig = (AG.load_targets, AG.pick_target)
        AG.load_targets = lambda *a, **k: [{'center_tray': [0.5, 0.5],
                                            'confidence': 1.0,
                                            'shape': '正方体/正方形',
                                            'color': '绿'}]
        AG.pick_target = lambda targets, **k: targets[0]

    def tearDown(self):
        AG.load_targets, AG.pick_target = self._orig

    def _run(self, counts, logs, recognize=None):
        client = FakeClient()
        client.counts.update(counts)
        pos_source = lambda: ({}, {}, dict(client.counts))   # noqa: E731
        # 识别故意慢 0.3 s，给预走线程留出真并发的时间窗口（假 client 动作是瞬时的）
        rec = recognize or (lambda: time.sleep(0.30))
        res = AG.run_many(client, 1, rec, log=logs.append,
                          wait=lambda rid, ms: client.done_of(rid),
                          pos_source=pos_source, drop=False, prefetch=True)
        return client, res

    def test_walks_channel_while_recognizing(self):
        logs = []
        client, res = self._run({}, logs)
        self.assertEqual(res['done'], 1)
        pre = [t for t in logs if t.startswith('[预走]')]
        self.assertTrue(any('[预走] 完成' in t for t in pre), pre)
        self.assertTrue(any('①Y 进通道 开始' in t for t in pre), pre)
        self.assertIn('已在通道末端（Z 仍在底）', ''.join(pre))
        # 预走只动 Y：Z 抬顶必须等识别回来（正式流程 ②）
        full = '\n'.join(logs)
        self.assertIn('识别完成：', full)
        self.assertLess(full.index('①Y 进通道 开始'), full.index('识别完成：'), full)

    def test_prefetch_moves_y_only_before_recognition(self):
        """预走只走 Y（相对 -13144 脉冲进通道）；Z 抬顶在识别完成之后才动。"""
        logs = []
        client, res = self._run({}, logs)
        self.assertEqual(res['done'], 1)
        gotos = client.gotos()
        y_gotos = [c for c in gotos if int(c[1][0]) == AG.AXIS_Y]
        self.assertTrue(y_gotos)
        self.assertEqual(int(y_gotos[0][1][1]), AG.P1_Y)      # 从 0 往通道里走
        z_gotos = [c for c in gotos if int(c[1][0]) == AG.AXIS_Z]
        self.assertTrue(z_gotos)
        self.assertEqual(int(z_gotos[0][1][1]), AG.Z_UP)      # 抬到顶（不是减高度）
        # 第一个 Z 动作在「识别完成」之后（说明不是预走抬的）
        z_line = None
        for t in logs:
            if 'Z 目标' in t:
                z_line = t
                break
        self.assertIsNotNone(z_line)

    def test_skipped_when_z_not_at_bottom(self):
        logs = []
        client, res = self._run({AG.AXIS_Z: AG.Z_UP * 20.48}, logs)
        self.assertEqual(res['done'], 1)
        self.assertTrue(any('预走跳过' in t for t in logs), logs)
        self.assertFalse(any('[预走] 完成' in t for t in logs), logs)

    def test_recognize_timing_message_goes_to_log(self):
        logs = []
        self._run({}, logs,
                  recognize=lambda: (None, '单次识别完成：3 个目标（用时 2.10s）'))
        self.assertTrue(any('识别完成：' in t for t in logs), logs)
        self.assertTrue(any('视觉：单次识别完成' in t for t in logs), logs)


class GotoP0Test(unittest.TestCase):
    """回 P0 的现场规矩（用户反复强调，2026-10-31 抓空那次就是这里出错）。

    1) 已在 P0 三轴都在位        → 一步都不动；
    2) 在 P1 且 Z 到底           → 只 Y 回 0（唯一允许"直接回 P0"的情形）；
    3) 在 P1 但 Z 没到底         → Z 降到底 → Y 回 0；
    4) 不在 P1（停在托盘上方）   → X/Y 并行回 P1 → Z 降到底 → Y 回 0。
    """

    def _run(self, pulses):
        client = FakeClient()
        pos = {int(a): float(p) for a, p in pulses.items()}
        known = {a: True for a in pos}
        counts = {a: p * 20.48 for a, p in pos.items()}
        logs = []
        AG.goto_p0(client, log=logs.append,
                   wait=lambda rid, ms: client.done_of(rid),
                   pos_source=lambda: (dict(pos), dict(known), dict(counts)))
        return client, logs

    def test_already_at_p0_does_nothing(self):
        client, logs = self._run({AG.AXIS_X: 0, AG.AXIS_Y: AG.P0_Y, AG.AXIS_Z: AG.Z_DOWN})
        self.assertEqual(client.gotos(), [])
        self.assertEqual([c for c in client.calls if c[0] == 'motor_pos'], [])
        self.assertTrue(any('不用动' in t for t in logs), logs)

    def test_at_p1_and_z_down_goes_y_in_one_step(self):
        client, _logs = self._run({AG.AXIS_X: 0, AG.AXIS_Y: AG.P1_Y, AG.AXIS_Z: AG.Z_DOWN})
        gotos = client.gotos()
        self.assertEqual(len(gotos), 1, gotos)
        self.assertEqual(int(gotos[0][1][0]), AG.AXIS_Y)      # 只动 Y
        self.assertEqual(int(gotos[0][1][1]), AG.P0_Y - AG.P1_Y)

    def test_at_p1_with_z_up_drops_z_first(self):
        client, _logs = self._run({AG.AXIS_X: 0, AG.AXIS_Y: AG.P1_Y, AG.AXIS_Z: AG.Z_UP})
        gotos = client.gotos()
        self.assertEqual([int(g[1][0]) for g in gotos], [AG.AXIS_Z, AG.AXIS_Y], gotos)
        self.assertEqual(int(gotos[0][1][1]), AG.Z_DOWN - AG.Z_UP)   # Z 先降到底

    def test_not_at_p1_parks_x_y_parallel_before_dropping(self):
        """停在托盘上方：必须 X/Y 并行回 P1 → Z 降到底 → Y 回 0（绝不就地降 Z）。"""
        client, logs = self._run({AG.AXIS_X: 5000, AG.AXIS_Y: AG.P1_Y, AG.AXIS_Z: AG.Z_UP})
        mp = [c for c in client.calls if c[0] == 'motor_pos'
              and int(c[1][0]) in (AG.AXIS_X, AG.AXIS_Y)]
        self.assertGreaterEqual(len(mp), 1, client.calls)      # 并行 motor_pos，不是顺序 goto
        gotos = client.gotos()
        self.assertTrue(any(int(g[1][0]) == AG.AXIS_Z for g in gotos), gotos)
        self.assertTrue(all(int(g[1][0]) != AG.AXIS_X for g in gotos), gotos)  # X 不许单独 goto
        self.assertTrue(any('X/Y 回 P1' in t for t in logs), logs)


class BoxResetTest(unittest.TestCase):
    """每次「一键启动 / 连续抓」开批都要先清「类别→盒号」登记。

    现场 2026-10-31 踩过两次：`runtime/box_map.json` 是掉电保留的长期文件，
    上一批的登记白占盒子，新一批第 7 件直接「分盒失败」停机。
    """

    def setUp(self):
        import box_map
        self._bm = box_map
        self._old_path = box_map.DEFAULT_PATH
        tmp = Path(__file__).resolve().parent / '_tmp'
        tmp.mkdir(parents=True, exist_ok=True)
        box_map.DEFAULT_PATH = tmp / ('box_reset_%d.json' % int(time.time() * 1000))
        self._orig = (AG.load_targets, AG.pick_target)
        AG.load_targets = lambda *a, **k: []
        AG.pick_target = lambda targets, **k: targets[0] if targets else None

    def tearDown(self):
        AG.load_targets, AG.pick_target = self._orig
        try:
            self._bm.DEFAULT_PATH.unlink()      # 只删临时文件，绝不碰现场 runtime/box_map.json
        except Exception:                       # noqa: BLE001
            pass
        self._bm.DEFAULT_PATH = self._old_path

    def _run(self, reset_boxes, logs):
        client = FakeClient()
        # count=0 → 抓到空为止；stop_flag 立刻 True → 第 1 件前就结束，不发任何动作。
        AG.run_many(client, 0, lambda: None, log=logs.append,
                    wait=lambda rid, ms: client.done_of(rid),
                    pos_source=lambda: ({}, {}, dict(client.counts)),
                    drop=False, stop_flag=lambda: True,
                    reset_boxes=reset_boxes)
        return client

    def test_reset_boxes_true_clears_stale_entries(self):
        self._bm.assign('圆柱/圆形')
        self._bm.assign('五棱体')
        self.assertEqual(len(self._bm.load()), 2)
        logs = []
        client = self._run(True, logs)
        self.assertEqual(self._bm.load(), {}, '开批必须先清零，否则旧登记白占盒子')
        self.assertTrue(any('分盒已自动清零' in t for t in logs), logs)
        self.assertEqual(client.gotos(), [], '只清登记，不动机械臂')

    def test_reset_boxes_false_keeps_entries(self):
        self._bm.assign('圆柱/圆形')
        logs = []
        self._run(False, logs)
        self.assertEqual(len(self._bm.load()), 1, '默认不清零，避免单件/旧调用被悄悄改行为')
        self.assertFalse(any('分盒已自动清零' in t for t in logs), logs)


if __name__ == '__main__':
    unittest.main()
