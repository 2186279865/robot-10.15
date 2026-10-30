#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pi_ctl.py（上位机侧）—— 给「重启派端」按钮用的极简客户端。

用法（命令行也行，方便现场排查）::

    python pi_ctl.py ping            192.168.100.198
    python pi_ctl.py restart_display 192.168.100.198
    python pi_ctl.py restart_agent   192.168.100.198
    python pi_ctl.py reboot          192.168.100.198

协议：TCP 5062，一行纯文本指令进，一行文本结果出（2 s 超时）。
"""

import socket
import sys

DEFAULT_PORT = 5062


def send(host, cmd, port=DEFAULT_PORT, timeout=3.0):
    """把一条指令发给派端，返回派端回的那行文本；失败抛 OSError。"""
    with socket.create_connection((host, int(port)), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((cmd.strip() + '\n').encode('utf-8'))
        buf = b''
        while b'\n' not in buf and len(buf) < 256:
            chunk = sock.recv(256)
            if not chunk:
                break
            buf += chunk
    return buf.decode('utf-8', 'replace').strip()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, host = argv[0], argv[1]
    port = int(argv[2]) if len(argv) > 2 else DEFAULT_PORT
    try:
        print(send(host, cmd, port))
    except OSError as exc:
        print('失败：%s' % exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
