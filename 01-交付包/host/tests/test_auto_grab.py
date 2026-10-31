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

    def test_tray_must_be_in_place_before_dropping(self):
        """转盘**必须**在爪子到落料位之前到位（用户 2026-10-31 明确）：

        没转完就等（宁可多等），转盘报错则中止本件、不降 Z 不松爪——绝不落错盒。
        """
        client = FakeClient()
        client.counts = {1: 0, 3: 0, 4: 0, 5: 0}
        gate = threading.Event()
        orig_turn = AG.tray_turn

        def _blocking_turn(*_a, **_k):
            gate.wait(2.0)          # 模拟"转盘比抓取慢，还没转完"
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
        self.assertGreaterEqual(dt, 1.0, '转盘没到位就必须等它（不能提前降 Z 松爪）')
        self.assertTrue([t for t in logs if '还没到位 → 等它' in t], logs)
        self.assertIn(('gripper', ('open',)), [(c[0], c[1]) for c in client.calls])

    def test_tray_error_aborts_before_dropping(self):
        """转盘报错 → 不降 Z、不松爪，整件抛 GrabError（防落错盒）。"""
        client = FakeClient()
        client.counts = {1: 0, 3: 0, 4: 0, 5: 0}
        orig_turn = AG.tray_turn

        def _bad_turn(*_a, **_k):
            raise RuntimeError('motor_pos,3 超时')

        AG.tray_turn = _bad_turn
        try:
            with self.assertRaises(AG.GrabError):
                AG.run_once(client, {'center_tray': [0.5, 0.5]}, log=lambda _t: None,
                            wait=lambda rid, ms: client.done_of(rid), calib=_calib(),
                            pos_source=lambda: ({}, {}, dict(client.counts)),
                            tray_deg=60.0, tray_early=True, state={})
        finally:
            AG.tray_turn = orig_turn
        self.assertNotIn(('gripper', ('open',)), [(c[0], c[1]) for c in client.calls],
                         '转盘没转好就绝不能松爪')


