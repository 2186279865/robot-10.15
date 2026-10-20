"""标定向导的离线测试（不碰相机、不碰串口、不弹窗）。

覆盖：
* calib_wizard.measure_points + solve：三组点能反解出已知仿射，残差 ~0；
* calib_wizard.read_target：新鲜/过期/带 prefer 编号/无目标/格式不符；
* vision_grasp 的转盘状态文件与 start_box 覆盖。
"""
import json
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_DIR = HERE.parent
ROOT = HOST_DIR.parents[2]

for path in (str(HOST_DIR), str(ROOT / 'new_eye')):
    if path not in sys.path:
        sys.path.insert(0, path)

import calib_wizard as W  # noqa: E402
import tray_calib as TRAY  # noqa: E402
import vision_grasp as VG  # noqa: E402

TRUTH = {'a': 1600.0, 'b': 210.0, 'c': -180.0, 'd': 1480.0, 'tx': 3200.0, 'ty': -900.0}
PULSES = 3000


def tmp_path(name):
    # ghr/ 子目录对脚本进程只读，写仓库根目录；用完删掉
    return ROOT / name


class TestMeasureAndSolve(unittest.TestCase):
    def test_three_points_recover_affine(self):
        # 用已知仿射反推：X/Y 各走 PULSES 脉冲后，物块在画面里应该出现在哪
        base = (0.50, 0.50)
        a, b, c, d = (TRUTH[key] for key in 'abcd')
        det = a * d - b * c
        du_x, dv_x = d * PULSES / det, -c * PULSES / det
        du_y, dv_y = -b * PULSES / det, a * PULSES / det
        x_uv = (base[0] + du_x, base[1] + dv_x)
        y_uv = (base[0] + du_y, base[1] + dv_y)
        points = W.measure_points(base, x_uv, y_uv, PULSES)
        result = W.solve(points, write=False)
        self.assertLess(result['max_abs_pulse']['dx'], 2.0)
        self.assertLess(result['max_abs_pulse']['dy'], 2.0)
        # 方向：X 正脉冲让 u 变大（du>0），Y 正脉冲让 v 变大
        self.assertGreater(result['per_pulse']['x']['du'], 0)
        self.assertGreater(result['per_pulse']['y']['dv'], 0)
        self.assertIn('残差', W.summary(result))

    def test_reject_bad_points(self):
        with self.assertRaises(ValueError):
            W.measure_points((0.5, 0.5), (0.6, 0.5), (0.5, 0.6), 0)
        # 三个点重合：视觉没动，check_points 要在解方程之前拦住
        points = W.measure_points((0.5, 0.5), (0.5, 0.5), (0.5, 0.5), PULSES)
        problems = W.check_points(points)
        self.assertEqual(len(problems), 2)
        self.assertIn('X 走完后视觉几乎没动', problems[0])
        with self.assertRaises(ValueError) as ctx:
            W.solve(points, write=False)
        self.assertIn('不合格', str(ctx.exception))

    def test_report_mentions_direction_and_residual(self):
        base = (0.50, 0.50)
        a, b, c, d = (TRUTH[key] for key in 'abcd')
        det = a * d - b * c
        x_uv = (base[0] + d * PULSES / det, base[1] - c * PULSES / det)
        y_uv = (base[0] - b * PULSES / det, base[1] + a * PULSES / det)
        result = W.solve(W.measure_points(base, x_uv, y_uv, PULSES), write=False)
        text = W.report(result, PULSES)
        self.assertIn('X 轴正脉冲', text)
        self.assertIn('走 3000 脉冲', text)
        self.assertIn('✓', text)
        self.assertIn('残差', text)


