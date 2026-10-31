# -*- coding: utf-8 -*-
"""派端屏「每次一键启动刷新整页」的用例（用户 2026-10-31 明确要求）。

以前开批只推一条 state，屏上会留着上一批的盒卡片和计时，看着像没启动过。
现在开批先推一条 `kind=reset`：6 个盒卡片清空、计数/计时归零、回「等待第一件」。
"""
import pathlib
import sys
import unittest

HOST = pathlib.Path(__file__).resolve().parent.parent
PKG = HOST.parent
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

import auto_grab as AG                      # noqa: E402
import display_link                         # noqa: E402


class DisplayResetMessageTest(unittest.TestCase):
    def test_reset_message_shape(self):
        link = display_link.DisplayLink('192.168.100.198')   # 不 start()：只塞队列，不连网
        link.reset(total=12, last_event='开始连续抓 ∞ 件')
        msg = link._q.get_nowait()
        self.assertEqual(msg['kind'], 'reset')
        self.assertEqual(msg['total'], 12)
        self.assertEqual(msg['last_event'], '开始连续抓 ∞ 件')

    def test_reset_without_args(self):
        link = display_link.DisplayLink('192.168.100.198')
        link.reset()
        msg = link._q.get_nowait()
        self.assertEqual(msg, {'kind': 'reset'})


class PiDisplayResetHandlerTest(unittest.TestCase):
    def test_pi_display_clears_boxes_on_reset(self):
        src = (PKG / 'orangepi' / 'pi' / 'pi_display.py').read_text(encoding='utf-8')
        self.assertIn("kind == 'reset'", src)
        # 必须真的把 6 个盒卡片清空，而不是只改状态文字
        self.assertIn("self.boxes[_i] = {'label': '', 'color': '', 'items': []}", src)
        self.assertIn('self.done = 0', src)


class RunManyResetsDisplayTest(unittest.TestCase):
    """开批第一件事就是刷屏（哪怕这一批一件都没抓也会先刷）。"""

    class _Display:
        def __init__(self):
            self.calls = []

        def reset(self, **kw):
            self.calls.append(('reset', kw))

        def state(self, **kw):
            self.calls.append(('state', kw))

    def test_reset_is_the_first_display_call(self):
        sys.path.insert(0, str(HOST / 'tests'))
        from test_auto_grab import FakeClient      # noqa: PLC0415

        disp = self._Display()
        orig = AG.load_targets
        AG.load_targets = lambda *a, **k: []
        logs = []
        try:
            res = AG.run_many(FakeClient(), 1, lambda: None, log=logs.append,
                              wait=lambda rid, ms: None,
                              pos_source=lambda: ({}, {}, {4: 0.0,
                                                         5: float(AG.P1_Y) * 20.48}),
                              drop=False,
                              display=disp, warp_path=None)
        finally:
            AG.load_targets = orig
        self.assertEqual(res['done'], 0)
        self.assertTrue(disp.calls, '开批应该推过消息')
        self.assertEqual(disp.calls[0][0], 'reset', disp.calls)
        self.assertTrue(any('派端屏已刷新' in t for t in logs), logs)


class FixedLoopSourceTest(unittest.TestCase):
    """现场固定时序的硬要求，用源码断言守住（改回去会红）。"""

    def _src(self, name):
        return (HOST / name).read_text(encoding='utf-8')

    def test_one_click_passes_recognize_each(self):
        """每件都要重新拍照识别（用户 m04727：松爪后立刻拍照）。"""
        src = self._src('calib_tab.py')
        self.assertIn('recognize_each=True', src)

    def test_prefetch_walks_y_then_z(self):
        src = self._src('auto_grab.py')
        y = src.index("'①Y 进通道'")
        z = src.index("'②Z 抬顶'")
        self.assertLess(y, z, '预走必须先把 Y 走进通道，再抬 Z')

    def test_grab_settle_is_200ms(self):
        src = self._src('auto_grab.py')
        self.assertIn('SETTLE_GRAB_MS = 200', src)

    def test_tray_join_is_before_dropping(self):
        """转盘必须在「降 Z → 松爪」之前硬等到位，而不是只 WARN 就松爪。"""
        src = self._src('auto_grab.py')
        join = src.index('tray_job.join(timeout=TRAY_JOIN_TIMEOUT_S)')
        open_ = src.index("client.send('gripper', 'open'")
        self.assertLess(join, open_, '转盘等到位必须发生在松爪之前')


if __name__ == '__main__':
    unittest.main()
