"""一次性驱动：起和 main.py 一样的一体化界面，自动点「打开双画面」，等真图进来后截图。

只用于本次真机验收，不属于交付物。
用法： python .survey\gui_live_shot.py 172.20.240.14 [等待秒数]
"""
import argparse
import os
import sys
import tkinter as tk
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / '10.21'
for _p in (PKG / 'vision', PKG / 'host', PKG / 'common', PKG):
    sys.path.insert(0, str(_p))          # 最后插入 PKG，保证 import main 命中 10.21/main.py

host = sys.argv[1] if len(sys.argv) > 1 else '172.20.240.14'
wait_s = float(sys.argv[2]) if len(sys.argv) > 2 else 18.0

os.environ['ROBOT_PI'] = host
os.environ['ROBOT_PI_IMG_PORT'] = '5057'
# cam_map 不在这里写死：由 10.21/vision/config.json 的 remote.cam_map 决定（也可用 ROBOT_PI_CAM_MAP 覆盖）

import main as M                      # noqa: E402

ns = argparse.Namespace(
    pi=host, pi_img_port=None, pi_ser_port=None,
    port=f'tcp://{host}:5058', sim=False, autoconnect=True,
    no_vision=False, selftest=False,
)
M.apply_pi_args(ns)

import host_gui                       # noqa: E402

root = tk.Tk()
root.title(M.TITLE)
app = host_gui.App(root, ns)
_frame, vision = M.build_vision_tab(app.notebook, index=0, ctrl=app)
app.notebook.select(0)
root.geometry('1760x980')
root.title(M.TITLE)
root.update()

print('tabs:', [app.notebook.tab(i, 'text').strip() for i in range(app.notebook.index('end'))])
if vision is None:
    print('!! 视觉页没建起来')
    sys.exit(2)
vision.start()                         # 等价于点「打开双画面」
print('已触发打开双画面，等 %ss 收图…' % wait_s)


def shot():
    try:
        from PIL import ImageGrab
        root.update()
        root.lift()
        root.focus_force()
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        out = PKG / 'runtime' / 'real_pi_live.png'
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out)
        print('screenshot:', out)
    except Exception as exc:
        print('snapshot failed:', exc)
    root.after(300, root.destroy)


root.after(int(wait_s * 1000), shot)
root.mainloop()
print('done')