class TestReadTarget(unittest.TestCase):
    def _write(self, payload, name='_tmp_wizard_targets.json'):
        path = tmp_path(name)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def payload(self, targets, **extra):
        data = {'schema_version': 5, 'ts': time.time(), 'motion_authorized': False,
                'targets': targets}
        data.update(extra)
        return data

    def test_pick_nearest_to_center(self):
        path = self._write(self.payload([
            {'id': 1, 'class_name': '圆柱/圆形', 'center_tray': [0.05, 0.05]},
            {'id': 2, 'class_name': '五棱体', 'center_tray': [0.52, 0.48]},
        ]))
        u, v, label = W.read_target(path)
        self.assertEqual((round(u, 2), round(v, 2)), (0.52, 0.48))
        self.assertEqual(label, '#2 五棱体')

    def test_prefer_id(self):
        path = self._write(self.payload([
            {'id': 1, 'class_name': 'a', 'center_tray': [0.05, 0.05]},
            {'id': 2, 'class_name': 'b', 'center_tray': [0.52, 0.48]},
        ]))
        u, _, label = W.read_target(path, prefer='1')
        self.assertAlmostEqual(u, 0.05)
        self.assertEqual(label, '#1 a')
        with self.assertRaises(ValueError):
            W.read_target(path, prefer='9')

    def test_reject_stale_and_empty(self):
        stale = self._write(self.payload([{'id': 1, 'center_tray': [0.5, 0.5]}],
                                         ts=time.time() - 120))
        with self.assertRaises(ValueError) as ctx:
            W.read_target(stale)
        self.assertIn('过期', str(ctx.exception))
        empty = self._write(self.payload([]))
        with self.assertRaises(ValueError):
            W.read_target(empty)
        bad = self._write({'schema_version': 5, 'ts': time.time(),
                           'motion_authorized': True, 'targets': []})
        with self.assertRaises(ValueError):
            W.read_target(bad)


class TestHandPoints(unittest.TestCase):
    """手推模式：用编码器回读折算脉冲坐标。"""

    def test_counts_to_pulses_scale(self):
        # 1 圈 = 65536 counts = 3200 脉冲
        self.assertAlmostEqual(W.counts_to_pulses(65536), 3200.0)
        self.assertAlmostEqual(W.counts_to_pulses(655360), 32000.0)
        self.assertAlmostEqual(W.counts_to_pulses(-65536), -3200.0)

    def test_wrap_delta_shortest_path(self):
        # 65530 -> 10 是 +16 counts（跨零点），不是 -65520
        self.assertAlmostEqual(W.wrap_delta(65530, 10), 16.0)
        self.assertAlmostEqual(W.wrap_delta(10, 65530), -16.0)
        self.assertAlmostEqual(W.wrap_delta(1000, 1600), 600.0)

    def test_hand_points_recover_affine(self):
        # 用真值仿射造采样：给定脉冲位移 -> u/v，再把脉冲换成编码器计数
        a, b, c, d = (TRUTH[key] for key in 'abcd')
        det = a * d - b * c
        moves = [(0.0, 0.0), (900.0, 0.0), (900.0, 700.0), (200.0, 700.0)]
        samples = []
        base_uv = (0.50, 0.50)
        for px, py in moves:
            du = (d * px - b * py) / det
            dv = (-c * px + a * py) / det
            samples.append({
                'u': base_uv[0] + du, 'v': base_uv[1] + dv,
                'counts_x': int(round(px * W.COUNTS_PER_REV / W.PULSES_PER_REV)) % 65536,
                'counts_y': int(round(py * W.COUNTS_PER_REV / W.PULSES_PER_REV)) % 65536,
            })
        points = W.hand_points(samples)
        self.assertEqual(len(points), 4)
        self.assertAlmostEqual(points[1]['px'], 900.0, delta=0.1)
        self.assertAlmostEqual(points[2]['py'], 700.0, delta=0.1)
        result = W.solve(points, write=False)
        self.assertLess(result['max_abs_pulse']['dx'], 2.0)
        self.assertLess(result['max_abs_pulse']['dy'], 2.0)
        self.assertGreater(result['per_pulse']['x']['du'], 0)
        self.assertGreater(result['per_pulse']['y']['dv'], 0)

    def test_hand_points_negmoves_and_wrap(self):
        # 从接近满圈的位置往回走：回读会跨零点，仍要求能算出负位移
        samples = [
            {'u': 0.5, 'v': 0.5, 'counts_x': 65000, 'counts_y': 100},
            {'u': 0.4, 'v': 0.5, 'counts_x': 45000, 'counts_y': 100},   # -500 脉冲
            {'u': 0.5, 'v': 0.6, 'counts_x': 65000, 'counts_y': 5000},  # 回到 0；Y +153 脉冲
        ]
        points = W.hand_points(samples)
        self.assertAlmostEqual(points[1]['px'], -3200.0 * 20000 / 65536, delta=0.1)
        self.assertAlmostEqual(points[2]['px'], 0.0, delta=0.1)
        self.assertGreater(points[2]['py'], 0.0)

    def test_hand_points_rejects_half_turn_ambiguity(self):
        samples = [
            {'u': 0.5, 'v': 0.5, 'counts_x': 0, 'counts_y': 0},
            # 实际推了 33000 counts（>半圈）：单圈编码器会读成 -32536，方向不可信
            {'u': 0.4, 'v': 0.5, 'counts_x': 33000, 'counts_y': 0},
            {'u': 0.5, 'v': 0.6, 'counts_x': 0, 'counts_y': 3000},
        ]
        with self.assertRaises(ValueError) as ctx:
            W.hand_points(samples)
        self.assertIn('半圈', str(ctx.exception))

    def test_hand_points_needs_three(self):
        with self.assertRaises(ValueError):
            W.hand_points([{'u': 0.5, 'v': 0.5, 'counts_x': 0, 'counts_y': 0}])


