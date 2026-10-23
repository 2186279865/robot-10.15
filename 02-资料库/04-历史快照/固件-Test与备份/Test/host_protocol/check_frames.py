"""用上位机自己的 protocol.py 交叉验证固件真实发出的帧。

frames.txt 由 Test/host_protocol/test_host_protocol.exe 写出：里面的每一行都是
固件 HostLink_QueueTx 实际发出去的一整帧（含行尾 LF）。这个脚本只做一件事——
让上位机的解析器把它们逐条解出来。任何一条解析失败、校验和不符、错误码不在
E01~E13、或者帧长超过 96 字节，都算失败。

这样两端就不是"各自测各自的"：C 侧的编码必须能被 Python 侧真实解析。
"""

from __future__ import annotations

import collections
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "上位机"))

import protocol as P  # noqa: E402  （上位机的协议模块）


def main() -> int:
    path = HERE / "frames.txt"
    if not path.exists():
        print("缺少 frames.txt：请先运行 test_host_protocol.exe")
        return 1

    lines = [line for line in path.read_text(encoding="ascii").split("\n") if line]
    kinds: collections.Counter = collections.Counter()
    errs: collections.Counter = collections.Counter()
    bad = []

    for line in lines:
        try:
            frame = P.parse_line(line)
        except P.ProtocolError as exc:
            bad.append((line, exc))
            continue
        kinds[frame.kind] += 1
        if frame.kind == "err":
            errs[frame.error_code] += 1

    print(f"frames.txt：{len(lines)} 帧")
    print(f"  类型：{dict(kinds)}")
    print(f"  错误码：{dict(errs)}")

    if bad:
        for line, exc in bad[:10]:
            print(f"  解析失败：{line!r} -> {exc}")
        print(f"失败：{len(bad)} 帧无法被上位机解析")
        return 1

    if len(lines) < 20:
        print("失败：帧数过少，C 侧测试可能没有真正跑起来")
        return 1

    # 这几类帧必须出现过，否则说明用例覆盖漏了
    for kind in ("ok", "err", "event", "tele"):
        if kinds.get(kind, 0) == 0:
            print(f"失败：没有任何 {kind} 帧")
            return 1

    print("OK：固件发出的每一帧都能被 protocol.py 解析")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
