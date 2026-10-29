"""X/Y 标定向导（标签页版，只保留“视觉点标注”模式）。

流程（一次性）：
    1) new_eye 顶视画面点「标注点」，在物块/贴纸位置点 3~6 个点（自动写 calib_marks.json）
    2) 本页「刷新标注点」
    3) 用点动 / 单轴页把夹爪移到每个标注点的正上方，选中该行点「记录爪位」
    4) 全部记完点「求解并保存」→ new_eye/tray_calib.json

脉冲来源优先用驱动器回读（若该轴接了回读线），否则用上位机软位置。
"""
from __future__ import annotations

import threading
import time

import cv2
import numpy as np
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageTk

import auto_grab as AG
import calib_wizard as W
import grasp_calib as GC
import prep_pose as PP
import units
import vision_grasp as VG

AXIS_X, AXIS_Y = VG.AXIS_X, VG.AXIS_Y
AXIS_NAMES = {AXIS_X: 'X(4)', AXIS_Y: 'Y(5)', VG.AXIS_Z: 'Z(1)', VG.AXIS_TRAY: '托盘(3)'}


def _travel_limits():
    """读软限位（脉冲），给标定自检用：{轴号: (min, max)}。失败返回 None。"""
    try:
        import auto_runner as _AR
        limits = _AR.load_limits() or {}
        out = {}
        for key, val in limits.items():
            try:
                out[int(key)] = (int(val['min']), int(val['max']))
            except Exception:
                continue
        return out or None
    except Exception:
        return None


PREVIEW_W, PREVIEW_H = 336, 189     # 顶视预览尺寸（16:9）


class CalibTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.marks: list = []
        self.var_write = tk.BooleanVar(value=True)
        self.var_jog = tk.StringVar(value='500')
        self.var_flip_x = tk.BooleanVar(value=False)
        self.var_flip_y = tk.BooleanVar(value=False)
        self._last_uv = None
        self._jog_token = 0
        self._prep_running = False
        self._many_stop = False
        self._prep_started = 0.0
        self._build()
        self._refresh_pos()
        self.on_marks_refresh()
        self.after(700, self._tick)

    # ---------------------------------------------------------------- 界面
    def _build(self):
        ttk.Label(self, justify='left', wraplength=1180, foreground='#884400', text=(
            '怎么用：① 在「视觉」页顶视画面上点「标注点」，在物块/贴纸位置点 3~6 个点（会自动存文件）；'
            '② 回到本页点「刷新标注点」；③ 用下面的点动（或「四轴控制」页）把夹爪移到同一个物理位置上方，'
            '选中那一行点「记录爪位」；④ 全部记完点「求解并保存」。\n'
            '要点：点要分散（别排成一条线）、至少 3 个；夹爪每次都要对到同一个“爪子中心”。'
        )).pack(anchor='w')

        bar = ttk.Frame(self)
        bar.pack(fill='x', pady=(6, 2))
        ttk.Button(bar, text='一键走出固定路线', command=self.on_prep_route
                   ).pack(side='left', padx=(0, 12))
        ttk.Button(bar, text='自动抓一件', command=self.on_auto_grab
                   ).pack(side='left', padx=(0, 6))
        self.many_btn = ttk.Button(bar, text='连续抓', command=self.on_auto_many)
        self.many_btn.pack(side='left', padx=(0, 3))
        self.many_lbl = ttk.Label(bar, text='', foreground='#005599')
        self.many_lbl.pack(side='left', padx=(0, 10))
        ttk.Button(bar, text='分盒清零', command=self.on_box_reset
                   ).pack(side='left', padx=(0, 6))
        self.many_var = tk.IntVar(value=6)
        ttk.Spinbox(bar, from_=1, to=20, width=3, textvariable=self.many_var
                    ).pack(side='left', padx=(0, 2))
        ttk.Label(bar, text='件').pack(side='left', padx=(0, 12))
        self.drop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='抓完落货（转盘60°+松爪）', variable=self.drop_var
                        ).pack(side='left', padx=(0, 12))
        ttk.Button(bar, text='记录抓取点', command=self.on_record_grasp
                   ).pack(side='left', padx=(0, 4))
        ttk.Button(bar, text='残差预览', command=self.on_preview_grasp
                   ).pack(side='left', padx=3)
        ttk.Button(bar, text='删掉最后一点', command=self.on_drop_last_grasp
                   ).pack(side='left', padx=3)
        ttk.Button(bar, text='抓取点求解', command=self.on_solve_grasp
                   ).pack(side='left', padx=(0, 12))

        bar2 = ttk.Frame(self)
        bar2.pack(fill='x', pady=(0, 6))
        ttk.Button(bar2, text='刷新标注点', command=self.on_marks_refresh
                   ).pack(side='left', padx=(0, 6))
        ttk.Button(bar2, text='记录爪位', command=self.on_marks_record
                   ).pack(side='left', padx=(0, 6))
        ttk.Button(bar2, text='清除这个爪位', command=self.on_marks_clearrow
                   ).pack(side='left', padx=(0, 6))
        ttk.Button(bar2, text='求解并保存', command=self.on_marks_solve
                   ).pack(side='left', padx=(0, 6))
        ttk.Checkbutton(bar2, text='写 tray_calib.json',
                        variable=self.var_write).pack(side='left', padx=8)

        self.marks_label = ttk.Label(self, text='标注点：—', foreground='#0a3d91')
        self.marks_label.pack(anchor='w')
        self.pos_label = ttk.Label(self, text='软位置：X=+0 Y=+0 脉冲', foreground='#0a3d91')
        self.pos_label.pack(anchor='w', pady=(2, 0))
        self.calib_status = ttk.Label(self, text='标定状态：—', foreground='#4a4a4a')
        self.calib_status.pack(anchor='w', pady=(2, 0))
        self.refresh_calib_status()

        ttk.Label(self, text='标注点（P 编号 = new_eye 里点的顺序；X/Y 是记录的爪位脉冲）'
                  ).pack(anchor='w', pady=(8, 0))
        # 顶视小预览：放在列表上方，点动 X/Y 时不用来回切页
        pv = ttk.LabelFrame(self, text='顶视预览（0.25 s 刷新）', padding=(6, 4))
        pv.pack(fill='x', pady=(6, 4))
        blank = Image.new('RGB', (PREVIEW_W, PREVIEW_H), (32, 33, 36))
        self._pv_photo = ImageTk.PhotoImage(blank)      # 占位图，同时定住控件尺寸
        self._pv_label = tk.Label(pv, image=self._pv_photo, relief='solid', bd=1)
        self._pv_label.pack(side='left')
        self._pv_note = ttk.Label(pv, text='（点动 X/Y 时这里看顶视图）', foreground='#666')
        self._pv_note.pack(side='left', padx=(10, 0), anchor='n')

        box = ttk.Frame(self)
        box.pack(fill='both', expand=True)

        # 用 grid 固定三列：标注点列表 | 滚动条 | 顶视小预览（pack 的 expand 会把预览挤没）
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(box, height=8, width=64, font=('Consolas', 10),
                                  bg='#f7f8fa', relief='solid', bd=1)
        sb = ttk.Scrollbar(box, orient='vertical', command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.grid(row=0, column=0, sticky='nsew')
        sb.grid(row=0, column=1, sticky='ns')

        self.after(400, self._preview_tick)

        jog = ttk.LabelFrame(self, text='XY 点动（脉冲为非负；方向用按钮/取反）', padding=8)
        jog.pack(fill='x', pady=(8, 0))
        row = ttk.Frame(jog)
        row.pack(fill='x')
        ttk.Button(row, text='X＋', width=6, command=lambda: self.on_jog(AXIS_X, +1)
                   ).pack(side='left', padx=(0, 4))
        ttk.Button(row, text='X－', width=6, command=lambda: self.on_jog(AXIS_X, -1)
                   ).pack(side='left', padx=(0, 4))
        ttk.Button(row, text='Y＋', width=6, command=lambda: self.on_jog(AXIS_Y, +1)
                   ).pack(side='left', padx=(0, 4))
        ttk.Button(row, text='Y－', width=6, command=lambda: self.on_jog(AXIS_Y, -1)
                   ).pack(side='left', padx=(0, 10))
        ttk.Label(row, text='脉冲').pack(side='left')
        ttk.Entry(row, textvariable=self.var_jog, width=7).pack(side='left', padx=4)
        ttk.Checkbutton(row, text='X取反', variable=self.var_flip_x).pack(side='left', padx=(6, 0))
        ttk.Checkbutton(row, text='Y取反', variable=self.var_flip_y).pack(side='left', padx=(6, 0))

        row2 = ttk.Frame(jog)
        row2.pack(fill='x', pady=(6, 0))
        for text, cmd in (('使能 X/Y', self.on_enable_xy),
                          ('取消使能 X/Y', self.on_disable_xy),
                          ('读回读脉冲', self.on_query_counts),
                          ('停止所有轴', self.on_stop_all)):
            ttk.Button(row2, text=text, command=cmd).pack(side='left', padx=(0, 5))

        ttk.Label(self, text='日志').pack(anchor='w', pady=(8, 0))
        lg = ttk.Frame(self)
        lg.pack(fill='both', expand=True, pady=(2, 0))
        self.log = tk.Text(lg, height=9, font=('Consolas', 9), bg='#f7f8fa',
                           relief='solid', bd=1, wrap='word')
        sb2 = ttk.Scrollbar(lg, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=sb2.set)
        self.log.pack(side='left', fill='both', expand=True)
        sb2.pack(side='right', fill='y')

    # ------------------------------------------------ 标定状态（读文件算残差）
    def refresh_calib_status(self):
        """一行摘要：抓取点组数 / 覆盖范围 / 残差 / 更新时间。"""
        try:
            import json
            from pathlib import Path as _P
            root = _P(__file__).resolve().parent.parent
            pairs_path = root / 'vision' / 'runtime' / 'grasp_pairs.json'
            calib_path = root / 'vision' / 'tray_calib.json'
            n = 0
            us = []
            vs = []
            if pairs_path.exists():
                data = json.loads(pairs_path.read_text(encoding='utf-8'))
                pairs = data if isinstance(data, list) else data.get('pairs', [])
                n = len(pairs)
                for item in pairs:
                    try:
                        us.append(float(item.get('u')))
                        vs.append(float(item.get('v')))
                    except (TypeError, ValueError):
                        continue
            updated = ''
            if calib_path.exists():
                import datetime
                ts = calib_path.stat().st_mtime
                updated = datetime.datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')
            parts = ['标定状态：抓取点 %d 组' % n]
            if us and vs:
                parts.append('u %.2f–%.2f / v %.2f–%.2f'
                             % (min(us), max(us), min(vs), max(vs)))
            if n < 3:
                parts.append('⚠ 少于 3 组，无法求解')
            if updated:
                parts.append('tray_calib 更新 ' + updated)
            self.calib_status.configure(
                text='｜'.join(parts),
                foreground='#b26a00' if n < 3 else '#0a6b0a')
        except Exception as exc:                # noqa: BLE001
            try:
                self.calib_status.configure(text='标定状态：读取失败 %s' % exc,
                                            foreground='#b00020')
            except Exception:                   # noqa: BLE001
                pass

    # ------------------------------------------------- 固定位置按钮（回 P1 等）
    PRESETS = {
        'p1': ('回到 P1（X 0 / Y -13144 / Z 顶 -152000）',
               {4: 0, 5: -13144, 1: -152000}, (4, 5, 1), {1: 1500, 4: 120, 5: 300}),
        'z_up': ('Z 抬到顶（-152000 脉冲）', {1: -152000}, (1,), {1: 1500}),
        'z_down': ('Z 降到底（0 脉冲）', {1: 0}, (1,), {1: 1500}),
        'to_p1': ('走通道：P0 → P1（Y -13144）', {5: -13144}, (5,), {5: 300}),
        'to_p0': ('退通道：P1 → P0（Y 0）', {5: 0}, (5,), {5: 300}),
    }

    def on_preset(self, key):
        """固定位置按钮：按目标位置闭环走「增量」，已在目标位就跳过。"""
        client = getattr(self.app, 'client', None)
        if client is None or not getattr(client, 'online', False):
            self.say('还没连接：先在顶部下拉框选「派 robot-pi …」并点「连接」')
            return
        if self._prep_running:
            # 兜底：万一 worker 因为异常没复位标志，超过 10 分钟自动放行
            if time.monotonic() - getattr(self, '_prep_started', 0.0) > 600.0:
                self._prep_running = False
                self.say('上一轮动作状态卡住了（超过 10 分钟），已自动复位，请重试')
            else:
                self.say('还有动作在跑，等它跑完再点')
                return
        title, targets, order, rpm = self.PRESETS[key]
        lines = []
        for axis in order:
            target = int(targets[axis])
            lines.append('%s %+d 脉冲（%s counts）'
                         % (PP.AXIS_LABEL.get(axis, axis), target,
                            format(PP.counts_of(target), ',')))
        if not messagebox.askyesno(title, '将走到固定位置（已经在目标位就不会动）：\n\n%s\n\n'
                                          '顺序：%s\n\n手不要挡在运动范围内，急停可按「停止全部」。\n继续？'
                                          % ('\n'.join(lines),
                                             ' → '.join(PP.AXIS_LABEL.get(a, str(a)) for a in order))):
            return
        self._prep_running = True
        self._prep_started = time.monotonic()
        self.say('开始：%s' % title)
        try:
            # 注意：rpm 可能是 {轴: 转速} 的字典，不能再 int() 一次
            threading.Thread(target=self._preset_worker,
                             args=(client, title, dict(targets), tuple(order), rpm),
                             daemon=True).start()
        except Exception as exc:                 # noqa: BLE001
            self._prep_running = False
            self.say('%s 启动失败：%s: %s' % (title, type(exc).__name__, exc))

    def _preset_worker(self, client, title, targets, order, rpm):
        def waiter(req_id, timeout_ms):
            end = time.monotonic() + timeout_ms / 1000.0
            while time.monotonic() < end:
                got = client.done_of(req_id) if hasattr(client, 'done_of') else None
                if got is not None:
                    kind = 'reply' if got.get('kind') == 'reply' else (
                        'err' if got.get('kind') == 'err' else 'timeout')
                    return {'kind': kind, 'code': got.get('code', ''),
                            'text': '', 'pairs': got.get('pairs') or {}}
                time.sleep(0.05)
            return None

        soft = dict(getattr(self.app, 'soft_pos', {}) or {})
        known = dict(getattr(self.app, 'pos_known', {}) or {})
        counts = {}
        now = client.now_ms()
        panels = getattr(self.app, 'axes', {}) or {}
        for axis in order:
            panel = panels.get(axis)
            if panel is None:
                continue
            val = getattr(panel, 'last_counts', None)
            age = now - int(getattr(panel, 'last_counts_ms', 0) or 0)
            if val is not None and age < 3000:
                counts[axis] = int(val)
        try:
            results = PP.move_abs(client, targets, order=order, rpm=rpm,
                                  acc=int(PP.load().get('acc', 0)),
                                  log=lambda t: self.after(0, self.say, t), wait=waiter,
                                  soft=soft, known=known, counts=counts)
        except Exception as exc:                      # noqa: BLE001
            self.after(0, self.say, '%s 异常：%s: %s' % (title, type(exc).__name__, exc))
            results = []
        finally:
            self._prep_running = False
        if results and all(r.get('ok') for r in results):
            self.after(0, self.say, '%s：完成' % title)
        elif not results:
            self.after(0, self.say, '%s：不需要动（已在目标位）' % title)
        else:
            self.after(0, self.say, '%s：没走完，看上面的失败原因' % title)

    # ----------------------------------------------------------- 视觉预览
    def _preview_stream(self):
        app = self.app
        vision = getattr(app, 'vision', None) or getattr(app, 'vision_app', None)
        if vision is None:
            return None
        return getattr(vision, 'a', None)          # 顶视 Stream

    def _preview_update(self):
        stream = self._preview_stream()
        get = getattr(stream, 'get', None)
        frame = get() if callable(get) else None
        if frame is None:
            self._pv_note.configure(text='视觉还没开（先在「视觉」页开双画面）')
            return
        h, w = frame.shape[:2]
        scale = min(PREVIEW_W / float(w), PREVIEW_H / float(h))
        nw = max(1, int(w * scale))
        nh = max(1, int(h * scale))
        small = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        self._preview_overlay(rgb, w, h, nw, nh)
        photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        self._pv_photo = photo                     # 必须保住引用，否则图会变白
        self._pv_label.configure(image=photo, text='')
        self._pv_note.configure(text='%dx%d' % (w, h))

    def _preview_overlay(self, rgb, w, h, nw, nh):
        """在预览图上画四点框 + 标注点（P1..Pn），方便边看边对准。"""
        vision = getattr(self.app, 'vision', None) or getattr(self.app, 'vision_app', None)
        if vision is None:
            return
        sx, sy = nw / float(w), nh / float(h)
        pts = list(getattr(vision, 'points', None) or [])
        if len(pts) == 4:
            try:
                poly = np.array([[int(round(float(x) * sx)), int(round(float(y) * sy))]
                                 for x, y in pts], np.int32)
                cv2.polylines(rgb, [poly], True, (0, 230, 120), 2)
                for i, (x, y) in enumerate(pts, 1):
                    cx, cy = int(round(float(x) * sx)), int(round(float(y) * sy))
                    cv2.circle(rgb, (cx, cy), 4, (0, 230, 120), -1)
                    cv2.putText(rgb, str(i), (cx + 6, cy - 6), cv2.FONT_HERSHEY_SIMPLEX,
                                0.45, (0, 230, 120), 1, cv2.LINE_AA)
            except Exception:                      # noqa: BLE001
                pass
        marks = list(getattr(vision, 'marks', None) or [])
        if not marks or len(pts) != 4:
            return
        try:                                       # 托盘坐标(0..700) -> 原图 -> 预览
            p = np.array(pts, dtype=np.float32)
            dst = np.array([[0, 0], [700, 0], [700, 700], [0, 700]], dtype=np.float32)
            inv = np.linalg.inv(cv2.getPerspectiveTransform(p, dst))
        except Exception:                          # noqa: BLE001
            return
        for i, m in enumerate(marks, 1):
            try:
                v = inv @ np.array([float(m['x']), float(m['y']), 1.0])
                if abs(v[2]) < 1e-9:
                    continue
                px = int(round(v[0] / v[2] * sx))
                py = int(round(v[1] / v[2] * sy))
            except Exception:                      # noqa: BLE001
                continue
            if not (0 <= px < nw and 0 <= py < nh):
                continue
            cv2.drawMarker(rgb, (px, py), (0, 229, 255), cv2.MARKER_CROSS, 14, 2)
            cv2.putText(rgb, 'P%d' % i, (px + 7, py - 7), cv2.FONT_HERSHEY_SIMPLEX,
                        0.45, (0, 229, 255), 1, cv2.LINE_AA)

    def _preview_tick(self):
        # 本页没显示时不做任何绘制（省 CPU，也避免切页时和视觉页抢主线程）
        if self.winfo_ismapped() and self.app.notebook.tab('current', 'text').strip() == 'X/Y 标定':
            try:
                self._preview_update()
            except Exception as exc:               # noqa: BLE001
                try:
                    self._pv_note.configure(text='预览出错：%s' % exc)
                except Exception:                  # noqa: BLE001
                    pass
        self.after(400, self._preview_tick)

    # ------------------------------------------------------- 抓取点标定
    def _current_xy_pulses(self):
        """当前 X/Y 位置（脉冲）：优先 3 s 内回读 counts，其次已知软位置。"""
        soft, known, counts = self._pos_source()
        if 4 in counts and 5 in counts:
            return (units.counts_to_pulses(counts[4]),
                    units.counts_to_pulses(counts[5]), '回读 counts')
        if known.get(4) and known.get(5) and 4 in soft and 5 in soft:
            return float(soft[4]), float(soft[5]), '软位置'
        return None, None, ''

    def on_record_grasp(self):
        """把"当前爪子位置"和"画面里物块的 u,v"配成一对抓取点。"""
        client = getattr(self.app, 'client', None)
        if client is None or not getattr(client, 'online', False):
            self.say('还没连接：先选「派 robot-pi …」点「连接」')
            return
        x, y, src = self._current_xy_pulses()
        if x is None:
            self.say('拿不到当前位置：先在四轴页点「查位置/查状态」刷新回读')
            return
        targets = AG.load_targets()
        if not targets:
            self.say('没有视觉目标：先到「视觉」页做一次「单次识别」')
            return
        target = targets[0]
        if len(targets) > 1:
            lines = ['%d) %s  u=%.2f v=%.2f' % (i + 1, AG.describe(t),
                                                t['center_tray'][0], t['center_tray'][1])
                     for i, t in enumerate(targets[:9])]
            pick = simpledialog.askinteger(
                '选择物块', '这次对准的是哪个物块？\n\n' + '\n'.join(lines),
                parent=self, minvalue=1, maxvalue=min(9, len(targets)))
            if not pick:
                return
            target = targets[pick - 1]
        u, v = (float(target['center_tray'][0]), float(target['center_tray'][1]))
        try:
            pairs = GC.add_pair(u, v, x, y,
                                note='%s %s' % (target.get('class_name') or '', src))
        except ValueError as exc:
            self.say('记录失败：%s' % exc)
            return
        self.say('已记录第 %d 个抓取点：u=%.3f v=%.3f → X%+.0f / Y%+.0f 脉冲（%s）'
                 % (len(pairs), u, v, x, y, src))
        self.say(GC.describe(pairs))
        if len(pairs) < GC.MIN_PAIRS:
            self.say('再摆 %d 个位置的物块记录，凑够 %d 个点才能求解'
                     % (GC.MIN_PAIRS - len(pairs), GC.MIN_PAIRS))

    def on_preview_grasp(self):
        """只试算不写盘：列出每个点的残差，挑最差的重记。"""
        pairs = GC.load_pairs()
        if not pairs:
            self.say('还没有抓取点：先摆物块、对准、点「记录抓取点」')
            return
        try:
            r = GC.preview(pairs)
        except ValueError as exc:
            self.say('还不能试算：%s' % exc)
            return
        self.say(GC.describe(pairs))
        self.say('残差预览（%d 点，最大 %.0f 脉冲 ≈ %.1f mm）：'
                 % (r['n'], r['max'], r['max'] * 0.0166))
        for row in r['rows'][:6]:
            self.say('   第 %d 点 u=%.3f v=%.3f → dx %+.0f / dy %+.0f（%.0f 脉冲）'
                     % (row['idx'], row['u'] or 0, row['v'] or 0,
                        row['dx'], row['dy'], row['dist']))
        if r['max'] > GC.OUTLIER_PULSE:
            self.say('最大残差 >%.0f 脉冲：先「删掉最后一点」或重记那个点，再求解'
                     % GC.OUTLIER_PULSE)

    def on_drop_last_grasp(self):
        pairs = GC.remove_last()
        self.say('已删掉最后一个点，剩 %d 个' % len(pairs))
        if pairs:
            self.say(GC.describe(pairs))

    def on_solve_grasp(self):
        """用记录的抓取点拟合 (u,v)→(X,Y)，写进 tray_calib.json。"""
        pairs = GC.load_pairs()
        if not pairs:
            self.say('还没有抓取点：先摆物块、对准、点「记录抓取点」')
            return
        try:
            result = GC.solve(pairs, write=True)
        except ValueError as exc:
            self.say('求解失败：%s' % exc)
            return
        aff = result.get('affine') or {}
        self.say('抓取点标定成功（%d 个点）：X = %.1f·u %+.1f·v %+.1f，'
                 'Y = %.1f·u %+.1f·v %+.1f'
                 % (len(pairs), aff.get('a', 0), aff.get('b', 0), aff.get('tx', 0),
                    aff.get('c', 0), aff.get('d', 0), aff.get('ty', 0)))
        mx = result.get('max_abs') or {}
        self.say('残差最大：dx %.0f / dy %.0f 脉冲（越小越好）'
                 % (mx.get('dx', 0), mx.get('dy', 0)))
        used = result.get('used')
        if used and used != len(pairs):
            self.say('求解实际使用 %d 个点（剔除 %d 个离群点）'
                     % (used, len(pairs) - used))
            for d in (result.get('dropped') or []):
                self.say('   剔除：u=%.3f v=%.3f 残差 %.0f 脉冲'
                         % (d['u'], d['v'], d['residual']))
        if result.get('warning'):
            self.say(result['warning'])
        if result.get('path'):
            self.say('已写入 %s（旧文件已备份）' % result['path'])
        self.say('下一步：放一件货点「自动抓一件」验证落点')

    # ------------------------------------------------------------- 自动抓一件
    def _has_fresh_counts(self, max_age_ms: int = 3000) -> bool:
        """四轴里是否已有一路新鲜的编码器回读（自动抓靠它算绝对位置）。"""
        client = getattr(self.app, 'client', None)
        try:
            now = client.now_ms() if client is not None else 0
        except Exception:                              # noqa: BLE001
            now = 0
        for axis, panel in (getattr(self.app, 'axes', {}) or {}).items():
            val = getattr(panel, 'last_counts', None)
            age = now - int(getattr(panel, 'last_counts_ms', 0) or 0)
            if val is not None and 0 <= age < max_age_ms:
                return True
        return False

    def _ensure_pos(self) -> bool:
        """自动动作前确保有位置来源：没开遥测就临时订阅一下，等 ~1.2 s。"""
        import time as _time
        client = getattr(self.app, 'client', None)
        if client is None or not getattr(client, 'online', False):
            return False
        if self._has_fresh_counts():
            return True
        for axis in (1, 3, 4, 5):
            try:
                client.send('tele_on', int(axis), 200)
            except Exception:                          # noqa: BLE001
                pass
        for _ in range(12):
            self.update_idletasks()
            _time.sleep(0.1)
            if self._has_fresh_counts():
                self.say('已自动订阅遥测，位置可读')
                return True
        return self._has_fresh_counts(5000)

    def _pos_source(self):
        """给 auto_grab/move_abs 用：回读 counts（3 s 内）+ 软位置 + 位置可信标志。"""
        client = getattr(self.app, 'client', None)
        soft = dict(getattr(self.app, 'soft_pos', {}) or {})
        known = dict(getattr(self.app, 'pos_known', {}) or {})
        counts = {}
        try:
            now = client.now_ms() if client is not None else 0
        except Exception:                             # noqa: BLE001
            now = 0
        for axis, panel in (getattr(self.app, 'axes', {}) or {}).items():
            val = getattr(panel, 'last_counts', None)
            age = now - int(getattr(panel, 'last_counts_ms', 0) or 0)
            if val is not None and age < 3000:
                counts[int(axis)] = int(val)
        return soft, known, counts

    def on_auto_grab(self):
        """识别一件 → 确认 → 自动抓放（顺序见 auto_grab 模块）。"""
        client = getattr(self.app, 'client', None)
        if client is None or not getattr(client, 'online', False):
            self.say('还没连接：先在顶部下拉框选「派 robot-pi …」并点「连接」')
            return
        if self._prep_running:
            import time as _time
            _t0 = getattr(self, '_prep_since', None)
            if _t0 is not None and (_time.time() - _t0) > 180.0:
                self.say('上一条动作已超过 180 s，判定为卡死，已复位状态，可以重新点')
                self._prep_running = False
            else:
                self.say('还有动作在跑（自 %s 起），等它跑完再点'
                         % (time.strftime('%H:%M:%S', time.localtime(_t0)) if _t0 else '未知时间'))
                return

        if not self._ensure_pos():
            self.say('读不到当前位置（没有编码器回读）：先在「四轴控制」页点「查位置」或勾「订阅遥测」再试')
            return
        vision = (getattr(self.app, 'vision', None)
                  or getattr(self.app, 'vision_app', None))
        if vision is not None and hasattr(vision, 'recognize'):
            self.say('先做一次视觉识别（进度见「单次识别」旁的进度条）…')
            try:
                started = vision.recognize(
                    callback=lambda ok, info: self.after(
                        0, self._after_recognize_grab, ok, info))
            except TypeError:                     # 老版本没有 callback 参数
                started = True
                try:
                    ok = bool(vision.recognize_safe()) if hasattr(vision, 'recognize_safe') else True
                except Exception as exc:          # noqa: BLE001
                    self.say('识别失败：%s: %s' % (type(exc).__name__, exc))
                    return
                self._after_recognize_grab(ok, '')
                started = True
            except Exception as exc:              # noqa: BLE001
                self.say('识别失败：%s: %s' % (type(exc).__name__, exc))
                return
            if not started:
                return
            return
        self._start_auto_grab(client)

    def _after_recognize_grab(self, ok, info):
        """视觉识别结束后（主线程）继续：读目标 → 弹确认 → 起执行线程。"""
        if not ok:
            self.say('识别没成功（堆栈见 vision/runtime/vision_error.log）：%s' % (info or ''))
            return
        client = getattr(self.app, 'client', None)
        if client is None or not getattr(client, 'online', False):
            self.say('连接断开了，先重新连接')
            return
        self._start_auto_grab(client)

    def _start_auto_grab(self, client):
        try:
            targets = AG.load_targets()
            target = AG.pick_target(targets)
            x, y = AG.target_xy(target)
        except AG.GrabError as exc:
            self.say(str(exc))
            return
        except Exception as exc:                      # noqa: BLE001
            self.say('准备失败：%s: %s' % (type(exc).__name__, exc))
            return

        drop = bool(self.drop_var.get())
        msg = ('目标：%s\n\nX %+.0f 脉冲 / Y %+.0f 脉冲\n\n'
               '会做（前两步就是固定路线，穿通道时 Z 在底部）：\n'
               '1) Y 进通道到末端 → 2) Z 抬顶 → 3) X/Y 到物块上方 → '
               '4) Z 降底 → 5) 夹紧 → 6) Z 抬顶 → 7) X/Y 回通道末端 → '
               '8) Z 降底 → 9) Y 退 P0%s\n\n'
               '手不要放在机构运动范围内，急停可点「停止全部」。\n继续？'
               % (AG.describe(target), x, y,
                  (' → 10) 转盘 60° → 11) 松爪回预备' if drop
                   else '（到这里停，物块还在爪里）')))
        if not messagebox.askyesno('自动抓一件', msg):
            return
        self._prep_running = True
        self._prep_started = time.monotonic()
        self.say('开始自动抓一件…')
        try:
            threading.Thread(target=self._auto_worker, args=(client, target, drop),
                             daemon=True).start()
        except Exception as exc:                      # noqa: BLE001
            self._prep_running = False
            self.say('启动线程失败：%s: %s' % (type(exc).__name__, exc))

    def _auto_worker(self, client, target, drop=True):
        def waiter(req_id, timeout_ms):
            end = time.monotonic() + timeout_ms / 1000.0
            while time.monotonic() < end:
                got = client.done_of(req_id) if hasattr(client, 'done_of') else None
                if got is not None:
                    kind = 'reply' if got.get('kind') == 'reply' else (
                        'err' if got.get('kind') == 'err' else 'timeout')
                    return {'kind': kind, 'code': got.get('code', ''),
                            'text': '', 'pairs': got.get('pairs') or {}}
                time.sleep(0.05)
            return None

        disp = getattr(self.app, 'display', None)
        _shape = str(target.get('shape') or '—')
        _color = str(target.get('color') or '')
        try:
            import box_map as _BM
            _key = str(target.get('class_key') or '')
            if not _key:
                _key = '%s|%s' % (target.get('shape') or '?', target.get('color') or '?')
            _box = int(_BM.lookup(_key) or 1)
        except Exception:                             # noqa: BLE001
            _box = int(target.get('box') or 1)
        try:
            if disp is not None:
                disp.item(seq=1, name=_shape, color=_color, box=_box,
                          box_label='%d 号盒' % _box, total_done=0,
                          last_event='第 1 件：%s %s → %d 号盒' % (_shape, _color, _box))
                disp.state(done=0, total=1, status='running',
                           last_event='正在抓第 1 件')
        except Exception:                             # noqa: BLE001
            pass
        _ok = False
        try:
            AG.run_once(client, target, log=lambda t: self.after(0, self.say, t),
                        wait=waiter, pos_source=self._pos_source, drop=drop)
            _ok = True
        except Exception as exc:                      # noqa: BLE001
            self.after(0, self.say, '自动抓一件失败：%s: %s'
                       % (type(exc).__name__, exc))
        finally:
            try:
                if disp is not None:
                    if _ok:
                        disp.item_done(seq=1, total_done=1, ok=True,
                                       last_event='第 1 件已入 %d 号盒' % _box)
                        disp.state(done=1, total=1, status='running',
                                   last_event='已完成 1 件')
                        self.app._disp_done, self.app._disp_total = 1, 1
                    else:
                        disp.alarm(text='第 1 件失败')
            except Exception:                         # noqa: BLE001
                pass
            self._prep_running = False
            self.after(0, self._refresh_pos)

    # ------------------------------------------------------- 一键走出固定路线
    def on_auto_many(self):
        """连续抓 N 件：一次识别→队列逐件；每件都走与「自动抓一件」相同的 run_once。"""
        try:
            self._on_auto_many()
        except Exception as exc:                       # noqa: BLE001
            # 点按钮时绝不能"静默无反应"：任何异常都要看得见
            self.say('连续抓按钮出错：%s: %s' % (type(exc).__name__, exc))
            try:
                messagebox.showerror('连续抓出错', '%s: %s' % (type(exc).__name__, exc))
            except Exception:                          # noqa: BLE001
                pass

    def _set_many_state(self, running: bool, text: str = '') -> None:
        """连续抓的可见状态：按钮文字 + 本页标签 + 顶栏状态。"""
        try:
            self.many_btn.configure(text=('■ 停止连续抓' if running else '连续抓'))
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.many_lbl.configure(text=text)
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.app.status_label.configure(text=text or '就绪')
        except Exception:                                   # noqa: BLE001
            pass

    def _many_event(self, ev: dict) -> None:
        """run_many 的事件 → 界面状态（异常一律吞）。"""
        try:
            kind = ev.get('kind')
            if kind == 'item_start':
                self._set_many_state(True, '连续抓 %d/%d · 识别中…'
                                     % (ev.get('i', 0), ev.get('n', 0)))
            elif kind == 'target':
                self._set_many_state(True, '连续抓 %d 件 · %s → %s 号盒'
                                     % (ev.get('i', 0), ev.get('target', ''), ev.get('box', 0)))
            elif kind == 'item_ok':
                self._set_many_state(True, '连续抓：第 %d 件完成' % ev.get('i', 0))
            elif kind == 'item_fail':
                self._set_many_state(True, '连续抓：第 %d 件失败（%s）'
                                     % (ev.get('i', 0), ev.get('reason', '')))
            elif kind == 'finished':
                self._set_many_state(False, '连续抓结束：成功 %s / 失败 %s，用时 %.1f s'
                                     % (ev.get('done'), ev.get('failed'),
                                        ev.get('elapsed') or 0))
        except Exception:                                   # noqa: BLE001
            pass

    def _on_auto_many(self):
        # 已经在跑 → 这个按钮就是「停止」：当前件走完后停
        if getattr(self, '_prep_running', False) and                 getattr(self, '_many_thread', None) is not None and self._many_thread.is_alive():
            self._many_stop = True
            self.say('收到停止请求：当前件走完就停（再点一次不会重复）')
            self._set_many_state(True, '连续抓：正在停止（等当前件走完）…')
            return
        # 点按钮立刻给反馈：绝不允许“看起来没反应”
        self.say('连续抓：收到点击，正在检查前置条件…')
        if self._prep_running:
            self.say('还有动作在跑，等它跑完再点')
            return
        client = getattr(self.app, 'client', None)
        if client is None or not getattr(client, 'online', False):
            self.say('还没连接：先在顶部下拉框选「派 robot-pi …」并点「连接」')
            return
        vision = (getattr(self.app, 'vision', None)
                  or getattr(self.app, 'vision_app', None))
        if vision is None or not hasattr(vision, 'recognize_safe'):
            self.say('视觉没开：先到「视觉」页点「打开双画面」')
            return
        try:
            count = int(self.many_var.get())
        except Exception:                                  # noqa: BLE001
            count = 6
        count = max(1, min(20, count))
        if not self._ensure_pos():
            self.say('读不到当前位置（没有编码器回读）：先在「四轴控制」页点「查位置」或勾「订阅遥测」再试')
            messagebox.showwarning('读不到位置', '没有编码器回读，无法安全自动移动。\n\n'
                                   '先在「四轴控制」页点一次「查位置」，或勾上四轴的「订阅遥测」，再点「连续抓」。')
            return
        drop = bool(self.drop_var.get())
        if not drop:
            # 连续抓必须落货：自动帮用户勾上（弹模态框会让人以为界面卡死）
            self.drop_var.set(True)
            drop = True
            self.say('连续抓必须落货，已自动勾选「抓完落货（转盘60°+松爪）」')
        self.say('连续抓 %d 件（含转盘 60° + 松爪）：一次识别排队逐件，每件流程与「自动抓一件」相同；'
                 '单件失败会重新识别并重试一次；急停请点顶部「停止全部」' % count)
        self._prep_running = True
        self._many_stop = False
        self._set_many_state(True, '连续抓：启动中…（%d 件）' % count)
        self.say('▶ 开始连续抓 %d 件（一次识别→队列逐件，失败重试一次）' % count)
        self._many_thread = threading.Thread(
            target=self._many_worker, args=(client, vision, count, drop), daemon=True)
        self._many_thread.start()

    def on_box_reset(self):
        """把"类别→盒号"的登记清空（每次比赛/换一批货前用）。"""
        import box_map as BM
        if not messagebox.askyesno('分盒清零', '清空「类别→盒号」的登记？\n\n'
                                   '（只清登记，不动转盘、不动机械臂）\n当前：%s'
                                   % BM.describe()):
            return
        BM.reset()
        self.say('分盒登记已清空，现在：%s' % BM.describe())

    def _many_worker(self, client, vision, count, drop):
        try:
            _disp = getattr(self.app, 'display', None)
            _warp = None
            try:
                _warp = Path(vision.runtime_dir) / 'last_top_warp.jpg' \
                    if getattr(vision, 'runtime_dir', None) else None
            except Exception:                               # noqa: BLE001
                _warp = None
            res = AG.run_many(
                client, count,
                recognize=(vision.recognize_blocking
                       if hasattr(vision, 'recognize_blocking') else vision.recognize_safe),
                log=self.say,
                pos_source=self._pos_source,
                drop=drop,
                stop_flag=lambda: bool(self._many_stop),
                on_event=self._many_event,
                display=_disp,
                warp_path=_warp,
            )
            self.say('连续抓结束：成功 %s 件、失败 %s 件，用时 %.1f s'
                     % (res.get('done'), res.get('failed'), res.get('elapsed') or 0))
        except Exception as exc:                            # noqa: BLE001
            self.say('连续抓失败：%s: %s' % (type(exc).__name__, exc))
        finally:
            self._prep_running = False
            self._many_stop = False
            try:
                if not (self.many_lbl.cget('text') or '').startswith('连续抓结束'):
                    self._set_many_state(False, '连续抓已结束')
            except Exception:                               # noqa: BLE001
                pass
            try:
                self._refresh_pos()
            except Exception:                          # noqa: BLE001
                pass

    def on_prep_route(self):
        """从初始位走固定通道（Y 让开 → Z 抬起），走完停在工作位，等你标定。"""
        client = getattr(self.app, 'client', None)
        if client is None or not getattr(client, 'online', False):
            self.say('还没连接：先在顶部下拉框选「派 robot-pi …」并点「连接」')
            return
        if self._prep_running:
            self.say('固定路线正在走，等它走完')
            return
        cfg = PP.load()
        if not messagebox.askyesno(
                '一键走出固定路线',
                '将从初始位走固定通道（目标位置，已经在目标位就不会再走）：\n\n%s\n\n'
                '过程中手不要挡在机构运动范围内，急停随时可按「停止全部」。\n继续？'
                % PP.describe(cfg)):
            return
        self._prep_running = True
        self.say('开始走固定路线：%s' % PP.describe(cfg))
        th = threading.Thread(target=self._prep_worker, args=(client, cfg), daemon=True)
        th.start()

    def _prep_worker(self, client, cfg):
        """后台线程执行；事件只在主线程里改界面。"""
        def waiter(req_id, timeout_ms):
            end = time.monotonic() + timeout_ms / 1000.0
            while time.monotonic() < end:
                got = client.done_of(req_id) if hasattr(client, 'done_of') else None
                if got is not None:
                    kind = 'reply' if got.get('kind') == 'reply' else (
                        'err' if got.get('kind') == 'err' else 'timeout')
                    return {'kind': kind, 'code': got.get('code', ''),
                            'text': '', 'pairs': got.get('pairs') or {}}
                time.sleep(0.05)
            return None

        soft = dict(getattr(self.app, 'soft_pos', {}) or {})
        known = dict(getattr(self.app, 'pos_known', {}) or {})
        # 优先用四轴页收到的“回读 counts”：软位置只按命令累加，超时/断电后会不准。
        counts = {}
        now = client.now_ms()
        panels = getattr(self.app, 'axes', {}) or {}
        for axis in (PP.AXIS_Y, PP.AXIS_Z):
            panel = panels.get(axis)
            if panel is None:
                continue
            val = getattr(panel, 'last_counts', None)
            age = now - int(getattr(panel, 'last_counts_ms', 0) or 0)
            if val is not None and age < 3000:
                counts[axis] = int(val)
        try:
            results = PP.run(client, cfg, log=lambda t: self.after(0, self.say, t), wait=waiter,
                             soft=soft, known=known, counts=counts)
        except Exception as exc:                      # noqa: BLE001
            self.after(0, self.say, '固定路线异常：%s: %s' % (type(exc).__name__, exc))
            results = []
        finally:
            self._prep_running = False

        ok = all(r.get('ok') for r in results)
        if ok and not results:
            self.after(0, self.say, '固定路线：Y/Z 都已在目标位，不需要再动')
            return
        if ok:
            self.after(0, self.say, '固定路线完成：现在应该在工作位了，可以开始标定（用点动把爪子对到标注点上方）')
        else:
            self.after(0, self.say, '固定路线没走完：先看上面的失败原因，必要时点「停止所有轴」再重试')

    # ---------------------------------------------------------------- 工具
    def say(self, text):
        self.log.insert('end', text + '\n')
        self.log.see('end')
        try:
            self.app.note(text)
        except Exception:
            pass

    def _soft(self):
        app = self.app
        soft = getattr(app, 'soft_pos', None) or {}
        known = getattr(app, 'pos_known', None) or {}
        return (int(soft.get(AXIS_X, 0)), int(soft.get(AXIS_Y, 0)),
                bool(known.get(AXIS_X, True)), bool(known.get(AXIS_Y, True)))

    def _refresh_pos(self):
        x, y, kx, ky = self._soft()
        flag = '' if (kx and ky) else '（未知：用过连续/绝对模式，请先回位并位置清零）'
        self.pos_label.configure(text=f'软位置：X={x:+d} Y={y:+d} 脉冲{flag}')

    def _tick(self):
        try:
            self._refresh_pos()
        finally:
            self.after(700, self._tick)

    def _current_pulses(self):
        x, y, kx, ky = self._soft()
        client = self.app.client
        data = getattr(client, 'last_counts', {}) or {}
        rb = {'x': data.get(AXIS_X), 'y': data.get(AXIS_Y)}
        now = client.now_ms() if hasattr(client, 'now_ms') else 0
        picked = W.pick_pulses((x, y), rb, now)
        return picked['px'], picked['py'], picked['source'], (kx and ky)

    def _jog_pulses(self):
        try:
            n = int((self.var_jog.get() or '').strip())
        except ValueError:
            messagebox.showwarning('脉冲数不对', '点动脉冲要填整数，例如 500')
            return None
        if n <= 0:
            messagebox.showwarning('脉冲数不对', '点动脉冲要大于 0（方向用按钮/取反控制）')
            return None
        return n

    # ---------------------------------------------------------------- 点动
    def on_jog(self, axis, sign):
        client = self.app.client
        if not getattr(client, 'online', False):
            messagebox.showwarning('未连接', '先在主界面连接机器人。')
            return
        pulses = self._jog_pulses()
        if pulses is None:
            return
        flip = self.var_flip_x.get() if axis == AXIS_X else self.var_flip_y.get()
        direction = W.jog_direction(sign, flip)
        name = AXIS_NAMES.get(axis, str(axis))
        self.say(f'点动：{name}{"+" if sign > 0 else "−"} {pulses} 脉冲（方向 {direction}，60rpm）')
        client.send('motor_enable', axis, 1)
        self._jog_token = token = self._jog_token + 1
        self.after(120, lambda: self._send_jog_pos(token, axis, direction, pulses, name))
        self.after(650, lambda: self._report_jog(token, name))

    def _send_jog_pos(self, token, axis, direction, pulses, name):
        if token != self._jog_token:
            return
        self._jog_pending = (name, self.app.client.send(
            'motor_pos', axis, direction, 60, 100, pulses, 'rel'))

    def _report_jog(self, token, name):
        if token != self._jog_token:
            return
        self._refresh_pos()
        client = self.app.client
        err = getattr(client, 'last_err', None)
        ok = getattr(client, 'last_ok', None)
        now = client.now_ms() if hasattr(client, 'now_ms') else 0
        if err and err.get('cmd', '').startswith('motor') and (
                not ok or err['ts'] >= ok.get('ts', 0)) and abs(now - err['ts']) < 2000:
            self.say(f'✗ {name} 被固件拒绝：{err.get("code", "")} {err.get("text", "")}')
            self.say('  E13=位置未清零；E02=参数错；E04=轴不可用；锁存错误先点「解除停止锁存」')
        elif ok and ok.get('cmd') == 'motor_pos' and abs(now - ok['ts']) < 2000:
            self.say(f'✓ {name} 点动已被固件确认（ok）')
        elif getattr(self, '_jog_pending', None) is not None:
            self.say(f'? {name} 点动没有收到应答（看主界面日志的 RX/超时）')

    def on_enable_xy(self):
        if not getattr(self.app.client, 'online', False):
            self.say('使能失败：未连接')
            return
        for axis in (AXIS_X, AXIS_Y):
            self.app.client.send('motor_enable', axis, 1)
        self.say('已下发：X/Y 使能（on=1）')
        self.after(400, self._refresh_pos)

    def on_disable_xy(self):
        client = self.app.client
        if not getattr(client, 'online', False):
            messagebox.showwarning('未连接', '先在主界面连接机器人。')
            return
        if not messagebox.askyesno('取消使能',
                                   '会先「停止所有轴」，再取消 X/Y 使能。\n'
                                   '取消后轴可能失力下滑——确认手在急停旁。\n\n继续吗？'):
            return
        client.send('stop_all')
        for axis in (AXIS_X, AXIS_Y):
            client.send('motor_enable', axis, 0)
        self.say('已下发：停止所有轴 + 取消 X/Y 使能（丝杠自锁推不动属正常）')

    def on_query_counts(self):
        client = self.app.client
        if not getattr(client, 'online', False):
            self.say('读取回读失败：未连接')
            return
        rb = tuple(getattr(client, 'readback_axes', ()) or ())
        self.say('回读轴：' + ('、'.join(str(a) for a in rb) if rb else '无（握手未报告回读轴）'))
        for axis, name in ((AXIS_X, 'X'), (AXIS_Y, 'Y')):
            if rb and axis not in rb:
                self.say(f'· {name}({axis}) 没接回读，固件会回 E04（只能靠软位置）')
                continue
            client.send('motor_query', axis, 'pos', timeout_ms=400)
        self.after(500, self._report_counts)

    def _report_counts(self):
        client = self.app.client
        data = getattr(client, 'last_counts', {}) or {}
        now = client.now_ms() if hasattr(client, 'now_ms') else 0
        rows = []
        for axis, name in ((AXIS_X, 'X'), (AXIS_Y, 'Y')):
            item = data.get(axis)
            if not item:
                rows.append(f'{name}=—')
                continue
            age = max(0, now - int(item.get('ts', 0)))
            rows.append('%s=%s counts（≈%+.0f 脉冲，%dms 前）'
                        % (name, item.get('counts'), W.counts_to_pulses(item.get('counts', 0)), age))
        self.say('回读：' + '，'.join(rows))

    def on_stop_all(self):
        if not getattr(self.app.client, 'online', False):
            messagebox.showwarning('未连接', '先在主界面连接机器人。')
            return
        self.app.client.send('stop_all')
        self.say('已下发：停止所有轴。')

    # ---------------------------------------------------------------- 标注点
    def _marks_text(self, i, m):
        x = '—' if m.get('px') is None else '%+d' % int(m['px'])
        y = '—' if m.get('py') is None else '%+d' % int(m['py'])
        return 'P%d  u=%.4f v=%.4f   X=%s Y=%s' % (i, m['u'], m['v'], x, y)

    def _refresh_marks(self, keep_index=None):
        if keep_index is None:
            sel = self.listbox.curselection()
            keep_index = sel[0] if sel else None
        self.listbox.delete(0, 'end')
        for i, m in enumerate(self.marks, 1):
            self.listbox.insert('end', self._marks_text(i, m))
        done = sum(1 for m in self.marks if m.get('px') is not None)
        self.marks_label.configure(
            text='标注点：共 %d 个，已记录爪位 %d 个（至少 3 个，且不要在一条线上）'
                 % (len(self.marks), done))
        if keep_index is not None and 0 <= keep_index < len(self.marks):
            self.listbox.selection_set(keep_index)
            self.listbox.see(keep_index)

    def on_marks_refresh(self):
        self.marks = W.load_marks()
        self._refresh_marks(0 if self.marks else None)
        self._refresh_pos()
        if not self.marks:
            self.say('没有读到标注点：先在「视觉」页顶视画面点「标注点」，在画面上点几个位置。')
            self.say('文件位置：%s' % W.MARKS_PATH)
        else:
            self.say('读到 %d 个标注点（来自 %s）' % (len(self.marks), W.MARKS_PATH))

    def on_marks_record(self):
        if not self.marks:
            messagebox.showinfo('没有标注点', '先在「视觉」页点标注点，再回来点「刷新标注点」。')
            return
        sel = self.listbox.curselection()
        if sel:
            idx = sel[0]
        else:
            idx = next((i for i, m in enumerate(self.marks) if m.get('px') is None), None)
            if idx is None:
                messagebox.showinfo('都记完了', '每个标注点都有爪位了；点「求解并保存」即可。')
                return
        x, y, src, known = self._current_pulses()
        if src == 'soft' and not known:
            if not messagebox.askyesno('位置未知',
                                       'X/Y 软位置是“未知”（用过连续/绝对模式或断线过），也没有新鲜回读。\n'
                                       '先点「读回读脉冲」或位置清零，仍要记录吗？'):
                return
        self.marks[idx]['px'] = float(x)
        self.marks[idx]['py'] = float(y)
        try:
            W.save_marks(self.marks)
        except Exception as exc:
            self.say('⚠ 写标注点文件失败：%s' % exc)
        self.say('P%d：记录爪位 X=%+.0f Y=%+.0f 脉冲（u=%.4f v=%.4f，来源 %s）'
                 % (idx + 1, x, y, self.marks[idx]['u'], self.marks[idx]['v'],
                    '回读' if src == 'readback' else '软位置'))
        nxt = next((i for i, m in enumerate(self.marks) if m.get('px') is None), idx)
        self._refresh_marks(nxt)
        self._refresh_pos()

    def on_marks_clearrow(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self.marks[idx].pop('px', None)
        self.marks[idx].pop('py', None)
        try:
            W.save_marks(self.marks)
        except Exception:
            pass
        self.say('P%d 的爪位已清除' % (idx + 1))
        self._refresh_marks(idx)

    def on_marks_solve(self):
        try:
            points = W.marks_points(self.marks)
            result = W.solve(points, write=bool(self.var_write.get()),
                             travel=_travel_limits())
        except Exception as exc:
            messagebox.showerror('标注点标定失败', str(exc))
            return
        self._show_result(result, points)

    def _show_result(self, result, points):
        span = int(max(max(abs(p['px']), abs(p['py'])) for p in points)) or 1000
        self.say('———— 标定结果 ————')
        for line in W.report(result, span).splitlines():
            self.say(line)
        if result.get('path'):
            self.say('已写入：%s' % result['path'])
            if result.get('backup'):
                self.say('旧标定已备份：%s' % result['backup'])
        self.say('之后在「视觉目标」页“生成抓取序列”就会用这份标定。')


def build(notebook, app, index=2):
    """把标定页插到 Notebook（默认第 3 个标签）。"""
    frame = ttk.Frame(notebook)
    notebook.insert(index, frame, text='  X/Y 标定  ')
    try:
        tab = CalibTab(frame, app)
        tab.pack(fill='both', expand=True)
        return tab
    except Exception as exc:            # 缺 calib_wizard 等
        ttk.Label(frame, padding=24, justify='left',
                  text='标定页没有启动成功：\n\n%s: %s' % (type(exc).__name__, exc)).pack(anchor='nw')
        return None
