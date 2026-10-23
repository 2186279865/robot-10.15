#!/usr/bin/env python
"""Wait until the given TCP ports are open (or --closed: until they are free).

Pure standard library so it works with any interpreter, including a bare
system python without project deps.

Usage::

    python 10.21/tests/wait_ports.py 15057 15058
    python 10.21/tests/wait_ports.py --closed 15057 15058 --timeout 10
"""
from __future__ import annotations

import argparse
import socket
import sys
import time


def port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('ports', nargs='+', type=int)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--timeout', type=float, default=15.0)
    ap.add_argument('--closed', action='store_true',
                    help='wait until the ports are closed instead of open')
    args = ap.parse_args(argv)

    want = not args.closed
    deadline = time.time() + args.timeout
    while True:
        states = [port_open(args.host, p) for p in args.ports]
        if all(s == want for s in states):
            word = 'open' if want else 'closed'
            print('  ports %s %s' % (', '.join(str(p) for p in args.ports), word))
            return 0
        if time.time() > deadline:
            print('  TIMEOUT waiting for ports %s to be %s'
                  % (', '.join(str(p) for p in args.ports),
                     'open' if want else 'closed'), file=sys.stderr)
            return 1
        time.sleep(0.1)


if __name__ == '__main__':
    sys.exit(main())
