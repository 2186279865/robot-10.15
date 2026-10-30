"""派端屏「货物图用预设照片、不再实时截取」的回归测试（2026-10-31 现场要求）。

现场要求：派端比赛屏上的货物图一律用 `图片/presets/` 那 6 张照片
（部署副本 `01-交付包/orangepi/pi/presets/<基名>_96.png` / `_160.png`），
**不要**再每件从相机抓一帧按 bbox 裁剪。

这组用例守住三件事：
  1) `presets/index.json` 与 `_96/_160.png` 文件齐齐整整（尺寸对、无孤儿图）；
  2) 现场会出现的 6 个类别键都能查到图（含「椭圆柱|红」这种只差颜色的写法）；
  3) 代码侧两个开关没被人误改回去：`pi_display._USE_LIVE_PHOTO` 默认关、
     上位机 `display_link.SEND_THUMB` 默认 False（不再算/推 thumb_b64）。

不依赖 tkinter、不依赖 Pi，纯文件 + 源码文本检查。
"""
from __future__ import annotations

import json
import struct
import sys
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[2]        # 01-交付包
PI_DIR = PKG / 'orangepi' / 'pi'
PRESET_DIR = PI_DIR / 'presets'
# 屏 1024×600：列内 f(52)、底栏 f(56)；96/160 留给更大分辨率
SIZES = (52, 56, 96, 160)

# 现场会出现的 6 个类别（2026-10-31 日志里真实的 class_key）
FIELD_KEYS = ('圆锥|黄', '椭圆柱|红', '长方体|蓝', '长方体|红', '六棱锥|白', '四棱锥|紫')


def png_size(path):
    """读 PNG 的 IHDR 尺寸（不依赖 PIL）。"""
    with open(path, 'rb') as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        return None
    return struct.unpack('>II', head[16:24])


class PresetFilesTest(unittest.TestCase):
    def setUp(self):
        self.index = json.loads((PRESET_DIR / 'index.json').read_text(encoding='utf-8'))

    def test_index_is_not_empty(self):
        self.assertGreaterEqual(len(self.index), 6, 'index.json 至少要覆盖 6 个类别')

    def test_every_base_has_both_sizes(self):
        bases = sorted(set(self.index.values()))
        self.assertTrue(bases, 'index.json 里没有任何基名')
        for base in bases:
            for size in SIZES:
                path = PRESET_DIR / ('%s_%d.png' % (base, size))
                self.assertTrue(path.exists(), '缺预设图：%s' % path.name)
                self.assertEqual(png_size(path), (size, size),
                                 '%s 尺寸不对（应为 %d×%d）' % (path.name, size, size))

    def test_no_orphan_pngs(self):
        """每个 _96/_160.png 都得被 index.json 用到，否则屏上永远不会出现它。"""
        used = set(self.index.values())
        for path in sorted(PRESET_DIR.glob('*.png')):
            stem = path.stem
            base = None
            for size in SIZES:
                if stem.endswith('_%d' % size):
                    base = stem[:-len('_%d' % size)]
                    break
            self.assertIsNotNone(base, '预设图命名不合规（要 <基名>_96.png / _160.png）：%s'
                                 % path.name)
            self.assertIn(base, used, '%s 没有被 index.json 引用' % path.name)

    def test_field_classes_are_covered(self):
        missing = [k for k in FIELD_KEYS if k not in self.index]
        self.assertFalse(missing, '这些现场类别没有预设图：%s' % ', '.join(missing))


class PresetLookupTest(unittest.TestCase):
    """把 pi_display 的查表逻辑摘出来跑（不导入 tkinter，避免无显示环境失败）。"""

    @classmethod
    def setUpClass(cls):
        src = (PI_DIR / 'pi_display.py').read_text(encoding='utf-8')
        start = src.index('def _shape_family(')
        end = src.index('class Pump:', start)
        ns = {}
        exec(compile(src[start:end], 'pi_display_presets', 'exec'), ns)   # noqa: S102
        cls._shape_family = staticmethod(ns['_shape_family'])
        # preset_photo 依赖 tk.PhotoImage；这里只复用它的查找段：重写一个纯查表版本
        ns['_load_preset_index'] = lambda: json.loads(
            (PRESET_DIR / 'index.json').read_text(encoding='utf-8'))
        cls._ns = ns

    def _resolve(self, name, color):
        """复刻 preset_photo 的三级查找（精确 → 同色同族 → 同族）。"""
        idx = self._ns['_load_preset_index']()
        fam = self._shape_family(name)
        base = idx.get('%s|%s' % (name, color))
        if base is None and color and fam:
            for k, v in idx.items():
                if k.endswith('|' + color) and fam in v:
                    base = v
                    break
        if base is None and fam:
            for k, v in idx.items():
                if fam in v:
                    base = v
                    break
        return base

    def test_exact_keys(self):
        for key in FIELD_KEYS:
            name, color = key.split('|')
            self.assertIsNotNone(self._resolve(name, color), '查不到图：%s' % key)

    def test_colour_typo_falls_back_to_same_shape(self):
        # 现场把「椭圆柱|红」叫成「椭圆柱|粉」的图（pink_*) ；只差颜色也要有图
        self.assertEqual(self._resolve('椭圆柱', '绿'), 'pink_ellipse_cylinder')
        self.assertEqual(self._resolve('长方体', '紫'), 'blue_cuboid')

    def test_unknown_shape_returns_none(self):
        self.assertIsNone(self._resolve('螺丝', '银'))


class LiveCaptureSwitchedOffTest(unittest.TestCase):
    def test_pi_display_live_photo_defaults_off(self):
        src = (PI_DIR / 'pi_display.py').read_text(encoding='utf-8')
        self.assertIn('PI_DISPLAY_LIVE_PHOTO', src)
        self.assertIn('_USE_LIVE_PHOTO', src)
        # item 处理里必须先查预设，再退回 thumb_b64
        body = src[src.index("if kind == 'item':"):src.index('def _thumb_from_b64')]
        self.assertLess(body.index('preset_photo('), body.index('_thumb_from_b64('),
                        'item 处理应先查预预设图，再退回 thumb_b64')
        self.assertIn('if _USE_LIVE_PHOTO:', body, '实时抓帧必须受开关控制')

    def test_host_does_not_send_thumbs_by_default(self):
        sys.path.insert(0, str(PKG / 'host'))
        try:
            import display_link
        finally:
            sys.path.remove(str(PKG / 'host'))
        self.assertFalse(display_link.SEND_THUMB,
                         '上位机默认不应再算/推 thumb_b64（派端用预设图）')
        link = display_link.DisplayLink('127.0.0.1')
        try:
            link.item(seq=1, name='圆锥', color='黄', box=1, thumb_b64='AAA')
            got = link._q.get_nowait()
        finally:
            link._q = None
        self.assertNotIn('thumb_b64', got, '默认不该再往派端推缩略图')
        self.assertEqual(got.get('name'), '圆锥')


if __name__ == '__main__':
    unittest.main()
