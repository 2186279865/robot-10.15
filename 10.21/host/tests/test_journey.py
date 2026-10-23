"""journey（回上电初始位的移动记录）纯逻辑单测：不碰 tkinter / 串口。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import journey as J


class JourneyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="journey_")
        self.path = Path(self.dir) / "journey.json"

    def tearDown(self):
        for item in Path(self.dir).glob("*"):
            try:
                item.unlink()
            except OSError:
                pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    # ---- 基本结构 ----
    def test_00_empty(self):
        route = J.empty("0001")
        self.assertEqual(route["steps"], [])
        self.assertEqual(route["boot"], "0001")
        self.assertIn("还没动过", J.describe(route))
        self.assertTrue(J.validate(route))

    def test_01_append_and_describe(self):
        route = J.empty()
        J.append(route, 4, 0, 60, 1, 3200)
        J.append(route, 5, 1, 60, 1, 1600)
        J.append(route, 3, 0, 30, 1, 533)
        self.assertEqual(len(route["steps"]), 3)
        text = J.describe(route)
        self.assertIn("3 段", text)
        self.assertIn("X +3200", text)
        self.assertIn("Y -1600", text)
        self.assertIn("托盘 +533", text)

    def test_02_reject_bad_params(self):
        route = J.empty()
        for args in ((2, 0, 60, 1, 100),      # 轴不存在
                     (4, 2, 60, 1, 100),      # 方向非法
                     (4, 0, 60, 1, 0),        # 脉冲 0
                     (4, 0, 60, 1, -5),       # 负脉冲
                     (4, 0, 60, 1, J.MAX_PULSES + 1)):
            with self.assertRaises(ValueError):
                J.append(route, *args)
        self.assertEqual(route["steps"], [])

    def test_03_reverse_flips_and_reverses(self):
        route = J.empty()
        J.append(route, 4, 0, 60, 1, 3200)
        J.append(route, 5, 1, 40, 2, 1600)
        steps = J.reverse_steps(route)
        self.assertEqual([s["axis"] for s in steps], [5, 4])
        self.assertEqual([s["dir"] for s in steps], [0, 1])
        self.assertEqual([s["pulses"] for s in steps], [1600, 3200])
        self.assertEqual([s["rpm"] for s in steps], [40, 60])

    def test_04_net_signed(self):
        route = J.empty()
        J.append(route, 4, 0, 60, 1, 3200)
        J.append(route, 4, 1, 60, 1, 1200)
        self.assertEqual(J.net(route)[4], 2000)
        self.assertEqual(J.net(route)[5], 0)

    def test_05_clear(self):
        route = J.empty()
        J.append(route, 4, 0, 60, 1, 100)
        J.clear(route)
        self.assertEqual(J.describe(route)[:2], "0 ")
        self.assertTrue(J.validate(route))

    # ---- 时间估算 ----
    def test_06_step_ms_grows(self):
        fast = {"rpm": 600, "pulses": 3200}
        slow = {"rpm": 60, "pulses": 3200}
        self.assertGreater(J.step_ms(slow), J.step_ms(fast))
        route = J.empty()
        J.append(route, 4, 0, 60, 1, 3200)
        J.append(route, 5, 0, 60, 1, 1600)
        self.assertGreater(J.estimate_ms(route), J.step_ms(route["steps"][0]))

    # ---- 落盘 ----
    def test_07_save_load_roundtrip(self):
        route = J.empty("00AA")
        J.append(route, 4, 0, 60, 1, 3200)
        J.append(route, 3, 1, 30, 1, 533)
        J.save(route, self.path)
        back = J.load(self.path, boot="00AA")
        self.assertEqual(len(back["steps"]), 2)
        self.assertEqual(back["steps"][1]["axis"], 3)

    def test_08_load_missing_or_corrupt(self):
        self.assertEqual(J.load(self.path)["steps"], [])
        self.path.write_text("{not json", encoding="utf8")
        self.assertEqual(J.load(self.path)["steps"], [])

    def test_09_load_boot_mismatch_resets(self):
        route = J.empty("00AA")
        J.append(route, 4, 0, 60, 1, 3200)
        J.save(route, self.path)
        self.assertEqual(J.load(self.path, boot="00AB")["steps"], [])
        self.assertEqual(len(J.load(self.path, boot="00AA")["steps"]), 1)

    def test_10_load_filters_bad_steps(self):
        payload = {
            "version": J.VERSION,
            "steps": [
                {"axis": 4, "dir": 0, "rpm": 60, "acc": 1, "pulses": 100},
                {"axis": 2, "dir": 0, "rpm": 60, "acc": 1, "pulses": 100},
                {"axis": 5, "dir": 0, "rpm": 60, "acc": 1, "pulses": 0},
                {"axis": 1},                            # 缺字段
            ],
        }
        self.path.write_text(json.dumps(payload), encoding="utf8")
        steps = J.load(self.path)["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["axis"], 4)

    def test_11_caps_step_count(self):
        route = J.empty()
        for _ in range(J.MAX_STEPS + 5):
            J.append(route, 4, 0, 60, 1, 10)
        self.assertEqual(len(route["steps"]), J.MAX_STEPS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