class TestPulsePoints(unittest.TestCase):
    """电机带动模式：指令脉冲已知，直接组标定点（不需要编码器回读）。"""

    def test_pulse_points_recover_affine(self):
        a, b, c, d = (TRUTH[key] for key in 'abcd')
        det = a * d - b * c
        moves = [(0.0, 0.0), (3000.0, 0.0), (0.0, 3000.0)]
        samples = []
        for px, py in moves:
            du = (d * px - b * py) / det
            dv = (-c * px + a * py) / det
            samples.append({'u': 0.5 + du, 'v': 0.5 + dv, 'px': px, 'py': py})
        result = W.solve_pulse(samples, write=False)
        self.assertLess(result['max_abs_pulse']['dx'], 1.0)
        self.assertLess(result['max_abs_pulse']['dy'], 1.0)
        self.assertGreater(result['per_pulse']['x']['du'], 0)
        self.assertGreater(result['per_pulse']['y']['dv'], 0)

    def test_pulse_points_rejects_bad_sample(self):
        bad = [{'u': 0.5, 'v': 0.5, 'px': 0.0, 'py': 0.0},
               {'u': 0.5, 'v': 0.5, 'px': 0.0},
               {'u': 0.5, 'v': 0.5, 'px': 0.0, 'py': 0.0}]
        with self.assertRaises(ValueError) as ctx:
            W.pulse_points(bad)
        self.assertIn('py', str(ctx.exception))

    def test_vision_ts_reads_file(self):
        path = tmp_path('_tmp_vision_ts.json')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.assertEqual(W.vision_ts(path), 0.0)
        path.write_text(json.dumps({'ts': 1700000000.5}), encoding='utf-8')
        self.assertAlmostEqual(W.vision_ts(path), 1700000000.5)
        path.write_text('{oops', encoding='utf-8')
        self.assertEqual(W.vision_ts(path), 0.0)


