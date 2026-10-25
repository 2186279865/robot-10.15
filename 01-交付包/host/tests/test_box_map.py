"""box_map（类别→盒子动态分配）单测：现场无实物时靠它自动占空盒。"""
import json
import unittest
import uuid
from pathlib import Path

import box_map
import vision_grasp as VG

TMP_ROOT = Path(__file__).resolve().parent / '_tmp'


def _tmp_path(name='box_map'):
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TMP_ROOT / ('%s_%s.json' % (name, uuid.uuid4().hex))


class BoxMapTests(unittest.TestCase):
    def setUp(self):
        self._old = box_map.DEFAULT_PATH
        self.tmp = _tmp_path('a')
        box_map.DEFAULT_PATH = self.tmp

    def tearDown(self):
        box_map.DEFAULT_PATH = self._old
        try:
            self.tmp.unlink()
        except OSError:
            pass

    def test_assign_increments(self):
        self.assertEqual(box_map.assign('五棱柱|蓝'), 1)
        self.assertEqual(box_map.assign('球|红'), 2)
        self.assertEqual(box_map.assign('五棱柱|蓝'), 1)     # 幂等
        self.assertEqual(len(box_map.load()), 2)

    def test_next_free_skips_used(self):
        box_map.assign('A', box=3)
        self.assertEqual(box_map.assign('B'), 1)
        self.assertEqual(box_map.assign('C'), 2)

    def test_capacity_limit(self):
        for i in range(6):
            box_map.assign('类%d' % i)
        with self.assertRaises(ValueError):
            box_map.assign('第7类')

    def test_persist_roundtrip(self):
        box_map.assign('五棱柱|蓝', box=4)
        self.assertTrue(self.tmp.exists())
        data = json.loads(self.tmp.read_text(encoding='utf-8'))
        self.assertEqual(data['map']['五棱柱|蓝'], 4)
        self.assertEqual(box_map.lookup('五棱柱|蓝'), 4)

    def test_reset(self):
        box_map.assign('A')
        box_map.reset()
        self.assertEqual(box_map.load(), {})

    def test_describe_empty(self):
        self.assertIn('0 类', box_map.describe())


class VisionGraspBoxTests(unittest.TestCase):
    def setUp(self):
        self._old = box_map.DEFAULT_PATH
        box_map.DEFAULT_PATH = _tmp_path('b')

    def tearDown(self):
        box_map.DEFAULT_PATH = self._old

    def test_ensure_boxes_assigns_once(self):
        targets = [{'class_name': '五棱柱|蓝'}, {'class_name': '球|红'}]
        first = VG.ensure_boxes(targets)
        self.assertEqual(len(first), 2)
        second = VG.ensure_boxes(targets)
        self.assertEqual(second, {})

    def test_manual_mapping_wins(self):
        cfg = VG.load_config()
        cfg['tray']['class_to_box'] = {'五棱柱|蓝': 6}
        self.assertEqual(VG._box_of({'class_name': '五棱柱|蓝'}, cfg), 6)
        # 手动映射的类别不该被自动分配
        assigned = VG.ensure_boxes([{'class_name': '五棱柱|蓝'}], cfg)
        self.assertEqual(assigned, {})

    def test_auto_assign_off(self):
        cfg = VG.load_config()
        cfg['tray']['auto_assign'] = 0
        assigned = VG.ensure_boxes([{'class_name': '球|红'}], cfg)
        self.assertEqual(assigned, {})
        # 关掉后按老逻辑（class_id）兜底，默认盒 1
        self.assertEqual(VG._box_of({'class_name': '球|红'}, cfg), 1)

    def test_sort_groups_by_box(self):
        targets = [
            {'class_name': 'B|蓝', 'center_tray': [0.5, 0.5]},
            {'class_name': 'A|红', 'center_tray': [0.4, 0.2]},
        ]
        cfg = VG.load_config()
        ordered = VG.ordered_targets(targets, cfg)
        # 先分配到的类别在盒 1，应该排在前面
        self.assertEqual(ordered[0]['class_name'], 'B|蓝')


if __name__ == '__main__':
    unittest.main()
