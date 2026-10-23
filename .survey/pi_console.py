#!/usr/bin/env python3
"""通过 USB-TTL 串口往香橙派控制台按顺序发命令，并把回显打到 stdout。

用法:
  python .survey/pi_console.py "cmd1" "cmd2" ...
  python .survey/pi_console.py --port COM8 --baud 115200 --wait 2.5 "cmd"
"""
from __future__ import annotations

import argparse
import sys
import time

import serial


def drain(ser: serial.Serial, quiet: float = 0.35, limit: float = 10.0) -> str:
    """读到安静 quiet 秒或累计 limit 秒为止。"""
    buf = bytearray()
    t0 = time.monotonic()
    last = time.monotonic()
    while True:
        chunk = ser.read(4096)
        now = time.monotonic()
        if chunk:
            buf.extend(chunk)
            last = now
        elif now - last >= quiet:
            break
        if now - t0 >= limit:
            break
    return buf.decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM8")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--wait", type=float, default=1.2, help="每条命令后最多等多久")
    ap.add_argument("cmds", nargs="*", help="依次执行的命令")
    args = ap.parse_args()

    with serial.Serial(args.port, args.baud, timeout=0.1, write_timeout=2) as ser:
        ser.dtr = True
        ser.rts = True
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b"\r")
        print("=== 唤醒 ===")
        print(drain(ser, quiet=0.5, limit=4.0))

        for cmd in args.cmds:
            print(f"=== $ {cmd} ===")
            ser.write(cmd.encode("utf-8") + b"\r")
            print(drain(ser, quiet=0.5, limit=max(args.wait, 1.0)))
        return 0


if __name__ == "__main__":
    sys.exit(main())
