import json
import queue
import socket
import threading
import time
import tkinter as tk
import traceback
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
from camera_factory import make_camera
from fusion import fuse

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'config.json'
MARKS = ROOT / 'runtime' / 'calib_marks.json'

# 类别配色（最多六类，未知/待登记用琥珀色）
CLS_COLORS = {'1': '#ff6b6b', '2': '#ffd400', '3': '#6ee06e',
              '4': '#5cc8ff', '5': '#c58cff', '6': '#ff9f40'}


class Stream:
    def __init__(self, i, w, h, f):
        self.cam = make_camera(i, w, h, f)
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
    def _vlm_crop_now(self):
        """取当前目标区域（没有目标就取整幅校正图）给 DeepSeek 看。"""
        tf = self.tf
        if tf is None or len(getattr(self, 'points', [])) != 4:
            return None
        s = 700
        src = np.array(self.points, dtype=np.float32)
        dst = np.array([[0, 0], [s, 0], [s, s], [0, s]], dtype=np.float32)
        H = cv2.getPerspectiveTransform(src, dst)
        warp = cv2.warpPerspective(tf, H, (s, s))
        crop = None
        try:
            items = getattr(self, 'last_items', None) or []
            if items:
                x, y, w, h = items[0].get('bbox_warped') or (0, 0, 0, 0)
                if w > 10 and h > 10:
                    pad = int(max(w, h) * 0.25)
                    x0, y0 = max(0, x - pad), max(0, y - pad)
                    x1, y1 = min(s, x + w + pad), min(s, y + h + pad)
                    crop = warp[y0:y1, x0:x1]
        except Exception:                     # noqa: BLE001
            crop = None
        return crop if crop is not None and crop.size else warp

    def on_cloud_check(self):
        """用云端 DeepSeek 复核当前画面里的物块（需联网）。"""
        try:
            import cloud_vlm as CV
        except Exception as exc:                                  # noqa: BLE001
            messagebox.showerror('DeepSeek 复核', '模块不可用：%s' % exc, parent=self)
            return
        if not CV.available():
            messagebox.showwarning('DeepSeek 复核',
                                   '云端不可用（没网 / 没配 DEEPSEEK_API_KEY）。\n'
                                   '可把 key 写进 vision\\deepseek_key.txt 或设环境变量。',
                                   parent=self)
            return
        frame = self._vlm_crop_now()
        if frame is None:
            self.note('DeepSeek 复核：没有可用的画面（先打开双画面）')
            return
        self.note('DeepSeek 复核中…')

        def work():
            r = CV.ask_objects(frame)
            self.after(0, lambda: self._cloud_done(r))

        threading.Thread(target=work, daemon=True).start()

    def _cloud_done(self, r):
        if not r or not r.get('objects'):
            self.note('DeepSeek 复核失败（网络/额度/解析）')
            messagebox.showwarning('DeepSeek 复核', '没拿到结果，可能是网络或额度问题', parent=self)
            return
        lines = []
        for i, o in enumerate(r['objects'][:8], 1):
            lines.append('#%d %s + %s（conf %.2f）%s' % (
                i, o.get('shape'), o.get('color') or '?', float(o.get('conf') or 0),
                (' · ' + o['reason']) if o.get('reason') else ''))
        text = '用时 %d ms\n\n' % int(r.get('ms') or 0) + '\n'.join(lines)
        self.note('DeepSeek 复核：' + ' | '.join(lines)[:200])
        messagebox.showinfo('DeepSeek 复核', text, parent=self)

    def __init__(self, r, embedded=False, ctrl=None, with_ctrl=True):
        self.r = r
        self.embedded = bool(embedded)
        self.ctrl = ctrl
        self.with_ctrl = bool(with_ctrl)
        self.calib = None
        self._tree_sig = None
        self._axis_labels = {}          # with_ctrl=False 时没有控制面板
        self.ctrl_state = None
        if not self.embedded:
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
        self.mark_mode = False
        self.marks = self.load_marks()
        self.segmenter = None
        self.samples = classify.load_samples()
        self.fused = []
        self.sam_lock = threading.Lock()
        self._rec_q = queue.Queue()
        self._rec_busy = False
        self._rec_cb = None
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
        if self.ctrl is not None and self.with_ctrl:
            self.r.after(800, self._ctrl_tick)
        if not self.embedded:
            self.r.after(300, self.start)

    # ------------------------------------------------------------------ 界面
    def build(self):
        bar = ttk.Frame(self.r, padding=8)
        bar.pack(fill='x')
        ttk.Label(bar, text='顶视相机').pack(side='left')
        self.tv = tk.StringVar(value=str(self.cfg.get('top_camera_index', 3)))
        self.tc = ttk.Combobox(bar, textvariable=self.tv, width=6, state='normal')
        self.tc.pack(side='left', padx=4)
        ttk.Label(bar, text='侧视相机').pack(side='left')
        self.sv = tk.StringVar(value=str(self.cfg.get('side_camera_index', 0)))
        self.sc = ttk.Combobox(bar, textvariable=self.sv, width=6, state='normal')
        self.sc.pack(side='left', padx=4)
        ttk.Button(bar, text='扫描相机', command=self.scan).pack(side='left', padx=5)
        ttk.Button(bar, text='打开双画面', command=self.start).pack(side='left')
        ttk.Button(bar, text='停止', command=self.stop).pack(side='left')
        self.gray = tk.BooleanVar(value=bool(self.cfg.get('gray_objects', False)))
        ttk.Checkbutton(bar, text='含灰/黑物料', variable=self.gray,
                        command=lambda: self.cfg.__setitem__('gray_objects', self.gray.get())
                        ).pack(side='left', padx=4)
        self.st = tk.StringVar(value='先扫描相机')

        body = ttk.Frame(self.r, padding=(8, 4, 8, 0))
        body.pack(fill='both', expand=True)
        self._body = body

        # 右侧：控制面板（独立运行）或 目标列表 + 抓取按钮（一体化）
        rp = ttk.Frame(body, width=440)
        rp.pack(side='right', fill='y', padx=(8, 0))
        rp.pack_propagate(False)
        if self.with_ctrl:
            self._build_ctrl(rp)
        else:
            self._build_targets(rp)
        ttk.Label(rp, text='目标详情 / 状态（所有文字信息）',
                  font=('Microsoft YaHei', 10, 'bold')).pack(anchor='w', pady=(6, 0))
        tf = ttk.Frame(rp)
        tf.pack(fill='both', expand=True)
        self.txt = tk.Text(tf, width=44, wrap='word', bg='#1b1c1f', fg='#d7dde4',
                           relief='flat', font=('Consolas', 10), padx=8, pady=6,
                           spacing1=1, insertwidth=0)
        self.txt.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(tf, orient='vertical', command=self.txt.yview)
        sb.pack(side='right', fill='y')
        self.txt.configure(yscrollcommand=sb.set, state='disabled')
        self.txt.tag_configure('hd', foreground='#7fd1ff',
                               font=('Consolas', 10, 'bold'), spacing1=8)
        self.txt.tag_configure('wr', foreground='#ff9f40')

        # 左列：两路画面上下叠放（上下各占一半高度）
        left = ttk.Frame(body)
        left.pack(side='left', fill='both', expand=True)
        top_box = ttk.Frame(left)
        top_box.pack(fill='both', expand=True)
        ttk.Label(top_box, text='顶视：四点模式依次点左上、右上、右下、左下；右键撤销'
                  ).pack(anchor='w')
        self.ca = tk.Canvas(top_box, bg='#202124', highlightthickness=0, height=10)
        self.ca.pack(fill='both', expand=True)
        bot_box = ttk.Frame(left)
        bot_box.pack(fill='both', expand=True)
        ttk.Label(bot_box, text='副摄画面：可独立旋转和四点定位（用于双摄融合与类别核对）'
                  ).pack(anchor='w')
        self.cb = tk.Canvas(bot_box, bg='#202124', highlightthickness=0, height=10)
        self.cb.pack(fill='both', expand=True)
        self.va = View(self.ca, lambda e: self.canvas_click(e, 'top'))
        self.vb = View(self.cb, lambda e: self.canvas_click(e, 'side'))
        self.ca.bind('<Button-3>', lambda e: self.undo_point(e, 'top'))
        self.cb.bind('<Button-3>', lambda e: self.undo_point(e, 'side'))
        self.update_text()

        # ---------------- 基础工具条（现场只用这些） ----------------
        bot = ttk.Frame(self.r, padding=(8, 4, 8, 2))
        bot.pack(fill='x', before=body)
        self.bot_frame = bot
        ttk.Button(bot, text='顶视四点框选',
                   command=lambda: self.start_points('top')).pack(side='left', padx=3)
        ttk.Button(bot, text='副摄四点框选',
                   command=lambda: self.start_points('side')).pack(side='left', padx=3)
        ttk.Button(bot, text='清除定位', command=self.clear).pack(side='left', padx=3)
        ttk.Button(bot, text='采集空托盘', command=self.capture_empty).pack(side='left', padx=3)
        ttk.Button(bot, text='DeepSeek复核', command=self.on_cloud_check).pack(side='left', padx=3)
        ttk.Button(bot, text='交换主副摄', command=self.swap_cams).pack(side='left', padx=3)
        self.adv_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bot, text='高级', variable=self.adv_var,
                        command=self.toggle_adv).pack(side='right', padx=6)

        # ---------------- 识别行 ----------------
        rrow = ttk.Frame(self.r, padding=(8, 0, 8, 2))
        rrow.pack(fill='x', before=body)
        self.rec_btn = ttk.Button(rrow, text='单次识别', width=12,
                                  command=self.recognize_safe)
        self.rec_btn.pack(side='left', padx=(0, 6))
        self.rec_pb = ttk.Progressbar(rrow, mode='determinate', length=260,
                                      maximum=100, value=0)
        self.rec_pb.pack(side='left', padx=(0, 8))
        self.rec_pb_lbl = ttk.Label(rrow, text='空闲', width=38)
        self.rec_pb_lbl.pack(side='left', padx=(0, 10))
        self.live_btn = ttk.Button(rrow, text='实时分割', command=self.toggle_live)
        self.live_btn.pack(side='left', padx=3)

        # ---------------- 高级工具条（默认隐藏） ----------------
        self.adv_bar = ttk.Frame(self.r, padding=(8, 0, 8, 2))
        for name, v in [('顶视', self.va), ('侧视', self.vb)]:
            ttk.Label(self.adv_bar, text=name).pack(side='left', padx=(8, 2))
            ttk.Button(self.adv_bar, text='↶左转90°',
                       command=lambda v=v: self.rotate(v, -90)).pack(side='left', padx=1)
            ttk.Button(self.adv_bar, text='↷右转90°',
                       command=lambda v=v: self.rotate(v, 90)).pack(side='left', padx=1)
            ttk.Button(self.adv_bar, text='复位',
                       command=lambda v=v: self.reset_view(v)).pack(side='left', padx=2)
        ttk.Label(self.adv_bar, text='副摄方向').pack(side='left', padx=(8, 2))
        self.flip_var = tk.StringVar(value={0: '自动', 1: '同向', 2: '镜像'}.get(
            int(self.cfg.get('side_flip', 0) or 0), '自动'))
        cb = ttk.Combobox(self.adv_bar, textvariable=self.flip_var,
                          values=['自动', '同向', '镜像'], width=5, state='readonly')
        cb.pack(side='left')
        cb.bind('<<ComboboxSelected>>', lambda e: self.set_flip(self.flip_var.get()))
        ttk.Label(self.adv_bar, text='对齐轴').pack(side='left', padx=(8, 2))
        self.axis_var = tk.StringVar(value={0: '自动', 1: '左右', 2: '前后'}.get(
            int(self.cfg.get('side_axis', 0) or 0), '自动'))
        cba = ttk.Combobox(self.adv_bar, textvariable=self.axis_var,
                           values=['自动', '左右', '前后'], width=5, state='readonly')
        cba.pack(side='left')
        cba.bind('<<ComboboxSelected>>', lambda e: self.set_axis(self.axis_var.get()))
        ttk.Button(self.adv_bar, text='保存配置', command=self.save).pack(side='left', padx=6)
        self.link_var = tk.BooleanVar(value=bool(self.cfg.get('link_enabled', False)))
        ttk.Checkbutton(self.adv_bar, text='网络推送', variable=self.link_var,
                        command=self.toggle_link).pack(side='left', padx=4)
        self.reg_btn = ttk.Button(self.adv_bar, text='登记样本', command=self.toggle_register)
        self.reg_btn.pack(side='left', padx=3)
        self.mark_btn = ttk.Button(self.adv_bar, text='标注点', command=self.toggle_marks)
        self.mark_btn.pack(side='left', padx=3)
        ttk.Button(self.adv_bar, text='清空标注',
                   command=self.clear_marks).pack(side='left', padx=3)

    # ------------------------------------------------------------ 四轴/夹爪
    # ------------------------------------------------- 高级条 / 目标列表
    def toggle_adv(self):
        try:
            if self.adv_var.get():
                self.adv_bar.pack(fill='x', before=self._body)
            else:
                self.adv_bar.pack_forget()
        except Exception:                       # noqa: BLE001
            pass

    def _build_targets(self, parent):
        """一体化模式右栏：目标列表 + 抓取按钮（替代原本的四轴点动面板）。"""
        ttk.Label(parent, text='识别目标（点一行看明细）',
                  font=('Microsoft YaHei', 10, 'bold')).pack(anchor='w')
        cols = ('cls', 'uv', 'conf')
        self.tree = ttk.Treeview(parent, columns=cols, show='headings', height=7)
        for key, title, width in (('cls', '类别', 168), ('uv', 'u , v', 96),
                                  ('conf', '置信/投票', 96)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor='w', stretch=False)
        self.tree.pack(fill='x', pady=(2, 4))
        self.tree.bind('<<TreeviewSelect>>', self._on_target_pick)

        r1 = ttk.Frame(parent)
        r1.pack(fill='x', pady=(2, 0))
        self.drop_var2 = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text='抓完落货（转盘+松爪）',
                        variable=self.drop_var2).pack(side='left')
        ttk.Label(r1, text='件数').pack(side='left', padx=(10, 2))
        self.many_var2 = tk.StringVar(value='6')
        ttk.Entry(r1, textvariable=self.many_var2, width=4).pack(side='left')

        r2 = ttk.Frame(parent)
        r2.pack(fill='x', pady=(4, 2))
        self.grab_one_btn = ttk.Button(r2, text='自动抓一件', command=self.on_grab_one)
        self.grab_one_btn.pack(side='left')
        ttk.Button(r2, text='连续抓', command=self.on_grab_many).pack(side='left', padx=6)
        ttk.Button(r2, text='分盒清零', command=self.on_box_reset).pack(side='left')

    def attach_calib(self, calib):
        """main.py 建好标定页后调用：视觉页的抓取按钮直接复用标定页的实现。"""
        self.calib = calib

    def _sync_calib_opts(self):
        calib = getattr(self, 'calib', None)
        if calib is None:
            self.note('标定页没加载，抓取按钮不可用')
            return None
        try:
            if hasattr(calib, 'drop_var') and hasattr(self, 'drop_var2'):
                calib.drop_var.set(bool(self.drop_var2.get()))
            if hasattr(calib, 'many_var') and hasattr(self, 'many_var2'):
                calib.many_var.set(str(self.many_var2.get()).strip() or '6')
        except Exception:                       # noqa: BLE001
            pass
        return calib

    def on_grab_one(self):
        calib = self._sync_calib_opts()
        if calib is None:
            return
        try:
            calib.on_auto_grab()
        except Exception as exc:                # noqa: BLE001
            self.note('自动抓一件失败：%s: %s' % (type(exc).__name__, exc))

    def on_grab_many(self):
        calib = self._sync_calib_opts()
        if calib is None:
            return
        try:
            calib.on_auto_many()
        except Exception as exc:                # noqa: BLE001
            self.note('连续抓失败：%s: %s' % (type(exc).__name__, exc))

    def on_box_reset(self):
        calib = _calib = getattr(self, 'calib', None)
        if _calib is None:
            self.note('标定页没加载，分盒清零不可用')
            return
        try:
            _calib.on_box_reset()
        except Exception as exc:                # noqa: BLE001
            self.note('分盒清零失败：%s: %s' % (type(exc).__name__, exc))

    def _fill_targets(self):
        """把当前识别结果刷到列表里（内容没变就不重建，避免闪）。"""
        with self.sam_lock:
            fused = list(self.fused or [])
        rows = []
        for i, item in enumerate(fused, start=1):
            t = item.get('top') or item.get('side') or {}
            uv = t.get('center_tray') or [0.0, 0.0]
            conf = item.get('confidence')
            conf = float(conf) if isinstance(conf, (int, float)) else 0.0
            votes = t.get('class_votes')
            cls = t.get('class_name') or t.get('class_key') or '待登记'
            if t.get('shape'):
                cls = '%s | %s' % (t.get('shape'), t.get('color') or '')
            rows.append((i, cls, '%.3f , %.3f' % (uv[0], uv[1]),
                         '%.2f%s' % (conf, (' ' + votes) if votes else '')))
        sig = tuple((r[1], r[2], r[3]) for r in rows)
        if sig == self._tree_sig:
            return
        self._tree_sig = sig
        try:
            self.tree.delete(*self.tree.get_children())
            for i, cls, uv, cf in rows:
                self.tree.insert('', 'end', iid=str(i), values=(cls, uv, cf))
        except Exception:                       # noqa: BLE001
            pass

    def _on_target_pick(self, _event=None):
        try:
            sel = self.tree.selection()
            if not sel:
                return
            idx = int(sel[0]) - 1
            with self.sam_lock:
                fused = list(self.fused or [])
            if 0 <= idx < len(fused):
                item = fused[idx]
                t = item.get('top') or item.get('side') or {}
                uv = t.get('center_tray') or [0, 0]
                self.note('选中 #%d：%s（u=%.3f v=%.3f，视图 %s，投票 %s）'
                          % (idx + 1, t.get('shape') or '待登记', uv[0], uv[1],
                             item.get('views', '?'), t.get('class_votes', '-')))
        except Exception:                       # noqa: BLE001
            pass

    def _build_ctrl(self, parent):
        box = ttk.LabelFrame(parent, text='四轴 / 夹爪', padding=6)
        box.pack(fill='x')
        self._axis_labels = {}
        self.jog_var = tk.StringVar(value='500')
        self.var_flip_x = tk.BooleanVar(value=False)
        self.var_flip_y = tk.BooleanVar(value=False)
        for name, ax in (('X', 4), ('Y', 5), ('Z', 1), ('托盘', 3)):
            row = ttk.Frame(box)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=name, width=4).pack(side='left')
            ttk.Button(row, text='－', width=3,
                       command=lambda a=ax: self.ctrl_move(a, -1)).pack(side='left')
            ttk.Button(row, text='＋', width=3,
                       command=lambda a=ax: self.ctrl_move(a, +1)).pack(side='left', padx=2)
            lbl = ttk.Label(row, text='—', width=12)
            lbl.pack(side='left', padx=(6, 0))
            self._axis_labels[ax] = lbl
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(4, 0))
        ttk.Label(row, text='脉冲').pack(side='left')
        ttk.Entry(row, textvariable=self.jog_var, width=6).pack(side='left', padx=4)
        ttk.Button(row, text='使能', command=self.ctrl_enable).pack(side='left')
        ttk.Button(row, text='停止', command=self.ctrl_stop).pack(side='left', padx=(2, 0))
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(2, 0))
        ttk.Checkbutton(row, text='X取反', variable=self.var_flip_x).pack(side='left')
        ttk.Checkbutton(row, text='Y取反', variable=self.var_flip_y).pack(side='left', padx=(8, 0))
        ttk.Separator(box).pack(fill='x', pady=6)
        row = ttk.Frame(box)
        row.pack(fill='x')
        ttk.Label(row, text='夹爪').pack(side='left')
        for text, act in (('夹紧', 'grab'), ('松开', 'open'),
                          ('收紧一步', 'step_grab'), ('松开一步', 'step_release')):
            ttk.Button(row, text=text, width=8,
                       command=lambda a=act: self.ctrl_gripper(a)).pack(side='left', padx=1)
        row = ttk.Frame(box)
        row.pack(fill='x', pady=(4, 0))
        ttk.Label(row, text='舵机角度').pack(side='left')
        self.servo_var = tk.StringVar(value='180')
        ttk.Entry(row, textvariable=self.servo_var, width=6).pack(side='left', padx=4)
        ttk.Button(row, text='下发', command=self.ctrl_servo).pack(side='left')
        self.ctrl_state = ttk.Label(box, text='未连接控制板', foreground='#b00020')
        self.ctrl_state.pack(anchor='w', pady=(4, 0))

    def _ctrl_client(self):
        return getattr(getattr(self, 'ctrl', None), 'client', None)

    def _ctrl_ready(self):
        c = self._ctrl_client()
        if c is None or not getattr(c, 'online', False):
            self.note('控制板未连接：先在顶栏点「连接」')
            return None
        return c

    def ctrl_move(self, axis, sign):
        c = self._ctrl_ready()
        if c is None:
            return
        try:
            pulses = int((self.jog_var.get() or '').strip())
        except ValueError:
            self.note('点动脉冲要填整数，例如 500')
            return
        if pulses <= 0:
            self.note('点动脉冲要大于 0（方向用按钮/取反控制）')
            return
        direction = 1 if sign < 0 else 0
        if axis == 4 and self.var_flip_x.get():
            direction ^= 1
        if axis == 5 and self.var_flip_y.get():
            direction ^= 1
        name = {4: 'X', 5: 'Y', 1: 'Z', 3: '托盘'}.get(axis, str(axis))
        c.send('motor_enable', axis, 1)
        self.r.after(120, lambda: c.send('motor_pos', axis, direction, 60, 100,
                                        pulses, 'rel'))
        self.note('点动 %s%s：%d 脉冲（方向 %d，60rpm）'
                  % (name, '+' if sign > 0 else '−', pulses, direction))

    def ctrl_enable(self):
        c = self._ctrl_ready()
        if c is None:
            return
        for ax in (4, 5, 1, 3):
            c.send('motor_enable', ax, 1)
        self.note('已下发：四轴使能')

    def ctrl_stop(self):
        c = self._ctrl_ready()
        if c is None:
            return
        c.send('stop_all')
        self.note('已下发：停止所有轴')

    def ctrl_gripper(self, action):
        c = self._ctrl_ready()
        if c is None:
            return
        c.send('gripper', action, timeout_ms=2000)
        self.note('夹爪：' + action)

    def ctrl_servo(self):
        c = self._ctrl_ready()
        if c is None:
            return
        try:
            deg = int((self.servo_var.get() or '').strip())
        except ValueError:
            self.note('舵机角度要填整数')
            return
        c.send('servo_angle', deg)
        self.note('舵机角度：%d°' % deg)

    def _ctrl_tick(self):
        try:
            ctrl = getattr(self, 'ctrl', None)
            if ctrl is not None:
                soft = getattr(ctrl, 'soft_pos', None) or {}
                known = getattr(ctrl, 'pos_known', None) or {}
                for ax, lbl in self._axis_labels.items():
                    val = int(soft.get(ax, 0))
                    ok = bool(known.get(ax, True))
                    lbl.configure(text=('%+d' % val) if ok else '未知',
                                  foreground='#333333' if ok else '#b00020')
                online = getattr(getattr(ctrl, 'client', None), 'online', False)
                if self.ctrl_state is not None:
                    self.ctrl_state.configure(
                        text='已连接（软位置）' if online else '未连接控制板',
                        foreground='#1b5e20' if online else '#b00020')
        finally:
            self.r.after(800, self._ctrl_tick)

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
             + (' · 标注模式' if self.mark_mode else '')
             + (' · 登记模式' if self.register_mode else ''),
             '【帧率】刷新 %.1f | 顶视 %.1f | 副摄 %.1f fps' % (self.disp_fps, fa, fb)]
        if getattr(self, 'live_status', None):
            L.append('【状态】' + self.live_status)
        L += [
             '【分割】%.2f fps   融合目标 %d 个' % (self.det_fps, len(fused))]
        if self.marks:
            L.append('【标注】%d 个标定点（给上位机标定用），最近 u=%.3f v=%.3f'
                     % (len(self.marks), self.marks[-1]['u'], self.marks[-1]['v']))
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
    def _probe_remote_cam(self, host, port, cam_id):
        """问派端：这个槽位有没有相机。有 → True。"""
        import socket
        import wire
        try:
            s = socket.create_connection((host, int(port)), timeout=2.0)
            try:
                s.sendall(wire.pack_request(cam_id, 0, 0, 0))
                head = wire.recv_exact(s, 2)
                if head == wire.MAGIC_IMG:
                    return True
                return False
            finally:
                s.close()
        except Exception:            # noqa: BLE001
            return False

    def _camera_choices(self):
        """当前可用的相机编号：远程→派端槽位；本地→USB 相机索引。"""
        try:
            import camera_factory as CF
            if CF.is_remote():
                host = CF.remote_settings().get('host')
                port = CF.remote_settings().get('img_port')
                found = [str(i) for i in range(4)
                         if self._probe_remote_cam(host, port, i)]
                if found:
                    return found, '派端槽位'
                return [str(i) for i in range(4)], '派端槽位（未探测到，按 0~3 可选）'
        except Exception:            # noqa: BLE001
            pass
        found = []
        for i in range(10):
            c = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if c.isOpened():
                ok, fr = c.read()
                if ok and fr is not None:
                    found.append(str(i))
            c.release()
        return found, '本机相机'

    def scan(self):
        try:
            f, kind = self._camera_choices()
        except Exception as exc:     # noqa: BLE001
            self.note('扫描相机失败：%s' % exc)
            return
        self.tc['values'] = f
        self.sc['values'] = f
        # 当前值不在列表里就补进去，避免下拉看起来是空的
        for var, cb in ((self.tv, self.tc), (self.sv, self.sc)):
            cur = str(var.get()).strip()
            if cur and cur not in f:
                cb['values'] = list(f) + [cur]
        if f:
            self.note('发现%s：%s' % (kind, ','.join(f)))
        else:
            self.note('没有发现相机（%s）' % kind)

    def start(self):
        self.stop()
        try:
            def _idx(var, dflt):
                try:
                    return int(str(var.get()).strip())
                except Exception:    # noqa: BLE001
                    return int(dflt)

            top_i = _idx(self.tv, self.cfg.get('top_camera_index', 0))
            side_i = _idx(self.sv, self.cfg.get('side_camera_index', 1))
            self.tv.set(str(top_i))
            self.sv.set(str(side_i))
            self.a = Stream(top_i, self.cfg.get('width', 1920),
                            self.cfg.get('height', 1080), self.cfg.get('fps', 15))
            self.bstream = Stream(side_i, self.cfg.get('width', 1920),
                                  self.cfg.get('height', 1080), self.cfg.get('fps', 15))
            self.a.start()
            self.bstream.start()
            self.running = True
            self.note('双画面运行中')
            self._loop_armed = False
            self._schedule_loop()
            if not getattr(self, '_rec_pump_armed', False):
                self._rec_pump_armed = True
                self.r.after(120, self._rec_pump)
        except Exception as e:
            messagebox.showerror('打开失败', str(e))

    def refresh(self):
        """画一帧。注意：这里**不再**用 after 续期。

        以前每调用一次 refresh() 就多挂一条 80ms 定时器，而切页时
        main._on_tab 也会调 refresh() → 定时器越滚越多，界面就卡死了。
        续期统一交给 _loop / _schedule_loop。
        """
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
        if getattr(self, 'tree', None) is not None:
            self._fill_targets()

    def _loop(self):
        self._loop_armed = False
        if not self.running:
            return
        try:
            self.refresh()
        finally:
            self._schedule_loop()

    def _schedule_loop(self):
        if getattr(self, '_loop_armed', False) or not self.running:
            return
        self._loop_armed = True
        self.r.after(80, self._loop)

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
        if self.mark_mode:
            self.mark_mode = False
            if hasattr(self, 'mark_btn'):
                self.mark_btn.config(text='标注点')
            self.note('已结束标注：共 %d 个点' % len(self.marks))
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
        if self.mark_mode:
            self.undo_mark()
            return
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

    def swap_cams(self):
        """两路相机插反时的应急开关：交换 cam_map（顶视 ↔ 副摄）并立即重连画面。"""
        try:
            remote = self.cfg.setdefault('remote', {})
            cm = remote.get('cam_map') or {'1': 1, '2': 0}
            cm = {str(k): int(v) for k, v in cm.items()}
            cm = {k: (1 if v == 0 else 0) for k, v in cm.items()}
            remote['cam_map'] = cm
            CONFIG.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding='utf8')
            self.note('已交换主副摄：%s —— 正重新打开画面' % cm)
            try:
                self.stop()
            except Exception:            # noqa: BLE001
                pass
            self.start()
        except Exception as exc:         # noqa: BLE001
            self.note('交换主副摄失败：%s' % exc)

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
                bpath = Path(bgp)
                if not bpath.is_absolute():
                    bpath = ROOT / bpath
                bgi = None
                try:
                    if bpath.is_file():
                        bgi = cv2.imread(str(bpath), cv2.IMREAD_GRAYSCALE)
                    else:
                        self.note(f'空托盘背景不存在（{bpath.name}），本次跳过灰度差分')
                except Exception as exc:              # 背景图问题不该让整次识别失败
                    self.note(f'空托盘背景读不到（{exc}），本次跳过灰度差分')
                if bgi is not None:
                    bg = cv2.warpPerspective(bgi, H, (s, s))
        masks = self.segmenter.segment(warp, bg, self.cfg)
        t2 = time.perf_counter()
        out = []
        # 目标过滤：托盘外的杯子/夹爪常被 SAM 当成“物块”。
        # 物块在同尺寸托盘里最大也就 ~4.8 万像素（40 mm 级长方体），杯子/夹爪更大或贴边。
        tf = self.cfg.get('target_filter') or {}
        a_min = int(tf.get('area_min', 2000))
        a_max = int(tf.get('area_max', 48000))
        edge = int(tf.get('edge_px', 3))
        dropped = 0
        for m in masks:
            x, y, w, h = m['bbox']
            area = int(m.get('area_px') or 0)
            if area < a_min or area > a_max:
                dropped += 1
                continue
            if x <= edge or y <= edge or x + w >= s - edge or y + h >= s - edge:
                # 贴边目标：物块不会正好贴着托盘边框被切掉，多半是托盘外的物体
                if tf.get('drop_edge', True):
                    dropped += 1
                    continue
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
        if dropped:
            self.note('过滤掉 %d 个疑似托盘外目标（过大或贴边）' % dropped)
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
        try:
            self._rec_q.put(('stage', '顶视：校正 + SAM 分割…'))
        except Exception:                      # noqa: BLE001
            pass
        top, tw = self.detect_targets(tf, 'top', st) if tf is not None else ([], None)
        try:
            if tw is not None:                      # 存一份干净校正帧，便于离线对比/调试
                _rd = Path(__file__).parent / 'runtime'
                _rd.mkdir(exist_ok=True)
                _ok, _buf = cv2.imencode('.jpg', tw, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                if _ok:
                    (_rd / 'last_top_warp.jpg').write_bytes(_buf.tobytes())
        except Exception:                           # noqa: BLE001
            pass
        side, sw, reused = [], None, False
        if sf is not None:
            self._side_tick += 1
            if side_every > 1 and self.side_cache is not None \
                    and (self._side_tick % int(side_every)) != 0:
                side, sw = self.side_cache
                reused = True
            else:
                try:
                    self._rec_q.put(('stage', '副摄：校正 + SAM 分割…'))
                except Exception:              # noqa: BLE001
                    pass
                side, sw = self.detect_targets(sf, 'side', st)
                try:
                    if sw is not None:
                        _rd = Path(__file__).parent / 'runtime'
                        _rd.mkdir(exist_ok=True)
                        _ok, _buf = cv2.imencode('.jpg', sw, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
                        if _ok:
                            (_rd / 'last_side_warp.jpg').write_bytes(_buf.tobytes())
                except Exception:                   # noqa: BLE001
                    pass
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
        """分类：DeepSeek 看整幅校正图（裸图）返回物体，再与分割目标按中心最近配对。"""
        objs = []
        try:
            cc = (self.cfg or {}).get('cloud_vlm') or {}
            tops = [f.get('top') for f in fused if f.get('top') is not None]
            if (cc.get('enabled') and cc.get('mode') == 'primary'
                    and tw is not None and tops
                    and getattr(classify, '_CVLM', None) is not None):
                try:
                    self._rec_q.put(('stage', 'DeepSeek 识别中…'))
                except Exception:              # noqa: BLE001
                    pass
                _views = str(cc.get('views') or 'two')
                _call = (classify._CVLM.ask_objects2_cached if (_views != 'top' and sw is not None)
                         else classify._CVLM.ask_objects_cached)
                if _call is classify._CVLM.ask_objects2_cached:
                    _tmo = min(float(cc.get('timeout_s') or 10.0), 8.0)
                    r = _call(tw, sw, candidates=cc.get('candidates'),
                              timeout=_tmo, model=cc.get('model'))
                else:
                    _tmo = min(float(cc.get('timeout_s') or 10.0), 8.0)
                    r = _call(tw, candidates=cc.get('candidates'),
                              timeout=_tmo, model=cc.get('model'))
                objs = [o for o in (r or {}).get('objects', []) if o.get('shape')]
                self._vlm_ms = (r or {}).get('ms')
        except Exception:                                  # noqa: BLE001
            objs = []
        # DeepSeek 主分类模式：SAM 负责分割/定位/面积，DeepSeek 负责类别与颜色
        _cc0 = (self.cfg.get('cloud_vlm') or {})
        if _cc0.get('enabled') and str(_cc0.get('mode')) == 'primary' and not objs:
            self.note('DeepSeek 未返回目标（断网/无 Key/超时），已回退几何判据')
        if objs and str(_cc0.get('mode')) == 'primary':
            try:
                labeled = classify.attach_vlm_to_fused(fused, objs, tw,
                                                color_from_mask=bool(_cc0.get('color_from_mask', False)))
                if not labeled and _call is classify._CVLM.ask_objects2_cached and tw is not None:
                    # 两图结果与分割目标对不上（常见于模型把图2坐标混进来）→ 用单图重试一次
                    r2 = classify._CVLM.ask_objects_cached(
                        tw, candidates=cc.get('candidates'),
                        timeout=float(cc.get('timeout_s') or 10.0), model=cc.get('model'))
                    objs2 = [o for o in (r2 or {}).get('objects', []) if o.get('shape')]
                    if objs2:
                        objs = objs2
                        self._vlm_ms = (r2 or {}).get('ms')
                        self.note('双图结果未对齐，已用单图重试（%d 个目标）' % len(objs2))
                        labeled = classify.attach_vlm_to_fused(
                            fused, objs, tw,
                            color_from_mask=bool(_cc0.get('color_from_mask', False)))
                if labeled:
                    fused[:] = labeled
                    self.note('DeepSeek 分类 + SAM 定位：%d 个目标' % len(labeled))
                    return
                try:
                    _dbg = {'ms': (self._vlm_ms or 0), 'views': (objs[0].get('views') if objs else None),
                            'objs': objs[:8],
                            'items': [{'uv': (f.get('top') or {}).get('center_tray'),
                                       'shape': (f.get('top') or {}).get('shape')} for f in fused[:8]]}
                    (Path(__file__).parent / 'runtime').mkdir(exist_ok=True)
                    (Path(__file__).parent / 'runtime' / 'vlm_debug.json').write_text(
                        json.dumps(_dbg, ensure_ascii=False, indent=2), encoding='utf-8')
                except Exception:
                    pass
                self.note('DeepSeek 分类未与任何分割目标匹配，回退几何判据（详见 runtime/vlm_debug.json）')
            except Exception as exc:                       # noqa: BLE001
                self.note('DeepSeek 贴标签失败：%s' % exc)
        # 目标中心（归一化）
        tops = [f.get('top') for f in fused if f.get('top') is not None]
        used = set()
        for i, f in enumerate(fused, 1):
            t, s_ = f.get('top'), f.get('side')
            if t is not None and tw is not None:
                obj = None
                if objs:
                    cu, cv = t.get('center_tray') or [0.5, 0.5]
                    best, bd = None, 1e9
                    for k, o in enumerate(objs):
                        if k in used:
                            continue
                        oc = o.get('center') or [0.5, 0.5]
                        d = ((float(oc[0]) - float(cu)) ** 2 +
                             (float(oc[1]) - float(cv)) ** 2) ** .5
                        if d < bd:
                            best, bd = k, d
                    if best is not None and bd <= .22:
                        used.add(best)
                        obj = objs[best]
                classify.classify_item(tw, t, self.cfg, self.samples,
                                       side_warp=sw, side_item=s_, vlm_obj=obj)
            if s_ is not None and sw is not None and t is None:
                classify.classify_item(sw, s_, self.cfg, self.samples, side_only=True)

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
        self.draw_marks()

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

    # ------------------------------------------------- 运行台用的合成画面
    def render_overview(self, size: int = 700):
        """给「运行台」用：顶视校正图 + 四点框 + 标注点 + 目标框（BGR，失败返回 None）。

        文字用 PIL 画（cv2.putText 不支持中文），所以画面里能直接看到「#1 正方体/绿」。
        """
        try:
            if self.a is None:
                return None
            fr = self.a.get()
            if fr is None:
                return None
            W = 700
            if len(self.points) == 4:
                dst = np.float32([[0, 0], [W, 0], [W, W], [0, W]])
                H = cv2.getPerspectiveTransform(np.float32(self.points), dst)
                img = cv2.warpPerspective(fr, H, (W, W))
            else:
                img = cv2.resize(fr, (W, W))

            labels = []                      # (x, y, 文本, 颜色)
            # 四点框 = 校正图的四条边
            cv2.polylines(img, [np.int32([[1, 1], [W - 2, 1], [W - 2, W - 2], [1, W - 2]])],
                          True, (120, 255, 120), 2)
            for idx, (x, y) in enumerate([(14, 22), (W - 30, 22),
                                          (W - 30, W - 20), (14, W - 20)], 1):
                cv2.circle(img, (x, y), 6, (0, 200, 0), -1)
                labels.append((x + 9, y - 9, str(idx), (0, 160, 0)))

            # 标注点（青色十字 + P 编号）
            for idx, m in enumerate(getattr(self, 'marks', []) or [], 1):
                try:
                    mx, my = int(float(m['x'])), int(float(m['y']))
                except (KeyError, TypeError, ValueError):
                    continue
                cv2.drawMarker(img, (mx, my), (255, 229, 0),
                               markerType=cv2.MARKER_CROSS, markerSize=20, thickness=2)
                labels.append((mx + 10, my - 22, 'P%d' % idx, (200, 170, 0)))

            # 目标框 + 编号/类别
            for idx, f in enumerate(getattr(self, 'fused', []) or [], 1):
                tp = f.get('top')
                if not tp:
                    continue
                base = (0, 255, 120) if f.get('views') == 'top+side' else (0, 170, 255)
                cls = tp.get('class_name') or tp.get('shape') or '待登记'
                color_txt = tp.get('color') or ''
                text = '#%d %s%s' % (idx, str(cls)[:14],
                                     ('/' + color_txt) if color_txt else '')
                b = tp.get('bbox')
                if b:
                    try:
                        x, y, w, h = [int(v) for v in b]
                    except (TypeError, ValueError):
                        x = y = w = h = 0
                    if w > 0 and h > 0:
                        cv2.rectangle(img, (x, y), (x + w, y + h), base, 2)
                        labels.append((x, max(16, y - 24), text, base))
                        continue
                labels.append((6, 24 * idx, text, base))

            # 中文标签：PIL 画
            if labels:
                from PIL import Image as _Img, ImageDraw as _Draw, ImageFont as _Font
                pil = _Img.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                drw = _Draw.Draw(pil)
                font = None
                for cand in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf'):
                    try:
                        font = _Font.truetype(cand, 18)
                        break
                    except Exception:            # noqa: BLE001
                        continue
                for (x, y, text, bgr) in labels:
                    rgb = (int(bgr[2]), int(bgr[1]), int(bgr[0]))
                    drw.text((max(0, x + 1), max(0, y + 1)), text, font=font, fill=(0, 0, 0))
                    drw.text((max(0, x), max(0, y)), text, font=font, fill=rgb)
                img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)

            if size and int(size) != W:
                img = cv2.resize(img, (int(size), int(size)))
            return img
        except Exception:                       # noqa: BLE001
            return None

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
        if self.mark_click(e, which):
            return True
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

    # ---------------------------------------------------------- 标定标注点
    def load_marks(self):
        """读 new_eye/runtime/calib_marks.json（上位机“标注点标定”也用这份文件）。"""
        try:
            data = json.loads(MARKS.read_text(encoding='utf8'))
            if isinstance(data, dict):
                data = data.get('points') or []
            return [m for m in data if isinstance(m, dict) and 'u' in m and 'v' in m]
        except Exception:
            return []

    def save_marks(self):
        try:
            MARKS.parent.mkdir(parents=True, exist_ok=True)
            tmp = MARKS.with_name(MARKS.stem + '.%d.tmp' % threading.get_ident())
            payload = {'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                       'points': self.marks}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding='utf8')
            tmp.replace(MARKS)
            return True
        except Exception as exc:
            self.note('标注点保存失败：%s' % exc)
            return False

    def toggle_marks(self):
        if self.mark_mode:
            self.mark_mode = False
            self.mark_btn.config(text='标注点')
            self.note('已结束标注：共 %d 个点，已存 new_eye/runtime/calib_marks.json'
                      % len(self.marks))
            return
        if len(self.points) != 4:
            self.note('先完成顶视四点定位，再来标注点')
            return
        self.register_mode = False
        self.mark_mode = True
        self.mark_btn.config(text='结束标注')
        self.note('标注点：在顶视画面里点物块/标记的位置（右键撤销上一个，Esc 退出）；'
                  '记完到上位机“标定向导→标注点法”里把爪子移到同一位置记录脉冲')

    def frame_to_uv(self, fx, fy):
        """画面像素 → 四点框选区域内的归一化 u,v（与识别结果同一坐标系）。"""
        if len(self.points) != 4:
            return None
        s = 700
        p = np.array(self.points, dtype=np.float32)
        dst = np.array([[0, 0], [s, 0], [s, s], [0, s]], dtype=np.float32)
        H = cv2.getPerspectiveTransform(p, dst)
        q = cv2.perspectiveTransform(np.float32([[[fx, fy]]]), H)[0][0]
        return float(q[0] / s), float(q[1] / s)

    def mark_click(self, e, which):
        if not self.mark_mode:
            return False
        if which != 'top' or self.tf is None:
            return True
        fx, fy = self.va.canvas_to_frame(e.x, e.y, self.tf.shape)
        got = self.frame_to_uv(fx, fy)
        if got is None:
            self.note('顶视四点还没完成：先点「顶视四点框选」')
            return True
        u, v = got
        if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            self.note('点在四点多边形外面了（u=%.3f v=%.3f）：请点框内的标记' % (u, v))
            return True
        self.marks.append({'u': round(u, 4), 'v': round(v, 4),
                           'x': round(float(fx), 1), 'y': round(float(fy), 1)})
        self.save_marks()
        self.note('标注点 P%d：u=%.4f v=%.4f（共 %d 个）'
                  % (len(self.marks), u, v, len(self.marks)))

    def undo_mark(self):
        if self.marks:
            m = self.marks.pop()
            self.save_marks()
            self.note('已撤销标注点 P%d（剩 %d 个）' % (len(self.marks) + 1, len(self.marks)))

    def clear_marks(self):
        if not self.marks:
            self.note('还没有标注点')
            return
        if not messagebox.askyesno('清空标注', '清空全部 %d 个标注点？' % len(self.marks),
                                   parent=self.r):
            return
        self.marks = []
        self.save_marks()
        self.note('已清空标注点')

    def draw_marks(self):
        """顶视画面上画出标注点（青色十字 + P 编号）。"""
        if self.tf is None or not self.marks:
            return
        for i, m in enumerate(self.marks, 1):
            try:
                c = self.va.frame_to_canvas(float(m['x']), float(m['y']), self.tf.shape)
            except Exception:
                continue
            x, y = c
            self.ca.create_line(x - 9, y, x + 9, y, fill='#00e5ff', width=2)
            self.ca.create_line(x, y - 9, x, y + 9, fill='#00e5ff', width=2)
            self.ca.create_oval(x - 11, y - 11, x + 11, y + 11, outline='#00e5ff', width=1)
            self.ca.create_text(x + 13, y - 12, text='P%d' % i, fill='#00e5ff',
                                anchor='nw', font=('Arial', 11, 'bold'))

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
                    shape, _, color = str(val).partition('|')
                    aligned = classify._lookup(val, shape or None, color or None,
                                               self.cfg, self.samples,
                                               it.get('feature'))
                    it.update(aligned)
                    it['class_votes'] = f'{cnt}/{len(arr)}'
                else:
                    it[k] = val
        return base

    # ---------------- 单次识别（非阻塞 + 进度条） ----------------
    def recognize_safe(self):
        """「单次识别」按钮入口：起工作线程，主线程只负责进度条与提示。"""
        return self.recognize()

    def _rec_set(self, pct, text, busy=None):
        """主线程：更新进度条/标签（只在主线程调用）。"""
        try:
            if pct is not None:
                self.rec_pb.configure(value=max(0.0, min(100.0, pct)))
            if text is not None:
                self.rec_pb_lbl.configure(text=text)
            if busy is not None:
                self.rec_btn.configure(state=('disabled' if busy else 'normal'))
                if busy:
                    self.rec_pb.configure(mode='determinate')
        except Exception:                      # noqa: BLE001 —— 界面刷新失败不影响识别
            pass

    @staticmethod
    def _call_cb(cb, ok, info):
        """回调分发：支持函数，也支持 {'fn':..., 'quiet':bool} 字典。"""
        if cb is None:
            return
        fn = cb.get('fn') if isinstance(cb, dict) else cb
        if not callable(fn):
            return
        try:
            fn(ok, info)
        except Exception:                      # noqa: BLE001
            pass

    def _rec_pump(self):
        """主线程轮询工作线程的进度/结果（100 ms）。"""
        # 看门狗：识别超过 90 s 仍未结束 → 解除界面锁定并允许重试（线程可能仍卡在 SAM/网络里）
        try:
            if getattr(self, '_rec_busy', False):
                t0 = getattr(self, '_rec_t0', None)
                if t0 and (time.time() - t0) > 90.0:
                    self._rec_abort = True
                    self._rec_busy = False
                    self._rec_set(0, '识别超时（>90 s）', busy=False)
                    self.note('识别超时（>90 s）：已解除锁定，可点「单次识别」重试；'
                              '若反复超时请把「原始日志」这段发我')
        except Exception:                      # noqa: BLE001
            pass
        try:
            while True:
                kind, *rest = self._rec_q.get_nowait()
                if kind == 'progress':
                    i, n = rest
                    self._rec_set(100.0 * i / max(1, n), '识别中 %d/%d 帧' % (i, n))
                elif kind == 'stage':
                    self._rec_set(None, str(rest[0]))
                elif kind == 'note':
                    self.note(str(rest[0]))
                elif kind == 'done':
                    fused, cb, info = rest
                    self._rec_busy = False
                    self._rec_set(100, '完成：%d 个目标' % len(fused), busy=False)
                    self._call_cb(cb, True, info)
                elif kind == 'error':
                    cb, msg = rest
                    self._rec_busy = False
                    self._rec_set(0, '识别失败', busy=False)
                    self.note('识别失败：' + msg)
                    self._call_cb(cb, False, msg)
        except queue.Empty:
            pass
        try:
            self.r.after(100, self._rec_pump)
        except Exception:                      # noqa: BLE001
            pass

    def _recognize_precheck(self):
        """主线程前置检查：返回 (n_frames, error_text)。"""
        if self.tf is None:
            return 0, '还没有画面：先点「打开双画面」'
        if len(self.points) != 4:
            return 0, '请先完成顶视四点定位，再识别'
        if not self.ensure_segmenter():
            return 0, 'SAM 模型没加载成功'
        n = int(self.cfg.get('frames_for_vote', 3) or 3)
        return max(1, min(5, n)), ''

    def recognize(self, callback=None):
        """起线程做识别；callback(ok, info) 在识别结束后于主线程被调用。"""
        if getattr(self, '_rec_busy', False):
            self.note('正在识别中，请稍候…')
            return False
        n, err = self._recognize_precheck()
        if err:
            self.note(err)
            if callback:
                callback(False, err)
            return False
        self._rec_busy = True
        self._rec_abort = False
        self._rec_t0 = time.time()
        self._rec_cb = callback
        self._rec_set(0, '识别中 0/%d 帧' % n, busy=True)

        def work():
            try:
                fused, info = self._recognize_core(n)
            except Exception:                  # noqa: BLE001
                tb = traceback.format_exc()
                try:
                    out = ROOT / 'runtime'
                    out.mkdir(exist_ok=True)
                    (out / 'vision_error.log').write_text(tb, encoding='utf8')
                except Exception:              # noqa: BLE001
                    pass
                last = [ln for ln in tb.strip().splitlines() if ln.strip()][-1]
                self._rec_q.put(('error', callback, last))
                return
            self._rec_q.put(('done', fused, callback, info))

        threading.Thread(target=work, daemon=True, name='recognize').start()
        return True

    def recognize_blocking(self):
        """工作线程内使用：同步跑完识别（不碰 Tk，只往队列里塞进度）。"""
        n, err = 0, ''
        # 前置状态由调用方（主线程）已检查；这里只做最保守的兜底
        if len(self.points) != 4:
            raise RuntimeError('请先完成顶视四点定位')
        n = int(self.cfg.get('frames_for_vote', 3) or 3)
        n = max(2, min(5, n))
        return self._recognize_core(n)

    def _recognize_core(self, n):
        """真正的识别计算：不碰任何 Tk 控件，可在任意线程运行。"""
        runs = []
        stats_all = []
        t_start = time.perf_counter()
        for i in range(n):
            if getattr(self, '_rec_abort', False):
                raise RuntimeError('识别已被取消（超时）')
            tf = self.a.get() if self.a else self.tf
            sf = (self.bstream.get() if (self.bstream is not None
                                         and len(self.side_points) == 4) else None)
            if tf is None:
                break
            self._rec_q.put(('progress', i + 1, n))
            r0 = time.perf_counter()
            runs.append(self.detect_and_fuse(tf, sf, stats_out=stats_all))
            self._merge_times({'frame': time.perf_counter() - r0})
            time.sleep(.08)
        if not runs:
            raise RuntimeError('没有可用画面')
        self._rec_q.put(('stage', '投票…'))
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
        msg = (f'单次识别完成：{len(fused)} 个目标（顶视{ntop}/副摄{nside}，{n} 帧投票），'
               f'用时 {total:.2f}s（顶视 {tw_:.2f}s / 副摄 {sw_:.2f}s / 融合 '
               f'{acc.get("fuse", 0.0):.2f}s / 分类 {acc.get("classify", 0.0):.2f}s / '
               f'写JSON {wr:.2f}s）| {ctxt}')
        self._rec_q.put(('note', msg))
        return fused, msg


if __name__ == '__main__':
    r = tk.Tk()
    a = App(r)
    r.protocol('WM_DELETE_WINDOW', lambda: (a.stop(), r.destroy()))
    r.mainloop()

