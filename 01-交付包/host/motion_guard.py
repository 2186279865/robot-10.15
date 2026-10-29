"""运动安全护栏（2026-10-29 事故后加）：软限位、基准一致性、静止判定。

背景：位置清零在轴还在运动时被“部分执行”（部分轴成功、部分轴 E09 被拒），
导致 counts 零点与软位置/脉冲目标空间错位；此后按 counts 算增量会得到
荒唐的步长（例：Z 目标 -152000，回读 +153612 脉冲 → 步长 -305612）。
本次事故靠“单步 >200000 拒绝”侥幸拦住，否则就是直冲撞机。
"""
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

COUNTS_PER_PULSE = 20.48
LIMITS_PATH = Path(__file__).with_name('limits.json')
#: limits.json 缺失时的兜底（与现网一致）
DEFAULT_LIMITS = {1: (-152501, 500), 3: (-4000, 4000), 4: (-500, 12000), 5: (-55000, 500)}


def load_limits(path=None) -> Dict[int, Tuple[int, int]]:
    p = Path(path) if path else LIMITS_PATH
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
    except Exception:                                   # noqa: BLE001
        return dict(DEFAULT_LIMITS)
    out: Dict[int, Tuple[int, int]] = {}
    for key, val in raw.items():
        if not str(key).isdigit():
            continue
        try:
            out[int(key)] = (int(val['min']), int(val['max']))
        except Exception:                               # noqa: BLE001
            continue
    return out or dict(DEFAULT_LIMITS)


def check_absolute(limits: Dict[int, Tuple[int, int]], axis: int, pulses: float) -> Tuple[bool, str]:
    """绝对目标是否在该轴软限位内。"""
    lo, hi = limits.get(int(axis), (-10 ** 9, 10 ** 9))
    if pulses < lo or pulses > hi:
        return False, '目标 %+.0f 脉冲超出软限位 [%+d, %+d]' % (pulses, lo, hi)
    return True, ''


def counts_to_pulses(counts: float) -> float:
    return float(counts) / COUNTS_PER_PULSE


def consistency(soft: Optional[Dict[int, float]], counts: Optional[Dict[int, float]],
                axes: Iterable[int] = (1, 3, 4, 5), tol_pulses: float = 500.0) -> List[Tuple[int, float, float, float]]:
    """软位置 vs 编码器回读是否一致（都换算成脉冲）。

    返回 [(axis, soft_pulses, counts_pulses, 差值)]；空列表=一致。
    """
    bad = []
    if not soft or not counts:
        return bad
    for a in axes:
        s = soft.get(a)
        c = counts.get(a)
        if s is None or c is None:
            continue
        cp = counts_to_pulses(c)
        diff = cp - float(s)
        if abs(diff) > float(tol_pulses):
            bad.append((int(a), float(s), cp, diff))
    return bad


def describe_consistency(bad) -> str:
    names = {1: 'Z', 3: '托盘', 4: 'X', 5: 'Y'}
    parts = []
    for axis, s, cp, diff in bad:
        parts.append('%s 软位置%+d 但回读%+d（差%+d 脉冲）' % (names.get(axis, axis), int(s), int(cp), int(diff)))
    return '；'.join(parts)
