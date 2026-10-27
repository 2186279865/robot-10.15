"""运行台（比赛当天唯一需要看的页面）。

设计目标：所有"比赛要按的东西"集中在一页，且只放这些。
- 左：顶视主摄大画面（叠加目标框/编号/类别/抓取点十字）
- 右：开始 / 暂停 / 停止 + 件数 + 落货 + 进度 + 结果表
- 底：大字提示条

依赖全部复用现有实现：
- 画面：vision.gui.App.a（顶视 Stream，只读最新帧，不额外拉流）
- 识别：vision.recognize_blocking()
- 抓取：auto_grab.run_many(...)
- 分盒：box_map
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageTk

import auto_grab as AG

RUN_W, RUN_H = 900, 700          # 主画面显示区（按比例缩放，黑边填充）


class RunTab:
    """正式运行台。"""

    def __init__(self, notebook, app, index: int = 0):
        self.app = app
        self.vision = None            # attach_vision() 时绑定
        self.calib = None             # attach_calib() 时绑定
        self.frame = ttk.Frame(notebook)
        notebook.insert(index, self.frame, text='运行台')

        self._photo = None
        self._run_thread: Optional[threading.Thread] = None
        self._stop = False
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._items: list[dict] = []
        self._total = 6
        self._done = 0
        self._ok = 0
        self._fail = 0
        self._t_start = 0.0
        self._t_item = 0.0

        self._build()
        self.frame.after(200, self._tick)
        self.frame.after(300, self._pump_events)

    # ------------------------------------------------------------ 装配
    def attach_vision(self, vision):
        self.vision = vision

    def attach_calib(self, calib):
        self.calib = calib

    def _build(self):
        # 状态条
        st = ttk.Frame(self.frame)
        st.pack(fill='x', padx=8, pady=(6, 2))
        self.lbl_conn = ttk.Label(st, text='● 未连接', foreground='#b00020',
                                  font=('Microsoft YaHei', 10, 'bold'))
        self.lbl_conn.pack(side='left')
        self.lbl_cam = ttk.Label(st, text='　相机 —', foreground='#666666')
        self.lbl_cam.pack(side='left')
        self.lbl_fw = ttk.Label(st, text='　固件 —', foreground='#666666')
        self.lbl_fw.pack(side='left')
        self.lbl_box = ttk.Label(st, text='　分盒 —', foreground='#666666')
        self.lbl_box.pack(side='left')

        body = ttk.Frame(self.frame)
        body.pack(fill='both', expand=True, padx=8, pady=4)

        # 右：操作面板（先 pack，避免被左边 expand 的 canvas 挤没）
        right = ttk.Frame(body, width=430)
        right.pack(side='right', fill='y', padx=(10, 0))

        # 左：主摄画面
        left = ttk.LabelFrame(body, text='顶视主摄')
        left.pack(side='left', fill='both', expand=True)
        self.canvas = tk.Canvas(left, width=RUN_W, height=RUN_H, bg='#141414',
                                highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)

        self.btn_start = ttk.Button(right, text='▶▶  开始抓取', command=self.on_start)
        self.btn_start.pack(fill='x', ipady=16, pady=(2, 6))
        row = ttk.Frame(right)
        row.pack(fill='x')
        self.btn_pause = ttk.Button(row, text='⏸ 暂停', command=self.on_pause, state='disabled')
        self.btn_pause.pack(side='left', fill='x', expand=True, ipady=8)
        self.btn_stop = ttk.Button(row, text='■ 停止', command=self.on_stop, state='disabled')
        self.btn_stop.pack(side='left', fill='x', expand=True, ipady=8, padx=(6, 0))

        opt = ttk.LabelFrame(right, text='本次任务')
        opt.pack(fill='x', pady=(10, 4))
        r1 = ttk.Frame(opt)
        r1.pack(fill='x', padx=6, pady=3)
        ttk.Label(r1, text='件数').pack(side='left')
        self.var_count = tk.IntVar(value=6)
        ttk.Spinbox(r1, from_=1, to=20, width=4, textvariable=self.var_count
                    ).pack(side='left', padx=6)
        self.var_drop = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text='抓完落货（转盘 + 松爪）', variable=self.var_drop
                        ).pack(anchor='w', padx=6)
        self.var_skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text='失败自动跳过（继续下一件）', variable=self.var_skip
                        ).pack(anchor='w', padx=6, pady=(0, 4))

        prog = ttk.LabelFrame(right, text='进度')
        prog.pack(fill='x', pady=(6, 4))
        self.lbl_prog = ttk.Label(prog, text='第 0 / 6 件　本件 —',
                                  font=('Microsoft YaHei', 11, 'bold'))
        self.lbl_prog.pack(anchor='w', padx=6)
        self.pbar = ttk.Progressbar(prog, maximum=100, value=0)
        self.pbar.pack(fill='x', padx=6, pady=(2, 6))

        res = ttk.LabelFrame(right, text='结果')
        res.pack(fill='both', expand=True, pady=(6, 0))
        cols = ('cls', 'box', 'ms', 'r')
        self.tree = ttk.Treeview(res, columns=cols, show='headings', height=8)
        for key, title, width in (('cls', '类别', 150), ('box', '盒', 40),
                                  ('ms', '耗时', 70), ('r', '结果', 70)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor='w', stretch=(key == 'cls'))
        self.tree.pack(fill='both', expand=True, padx=4, pady=4)

        # 底部大字提示条
        self.lbl_hint = ttk.Label(self.frame, text='待机：放好物块后点「开始抓取」',
                                  font=('Microsoft YaHei', 12, 'bold'),
                                  foreground='#123a8a')
        self.lbl_hint.pack(fill='x', padx=10, pady=(2, 8))

    # ------------------------------------------------------------ 周期刷新
    def _tick(self):
        self._draw()
        self._refresh_status()
        self.frame.after(200, self._tick)

    def _draw(self):
        vision = self.vision
        frame = None
        if vision is not None and getattr(vision, 'a', None) is not None:
            try:
                frame = vision.a.get()
            except Exception:                    # noqa: BLE001
                frame = None
        if frame is None:
            return
        fh, fw = frame.shape[:2]
        scale = min(RUN_W / float(fw), RUN_H / float(fh))
        nw, nh = int(fw * scale), int(fh * scale)
        small = cv2.resize(frame, (nw, nh))
        # 叠加目标框（用视觉已有的融合结果）
        try:
            fused = list(getattr(vision, 'fused', None) or [])
        except Exception:                        # noqa: BLE001
            fused = []
        for i, item in enumerate(fused, start=1):
            t = item.get('top') or item.get('side') or {}
            bbox = t.get('bbox')
            if bbox:
                x, y, w, h = bbox
                x, y, w, h = (int(v * scale) for v in (x, y, w, h))
                cv2.rectangle(small, (x, y), (x + w, y + h), (0, 255, 120), 2)
                cv2.putText(small, '#%d' % i, (x + 2, max(14, y - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 120), 2)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        self._photo = ImageTk.PhotoImage(img)
        self.canvas.delete('all')
        cx = (RUN_W - nw) // 2
        cy = (RUN_H - nh) // 2
        self.canvas.create_image(cx, cy, image=self._photo, anchor='nw')

    def _refresh_status(self):
        client = getattr(self.app, 'client', None)
        online = bool(getattr(client, 'online', False))
        self.lbl_conn.configure(text='● 已连接' if online else '● 未连接',
                                foreground='#0a6b0a' if online else '#b00020')
        fw = getattr(client, 'fw_version', None) if client is not None else None
        self.lbl_fw.configure(text='　固件 %s' % (fw or '—'))
        fps = 0.0
        vision = self.vision
        if vision is not None:
            try:
                fps = float(getattr(vision, 'fps_top', 0.0) or 0.0)
            except Exception:                    # noqa: BLE001
                fps = 0.0
        self.lbl_cam.configure(text='　相机 %s' % ('%.1f fps' % fps if fps else '—'))
        try:
            import box_map as BM
            self.lbl_box.configure(text='　分盒 %s' % BM.describe())
        except Exception:                        # noqa: BLE001
            self.lbl_box.configure(text='　分盒 —')

    # ------------------------------------------------------------ 按钮
    def _client_or_warn(self):
        client = getattr(self.app, 'client', None)
        if client is None or not getattr(client, 'online', False):
            messagebox.showwarning('运行台', '还没连接控制器：先在顶部选端口并点「连接」')
            return None
        return client

    def on_start(self):
        if self._run_thread is not None and self._run_thread.is_alive():
            return
        client = self._client_or_warn()
        if client is None:
            return
        if self.vision is None:
            messagebox.showwarning('运行台', '视觉页没加载')
            return
        try:
            self._total = max(1, int(self.var_count.get()))
        except (tk.TclError, ValueError):
            self._total = 6
        self._items = []
        self._done = 0
        self._ok = 0
        self._fail = 0
        self._stop = False
        self._t_start = time.monotonic()
        self._t_item = time.monotonic()
        self.tree.delete(*self.tree.get_children())
        self.pbar.configure(value=0)
        self.lbl_prog.configure(text='第 0 / %d 件　本件 —' % self._total)
        self.lbl_hint.configure(text='准备中：先识别一次…', foreground='#123a8a')
        self.btn_start.configure(state='disabled')
        self.btn_pause.configure(state='normal', text='⏸ 暂停')
        self.btn_stop.configure(state='normal')
        self._run_thread = threading.Thread(target=self._worker, args=(client,), daemon=True)
        self._run_thread.start()

    def _pos_source(self):
        if self.calib is not None and hasattr(self.calib, '_pos_source'):
            try:
                return self.calib._pos_source()
            except Exception:                    # noqa: BLE001
                return (None, None, None)
        return (None, None, None)

    def _worker(self, client):
        vision = self.vision
        recognize = (vision.recognize_blocking if hasattr(vision, 'recognize_blocking')
                     else vision.recognize_safe)
        count = self._total
        drop = bool(self.var_drop.get())
        for i in range(1, count + 1):
            if self._stop:
                self._events.put(('stop', i))
                break
            self._t_item = time.monotonic()
            self._events.put(('item_start', i))
            try:
                info = recognize()
                targets = AG.load_targets()
                if not targets:
                    self._events.put(('item_fail', i, '没有识别到目标'))
                    continue
                target = AG.pick_target(targets)
                self._events.put(('target', i, target))
                pos = self._pos_source()

                def _log(msg, level='info'):
                    self._events.put(('log', i, msg))

                res = AG.run_once(client, target, log=_log, pos_source=lambda: pos,
                                  drop=drop)
                self._events.put(('item_ok', i, target, res))
            except Exception as exc:             # noqa: BLE001
                self._events.put(('item_fail', i, '%s: %s' % (type(exc).__name__, exc)))
                if not self.var_skip.get():
                    break
        self._events.put(('finished',))

    def on_pause(self):
        # P1：暂停 = 停止（下一件不再开始），后续再做"暂停/继续"
        self.on_stop()

    def on_stop(self):
        self._stop = True
        self.btn_pause.configure(state='disabled')
        self.lbl_hint.configure(text='已请求停止：等当前动作收尾…', foreground='#b26a00')

    # ------------------------------------------------------------ 事件泵
    def _pump_events(self):
        try:
            while True:
                ev = self._events.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        self.frame.after(120, self._pump_events)

    def _handle(self, ev):
        kind = ev[0]
        if kind == 'item_start':
            i = ev[1]
            self.lbl_hint.configure(text='正在处理第 %d / %d 件：识别中…' % (i, self._total),
                                    foreground='#123a8a')
        elif kind == 'target':
            i, target = ev[1], ev[2]
            cls = '%s/%s' % (target.get('shape') or '?', target.get('color') or '?')
            self.lbl_hint.configure(text='第 %d / %d 件：%s → 前往抓取' % (i, self._total, cls),
                                    foreground='#123a8a')
        elif kind == 'item_ok':
            i, target, res = ev[1], ev[2], ev[3]
            ms = (time.monotonic() - self._t_item) * 1000.0
            cls = '%s/%s' % (target.get('shape') or '?', target.get('color') or '?')
            self._ok += 1
            self._add_row(i, cls, '-', ms, '✅')
        elif kind == 'item_fail':
            i, reason = ev[1], ev[2]
            ms = (time.monotonic() - self._t_item) * 1000.0
            self._fail += 1
            self._add_row(i, '—', '-', ms, '❌ ' + str(reason)[:18])
            self.lbl_hint.configure(text='第 %d 件失败：%s' % (i, reason), foreground='#b00020')
        elif kind == 'log':
            msg = ev[2]
            self.lbl_hint.configure(text=str(msg)[:140], foreground='#123a8a')
        elif kind == 'stop':
            self.lbl_hint.configure(text='已停止（第 %s 件前）' % ev[1], foreground='#b26a00')
        elif kind == 'finished':
            self._finish()

    def _add_row(self, i, cls, box, ms, result):
        self._done += 1
        self.tree.insert('', 'end', values=(cls, box, '%.1f s' % (ms / 1000.0), result))
        pct = 100.0 * self._done / max(1, self._total)
        self.pbar.configure(value=pct)
        self.lbl_prog.configure(text='第 %d / %d 件　本件 %.1f s'
                                % (self._done, self._total, ms / 1000.0))

    def _finish(self):
        self.btn_start.configure(state='normal')
        self.btn_pause.configure(state='disabled')
        self.btn_stop.configure(state='disabled')
        used = time.monotonic() - self._t_start
        self.lbl_hint.configure(
            text='完成：成功 %d 件 / 失败 %d 件｜总用时 %d:%02d'
                 % (self._ok, self._fail, int(used) // 60, int(used) % 60),
            foreground='#0a6b0a' if not self._fail else '#b26a00')


def build_run_tab(notebook, app, index: int = 0) -> RunTab:
    return RunTab(notebook, app, index=index)
