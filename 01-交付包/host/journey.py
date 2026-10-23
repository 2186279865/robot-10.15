"""本次上电后的“全部移动”记录：一条路，可以原路返回初始位。

和 `approach_route.py` 的区别：
- `approach_route`：**示教**用，只有 X/Y/Z，存一次、以后每次抓取前重放；
- `journey`（本模块）：**自动**记录，四轴全收（含托盘 3），从上电开始一直追加，
  点「原路返回初始位」时把这些位移**倒序、反向**再走一遍，机械臂就回到上电位置。

只做数据与校验，不碰 tkinter、不碰串口，方便单测。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEFAULT_PATH = ROOT / "runtime" / "journey.json"
VERSION = 1
#: 四轴全收：Z=1、托盘=3、X=4、Y=5（与固件 stepper.h 的地址一致）
AXES = (1, 3, 4, 5)
AXIS_NAMES = {1: "Z", 3: "托盘", 4: "X", 5: "Y"}
PULSES_PER_REV = 3200
MAX_PULSES = 200 * 3200
MAX_STEPS = 2000          # 一次上电最多记 2000 段，超了丢最旧的
SETTLE_MS = 400           # 每段后的缓冲（开环等待）


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def empty(boot=None) -> dict:
    return {"version": VERSION, "boot": boot, "started_at": _now_text(), "steps": []}


def _clean(raw, boot=None) -> dict:
    route = empty(boot)
    if isinstance(raw, dict):
        route["boot"] = raw.get("boot", boot)
        route["started_at"] = raw.get("started_at") or route["started_at"]
        for item in raw.get("steps") or []:
            try:
                axis = int(item["axis"])
                if axis not in AXES:
                    continue
                pulses = int(item["pulses"])
                if pulses <= 0:
                    continue
                route["steps"].append({
                    "axis": axis,
                    "dir": 1 if int(item.get("dir", 0)) else 0,
                    "rpm": max(1, int(item.get("rpm", 60))),
                    "acc": max(0, int(item.get("acc", 1))),
                    "pulses": min(pulses, MAX_PULSES),
                    "kind": "move",
                })
            except (KeyError, TypeError, ValueError):
                continue
    route["steps"] = route["steps"][-MAX_STEPS:]
    return route


def load(path=None, boot=None) -> dict:
    """读回记录；文件不存在/损坏/boot 对不上 → 空记录（绝不抛）。"""
    target = Path(path) if path else DEFAULT_PATH
    if not target.exists():
        return empty(boot)
    try:
        raw = json.loads(target.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return empty(boot)
    if not isinstance(raw, dict) or int(raw.get("version", 0)) != VERSION:
        return empty(boot)
    route = _clean(raw, boot)
    if boot is not None and route.get("boot") not in (None, boot):
        # 固件重启过（boot_id 变了）：上次的记录不再对应这次上电
        return empty(boot)
    return route


def save(route: dict, path=None) -> Path:
    target = Path(path) if path else DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(route)
    payload["version"] = VERSION
    payload["saved_at"] = _now_text()
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf8")
    tmp.replace(target)
    return target


def clear(route: dict) -> None:
    route["steps"] = []


def append(route: dict, axis: int, direction: int, rpm: int, acc: int, pulses: int) -> dict:
    """记一段真实发生的相对位移；参数非法抛 ValueError（中文原因）。"""
    try:
        axis = int(axis)
        direction = int(direction)
        rpm = max(1, int(rpm))
        acc = max(0, int(acc))
        pulses = int(pulses)
    except (TypeError, ValueError):
        raise ValueError("参数不是整数")
    if axis not in AXES:
        raise ValueError("轴只能是 Z(1)/托盘(3)/X(4)/Y(5)")
    if direction not in (0, 1):
        raise ValueError("方向只能是 0(正)/1(负)")
    if pulses <= 0:
        raise ValueError("脉冲数必须大于 0")
    if pulses > MAX_PULSES:
        raise ValueError("单段脉冲太大（>%d）" % MAX_PULSES)
    step = {"axis": axis, "dir": direction, "rpm": rpm, "acc": acc,
            "pulses": pulses, "kind": "move"}
    route.setdefault("steps", []).append(step)
    if len(route["steps"]) > MAX_STEPS:
        del route["steps"][: len(route["steps"]) - MAX_STEPS]
    return step


def reverse_steps(route: dict) -> list:
    """倒序 + 方向取反 = 原路返回。"""
    out = []
    for step in reversed(route.get("steps") or []):
        if step.get("kind", "move") != "move":
            continue
        out.append({
            "axis": int(step["axis"]),
            "dir": 1 - (1 if int(step.get("dir", 0)) else 0),
            "rpm": max(1, int(step.get("rpm", 60))),
            "acc": max(0, int(step.get("acc", 1))),
            "pulses": abs(int(step["pulses"])),
        })
    return out


def net(route: dict) -> dict:
    """每条轴的净位移（正-负，单位脉冲），给界面显示用。"""
    totals = {axis: 0 for axis in AXES}
    for step in route.get("steps") or []:
        axis = int(step["axis"])
        if axis in totals:
            totals[axis] += -step["pulses"] if int(step["dir"]) else step["pulses"]
    return totals


def describe(route: dict) -> str:
    steps = route.get("steps") or []
    if not steps:
        return "0 段（上电后还没动过）"
    parts = []
    for axis in AXES:
        total = net(route)[axis]
        if total:
            parts.append("%s %+d" % (AXIS_NAMES[axis], total))
    return "%d 段｜净 %s" % (len(steps), " ".join(parts) if parts else "0")


def step_ms(step: dict) -> int:
    """单段预计耗时（开环估算 + 缓冲）。"""
    rpm = max(1, int(step.get("rpm", 60)))
    pulses = max(1, int(step.get("pulses", 1)))
    return int(pulses / (rpm * PULSES_PER_REV / 60.0) * 1000) + SETTLE_MS


def estimate_ms(route: dict) -> int:
    return sum(step_ms(s) for s in route.get("steps") or [])


def validate(route: dict) -> list:
    problems = []
    if not (route.get("steps") or []):
        problems.append("还没有记录（上电后先动一下，或者先跑一遍抓取）")
    if len(route.get("steps") or []) > MAX_STEPS:
        problems.append("段数太多")
    return problems
