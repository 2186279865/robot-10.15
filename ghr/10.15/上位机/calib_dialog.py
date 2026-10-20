"""“X/Y 标定向导”对话框：用单轴页手动走轴 + 视觉给 u/v，向导只负责记账和求解。

主流程（默认“手动走 + 视觉记录”模式）：
    1. new_eye：点一次「单次识别」，物块 u/v 写进 latest_targets.json；
    2. 向导：「① 读取视觉 u/v」→「② 记录坐标」，把 (u, v, 当前 X/Y 软位置) 存一个点；
    3. 用**单轴页**手动走 X/Y 到下一个位置（每走一次，软位置自动累计）；
    4. new_eye 再点一次「单次识别」，向导「① 读取」「② 记录」→ 第 2 个点；
    5. 想核对就点「读取现在走过的脉冲」（显示当前软位置与相对上一点的 Δ 脉冲）；
    6. 「一键回 0」把 X/Y 开回 0 基准点，再重复 3~5 次（3 个点就够，建议 4~5 个）；
    7. 「③ 求解并保存」，写 new_eye/tray_calib.json。

备选“手推 + 回读”模式：只有当平台能被手推动（皮带/同步带）时才用；滚珠丝杠自锁推不动属正常。
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, ttk

import calib_wizard as W

AXIS_Z, AXIS_X, AXIS_Y = 1, 4, 5
PULSES_PER_REV = 3200


class CalibDialog:
    def __init__(self, app):
        self.app = app
        self.samples = []          # 手动走模式：[{u, v, px, py}]
        self.push_samples = []     # 手推模式：[{u, v, counts_x, counts_y}]
        self._last_uv = None       # (u, v, label, monotonic)：缓存“① 读取”的结果
        self.win = tk.Toplevel(app.root)
        self.win.title('X/Y 标定向导（手动走轴 + 视觉坐标）')
        self.win.geometry('940x740')
        self.win.transient(app.root)
        self._build()

    # ---------- 界面 ----------
    def _build(self):
        box = ttk.Frame(self.win, padding=10)
        box.pack(fill='both', expand=True)

        ttk.Label(box, justify='left', wraplength=820, foreground='#884400', text=(
            '流程：new_eye 点「单次识别」→ 这里点「① 读取视觉 u/v」「② 记录坐标」→ '
            '用单轴页手动走 X/Y 到新位置 → 再「单次识别」→ 再「读取」「记录」。'
            '脉冲由上位机的软位置自动记账（每条轴的相对运动都会累加），不需要编码器回读。'
        )).pack(anchor='w')

        mode_row = ttk.Frame(box)
        mode_row.pack(fill='x', pady=(8, 2))
        ttk.Label(mode_row, text='模式：').pack(side='left')
        self.var_mode = tk.StringVar(value='step')
        ttk.Radiobutton(mode_row, text='手动走轴 + 视觉记录（推荐）', value='step',
                        variable=self.var_mode, command=self.on_mode).pack(side='left')
        ttk.Radiobutton(mode_row, text='手推 + 编码器回读（备选）', value='push',
                        variable=self.var_mode, command=self.on_mode).pack(
                            side='left', padx=(12, 0))
        ttk.Label(mode_row, text='只标目标编号(可空)').pack(side='left', padx=(16, 4))
        self.var_id = tk.StringVar(value='')
        ttk.Entry(mode_row, textvariable=self.var_id, width=6).pack(side='left')

        self.body = ttk.Frame(box)
        self.body.pack(fill='both', expand=True, pady=(6, 0))
        self.frame_step = ttk.Frame(self.body)
        self.frame_push = ttk.Frame(self.body)
        self.var_write = tk.BooleanVar(value=True)
        self._build_step(self.frame_step)
        self._build_push(self.frame_push)

        ttk.Label(box, text='日志').pack(anchor='w', pady=(8, 0))
        self.log = tk.Text(box, height=10, font=('Consolas', 9),
                           bg='#f7f8fa', relief='solid', bd=1)
        self.log.pack(fill='both', expand=True, pady=(2, 0))
        self.on_mode()

    # ---- 手动走 + 视觉记录 ----
    def _build_step(self, frame):
        bar1 = ttk.Frame(frame)
        bar1.pack(fill='x', pady=4)
        self.btn_read = ttk.Button(bar1, text='① 读取视觉 u/v', command=self.on_read)
        self.btn_record = ttk.Button(bar1, text='② 记录坐标',
                                     command=self.on_record)
        self.btn_pulses = ttk.Button(bar1, text='读取当前脉冲', command=self.on_pulses)
        for btn in (self.btn_read, self.btn_record, self.btn_pulses):
            btn.pack(side='left', padx=(0, 6))

        bar2 = ttk.Frame(frame)
        bar2.pack(fill='x', pady=2)
        self.btn_home = ttk.Button(bar2, text='一键回 0', command=self.on_home)
        self.btn_undo = ttk.Button(bar2, text='撤销上一点', command=self.on_undo)
        self.btn_clear = ttk.Button(bar2, text='清空采样', command=self.on_clear)
        self.btn_solve = ttk.Button(bar2, text='③ 求解并保存', command=self.on_solve)
        self.btn_ortho = ttk.Button(bar2, text='简化求解（只走 X/Y）',
                                    command=self.on_solve_ortho)
        for btn in (self.btn_home, self.btn_undo, self.btn_clear, self.btn_solve,
                    self.btn_ortho):
            btn.pack(side='left', padx=(0, 6))

        bar3 = ttk.Frame(frame)
        bar3.pack(fill='x', pady=2)
        ttk.Checkbutton(bar3, text='写 new_eye/tray_calib.json',
                        variable=self.var_write).pack(side='left')
        self.pos_label = ttk.Label(bar3, text='软位置：X=0 Y=0 脉冲', foreground='#0a3d91')
        self.pos_label.pack(side='left', padx=12)

        self.uv_label = ttk.Label(frame, text='最近一次视觉：—', foreground='#0a3d91')
        self.uv_label.pack(anchor='w', pady=(2, 0))

        ttk.Label(frame, justify='left', wraplength=820, foreground='#555555', text=(
            '提示：用「四轴控制」页里 X(4)/Y(5) 面板的「▶ 相对走」移动（脉冲数填正数、方向选正负）。'
            '别用「▶ 连续走」和「▶ 绝对走到此」，它们会让软位置变未知。'
            '「一键回 0」会按软位置把 X/Y 开回 0 基准点（即最近一次「位置清零」的位置）。\n'
            '最省事的做法：只走两次——第 2 点只动 X、第 3 点只动 Y，然后点「简化求解（只走 X/Y）」，'
            '它假设 X→左右、Y→前后，不需要点摆成三角形，也不会报“矩阵奇异”。'
        )).pack(anchor='w', pady=(4, 0))

        ttk.Label(frame, text='采样点（每点：u/v + 脉冲 + 相对上一点的 Δ）').pack(
            anchor='w', pady=(8, 0))
        self.list_step = tk.Listbox(frame, height=8, font=('Consolas', 9),
                                    bg='#f7f8fa', relief='solid', bd=1)
        self.list_step.pack(fill='both', expand=True, pady=(2, 0))

    # ---- 手推 + 回读 ----
    def _build_push(self, frame):
        ttk.Label(frame, justify='left', wraplength=820, foreground='#884400', text=(
            '只有平台能被手推动（皮带/同步带）才用这个模式；滚珠丝杠自锁推不动是正常的。\n'
            '做法：取消使能 → 手推到位置 → 「记录」→ 重复 3~5 个点 → 求解。'
            '单次推动别超过 1/3 圈（单圈编码器只测最短路径）。'
        )).pack(anchor='w')

        bar1 = ttk.Frame(frame)
        bar1.pack(fill='x', pady=6)
        self.btn_disable = ttk.Button(bar1, text='取消 X/Y 使能（可手推）',
                                      command=self.on_disable)
        self.btn_enable = ttk.Button(bar1, text='恢复 X/Y 使能', command=self.on_enable)
        for btn in (self.btn_disable, self.btn_enable):
            btn.pack(side='left', padx=(0, 6))

        bar2 = ttk.Frame(frame)
        bar2.pack(fill='x', pady=2)
        self.btn_push_record = ttk.Button(bar2, text='记录当前位置（视觉 + 回读）',
                                          command=self.on_push_record)
        self.btn_push_solve = ttk.Button(bar2, text='求解并保存标定文件',
                                         command=self.on_push_solve)
        for btn in (self.btn_push_record, self.btn_push_solve):
            btn.pack(side='left', padx=(0, 6))
        ttk.Checkbutton(bar2, text='写 new_eye/tray_calib.json',
                        variable=self.var_write).pack(side='left', padx=8)
        self.counts_label = ttk.Label(bar2, text='回读：—', foreground='#0a3d91')
        self.counts_label.pack(side='left', padx=8)

        ttk.Label(frame, text='采样点（回读 counts，以第 1 点为原点）').pack(anchor='w', pady=(8, 0))
        self.list_push = tk.Listbox(frame, height=8, font=('Consolas', 9),
                                    bg='#f7f8fa', relief='solid', bd=1)
        self.list_push.pack(fill='both', expand=True, pady=(2, 0))

    # ---------- 通用 ----------
    def on_mode(self):
        self.frame_step.pack_forget()
        self.frame_push.pack_forget()
        if self.var_mode.get() == 'step':
            self.frame_step.pack(fill='both', expand=True)
            self._refresh_pos()
        else:
            self.frame_push.pack(fill='both', expand=True)

    def say(self, text):
        self.log.insert('end', text + '\n')
        self.log.see('end')

    def _soft(self):
        """读上位机软位置（由 HostClient.motion_hook 记账）。"""
        app = self.app
        soft = getattr(app, 'soft_pos', None) or {}
        known = getattr(app, 'pos_known', None) or {}
        x = int(soft.get(AXIS_X, 0))
        y = int(soft.get(AXIS_Y, 0))
        return x, y, bool(known.get(AXIS_X, True)), bool(known.get(AXIS_Y, True))

    def _refresh_pos(self):
        x, y, kx, ky = self._soft()
        flag = ''
        if not (kx and ky):
            flag = '（未知：用过连续/绝对模式，请先回位并位置清零）'
        self.pos_label.configure(text=f'软位置：X={x:+d} Y={y:+d} 脉冲{flag}')

    def _read_uv(self, use_cache=False):
        """读视觉 u/v。use_cache=True 时优先用 30s 内"① 读取"拿到的值，避免记录时又读一帧。"""
        now = time.monotonic()
        if use_cache and self._last_uv and now - self._last_uv[3] < 30:
            u, v, label = self._last_uv[0], self._last_uv[1], self._last_uv[2]
            self.say(f'用刚才读到的视觉值：{label}  u={u:.4f}  v={v:.4f}')
            return u, v
        prefer = self.var_id.get().strip() or None
        try:
            u, v, label = W.read_target(prefer=prefer)
        except Exception as exc:
            messagebox.showerror('读取视觉失败', str(exc))
            return None
        self._last_uv = (u, v, label, now)
        self.say(f'视觉：{label}  u={u:.4f}  v={v:.4f}')
        self.uv_label.configure(text=f'最近一次视觉：{label}  u={u:.4f}  v={v:.4f}')
        return u, v

    # ---------- 手动走模式 ----------
    def on_read(self):
        self._read_uv()

    def on_record(self):
        got = self._read_uv(use_cache=True)
        if got is None:
            return
        u, v = got
        x, y, kx, ky = self._soft()
        if not (kx and ky):
            if not messagebox.askyesno('位置未知', 'X/Y 软位置是“未知”（用过连续/绝对模式，或断线过）。\n'
                                       '现在记录的脉冲可能不对。仍要记录吗？'):
                return
        self.samples.append({'u': u, 'v': v, 'px': float(x), 'py': float(y)})
        self.say(f'点 {len(self.samples)}：u={u:.4f} v={v:.4f} → X={x:+d} Y={y:+d} 脉冲')
        self._refresh_samples()
        self._refresh_pos()

    def on_pulses(self):
        x, y, kx, ky = self._soft()
        self._refresh_pos()
        if self.samples:
            last = self.samples[-1]
            dx, dy = x - last['px'], y - last['py']
            self.say(f'现在走过的脉冲：X={x:+d} Y={y:+d}；'
                     f'相对上一点 ΔX={dx:+.0f} ΔY={dy:+.0f}')
        else:
            self.say(f'现在走过的脉冲：X={x:+d} Y={y:+d}（还没有记录点）')
        if not (kx and ky):
            self.say('⚠ 位置未知：用过「连续走」或「绝对走到此」/断线重连过；'
                     '请用相对走重放或位置清零后再标。')

    def on_home(self):
        if not self.app.client.online:
            messagebox.showwarning('未连接', '先在主界面连接机器人。')
            return
        x, y, kx, ky = self._soft()
        if not (kx and ky):
            messagebox.showerror('位置未知', 'X/Y 软位置未知，无法自动回 0。\n'
                                 '请手动把平台挪回基准位置，再对该轴点「位置清零」。')
            return
        if x == 0 and y == 0:
            self.say('X/Y 已经在 0 脉冲点。')
            return
        if not messagebox.askyesno(
            '一键回 0 脉冲点',
            f'将把 X 走 {-x:+d}、Y 走 {-y:+d} 脉冲，回到 0 基准点。\n\n'
            '0 基准点 = 最近一次「位置清零」的位置。\n确认路径上没有人/物再继续。',
        ):
            return
        rpm, acc = 60, 100
        for axis, delta in ((AXIS_X, -x), (AXIS_Y, -y)):
            if delta == 0:
                continue
            self.app.client.send('motor_enable', axis, 1)
            self.app.client.send('motor_pos', axis, 0 if delta > 0 else 1,
                                 rpm, acc, abs(delta), 'rel')
            name = 'X' if axis == AXIS_X else 'Y'
            self.say(f'{name} 回 0：{delta:+d} 脉冲（{"正" if delta > 0 else "负"}向）')
        self.say('已下发回 0。等轴停下后点「读取现在走过的脉冲」确认是 0。')
        self.win.after(1500, self._refresh_pos)

    def on_undo(self):
        if self.samples:
            item = self.samples.pop()
            self.say(f'撤销点 {len(self.samples) + 1}（u={item["u"]:.4f} v={item["v"]:.4f}）')
        self._refresh_samples()

    def on_clear(self):
        self.samples.clear()
        self._refresh_samples()
        self.say('已清空采样。')

    def _refresh_samples(self):
        self.list_step.delete(0, 'end')
        prev = None
        for index, item in enumerate(self.samples, start=1):
            line = (f'P{index}  u={item["u"]:.4f} v={item["v"]:.4f}  '
                    f'X={item["px"]:+.0f} Y={item["py"]:+.0f} 脉冲')
            if prev is not None:
                line += (f'  ΔX={item["px"] - prev["px"]:+.0f} '
                         f'ΔY={item["py"] - prev["py"]:+.0f}')
            self.list_step.insert('end', line)
            prev = item

    def on_solve(self):
        if len(self.samples) < 3:
            messagebox.showinfo('还差数据', f'至少要 3 个采样点（现在 {len(self.samples)} 个）')
            return
        points = None
        try:
            points = W.pulse_points(self.samples)
            result = W.solve(points, write=bool(self.var_write.get()))
        except Exception as exc:
            text = str(exc)
            if points is None:
                messagebox.showerror('标定失败', text)
                return
            # 通用 3 点仿射失败（最常见：点共线 / 矩阵奇异）→ 问用户要不要用简化法
            simple = messagebox.askyesno(
                '改用简化标定？',
                '通用 3 点求解失败：\n' + text + '\n\n'
                '要改用**简化法**吗？\n'
                '简化法只假设两点：X 走时画面左右（u）变、Y 走时画面前后（v）变。\n'
                '不需要点形成三角形，也不会再报“矩阵奇异/共线”。\n'
                '代价：如果 X/Y 明显不垂直或相机装歪，会有固定偏移（报告里会给出检查值）。',
            )
            if not simple:
                messagebox.showerror('标定失败', text)
                return
            try:
                result = W.solve_ortho(points, write=bool(self.var_write.get()))
            except Exception as exc2:
                messagebox.showerror('标定失败', str(exc2))
                return
        self._show_result(result, points)

    def on_solve_ortho(self):
        """简化求解：只假设 X→u、Y→v，不需要点形成三角形。"""
        if len(self.samples) < 3:
            messagebox.showinfo('还差数据', f'至少要 3 个采样点（现在 {len(self.samples)} 个）')
            return
        try:
            points = W.pulse_points(self.samples)
            result = W.solve_ortho(points, write=bool(self.var_write.get()))
        except Exception as exc:
            messagebox.showerror('简化标定失败', str(exc))
            return
        self._show_result(result, points)

    def _show_result(self, result, points):
        self.say('———— 标定结果 ————')
        span = int(max(max(abs(p['px']), abs(p['py'])) for p in points)) or 1000
        for line in W.report(result, span).splitlines():
            self.say(line)
        if result['path']:
            self.say(f'已写入：{result["path"]}')
            if result.get('backup'):
                self.say(f'旧标定已备份：{result["backup"]}')
            self.app.note(f'X/Y 标定已保存：{result["path"]}')
        self.say('之后在“视觉目标”页生成抓取序列就会用这份标定。')

    # ---------- 手推模式 ----------
    def on_disable(self):
        if not self.app.client.online:
            messagebox.showwarning('未连接', '先在主界面连接机器人。')
            return
        self.app.client.send('stop_all')
        for axis in (AXIS_X, AXIS_Y):
            self.app.client.send('motor_enable', axis, 0)
        self.say('已下发：全轴停止 + X/Y 取消使能。现在可以试着用手推；推不动说明是自锁传动。')

    def on_enable(self):
        if not self.app.client.online:
            return
        for axis in (AXIS_X, AXIS_Y):
            self.app.client.send('motor_enable', axis, 1)
        self.say('已恢复 X/Y 使能。')

    def on_push_record(self):
        if not self.app.client.online:
            messagebox.showwarning('未连接', '先在主界面连接机器人。')
            return
        got = self._read_uv()
        if got is None:
            return
        u, v = got
        readback = tuple(getattr(self.app.client, 'readback_axes', ()) or ())
        for axis in (AXIS_X, AXIS_Y):
            if readback and axis not in readback:
                messagebox.showerror('没有回读', f'轴 {axis} 不在回读名单里。')
                return
        for axis in (AXIS_X, AXIS_Y):
            self.app.client.send('motor_query', axis, 'pos', timeout_ms=300)

        def grab(attempt=1):
            values, missing = {}, []
            for axis in (AXIS_X, AXIS_Y):
                panel = getattr(self.app, 'axes', {}).get(axis)
                counts = getattr(panel, 'last_counts', None) if panel else None
                if counts is None:
                    missing.append(axis)
                else:
                    values[axis] = int(counts)
            if missing and attempt < 3:
                self.win.after(450, lambda: grab(attempt + 1))
                return
            if missing:
                messagebox.showerror('读不到回读', '查不到轴 '
                                     + '/'.join(str(a) for a in missing) + ' 的编码器回读。')
                return
            self.push_samples.append({'u': u, 'v': v,
                                      'counts_x': values[AXIS_X],
                                      'counts_y': values[AXIS_Y]})
            self.counts_label.configure(text=f'回读：X={values[AXIS_X]} '
                                        f'Y={values[AXIS_Y]} counts')
            self.say(f'点 {len(self.push_samples)}：u={u:.4f} v={v:.4f}，'
                     f'X={values[AXIS_X]} Y={values[AXIS_Y]} counts')
            self.list_push.delete(0, 'end')
            for index, item in enumerate(self.push_samples, start=1):
                self.list_push.insert('end', f'P{index}  u={item["u"]:.4f} '
                                      f'v={item["v"]:.4f}  X={item["counts_x"]} '
                                      f'Y={item["counts_y"]} counts')

        self.win.after(450, grab)

    def on_push_solve(self):
        if len(self.push_samples) < 3:
            messagebox.showinfo('还差数据', f'至少要 3 个点（现在 {len(self.push_samples)} 个）')
            return
        try:
            result = W.solve_hand(self.push_samples, write=bool(self.var_write.get()))
        except Exception as exc:
            messagebox.showerror('标定失败', str(exc))
            return
        span = int(max(max(abs(p['px']), abs(p['py'])) for p in result['points'])) or 1000
        self.say('———— 标定结果 ————')
        for line in W.report(result, span).splitlines():
            self.say(line)
        if result['path']:
            self.say(f'已写入：{result["path"]}')
            self.app.note(f'X/Y 标定已保存：{result["path"]}')


def open_wizard(app):
    return CalibDialog(app)
