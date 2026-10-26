# -*- coding: utf-8 -*-
"""单位换算与文件单位校验（1 圈 = 3200 脉冲 = 65536 counts）。

这组测试的目的：以后任何"两套单位"的回归，都会在这里先炸。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = HERE.parent
sys.path.insert(0, str(HOST))

import units  # noqa: E402
import auto_runner as AR  # noqa: E402
import prep_pose as PP  # noqa: E402


class ConversionTests(unittest.TestCase):
    def test_one_rev_is_3200_pulses_and_65536_counts(self) -> None:
        self.assertEqual(units.PULSES_PER_REV, 3200)
        self.assertEqual(units.COUNTS_PER_REV, 65536)
        self.assertAlmostEqual(units.COUNTS_PER_PULSE, 20.48, places=6)

    def test_counts_to_pulses(self) -> None:
        self.assertAlmostEqual(units.counts_to_pulses(65536), 3200.0, places=6)
        self.assertAlmostEqual(units.counts_to_pulses(-3128960), -152781.25, places=2)

    def test_pulses_to_counts_roundtrip(self) -> None:
        self.assertEqual(units.pulses_to_counts(3200), 65536)
        self.assertEqual(units.pulses_to_counts(-13144), -269189)

    def test_revs(self) -> None:
        self.assertAlmostEqual(units.revs(3200), 1.0, places=9)
        self.assertAlmostEqual(units.revs(-160000), -50.0, places=9)

    def test_fmt_readback_has_both_units(self) -> None:
        text = units.fmt_readback(-3128960, rpm=0, age_ms=100)
        self.assertIn("脉冲", text)
        self.assertIn("counts", text)
        self.assertIn("152781", text)

    def test_fmt_pulses(self) -> None:
        self.assertIn("+3200", units.fmt_pulses(3200))


class UnitFieldTests(unittest.TestCase):
    def test_default_is_pulse(self) -> None:
        self.assertEqual(units.check_unit({}), "pulse")

    def test_explicit_counts(self) -> None:
        self.assertEqual(units.check_unit({"unit": "counts"}), "counts")

    def test_unknown_unit_raises(self) -> None:
        with self.assertRaises(ValueError):
            units.check_unit({"unit": "mm"}, name="测试文件")


class FileUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="units_")

    def test_load_limits_flat_pulse(self) -> None:
        p = Path(self.tmp) / "limits.json"
        p.write_text(json.dumps({"unit": "pulse", "4": {"min": -500, "max": 8878}}), encoding="utf-8")
        lim = AR.load_limits(p)
        self.assertEqual(lim["4"], {"min": -500, "max": 8878})

    def test_load_limits_converts_counts(self) -> None:
        p = Path(self.tmp) / "limits.json"
        # 3200 脉冲 = 65536 counts
        p.write_text(json.dumps({"unit": "counts", "4": {"min": -10240, "max": 65536}}), encoding="utf-8")
        lim = AR.load_limits(p)
        self.assertEqual(lim["4"], {"min": -500, "max": 3200})

    def test_prep_pose_converts_counts(self) -> None:
        p = Path(self.tmp) / "prep_pose.json"
        p.write_text(json.dumps({
            "unit": "counts", "version": 1,
            "y_pulses": -269189, "z_pulses": -3112960,
            "y_rpm": 300, "z_rpm": 1500,
        }), encoding="utf-8")
        cfg = PP.load(p)
        self.assertEqual(cfg["y_pulses"], -13144)
        self.assertEqual(cfg["z_pulses"], -152000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
