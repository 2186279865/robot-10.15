"""白色/浅色物块候选分支的回归测试（2026-10-31 现场问题）。

现场症状：托盘上两个**白色六棱锥**一直识别不出来。
根因有两道门（都实测过）：
  1. 候选前景 = `饱和度 > 45 & 亮度 > 35`，白块实测 S≈11~15 → SAM 一个提示点都落不到；
  2. 即便打了点，掩膜还要过「物体性」门（`sat_mean < 32 and diff_mean < 20` 就丢），
     白块 S≈10、现场又没开 gray_objects（无空托盘差分）→ 必被丢。

修法：
  - 新增 `_white_mask()`：局部亮度比 `V/模糊(V) > 1.10` + 低饱和 + 够亮；
  - `_white_ok()`：块级面积/填充率过滤（滤掉托盘内壁的斜向反光带）；
  - `segment()` 把白色候选并入前景，并放宽「物体性」门（白候选命中 ≥35% 即可）。

这里用**合成图**和**假 SAM** 验证，不加载真模型（不依赖 CUDA）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

PKG = Path(__file__).resolve().parents[2]      # 01-交付包
for _p in (PKG / 'vision', PKG / 'host'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cv2                       # noqa: E402
import auto_segment as AS        # noqa: E402

S = 700


def _tray(bgr=(120, 120, 120)):
    img = np.zeros((S, S, 3), np.uint8)
    img[:, :] = bgr
    return img


def _white_hexagon(img, cx, cy, r=52, bgr=(238, 238, 238)):
    """画一个不规则六边形（白棱锥俯视轮廓），返回它的掩膜。"""
    pts = []
    for i in range(6):
        a = np.deg2rad(60 * i + 15)
        pts.append((int(cx + r * np.cos(a)), int(cy + r * np.sin(a))))
    mask = np.zeros((S, S), np.uint8)
    cv2.fillPoly(mask, [np.array(pts, np.int32)], 255)
    img[mask > 0] = bgr
    return mask


class _Tensor:
    """假张量：只要 .cpu().numpy()。"""

    def __init__(self, arr):
        self._a = arr

    def cpu(self):
        return self

    def numpy(self):
        return self._a


class _Masks:
    def __init__(self, arr):
        self.data = _Tensor(arr)


class _Boxes:
    def __init__(self, conf):
        self.conf = _Tensor(np.asarray(conf, dtype=np.float32))


class _Result:
    def __init__(self, masks, conf):
        self.masks = _Masks(masks)
        self.boxes = _Boxes(conf)


class _FakeSAM:
    """对每个提示点都返回同一个掩膜（模拟 SAM 把该物体分出来）。"""

    def __init__(self, mask_bool):
        self.mask = mask_bool.astype(np.float32)
        self.calls = []

    def predict(self, frame, points=None, labels=None, verbose=False, device=None):
        pts = list(points or [])
        self.calls.append(pts)
        n = max(1, len(pts))
        return [_Result(np.repeat(self.mask[None], n, axis=0), [.9] * n)]


class WhiteCandidateTest(unittest.TestCase):
    def setUp(self):
        # 与 vision/config.json 现场值保持一致
        self.cfg = {'white_objects': True, 'white_ratio': 1.10, 'white_sat_max': 55,
                    'white_val_min': 80, 'white_min_area': 5000, 'white_min_fill': 0.35}

    def test_white_object_is_not_a_colour_candidate(self):
        """白块的饱和度太低，老的颜色分支根本看不到它（这就是现场 bug 的第 1 道门）。"""
        img = _tray()
        _white_hexagon(img, 300, 400)
        color, _g, _d, _hsv, white = AS.AutoSegmenter._foregrounds(img, None, self.cfg)
        self.assertEqual(int((color > 0)[350:450, 250:350].sum()), 0,
                         '白色物体不该出现在颜色候选里（S≈10 < 45）')
        self.assertGreater(int((white > 0)[350:450, 250:350].sum()), 0,
                           '新增的白色分支必须能看到它')

    def test_white_blob_passes_area_and_reaches_sam(self):
        """白棱锥要能走完：候选 → 提示点 → SAM 掩膜 → 过物体性门。"""
        img = _tray()
        mask = _white_hexagon(img, 300, 400)
        seg = object.__new__(AS.AutoSegmenter)      # 不加载真模型
        seg.model = _FakeSAM(mask > 0)
        stats = {}
        out = seg.segment(img, None, self.cfg, stats)
        self.assertEqual(stats.get('white_blobs'), 1)
        self.assertTrue(out, '白色六棱锥应该能分成一个实例（第 2 道门也要放行）')
        x, y, w, h = out[0]['bbox']
        self.assertTrue(x <= 300 <= x + w and y <= 400 <= y + h)
        self.assertTrue(seg.model.calls and seg.model.calls[0],
                        'SAM 应该收到落在白块上的提示点')

    def test_rim_like_band_is_rejected(self):
        """托盘内壁的斜向反光带（面积小、细长）不能变成假目标。"""
        img = _tray()
        band = np.zeros((S, S), np.uint8)
        pts = np.array([[420, 300], [700, 470], [700, 540], [420, 380]], np.int32)
        cv2.fillPoly(band, [pts], 255)
        img[band > 0] = (238, 238, 238)
        white = AS.AutoSegmenter._white_mask(
            cv2.cvtColor(img, cv2.COLOR_BGR2HSV), self.cfg)
        for b in AS.AutoSegmenter._blobs(white, None, 0.0,
                                         int(self.cfg['white_min_area'] * 0.6)):
            self.assertFalse(AS.AutoSegmenter._white_ok(b['bbox'], b['area'], self.cfg),
                             '细长反光带必须被 _white_ok 挡掉：%r' % (b['bbox'],))

    def test_coloured_block_stays_in_colour_branch_only(self):
        img = _tray()
        mask = np.zeros((S, S), np.uint8)
        cv2.rectangle(mask, (250, 350), (360, 460), 255, -1)
        img[mask > 0] = (40, 40, 205)               # 饱和红（BGR）
        color, _g, _d, _hsv, white = AS.AutoSegmenter._foregrounds(img, None, self.cfg)
        self.assertGreater(int((color > 0)[380:430, 280:330].sum()), 0)
        self.assertEqual(int((white > 0)[380:430, 280:330].sum()), 0,
                         '高饱和的彩色块不该被当成白色候选')

    def test_flat_tray_has_no_white_phantom(self):
        img = _tray()
        _c, _g, _d, _hsv, white = AS.AutoSegmenter._foregrounds(img, None, self.cfg)
        self.assertEqual(len(AS.AutoSegmenter._blobs(
            white, None, 0.0, int(self.cfg['white_min_area']))), 0,
            '空托盘（均匀灰面）不能凭空冒出白色候选')

    def test_small_glint_is_filtered_by_area(self):
        img = _tray()
        img[400:414, 300:314] = (250, 250, 250)     # 14x14 反光点 ≈200 px
        _c, _g, _d, _hsv, white = AS.AutoSegmenter._foregrounds(img, None, self.cfg)
        blobs = AS.AutoSegmenter._blobs(white, None, 0.0,
                                        int(self.cfg['white_min_area'] * 0.6))
        self.assertTrue(all(not AS.AutoSegmenter._white_ok(b['bbox'], b['area'], self.cfg)
                            for b in blobs), '小块反光不该浪费 SAM 提示点')

    def test_white_branch_can_be_disabled(self):
        img = _tray()
        _white_hexagon(img, 300, 400)
        cfg = dict(self.cfg, white_objects=False)
        parts = AS.AutoSegmenter._foregrounds(img, None, cfg)
        self.assertEqual(len(parts), 5)
        self.assertIsNone(parts[4], 'white_objects=false 时不应该生成白色候选')


if __name__ == '__main__':
    unittest.main()
