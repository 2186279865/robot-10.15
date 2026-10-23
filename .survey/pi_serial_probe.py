"""Probe the serial port to see whether a Raspberry Pi console answers.

Read-only in spirit: sends only a newline (and optionally a login user name
if you pass --user), prints exactly what comes back.

Usage:
    python .survey/pi_serial_probe.py --port COM7 --baud 115200
    python .survey/pi_serial_probe.py --port COM7 --user pi --wait 6
"""
from __future__ import annotations

import argparse
import sys
import time


def read_for(ser, seconds: float, echo: bool = True) -> bytes:
    buf = bytearray()
    end = time.time() + seconds
    while time.time() < end:
        n = ser.in_waiting
        chunk = ser.read(n if n else 1)
        if chunk:
            buf += chunk
            if echo:
                sys.stdout.write(chunk.decode('utf-8', 'replace'))
                sys.stdout.flush()
    return bytes(buf)


def main() -> int:
    import serial  # pyserial

    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='COM7')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--wait', type=float, default=4.0)
    ap.add_argument('--user', default=None, help='send this login name if a login prompt appears')
    ap.add_argument('--password', default=None, help='send this password after the user name')
    ap.add_argument('--cmd', default=None, help='shell command to run if a prompt is already present')
    args = ap.parse_args()

    print('--- open %s @ %d 8N1 ---' % (args.port, args.baud))
    try:
        ser = serial.Serial(port=args.port, baudrate=args.baud, bytesize=8,
                            parity='N', stopbits=1, timeout=0.2,
                            rtscts=False, dsrdtr=False)
    except Exception as exc:                       # noqa: BLE001
        print('OPEN FAILED: %r' % (exc,))
        return 2

    try:
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b'\r')
        ser.flush()
        time.sleep(0.4)
        data = read_for(ser, args.wait)

        text = data.decode('utf-8', 'replace')
        print('\n--- %d bytes received ---' % len(data))
        print('repr:', repr(data[:400]))

        low = text.lower()
        if 'login:' in low and args.user:
            print('\n--- sending login ---')
            ser.write((args.user + '\r').encode())
            ser.flush()
            read_for(ser, 2.0)
            if args.password:
                ser.write((args.password + '\r').encode())
                ser.flush()
                read_for(ser, 3.0)
                if args.cmd:
                    ser.write((args.cmd + '\r').encode())
                    ser.flush()
                    read_for(ser, 4.0)
        elif args.cmd and ('$' in text or '#' in text or '>' in text):
            print('\n--- prompt detected, sending command ---')
            ser.write((args.cmd + '\r').encode())
            ser.flush()
            read_for(ser, 5.0)
        return 0
    finally:
        ser.close()
        print('\n--- closed ---')


if __name__ == '__main__':
    sys.exit(main())
