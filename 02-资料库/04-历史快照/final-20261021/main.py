"""new_eye + 10.15 上位机 一体化入口。

一个窗口、多个标签页：
    视觉（new_eye）  —— 双相机预览 / 四点标定 / 实时分割 / 单次识别 / 样本登记
    四轴控制 ...    —— 10.15 上位机原有页面（四轴、夹爪、参数、动作序列、视觉目标、日志）

用法（在 final 目录下）：
    python main.py                     # 正常启动（用最终 config.json）
    python main.py --sim               # 控制走模拟后端，不连硬件
    python main.py --port COM5 --connect
    python main.py --no-vision         # 只开控制（没有 cv2/torch 时自动降级）
    python main.py --selftest          # 自检：建界面、切页、截图、退出

依赖：requirements.txt；模型 new_eye/models/sam_b.pt。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VISION_DIR = HERE / 'new_eye'
HOST_DIR = HERE / 'ghr' / '10.15' / '上位机'
for _p in (str(VISION_DIR), str(HOST_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tkinter as tk                      # noqa: E402
from tkinter import ttk, messagebox       # noqa: E402


def build_vision_tab(notebook, index=0, ctrl=None):
    """把 new_eye 的界面放进同一个 Notebook；失败时放一个说明页。"""
    frame = ttk.Frame(notebook)
    notebook.insert(index, frame, text='  视觉  ')
    try:
        import gui as vision_gui          # new_eye/gui.py
        app = vision_gui.App(frame, embedded=True, ctrl=ctrl)
        return frame, app
    except Exception as exc:              # 缺依赖 / 模型缺失
        msg = f'{type(exc).__name__}: {exc}'
        ttk.Label(frame, padding=24, justify='left', text=(
            '视觉页没有启动成功：\n\n' + msg + '\n\n'
            '常见原因与处理：\n'
            '  1) 没装依赖：在 final 目录执行  pip install -r requirements.txt\n'
            '  2) 模型缺失：new_eye/models/sam_b.pt 不存在\n'
            '  3) 相机被别的程序占用：关掉 OBS / 相机预览后重开\n\n'
            '控制页不受影响，仍可正常使用。'
        )).pack(anchor='nw')
        return frame, None


def build_calib_tab(notebook, app, index=2):
    """X/Y 标定向导（只保留视觉点标注模式），插在“四轴控制”后面。"""
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='new_eye + 上位机 一体化软件')
    parser.add_argument('--port', help='串口设备名，例如 COM5')
    parser.add_argument('--sim', action='store_true', help='控制侧使用模拟后端')
    parser.add_argument('--connect', action='store_true', dest='autoconnect',
                        help='启动后自动连接')
    parser.add_argument('--no-vision', action='store_true', help='不加载视觉页')
    parser.add_argument('--selftest', action='store_true',
                        help='建界面、切页、截图后退出（开发自检用）')
    args = parser.parse_args(argv)

    import host_gui                        # ghr/10.15/上位机/host_gui.py

    root = tk.Tk()
    root.title('机器人视觉分拣系统 — 视觉 + 控制（一体化）')

    app = host_gui.App(root, args)         # 控制页（自带 Notebook 与所有标签页）

    vision = None
    calib = None
    if not args.no_vision:
        try:
            _frame, vision = build_vision_tab(app.notebook, index=0, ctrl=app)
            app.notebook.select(0)
        except Exception as exc:           # 理论上已被 build_vision_tab 兜住
            messagebox.showwarning('视觉页', f'视觉页加载失败：{exc}')
    try:
        calib = build_calib_tab(app.notebook, app, index=2)
        app.calib_tab = calib          # 让「视觉目标」页的“标定向导”按钮切到这个标签
    except Exception as exc:
        print('标定页加载失败：', exc)
    root.geometry('1760x980')              # 放在 App 之后，覆盖它的默认尺寸
    root.title('机器人视觉分拣系统 — 视觉 + 控制（一体化）')
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

    def _snap(name='final_app_proof.png'):
        try:
            from PIL import ImageGrab
            root.update()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            w, h = root.winfo_width(), root.winfo_height()
            out = HERE / 'runtime' / name
            out.parent.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(out)
            print('screenshot:', out)
        except Exception as exc:
            print('snapshot failed:', exc)

    if args.selftest:
        def _selftest():
            print('tabs:', [app.notebook.tab(i, 'text').strip()
                            for i in range(app.notebook.index('end'))])
            print('vision:', 'ok' if vision is not None else 'skipped')
            print('calib:', 'ok' if calib is not None else 'skipped')
            app.notebook.select(0)
            root.update()
            time.sleep(0.3)
            _snap()
            for idx in range(app.notebook.index('end')):
                name = app.notebook.tab(idx, 'text').strip()
                if name in ('视觉目标', 'X/Y 标定'):
                    app.notebook.select(idx)
                    root.update()
                    time.sleep(0.3)
                    _snap('final_app_%s.png' % ('vision_targets' if name == '视觉目标'
                                               else 'calib'))
            time.sleep(0.3)
            root.destroy()
        root.after(2500, _selftest)

    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
