"""视觉目录解析：上位机要在哪里找视觉产物。

`final/` 里上位机在 `final/ghr/10.15/上位机/`，视觉在 `final/new_eye/`，所以老代码用
`Path(__file__).parents[3] / 'new_eye'` 硬算相对层级。搬到 `10.21/` 之后层级变了
（`10.21/host/` 与 `10.21/vision/` 是兄弟目录），再按 parents[N] 算就会指到包外，
所以统一从这里取路径。

优先级：
1. `10.21/vision/`（本包，正常情况）
2. `<包外>/final/new_eye/`（搬家前的旧布局，兼容用）
3. `<包外>/new_eye/`（仓库主线布局，兼容用）
"""
from __future__ import annotations

from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent
PKG_ROOT = HOST_DIR.parent


def _pick_vision_dir():
    for cand in (
        PKG_ROOT / 'vision',
        PKG_ROOT.parent / 'final' / 'new_eye',
        PKG_ROOT.parent / 'new_eye',
    ):
        if (cand / 'tray_calib.py').is_file():
            return cand
    # 都没有也返回本包路径，让调用方自己报“文件不存在”，错误信息里路径才对
    return PKG_ROOT / 'vision'


VISION_DIR = _pick_vision_dir()
VISION_RUNTIME = VISION_DIR / 'runtime'
LATEST_TARGETS = VISION_RUNTIME / 'latest_targets.json'
TRAY_CALIB = VISION_DIR / 'tray_calib.json'
CALIB_MARKS = VISION_RUNTIME / 'calib_marks.json'
TRAY_CALIB_PY = VISION_DIR / 'tray_calib.py'
