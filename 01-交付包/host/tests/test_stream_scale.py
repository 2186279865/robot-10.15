"""图传降负载（2026-10-31 现场问题）的回归测试。

现场症状：Z 降到底之后要停顿很久才开始夹紧/抬升。
根因不是轴慢，而是**图传把派端 CPU 和无线挤满**，导致运动回执迟到 2~15 s
（固件自报只花 1.0~1.6 s，`host_client` 的迟到统计能点名）。

修法三处，这里测前两处的静态部分：
  1. 取帧降到派端相机原生 1280×720 + JPEG 质量 60（原来请求 1920×1080，
     派端还要用 INTER_LINEAR 放大，白花 2.25 倍像素/码流）；
  2. 标定点是按 1920×1080 量的 → `App.detect_targets` 按实际帧尺寸**按比例缩放**源点，
     下游 `center_tray` 仍是 0~1 归一化坐标，所以**不用重新标定**；
  3. 预览节流 `preview_fps`（识别时 `_ramp_streams` 临时拉满）。
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

    def test_requests_native_resolution(self):
        self.assertEqual((int(self.cfg['width']), int(self.cfg['height'])), (1280, 720),
                         '派端相机原生是 1280×720，请求更大只会让派端放大（白烧 CPU）')

    def test_keeps_calibration_reference_size(self):
        self.assertEqual((int(self.cfg['calib_width']), int(self.cfg['calib_height'])),
                         (1920, 1080), '标定点是按 1920×1080 量的，改这里等于要求重新标定')

    def test_preview_is_throttled_and_quality_reduced(self):
        self.assertGreater(float(self.cfg['preview_fps']), 0.0)
        self.assertLessEqual(float(self.cfg['preview_fps']), 6.0)
        self.assertLessEqual(int(self.cfg['img_quality']), 75)

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
