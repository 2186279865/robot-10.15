#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""队友电脑一键自检（19 项）。

用法（仓库根目录）：
    python 04-队友视觉调试包/tools/check_teammate.py
    python 04-队友视觉调试包/tools/check_teammate.py --no-segment   # 跳过 SAM 实跑（省 3-10 秒）

退出码：0 = 全过 / 1 = 有失败项。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG4 = HERE.parent                        # 04-队友视觉调试包
ROOT = PKG4.parent                        # 仓库根
PKG1 = ROOT / "01-交付包"
VISION = PKG1 / "vision"
MODEL = VISION / "models" / "sam_b.pt"
MODEL_BYTES = 375042383
SAMPLES = PKG4 / "samples"

# ultralytics 会往 %APPDATA% 写配置；受限环境下会刷一堆 ERROR，指到仓库内即可
os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT / ".yolo_config"))

OK = "PASS"
NO = "FAIL"
WARN = "WARN"

results: list[tuple[str, str, str]] = []


def add(name: str, state: str, detail: str = "") -> None:
    results.append((state, name, detail))


def check_python() -> None:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) in ((3, 10), (3, 11), (3, 12)):
        add("Python 3.10-3.12", OK, ver)
    elif v.major == 3 and v.minor >= 13:
        add("Python 3.10-3.12", WARN, f"{ver}：能用但 ultralytics/torch 轮子可能还没跟上，出问题请降到 3.12")
    else:
        add("Python 3.10-3.12", NO, f"{ver}：太旧/太新，请装 3.12")
    add("64 位解释器", OK if sys.maxsize > 2**32 else NO, "64-bit" if sys.maxsize > 2**32 else "32-bit")


def check_modules() -> None:
    for mod, tip in (
        ("cv2", "pip install opencv-python"),
        ("numpy", "pip install numpy"),
        ("PIL", "pip install pillow"),
        ("torch", "pip install torch torchvision"),
        ("ultralytics", "pip install ultralytics"),
    ):
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "?")
            add(f"依赖 {mod}", OK, str(ver))
        except Exception as exc:                       # noqa: BLE001
            add(f"依赖 {mod}", NO, f"缺失（{tip}）：{exc}")


def check_tk() -> None:
    try:
        import tkinter  # noqa: F401

        add("tkinter（可选，看画面用）", OK, "可用")
    except Exception as exc:                           # noqa: BLE001
        add("tkinter（可选，看画面用）", WARN, f"不可用：{exc}")


def check_repo() -> None:
    if not PKG1.is_dir():
        add("仓库结构 01-交付包/", NO, f"找不到 {PKG1}（是不是没在仓库根目录跑？）")
        return
    add("仓库结构 01-交付包/", OK, str(PKG1))
    for rel in ("vision/auto_segment.py", "vision/tray_calib.py", "host/host_client.py",
                "common/wire.py", "docs/网络协议-接口约定.md"):
        p = PKG1 / rel
        add(f"文件 {rel}", OK if p.is_file() else NO, f"{p.stat().st_size} B" if p.is_file() else "缺失")


def check_model() -> None:
    if not MODEL.is_file():
        add("模型 sam_b.pt", NO,
            f"缺失：{MODEL}（357.7 MB，不在 git 里，必须从 A 机拷过来放到这个路径）")
        return
    size = MODEL.stat().st_size
    if size == MODEL_BYTES:
        add("模型 sam_b.pt", OK, f"{size:,} B（字节数正确）")
    elif abs(size - MODEL_BYTES) < 1024:
        add("模型 sam_b.pt", WARN, f"{size:,} B（与期望差 {size - MODEL_BYTES:+d} B，可能拷贝不完整）")
    else:
        add("模型 sam_b.pt", NO, f"{size:,} B ≠ 期望 {MODEL_BYTES:,} B（拷贝不完整/放错文件）")


def check_venv() -> None:
    exe = Path(sys.executable)
    if (PKG1 / ".venv").is_dir() and (PKG1 / ".venv" / "Scripts" / "python.exe").is_file():
        add(".venv 存在", OK, str(PKG1 / ".venv"))
    else:
        add(".venv 存在", WARN, "没建虚拟环境（可以，但建议 cd 01-交付包 && setup.bat）")
    add("当前解释器", OK, str(exe))


