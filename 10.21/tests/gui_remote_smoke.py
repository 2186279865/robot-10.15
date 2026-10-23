#!/usr/bin/env python
r"""10.21 视觉页「远程取帧」端到端冒烟（可选，比 test_net_local 更接近真实使用）。

跑法::

    Qwen\.venv\Scripts\python.exe 10.21\tests\gui_remote_smoke.py

它会：
1. 以子进程启动 `10.21/pi/pi_agent.py --fake-camera --fake-serial
   --img-port 15057 --ser-port 15058 --no-discovery`；
2. 设 ROBOT_PI=127.0.0.1 / ROBOT_PI_IMG_PORT=15057，让 camera_factory 走远程；
3. 建出真正的 8 页一体化窗口（host_gui.App + main.build_vision_tab），
   把视觉页顶/侧相机索引改成远程 cam 0 / 1，点「开始」等价地调用 App.start()；
4. 等 6 s，断言视觉页真的收到了远程帧（Stream.frames>0 且 get() 有图），
   并把窗口截图存到 10.21/runtime/remote_gui_smoke.png；
5. 收尾停流、销毁窗口、terminate 子进程。

这条脚本只证明「GUI + RemoteCamera + pi_agent 图传」这条链路能出画面；
取帧失败时是否崩溃由 test_net_local.py 的 (c) 覆盖。
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent       # 10.21/tests
PKG = HERE.parent                            # 10.21
IMG_PORT = 15057
SER_PORT = 15058

for _p in (str(PKG / 'common'), str(PKG), str(PKG / 'host'), str(PKG / 'vision')):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def wait_port(port: int, timeout: float = 12.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def main() -> int:
    agent = subprocess.Popen(
        [sys.executable, str(PKG / 'pi' / 'pi_agent.py'), '--fake-camera',
         '--fake-serial', '--img-port', str(IMG_PORT), '--ser-port', str(SER_PORT),
         '--no-discovery'],
        cwd=str(PKG), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    root = None
    vision = None
    try:
        if not (wait_port(IMG_PORT) and wait_port(SER_PORT)):
            print('FAIL  pi_agent 没起来（15057/15058）')
            return 1
        os.environ['ROBOT_PI'] = '127.0.0.1'
        os.environ['ROBOT_PI_IMG_PORT'] = str(IMG_PORT)

        import importlib.util

        # 用文件路径加载 10.21/main.py：sys.path 里还有 10.21/vision/main.py，
        # 直接 `import main` 会拿到视觉那份。
        spec = importlib.util.spec_from_file_location('pkg_main_1021', PKG / 'main.py')
        pkg_main = importlib.util.module_from_spec(spec)
        sys.modules['pkg_main_1021'] = pkg_main
        spec.loader.exec_module(pkg_main)

        import host_gui

        root = tk.Tk()
        ns = argparse.Namespace(port=None, sim=True, autoconnect=False,
                                no_vision=False, selftest=False, pi=None,
                                pi_img_port=None, pi_ser_port=None)
        app = host_gui.App(root, ns)
        _frame, vision = pkg_main.build_vision_tab(app.notebook, index=0, ctrl=app)
        root.geometry('1760x980')
        root.update()
        if vision is None:
            print('FAIL  视觉页没建起来')
            return 1
        # 远程模式：组合框里的数字就是 wire 的 cam id（0=顶视，1=侧视）
        vision.tv.set('0')
        vision.sv.set('1')
        vision.start()
        app.notebook.select(0)                    # 切到视觉页，截图里能看到远程画面
        root.update()

        end = time.monotonic() + 6.0
        while time.monotonic() < end:
            root.update()
            time.sleep(0.02)

        top = getattr(vision.a, 'frames', 0) if getattr(vision, 'a', None) else 0
        side = getattr(vision.bstream, 'frames', 0) if getattr(vision, 'bstream', None) else 0
        img = vision.a.get() if top else None
        shot = PKG / 'runtime' / 'remote_gui_smoke.png'
        shot.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import ImageGrab
            root.update()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + root.winfo_width(),
                                 y + root.winfo_height())).save(shot)
            print('screenshot:', shot)
        except Exception as exc:                     # noqa: BLE001
            print('screenshot failed:', exc)

        shape = None if img is None else img.shape
        print('远程帧数：顶视 %d，侧视 %d；顶视帧尺寸 %r' % (top, side, shape))
        if top <= 0 or img is None:
            print('FAIL  视觉页没有收到远程帧')
            return 1
        print('PASS  视觉页经 WiFi 收到远程帧并渲染（cam0 %r）' % (shape,))
        return 0
    finally:
        try:
            if vision is not None:
                vision.stop()
        except Exception:
            pass
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass
        try:
            agent.terminate()
            agent.wait(timeout=3)
        except Exception:
            try:
                agent.kill()
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