class ReleasePacingTest(unittest.TestCase):
    """松爪必须有「张嘴时间」：open 与 ready 之间要留够舵机从 135° 张到 180°。

    2026-10-31 现场：把 SETTLE_OPEN_MS 清零后，open/ready 相隔 ~1 ms 下发，
    固件 `host_cmd_gripper` 收到 ready 立刻 Servo_SetAngle(168) 覆盖 open，
    爪子张不开、物块不掉，用户看到的就是「爪子没松/预备角度没了」。
    """

    def _run(self):
        client = FakeClient()
        events = []
        orig_send = client.send

        def send(name, *args, **kw):
            events.append(('send', name, args))
            return orig_send(name, *args, **kw)

        class _Time:
            """只拦 sleep 记时长，其余属性转给真实 time（不真的睡）。"""

            @staticmethod
            def sleep(secs):
                events.append(('sleep', float(secs), ()))

            def __getattr__(self, name):
                return getattr(time, name)

        client.send = send
        orig_time = AG.time
        AG.time = _Time()
        try:
            res = AG.run_once(client, {'center_tray': [0.5, 0.5]}, log=lambda _t: None,
                              wait=lambda rid, ms: client.done_of(rid), calib=_calib(),
                              pos_source=lambda: ({}, {}, dict(client.counts)),
                              tray_deg=0.0)          # 不转盘，专看松爪节奏
        finally:
            AG.time = orig_time
        return res, events

    def test_open_then_ready_keeps_a_mouth_opening_gap(self):
        res, events = self._run()
        self.assertTrue(res['ok'])
        names = [(e[1], e[2]) for e in events]          # (cmd, args)
        idx_open = names.index(('gripper', ('open',)))
        idx_ready = names.index(('gripper', ('ready',)))
        self.assertLess(idx_open, idx_ready)
        gaps = [e[1] for e in events[idx_open:idx_ready] if e[0] == 'sleep']
        self.assertTrue(gaps, 'open 与 ready 之间必须有一段时间（否则舵机张不开）')
        self.assertGreaterEqual(max(gaps), 0.15,
                                '张嘴时间至少 150 ms（舵机 135°→180° 约 0.1~0.2 s）')


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
    """一键启动要「点完就动」：识别期间先把「①Y 进通道 → ②Z 抬顶」走掉。

    用户 2026-10-31 明确的顺序：识别期间「先动 y，然后动 z，然后等待识别结果，
    再移动 x、y 轴」；每件松爪后也是「立刻拍照识别，同时 z 抬顶」——就是这条线程。
    预走是安全敏感的（Z 不在底时绝不能动 Y 进通道），所以两个方向都要测：
    在底 → 先 Y 后 Z 都走掉；不在底 / 没读数 → 只打一行 "跳过"，按常规顺序走。
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
        self.assertIn('已在通道末端抬到顶', ''.join(pre))
        full = '\n'.join(logs)
        self.assertIn('识别完成：', full)
        # 顺序：先 Y、再 Z，而且都在识别完成之前（识别期间机械臂自己在走）
        iy = full.index('①Y 进通道 开始')
        iz = full.index('②Z 抬顶 开始')
        iend = full.index('识别完成：')
        self.assertLess(iy, iz, full)
        self.assertLess(iz, iend, full)

    def test_prefetch_moves_y_then_z_before_recognition(self):
        """预走 = 先 Y（-13144 进通道）再 Z（-152000 抬顶），都在识别回来之前走完。"""
        logs = []
        client, res = self._run({}, logs)
        self.assertEqual(res['done'], 1)
        gotos = client.gotos()
        y_gotos = [c for c in gotos if int(c[1][0]) == AG.AXIS_Y]
        z_gotos = [c for c in gotos if int(c[1][0]) == AG.AXIS_Z]
        self.assertTrue(y_gotos and z_gotos)
        self.assertEqual(int(y_gotos[0][1][1]), AG.P1_Y)      # 从 0 往通道里走
        self.assertEqual(int(z_gotos[0][1][1]), AG.Z_UP)      # 抬到顶（不是减高度）
        # Y 必须排在 Z 前面（穿通道时 Z 要在底）
        calls = client.calls
        iy = [i for i, c in enumerate(calls) if c[0] == 'goto' and int(c[1][0]) == AG.AXIS_Y][0]
        iz = [i for i, c in enumerate(calls) if c[0] == 'goto' and int(c[1][0]) == AG.AXIS_Z][0]
        self.assertLess(iy, iz, calls)

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


class BoxForTests(unittest.TestCase):
    """分盒不再会因为「没定名」或「6 个盒都占满」停批（2026-10-31 现场两次停机）。

    07:41 / 07:47 两次 `分盒失败：6 个盒子都已分配给其它类别` 都是这个原因：
    视觉给出 `待登记|红`、`副摄待确认|蓝/浅蓝` 这种没定名的键，被当成新类别
    占了盒子；6 类一满，第 7 件就 break 整批停。
    """

    def setUp(self):
        import box_map
        self._bm = box_map
        self._old_path = box_map.DEFAULT_PATH
        tmp = Path(__file__).resolve().parent / '_tmp'
        tmp.mkdir(parents=True, exist_ok=True)
        box_map.DEFAULT_PATH = tmp / ('box_for_%d.json' % int(time.time() * 1000))

    def tearDown(self):
        try:
            self._bm.DEFAULT_PATH.unlink()
        except Exception:                       # noqa: BLE001
            pass
        self._bm.DEFAULT_PATH = self._old_path

    def test_named_new_key_gets_a_free_box(self):
        box, used = AG.box_for('长方体|蓝', log=lambda _t: None)
        self.assertEqual((box, used), (1, '长方体|蓝'))
        self.assertEqual(self._bm.load(), {'长方体|蓝': 1})

    def test_unnamed_key_reuses_same_colour_box(self):
        self._bm.assign('椭圆柱|红')            # 1 号盒
        logs = []
        box, used = AG.box_for('待登记|红', log=logs.append)
        self.assertEqual(box, 1)
        self.assertEqual(used, '椭圆柱|红')
        self.assertEqual(self._bm.load(), {'椭圆柱|红': 1}, '没定名的件不占新盒')
        self.assertTrue(any('没定名' in t for t in logs), logs)

    def test_unnamed_key_takes_free_box_and_registers_itself(self):
        box, used = AG.box_for('待登记|蓝', log=lambda _t: None)
        self.assertEqual((box, used), (1, '待登记|蓝'))
        self.assertEqual(self._bm.load(), {'待登记|蓝': 1})

    def test_full_table_reuses_last_box_instead_of_raising(self):
        for key in ('圆锥|黄', '长方体|蓝', '长方体|红', '四棱锥|紫', '椭圆柱|红', '六棱锥|白'):
            self._bm.assign(key)
        logs = []
        box, used = AG.box_for('长方体|紫', log=logs.append)   # 第 7 类：不再抛异常
        self.assertIn(box, (1, 2, 3, 4, 5, 6))
        self.assertIn(used, set(self._bm.load()))
        self.assertEqual(len(self._bm.load()), 6, '满盒时不再登记新类别')
        self.assertTrue(any('拿不到空盒' in t for t in logs), logs)

    def test_unnamed_when_table_full_does_not_raise(self):
        for key in ('圆锥|黄', '长方体|蓝', '长方体|红', '四棱锥|紫', '椭圆柱|红', '六棱锥|白'):
            self._bm.assign(key)
        box, used = AG.box_for('待登记|绿', log=lambda _t: None)
        self.assertIn(box, (1, 2, 3, 4, 5, 6))
        self.assertEqual(len(self._bm.load()), 6)

    def test_is_named_knows_the_visual_placeholders(self):
        for key in ('待登记|红', '副摄待确认|蓝/浅蓝', '—|蓝', '未知|红', '|红'):
            self.assertFalse(AG.is_named(key), key)
        for key in ('长方体|蓝', '圆锥|黄', '六棱锥|白'):
            self.assertTrue(AG.is_named(key), key)

    def test_pick_order_prefers_named_targets(self):
        named = {'shape': '长方体', 'color': '蓝', 'confidence': 0.30,
                 'area_px': 1000.0, 'center_tray': (0.4, 0.6)}
        unnamed = {'shape': '待登记', 'color': '红', 'confidence': 0.95,
                   'area_px': 9000.0, 'center_tray': (0.1, 0.1)}
        ordered = AG.pick_order([unnamed, named])
        self.assertEqual(ordered[0], named, '定名的先抓，别让没定名的白占分盒名额')
        self.assertEqual(ordered[1], unnamed)


if __name__ == '__main__':
    unittest.main()