def check_segment(do_segment: bool) -> None:
    """按真机契约实跑一次分割：list[dict] + 关键字。"""
    if not do_segment:
        add("AutoSegmenter 实跑", WARN, "已按 --no-segment 跳过")
        return
    if not MODEL.is_file():
        add("AutoSegmenter 实跑", NO, "模型缺失，跳过")
        return
    sample = None
    for name in ("real_cam0.jpg", "real_new0.jpg", "real_cam1.jpg", "real_new1.jpg"):
        if (SAMPLES / name).is_file():
            sample = SAMPLES / name
            break
    if sample is None:
        add("AutoSegmenter 实跑", NO, "samples/ 下没有样例图")
        return
    try:
        sys.path.insert(0, str(VISION))
        from auto_segment import AutoSegmenter          # noqa: WPS433
        import cv2

        frame = cv2.imread(str(sample))
        if frame is None:
            add("AutoSegmenter 实跑", NO, f"读不出图：{sample}")
            return
        seg = AutoSegmenter(str(MODEL))
        out = seg.segment(frame)
        need = {"mask", "contour", "bbox", "center_px", "center_tray", "area_px", "confidence"}
        if not isinstance(out, list):
            add("AutoSegmenter 实跑", NO, f"返回值是 {type(out).__name__}，契约要求 list[dict]")
            return
        if out and not need.issubset(set(out[0].keys())):
            miss = need - set(out[0].keys())
            add("AutoSegmenter 实跑", NO, f"返回字典缺字段：{sorted(miss)}")
            return
        add("AutoSegmenter 实跑", OK,
            f"{sample.name} → {len(out)} 个目标"
            + (f"，首个 center_tray={out[0]['center_tray']}" if out else "（0 目标也算通过，图里可能没物体）"))
    except Exception as exc:                           # noqa: BLE001
        import traceback

        add("AutoSegmenter 实跑", NO, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


def check_git() -> None:
    import subprocess

    def run(*args: str) -> str:
        try:
            return subprocess.run(  # noqa: S603
                ["git", *args], cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10
            ).stdout.strip()
        except Exception:                              # noqa: BLE001
            return ""

    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    add("git 分支", OK if branch else WARN, branch or "（不是 git 仓库或没有 git）")
    if branch == "main":
        add("不要直接在 main 上改", WARN, "建议：git checkout -b feat/vision-你的名字")
    else:
        add("不要直接在 main 上改", OK, f"当前在 {branch}")
    dirty = run("status", "--porcelain")
    add("工作区状态", OK if not dirty else WARN,
        "干净" if not dirty else f"{len(dirty.splitlines())} 个未提交改动")


def main() -> int:
    ap = argparse.ArgumentParser(description="队友电脑一键自检")
    ap.add_argument("--no-segment", action="store_true", help="跳过 SAM 实跑（省 3-10 秒）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（给 AI 解析）")
    args = ap.parse_args()

    print("=" * 66)
    print(" 队友视觉调试包 · 一键自检")
    print(f" 仓库根：{ROOT}")
    print("=" * 66)

    check_python()
    check_repo()
    check_model()
    check_venv()
    check_modules()
    check_tk()
    check_git()
    check_segment(not args.no_segment)

    fails = [r for r in results if r[0] == NO]
    warns = [r for r in results if r[0] == WARN]

    if args.json:
        print(json.dumps(
            {"ok": not fails, "results": [{"state": s, "name": n, "detail": d} for s, n, d in results]},
            ensure_ascii=False, indent=2))
    else:
        for state, name, detail in results:
            mark = {OK: "[ OK ]", NO: "[FAIL]", WARN: "[WARN]"}[state]
            print(f"{mark} {name}" + (f" — {detail}" if detail else ""))
        print("-" * 66)
        print(f"通过 {len(results) - len(fails) - len(warns)} / 警告 {len(warns)} / 失败 {len(fails)}")
        if fails:
            print("\n下一步：先修 FAIL 项（看上面的提示），再重跑本脚本。")
            print("详细说明：04-队友视觉调试包/指南-部署与视觉调试.md")
        else:
            print("\n可以开始干活了：")
            print("  1) 看画面：python 04-队友视觉调试包/tools/view_live.py --host <派IP>")
            print("  2) 离线跑：python 04-队友视觉调试包/tools/vision_offline.py --image samples/real_cam0.jpg")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
