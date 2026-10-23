"""一次性验收：移动记录（journey）→ 一键原路返回。只用于本次验收，不属于交付物。

用法： python .survey\journey_smoke.py
"""
import argparse
import pathlib
import sys
import tempfile
import time
import tkinter as tk
from unittest import mock

PKG = pathlib.Path(__file__).resolve().parent.parent / '10.21'
for _p in (PKG / 'vision', PKG / 'host', PKG / 'common', PKG):
    sys.path.insert(0, str(_p))

import approach_route as AR           # noqa: E402
import journey as J                   # noqa: E402
import host_gui                       # noqa: E402

TMP = pathlib.Path(tempfile.mkdtemp(prefix='journey_smoke_'))
AR.DEFAULT_PATH = TMP / 'approach_route.json'
J.DEFAULT_PATH = TMP / 'journey.json'

ns = argparse.Namespace(pi=None, pi_img_port=None, pi_ser_port=None,
                        port='', sim=True, autoconnect=True,
                        no_vision=True, selftest=False)

root = tk.Tk()
app = host_gui.App(root, ns)
app.approach = AR.empty()
app.journey = J.empty()
app.approach_refresh()
app.journey_refresh()


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def wait_until(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if pred():
            return True
        time.sleep(0.02)
    return False


fails = []
sent = []


def check(name, ok, detail=''):
    print(('[ OK ] ' if ok else '[FAIL] ') + name + (f'  {detail}' if detail else ''))
    if not ok:
        fails.append(name)


pump(0.3)
check('模拟后端连上', wait_until(lambda: app.client.online, 10.0))

real_send = app.client.send


def spy(cmd, *args, **kwargs):
    sent.append((cmd, args))
    return real_send(cmd, *args, **kwargs)


app.client.send = spy

# --- 1) 自动记录：相对走 X / 绝对 Y / 托盘 / 速度模式 ---
app.client.send('motor_enable', 4, 1)
app.client.send('motor_pos', 4, 0, 60, 1, 3200, 'rel')
app.client.send('motor_pos', 5, 1, 60, 1, 1600, 'rel')
app.client.send('motor_pos', 3, 0, 30, 1, 533, 'rel')
pump(0.4)
steps = app.journey['steps']
check('四轴都记（含托盘）3 段', len(steps) == 3, f'{[(s["axis"], s["pulses"]) for s in steps]}')
check('状态行显示净位移', 'X +3200' in app.journey_status.cget('text'),
      app.journey_status.cget('text'))
check('已落盘', J.DEFAULT_PATH.exists())

app.client.send('motor_speed', 1, 1, 60, 1)
time.sleep(0.35)
pump(0.05)
app.client.send('motor_stop', 1)
pump(0.4)
steps = app.journey['steps']
check('常用走停下后也记一段（Z 负向）',
      len(steps) == 4 and steps[3]['axis'] == 1 and steps[3]['dir'] == 1,
      f'{[(s["axis"], s["dir"], s["pulses"]) for s in steps]}')

# --- 2) 一键原路返回 ---
before = list(app.journey['steps'])
expect = [(s['axis'], 1 - s['dir'], s['pulses']) for s in reversed(before)]
sent.clear()
with mock.patch.object(host_gui.messagebox, 'askyesno', return_value=True):
    app.journey_return()
check('进入返回态', app._journey_running)
check('返回跑完', wait_until(lambda: not app._journey_running, 25.0))
pump(0.4)

moves = [(a[0], a[1], a[4]) for c, a in sent if c == 'motor_pos']
check('倒序 + 方向取反', moves == expect, f'{moves}')
check('每段前先使能', len([1 for c, a in sent if c == 'motor_enable']) == len(expect))
check('回到起点后记录清空', not (app.journey.get('steps') or []) and '还没动过' in app.journey_status.cget('text'))

# --- 3) 急停能打断返回 ---
app.client.send('motor_pos', 4, 0, 60, 1, 3200, 'rel')
pump(0.3)
sent.clear()
with mock.patch.object(host_gui.messagebox, 'askyesno', return_value=True):
    app.journey_return()
pump(0.05)
app.emergency_stop()
pump(0.3)
check('急停清空返回队列', (not app._journey_running) and not app._journey_queue)

app.client.send = real_send
root.update()
root.destroy()
print('RESULT:', 'ALL PASSED' if not fails else f'FAILED {fails}')
sys.exit(0 if not fails else 1)
