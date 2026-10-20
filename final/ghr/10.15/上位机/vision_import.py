"""视觉目标导入（只读视觉 + 生成待确认的抓取序列 + 标定向导）。

数据来源：`new_eye/runtime/latest_targets.json`（schema 4/5，托盘归一化坐标 u/v）。
本模块做三件事：
1. 把视觉结果列出来，标出"过期 / 待登记 / 副摄未印证"等风险；
2. 按用户选中的目标生成 `sequence.Sequence`，填到"动作序列"页，**由用户自己点运行**。
   生成不会自动下发任何运动指令；`motion_authorized` 必须是 false 才接受数据；
   生成后把"转盘停在哪个盒子"写进 `vision_grasp.tray_state.json`，下次从那里继续；
3. "标定向导"按钮：打开 `calib_dialog`，按顺序让 X/Y 走固定脉冲、记录 u/v，解出标定。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from tkinter import ttk, messagebox

import vision_grasp

ROOT = Path(__file__).resolve().parents[3]
TARGET = ROOT / 'new_eye/runtime/latest_targets.json'
CALIB = ROOT / 'new_eye/tray_calib.json'

#: 视觉大约 1 fps，超过这个秒数就当过期（避免拿几分钟前的图去抓）
MAX_AGE_S = 8.0


def read_targets(path=TARGET, max_age=MAX_AGE_S):
    path = Path(path)
    if not path.exists():
        raise ValueError(f'还没有视觉结果：{path}\n先在 new_eye 里点“单次识别”或开“实时分割”')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') not in (4, 5):
        raise ValueError('不支持的视觉数据格式（需要 schema_version 4/5，即 new_eye 的输出）')
    if data.get('motion_authorized') is not False:
        raise ValueError('这里只接受未授权运动的目标（motion_authorized 必须为 false）')
    ts = float(data.get('ts') or 0.0)
    if ts <= 0:
        raise ValueError('视觉数据缺少时间戳 ts')
    age = time.time() - ts
    if age > max_age:
        raise ValueError(f'视觉数据已过期 {age:.1f} 秒（阈值 {max_age:.0f}s），请重新识别')
    targets = data.get('targets') or []
    if not isinstance(targets, list):
        raise ValueError('targets 不是列表')
    for target in targets:
        uv = target.get('center_tray')
        if not (isinstance(uv, (list, tuple)) and len(uv) == 2):
            raise ValueError(f'目标 {target.get("id")} 缺少 center_tray（托盘归一化坐标）')
        u, v = float(uv[0]), float(uv[1])
        if not (math.isfinite(u) and math.isfinite(v) and 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            raise ValueError(f'目标 {target.get("id")} 的 u/v 超出 0~1：{uv}')
    return data, age


def load_calib(path=CALIB):
    path = Path(path)
    if not path.exists():
        raise ValueError(f'还没有托盘标定：{path}\n先按 new_eye/联调与通讯方案.md 第 4 节标定一次')
    data = json.loads(path.read_text(encoding='utf-8'))
    if 'affine' not in data:
        raise ValueError(f'{path} 不是有效标定文件（缺 affine）')
    return data


def build_tab(notebook, app=None):
    panel = ttk.Frame(notebook, padding=8)
    notebook.add(panel, text='视觉目标')

    ttk.Label(panel, foreground='#884400', wraplength=900,
              text='new_eye 顶视分割结果（托盘归一化坐标）。勾选目标后“生成抓取序列”，'
                   '序列会填到“动作序列”页并等你手动点运行；未示教的参数会列出来。').pack(anchor='w')

    status = ttk.Label(panel, text=str(TARGET), wraplength=900)
    status.pack(anchor='w', pady=6)

    columns = ('id', 'cls', 'u', 'v', 'views', 'flags')
    tree = ttk.Treeview(panel, columns=columns, show='headings', height=12,
                        selectmode='extended')
    for key, label, width in (('id', '编号', 70), ('cls', '类别', 150), ('u', 'u', 90),
                              ('v', 'v', 90), ('views', '视角', 110), ('flags', '风险', 260)):
        tree.heading(key, text=label)
        tree.column(key, width=width, anchor='w')
    tree.pack(fill='both', expand=True)

    state = {'targets': [], 'age': 0.0, 'data': None}

    def refresh():
        tree.delete(*tree.get_children())
        state['targets'] = []
        try:
            data, age = read_targets()
        except Exception as exc:
            status.configure(text='未加载有效视觉目标')
            messagebox.showerror('视觉数据', str(exc))
            return
        state.update(data=data, age=age)
        targets = data.get('targets') or []
        state['targets'] = targets
        status.configure(
            text=f"批次 {data.get('seq', '?')} ｜ 拍摄距今 {age:.1f}s ｜ "
                 f"目标 {len(targets)} 个 ｜ schema {data.get('schema_version')} ｜ {TARGET}")
        for index, target in enumerate(targets):
            uv = target.get('center_tray') or [0, 0]
            flags = []
            if target.get('need_review'):
                flags.append('待登记')
            if target.get('views') == 'top':
                flags.append('副摄未印证')
            if target.get('views') == 'side':
                flags.append('副摄多出')
            if target.get('color_mismatch'):
                flags.append('两路颜色不一致')
            tree.insert('', 'end', iid=str(index),
                        values=(target.get('id'), target.get('class_name') or '待登记',
                                f'{float(uv[0]):.3f}', f'{float(uv[1]):.3f}',
                                target.get('views') or '-', ' / '.join(flags)))
        if targets:
            tree.selection_set(tree.get_children()[0])

    def generate():
        selection = tree.selection()
        if not selection:
            messagebox.showinfo('生成抓取序列', '先在列表里选中要抓的目标（可多选）')
            return
        if not state['targets']:
            messagebox.showinfo('生成抓取序列', '先点“读取最新视觉结果”')
            return
        chosen = [state['targets'][int(iid)] for iid in selection]
        try:
            import home_route as HR
            home = HR.load_state()
        except Exception:
            home = {}
        if not home.get('ok'):
            if not messagebox.askyesno(
                    '还没建立基准',
                    '还没执行过「开机回基准」。\n\n'
                    '现在所有 X/Y 坐标都按“上位机启动时/上次清零的位置”解释，'
                    '和标定时用的 0 基准可能不一致，抓取会整体偏移。\n\n'
                    '仍然要生成序列吗？（建议先放开机位 → 顶栏「开机回基准」）'):
                return
        try:
            calib = load_calib()
        except Exception as exc:
            messagebox.showerror('托盘标定', str(exc))
            return
        tray_state = vision_grasp.load_state()
        try:
            seq, warnings, meta = vision_grasp.build_sequence(
                chosen, calib, start_box=tray_state['current_box'])
        except Exception as exc:
            messagebox.showerror('生成序列失败', str(exc))
            return
        summary = '\n'.join(warnings[:8]) if warnings else '示教参数看起来齐了'
        if not messagebox.askyesno(
                '生成抓取序列',
                f'共 {len(chosen)} 个目标，生成 {len(seq.steps)} 步。\n'
                f'转盘：盒{meta["start_box"]} → 盒{meta["final_box"]}。\n\n'
                f'提醒：\n{summary}\n\n生成后请在“动作序列”页检查，确认无误再点运行。'):
            return
        # 只有用户确认生成后才记“转盘停在哪”，避免取消后状态错乱
        vision_grasp.save_state(meta['final_box'])
        if app is not None:
            app.sequence = seq
            app.seq_refresh()
            app.note(f'已生成视觉抓取序列：{len(seq.steps)} 步（{len(chosen)} 个目标），'
                     f'转盘 盒{meta["start_box"]}→盒{meta["final_box"]}，未自动运行')
        else:
            messagebox.showinfo('生成抓取序列', f'已生成 {len(seq.steps)} 步（没有界面连接，未写入）')

    def open_wizard():
        if app is None:
            messagebox.showinfo('标定向导', '需要主界面连接（app 为空）')
            return
        # 一体化界面里“X/Y 标定”是独立标签页：直接切过去（只保留视觉点标注模式）
        tab = getattr(app, 'calib_tab', None)
        notebook = getattr(app, 'notebook', None)
        if tab is not None and notebook is not None:
            try:
                notebook.select(tab.master)
                tab.on_marks_refresh()
                try:
                    app.note('已切到「X/Y 标定」页')
                except Exception:
                    pass
                return
            except Exception:
                pass
        try:
            import calib_dialog
        except Exception as exc:  # 缺 tkinter 时也不能让整个页崩
            messagebox.showerror('标定向导', f'打不开标定向导：{exc}')
            return
        calib_dialog.open_wizard(app)

    def reset_state():
        vision_grasp.save_state(1)
        messagebox.showinfo('转盘记忆', '已把“转盘当前盒”重置为盒 1（手动转过转盘后请点这个）')

    bar = ttk.Frame(panel)
    bar.pack(fill='x', pady=8)
    ttk.Button(bar, text='读取最新视觉结果（不运动）', command=refresh).pack(side='left')
    ttk.Button(bar, text='标定向导（u/v → X/Y 脉冲）', command=open_wizard).pack(side='left', padx=8)
    ttk.Button(bar, text='生成抓取序列（选中目标）', command=generate).pack(side='left')
    ttk.Button(bar, text='转盘记忆重置为盒1', command=reset_state).pack(side='left', padx=8)
    ttk.Label(bar, text=f'标定文件：{CALIB.name}', foreground='#666666').pack(side='left', padx=8)

    return panel
