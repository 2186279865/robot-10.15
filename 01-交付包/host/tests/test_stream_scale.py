"""图传 / 标定坐标系的回归测试（2026-10-31 现场问题）。

现场症状：Z 降到底之后要停顿很久才开始夹紧/抬升。
根因不是轴慢，而是**图传把派端 CPU 和无线挤满**，导致运动回执迟到 2~15 s
（固件自报只花 1.0~1.6 s，`host_client` 的迟到统计能点名）。

当时的修法有三处，实际留下的是：
  1. **回执不阻塞运动**（`prep_pose.await_goto` + `host_client.extend_deadline`）——保留；
  2. **降图传分辨率/质量/限帧**——试过 1280×720 + 质量 60 + 预览 4 fps，链路确实变松，
     但画面标注（校正点 P1~P4、目标框、`_vlm_crop_now` 裁剪、运行台叠加框）都按
     1920×1080 标定坐标画，降分辨率后整体错位 → 已回退。
     本文件因此守住「取帧尺寸 == 标定基准」这条，防止下次又单方面降分辨率；
  3. `Stream.max_fps` 节流的算术边界（代码保留，配置里 `preview_fps=0` 即不限帧）。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np

PKG = Path(__file__).resolve().parents[2]      # 01-交付包
VISION = PKG / 'vision'
for _p in (VISION, PKG / 'host'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cv2                       # noqa: E402
import gui as V                  # noqa: E402


class _RectSegmenter:
    """假分割器：不跑模型，只记录收到的 warp 尺寸并返回空列表。"""

    def __init__(self):
        self.calls = []

    def segment(self, warp, bg=None, cfg=None, stats=None):
        self.calls.append((warp.shape, None if bg is None else bg.shape))
        return []


def _new_app(pts, cfg=None):
    """不跑 __init__（会建 Tk 控件），只摆好 detect_targets 需要的字段。"""
    app = object.__new__(V.App)
    app.points = [tuple(p) for p in pts]
    app.side_points = []
    app.cfg = dict(cfg or {})
    app.segmenter = _RectSegmenter()
    app.note = lambda *_a, **_k: None
    return app


class DetectTargetsScaleTest(unittest.TestCase):
    """标定点是 1920×1080 坐标；取帧变小后必须按比例缩放，否则整个托盘错位。"""

    PTS = [(100.0, 100.0), (500.0, 100.0), (500.0, 400.0), (100.0, 400.0)]

    def _expected_warp(self, frame, fw, fh):
        """独立算一遍：标定点按 (fw/1920, fh/1080) 缩放后投到 700×700。"""
        sx, sy = fw / 1920.0, fh / 1080.0
        src = np.array([(x * sx, y * sy) for (x, y) in self.PTS], np.float32)
        dst = np.array([[0, 0], [700, 0], [700, 700], [0, 700]], np.float32)
        H = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(frame, H, (700, 700))

    def _check(self, fw, fh):
        app = _new_app(self.PTS, {'calib_width': 1920, 'calib_height': 1080})
        rs = np.random.RandomState(7)
        frame = rs.randint(0, 255, (fh, fw, 3)).astype(np.uint8)
        _masks, warp = app.detect_targets(frame, 'top')
        self.assertIsNotNone(warp, 'detect_targets 应该返回校正后的 warp 图')
        self.assertEqual(warp.shape[:2], (700, 700))
        expect = self._expected_warp(frame, fw, fh)
        diff = float(np.abs(warp.astype(np.int16) - expect.astype(np.int16)).mean())
        self.assertLess(diff, 1.0, '校正结果与「按比例缩放标定点」的结果应逐像素一致')

    def test_scales_points_for_720p_frames(self):
        self._check(1280, 720)

    def test_no_scaling_at_calibration_resolution(self):
        self._check(1920, 1080)

    def test_segmenter_still_gets_700_square_warp(self):
        app = _new_app(self.PTS, {'calib_width': 1920, 'calib_height': 1080})
        app.detect_targets(np.zeros((720, 1280, 3), np.uint8), 'top')
        self.assertEqual(app.segmenter.calls[0][0][:2], (700, 700))


class ShippedConfigTest(unittest.TestCase):
    """仓库自带的 config.json 必须保持「降负载」这套取值（现场按它跑）。"""

    def setUp(self):
        self.cfg = json.loads((VISION / 'config.json').read_text(encoding='utf-8'))

    def test_requests_calibration_resolution(self):
        """取帧分辨率必须等于标定基准：显示路径（校正点/目标框/裁剪）都按这个坐标系画。

        2026-10-31 曾把请求降到派端原生 1280×720 来降链路负载，但标注整体错位（现场当场发现），
        已回退。要再降分辨率，得同时给 gui 的 points 绘制、`_vlm_crop_now`、run_tab 叠加框加缩放。
        """
        self.assertEqual((int(self.cfg['width']), int(self.cfg['height'])), (1920, 1080))
        self.assertEqual((int(self.cfg['calib_width']), int(self.cfg['calib_height'])),
                         (int(self.cfg['width']), int(self.cfg['height'])),
                         '取帧尺寸和标定基准不一致 → 画面标注会错位')

    def test_stream_uses_pi_defaults(self):
        """预览不限帧、JPEG 质量交给派端配置（0）；回退后不该再带限帧/降质。"""
        self.assertEqual(float(self.cfg.get('preview_fps') or 0), 0.0)
        self.assertEqual(int(self.cfg.get('img_quality') or 0), 0)

    def test_empty_background_still_configured(self):
        # 白块/灰度差分都靠它；别在改分辨率时把它删了
        self.assertTrue(bool(self.cfg.get('empty_background')))


class StreamThrottleTest(unittest.TestCase):
    """`Stream.max_fps` 的边界行为（真实取流在 GUI 里跑，这里只测算术）。"""

    def test_set_max_fps_clamps_negative(self):
        st = object.__new__(V.Stream)
        st.max_fps = 4.0
        st.set_max_fps(-2)
        self.assertEqual(st.max_fps, 0.0)
        st.set_max_fps(4)
        self.assertEqual(st.max_fps, 4.0)


if __name__ == '__main__':
    unittest.main()
