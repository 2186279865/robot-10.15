"""界面冒烟测试：真的把 tkinter 界面建起来跑一遍模拟后端。

无显示环境（TclError）直接跳过，不算失败。
整套测试共用一个 App 实例，避免反复建/拆窗口带来的 TclError 噪音。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import protocol as P  # noqa: E402

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
        # 隔离运行期文件：真实 runtime/ 里可能已经有用户示教/移动记录，
        # 否则「自动先走接近段」会让序列用例一直停在 idle。
        import approach_route as AR
        import journey as J
        cls._tmpdir = tempfile.mkdtemp(prefix="gui_smoke_")
        AR.DEFAULT_PATH = pathlib.Path(cls._tmpdir) / "approach_route.json"
        J.DEFAULT_PATH = pathlib.Path(cls._tmpdir) / "journey.json"
        cls.app.approach = AR.empty()
        cls.app.journey = J.empty()
        cls.app.approach_refresh()
        cls.app.journey_refresh()

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
        self.assertEqual(self.app.client.readback_axes, P.VALID_AXES)
        self.assertIn("固件 sim", self.app.fw_label.cget("text"))

    def test_01b_auto_ready_after_connect(self):
        """hello 变在线后要自动补发一次 gripper ready（预备位 = 当前存盘值）。"""
        log = self.app.log_text.get("1.0", "end")
        self.assertIn("自动请求夹爪回预备位", log)
        self.assertIn(str(self.app._load_gripper_ready()), self.app.gripper_status.cget("text"))
        self.assertIn("ready", self.app.gripper_status.cget("text"))

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

        # 把名单收成只有 Z 轴 → 其它轴必须明确显示"未接回读"，不能让用户以为有实测值
        self.app.client.send("param_set", "readback_mask", 1)
        self.app.client.begin_handshake()
        _pump(self.root, 0.8)
        self.assertEqual(self.app.client.readback_axes, (1,))
        self.assertIn("未接回读", self.app.axes[4].readback.cget("text"))

        # 恢复默认：四轴都接
        self.app.client.send("param_set", "readback_mask", 15)
        self.app.client.begin_handshake()
        _pump(self.root, 0.8)
        self.assertEqual(self.app.client.readback_axes, P.VALID_AXES)
        self.assertNotIn("未接回读", self.app.axes[4].readback.cget("text"))

    def test_04_gripper_buttons_and_status(self):
        self.app.send_gripper("grab")
        _pump(self.root, 0.4)
        self.assertIn("grab", self.app.gripper_status.cget("text"))
        self.assertIn("gripper ok", self.app.log_text.get("1.0", "end"))
        # 三档角度：夹紧 135 / 预备 158 / 松开 180；预备按钮在夹紧与松开之间
        self.assertIn("预备(%d)" % self.app._load_gripper_ready(), self.app.gripper_buttons["ready"].cget("text"))
        _texts = [btn.cget("text") for btn in self.app.gripper_buttons.values()]
        self.assertEqual(_texts.index("预备(%d)" % self.app._load_gripper_ready()), 1)
        self.app.gripper_buttons["ready"].invoke()
        _pump(self.root, 0.4)
        status = self.app.gripper_status.cget("text")
        self.assertIn("ready", status)
        self.assertIn(str(self.app._load_gripper_ready()), status)

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
        # 默认四轴都算接了回读线，这里先声明只接了 Z 轴，才能试出"没有回读能力"这条门
        self.app.client.send("param_set", "readback_mask", 1)
        self.app.client.begin_handshake()
        _pump(self.root, 0.8)
        self.assertEqual(self.app.client.readback_axes, (1,))
        self.app.seq_clear()
        self.app.seq_kind.set("wait_reached")
        self.app.seq_args.set("4,3000")
        self.app.seq_add()
        self.assertEqual(self.app.sequence.steps, [])
        self.assertIn("没有回读能力", self.app.log_text.get("1.0", "end"))

        # 恢复默认（四轴都接），别影响后面的用例
        self.app.client.send("param_set", "readback_mask", 15)
        self.app.client.begin_handshake()
        _pump(self.root, 0.8)
        self.assertEqual(self.app.client.readback_axes, P.VALID_AXES)

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

    # -- 位置模式：手动输入圈数 + 度数 ---------------------------------------
    def test_10_turns_and_degrees_make_pulses(self):
        panel = self.app.axes[1]
        panel.turns.set(2.0)
        panel.degrees.set(0.0)
        self.root.update()
        self.assertEqual(panel.total_pulses(), 6400, "2 圈 = 6400 脉冲")
        self.assertIn("6400 脉冲", panel.pulse_label.cget("text"))

        panel.turns.set(1.0)
        panel.degrees.set(45.0)
        self.root.update()
        self.assertEqual(panel.total_pulses(), 3600, "1 圈 45 度 = 3200 + 400 脉冲")
        self.assertIn("3600 脉冲", panel.target_label.cget("text"))
        self.assertIn("405.0", panel.target_label.cget("text"))

        panel.turns.set(0.0)
        panel.degrees.set(90.0)
        self.root.update()
        self.assertEqual(panel.total_pulses(), 800, "只有 90 度 = 800 脉冲")

        panel.degrees.set(1.0)
        self.root.update()
        self.assertEqual(panel.total_pulses(), 9, "1° = 3200/360 脉冲，四舍五入到 9")

        # 圈和度都是 0：不发指令，只提示
        panel.turns.set(0.0)
        panel.degrees.set(0.0)
        self.app.clear_log()
        panel.send_pos("rel")
        _pump(self.root, 0.2)
        self.assertNotIn("motor_pos", self.app.log_text.get("1.0", "end"), "0 圈 0 度不能下发")

        # 负数（绕过 Spinbox 下限直接塞变量）→ 输入无效、不发指令
        panel.degrees.set(-5.0)
        self.root.update()
        self.assertIsNone(panel.total_pulses())
        self.assertIn("输入无效", panel.target_label.cget("text"))

        panel.turns.set(1.0)
        panel.degrees.set(0.0)
        self.root.update()

    def test_11_absolute_position_requires_reference_then_dispatches(self):
        # 先解除前面用例留下的停止锁存（真机上按过停止后也要这么做）
        self.app.clear_latch()
        _pump(self.root, 0.4)
        self.assertFalse(self.app.client.latched, "stop_clear 之后界面不应再显示锁存")

        panel = self.app.axes[1]
        panel.reset_reference()
        self.assertFalse(panel.reference_valid)
        self.assertIn("未建立", panel.ref_label.cget("text"))

        asked = {"n": 0}
        real = self.gui.messagebox.askyesno
        self.gui.messagebox.askyesno = lambda *a, **k: bool(
            asked.__setitem__("n", asked["n"] + 1) and False
        )
        try:
            self.app.clear_log()
            panel.send_pos("abs")
            _pump(self.root, 0.3)
        finally:
            self.gui.messagebox.askyesno = real
        self.assertEqual(asked["n"], 1, "基准未建立时必须先问用户")
        self.assertNotIn("motor_pos ok", self.app.log_text.get("1.0", "end"))

        panel.mark_zeroed()
        self.assertTrue(panel.reference_valid)
        self.assertIn("已建立", panel.ref_label.cget("text"))

        panel.turns.set(0.5)
        panel.degrees.set(0.0)
        self.app.clear_log()
        panel.send_pos("abs")
        _pump(self.root, 0.5)
        self.assertIn("motor_pos ok", self.app.log_text.get("1.0", "end"))
        self.assertIn("$", self.app.log_text.get("1.0", "end"))

        self.app.clear_log()
        panel.send_pos("rel")
        _pump(self.root, 0.5)
        self.assertIn("motor_pos ok", self.app.log_text.get("1.0", "end"))

    def test_12_hello_resets_position_reference(self):
        panel = self.app.axes[4]
        panel.mark_zeroed()
        self.assertTrue(panel.reference_valid)
        self.app.client.begin_handshake()
        _pump(self.root, 0.6)
        self.assertFalse(panel.reference_valid, "新会话必须把绝对位置基准作废")
        self.assertTrue(self.app.client.online)


if __name__ == "__main__":
    unittest.main()
