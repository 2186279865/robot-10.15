"""`prep_pose` 单测：配置读写 + 两步顺序 + 失败即停。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import prep_pose as PP


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

    def test_estimate_caps_rpm_at_50(self):
        fast = PP.estimate_timeout_ms(32000, 3000)
        slow = PP.estimate_timeout_ms(32000, 50)
        self.assertEqual(fast, slow)

    def test_goto_budget_reasonable(self):
        self.assertGreater(PP.goto_budget_ms(152000, 1500), 60000)
        self.assertLess(PP.goto_budget_ms(152000, 1500), 120000)


if __name__ == '__main__':
    unittest.main()
