import json
import socket
import threading
import time
import tkinter as tk
from collections import Counter
from pathlib import Path
from tkinter import ttk, messagebox, simpledialog

import cv2
import numpy as np
from PIL import Image, ImageTk

import classify
import link
from auto_segment import AutoSegmenter
from camera import Camera
from fusion import fuse

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'config.json'

# 类别配色（最多六类，未知/待登记用琥珀色）
CLS_COLORS = {'1': '#ff6b6b', '2': '#ffd400', '3': '#6ee06e',
              '4': '#5cc8ff', '5': '#c58cff', '6': '#ff9f40'}


class Stream:
    def __init__(self, i, w, h, f):
        self.cam = Camera(i, w, h, f)
        self.frame = None
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.frames = 0
        self.t0 = time.time()

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            self.cam.open()
            while not self.stop_evt.is_set():
                ok = self.cam.cap.grab()
                if ok:
                    ok, fr = self.cam.cap.retrieve()
                    if ok:
                        with self.lock:
                            self.frame = fr
                        self.frames += 1
        except Exception as e:
            self.error = str(e)
        finally:
            self.cam.close()

    def fps(self):
        dt = time.time() - self.t0
        return (self.frames / dt) if dt > 0 else 0.0

    def get(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def close(self):
        self.stop_evt.set()
        self.cam.close()


class View:
    def __init__(self, c, click=None):
        self.c = c
        self.angle = 0
        self.zoom = 1
        self.px = 0
        self.py = 0
        self.photo = None
        self.drag = None
        self.click = click
        c.bind('<ButtonPress-1>', self.b)
        c.bind('<B1-Motion>', self.m)
        c.bind('<ButtonRelease-1>', lambda e: setattr(self, 'drag', None))
        c.bind('<MouseWheel>', self.w)

    def b(self, e):
        if self.click and self.click(e):
            return
        self.drag = (e.x, e.y, self.px, self.py)

    def m(self, e):
        if self.drag:
            x, y, px, py = self.drag
            self.px = px + e.x - x
            self.py = py + e.y - y

    def w(self, e):
        self.zoom = max(.25, min(4, self.zoom * (1.1 if e.delta > 0 else .9)))

    def draw(self, fr, points=None):
        if fr is None:
            return
        if self.angle:
            fr = cv2.rotate(fr, {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
                                 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[self.angle])
        h, w = fr.shape[:2]
        cw, ch = max(240, self.c.winfo_width()), max(180, self.c.winfo_height())
        s = min(cw / w, ch / h) * self.zoom
        im = cv2.resize(fr, (int(w * s), int(h * s)))
        im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        self.photo = ImageTk.PhotoImage(Image.fromarray(im))
        self.c.delete('all')
        ox = (cw - im.shape[1]) / 2 + self.px
        oy = (ch - im.shape[0]) / 2 + self.py
        self.c.create_image(ox, oy, image=self.photo, anchor='nw')
        if points:
            pts = [(int(x), int(y)) for x, y in points]
            if len(pts) > 1:
                self.c.create_line(*sum(([x, y] for x, y in pts), []), fill='#20e070',
                                   width=3, tag='points')
            if len(pts) == 4:
                self.c.create_line(pts[3][0], pts[3][1], pts[0][0], pts[0][1],
                                   fill='#20e070', width=3, tag='points')
            for i, (x, y) in enumerate(pts):
                self.c.create_oval(x - 6, y - 6, x + 6, y + 6, fill='#20e070',
                                   outline='white', width=1, tag='points')
                self.c.create_text(x + 11, y, text=str(i + 1), fill='#aaffaa',
                                   anchor='w', tag='points')

    def frame_to_canvas(self, x, y, shape):
        h, w = shape[:2]
        if self.angle in (90, 270):
            w, h = h, w
        if self.angle == 90:
            x, y = shape[0] - 1 - y, x
        elif self.angle == 180:
            x, y = shape[1] - 1 - x, shape[0] - 1 - y
        elif self.angle == 270:
            x, y = y, shape[1] - 1 - x
        cw, ch = max(240, self.c.winfo_width()), max(180, self.c.winfo_height())
        s = min(cw / w, ch / h) * self.zoom
        return (cw - w * s) / 2 + self.px + x * s, (ch - h * s) / 2 + self.py + y * s

    def canvas_to_frame(self, x, y, shape):
        h, w = shape[:2]
        rw, rh = (h, w) if self.angle in (90, 270) else (w, h)
        cw, ch = max(240, self.c.winfo_width()), max(180, self.c.winfo_height())
        s = min(cw / rw, ch / rh) * self.zoom
        u = (x - ((cw - rw * s) / 2 + self.px)) / s
        v = (y - ((ch - rh * s) / 2 + self.py)) / s
        if self.angle == 90:
            return (v, shape[0] - 1 - u)
        if self.angle == 180:
            return (w - 1 - u, h - 1 - v)
        if self.angle == 270:
            return (shape[1] - 1 - v, u)
        return (u, v)


class App:
    def __init__(self, r):
        self.r = r
        r.title('new_eye 双相机视觉预览（顶视+副摄融合）')
        r.geometry('1680x920')
        self.cfg = json.loads(CONFIG.read_text(encoding='utf8'))
        self.a = self.bstream = None
        self.running = False
        self.tf = self.sf = None
        self.roi = None
        self.points = [tuple(p) for p in self.cfg.get('top_points', [])]
        self.side_points = [tuple(p) for p in self.cfg.get('side_points', [])]
        self.point_mode = None
        self.register_mode = False
        self.segmenter = None
        self.samples = classify.load_samples()
        self.fused = []
        self.sam_lock = threading.Lock()
        self.live = False
        self.live_thread = None
        self.live_msg = None
        self.live_err = None
        self.det_fps = 0.0
        self.disp_fps = 0.0
        self.times = {}
        self.fuse_info = {}
        self.link = None
        self.link_port = int(self.cfg.get('link_port', 5055) or 5055)
        self.side_cache = None
        self._side_tick = 0
        self._drawn = 0
        self._fps_t0 = time.time()
        self.warps = (None, None)
        self.logs = []
        self.live_status = None
        self.build()
        self.va.angle = int(self.cfg.get('top_rotation', 0))
        self.vb.angle = int(self.cfg.get('side_rotation', 0))
        self.r.bind('<Escape>', lambda e: self.cancel_modes())
        self.r.after(300, self.start)

    # ------------------------------------------------------------------ 界面
    def build(self):
        bar = ttk.Frame(self.r, padding=8)
        bar.pack(fill='x')
        ttk.Label(bar, text='顶视相机').pack(side='left')
        self.tv = tk.StringVar(value=str(self.cfg.get('top_camera_index', 3)))
        self.tc = ttk.Combobox(bar, textvariable=self.tv, width=6, state='readonly')
        self.tc.pack(side='left', padx=4)
        ttk.Label(bar, text='侧视相机').pack(side='left')
        self.sv = tk.StringVar(value=str(self.cfg.get('side_camera_index', 0)))
        self.sc = ttk.Combobox(bar, textvariable=self.sv, width=6, state='readonly')
        self.sc.pack(side='left', padx=4)
        ttk.Button(bar, text='扫描相机', command=self.scan).pack(side='left', padx=5)
        ttk.Button(bar, text='打开双画面', command=self.start).pack(side='left')
        ttk.Button(bar, text='停止', command=self.stop).pack(side='left')
        self.gray = tk.BooleanVar(value=bool(self.cfg.get('gray_objects', False)))
        ttk.Checkbutton(bar, text='含灰/黑物料', variable=self.gray,
                        command=lambda: self.cfg.__setitem__('gray_objects', self.gray.get())
                        ).pack(side='left', padx=4)
        ttk.Button(bar, text='保存配置', command=self.save).pack(side='left')
        self.link_var = tk.BooleanVar(value=bool(self.cfg.get('link_enabled', False)))
        ttk.Checkbutton(bar, text='网络推送', variable=self.link_var,
                        command=self.toggle_link).pack(side='left', padx=4)
        self.st = tk.StringVar(value='先扫描相机')

        body = ttk.Frame(self.r, padding=(8, 0, 8, 0))
        body.pack(fill='both', expand=True)
        rp = ttk.Frame(body, width=540)
        rp.pack(side='right', fill='y', padx=(8, 0))
        ttk.Label(rp, text='目标详情 / 状态（所有文字信息）',
                  font=('Microsoft YaHei', 10, 'bold')).pack(anchor='w')
        tf = ttk.Frame(rp)
        tf.pack(fill='both', expand=True)
        self.txt = tk.Text(tf, width=52, wrap='word', bg='#1b1c1f', fg='#d7dde4',
                           relief='flat', font=('Consolas', 12), padx=8, pady=6,
                           spacing1=1, insertwidth=0)
        self.txt.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(tf, orient='vertical', command=self.txt.yview)
        sb.pack(side='right', fill='y')
        self.txt.configure(yscrollcommand=sb.set, state='disabled')
        self.txt.tag_configure('hd', foreground='#7fd1ff',
                               font=('Consolas', 12, 'bold'), spacing1=8)
        self.txt.tag_configure('wr', foreground='#ff9f40')

        pan = ttk.PanedWindow(body, orient='horizontal')
        pan.pack(side='left', fill='both', expand=True)
        l = ttk.Frame(pan)
        q = ttk.Frame(pan)
        pan.add(l, weight=1)
        pan.add(q, weight=1)
        ttk.Label(l, text='顶视：四点模式依次点左上、右上、右下、左下；右键撤销').pack(anchor='w')
        ttk.Label(q, text='副摄画面：可独立旋转和四点定位（用于双摄融合与类别核对）').pack(anchor='w')
        self.ca = tk.Canvas(l, bg='#202124', highlightthickness=0)
        self.cb = tk.Canvas(q, bg='#202124', highlightthickness=0)
        self.ca.pack(fill='both', expand=True)
        self.cb.pack(fill='both', expand=True)
        self.va = View(self.ca, lambda e: self.canvas_click(e, 'top'))
        self.vb = View(self.cb, lambda e: self.canvas_click(e, 'side'))
        self.ca.bind('<Button-3>', lambda e: self.undo_point(e, 'top'))
        self.cb.bind('<Button-3>', lambda e: self.undo_point(e, 'side'))
        self.update_text()

        bot = ttk.Frame(self.r, padding=8)
        bot.pack(fill='x')
        for name, v in [('顶视', self.va), ('侧视', self.vb)]:
            ttk.Label(bot, text=name).pack(side='left', padx=(8, 2))
            ttk.Button(bot, text='↶左转90°', command=lambda v=v: self.rotate(v, -90)).pack(side='left', padx=1)
            ttk.Button(bot, text='↷右转90°', command=lambda v=v: self.rotate(v, 90)).pack(side='left', padx=1)
            ttk.Button(bot, text='复位', command=lambda v=v: self.reset_view(v)).pack(side='left', padx=2)
        ttk.Button(bot, text='顶视四点框选', command=lambda: self.start_points('top')).pack(side='left', padx=3)
        ttk.Button(bot, text='副摄四点框选', command=lambda: self.start_points('side')).pack(side='left', padx=3)
        ttk.Label(bot, text='副摄方向').pack(side='left', padx=(8, 2))
        self.flip_var = tk.StringVar(value={0: '自动', 1: '同向', 2: '镜像'}.get(
            int(self.cfg.get('side_flip', 0) or 0), '自动'))
        cb = ttk.Combobox(bot, textvariable=self.flip_var, values=['自动', '同向', '镜像'],
                          width=5, state='readonly')
        cb.pack(side='left')
        cb.bind('<<ComboboxSelected>>', lambda e: self.set_flip(self.flip_var.get()))
        ttk.Label(bot, text='对齐轴').pack(side='left', padx=(8, 2))
        self.axis_var = tk.StringVar(value={0: '自动', 1: '左右', 2: '前后'}.get(
            int(self.cfg.get('side_axis', 0) or 0), '自动'))
        cba = ttk.Combobox(bot, textvariable=self.axis_var, values=['自动', '左右', '前后'],
                           width=5, state='readonly')
        cba.pack(side='left')
        cba.bind('<<ComboboxSelected>>', lambda e: self.set_axis(self.axis_var.get()))
        ttk.Button(bot, text='采集空托盘', command=self.capture_empty).pack(side='left', padx=3)
        ttk.Button(bot, text='清除定位', command=self.clear).pack(side='left', padx=3)
        ttk.Button(bot, text='单次识别', command=self.recognize).pack(side='left', padx=3)
        self.live_btn = ttk.Button(bot, text='实时分割', command=self.toggle_live)
        self.live_btn.pack(side='left', padx=3)
        self.reg_btn = ttk.Button(bot, text='登记样本', command=self.toggle_register)
        self.reg_btn.pack(side='left', padx=3)

    # -------------------------------------------------------------- 文字面板
    def note(self, msg):
        """状态栏 + 右侧文字面板共用的消息入口。"""
        self.st.set(msg)
        self.logs.append(time.strftime('%H:%M:%S') + '  ' + msg)
        self.logs = self.logs[-9:]

    def _panel_text(self):
        fa = self.a.fps() if self.a else 0.0
        fb = self.bstream.fps() if self.bstream else 0.0
        fused = self.fused
        ntop = sum(1 for f in fused if f.get('top'))
        nside = sum(1 for f in fused if f.get('side'))
        cs = classify.class_summary(fused)
        # “副摄待确认”是未确认项，不算类别，单独计数，避免看起来像多出一个类别
        unc = cs.pop('副摄待确认', 0)
        ctxt = '  '.join(f'{k}×{v}' for k, v in cs.items()) or '—'
        if unc:
            ctxt += '  未确认×%d' % unc
        L = ['【运行】' + ('实时分割中' if self.live else ('相机已打开' if self.running else '未打开相机'))
             + (' · 登记模式' if self.register_mode else ''),
             '【帧率】刷新 %.1f | 顶视 %.1f | 副摄 %.1f fps' % (self.disp_fps, fa, fb)]
        if getattr(self, 'live_status', None):
            L.append('【状态】' + self.live_status)
        L += [
             '【分割】%.2f fps   融合目标 %d 个' % (self.det_fps, len(fused))]
        fi = getattr(self, 'fuse_info', None) or {}
        if fi.get('n_top') is not None:
            fdir = ('镜像' if fi.get('flip') else '同向')
            if fi.get('auto'):
                fdir += '（自动）'
            fax = fi.get('axis_name') or '左右(u)'
            if fi.get('axis_mode') in (0, None):
                fax += '（自动）'
            md = fi.get('mean_dist')
            L.append('【融合】顶视 %d / 副摄 %d  →  配对 %d 个（Δ %s，对齐轴 %s，方向 %s），'
                     '副摄未印证 %d，副摄多出 %d'
                     % (fi['n_top'], fi['n_side'], fi['matched'],
                        '—' if md is None else '%.3f' % md, fax, fdir,
                        fi.get('unmatched_top', 0), fi.get('unmatched_side', 0)))
            if fi['n_top'] and fi['n_side'] and not fi['matched']:
                L.append('⚠ 两路一个都没配上：先试“对齐轴”左右/前后与“副摄方向”，'
                         '再确认副摄四点是否按托盘同一方位顺序点的；'
                         '顺序不一致时两路托盘坐标对不上，融合必然失效。')
        t = self.times
        if t:
            def ms(v):
                if v is None:
                    return '—'
                return '%.0fms' % (v * 1000) if v < 1 else '%.2fs' % v

            def tot(*ks):
                vals = [t[k] for k in ks if k in t]
                return None if not vals else sum(vals)
            L.append('【耗时】整帧 %s   顶视 %s（分割 %s）'
                     % (ms(t.get('frame')), ms(tot('top.warp', 'top.sam', 'top.post')),
                        ms(t.get('top.sam'))))
            L.append('        顶视细分：校正 %s / 分割 %s / 反投影 %s'
                     % (ms(t.get('top.warp')), ms(t.get('top.sam')), ms(t.get('top.post'))))
            L.append('        副摄 %s（分割 %s）  融合 %s  分类 %s  写JSON %s'
                     % (ms(tot('side.warp', 'side.sam', 'side.post')),
                        ms(t.get('side.sam')), ms(t.get('fuse')),
                        ms(t.get('classify')), ms(t.get('write'))))
        L += [
             '【类别】' + ctxt,
             '',
             '【目标明细】']
        if not fused:
            L.append('（暂无目标：打开双画面后点“单次识别”，或开“实时分割”）')
        for f in sorted(fused, key=lambda x: x['id']):
            it = f.get('top') or f.get('side') or {}
            name = it.get('class_name') or '—'
            flag = '   ⚠待登记' if it.get('need_review') else ''
            if f.get('color_mismatch'):
                flag += '   ⚠两路颜色不一致'
            L.append('#%s  %s%s' % (f['id'], name, flag))
            u, v = it.get('center_tray') or (0.0, 0.0)
            L.append('    %s + %s   conf %.2f' % (it.get('shape') or '?',
                                                  it.get('color') or '?',
                                                  float(it.get('class_conf') or 0.0)))
            tail = '%s  u=%.2f v=%.2f  %dpx  %s' % (f['views'], u, v,
                                                   int(it.get('area_px') or 0),
                                                   it.get('class_source') or '—')
            if it.get('class_votes'):
                tail += '  投票%s' % it['class_votes']
            if f['views'] == 'top':
                tail += '  （副摄未印证）'
            L.append('    ' + tail)
        L.append('')
        L.append('【最近消息】')
        L += self.logs[-9:] or ['—']
        return '\n'.join(L)

    def update_text(self):
        if not hasattr(self, 'txt'):
            return
        s = self._panel_text()
        if s == getattr(self, '_panel_sig', None):
            return
        self._panel_sig = s
        t = self.txt
        t.configure(state='normal')
        t.delete('1.0', 'end')
        for line in s.split('\n'):
            tag = 'hd' if line.startswith('【') else ('wr' if '⚠' in line else '')
            t.insert('end', line + '\n', tag)
        t.configure(state='disabled')
        t.see('end')

    # ------------------------------------------------------------------ 相机/视图
    def scan(self):
        f = []
        for i in range(10):
            c = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if c.isOpened():
                ok, fr = c.read()
                if ok and fr is not None:
                    f.append(str(i))
            c.release()
        self.tc['values'] = f
        self.sc['values'] = f
        self.note('发现相机：' + ','.join(f) if f else '没有发现相机')

    def start(self):
        self.stop()
        try:
            self.a = Stream(int(self.tv.get()), self.cfg.get('width', 1920),
                            self.cfg.get('height', 1080), self.cfg.get('fps', 15))
            self.bstream = Stream(int(self.sv.get()), self.cfg.get('width', 1920),
                                  self.cfg.get('height', 1080), self.cfg.get('fps', 15))
            self.a.start()
            self.bstream.start()
            self.running = True
            self.note('双画面运行中')
            self.refresh()
        except Exception as e:
            messagebox.showerror('打开失败', str(e))

    def refresh(self):
        if not self.running:
            return
        self.tf = self.a.get()
        self.sf = self.bstream.get()
        self.va.draw(self.tf, self.canvas_points('top'))
        self.vb.draw(self.sf, self.canvas_points('side'))
        now = time.time()
        el = now - self._fps_t0
        if el >= 1.0:
            self.disp_fps = self._drawn / el
            self._drawn = 0
            self._fps_t0 = now
        self._drawn += 1
        self.draw_targets()
        self.sync_live()
        self.update_text()
        self.r.after(80, self.refresh)

    def stop(self):
        if getattr(self, 'live', False):
            self.stop_live()
        self.live = False
        if hasattr(self, 'live_btn'):
            self.live_btn.config(text='实时分割')
        with self.sam_lock:
            self.fused = []
        self.running = False
        if self.a:
            self.a.close()
        if self.bstream:
            self.bstream.close()
        self.a = self.bstream = None

    def cancel_modes(self):
        if self.register_mode:
            self.register_mode = False
            self.note('已退出登记模式')
        self.point_mode = None

    def clear(self):
        self.stop_live()
        self.roi = None
        self.points = []
        self.side_points = []
        self.point_mode = None
        with self.sam_lock:
            self.fused = []
        self.note('已清除两路四点定位')

    def start_points(self, which):
        self.register_mode = False
        self.point_mode = which
        with self.sam_lock:
            self.fused = []
        getattr(self, 'points' if which == 'top' else 'side_points').clear()
        self.note(('顶视' if which == 'top' else '副摄') + '四点定位：依次点击四角')

    def point_click(self, e, which):
        if self.point_mode != which:
            return False
        fr = self.tf if which == 'top' else self.sf
        if fr is None:
            return False
        pts = self.points if which == 'top' else self.side_points
        view = self.va if which == 'top' else self.vb
        if len(pts) >= 4:
            pts.clear()
        pts.append(view.canvas_to_frame(e.x, e.y, fr.shape))
        if len(pts) == 4:
            p = np.array(pts, dtype=np.float32)
            if abs(cv2.contourArea(p)) < 1000 or self.self_intersect(p):
                pts.pop()
                self.note('四点无效：区域过小或四边交叉')
                return True
            self.point_mode = None
            with self.sam_lock:
                self.fused = []
            self.note(('顶视' if which == 'top' else '副摄') + '四点已完成，识别只在框内')
        return True

    def self_intersect(self, p):
        def cross(a, b, c):
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (cross(p[0], p[1], p[2]) * cross(p[0], p[1], p[3]) <= 0
                and cross(p[2], p[3], p[0]) * cross(p[2], p[3], p[1]) <= 0)

    def undo_point(self, e, which):
        if self.register_mode:
            self.register_mode = False
            self.note('已退出登记模式')
            return
        pts = self.points if which == 'top' else self.side_points
        if pts:
            pts.pop()
            with self.sam_lock:
                self.fused = []
            self.note(f'已撤销，当前 {len(pts)}/4 个点')

    def rotate(self, v, delta):
        v.angle = (v.angle + delta) % 360
        self.note(f'画面方向：{v.angle}°')

    def reset_view(self, v):
        v.angle = 0
        v.zoom = 1
        v.px = 0
        v.py = 0
        self.note('视角已复位')

    def canvas_points(self, which):
        fr = self.tf if which == 'top' else self.sf
        pts = self.points if which == 'top' else self.side_points
        view = self.va if which == 'top' else self.vb
        if fr is None:
            return []
        return [view.frame_to_canvas(x, y, fr.shape) for x, y in pts]

    def set_flip(self, text):
        """副摄画面左右方向：自动 / 同向 / 镜像。"""
        mode = {'自动': 0, '同向': 1, '镜像': 2}.get(text, 0)
        self.cfg['side_flip'] = mode
        self.note('副摄方向：' + {'自动': '自动判断', '同向': '同向（不翻转）',
                                  '镜像': '镜像（左右翻转）'}[text] + '，下一帧生效')

    def set_axis(self, text):
        """副摄水平方向对应托盘的哪根轴：自动 / 左右(u) / 前后(v)。"""
        mode = {'自动': 0, '左右': 1, '前后': 2}.get(text, 0)
        self.cfg['side_axis'] = mode
        self.note('副摄对齐轴：' + {'自动': '自动判断', '左右': '左右(u)',
                                    '前后': '前后(v)'}[text] + '，下一帧生效')

    def save(self):
        self.cfg['top_camera_index'] = int(self.tv.get())
        self.cfg['side_camera_index'] = int(self.sv.get())
        self.cfg['top_roi'] = self.roi
        self.cfg['top_points'] = self.points
        self.cfg['side_points'] = self.side_points
        self.cfg['top_rotation'] = self.va.angle
        self.cfg['side_rotation'] = self.vb.angle
        CONFIG.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding='utf8')
        self.note('配置已保存，下次打开自动使用')

    def capture_empty(self):
        if self.tf is None:
            self.note('请先打开双画面')
            return
        frame = self.tf.copy()
        if len(self.points) == 4:
            p = np.array(self.points, dtype='float32')
            s = 700
            dst = np.array([[0, 0], [s, 0], [s, s], [0, s]], dtype='float32')
            frame = cv2.warpPerspective(frame, cv2.getPerspectiveTransform(p, dst), (s, s))
        o = ROOT / 'runtime'
        o.mkdir(exist_ok=True)
        path = o / 'empty_background.png'
        cv2.imwrite(str(path), cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        self.cfg['empty_background'] = str(path)
        self.save()
        self.note('空托盘背景已采集，请移开空托盘后再放货物识别')

    def ensure_segmenter(self):
        if self.segmenter is not None:
            return True
        try:
            self.segmenter = AutoSegmenter(self.cfg.get('sam_model'))
        except Exception as e:
            self.note(str(e))
            messagebox.showerror('缺少SAM权重', str(e))
            return False
        return True

    def toggle_link(self):
        """网络推送：把每帧目标 JSON 广播给局域网内的机器人（TCP，默认 5055）。"""
        want = self.link_var.get()
        self.cfg['link_enabled'] = bool(want)
        if want:
            if self.link is None:
                self.link = link.TargetServer(self.link_port)
            try:
                self.link.start()
            except OSError as e:
                self.link = None
                self.link_var.set(False)
                self.note('网络推送启动失败：%s（端口 %d 可能被占用）' % (e, self.link_port))
                return
            ip = '127.0.0.1'
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
                s.close()
            except OSError:
                pass
            self.note('网络推送已开启：端口 %d，本机 IP %s（机器人连 tcp://%s:%d）'
                      % (self.link_port, ip, ip, self.link_port))
        else:
            if self.link is not None:
                self.link.stop()
            self.note('网络推送已关闭')

    # ------------------------------------------------------------- 识别：双摄
    def detect_targets(self, frame, which='top', stats=None):
        pts = self.points if which == 'top' else self.side_points
        if frame is None or len(pts) != 4:
            return [], None
        t0 = time.perf_counter()
        s = 700
        p = np.array(pts, dtype=np.float32)
        dst = np.array([[0, 0], [s, 0], [s, s], [0, s]], dtype=np.float32)
        H = cv2.getPerspectiveTransform(p, dst)
        warp = cv2.warpPerspective(frame, H, (s, s))
        inv = np.linalg.inv(H)
        t1 = time.perf_counter()
        bg = None
        if which == 'top':
            bgp = self.cfg.get('empty_background')
            if bgp:
                bgi = cv2.imread(str(bgp), cv2.IMREAD_GRAYSCALE)
                if bgi is not None:
                    bg = cv2.warpPerspective(bgi, H, (s, s))
        masks = self.segmenter.segment(warp, bg, self.cfg)
        t2 = time.perf_counter()
        out = []
        for m in masks:
            x, y, w, h = m['bbox']
            corners = np.float32([[[x, y], [x + w, y], [x + w, y + h], [x, y + h]]])
            back = cv2.perspectiveTransform(corners, inv)[0]
            bx, by, bw, bh = cv2.boundingRect(back.astype(np.float32))
            cont = np.asarray(m['contour'], dtype=np.float32).reshape(-1, 1, 2)
            cont_o = cv2.perspectiveTransform(cont, inv).reshape(-1, 2)
            cx, cy = m['center_px']
            ctr = cv2.perspectiveTransform(np.float32([[[cx, cy]]]), inv)[0][0]
            out.append({'view': which,
                        'mask': m['mask'], 'contour': m['contour'],
                        'bbox_warped': [int(v) for v in m['bbox']],
                        'bbox_orig': [int(bx), int(by), int(bw), int(bh)],
                        'contour_orig': cont_o,
                        'center_orig': (float(ctr[0]), float(ctr[1])),
                        'center_tray': m['center_tray'],
                        'area_px': m['area_px'], 'confidence': m['confidence']})
        # 先给每个目标量一次颜色（两路都量），融合时用它判断两路是否指向同一物体
        for it in out:
            try:
                c, cc, _h = classify._color_stats(warp, it['mask'])
                it['color'], it['color_conf'] = c, round(float(cc), 2)
            except Exception:
                it['color'], it['color_conf'] = '未知', 0.0
        if stats is not None:
            t3 = time.perf_counter()
            stats[which + '.warp'] = t1 - t0
            stats[which + '.sam'] = t2 - t1
            stats[which + '.post'] = t3 - t2
        return out, warp

    def detect_and_fuse(self, tf, sf, side_every=1, stats_out=None):
        st = {}
        t0 = time.perf_counter()
        top, tw = self.detect_targets(tf, 'top', st) if tf is not None else ([], None)
        side, sw, reused = [], None, False
        if sf is not None:
            self._side_tick += 1
            if side_every > 1 and self.side_cache is not None \
                    and (self._side_tick % int(side_every)) != 0:
                side, sw = self.side_cache
                reused = True
            else:
                side, sw = self.detect_targets(sf, 'side', st)
                self.side_cache = (side, sw)
        elif self.side_cache is not None:
            side, sw = self.side_cache
            reused = True
        st['side_reused'] = 1.0 if reused else 0.0
        t1 = time.perf_counter()
        info = {}
        fused = fuse(top, side, flip_mode=int(self.cfg.get('side_flip', 0) or 0),
                     axis_mode=int(self.cfg.get('side_axis', 0) or 0), info=info)
        self.fuse_info = info
        t2 = time.perf_counter()
        self.classify_fused(fused, tw, sw)
        t3 = time.perf_counter()
        self.warps = (tw, sw)
        st['fuse'] = t2 - t1
        st['classify'] = t3 - t2
        st['core'] = t3 - t0
        self._merge_times(st)
        if stats_out is not None:
            stats_out.append(dict(st))
        return fused, len(top), len(side)

    def _merge_times(self, st):
        """把单帧各阶段耗时做指数平滑后存进 self.times，供右侧面板显示。"""
        a = .6
        for k, v in st.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            old = self.times.get(k)
            self.times[k] = float(v) if old is None else old * (1 - a) + float(v) * a

    def classify_fused(self, fused, tw, sw):
        for f in fused:
            t, s = f.get('top'), f.get('side')
            if t is not None and tw is not None:
                classify.classify_item(tw, t, self.cfg, self.samples, side_warp=sw, side_item=s)
            if s is not None and sw is not None and t is None:
                classify.classify_item(sw, s, self.cfg, self.samples, side_only=True)

    def write_targets(self, fused):
        o = ROOT / 'runtime'
        o.mkdir(exist_ok=True)
        z = []
        for f in fused:
            t = f.get('top')
            sd = f.get('side')
            it = t or sd
            if it is None:
                continue
            row = {'id': f['id'], 'views': f['views'],
                   'confidence': round(float(f['confidence']), 3),
                   'bbox': list(it['bbox_orig']), 'bbox_warped': list(it['bbox_warped']),
                   'center_px': [round(it['center_orig'][0], 2), round(it['center_orig'][1], 2)],
                   'area_px': it['area_px'],
                   'center_tray': [round(float(it['center_tray'][0]), 4),
                                   round(float(it['center_tray'][1]), 4)],
                   'shape': it.get('shape'), 'shape_conf': it.get('shape_conf'),
                   'color': it.get('color'), 'color_conf': it.get('color_conf'),
                   'class_id': it.get('class_id'), 'class_name': it.get('class_name'),
                   'class_key': it.get('class_key'), 'class_conf': it.get('class_conf'),
                   'class_source': it.get('class_source'),
                   'need_review': bool(it.get('need_review')) or bool(f.get('color_mismatch')),
                   'color_mismatch': bool(f.get('color_mismatch')),
                   'class_votes': it.get('class_votes')}
            if t and sd:
                row['side_bbox'] = list(sd['bbox_orig'])
                row['side_center_px'] = [round(sd['center_orig'][0], 2),
                                         round(sd['center_orig'][1], 2)]
                row['height_ratio'] = round(sd['bbox_warped'][3] / 700.0, 3)
            z.append(row)
        self._seq = getattr(self, '_seq', 0) + 1
        payload = {'schema_version': 5, 'motion_authorized': False, 'points_used': True,
                   'max_classes': 6,
                   'seq': self._seq, 'ts': round(time.time(), 3),
                   'classification_logic': 'rule features on SAM masks (shape+colour) '
                                           'with on-site sample registration',
                   'fusion': dict(getattr(self, 'fuse_info', None) or {}),
                   'class_summary': classify.class_summary(fused),
                   'targets': z}
        tmp = o / f'latest_targets.{threading.get_ident()}.tmp'
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf8')
        tmp.replace(o / 'latest_targets.json')
        if getattr(self, 'link', None) is not None:
            self.link.send(link.control_frame(payload))

    # ------------------------------------------------------------------ 绘制
    def item_color(self, item, fallback):
        cid = str(item.get('class_id') or '')
        return CLS_COLORS.get(cid, fallback)

    def draw_item(self, canvas, view, shape, item, idx, color):
        co = item.get('contour_orig')
        if co is not None and len(co) >= 3:
            line = []
            for px, py in co:
                q = view.frame_to_canvas(float(px), float(py), shape)
                line.extend((q[0], q[1]))
            line.extend((line[0], line[1]))
            canvas.create_line(*line, fill=color, width=2)
        x, y, w, h = item['bbox_orig']
        p1 = view.frame_to_canvas(x, y, shape)
        p2 = view.frame_to_canvas(x + w, y + h, shape)
        canvas.create_rectangle(*p1, *p2, outline=color, width=2)
        name = item.get('class_name') or item.get('shape') or ''
        label = f'#{idx}' + (f' {name}' if name else '')
        self._place_label(canvas, p1, p2, label, color)
        cx, cy = item['center_orig']
        c = view.frame_to_canvas(cx, cy, shape)
        canvas.create_line(c[0] - 7, c[1], c[0] + 7, c[1], fill='#ff3030', width=2)
        canvas.create_line(c[0], c[1] - 7, c[0], c[1] + 7, fill='#ff3030', width=2)

    def _place_label(self, canvas, p1, p2, text, color):
        """把编号标签放在框外，自动避开已经放过的标签，避免多个目标挤在一起时糊成一片。"""
        lw = 9 + 8 * len(text)
        lh = 20
        x = p1[0] + 2
        tries = [max(4, p1[1] - 6), p2[1] + lh + 4]
        for k in range(1, 5):
            tries.append(max(4, p1[1] - 6 - k * lh))
            tries.append(p2[1] + lh + 4 + k * lh)
        for ty in tries:
            box = (x, ty - lh, x + lw, ty)
            if all(box[2] < a[0] or box[0] > a[2] or box[3] < a[1] or box[1] > a[3]
                   for a in self._labels):
                self._labels.append(box)
                canvas.create_text(x, ty - lh + 2, text=text, fill=color, anchor='nw',
                                   font=('Arial', 12, 'bold'))
                return
        canvas.create_text(x, tries[0] - lh + 2, text=text, fill=color, anchor='nw',
                           font=('Arial', 12, 'bold'))

    def draw_targets(self):
        fused = self.fused
        self._labels = []
        if self.tf is not None:
            for f in fused:
                t = f.get('top')
                if t is not None:
                    base = '#ffd400' if f['views'] == 'top+side' else '#ff9f40'
                    self.draw_item(self.ca, self.va, self.tf.shape, t, f['id'],
                                   self.item_color(t, base))
        if self.sf is not None:
            # 一对一融合后每块副摄侧影最多对应一个顶视目标，标签直接取它即可；
            # side-only 目标只画在副摄画面，标签为“副摄待确认”。
            for f in fused:
                s = f.get('side')
                if s is None:
                    continue
                t = f.get('top')
                shown = dict(s)
                if t is not None and t.get('class_name'):
                    shown['class_name'] = t['class_name']
                base = '#ffd400' if f['views'] == 'top+side' else '#ff9f40'
                self.draw_item(self.cb, self.vb, self.sf.shape, shown, f['id'],
                               self.item_color(t or s, base))

    # -------------------------------------------------------------- 实时识别
    def toggle_live(self):
        if self.live:
            self.stop_live()
            return
        if len(self.points) != 4:
            self.note('请先完成顶视四点定位，再开实时分割')
            return
        if not self.ensure_segmenter():
            return
        if len(self.side_points) != 4:
            self.note('副摄未做四点定位：先按单顶视实时分割，点“副摄四点框选”可启用双摄融合')
        self.live = True
        self.live_err = None
        self.live_msg = None
        self.live_btn.config(text='停止实时')
        self.note('实时分割启动中（首次加载SAM约需数秒）...')
        self.live_thread = threading.Thread(target=self.live_loop, daemon=True)
        self.live_thread.start()

    def stop_live(self):
        self.live = False
        self.live_msg = None
        self.live_err = None
        self.live_status = None
        self.register_mode = False
        if hasattr(self, 'live_btn'):
            self.live_btn.config(text='实时分割')
        with self.sam_lock:
            self.fused = []
        n = getattr(self, '_live_n', 0)
        tot = getattr(self, '_live_sum', 0.0)
        self._live_n, self._live_sum = 0, 0.0
        if n > 1 and tot > 0:
            self.note(f'实时分割已停止：{n} 帧，平均 {tot / n:.2f}s/帧'
                      f'（约 {n / tot:.2f} fps）')
        else:
            self.note('实时分割已停止')

    def sync_live(self):
        if not hasattr(self, 'live_btn'):
            return
        if self.live_msg is not None:
            self.live_status = self.live_msg
            self.live_msg = None
        if not self.live and self.live_err:
            msg = self.live_err
            self.live_err = None
            self.note(msg)
            self.live_btn.config(text='实时分割')

    def live_loop(self):
        fail = 0
        while self.live:
            if len(self.points) != 4:
                time.sleep(.2)
                continue
            fr = self.a.get() if self.a else None
            if fr is None:
                time.sleep(.05)
                continue
            side_fr = (self.bstream.get() if (self.bstream is not None
                                              and len(self.side_points) == 4) else None)
            t0 = time.perf_counter()
            try:
                fused, ntop, nside = self.detect_and_fuse(
                    fr, side_fr, int(self.cfg.get('side_every', 2) or 1))
            except Exception as e:
                fail += 1
                if fail >= 5:
                    self.live = False
                    self.live_err = f'实时分割失败：{e}'
                    return
                time.sleep(.3)
                continue
            fail = 0
            if not self.live:
                break
            dt = max(1e-3, time.perf_counter() - t0)
            self.det_fps = (self.det_fps * .7 + .3 * (1.0 / dt)) if self.det_fps else 1.0 / dt
            with self.sam_lock:
                self.fused = fused
            w0 = time.perf_counter()
            self.write_targets(fused)
            dt_all = time.perf_counter() - t0
            self._live_n = getattr(self, '_live_n', 0) + 1
            self._live_sum = getattr(self, '_live_sum', 0.0) + dt_all
            self._merge_times({'write': time.perf_counter() - w0,
                               'frame': dt_all})
            self.live_msg = (f'实时分割：{len(fused)} 个目标（顶视{ntop}/副摄{nside}） '
                             f'{self.det_fps:.2f} fps')

    # -------------------------------------------------------------- 样本登记
    def toggle_register(self):
        if self.register_mode:
            self.register_mode = False
            self.note('已退出登记模式')
            return
        if not self.fused:
            self.note('先“单次识别”或开“实时分割”，有目标后才能登记样本')
            return
        self.register_mode = True
        self.point_mode = None
        self.note('登记样本：点击画面中的物体，输入类别编号/名称；右键或 Esc 取消')

    def canvas_click(self, e, which):
        if self.point_click(e, which):
            return True
        if not self.register_mode:
            return False
        fr = self.tf if which == 'top' else self.sf
        view = self.va if which == 'top' else self.vb
        if fr is None:
            return True
        fx, fy = view.canvas_to_frame(e.x, e.y, fr.shape)
        item = self.pick_item(fx, fy, which)
        if item is None:
            self.note('没点到目标：请点目标实体（框内）上')
            return True
        self.register_mode = False
        self.register_item(item, which)
        return True

    def pick_item(self, fx, fy, which):
        best = None
        for f in self.fused:
            it = f.get(which)
            if it is None:
                continue
            x, y, w, h = it['bbox_orig']
            if x - 10 <= fx <= x + w + 10 and y - 10 <= fy <= y + h + 10:
                return it
        return best

    def register_item(self, item, which):
        if which != 'top':
            self.note('副摄单独目标不能登记：请点顶视画面里的目标')
            return
        warp = self.warps[0]
        if warp is None:
            self.note('没有可用的顶视校正图，请先识别一次')
            return
        if item.get('feature') is None:
            classify.classify_item(warp, item, self.cfg, self.samples,
                                   side_warp=self.warps[1], side_item=None)
        s = simpledialog.askstring('登记样本',
                                   '输入类别编号或名称（例如 1、2… 或 “五棱体·黄”）',
                                   parent=self.r)
        if not s:
            self.note('已取消登记')
            return
        s = s.strip()
        cid = s
        name = f'类别{s}' if s.isdigit() else s
        c = classify.register(item, cid, name, self.cfg, self.samples)
        if c is None:
            self.note('类别名称为空，未登记')
            return
        self.save()
        self.write_targets(self.fused)
        self.note(f'已登记 {name}：{item.get("shape")} + {item.get("color")}，'
                  f'该类别样本 {len(c["samples"])} 个')

    # ------------------------------------------------------------------ 单次识别
    def vote_runs(self, runs):
        base = runs[0][0]
        if len(runs) < 2:
            return base
        for f in base:
            it = f.get('top') or f.get('side')
            if it is None:
                continue
            cu, cv_ = it['center_tray']
            votes = {'class_key': [], 'shape': [], 'color': []}
            for fused, _tw, _sw in runs:
                best = None
                for g in fused:
                    gi = g.get('top') or g.get('side')
                    if gi is None:
                        continue
                    d = abs(gi['center_tray'][0] - cu) + abs(gi['center_tray'][1] - cv_)
                    if d <= .06 and (best is None or d < best[0]):
                        best = (d, gi)
                if best is None:
                    continue
                gi = best[1]
                for k in votes:
                    if gi.get(k):
                        votes[k].append(gi[k])
            for k, arr in votes.items():
                if not arr:
                    continue
                val, cnt = Counter(arr).most_common(1)[0]
                if k == 'class_key':
                    aligned = classify._lookup(val, *val.split('|', 1), self.cfg,
                                               self.samples, it.get('feature'))
                    it.update(aligned)
                    it['class_votes'] = f'{cnt}/{len(arr)}'
                else:
                    it[k] = val
        return base

    def recognize(self):
        if self.tf is None:
            return
        if len(self.points) != 4:
            self.note('请先完成顶视四点定位，再识别')
            messagebox.showwarning('缺少识别区域', '请先点击“顶视四点框选”，依次选择四个角点')
            return
        if not self.ensure_segmenter():
            return
        n = int(self.cfg.get('frames_for_vote', 3) or 3)
        n = max(2, min(5, n))
        self.note(f'双摄识别中（{n} 帧投票）...')
        self.r.update_idletasks()
        runs = []
        stats_all = []
        t_start = time.perf_counter()
        try:
            for _ in range(n):
                tf = self.a.get() if self.a else self.tf
                sf = (self.bstream.get() if (self.bstream is not None
                                             and len(self.side_points) == 4) else None)
                if tf is None:
                    break
                r0 = time.perf_counter()
                runs.append(self.detect_and_fuse(tf, sf, stats_out=stats_all))
                self._merge_times({'frame': time.perf_counter() - r0})
                time.sleep(.08)
        except Exception as e:
            self.note(f'识别失败：{e}')
            return
        if not runs:
            self.note('没有可用画面')
            return
        fused = self.vote_runs(runs)
        ntop = sum(1 for f in fused if f.get('top'))
        nside = sum(1 for f in fused if f.get('side'))
        with self.sam_lock:
            self.fused = fused
        w0 = time.perf_counter()
        self.write_targets(fused)
        wr = time.perf_counter() - w0
        self._merge_times({'write': wr})
        total = time.perf_counter() - t_start
        # 本次识别的各阶段累计耗时（{n} 帧相加，不是单帧平均）
        acc = {}
        for st in stats_all:
            for k, v in st.items():
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                acc[k] = acc.get(k, 0.0) + float(v)
        tw_ = sum(acc.get(k, 0.0) for k in ('top.warp', 'top.sam', 'top.post'))
        sw_ = sum(acc.get(k, 0.0) for k in ('side.warp', 'side.sam', 'side.post'))
        cs = classify.class_summary(fused)
        ctxt = ' '.join(f'{k}×{v}' for k, v in cs.items())
        self.note(f'单次识别完成：{len(fused)} 个目标（顶视{ntop}/副摄{nside}，{n} 帧投票），'
                  f'用时 {total:.2f}s（顶视 {tw_:.2f}s / 副摄 {sw_:.2f}s / 融合 '
                  f'{acc.get("fuse", 0.0):.2f}s / 分类 {acc.get("classify", 0.0):.2f}s / '
                  f'写JSON {wr:.2f}s）| {ctxt}')


if __name__ == '__main__':
    r = tk.Tk()
    a = App(r)
    r.protocol('WM_DELETE_WINDOW', lambda: (a.stop(), r.destroy()))
    r.mainloop()

