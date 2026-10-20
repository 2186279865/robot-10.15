"""视觉抓取链路离线测试（不碰串口、不碰相机、不弹窗）。

跑法（在 ghr/10.15/上位机 目录）：
    python -m unittest discover -s tests -v

覆盖：
* new_eye/tray_calib.py 的仿射反解与来回换算；
* vision_grasp.build_sequence 的步骤结构（先抬 Z 再走 X/Y、按类别转盘、只走最短角度）；
* vision_import.read_targets 对过期/格式/授权字段的拒绝逻辑。
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

import sequence as S  # noqa: E402
import tray_calib as TRAY  # noqa: E402
import vision_grasp as VG  # noqa: E402
import vision_import as VI  # noqa: E402


def affine_truth():
    return {'a': 1600.0, 'b': 210.0, 'c': -180.0, 'd': 1480.0, 'tx': 3200.0, 'ty': -900.0}


class TestTrayCalib(unittest.TestCase):
    def test_solve_roundtrip(self):
        truth = affine_truth()
        points = []
        for u, v in ((0.2, 0.2), (0.8, 0.25), (0.3, 0.75), (0.75, 0.7)):
            px, py = TRAY.uv_to_pulse(u, v, truth)
            points.append({'u': u, 'v': v, 'px': px, 'py': py})
        got = TRAY.solve(points)
        for key, value in truth.items():
            self.assertAlmostEqual(got[key], value, places=6)
        # 用反解结果再换算一次，误差应是浮点噪声
        for point in points:
            px, py = TRAY.uv_to_pulse(point['u'], point['v'], {'affine': got})
            self.assertAlmostEqual(px, point['px'], places=6)
            self.assertAlmostEqual(py, point['py'], places=6)

    def test_reject_collinear(self):
        points = [{'u': 0.1, 'v': 0.1, 'px': 0, 'py': 0},
                  {'u': 0.2, 'v': 0.2, 'px': 100, 'py': 100},
                  {'u': 0.3, 'v': 0.3, 'px': 200, 'py': 200}]
        with self.assertRaises(TRAY.CalibError):
            TRAY.solve(points)


class TestBuildSequence(unittest.TestCase):
    def setUp(self):
        self.calib = {'affine': affine_truth()}
        self.cfg = VG._merge(VG.DEFAULTS, {
            'z': {'safe': 1200, 'lift': 900, 'place': -100, 'grasp': {'圆柱/圆形': -3200}},
            'drop': {'x': 0, 'y': 0},
            'tray': {'class_to_box': {'圆柱/圆形': 1, '五棱体': 2}, 'start_box': 1},
        })
        self.targets = [
            {'id': 1, 'class_id': 1, 'class_name': '圆柱/圆形', 'center_tray': [0.30, 0.40]},
            {'id': 2, 'class_id': 2, 'class_name': '五棱体', 'center_tray': [0.62, 0.21]},
            {'id': 3, 'class_id': 1, 'class_name': '圆柱/圆形', 'center_tray': [0.35, 0.75]},
        ]

    def test_structure_and_order(self):
        seq, warnings, meta = VG.build_sequence(self.targets, self.calib, self.cfg)
        self.assertIsInstance(seq, S.Sequence)
        self.assertEqual(meta['start_box'], 1)
        self.assertEqual(meta['final_box'], 2)
        kinds = [step.kind for step in seq.steps]
        # 每个目标：安全高度 -> X -> Y -> 下降 -> 夹紧 -> 抬起 -> 回放料点 -> (转盘) -> 放料
        self.assertGreaterEqual(kinds.count('gripper'), len(self.targets) * 2)
        notes = [str(step.args[0]) for step in seq.steps if step.kind == 'note']
        # 同盒目标应该连着做：两个圆柱（盒1）在五棱体（盒2）之前；盒1内按 v 排序
        order = [n for n in notes if n.startswith('#')]
        self.assertEqual(len(order), 3)
        self.assertIn('目标 1', order[0])
        self.assertIn('目标 3', order[1])
        self.assertIn('目标 2', order[2])
        # 每步都能通过校验（没有任何 wait_reached，所以不需要回读轴）
        problems = [p for step in seq.steps for p in step.validate(())]
        self.assertEqual(problems, [])
        # 未示教的类别给 warning：圆柱有 grasp，五棱体没有
        self.assertTrue(any('五棱体' in w for w in warnings))

    def test_tray_shortest_path(self):
        cfg = VG._merge(self.cfg, {'tray': {'class_to_box': {'圆柱/圆形': 6, '五棱体': 1},
                                            'start_box': 1}})
        seq, _, meta = VG.build_sequence(self.targets[:1] + [self.targets[1]], self.calib, cfg)
        rel = [step.args for step in seq.steps
               if step.kind == 'motor_pos' and step.args[0] == VG.AXIS_TRAY]
        # 排序后先做盒1（五棱体，不转），再从盒1 -> 盒6：最短 -60°
        self.assertEqual(len(rel), 1)
        pulses, direction = int(rel[0][4]), int(rel[0][1])
        self.assertAlmostEqual(abs(pulses), 3200 / 6, delta=1)
        self.assertEqual(direction, 1)  # 负角度 -> 反向

    def test_reject_empty(self):
        with self.assertRaises(ValueError):
            VG.build_sequence([], self.calib, self.cfg)


class TestReadTargets(unittest.TestCase):
    def _write(self, payload):
        # 写到仓库根目录：Windows 沙箱下 ghr/ 子目录对脚本进程只读，
        # 系统 temp 目录 chmod 又会报 PermissionError，根目录两边都能写。
        path = ROOT / '_tmp_vision_targets.json'
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def payload(self, **extra):
        data = {
            'schema_version': 5,
            'ts': time.time(),
            'motion_authorized': False,
            'targets': [{'id': 1, 'class_id': 1, 'class_name': '圆柱/圆形',
                         'center_tray': [0.4, 0.5], 'views': 'top+side'}],
        }
        data.update(extra)
        return data

    def test_accepts_fresh_schema5(self):
        data, age = VI.read_targets(self._write(self.payload()))
        self.assertLess(age, 5)
        self.assertEqual(data['targets'][0]['center_tray'], [0.4, 0.5])

    def test_rejects_stale(self):
        with self.assertRaises(ValueError) as ctx:
            VI.read_targets(self._write(self.payload(ts=time.time() - 60)))
        self.assertIn('过期', str(ctx.exception))

    def test_rejects_authorized_and_old_schema(self):
        with self.assertRaises(ValueError):
            VI.read_targets(self._write(self.payload(motion_authorized=True)))
        with self.assertRaises(ValueError):
            VI.read_targets(self._write(self.payload(schema_version=3)))

    def test_rejects_bad_uv(self):
        with self.assertRaises(ValueError):
            VI.read_targets(self._write(self.payload(
                targets=[{'id': 1, 'center_tray': [1.2, 0.5]}])))
        with self.assertRaises(ValueError):
            VI.read_targets(self._write(self.payload(
                targets=[{'id': 1, 'center_tray': None}])))


if __name__ == '__main__':
    unittest.main(verbosity=2)
