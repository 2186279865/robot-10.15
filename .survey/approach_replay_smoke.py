"""一次性验收：在「模拟后端」上跑通 接近段示教 → 保存 → 序列前自动重放。

只用于本次验收，不属于交付物。
用法： python .survey\approach_replay_smoke.py
"""
import argparse
import sys
import time
import tkinter as tk
from pathlib import Path
from unittest import mock

PKG = Path(__file__).resolve().parent.parent / '10.21'
for _p in (PKG / 'vision', PKG / 'host', PKG / 'common', PKG):
    sys.path.insert(0, str(_p))

import main as M                      # noqa: E402
import approach_route as AR           # noqa: E402
import host_gui                       # noqa: E402

SMOKE_PATH = PKG / 'runtime' / '_approach_smoke.json'
SMOKE_PATH.unlink(missing_ok=True)
AR.DEFAULT_PATH = SMOKE_PATH          # 别污染真实的 runtime/approach_route.json

ns = argparse.Namespace(
    pi=None, pi_img_port=None, pi_ser_port=None,
    port='', sim=True, autoconnect=True,
    no_vision=True, selftest=False,
)

root = tk.Tk()
app = host_gui.App(root, ns)


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.02)


def wait_until(pred, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if pred():
            return True
        time.sleep(0.02)
    return False


fails = []


def check(name, ok, detail=''):
    print(('[ OK ] ' if ok else '[FAIL] ') + name + (f'  {detail}' if detail else ''))
    if not ok:
        fails.append(name)


pump(0.2)
check('模拟后端连上', wait_until(lambda: app.client.online, 8.0))

sent = []
real_send = app.client.send


def spy(cmd, *args, **kwargs):
    sent.append((cmd, args))
    return real_send(cmd, *args, **kwargs)


app.client.send = spy

# --- 1) 示教：三次点动，其中托盘(3) 不应被记录 ---
with mock.patch.object(host_gui.messagebox, 'askyesno', return_value=True):
    app.approach_begin()
check('进入示教态', app._approach_recording)

app.client.send('motor_enable', 4, 1)
app.client.send('motor_pos', 4, 0, 60, 1, 3200, 'rel')
app.client.send('motor_pos', 5, 1, 60, 1, 1600, 'rel')
app.client.send('motor_pos', 3, 0, 30, 1, 533, 'rel')   # 托盘：不该进接近段
pump(0.3)

steps = app.approach.get('steps') or []
check('记录 2 段（托盘被排除）', len(steps) == 2, f'steps={[(s["axis"], s["pulses"]) for s in steps]}')
check('顺序为 X 再 Y', [s['axis'] for s in steps] == [4, 5])

app.approach_end()
check('示教结束并保存', (not app._approach_recording) and SMOKE_PATH.exists())
check('落盘可读回', len(AR.load(SMOKE_PATH)['steps']) == 2)

# --- 2) 序列前自动重放：seq_run 应先走接近段，再启动序列 ---
seq_started = []
app._seq_run_now = lambda: seq_started.append('seq')      # 替换真正的序列启动
sent.clear()
app.seq_run()
check('进入重放态', app._approach_running)
ok = wait_until(lambda: not app._approach_running, 12.0)
check('重放跑完', ok)
pump(0.6)

moves = [(a[0], a[1], a[4]) for c, a in sent if c == 'motor_pos']
check('重放发了 X/Y 两段', moves == [(4, 0, 3200), (5, 1, 1600)], f'{moves}')
check('重放后才启动序列', seq_started == ['seq'], f'{seq_started}')
check('每段前先使能', [a[0] for c, a in sent if c == 'motor_enable'] == [4, 5])

# --- 3) 停止会打断重放 ---
sent.clear()
app.seq_run()
pump(0.05)
app.emergency_stop()
pump(0.3)
check('急停清空重放队列', (not app._approach_running) and not app._approach_queue)

# --- 4) 关闭自动执行开关后，seq_run 直接进序列 ---
app._approach_auto.set(False)
seq_started.clear()
app.seq_run()
pump(0.2)
check('关掉开关后不再先重放', (not app._approach_running) and seq_started == ['seq'])

# --- 5) 新增：绝对走到此 / 常用走 也要能记上 ---
with mock.patch.object(host_gui.messagebox, 'askyesno', return_value=True):
    app.approach_clear()
    app.approach_begin()
check('重新进入示教态', app._approach_recording)

sent.clear()
app.client.send('motor_zero', 4)                 # 让 X 的软位置已知（=0）
pump(0.2)
app.client.send('motor_pos', 4, 0, 60, 1, 800, 'abs')   # 绝对走到此：软位置 0 → 800
pump(0.3)
steps = app.approach.get('steps') or []
check('绝对走到此记成一段（换算成相对 800）',
      len(steps) == 1 and steps[0]['axis'] == 4 and steps[0]['pulses'] == 800,
      f'{[ (s["axis"], s["pulses"]) for s in steps ]}')

app.client.send('motor_speed', 5, 0, 60, 1)      # 常用走：转 0.4s 再停
time.sleep(0.4)
pump(0.05)
app.client.send('motor_stop', 5)
pump(0.3)
steps = app.approach.get('steps') or []
check('常用走停下后结算成一段（约 60RPM×0.4s≈1280 脉冲）',
      len(steps) == 2 and steps[1]['axis'] == 5 and 800 <= steps[1]['pulses'] <= 2400,
      f'{[ (s["axis"], s["pulses"]) for s in steps ]}')
app.approach_end()

app.client.send = real_send
root.update()
root.destroy()
SMOKE_PATH.unlink(missing_ok=True)

print('RESULT:', 'ALL PASSED' if not fails else f'FAILED {fails}')
sys.exit(0 if not fails else 1)
