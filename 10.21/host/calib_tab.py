"""X/Y 标定向导（标签页版，只保留“视觉点标注”模式）。

流程（一次性）：
    1) new_eye 顶视画面点「标注点」，在物块/贴纸位置点 3~6 个点（自动写 calib_marks.json）
    2) 本页「刷新标注点」
    3) 用点动 / 单轴页把夹爪移到每个标注点的正上方，选中该行点「记录爪位」
    4) 全部记完点「求解并保存」→ new_eye/tray_calib.json

脉冲来源优先用驱动器回读（若该轴接了回读线），否则用上位机软位置。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import calib_wizard as W
import vision_grasp as VG

AXIS_X, AXIS_Y = VG.AXIS_X, VG.AXIS_Y
AXIS_NAMES = {AXIS_X: 'X(4)', AXIS_Y: 'Y(5)', VG.AXIS_Z: 'Z(1)', VG.AXIS_TRAY: '托盘(3)'}


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
        bar.pack(fill='x', pady=6)
        ttk.Button(bar, text='刷新标注点', command=self.on_marks_refresh).pack(side='left', padx=(0, 6))
        ttk.Button(bar, text='记录爪位', command=self.on_marks_record).pack(side='left', padx=(0, 6))
        ttk.Button(bar, text='清除这个爪位', command=self.on_marks_clearrow).pack(side='left', padx=(0, 6))
        ttk.Button(bar, text='求解并保存', command=self.on_marks_solve).pack(side='left', padx=(0, 6))
        ttk.Checkbutton(bar, text='写 new_eye/tray_calib.json',
                        variable=self.var_write).pack(side='left', padx=8)

        self.marks_label = ttk.Label(self, text='标注点：—', foreground='#0a3d91')
        self.marks_label.pack(anchor='w')
        self.pos_label = ttk.Label(self, text='软位置：X=+0 Y=+0 脉冲', foreground='#0a3d91')
        self.pos_label.pack(anchor='w', pady=(2, 0))

        ttk.Label(self, text='标注点（P 编号 = new_eye 里点的顺序；X/Y 是记录的爪位脉冲）'
                  ).pack(anchor='w', pady=(8, 0))
        box = ttk.Frame(self)
        box.pack(fill='both', expand=True)
        self.listbox = tk.Listbox(box, height=8, font=('Consolas', 10),
                                  bg='#f7f8fa', relief='solid', bd=1)
        sb = ttk.Scrollbar(box, orient='vertical', command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

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
            result = W.solve(points, write=bool(self.var_write.get()))
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
