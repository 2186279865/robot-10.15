#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pi_ctl.py —— 派端“遥控”服务（给上位机的「重启派端」按钮用）。

只做几件运维操作，不做运动控制：

    ping             → 回 "ok pi_ctl <uptime>s"
    restart_display  → 重启屏幕程序（pi_display.py，走 start_display.sh）
    restart_agent    → 重启图传/串口桥服务（systemctl restart robotpi）
    restart_all      → 先显示、后服务
    reboot           → 整机重启

安全策略：
  * 默认只接受私有网段来源（192.168/10/172.16-31），公网地址直接拒；
  * 每次只收一行纯文本指令，最多 64 字节，2 s 超时；
  * 所有动作都写日志到 /tmp/pi_ctl.log。

依赖：只用标准库。
运行（派端，autostart 里已经拉起）::

    python3 pi_ctl.py --port 5062
"""

import argparse
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
START_DISPLAY = os.path.join(BASE, 'start_display.sh')
LOG_PATH = '/tmp/pi_ctl.log'

log = logging.getLogger('pi_ctl')

PRIVATE_RE = re.compile(r'^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|127\.)')


def _is_private(addr):
    return bool(addr) and bool(PRIVATE_RE.match(addr))


def _spawn(argv, env_extra=None, new_session=True):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env,
                            start_new_session=new_session)


def _delayed(fn, delay=0.4):
    def run():
        time.sleep(delay)
        try:
            fn()
        except Exception:                              # noqa: BLE001
            log.exception('delayed action failed')
    threading.Thread(target=run, daemon=True).start()


def _restart_display():
    log.info('restart display')
    _spawn([START_DISPLAY], {'DISPLAY': ':0'})


def _restart_agent():
    log.info('restart agent (systemctl restart robotpi)')
    _spawn(['sudo', '-n', 'systemctl', 'restart', 'robotpi'])


def _reboot():
    log.info('reboot')
    _spawn(['sudo', '-n', 'systemctl', 'reboot'])


HANDLERS = {
    'ping': lambda: 'ok pi_ctl %.0fs' % (time.monotonic() - STARTED),
    'restart_display': lambda: (_delayed(_restart_display), 'ok restart_display')[1],
    'restart_agent': lambda: (_delayed(_restart_agent), 'ok restart_agent')[1],
    'restart_all': lambda: (_delayed(_restart_display, 0.2),
                            _delayed(_restart_agent, 1.2), 'ok restart_all')[2],
    'reboot': lambda: (_delayed(_reboot), 'ok reboot')[1],
}

STARTED = time.monotonic()


def handle(conn, addr):
    try:
        conn.settimeout(2.0)
        raw = b''
        while len(raw) < 64:
            chunk = conn.recv(64 - len(raw))
            if not chunk:
                break
            raw += chunk
            if b'\n' in chunk:
                break
        cmd = raw.decode('utf-8', 'replace').strip().lower()
        log.info('client %s cmd=%r', addr[0], cmd)
        if not _is_private(addr[0]):
            conn.sendall(b'err not-private\n')
            return
        fn = HANDLERS.get(cmd)
        if fn is None:
            conn.sendall(b'err unknown-cmd\n')
            return
        conn.sendall((fn() + '\n').encode('utf-8'))
    except Exception as exc:                           # noqa: BLE001
        log.warning('handle error: %s', exc)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(port, host='0.0.0.0'):
    global STARTED
    STARTED = time.monotonic()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    log.info('pi_ctl listening on %s:%d', host, port)
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser(description='派端遥控服务（重启屏幕/服务）')
    ap.add_argument('--port', type=int, default=5062)
    ap.add_argument('--host', default='0.0.0.0')
    args = ap.parse_args(argv)

    logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        serve(args.port, args.host)
    except KeyboardInterrupt:
        pass
    except Exception as exc:                           # noqa: BLE001
        log.exception('fatal: %s', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
