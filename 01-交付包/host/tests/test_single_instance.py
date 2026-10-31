"""单实例锁的单元测试（不建窗口、不弹对话框）。

2026-10-31 现场：同时开了两个窗口，两个都连同一块控制板 ——
  * 两个实例抢 5058 串口桥，来回 `失联 → 重连 → 主动断开`；
  * 关掉其中一个窗口时 `host_gui._on_close` 会 `client.stop_all()`，
    把另一个窗口**正在跑的那件**打成 `E14 reason=stopped`（07:35 一分钟 19 次停止）。
所以 `main.py` 启动时先抢一个本机 127.0.0.1:47319 的监听口，第二个实例直接提示退出。
"""
from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

HOST = Path(__file__).resolve().parent.parent
PKG = HOST.parent
for _p in (str(HOST), str(PKG)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main as M       # noqa: E402 —— 只 import，不调 main()


class SingleInstanceTest(unittest.TestCase):
    """锁真的能拦第二个实例，且释放后还能再抢到。"""

    PORT = 48765           # 测试用临时端口，别碰生产 LOCK_PORT

    def tearDown(self):
        # 兜底：把本用例可能还挂着的 socket 关掉，避免影响其它用例
        for attr in ('_s1', '_s2', '_s3'):
            s = getattr(self, attr, None)
            if s is not None:
                try:
                    s.close()
                except Exception:            # noqa: BLE001
                    pass

    def test_second_instance_is_refused_until_the_first_releases(self):
        self._s1 = M.acquire_single_instance(self.PORT)
        self.assertIsNotNone(self._s1, '第一次抢锁必须成功')
        self._s2 = M.acquire_single_instance(self.PORT)
        self.assertIsNone(self._s2, '第二个实例必须抢不到（否则两个窗口会抢串口）')
        self._s1.close()
        self._s1 = None
        self._s3 = M.acquire_single_instance(self.PORT)
        self.assertIsNotNone(self._s3, '前一个窗口关掉后必须能再抢到')

    def test_lock_port_is_not_a_robot_port(self):
        for busy in (5054, 5056, 5057, 5058, 5059, 5060):
            self.assertNotEqual(M.LOCK_PORT, busy, '锁端口不能和派端服务端口撞')

    def test_multi_instance_override_exists(self):
        src = Path(M.__file__).read_text(encoding='utf-8')
        self.assertIn('--allow-multi', src, '调试要多开时必须留有显式开关')
        self.assertIn('acquire_single_instance', src)
        self.assertIn('warn_already_running', src)

    def test_warning_mentions_what_breaks(self):
        # 只读源码里的提示文案：真调 warn_already_running() 会弹 MessageBox 卡住测试
        src = Path(M.__file__).read_text(encoding='utf-8')
        for word in ('5058', '停止全部'):
            self.assertIn(word, src, '提示里要说明到底会坏什么')


if __name__ == '__main__':
    unittest.main()
