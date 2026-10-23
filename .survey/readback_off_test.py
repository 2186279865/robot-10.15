"""诊断：STM32 是否在"每 1~2 秒自动重启"（boot id 递增 + 自发 hello）。

做法：只发只读/参数命令，统计 6 秒内收到的自发 hello 次数；然后把
readback_mask 设成 0 再统计一次（验证"PA3 悬空回读导致复位"的假设）。

不动电机、不动夹爪。
"""
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "10.21"
for _p in (PKG / "host", PKG / "common", PKG / "vision", PKG):
    sys.path.insert(0, str(_p))

import host_client as HC      # noqa: E402
import serial_link as SL      # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "172.20.240.14"


def pump(hc, link, seconds):
    boots, events = [], []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data = link.read()
        except Exception:
            time.sleep(0.05)
            continue
        if data:
            hc.feed_bytes(data)
        else:
            time.sleep(0.01)
        for ev in hc.poll_events():
            events.append(ev)
            txt = ev.text or ""
            if "boot=" in txt:
                boots.append(txt.split("boot=")[1][:8])
    return boots, events


def main():
    link = SL.open_best_link(f"tcp://{HOST}:5058", 115200)
    hc = HC.HostClient(link=link)
    hc.connect()
    boots, _ = pump(hc, link, 2.0)
    print(f"握手后 boot={hc.boot_id} 状态={hc.state}")

    print("\n--- 基线：6 秒内自发 hello 次数 ---")
    base_boots, _ = pump(hc, link, 6.0)
    print(f"收到 {len(base_boots)} 个 boot 标识：{base_boots}")
    unique_base = len(set(base_boots))
    print(f"其中不同 boot 值 {unique_base} 个 → {'**疑似在反复重启**' if unique_base > 1 else '没有重启'}")

    print("\n--- param_get readback_mask ---")
    hc.send("param_get", "readback_mask", timeout_ms=1200)
    pump(hc, link, 1.0)
    for ev in hc.poll_events():
        if ev.kind in ("reply", "err"):
            print(f"  [{ev.kind}] {ev.text}")

    print("\n--- param_set readback_mask=0（关掉四轴回读）---")
    hc.send("param_set", "readback_mask", 0, timeout_ms=1500)
    pump(hc, link, 1.5)
    for ev in hc.poll_events():
        if ev.kind in ("reply", "err"):
            print(f"  [{ev.kind}] {ev.text} {ev.data if ev.data else ''}")

    print("\n--- 关回读后：8 秒内自发 hello 次数 ---")
    after_boots, _ = pump(hc, link, 8.0)
    print(f"收到 {len(after_boots)} 个 boot 标识：{after_boots}")
    unique_after = len(set(after_boots))
    print(f"其中不同 boot 值 {unique_after} 个 → {'**仍然在重启**' if unique_after > 1 else '重启已停止'}")

    hc.disconnect("诊断结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
