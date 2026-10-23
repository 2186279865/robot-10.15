#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CHECK.py —— 交付包自检：Python 环境、依赖、模型、固件、目录结构是否齐全。

用法（在 01-交付包 目录下）：

    .venv\\Scripts\\python.exe CHECK.py
    python CHECK.py                 # 若依赖已装在系统 Python 里

退出码：0 = 全部通过；1 = 有 FAIL。
不联网、不碰硬件、不改任何文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

OK, WARN, FAIL = [], [], []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        OK.append((name, detail or ""))
    except AssertionError as exc:
        FAIL.append((name, str(exc)))
    except Exception as exc:                     # noqa: BLE001 - 自检脚本，任何异常都算失败项
        FAIL.append((name, f"{type(exc).__name__}: {exc}"))


def need(path: Path, min_bytes: int = 1) -> str:
    assert path.exists(), f"缺文件：{path.relative_to(ROOT)}"
    size = path.stat().st_size if path.is_file() else 0
    assert size >= min_bytes, f"{path.relative_to(ROOT)} 太小（{size} 字节，期望 ≥{min_bytes}）"
    return f"{size:,} 字节" if path.is_file() else "目录"


# ---------------------------------------------------------------- Python / 依赖
def _python() -> str:
    v = sys.version_info
    assert (3, 9) <= (v.major, v.minor) <= (3, 13), f"Python {v.major}.{v.minor} 不在 3.9–3.12 支持范围"
    return f"{v.major}.{v.minor}.{v.micro}"


def _numpy() -> str:
    import numpy
    assert tuple(map(int, numpy.__version__.split(".")[:2])) >= (1, 26), f"numpy {numpy.__version__} 太旧"
    return numpy.__version__


def _cv2() -> str:
    import cv2
    assert hasattr(cv2, "imdecode"), "opencv 不完整"
    return cv2.__version__


def _torch() -> str:
    import torch
    cuda = "cuda" if torch.cuda.is_available() else "cpu"
    return f"{torch.__version__} ({cuda})"


def _ultralytics() -> str:
    import ultralytics
    return getattr(ultralytics, "__version__", "?")


def _serial() -> str:
    import serial
    return serial.__version__


def _tk() -> str:
    import tkinter
    return str(tkinter.TkVersion)


# ---------------------------------------------------------------- 文件与结构
def _model() -> str:
    return need(ROOT / "vision" / "models" / "sam_b.pt", 300 * 1024 * 1024)


def _hex() -> str:
    return need(ROOT / "stm32" / "hex" / "STM32F429.hex", 60_000)


def _uvprojx() -> str:
    return need(ROOT / "stm32" / "project" / "STM32F407.uvprojx", 10_000)


def _main_c() -> str:
    return need(ROOT / "stm32" / "firmware" / "MY" / "main.c", 1_000)


def _wire() -> str:
    return need(ROOT / "common" / "wire.py", 1_000)


def _pi_agent() -> str:
    return need(ROOT / "orangepi" / "pi" / "pi_agent.py", 20_000)


def _host_gui() -> str:
    return need(ROOT / "host" / "host_gui.py", 20_000)


def _main_py() -> str:
    return need(ROOT / "main.py", 3_000)


def _entry_files() -> str:
    for name in ("run.bat", "setup.bat", "selftest.bat", "requirements.txt"):
        need(ROOT / name, 50)
    return "4 个入口文件都在"


def _bat_encoding() -> str:
    """批处理必须是纯 ASCII + CRLF，否则 cmd.exe 会把行切错（本项目踩过这个坑）。"""
    for name in ("run.bat", "setup.bat", "selftest.bat"):
        data = (ROOT / name).read_bytes()
        bad = [b for b in data if b > 127]
        assert not bad, f"{name} 里有 {len(bad)} 个非 ASCII 字节（cmd 会解析错行）"
        assert b"\r\n" in data, f"{name} 不是 CRLF 换行"
        assert data.count(b"\n") == data.count(b"\r\n"), f"{name} 混用了 LF/CRLF"
    return "run/setup/selftest 都是纯 ASCII + CRLF"


def _runtime_dir() -> str:
    (ROOT / "runtime").mkdir(exist_ok=True)
    return "runtime/ 可用（可随时清空）"


def _venv_hint() -> str:
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return "已有 .venv（setup.bat 已跑过）"
    raise AssertionError("还没有 .venv —— 先跑 setup.bat（或用系统 Python 直接跑本脚本）")


CHECKS = [
    ("Python 版本", _python),
    ("numpy", _numpy),
    ("opencv", _cv2),
    ("torch", _torch),
    ("ultralytics", _ultralytics),
    ("pyserial", _serial),
    ("tkinter", _tk),
    ("主入口 main.py", _main_py),
    ("上位机 host/host_gui.py", _host_gui),
    ("派端 orangepi/pi/pi_agent.py", _pi_agent),
    ("共用协议 common/wire.py", _wire),
    ("视觉权重 vision/models/sam_b.pt", _model),
    ("固件源码 stm32/firmware/MY/main.c", _main_c),
    ("Keil 工程 stm32/project/STM32F407.uvprojx", _uvprojx),
    ("烧录文件 stm32/hex/STM32F429.hex", _hex),
    ("入口文件齐全", _entry_files),
    ("批处理编码（ASCII+CRLF）", _bat_encoding),
    ("runtime 目录", _runtime_dir),
    (".venv（可选）", _venv_hint),
]


def main() -> int:
    print("=" * 64)
    print(" 01-交付包 自检")
    print("=" * 64)
    for name, fn in CHECKS:
        check(name, fn)

    width = max(len(n) for n, _ in OK + WARN + FAIL) if (OK or WARN or FAIL) else 10
    for name, detail in OK:
        print(f"[ OK ] {name:<{width}}  {detail}")
    for name, detail in WARN:
        print(f"[WARN] {name:<{width}}  {detail}")
    for name, detail in FAIL:
        print(f"[FAIL] {name:<{width}}  {detail}")

    print("-" * 64)
    print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项。")
    if FAIL:
        print()
        print("下一步：")
        print("  - 依赖缺失 → 双击 setup.bat（会建 .venv 并装 requirements.txt）")
        print("  - 其它缺失 → 这个包不完整，重新拷贝 01-交付包 整个目录")
        return 1
    print()
    print("环境齐了，可以：双击 run.bat（或 run.bat --pi <派IP>）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
