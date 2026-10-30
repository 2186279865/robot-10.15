"""类别 → 盒子序号 的动态登记（"第一件见到某类，就占一个空盒"）。

比赛现场不给实物、也没时间现场学习，所以不能靠赛前录样本。好在计分规则是
"同形同色同盒"，并不规定哪个类别必须进哪个盒 —— 于是可以：

    第一件"五棱柱|蓝"出现 → 分配 1 号盒
    第二件同类         → 还是 1 号盒
    新类别            → 2 号盒
    ……最多 6 类，正好 6 个盒子

映射落盘在 `runtime/box_map.json`，掉电不丢；也可以被 `vision_grasp.json`
里的 `tray.class_to_box` 手动覆盖（手动优先）。

只依赖标准库，方便单测。
"""
import json
import time
from pathlib import Path
from typing import Dict, Optional

PKG_ROOT = Path(__file__).resolve().parent.parent
MAX_BOXES = 6


def _default_path() -> Path:
    import os

    override = os.environ.get("ROBOT_BOX_MAP")
    if override:
        return Path(override)
    return PKG_ROOT / "runtime" / "box_map.json"


DEFAULT_PATH = _default_path()


def _clean(data) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not isinstance(data, dict):
        return out
    raw = data.get("map")
    if not isinstance(raw, dict):
        raw = data
    for key, value in raw.items():
        try:
            box = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= box <= MAX_BOXES:
            out[str(key)] = box
    return out


def load(path=None) -> Dict[str, int]:
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        return {}
    try:
        return _clean(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def save(mapping: Dict[str, int], path=None) -> Path:
    p = Path(path or DEFAULT_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "map": {str(k): int(v) for k, v in mapping.items()},
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def lookup(key: str, path=None) -> Optional[int]:
    return load(path).get(str(key))


def next_free(mapping: Dict[str, int]) -> Optional[int]:
    used = set(mapping.values())
    for box in range(1, MAX_BOXES + 1):
        if box not in used:
            return box
    return None


def assign(key: str, path=None, box: Optional[int] = None) -> int:
    """给类别分配盒子（已有就返回原来的）。6 个盒子都占满时抛 ValueError。"""
    key = str(key).strip()
    if not key:
        raise ValueError("类别名为空，无法分配盒子")
    p = Path(path or DEFAULT_PATH)
    mapping = load(p)
    if key in mapping:
        return mapping[key]
    if box is not None:
        box = int(box)
        if not 1 <= box <= MAX_BOXES:
            raise ValueError(f"盒子序号越界：{box}")
    else:
        box = next_free(mapping)
        if box is None:
            raise ValueError("6 个盒子都已分配给其它类别，无法再分配新类别")
    mapping[key] = box
    save(mapping, p)
    return box


def reset(path=None) -> None:
    save({}, path)


def auto_reset(path=None, log=None) -> str:
    """每批开始前自动清零登记（"一键启动 / 连续抓"入口调它）。

    **为什么必须自动清**：`box_map.json` 是掉电不丢的长期文件，上一批/上一天的
    登记会一直留着。6 个盒子各占一类，旧类别白占盒子 → 新一批第 7 件就报
    「6 个盒子都已分配给其它类别」直接停机（2026-10-31 现场踩了两次）。
    每批重新分盒本来就等价（计分只看"同形同色同盒"），所以开批即清零最省事。

    返回清零前的描述（写进日志留痕，方便回看上一批是怎么分的）。
    """
    before = describe(path)
    reset(path)
    if callable(log):
        try:
            log('分盒已自动清零（每批重新分配盒子）：%s → %s' % (before, describe(path)))
        except Exception:                            # noqa: BLE001 —— 日志失败不影响清零
            pass
    return before


def describe(path=None) -> str:
    mapping = load(path)
    if not mapping:
        return "0 类（还没开始分拣）"
    parts = [f"盒{box}={key}" for key, box in sorted(mapping.items(), key=lambda kv: kv[1])]
    return f"{len(mapping)} 类｜" + " ".join(parts)
