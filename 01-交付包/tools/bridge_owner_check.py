#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""验证串口桥的“独占控制 + 只读观察”行为（需要派端 agent 在跑）。

用法:
    .venv\Scripts\python.exe tools\bridge_owner_check.py --host 192.168.100.198

检查 5 件事:
  1) 控制口 5058 能连上并完成 hello（成为控制方）
  2) 第二个控制连接会被派端立刻关闭（独占生效）
  3) 第一个控制方发 ping，观察口 5054 能收到这条回包的广播
  4) 观察口往里写字节会被丢弃，控制方不受影响（再 ping 仍正常）
  5) 控制方断开后，新连接可以接管

注意：如果这时有另一台上位机（或你自己的 GUI）正占着 5058，本脚本第 1 步就会失败——
这正是独占行为，先把那台上位机断开再测。
"""
import argparse
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
for p in (PKG / "host", PKG, PKG / "common"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import serial_link                      # noqa: E402
from host_client import HostClient      # noqa: E402


def open_raw(host, port, timeout=3.0):
    s = socket.create_connection((host, int(port)), timeout=timeout)
    s.settimeout(1.0)
    return s


def drain(sock, seconds=1.5):
    buf = b""
    end = time.time() + seconds
    closed = False
    while time.time() < end:
        try:
            data = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            closed = True
            break
        if not data:
            closed = True
            break
        buf += data
    return buf, closed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ser-port", type=int, default=5058)
    ap.add_argument("--observer-port", type=int, default=5054)
    args = ap.parse_args()

    ok_all = []

    def check(name, ok, detail=""):
        ok_all.append(bool(ok))
        print(("[ OK ] " if ok else "[FAIL] ") + name + (("  " + detail) if detail else ""))

    print("目标 %s：控制口 %d / 观察口 %d\n" % (args.host, args.ser_port, args.observer_port))

    hc = HostClient(serial_link.open_best_link(
        "tcp://%s:%d" % (args.host, args.ser_port), 115200), service=True)
    connected = hc.connect()
    deadline = time.time() + 8
    while time.time() < deadline and not hc.online:
        time.sleep(0.05)
    check("控制口能连上并 hello 成功（成为控制方）", connected and hc.online,
          "online=%s fw=%s" % (hc.online, hc.fw_version))

    try:
        second = open_raw(args.host, args.ser_port)
        buf2, closed2 = drain(second, 1.5)
        check("第二个控制连接被派端关闭（独占生效）", closed2,
              "closed=%s bytes=%d" % (closed2, len(buf2)))
        try:
            second.close()
        except OSError:
            pass
    except OSError as exc:
        check("第二个控制连接被派端关闭（独占生效）", False, "连不上：%s" % exc)

    obs = open_raw(args.host, args.observer_port)
    time.sleep(0.3)
    hc.send("ping")
    buf3, closed3 = drain(obs, 1.5)
    check("观察口能收到串口回包广播", (not closed3) and (b"ok" in buf3),
          "bytes=%d closed=%s" % (len(buf3), closed3))

    try:
        obs.sendall(b"$99,stop_all*00\n")
    except OSError:
        pass
    time.sleep(0.3)
    rid = hc.send("ping")
    seen = False
    t = time.time()
    while time.time() - t < 3:
        d = hc.done_of(rid)
        if d:
            seen = True
            break
        time.sleep(0.03)
    check("观察口写入被丢弃，控制方仍能正常通信", seen and hc.online,
          "reply=%s online=%s" % (seen, hc.online))

    hc.disconnect('owner check')
    time.sleep(0.8)
    try:
        time.sleep(0.8)
        hc2 = HostClient(serial_link.open_best_link(
            "tcp://%s:%d" % (args.host, args.ser_port), 115200), service=True)
        hc2.connect()
        t = time.time()
        while time.time() - t < 6 and not hc2.online:
            time.sleep(0.05)
        check("控制方断开后新连接可接管", hc2.online, "online=%s" % hc2.online)
        hc2.disconnect()
    except OSError as exc:
        check("控制方断开后新连接可接管", False, str(exc))

    for s in (obs,):
        try:
            s.close()
        except OSError:
            pass

    print("\n通过 %d / %d" % (sum(ok_all), len(ok_all)))
    return 0 if all(ok_all) else 1


if __name__ == "__main__":
    sys.exit(main())
