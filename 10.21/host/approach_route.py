"""接近段示教（X / Y / Z 三轴）：把「整机上电起点 → 抓取点」这段路记下来，之后自动重放。

为什么要有这个模块
------------------
本机四轴**没有编码器回读**（PA3 那根回读线没接，`motor_query` 一直回 E04），
固件里的「绝对位置」实际是按相对下发。所以「从初始位走到抓取位」只能靠
**开环重复**：只要每次真的从同一个起点出发，重复精度就是步进电机本身的精度。

用户的操作流程（现场）：
    1) 整机上电 → 机械停在固定的初始位（这就是「起点」，不做任何清零）
    2) 点「开始示教」，用四轴控制页的点动按钮，把 X/Y/Z 一步步挪到抓取位
    3) 点「结束并保存」→ 写进 runtime/approach_route.json
    4) 以后每次「从初始位开始抓取」，这段路自动先跑一遍，跑完正好停在抓取位

记录粒度为「一段点动」= 一根轴 + 方向 + 脉冲数 + 转速 + 加速度，按操作顺序存。
托盘（地址 3）不记录：它有自己的六工位逻辑，混进来会把定位搞乱。

本文件只做**纯数据与校验**（无 tkinter、无串口），方便单测；界面与执行在 host_gui.py。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

#: 默认落盘位置：10.21/runtime/approach_route.json（不进 git，每台机器自己示教）
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "runtime" / "approach_route.json"

VERSION = 1

#: 允许示教的轴：X(4) / Y(5) / Z(1)。托盘(3) 明确排除。
AXES = (4, 5, 1)
AXIS_NAMES = {4: "X", 5: "Y", 1: "Z"}
PULSES_PER_REV = 3200

#: 单段脉冲上限（防止手滑输入天文数字）：100 圈
MAX_PULSES = 100 * PULSES_PER_REV
MAX_STEPS = 200


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def empty() -> dict:
    """空路线。"""
    return {"version": VERSION, "saved_at": "", "note": "", "steps": []}


def load(path: str | Path | None = None) -> dict:
    """读路线；文件不存在/损坏/版本不符 → 返回空路线（绝不抛，界面不能因为文件坏了打不开）。"""
    target = Path(path) if path else DEFAULT_PATH
    if not target.exists():
        return empty()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return empty()
    if not isinstance(data, dict) or int(data.get("version") or 0) != VERSION:
        return empty()
    steps = []
    for raw in data.get("steps") or []:
        try:
            steps.append({
                "axis": int(raw["axis"]),
                "dir": int(raw["dir"]),
                "rpm": int(raw["rpm"]),
                "acc": int(raw["acc"]),
                "pulses": int(raw["pulses"]),
            })
        except Exception:
            continue
    route = empty()
    route["saved_at"] = str(data.get("saved_at") or "")
    route["note"] = str(data.get("note") or "")
    route["steps"] = [s for s in steps if s["axis"] in AXES and s["pulses"] > 0]
    return route


def save(route: dict, path: str | Path | None = None) -> Path:
    """原子写：先写 .tmp 再替换，避免掉电写出半个文件。"""
    target = Path(path) if path else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    route = dict(route)
    route["version"] = VERSION
    route["saved_at"] = _now()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def add_step(route: dict, axis: int, direction: int, rpm: int, acc: int, pulses: int) -> dict:
    """追加一段。非法输入抛 ValueError（界面会把它显示出来，不静默吞掉）。"""
    axis, direction, rpm, acc, pulses = (
        int(axis), int(direction), int(rpm), int(acc), int(pulses))
    if axis not in AXES:
        raise ValueError(f"轴 {axis} 不能示教（只支持 X(4)/Y(5)/Z(1)，托盘不示教）")
    if direction not in (0, 1):
        raise ValueError(f"方向必须是 0(正) 或 1(负)，收到 {direction}")
    if pulses <= 0:
        raise ValueError(f"脉冲数必须 > 0，收到 {pulses}")
    if pulses > MAX_PULSES:
        raise ValueError(f"一段最多 {MAX_PULSES} 脉冲（{MAX_PULSES // PULSES_PER_REV} 圈）")
    if len(route.get("steps") or []) >= MAX_STEPS:
        raise ValueError(f"最多记 {MAX_STEPS} 段，先保存再清空重来")
    steps = list(route.get("steps") or [])
    steps.append({"axis": axis, "dir": direction, "rpm": rpm, "acc": acc, "pulses": pulses})
    route["steps"] = steps
    return route


def undo(route: dict) -> dict:
    """撤销最后一段。"""
    steps = list(route.get("steps") or [])
    if steps:
        steps.pop()
    route["steps"] = steps
    return route


def clear(route: dict) -> dict:
    """清空（已保存文件不动，等用户再点保存）。"""
    route["steps"] = []
    return route


def totals(route: dict) -> dict:
    """按轴汇总「正脉冲 - 负脉冲」，用于界面显示净位移。"""
    out = {axis: 0 for axis in AXES}
    for step in route.get("steps") or []:
        sign = 1 if int(step["dir"]) == 0 else -1
        out[int(step["axis"])] = out.get(int(step["axis"]), 0) + sign * int(step["pulses"])
    return out


def describe(route: dict) -> str:
    """一行摘要，给状态栏用：`3 段｜X +12000  Y -4500  Z +8000`。"""
    steps = route.get("steps") or []
    if not steps:
        return "0 段（还没示教）"
    parts = []
    for axis in AXES:
        net = totals(route).get(axis, 0)
        if net:
            parts.append(f"{AXIS_NAMES[axis]} {net:+d}")
    return f"{len(steps)} 段" + ("｜" + "  ".join(parts) if parts else "")


def step_ms(step: dict, pulses_per_rev: int = PULSES_PER_REV) -> int:
    """估算一段要跑多久（毫秒），额外给 400ms 缓冲。

    没有回读，只能按「转速 → 脉冲/秒」算；转速为 0 时按 60rpm 兜底。
    """
    rpm = max(1, int(step.get("rpm") or 60))
    pulses = max(1, int(step.get("pulses") or 1))
    per_second = rpm * pulses_per_rev / 60.0
    return int(pulses / per_second * 1000) + 400


def estimate_ms(route: dict) -> int:
    return sum(step_ms(s) for s in route.get("steps") or [])


def build_steps(route: dict, speed_rpm: int = 0) -> list[dict]:
    """重放用的步骤表。speed_rpm > 0 时统一改写转速（「速度后期调整」用）。"""
    speed_rpm = int(speed_rpm or 0)
    out = []
    for step in route.get("steps") or []:
        item = dict(step)
        if speed_rpm > 0:
            item["rpm"] = speed_rpm
        out.append(item)
    return out


def validate(route: dict) -> list[str]:
    """执行前的静态检查，返回问题列表（空 = 可以跑）。"""
    problems = []
    steps = route.get("steps") or []
    if not steps:
        problems.append("接近段是空的：先点「开始示教」走一遍再保存")
        return problems
    for index, step in enumerate(steps, 1):
        if int(step.get("axis")) not in AXES:
            problems.append(f"第 {index} 段的轴 {step.get('axis')} 不在 X/Y/Z 里")
        if int(step.get("pulses") or 0) <= 0:
            problems.append(f"第 {index} 段的脉冲数不是正数")
    return problems
