"""只读验证：笔记本 -> SocketLink(tcp://PI:5058) -> 派端串口桥 -> 真实 STM32。

只发"问"、不发"动"：
  hello（握手）、status、motor_query(vel/pos/flag)、param_get
绝不调用 motor_speed / motor_pos / motor_zero / enable / stop_all / task_*。

用法：
  python .survey\check_bridge_real.py 172.20.240.14 [5058]
"""
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "10.21"
sys.path.insert(0, str(PKG / "host"))
sys.path.insert(0, str(PKG / "common"))

import protocol as P            # noqa: E402
import host_client as HC        # noqa: E402
import serial_link as SL        # noqa: E402


def pump(hc, link, seconds):
    end = time.monotonic() + seconds
    got = 0
    while time.monotonic() < end:
        try:
            data = link.read()
        except Exception as exc:
            print(f"  ! read error: {exc}")
            time.sleep(0.05)
            continue
        if data:
            got += len(data)
            hc.feed_bytes(data)
        else:
            time.sleep(0.01)
    return got


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "172.20.240.14"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5058
    url = f"tcp://{host}:{port}"
    print(f"=== 目标 {url} ===")

    link = SL.open_best_link(url, 115200)
    print(f"链路: {link.describe()}")
    hc = HC.HostClient(link=link)
    if not hc.connect():
        print("!! 打不开链路")
        return 2
    t0 = time.monotonic()
    pump(hc, link, 1.6)                      # 等 hello 应答
    print(f"握手耗时 {time.monotonic() - t0:.2f}s  状态={hc.state}")
    print(f"state={hc.state} online={hc.online} fw={hc.fw_version!r} "
          f"boot={hc.boot_id!r} axes={hc.axis_list} readback={hc.readback_axes}")
    for ev in hc.poll_events():
        print(f"  [event/{ev.kind}] {ev.text}")

    print("\n--- status ---")
    hc.send("status")
    pump(hc, link, 0.8)
    for ev in hc.poll_events():
        print(f"  [event/{ev.kind}] {ev.text} {ev.data if ev.data else ''}")

    print("\n--- motor_query（只读问答，每轴 0.5s 间隔）---")
    for axis in (1, 3, 4, 5):
        for what in ("vel", "pos", "flag"):
            hc.send("motor_query", axis, what, timeout_ms=900)
            pump(hc, link, 0.5)
            err = hc.last_err
            ok = hc.last_ok
            if err:
                print(f"  axis {axis} {what}: ERR {err['code']} {err.get('text', '')}")
                hc.last_err = None
            elif ok:
                counts = hc.last_counts.get(axis)
                print(f"  axis {axis} {what}: ok  counts={counts}")
                hc.last_ok = None
            else:
                print(f"  axis {axis} {what}: 无应答（超时）")

    print("\n--- param_get 抽查 ---")
    for name in ("speed_rpm", "accel", "readback_mask"):
        hc.send("param_get", name, timeout_ms=900)
        pump(hc, link, 0.5)
        for ev in hc.poll_events():
            print(f"  {name}: [event/{ev.kind}] {ev.text} {ev.data if ev.data else ''}")

    print("\n--- 收尾（不发停止/运动命令，直接关链路）---")
    hc.disconnect("只读检查结束")
    print(f"rx={hc.rx_count} tx={hc.tx_count} bad={hc.bad_frame_count} oversize={hc.oversize_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
