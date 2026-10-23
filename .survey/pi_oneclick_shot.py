"""验收「像连串口一样连派」：起一体化界面 → 下拉框里应出现「派 …」→ 选中 → 点连接
→ 控制在线 + 视觉自动开双画面。只用于本次验收，不属于交付物。

用法： python .survey\pi_oneclick_shot.py [等待秒数]
"""
import argparse
import sys
import tkinter as tk
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / '10.21'
for _p in (PKG / 'vision', PKG / 'host', PKG / 'common', PKG):
    sys.path.insert(0, str(_p))

wait_s = float(sys.argv[1]) if len(sys.argv) > 1 else 14.0

import main as M                      # noqa: E402

ns = argparse.Namespace(
    pi=None, pi_img_port=None, pi_ser_port=None,
    port=None, sim=False, autoconnect=False,
    no_vision=False, selftest=False,
)

import host_gui                       # noqa: E402

root = tk.Tk()
root.title(M.TITLE)
app = host_gui.App(root, ns)
_frame, vision = M.build_vision_tab(app.notebook, index=0, ctrl=app)
app.attach_vision(vision)
app.notebook.select(0)
root.geometry('1760x980')
root.title(M.TITLE)
root.update()


def after_discovery():
    values = list(app.port_box.cget('values') or ())
    print('下拉框条目:')
    for v in values:
        print('   -', v)
    pi_items = [v for v in values if v.startswith('派 ')]
    if not pi_items:
        print('!! 下拉框里没有派；检查 UDP 5056 广播是否被拦')
        root.after(300, root.destroy)
        return
    app.port_text.set(pi_items[0])
    print('已选中:', pi_items[0])
    app.toggle_connect()
    print('已点连接…')
    root.after(int(wait_s * 1000), shot)


def shot():
    print('connected:', app.client.state, '| online:', app.client.online,
          '| 自动开视觉:', app._vision_auto_started)
    try:
        from PIL import ImageGrab
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        out = PKG / 'runtime' / 'pi_oneclick.png'
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out)
        print('screenshot:', out)
    except Exception as exc:
        print('snapshot failed:', exc)
    root.after(300, root.destroy)


root.after(3000, after_discovery)
root.mainloop()
print('done')
