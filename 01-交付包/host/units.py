# -*- coding: utf-8 -*-
"""单位换算的唯一入口。

对外（界面 / 文件 / 示教 / 限位 / 标定 / 序列）**一律用「脉冲」**。
编码器回读（0x36、遥测 counts）是另一种刻度，只允许在这里换算：

    1 圈 = 3200 脉冲 = 65536 counts   →   1 脉冲 = 20.48 counts

想加新的换算，只能加在这个文件里；其它地方禁止再写 20.48 / 65536 之类的裸数字。
"""
from __future__ import annotations

PULSES_PER_REV = 3200          # EMM V5：16 细分 × 200 步
COUNTS_PER_REV = 65536         # 0x36 回包：16 位角度 + 32 位多圈
COUNTS_PER_PULSE = COUNTS_PER_REV / float(PULSES_PER_REV)   # 20.48


def counts_to_pulses(counts: float) -> float:
    """编码器 counts → 脉冲（浮点，保留小数）。"""
    return float(counts) / COUNTS_PER_PULSE


def pulses_to_counts(pulses: float) -> int:
    """脉冲 → 编码器 counts（四舍五入成整数，固件侧就是这么比的）。"""
    return int(round(float(pulses) * COUNTS_PER_PULSE))


def revs(pulses: float) -> float:
    """脉冲 → 圈数。"""
    return float(pulses) / float(PULSES_PER_REV)


def fmt_pulses(pulses) -> str:
    """脉冲的紧凑写法：带符号整数 + 圈数。"""
    try:
        p = float(pulses)
    except (TypeError, ValueError):
        return str(pulses)
    return f"{p:+.0f} 脉冲（{revs(p):+.2f} 圈）"


def fmt_readback(counts, rpm=None, age_ms=None, stale: bool = False) -> str:
    """四轴页那行"回读"：主单位脉冲，括号里给 counts 与圈数。"""
    try:
        c = float(counts)
    except (TypeError, ValueError):
        return f"回读：{counts}"
    p = counts_to_pulses(c)
    deg = c * 360.0 / float(COUNTS_PER_REV)
    text = f"回读：{p:+.0f} 脉冲（{c:+.0f} counts，{deg:+.1f}°）"
    if rpm is not None:
        text += f" {rpm} RPM"
    if age_ms is not None:
        text += f" {float(age_ms) / 1000.0:.1f}s 前"
    if stale:
        text += " ⚠ 数据偏旧"
    return text


def check_unit(data, expected: str = "pulse", name: str = "数据") -> str:
    """校验文件里的 unit 字段。

    返回实际单位（'pulse' / 'counts' / 'unknown'）。
    - 没有 unit 字段：按 pulse 处理（老文件兼容），返回 'pulse'；
    - unit == 'counts'：返回 'counts'，调用方负责换算并记日志；
    - 其它值：抛 ValueError（宁可报错，不要静默用错单位）。
    """
    if not isinstance(data, dict):
        return expected
    unit = str(data.get("unit", expected)).strip().lower()
    if unit in ("pulse", "pulses", "脉冲"):
        return "pulse"
    if unit in ("count", "counts", "编码器计数"):
        return "counts"
    raise ValueError(f"{name} 的 unit 字段是 {unit!r}，只认 'pulse' 或 'counts'")
