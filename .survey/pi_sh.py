"""Run shell commands on the Orange Pi through the USB-TTL serial console.

The console is already logged in (user `orangepi`), so this tool only needs to
write a command, append an end marker, and read until the marker comes back.

Usage:
    python .survey/pi_sh.py "uname -a" "lsblk" --wait 20
    python .survey/pi_sh.py --sudo "apt-get update"          # uses password from env PI_PASS
    python .survey/pi_sh.py --file cmds.txt

Notes
- Output keeps the console's ANSI colour/paste escapes; they are stripped.
- If a command needs sudo, pass --sudo: it is wrapped with
  `echo "$PI_PASS" | sudo -S -p '' sh -c '...'`.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

PORT = os.environ.get('PI_PORT', 'COM7')
BAUD = int(os.environ.get('PI_BAUD', '115200'))
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07')


def clean(text: str) -> str:
    return ANSI.sub('', text).replace('\r', '')


def read_until(ser, marker: bytes, timeout: float) -> bytes:
    buf = bytearray()
    end = time.time() + timeout
    while time.time() < end:
        n = ser.in_waiting
        chunk = ser.read(n if n else 1)
        if chunk:
            buf += chunk
            if marker in bytes(buf):
                break
    return bytes(buf)


def main() -> int:
    import serial

    ap = argparse.ArgumentParser()
    ap.add_argument('commands', nargs='*')
    ap.add_argument('--file', default=None)
    ap.add_argument('--sudo', action='store_true', help='wrap every command in sudo')
    ap.add_argument('--wait', type=float, default=30.0, help='per-command timeout (s)')
    ap.add_argument('--port', default=PORT)
    ap.add_argument('--baud', type=int, default=BAUD)
    args = ap.parse_args()

    cmds = list(args.commands)
    if args.file:
        with open(args.file, 'r', encoding='utf-8') as fh:
            cmds += [ln.rstrip('\n') for ln in fh if ln.strip() and not ln.startswith('#')]
    if not cmds:
        print('nothing to run', file=sys.stderr)
        return 2

    password = os.environ.get('PI_PASS', '')

    ser = serial.Serial(port=args.port, baudrate=args.baud, bytesize=8, parity='N',
                        stopbits=1, timeout=0.2, rtscts=False, dsrdtr=False)
    rc = 0
    try:
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b'\r')
        ser.flush()
        read_until(ser, b'$ ', 5.0)              # wait for a clean prompt

        for i, cmd in enumerate(cmds, 1):
            if args.sudo:
                inner = cmd.replace('\\', '\\\\').replace('"', '\\"').replace("'", "'\\''")
                line = 'echo "%s" | sudo -S -p "" sh -c \'%s\'' % (password, cmd.replace("'", "'\\''"))
                del inner
            else:
                line = cmd
            marker = '__PI_SH_DONE_%d__' % i
            full = '%s; echo %s:$?' % (line, marker)
            ser.write((full + '\r').encode('utf-8', 'replace'))
            ser.flush()
            out = read_until(ser, marker.encode(), args.wait)
            text = clean(out.decode('utf-8', 'replace'))
            # drop the echoed command line and the marker tail
            lines = text.split('\n')
            body = []
            for ln in lines:
                s = ln.strip()
                if s.startswith(cmd[:40].strip()):
                    continue
                if marker in s:
                    tail = s.split(marker + ':')[-1].strip()
                    if tail and tail != '0':
                        rc = 1
                    continue
                body.append(ln)
            print('$ %s' % cmd)
            print('\n'.join(x.rstrip() for x in body if x.strip() != ''))
            print()
    finally:
        ser.close()
    return rc


if __name__ == '__main__':
    sys.exit(main())
