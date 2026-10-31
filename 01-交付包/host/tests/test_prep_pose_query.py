# -*- coding: utf-8 -*-
"""B+C 加固的回归用例（2026-10-31 18:12 现场实证）：

* 每段 X/Y 位移的基准必须 **motor_query 现查**（遥测 counts 1.6 s 才刷新一次，
  Y 42,675 脉冲/s → 拿它当基准会错几万脉冲）；
* 等到位用现查（每 0.20 s），到了立刻走，不再空等 1.15 倍余量；
* 残差按**剩余量整对重发**，绝不单轴、绝不重发整段；
* goto 遇 E10/E14 自动 stop_clear 后重试一次；
* 心跳 500 ms、派端看门狗 3000 ms（原来 1 s / 1.5 s 太接近，抖一下就注停车帧）。
"""
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))          # 01-交付包
for p in (os.path.join(PKG, 'host'), os.path.join(PKG, 'vision'), os.path.join(PKG, 'common')):
    if p not in sys.path:
        sys.path.insert(0, p)

import prep_pose as PP                                  # noqa: E402
import host_client as HC                                # noqa: E402
import wire                                             # noqa: E402

AXIS_X = 4
AXIS_Y = PP.AXIS_Y


class _Clock(object):
    """假时钟：sleep 只推进 now，不真睡。"""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def monotonic(self):
        return self.now

    def sleep(self, sec):
        self.now += max(0.0, float(sec))


class _QueryClient(object):
    """按轴维护脉冲位置的假 client：motor_query 回当下 counts，motor_pos 立刻改位置。

    move_factor<1 可模拟"只走了一部分"（用来验证残差整对重发）。
    goto_errors 可让前几条 goto 回 E10/E14（验证自动清锁存重试）。
    """

    def __init__(self, pos=None, move_factor=1.0, goto_errors=()):
        self.pos = {AXIS_X: 0.0, AXIS_Y: float(PP.P1_Y) if hasattr(PP, 'P1_Y') else 0.0}
        self.pos.update({int(k): float(v) for k, v in (pos or {}).items()})
        self.move_factor = float(move_factor)
        self.goto_errors = list(goto_errors)
        self.calls = []
        self._n = 0
        self.last_counts = {}

    # -- 内部 --
    def _rid(self, cmd, args):
        self._n += 1
        self.calls.append((cmd, args, self._n))
        return self._n

    def _find(self, rid):
        for cmd, args, i in self.calls:
            if i == rid:
                return cmd, args
        raise KeyError(rid)

    # -- client 接口 --
    def send(self, cmd, *args, **kw):
        rid = self._rid(cmd, args)
        if cmd == 'motor_pos':
            axis = int(args[0])
            neg = int(args[1]) == 1
            pulses = int(args[4])
            # move_factor 只作用于第一条 motor_pos（模拟"这一轮只走了八成"），
            # 之后的残差步进按实打实走完（真机开环相对位移是精确的）。
            factor = self.move_factor if not getattr(self, '_moved_once', False) else 1.0
            self._moved_once = True
            delta = (-pulses if neg else pulses) * factor
            self.pos[axis] = self.pos.get(axis, 0.0) + delta
        elif cmd == 'stop_clear':
            pass
        return rid

    def wait_reply(self, rid, timeout_ms=8000):
        cmd, args = self._find(rid)
        if cmd == 'motor_query':
            axis = int(args[0])
            counts = self.pos.get(axis, 0.0) * PP.COUNTS_PER_PULSE
            return {'kind': 'reply', 'code': '',
                    'pairs': {'addr': str(axis), 'counts': str(int(round(counts))), 'what': 'pos'}}
        if cmd == 'param_get':
            return {'kind': 'reply', 'code': '', 'pairs': {'value': '3200'}}
        return {'kind': 'reply', 'code': '', 'pairs': {'ok': '1'}}

    def goto(self, axis, pulses, tol=None, rpm=None, timeout_ms=None):
        rid = self._rid('motor_goto', (axis, pulses, tol, rpm))
        self._goto_err = {}
        if self.goto_errors:
            code = self.goto_errors.pop(0)
            if code:
                self._goto_err[rid] = code
            else:
                self.pos[int(axis)] = self.pos.get(int(axis), 0.0) + int(pulses)
        else:
            self.pos[int(axis)] = self.pos.get(int(axis), 0.0) + int(pulses)
        return rid

    def wait_reply_goto(self, rid, timeout_ms=8000):
        return None

    def extend_deadline(self, req_id, extra_ms=30000):
        return True

    def done_of(self, req_id):
        return None

    # exec 时统一走 wait_reply；goto 的 err 由这里补
    def _reply(self, rid, timeout_ms=8000):
        err = getattr(self, '_goto_err', {}).pop(rid, None)
        if err:
            return {'kind': 'err', 'code': err, 'text': err, 'pairs': {'reason': 'stopped'}}
        return _QueryClient.wait_reply(self, rid, timeout_ms)

    def motor_pos(self, axis=None):
        return [(int(a[1]), int(a[4])) for c, a, _i in self.calls
                if c == 'motor_pos' and (axis is None or int(a[0]) == int(axis))]

    def order(self):
        return [c for c, _a, _i in self.calls]


def _patch_time():
    clk = _Clock()
    old = PP.time
    PP.time = clk
    return clk, old


class QueryBaseTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, '_old_time', None) is not None:
            PP.time = self._old_time
            self._old_time = None

    def _run(self, client, targets, rpm=1600):
        clk, old = _patch_time()
        self._old_time = old
        logs = []
        # 让 wait_reply_goto/_reply 走统一路径
        client.wait_reply = client._reply
        try:
            res = PP.move_xy_fast(client, targets, rpm=rpm, log=logs.append,
                                  counts_fn=lambda: dict(client.last_counts))
        finally:
            PP.time = old
        return res, logs, clk

    def test_base_comes_from_motor_query_not_stale_telemetry(self):
        """Y 实际在 -13144（P1），遥测缓存却是 0 → 必须按现查的 -13144 算位移。"""
        client = _QueryClient(pos={AXIS_Y: -13144.0})
        client.last_counts = {AXIS_Y: 0.0}          # 遥测缓存（滞后/错）
        _res, _logs, _clk = self._run(client, {AXIS_Y: -25000})
        sent = client.motor_pos(AXIS_Y)
        self.assertEqual(sent, [(1, 11856)], sent)  # -25000 - (-13144) = -11856

    def test_motor_query_goes_out_before_the_move(self):
        client = _QueryClient(pos={AXIS_Y: -13144.0})
        _res, _logs, _clk = self._run(client, {AXIS_Y: -25000})
        order = client.order()
        first_q = order.index('motor_query')
        first_p = order.index('motor_pos')
        self.assertLess(first_q, first_p, order)

    def test_early_exit_when_already_arrived(self):
        """一发出就到了 → 不该空等到 t_est*1.15（假时钟里看总推进时间）。"""
        client = _QueryClient(pos={AXIS_Y: -13144.0})
        _res, logs, clk = self._run(client, {AXIS_Y: -25000})   # 11856 脉冲 → t_est≈0.28 s
        self.assertTrue(any('现查已到位' in t for t in logs), logs)
        self.assertLess(clk.now - 1000.0, 1.4)

    def test_residual_pair_resent_with_remaining_pulses(self):
        """只走了八成 → 残差按剩余量**再一次并行**下发（不是整段重发、不是单轴 goto）。"""
        client = _QueryClient(pos={AXIS_Y: -13144.0}, move_factor=0.8)
        _res, logs, _clk = self._run(client, {AXIS_Y: -25000})
        sent = client.motor_pos(AXIS_Y)
        self.assertEqual(len(sent), 2, sent)
        self.assertEqual(sent[0], (1, 11856), sent)
        self.assertEqual(sent[1], (1, 2371), sent)     # 剩余 20% 左右
        self.assertNotIn('motor_goto', client.order(), '不许退回单轴 goto')
        self.assertTrue(any('现查残差' in t for t in logs), logs)

    def test_both_axes_written_in_one_batch(self):
        """X/Y 两个 motor_pos 必须紧挨着发（中间不允许插别的运动指令）。"""
        client = _QueryClient(pos={AXIS_X: 0.0, AXIS_Y: -13144.0})
        _res, _logs, _clk = self._run(
            client, {AXIS_X: 6000, AXIS_Y: -25000}, rpm={AXIS_X: 300, AXIS_Y: 2000})
        order = client.order()
        px = [i for i, c in enumerate(order) if c == 'motor_pos']
        self.assertGreaterEqual(len(px), 2, order)
        self.assertEqual(px[1] - px[0], 1, '两轴必须背靠背，中间不能插指令：%s' % (order,))


class E10E14RetryTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, '_old_time', None) is not None:
            PP.time = self._old_time
            self._old_time = None

    def test_goto_retries_after_stop_clear(self):
        """first goto 回 E10 → 自动 stop_clear，按现查位置重算剩余量再试一次。"""
        client = _QueryClient(pos={PP.AXIS_Z: 0.0}, goto_errors=['E10'])
        client.wait_reply = client._reply
        clk, old = _patch_time()
        self._old_time = old
        logs = []
        try:
            res = PP.move_abs(client, {PP.AXIS_Z: -152000}, order=[PP.AXIS_Z],
                              rpm=2400, log=logs.append, wait=client._reply,
                              counts={PP.AXIS_Z: 0.0})
        finally:
            PP.time = old
        self.assertTrue(any(r['ok'] for r in res), res)
        self.assertIn('stop_clear', client.order(), client.order())
        self.assertEqual(len([c for c in client.order() if c == 'motor_goto']), 2,
                         client.order())
        self.assertTrue(any('自动 stop_clear 后重试' in t for t in logs), logs)


class LinkBudgetTests(unittest.TestCase):
    def test_heartbeat_is_faster_than_pi_watchdog(self):
        self.assertEqual(HC.HEARTBEAT_MS, 500)
        self.assertEqual(wire.SERIAL_WATCHDOG_MS, 3000)
        self.assertLess(HC.HEARTBEAT_MS * 2, wire.SERIAL_WATCHDOG_MS)

    def test_effective_rpm_matches_measured(self):
        self.assertEqual(PP.EFFECTIVE_RPM_BY_AXIS[AXIS_Y], 800.0)
        self.assertEqual(PP.EFFECTIVE_RPM_BY_AXIS[PP.AXIS_Z], 1900.0)


if __name__ == '__main__':
    unittest.main()
