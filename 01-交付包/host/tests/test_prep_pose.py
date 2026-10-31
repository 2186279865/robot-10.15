"""`prep_pose` 单测：配置读写 + 两步顺序 + 失败即停。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import prep_pose as PP
import protocol as P


class FakeClient:
    def __init__(self, latched=False, fail_on=None, raise_on=None, rep=None):
        self.latched = latched
        self.sent = []
        self.calls = []
        self.fail_on = fail_on          # 轴号：这一步回 err
        self.raise_on = raise_on        # 轴号：goto 抛异常
        self.rep = rep                  # 自定义应答

    def send(self, cmd, *args, **kw):
        self.sent.append((cmd,) + args)
        return len(self.sent)

    def goto(self, axis, pulses, rpm=None, **kw):
        if axis == self.raise_on:
            raise RuntimeError('模拟链路炸了')
        self.calls.append((axis, pulses, rpm))
        return len(self.calls)

    def wait_reply(self, req_id, timeout_ms=8000):
        axis = self.calls[req_id - 1][0]
        if axis == self.fail_on:
            return {'kind': 'err', 'code': 'E14', 'text': '闭环未到位', 'pairs': {'reason': 'lost'}}
        if self.rep is not None:
            return self.rep
        return {'kind': 'reply', 'code': '', 'text': 'motor_goto ok',
                'pairs': {'err': '12', 'passes': '0', 'elapsed_ms': '480'}}


def _cfg(**kw):
    cfg = dict(PP.DEFAULTS)
    cfg.update(kw)
    return cfg


class LoadSaveTests(unittest.TestCase):
    def test_missing_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = PP.load(Path(td) / 'nope.json')
        self.assertEqual(cfg['y_pulses'], PP.DEFAULTS['y_pulses'])
        self.assertEqual(cfg['z_pulses'], PP.DEFAULTS['z_pulses'])

    def test_partial_json_is_merged(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'prep.json'
            p.write_text(json.dumps({'y_pulses': -3000}), encoding='utf-8')
            cfg = PP.load(p)
        self.assertEqual(cfg['y_pulses'], -3000)
        self.assertEqual(cfg['z_pulses'], PP.DEFAULTS['z_pulses'])

    def test_broken_json_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'prep.json'
            p.write_text('{not json', encoding='utf-8')
            self.assertEqual(PP.load(p)['y_pulses'], PP.DEFAULTS['y_pulses'])

    def test_clamped(self):
        cfg = PP._clean({'y_pulses': 999999, 'z_pulses': -999999, 'y_rpm': 0, 'z_rpm': 99999})
        self.assertEqual(cfg['y_pulses'], 15000)
        self.assertEqual(cfg['z_pulses'], -160000)
        self.assertEqual(cfg['y_rpm'], 1)
        self.assertEqual(cfg['z_rpm'], 6000)

    def test_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = PP.save(_cfg(y_pulses=-1234, z_rpm=800), Path(td) / 'sub' / 'prep.json')
            cfg = PP.load(p)
        self.assertEqual(cfg['y_pulses'], -1234)
        self.assertEqual(cfg['z_rpm'], 800)

    def test_describe(self):
        text = PP.describe(_cfg())
        self.assertIn('Y %+d' % PP.DEFAULTS['y_pulses'], text)
        self.assertIn('Z %+d' % PP.DEFAULTS['z_pulses'], text)
        # 两个单位都写出来，避免“界面 counts 当脉冲”看岔
        self.assertIn('%s counts' % format(PP.counts_of(PP.DEFAULTS['y_pulses']), ','), text)

    def test_counts_of(self):
        self.assertEqual(PP.counts_of(1600), 32768)
        self.assertEqual(PP.counts_of(-3200), -65536)

    def test_estimate_timeout_grows_with_distance(self):
        short = PP.estimate_timeout_ms(1600, 300)
        long = PP.estimate_timeout_ms(64000, 300)
        self.assertGreater(long, short)
        self.assertGreaterEqual(short, 8000)


_ZERO = {PP.AXIS_Y: 0, PP.AXIS_Z: 0}


class RunTests(unittest.TestCase):
    def test_order_is_y_then_z(self):
        client = FakeClient()
        res = PP.run(client, _cfg(), log=lambda t: None, counts=_ZERO)
        self.assertEqual([c[0] for c in client.calls], [PP.AXIS_Y, PP.AXIS_Z])
        self.assertEqual(len(res), 2)
        self.assertTrue(all(r['ok'] for r in res))

    def test_latched_clears_stop_first(self):
        client = FakeClient(latched=True)
        PP.run(client, _cfg(), log=lambda t: None, counts=_ZERO)
        self.assertEqual(client.sent[0][0], 'stop_clear')

    def test_stop_clear_always_sent_first(self):
        # 界面上的 latched 会过期（比如重连后），所以无条件先清一次锁存
        client = FakeClient(latched=False)
        PP.run(client, _cfg(), log=lambda t: None, counts=_ZERO)
        cmds = [s[0] for s in client.sent]
        self.assertEqual(cmds[0], 'stop_clear')
        self.assertIn('param_set', cmds)

    def test_failure_stops_after_that_step(self):
        client = FakeClient(fail_on=PP.AXIS_Y)
        res = PP.run(client, _cfg(), log=lambda t: None, counts=_ZERO)
        self.assertEqual(len(res), 1)
        self.assertFalse(res[0]['ok'])
        self.assertEqual(len(client.calls), 1)

    def test_exception_is_reported(self):
        client = FakeClient(raise_on=PP.AXIS_Y)
        res = PP.run(client, _cfg(), log=lambda t: None, counts=_ZERO)
        self.assertEqual(res[0]['code'], 'EXC')
        self.assertIn('RuntimeError', res[0]['text'])

    def test_waiter_timeout(self):
        client = FakeClient()
        res = PP.run(client, _cfg(), log=lambda t: None, counts=_ZERO, wait=lambda rid, ms: None)
        self.assertEqual(res[0]['code'], 'TIMEOUT')
        self.assertEqual(len(res), 1)

    def test_zero_pulses_skipped(self):
        client = FakeClient()
        res = PP.run(client, _cfg(y_pulses=0), log=lambda t: None, counts=_ZERO)
        self.assertEqual([c[0] for c in client.calls], [PP.AXIS_Z])
        self.assertEqual(len(res), 1)

    def test_custom_waiter_is_used(self):
        seen = []

        def waiter(rid, ms):
            seen.append((rid, ms))
            return {'kind': 'reply', 'code': '', 'text': '', 'pairs': {'err': '3'}}

        client = FakeClient()
        PP.run(client, _cfg(), log=lambda t: None, counts=_ZERO, wait=waiter)
        self.assertEqual(len(seen), 2)

class SoftPositionTests(unittest.TestCase):
    """给了软位置就按“目标 - 当前”走，重复点按钮安全。"""

    def test_delta_from_current(self):
        client = FakeClient()
        PP.run(client, _cfg(), log=lambda t: None,
               soft={PP.AXIS_Y: -1000, PP.AXIS_Z: -1000},
               known={PP.AXIS_Y: True, PP.AXIS_Z: True})
        self.assertEqual(client.calls[0], (PP.AXIS_Y, PP.DEFAULTS['y_pulses'] + 1000, PP.DEFAULTS['y_rpm']))
        self.assertEqual(client.calls[1], (PP.AXIS_Z, PP.DEFAULTS['z_pulses'] + 1000, PP.DEFAULTS['z_rpm']))

    def test_already_there_does_nothing(self):
        client = FakeClient()
        res = PP.run(client, _cfg(), log=lambda t: None,
                     soft={PP.AXIS_Y: PP.DEFAULTS['y_pulses'], PP.AXIS_Z: PP.DEFAULTS['z_pulses']},
                     known={PP.AXIS_Y: True, PP.AXIS_Z: True})
        self.assertEqual(client.calls, [])
        self.assertEqual(res, [])

    def test_unknown_position_refuses(self):
        client = FakeClient()
        res = PP.run(client, _cfg(), log=lambda t: None,
                     soft={}, known={PP.AXIS_Y: False, PP.AXIS_Z: False})
        self.assertEqual(client.calls, [])
        self.assertEqual(res[0]['code'], 'POS_UNKNOWN')

class CountsBasedTests(unittest.TestCase):
    """有编码器回读时按 counts 算差额（软位置会不准）。"""

    def test_delta_from_counts(self):
        client = FakeClient()
        # 目标 Y = -13144 脉冲 = -269,189 counts；当前回读 -100,000 counts
        PP.run(client, _cfg(), log=lambda t: None,
               counts={PP.AXIS_Y: -100000, PP.AXIS_Z: PP.counts_of(PP.DEFAULTS['z_pulses'])},
               known={PP.AXIS_Y: True, PP.AXIS_Z: True})
        axis, pulses, rpm = client.calls[0]
        self.assertEqual(axis, PP.AXIS_Y)
        self.assertEqual(pulses, int(round((PP.counts_of(PP.DEFAULTS['y_pulses']) + 100000) / 20.48)))
        self.assertEqual(len(client.calls), 1)   # Z 已在目标位，跳过

    def test_counts_priority_over_soft(self):
        client = FakeClient()
        PP.run(client, _cfg(), log=lambda t: None,
               counts={PP.AXIS_Y: PP.counts_of(PP.DEFAULTS['y_pulses']),
                       PP.AXIS_Z: PP.counts_of(PP.DEFAULTS['z_pulses'])},
               soft={PP.AXIS_Y: 0, PP.AXIS_Z: 0}, known={PP.AXIS_Y: True, PP.AXIS_Z: True})
        self.assertEqual(client.calls, [])

    def test_estimate_uses_real_rpm_without_50_cap(self):
        """不许再把有效转速压到 50 圈/分：那会把 Z 满行程估成 122 s（假超时根源）。"""
        fast = PP.estimate_timeout_ms(32000, 3000)
        slow = PP.estimate_timeout_ms(32000, 300)
        self.assertLess(fast, slow)
        z_ms = PP.estimate_timeout_ms(152000, 2400)      # Z 满行程，实测 1.49 s
        self.assertGreater(z_ms, 2000)
        self.assertLess(z_ms, 20000)

    def test_goto_budget_reasonable(self):
        budget = PP.goto_budget_ms(152000, 2400)
        self.assertGreater(budget, 3000)
        self.assertLess(budget, 20000)


class GotoTolTests(unittest.TestCase):
    """Z 轴闭环容差必须走 GOTO_TOL_BY_AXIS（提速时「抬转速 + 放宽容差」是成对的）。

    2026-10-31：只抬转速不放宽容差 → 固件用 60 圈/分补一趟，Z 单趟反而从 1494 ms
    变成 1848/2182 ms（见交接文档 §8 第 21 条）。这条用例守住「容差真的传下去了」。
    """

    def test_move_abs_passes_per_axis_tol(self):
        seen = {}

        class _C:
            latched = False

            def send(self, *a, **k):
                return 1

            def goto(self, axis, pulses, rpm=None, tol=None, **kw):
                seen[int(axis)] = tol
                return len(seen)

            def wait_reply(self, rid, timeout_ms=8000):
                return {'kind': 'reply', 'code': '', 'text': '', 'pairs': {'ok': '1'}}

        PP.move_abs(_C(), {PP.AXIS_Z: -1000, PP.AXIS_Y: -500},
                    order=[PP.AXIS_Z, PP.AXIS_Y], counts={PP.AXIS_Z: 0.0, PP.AXIS_Y: 0.0})
        self.assertEqual(seen[PP.AXIS_Z], P.GOTO_TOL_BY_AXIS[PP.AXIS_Z])
        self.assertEqual(seen[PP.AXIS_Y], P.GOTO_TOL_BY_AXIS[PP.AXIS_Y])
        self.assertGreater(P.GOTO_TOL_BY_AXIS[PP.AXIS_Z], P.GOTO_TOL_BY_AXIS[PP.AXIS_Y])


class _TeleClient:
    """只有遥测 counts 的假 client：验证「回执迟到时按 counts 判到位」。

    现场 2026-10-31 04:38~04:55：固件 1.0~1.6 s 走完并把 counts 停在目标点，
    回执却迟到 2~15 s（图传挤满链路）→ 旧代码死等回执，表现为「Z 降底后停顿很久」。
    """

    def __init__(self, counts=None, move_fraction=1.0):
        self.counts = {int(k): float(v) for k, v in (counts or {}).items()}
        self.last_counts = {int(k): {'counts': v, 'ts': 0} for k, v in self.counts.items()}
        self.move_fraction = float(move_fraction)
        self.extended = []
        self.sent = []
        self._n = 0

    def _set(self, axis, counts):
        self.counts[int(axis)] = float(counts)
        self.last_counts[int(axis)] = {'counts': float(counts), 'ts': 0}

    def send(self, cmd, *args, **kw):
        self._n += 1
        self.sent.append((cmd, args))
        return self._n

    def goto(self, axis, pulses, rpm=None, tol=None, **kw):
        self._n += 1
        self.sent.append(('goto', (int(axis), int(pulses))))
        cur = self.counts.get(int(axis), 0.0)
        self._set(axis, cur + float(pulses) * PP.COUNTS_PER_PULSE * self.move_fraction)
        return self._n

    def extend_deadline(self, req_id, extra_ms=30000):
        self.extended.append((int(req_id), int(extra_ms)))
        return True


class AwaitGotoTests(unittest.TestCase):
    """`await_goto`：回执优先，回执迟到时按遥测 counts 判到位先继续。"""

    def test_reply_wins_when_it_arrives(self):
        client = _TeleClient({PP.AXIS_Z: 0.0})
        rep, how = PP.await_goto(
            client, 7, PP.AXIS_Z, PP.counts_of(-152000), 2000,
            lambda rid, ms: {'kind': 'reply', 'code': '', 'pairs': {'err': '0'}},
        )
        self.assertEqual(how, 'reply')
        self.assertEqual(rep['pairs']['err'], '0')

    def test_counts_win_when_ack_is_late(self):
        want = PP.counts_of(-152000)
        client = _TeleClient({PP.AXIS_Z: want + 500.0})   # 已在容差内
        t0 = PP.time.monotonic()
        rep, how = PP.await_goto(client, 8, PP.AXIS_Z, want, 5000, lambda rid, ms: None)
        cost = PP.time.monotonic() - t0
        self.assertEqual(how, 'counts')
        self.assertEqual(rep, {})
        self.assertLess(cost, 2.0, '判到位不该等满超时')

    def test_times_out_when_counts_are_far_and_no_ack(self):
        client = _TeleClient({PP.AXIS_Z: 0.0})
        rep, how = PP.await_goto(client, 9, PP.AXIS_Z, PP.counts_of(-152000), 300,
                                 lambda rid, ms: None)
        self.assertIsNone(rep)
        self.assertEqual(how, 'timeout')

    def test_move_abs_continues_and_extends_deadline(self):
        # 回执永不回来 → move_abs 应靠遥测 counts 判到位、继续下一步，而不是报 TIMEOUT
        client = _TeleClient({PP.AXIS_Y: 0.0})
        results = PP.move_abs(client, {PP.AXIS_Y: -13144}, order=[PP.AXIS_Y], rpm=2000,
                              counts={PP.AXIS_Y: 0.0}, wait=lambda rid, ms: None)
        self.assertTrue(results[0]['ok'], results)
        self.assertEqual(results[0].get('arrived_by'), 'counts')
        self.assertTrue(client.extended, '迟到的回执要 extend_deadline，避免被当成「结果未知」')

    def test_arrive_tolerance_is_per_axis(self):
        # Z 容差比 Y 大（Z 行程长、闭环容差本来就宽），但都至少 800 counts
        self.assertGreaterEqual(PP.arrive_tol_counts(PP.AXIS_Z), 800.0)
        self.assertGreaterEqual(PP.arrive_tol_counts(PP.AXIS_Y), 800.0)
        self.assertGreater(PP.arrive_tol_counts(PP.AXIS_Z), PP.arrive_tol_counts(PP.AXIS_Y) / 2)




class _OpenLoopTime:
    """假时钟：只记录 sleep，不真的等。"""

    def __init__(self):
        self.sleeps = []
        self._t = 0.0

    def sleep(self, s):
        self.sleeps.append(float(s))
        self._t += float(s)

    def monotonic(self):
        return self._t

    def perf_counter(self):
        return self._t

    def time(self):
        return self._t


class _OpenLoopClient:
    """假 client：只记 motor_pos / param_set，按需给 E09 一次。"""

    def __init__(self, e09_axes=(), e09_times=1):
        self.sent = []
        self._done = {}
        self._n = 0
        self._e09_left = {int(a): int(e09_times) for a in e09_axes}

    def send(self, cmd, *args, timeout_ms=None):
        self.sent.append((cmd,) + tuple(args))
        self._n += 1
        rid = self._n
        rep = {'kind': 'reply', 'code': '', 'text': '', 'pairs': {'ok': '1'}}
        if cmd == 'motor_pos':
            axis = int(args[0])
            if self._e09_left.get(axis, 0) > 0:
                self._e09_left[axis] -= 1
                rep = {'kind': 'err', 'code': 'E09', 'text': 'E09 goto_busy', 'pairs': {}}
        self._done[rid] = rep
        return rid

    def done_of(self, rid):
        return self._done.get(int(rid))

    def gotos(self):
        return [s for s in self.sent if s[0] in ('goto', 'motor_goto')]

    def motor_pos(self):
        return [s for s in self.sent if s[0] == 'motor_pos']

    def param_sets(self):
        return [s for s in self.sent if s[0] == 'param_set']


class OpenLoopTests(unittest.TestCase):
    """X/Y 横移是全开环：只发不判、不补走、账本靠实发脉冲（2026-10-31 现场定版）。"""

    def _run(self, targets, e09_axes=(), e09_times=1, speed_setup=True):
        cli = _OpenLoopClient(e09_axes=e09_axes, e09_times=e09_times)
        clock = _OpenLoopTime()
        logs = []
        old = PP.time
        PP.time = clock
        try:
            sent = PP.move_xy_fast(cli, targets, rpm={4: 300, 5: 2000},
                                   log=logs.append, speed_setup=speed_setup)
        finally:
            PP.time = old
        return cli, clock, logs, sent

    def test_both_axes_are_written_back_to_back(self):
        """两轴各出一条 motor_pos，且都在任何等待之前（真正的并行）。"""
        cli, clock, logs, sent = self._run({4: 5000, 5: -74277})
        pos = cli.motor_pos()
        self.assertEqual([p[1] for p in pos], [4, 5])
        self.assertEqual(pos[0][2], 0)                 # dir=0 → 正向
        self.assertEqual(pos[0][5], 5000)
        self.assertEqual(pos[1][2], 1)                 # dir=1 → 负向
        self.assertEqual(pos[1][5], 74277)
        self.assertTrue(any('同批发出' in t for t in logs), logs)

    def test_returns_sent_pulses_and_never_uses_goto(self):
        """返回实发脉冲给调用方记账本；全程不发 goto（不再补走）。"""
        cli, clock, logs, sent = self._run({4: 5000, 5: -74277})
        self.assertEqual(sent, {4: 5000, 5: -74277})
        self.assertEqual(cli.gotos(), [])
        self.assertEqual(len(cli.motor_pos()), 2)

    def test_wait_uses_measured_effective_rpm(self):
        """按实测有效转速估时：Y 74277 脉冲 @690 RPM ≈ 2.24 s（指令 2000 RPM 会低估）。"""
        cli, clock, logs, sent = self._run({5: -74277})
        self.assertEqual(len(clock.sleeps), 1, clock.sleeps)
        self.assertGreater(clock.sleeps[0], 1.9)
        self.assertLess(clock.sleeps[0], 2.6)
        self.assertTrue(any('不判读数、不补走' in t for t in logs), logs)

    def test_zero_targets_are_skipped(self):
        cli, clock, logs, sent = self._run({4: 0, 5: 0})
        self.assertEqual(cli.motor_pos(), [])
        self.assertEqual(sent, {})

    def test_e09_is_retried_not_failed(self):
        """E09（上一条还在跑）只重发那一轴，不判失败、不补走。"""
        cli, clock, logs, sent = self._run({5: -74277}, e09_axes=(5,))
        y = [p for p in cli.motor_pos() if p[1] == 5]
        self.assertEqual(len(y), 2, y)
        self.assertEqual(y[1][5], 74277)               # 重发同样的量
        self.assertTrue(any('E09' in t for t in logs), logs)
        self.assertEqual(sent, {5: -74277})

    def test_speed_setup_param_set_is_fire_and_forget(self):
        cli, clock, logs, sent = self._run({5: -1000}, speed_setup=True)
        ps = [p for p in cli.param_sets() if p[1] == 'speed_rpm_max']
        self.assertEqual(len(ps), 1, cli.param_sets())
        self.assertEqual(int(ps[0][2]), 3200)
        cli2, _, _, _ = self._run({5: -1000}, speed_setup=False)
        self.assertEqual([p for p in cli2.param_sets() if p[1] == 'speed_rpm_max'], [])