class TestOrthoSolve(unittest.TestCase):
    """简化求解：只假设 X→u、Y→v，不需要点形成三角形。"""

    def _ortho_points(self, x_dv=0.0, y_du=0.0):
        # 真值：X 走 3000 脉冲 → u +0.12；Y 走 3300 脉冲 → v +0.15
        base = {'u': 0.40, 'v': 0.40, 'px': 0.0, 'py': 0.0}
        xp = {'u': 0.52, 'v': 0.40 + x_dv, 'px': 3000.0, 'py': 0.0}
        yp = {'u': 0.40 + y_du, 'v': 0.55, 'px': 0.0, 'py': 3300.0}
        return [base, xp, yp]

    def test_recovers_scales(self):
        result = W.solve_ortho(self._ortho_points(), write=False)
        affine = result['affine']
        self.assertEqual(result['mode'], 'ortho')
        self.assertAlmostEqual(affine['a'], 3000 / 0.12, places=3)
        self.assertAlmostEqual(affine['d'], 3300 / 0.15, places=3)
        self.assertEqual((affine['b'], affine['c']), (0.0, 0.0))
        self.assertLess(result['max_abs_pulse']['dx'], 1.0)
        self.assertLess(result['max_abs_pulse']['dy'], 1.0)
        self.assertLess(abs(result['per_pulse']['x']['du'] - 0.12 / 3000), 1e-9)

    def test_rejects_single_axis(self):
        points = [{'u': 0.4, 'v': 0.4, 'px': 0.0, 'py': 0.0},
                  {'u': 0.5, 'v': 0.4, 'px': 3000.0, 'py': 0.0},
                  {'u': 0.6, 'v': 0.4, 'px': 6000.0, 'py': 0.0}]
        with self.assertRaises(ValueError) as ctx:
            W.solve_ortho(points, write=False)
        self.assertIn('Y 脉冲', str(ctx.exception))

    def test_rejects_no_uv_change(self):
        points = [{'u': 0.4, 'v': 0.4, 'px': 0.0, 'py': 0.0},
                  {'u': 0.4, 'v': 0.4, 'px': 3000.0, 'py': 0.0},
                  {'u': 0.4, 'v': 0.4, 'px': 0.0, 'py': 3000.0}]
        with self.assertRaises(ValueError):
            W.solve_ortho(points, write=False)

    def test_report_flags_coupling(self):
        ok = W.solve_ortho(self._ortho_points(), write=False)
        self.assertIn('✓', W.report(ok, 3000))
        bad = W.solve_ortho(self._ortho_points(x_dv=0.06), write=False)
        self.assertIn('⚠', W.report(bad, 3000))

    def test_writes_ortho_affine(self):
        path = tmp_path('_tmp_ortho.json')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        result = W.solve_ortho(self._ortho_points(), path=path, write=True)
        self.assertEqual(Path(result['path']), path)
        data = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(data['affine']['b'], 0.0)
        self.assertEqual(data['affine']['c'], 0.0)
        self.assertEqual(len(data['points']), 3)


class TestTrayState(unittest.TestCase):
    def test_state_roundtrip(self):
        path = tmp_path('_tmp_tray_state.json')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.assertEqual(VG.load_state(path), {'current_box': 1})
        VG.save_state(4, path)
        self.assertEqual(VG.load_state(path), {'current_box': 4})
        path.write_text('{bad json', encoding='utf-8')
        self.assertEqual(VG.load_state(path), {'current_box': 1})

    def test_start_box_override_and_meta(self):
        cfg = VG._merge(VG.DEFAULTS, {
            'z': {'safe': 1200, 'lift': 900, 'place': -100, 'grasp': {'圆柱/圆形': -3200}},
            'tray': {'class_to_box': {}, 'start_box': 1},
        })
        calib = {'affine': TRUTH}
        targets = [
            {'id': 1, 'class_id': 1, 'class_name': '圆柱/圆形', 'center_tray': [0.3, 0.4]},
            {'id': 2, 'class_id': 2, 'class_name': '五棱体', 'center_tray': [0.6, 0.2]},
        ]
        seq, _, meta = VG.build_sequence(targets, calib, cfg, start_box=6)
        self.assertEqual(meta['start_box'], 6)
        self.assertEqual(meta['final_box'], 2)
        self.assertEqual(VG.final_box(targets, cfg), 2)
        rel = [step.args for step in seq.steps
               if step.kind == 'motor_pos' and step.args[0] == VG.AXIS_TRAY]
        # 盒 6 -> 盒 1 是 60°（最短 -60°），盒 1 -> 盒 2 是 60°；两段都是 533 脉冲
        self.assertEqual(len(rel), 2)
        for args in rel:
            self.assertAlmostEqual(int(args[4]), 3200 / 6, delta=1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
