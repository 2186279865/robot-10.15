"""真机动作通道验证：只动舵机夹爪（0°/180°），不动四轴。

顺序：hello → status → stop_clear（解除停止锁存）→ 松开 → 夹紧 → 松开
每步抓一张顶视图存进 10.21/runtime/，用来肉眼确认爪子真的动了。
"""
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "10.21"
for _p in (PKG / "host", PKG / "common", PKG / "vision", PKG):
    sys.path.insert(0, str(_p))

import cv2                                   # noqa: E402
import numpy as np                           # noqa: E402
import host_client as HC                     # noqa: E402
import serial_link as SL                     # noqa: E402
import wire                                  # noqa: E402

HOST = sys.argv[1] if len(sys.argv) > 1 else "172.20.240.14"


def pump(hc, link, seconds):
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


def snap(name, cam_slot=0):
    s = wire.open_tcp(HOST, wire.IMG_PORT, 3.0)
    try:
        s.sendall(wire.pack_request(cam_slot, 0, 0, 85))
        head = wire.recv_exact(s, 2)
        if head != b"IM":
            print("  抓图失败：", wire.parse_error_header(head + wire.recv_exact(s, wire.ERR_HDR_LEN - 2)))
            return
        hdr = wire.parse_image_header(head + wire.recv_exact(s, wire.IMG_HDR_LEN - 2))
        body = wire.recv_exact(s, hdr["n"])
    finally:
        s.close()
    img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
    out = PKG / "runtime" / f"{name}.jpg"
    cv2.imwrite(str(out), img)
    print(f"  截图 {out.name} {hdr['width']}x{hdr['height']}")


def show(hc, label):
    for ev in hc.poll_events():
        if ev.kind in ("reply", "err", "note", "state"):
            print(f"  [{ev.kind}] {ev.text}")
    print(f"  -- {label} --")


def main():
    url = f"tcp://{HOST}:5058"
    link = SL.open_best_link(url, 115200)
    hc = HC.HostClient(link=link)
    if not hc.connect():
        print("打不开链路")
        return 2
    pump(hc, link, 1.8)
    print(f"握手：state={hc.state} fw={hc.fw_version} boot={hc.boot_id} "
          f"axes={hc.axis_list} readback={hc.readback_axes}")
    hc.poll_events()

    print("\n1) status")
    hc.send("status")
    pump(hc, link, 0.8)
    show(hc, "status")

    print("\n2) stop_clear（解除停止锁存）")
    hc.send("stop_clear")
    pump(hc, link, 0.8)
    show(hc, "stop_clear")

    print("\n3) gripper open（舵机 → 松开位）")
    hc.send("gripper", "open")
    pump(hc, link, 2.0)
    show(hc, "gripper open")
    snap("real_new_gripper_open", 1)

    print("\n4) gripper grab（舵机 → 夹紧位）")
    hc.send("gripper", "grab")
    pump(hc, link, 2.0)
    show(hc, "gripper grab")
    snap("real_new_gripper_grab", 1)

    print("\n5) gripper open（复原）")
    hc.send("gripper", "open")
    pump(hc, link, 2.0)
    show(hc, "gripper open (restore)")

    print("\n6) 收尾 status")
    hc.send("status")
    pump(hc, link, 0.8)
    show(hc, "status 最终")

    hc.disconnect("动作验证结束")
    print(f"rx={hc.rx_count} tx={hc.tx_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
