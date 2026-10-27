r"""10.21 一体化入口：视觉（new_eye）+ 上位机（10.21/host）共一个窗口、8 个标签页。

和 final/main.py 的结构一致，区别只在路径与网络化参数：

    python 10.21\main.py                        # 本地：USB 相机 + 串口
    python 10.21\main.py --sim                  # 控制走模拟后端
    python 10.21\main.py --port COM5 --connect
    python 10.21\main.py --pi 192.168.137.50    # ★ WiFi：相机从派上取帧，串口换 TCP
    python 10.21\main.py --pi 192.168.137.50 --pi-img-port 5057 --pi-ser-port 5058
    python 10.21\main.py --no-vision            # 只开控制（没 cv2/torch 时降级）
    python 10.21\main.py --selftest             # 建 8 页界面、逐页截图到 runtime/、退出

--pi HOST 做的事：
  1. os.environ['ROBOT_PI'] = HOST  → vision/camera_factory.py 据此选 RemoteCamera；
  2. 未显式给 --port 时，默认串口端口变成 tcp://HOST:5058
     （host_gui 的端口下拉框可编辑，open_best_link 认识 tcp:// 前缀）；
  3. --pi HOST --connect 就会自动走 TCP 连派上的串口桥。

最终 8 页顺序：视觉(0)、四轴控制(1)、X/Y 标定(2)、舵机夹爪(3)、参数(4)、
动作序列(5)、视觉目标(6)、原始日志(7)。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 10.21
VISION_DIR = HERE / 'vision'
HOST_DIR = HERE / 'host'
for _p in (str(VISION_DIR), str(HOST_DIR), str(HERE / 'common')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tkinter as tk                      # noqa: E402
from tkinter import ttk, messagebox       # noqa: E402

TITLE = '机器人视觉分拣系统 — 视觉 + 控制（一体化）'
DEFAULT_IMG_PORT = 5057
DEFAULT_SER_PORT = 5058


def _split_host_port(text: str):
    """`192.168.137.50` / `192.168.137.50:5058` → (host, port|None)。"""
    text = (text or '').strip()
    if not text:
        return '', None
    host, sep, raw = text.rpartition(':')
    if sep and host and raw.isdigit():
        return host, int(raw)
    return text, None


def apply_pi_args(args) -> None:
    """把 --pi* 翻译成环境变量 + 默认串口端口，供 vision / host_gui 读取。"""
    if not args.pi:
        if args.pi_img_port:
            os.environ['ROBOT_PI_IMG_PORT'] = str(int(args.pi_img_port))
        return
    host, embedded_port = _split_host_port(args.pi)
    if not host:
        return
    os.environ['ROBOT_PI'] = host
    img_port = args.pi_img_port or None
    if img_port is None:
        try:
            import wire as _wire          # 10.21/common/wire.py 的 IMG_PORT
            img_port = int(_wire.IMG_PORT)
        except Exception:                 # noqa: BLE001 - 没 common/ 也能跑
            img_port = DEFAULT_IMG_PORT
    os.environ['ROBOT_PI_IMG_PORT'] = str(int(img_port))
    ser_port = args.pi_ser_port or embedded_port or DEFAULT_SER_PORT
    if not args.port:
        args.port = 'tcp://%s:%d' % (host, int(ser_port))
    print('[main] WiFi 模式：相机 %s:%d，串口桥 %s' %
          (host, int(img_port), args.port), flush=True)


def build_vision_tab(notebook, index=0, ctrl=None):
    """把 10.21/vision 的界面放进同一个 Notebook；失败时放一个说明页。"""
    frame = ttk.Frame(notebook)
    notebook.insert(index, frame, text='  视觉  ')
    try:
        import gui as vision_gui          # 10.21/vision/gui.py
        app = vision_gui.App(frame, embedded=True, ctrl=ctrl, with_ctrl=(ctrl is None))
        return frame, app
    except Exception as exc:              # 缺依赖 / 模型缺失 / 远程连不上
        msg = f'{type(exc).__name__}: {exc}'
        ttk.Label(frame, padding=24, justify='left', text=(
            '视觉页没有启动成功：\n\n' + msg + '\n\n'
            '常见原因与处理：\n'
            '  1) 没装依赖：在 10.21 目录执行  pip install -r requirements.txt\n'
            '  2) 模型缺失：10.21/vision/models/sam_b.pt 不存在\n'
            '  3) 相机被别的程序占用：关掉 OBS / 相机预览后重开\n'
            '  4) 远程模式：确认 --pi 的 IP 可达、派上 pi_agent.py 已启动\n\n'
            '控制页不受影响，仍可正常使用。'
        )).pack(anchor='nw')
        return frame, None


def build_calib_tab(notebook, app, index=2):
    """X/Y 标定向导（视觉点标注模式），插在“四轴控制”后面。"""
    try:
        import calib_tab
        return calib_tab.build(notebook, app, index=index)
    except Exception as exc:
        frame = ttk.Frame(notebook)
        notebook.insert(index, frame, text='  X/Y 标定  ')
        ttk.Label(frame, padding=24, justify='left', text=(
            '标定页没有启动成功：\n\n%s: %s' % (type(exc).__name__, exc)
        )).pack(anchor='nw')
        return None


def _slug(text: str) -> str:
    """标签页文字 → 安全的文件名片段（中文保留）。"""
    text = (text or '').strip().replace('/', '_').replace('\\', '_')
    text = re.sub(r'[\s:：*?"<>|]+', '_', text)
    return text.strip('_') or 'tab'


def _snap(root, name):
    """截当前窗口到 10.21/runtime/<name>；PIL 缺失或截屏失败时返回 None。"""
    try:
        from PIL import ImageGrab
        root.update()
        root.lift()
        root.focus_force()
        root.update()
        time.sleep(0.05)
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        out = HERE / 'runtime' / name
        out.parent.mkdir(parents=True, exist_ok=True)
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out)
        print('screenshot:', out, flush=True)
        return out
    except Exception as exc:              # 无显示/无 PIL
        print('snapshot failed:', exc, flush=True)
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='10.21 机器人视觉分拣系统（视觉 + 控制一体化）')
    parser.add_argument('--port', help='串口设备名（COM5）或 TCP 端点（tcp://192.168.137.50:5058）')
    parser.add_argument('--sim', action='store_true', help='控制侧使用模拟后端')
    parser.add_argument('--connect', action='store_true', dest='autoconnect',
                        help='启动后自动连接')
    parser.add_argument('--no-vision', action='store_true', help='不加载视觉页')
    parser.add_argument('--selftest', action='store_true',
                        help='建界面、逐页截图到 runtime/ 后退出（开发自检用）')
    parser.add_argument('--pi', metavar='HOST', default=None,
                        help='树莓派 IP（也接受 HOST:亚端口）：相机远程取帧 + 串口走 TCP')
    parser.add_argument('--pi-img-port', type=int, default=None,
                        help='树莓派图传端口（默认 %d）' % DEFAULT_IMG_PORT)
    parser.add_argument('--pi-ser-port', type=int, default=None,
                        help='树莓派串口桥端口（默认 %d）' % DEFAULT_SER_PORT)
    args = parser.parse_args(argv)

    apply_pi_args(args)                   # 必须在 import host_gui / vision 之前设置环境变量

    import host_gui                        # 10.21/host/host_gui.py

    root = tk.Tk()
    root.title(TITLE)

    app = host_gui.App(root, args)         # 控制页（自带 Notebook 与 6 个标签页）

    run_tab = None
    vision = None
    calib = None
    if not args.no_vision:
        try:
            _frame, vision = build_vision_tab(app.notebook, index=0, ctrl=app)
            app.attach_vision(vision)      # 连到派时由控制页自动开双画面
            app.notebook.select(0)
        except Exception as exc:           # 理论上已被 build_vision_tab 兜住
            messagebox.showwarning('视觉页', f'视觉页加载失败：{exc}')
    # 运行台：比赛当天唯一操作页，插到最前面（index 0）
    try:
        import run_tab as RT
        run_tab = RT.build_run_tab(app.notebook, app, index=0)
        if vision is not None:
            run_tab.attach_vision(vision)
        app.run_tab = run_tab
    except Exception as exc:
        print('运行台加载失败：', exc)
    try:
        calib = build_calib_tab(app.notebook, app, index=3)   # 运行台占了 0，标定页顺延
        app.calib_tab = calib              # 让「视觉目标」页的“标定向导”按钮切到这个标签
        if vision is not None and hasattr(vision, 'attach_calib'):
            vision.attach_calib(calib)     # 视觉页右栏的「自动抓一件/连续抓/分盒清零」直接复用标定页实现
        if run_tab is not None:
            run_tab.attach_calib(calib)    # 运行台复用标定页的位置来源（counts/软位置）
    except Exception as exc:
        print('标定页加载失败：', exc)
    root.geometry('1760x980')              # 放在 App 之后，覆盖它的默认尺寸
    root.title(TITLE)

    def _tab_names():
        return [app.notebook.tab(i, 'text').strip()
                for i in range(app.notebook.index('end'))]

    if vision is not None or calib is not None:
        # 切页时补一次刷新（隐藏页期间 canvas 尺寸可能是 1px；标定页要重读标注点）
        def _on_tab(_event=None):
            try:
                name = app.notebook.tab('current', 'text').strip()
                if name == '视觉' and vision is not None:
                    vision.refresh()
                elif name == 'X/Y 标定' and calib is not None:
                    calib.on_marks_refresh()
            except Exception:
                pass
        app.notebook.bind('<<NotebookTabChanged>>', _on_tab)

    if args.selftest:
        def _selftest():
            try:
                names = _tab_names()
                print('title:', root.title(), flush=True)
                print('tabs:', names, flush=True)
                print('vision:', 'ok' if vision is not None else 'skipped',
                      '| calib:', 'ok' if calib is not None else 'skipped', flush=True)
                shots = []
                total = app.notebook.index('end')
                for idx in range(total):
                    app.notebook.select(idx)
                    root.update()
                    name = app.notebook.tab(idx, 'text').strip()
                    time.sleep(0.45)       # 等隐藏页的首帧/布局稳定
                    root.update()
                    out = _snap(root, 'selftest_%d_%s.png' % (idx + 1, _slug(name)))
                    if out is not None:
                        shots.append(out)
                print('selftest pages:', len(shots), '/', total, flush=True)
            except Exception as exc:       # 自检不许静默失败
                import traceback
                traceback.print_exc()
                print('selftest failed:', exc, flush=True)
            finally:
                try:
                    if vision is not None and hasattr(vision, 'stop'):
                        vision.stop()
                except Exception:
                    pass
                root.destroy()

        root.after(2500, _selftest)

    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
