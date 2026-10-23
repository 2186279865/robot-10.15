"""一次性驱动：起一体化界面，切到「四轴控制」页，截「接近段示教」面板。

只用于本次验收，不属于交付物。
用法： python .survey\approach_panel_shot.py [目标图片名]
"""
import argparse
import sys
import tkinter as tk
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / '10.21'
for _p in (PKG / 'vision', PKG / 'host', PKG / 'common', PKG):
    sys.path.insert(0, str(_p))

out_name = sys.argv[1] if len(sys.argv) > 1 else 'approach_panel.png'

import main as M                      # noqa: E402
ns = argparse.Namespace(
    pi=None, pi_img_port=None, pi_ser_port=None,
    port='', sim=False, autoconnect=False,
    no_vision=True, selftest=False,
)
M.apply_pi_args(ns)

import host_gui                       # noqa: E402

root = tk.Tk()
root.title(M.TITLE)
app = host_gui.App(root, ns)
root.geometry('1760x980')
root.update()

tabs = [app.notebook.tab(i, 'text').strip() for i in range(app.notebook.index('end'))]
print('tabs:', tabs)
axes_index = tabs.index('四轴控制') if '四轴控制' in tabs else 1
app.notebook.select(axes_index)
root.update()
print('approach status:', app._approach_status.cget('text') if app._approach_status else '(none)')


def shot():
    try:
        from PIL import ImageGrab
        root.update()
        root.lift()
        root.focus_force()
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        out = PKG / 'runtime' / out_name
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out)
        print('screenshot:', out)
    except Exception as exc:
        print('snapshot failed:', exc)
    root.after(300, root.destroy)


root.after(1200, shot)
root.mainloop()
print('done')
