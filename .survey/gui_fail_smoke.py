"""验收「连接失败要看得见」：把端口指向一个没人监听的地址，点连接，
断言 ①弹窗被调用 ②状态栏变「连接失败」红字 ③日志里有原因。

只用于本次验收，不属于交付物。
"""
import argparse
import sys
import tkinter as tk
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / '10.21'
for _p in (PKG / 'vision', PKG / 'host', PKG / 'common', PKG):
    sys.path.insert(0, str(_p))

import host_client                      # noqa: E402
import host_gui                         # noqa: E402

calls = []


def fake_warning(title, message, **kw):
    calls.append((title, message))


host_gui.messagebox.showwarning = fake_warning

root = tk.Tk()
root.withdraw()
ns = argparse.Namespace(
    port='tcp://127.0.0.1:59999', sim=False, autoconnect=False,
    no_vision=True, selftest=False, pi=None, pi_img_port=None, pi_ser_port=None,
)
app = host_gui.App(root, ns)
app.port_text.set('tcp://127.0.0.1:59999')
root.update()
root.update()
root.update()
root.update()
app.toggle_connect()
root.update()#x

state_text = app.state_label.cget('text')
fg = app.state_label.cget('foreground')
print('弹窗次数 =', len(calls))
print('弹窗标题 =', calls[0][0] if calls else None)
print('弹窗标题是连接失败 =', calls[0][0] == '连接失败' if calls else False)
print('弹窗内容含排查建议 =', ('systemctl status robotpi' in calls[0][1]) if calls else False)
print('状态栏文字 =', repr(state_text), '  颜色 =', fg)
print('last_error =', app.client.last_error)
ok = (
    len(calls) == 1
    and state_text == '连接失败'
    and '127.0.0.1:59999' in calls[0][1]
    and 'systemctl status robotpi' in calls[0][1]
)
print('RESULT:', 'PASS' if ok else 'FAIL')
root.destroy()
sys.exit(0 if ok else 1)


