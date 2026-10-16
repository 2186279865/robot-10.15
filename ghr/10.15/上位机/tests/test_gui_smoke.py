"""界面冒烟测试：真的把 tkinter 界面建起来跑一遍模拟后端。

无显示环境（TclError）直接跳过，不算失败。
整套测试共用一个 App 实例，避免反复建/拆窗口带来的 TclError 噪音。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
except Exception:  # pragma: no cover
    tk = None


def _pump(root, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


@unittest.skipIf(tk is None, "没有 tkinter")
class GuiSmokeTest(unittest.TestCase):
    root = None
    app = None
    gui = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except Exception as exc:  # pragma: no cover - 无显示环境
            raise unittest.SkipTest(f"无法创建窗口：{exc}")
        import host_gui

        cls.gui = host_gui

        class Args:
            sim = True
            port = None
            autoconnect = True
            selftest = False

        cls.app = host_gui.App(cls.root, Args())
        _pump(cls.root, 1.2)

    @classmethod
    def tearDownClass(cls):
        if cls.app is not None:
            try:
                cls.app.client.stop_service()
            except Exception:
                pass
        if cls.root is not None:
            cls.root.destroy()

    # -- 连接 ---------------------------------------------------------------
    def test_01_handshake_reaches_online(self):
        self.assertTrue(self.app.client.online, self.app.client.state)
        self.assertTrue(self.app.client.fw_version)
        self.assertEqual(self.app.client.readback_axes, (1,))
        self.assertIn("固件 sim", self.app.fw_label.cget("text"))

    def test_02_speed_command_gets_reply_and_logs_it(self):
        self.app.clear_log()
        self.app.axes[1].send_speed()
        _pump(self.root, 0.5)
        self.assertIn("motor_speed ok", self.app.log_text.get("1.0", "end"))

    def test_03_telemetry_updates_readback_label(self):
        panel = self.app.axes[1]
        panel.tele.set(True)
        panel.toggle_telemetry()
        _pump(self.root, 0.6)
        self.assertIn("脉冲", panel.readback.cget("text"), panel.readback.cget("text"))
        other = self.app.axes[4]
        other.tele.set(True)
        other.toggle_telemetry()
        _pump(self.root, 0.6)
        self.assertIn("未接回读", other.readback.cget("text"))

    def test_04_gripper_buttons_and_status(self):
        self.app.send_gripper("grab")
        _pump(self.root, 0.4)
        self.assertIn("grab", self.app.gripper_status.cget("text"))
        self.assertIn("gripper ok", self.app.log_text.get("1.0", "end"))

    def test_05_sequence_runs_to_done(self):
        self.app.seq_clear()
        self.app.seq_kind.set("motor_speed")
        self.app.seq_args.set("1,0,60,1")
        self.app.seq_wait.set("ms")
        self.app.seq_wait_ms.set(100)
        self.app.seq_add()
        self.app.seq_run()
        _pump(self.root, 0.8)
        self.assertEqual(self.app.runner.state, self.gui.S.DONE, self.app.runner.message)

    def test_06_stop_button_aborts_sequence_and_sends_stop_all(self):
        self.app.seq_clear()
        self.app.seq_kind.set("motor_speed")
        self.app.seq_args.set("1,0,60,1")
        self.app.seq_wait.set("ms")
        self.app.seq_wait_ms.set(1000)
        self.app.seq_add()
        self.app.seq_kind.set("motor_stop")
        self.app.seq_args.set("1")
        self.app.seq_add()
        self.assertEqual(len(self.app.sequence.steps), 2)
        self.app.seq_run()
        _pump(self.root, 0.4)
        self.assertEqual(self.app.runner.state, self.gui.S.WAITING, self.app.runner.message)
        self.app.emergency_stop()
        self.assertEqual(self.app.runner.state, self.gui.S.ABORTED)
        _pump(self.root, 0.4)
        self.assertIn("stop_all ok", self.app.log_text.get("1.0", "end"))

    def test_07_wait_reached_on_axis_without_readback_is_refused(self):
        self.app.seq_clear()
        self.app.seq_kind.set("wait_reached")
        self.app.seq_args.set("4,3000")
        self.app.seq_add()
        self.assertEqual(self.app.sequence.steps, [])
        self.assertIn("没有回读能力", self.app.log_text.get("1.0", "end"))

    def test_08_param_get_fills_entry(self):
        self.app.param_vars["speed_rpm_max"].set("")
        self.app.send_param_get("speed_rpm_max")
        _pump(self.root, 0.4)
        self.assertNotEqual(self.app.param_vars["speed_rpm_max"].get(), "")

    def test_09_gui_param_names_match_protocol_contract(self):
        import protocol as P

        self.assertEqual(
            set(self.gui.PARAM_LABELS),
            set(P.PARAM_DEFAULTS),
            "界面参数表与 protocol.PARAM_DEFAULTS 必须一一对应",
        )
        self.assertEqual(
            [name for name, _ in self.gui.KNOWN_PARAMS], list(P.PARAM_DEFAULTS)
        )


if __name__ == "__main__":
    unittest.main()
