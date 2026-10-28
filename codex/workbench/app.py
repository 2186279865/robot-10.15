from __future__ import annotations

import copy
import json
import math
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageGrab, ImageTk

from .automation import AutoSorter
from .cameras import demo_frame, make_stream, read_image, save_image
from .dataset import benchmark, capture_sample, write_report
from .robot import RobotClient, RobotError, SimRobot
from .settings import CONFIG, ROOT, RUNTIME, demo_robot_config, save
from .vision import VisionPipeline

BACKENDS = {'几何分析': 'geometry', '本地视觉模型': 'local', '云端视觉模型': 'cloud'}
AXES = {1: 'Z 升降', 3: '转盘', 4: 'X 横向', 5: 'Y 纵向'}


class App(tk.Tk):
    def __init__(self, cfg, args):
        super().__init__()
        self.title('机器人视觉与控制工作台 · CODEX')
        self.geometry(f'{min(1480, self.winfo_screenwidth() - 80)}x{min(920, self.winfo_screenheight() - 100)}+30+30')
        self.minsize(1080, 680)
        self.cfg, self.args = cfg, args
        self.demo = args.demo or args.smoke
        if self.demo:
            self.cfg['robot'] = demo_robot_config()
        self.events = queue.Queue()
        self.streams = {}
        self.static = {'top': None, 'side': None}
        self.items = []
        self.pipeline = VisionPipeline(self.cfg)
        self.robot = None
        self.sorter = None
        self.busy = False
        self.rec_busy = False
        self.live = False
        self.closing = False
        self.last_live = 0
        self.last_top = None
        self.last_side = None
        self.last_result_monotonic = 0
        self.roi_view = None
        self.roi_clicks = []
        self.canvas_maps = {}
        self.teach_points = []
        self.photos = {}
        self._build()
        if self.demo:
            self.static['top'] = demo_frame()
            self.static['side'] = demo_frame()
            self.cfg['camera']['top_points'] = [[0, 0], [1, 0], [1, 1], [0, 1]]
            self.pipeline.cfg['camera'] = copy.deepcopy(self.cfg['camera'])
            self.note('演示模式：合成物块、模拟电机，不连接硬件。几何模式只显示有依据的二维截面。')
        elif args.image:
            self.static['top'] = read_image(args.image)
            if args.side:
                self.static['side'] = read_image(args.side)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(60, self.pump)
        self.after(500, self.recognize if self.static['top'] is not None else lambda: None)
        if args.smoke:
            self.after(1800, self.recognize)
            self.after(3200, self.smoke)

    def note(self, text):
        self.events.put(('log', str(text)))

    def _build(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TButton', padding=(8, 6), font=('Microsoft YaHei UI', 10))
        style.configure('TLabel', font=('Microsoft YaHei UI', 10))
        style.configure('Treeview', rowheight=30, font=('Microsoft YaHei UI', 10))
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'))
        style.configure('Stop.TButton', foreground='white', background='#b42332', font=('Microsoft YaHei UI', 12, 'bold'))
        header = ttk.Frame(self, padding=(14, 10))
        header.pack(fill='x')
        ttk.Label(header, text='机器人视觉与控制', font=('Microsoft YaHei UI', 20, 'bold')).pack(side='left')
        self.status = tk.StringVar(value='模拟模式' if self.demo else '控制未连接')
        ttk.Label(header, textvariable=self.status).pack(side='left', padx=20)
        ttk.Button(header, text='停止全部', style='Stop.TButton', command=self.stop_all).pack(side='right')
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True, padx=12)
        self.vision_tab = ttk.Frame(self.notebook, padding=10)
        self.control_tab = ttk.Frame(self.notebook, padding=12)
        self.auto_tab = ttk.Frame(self.notebook, padding=12)
        self.config_tab = ttk.Frame(self.notebook, padding=10)
        self.eval_tab = ttk.Frame(self.notebook, padding=12)
        self.log_tab = ttk.Frame(self.notebook, padding=8)
        for frame, title in [(self.vision_tab, '视觉识别'), (self.control_tab, '手动控制'),
                             (self.auto_tab, '自动抓放'), (self.config_tab, '标定与配置'),
                             (self.eval_tab, '本地 / 云端评测'), (self.log_tab, '运行日志')]:
            self.notebook.add(frame, text='  ' + title + '  ')
        self.build_vision()
        self.build_control()
        self.build_auto()
        self.build_config()
        self.build_eval()
        self.logs = tk.Text(self.log_tab, wrap='word', font=('Consolas', 10), state='disabled')
        self.logs.pack(fill='both', expand=True)
        self.footer = tk.StringVar(value='准备就绪 · 不需要登记物块 · 接口未配置时可以离线图片和模拟运行')
        ttk.Label(self, textvariable=self.footer, padding=(14, 7)).pack(fill='x')

    def build_vision(self):
        toolbar = ttk.Frame(self.vision_tab)
        toolbar.pack(fill='x')
        self.backend = tk.StringVar(value=next(k for k, v in BACKENDS.items() if v == self.cfg['vision']['backend']))
        combo = ttk.Combobox(toolbar, textvariable=self.backend, values=list(BACKENDS), state='readonly', width=15)
        combo.pack(side='left')
        combo.bind('<<ComboboxSelected>>', lambda event: self.change_backend())
        for text, command in [('打开双摄', self.open_cameras), ('关闭双摄', self.close_cameras),
                              ('导入顶视', lambda: self.import_image('top')),
                              ('导入副视', lambda: self.import_image('side')),
                              ('识别一次', self.recognize), ('连续识别', self.toggle_live),
                              ('保存样本', self.save_sample)]:
            ttk.Button(toolbar, text=text, command=command).pack(side='left', padx=3)
        ttk.Label(self.vision_tab, text='首次识别不需要登记。几何模式保留截面名称；本地/云端模型补充立体形状。',
                  foreground='#506078').pack(anchor='w', pady=(8, 4))
        views = ttk.Frame(self.vision_tab)
        views.pack(fill='both', expand=True)
        self.canvases = {}
        for index, (view, name) in enumerate([('top', '主视相机'), ('side', '副视相机')]):
            views.columnconfigure(index, weight=1, uniform='camera')
            views.rowconfigure(0, weight=1)
            frame = ttk.LabelFrame(views, text=name, padding=5)
            frame.grid(row=0, column=index, sticky='nsew', padx=(0, 5))
            canvas = tk.Canvas(frame, bg='#111c2d', height=350, highlightthickness=0)
            canvas.pack(fill='both', expand=True)
            canvas.bind('<Button-1>', lambda e, v=view: self.canvas_click(e, v))
            self.canvases[view] = canvas
            tools = ttk.Frame(frame)
            tools.pack(fill='x', pady=3)
            ttk.Button(tools, text='框选托盘四点', command=lambda v=view: self.select_roi(v)).pack(side='left')
            ttk.Button(tools, text='采集空背景', command=lambda v=view: self.capture_background(v)).pack(side='left', padx=4)
            ttk.Button(tools, text='清除四点', command=lambda v=view: self.clear_roi(v)).pack(side='left')
        columns = ('id', 'shape', 'color', 'hollow', 'state', 'source', 'time', 'uv')
        self.table = ttk.Treeview(self.vision_tab, columns=columns, show='headings', height=5, selectmode='browse')
        for key, label, width in [('id', '编号', 55), ('shape', '形状', 160), ('color', '颜色', 80),
                                  ('hollow', '孔洞证据', 85), ('state', '稳定状态', 100),
                                  ('source', '识别方式', 100), ('time', '模型耗时', 100), ('uv', '托盘 u / v', 180)]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width)
        self.table.pack(fill='x', pady=(8, 0))

    def build_control(self):
        row = ttk.Frame(self.control_tab)
        row.pack(fill='x')
        self.endpoint = tk.StringVar(value=self.cfg['robot']['endpoint'])
        self.simulated = tk.BooleanVar(value=self.demo or self.cfg['robot']['simulated'])
        ttk.Label(row, text='控制端点').pack(side='left')
        ttk.Entry(row, textvariable=self.endpoint, width=38).pack(side='left', padx=8)
        ttk.Checkbutton(row, text='模拟机器人', variable=self.simulated).pack(side='left')
        ttk.Button(row, text='连接', command=self.begin_connect).pack(side='left', padx=4)
        ttk.Button(row, text='断开', command=self.disconnect_robot).pack(side='left')
        ttk.Button(row, text='显式解除停止', command=lambda: self.task(lambda: self.require_robot().unlock())).pack(side='left', padx=4)
        ttk.Label(self.control_tab, text='对外运动单位：脉冲。连接不会自动清除停止锁存；运动完成必须收到回包和新鲜位置。',
                  foreground='#506078').pack(anchor='w', pady=12)
        self.axis_positions, self.jog_values = {}, {}
        for axis, label in AXES.items():
            frame = ttk.LabelFrame(self.control_tab, text=f'{label}　地址 {axis}', padding=12)
            frame.pack(fill='x', pady=5)
            value = tk.StringVar(value='回读 —')
            self.axis_positions[axis] = value
            ttk.Label(frame, textvariable=value, width=31).pack(side='left')
            jog = tk.StringVar(value='100')
            self.jog_values[axis] = jog
            ttk.Entry(frame, textvariable=jog, width=10).pack(side='left')
            ttk.Button(frame, text='负向', command=lambda a=axis: self.manual_move(a, -1)).pack(side='left', padx=5)
            ttk.Button(frame, text='正向', command=lambda a=axis: self.manual_move(a, 1)).pack(side='left')
            ttk.Button(frame, text='使能', command=lambda a=axis: self.task(lambda: self.require_robot().request('motor_enable', a, 1))).pack(side='left', padx=5)
            if axis == 3:
                ttk.Button(frame, text='当前设为 1 号盒零点', command=lambda: self.task(lambda: self.require_robot().angle_zero())).pack(side='left')
        grip = ttk.LabelFrame(self.control_tab, text='夹爪', padding=12)
        grip.pack(fill='x', pady=12)
        for label, action in [('张开', 'open'), ('预备', 'ready'), ('夹紧', 'grab')]:
            ttk.Button(grip, text=label, command=lambda a=action: self.task(lambda: self.require_robot().grip(a))).pack(side='left', padx=5)
        ttk.Button(grip, text='确认本次上电基准为 P0', command=self.confirm_reference).pack(side='right')

    def build_auto(self):
        self.auto_state = tk.StringVar(value='IDLE')
        ttk.Label(self.auto_tab, textvariable=self.auto_state, font=('Consolas', 26, 'bold')).pack(anchor='w', pady=12)
        ttk.Label(self.auto_tab, text='固定通道：P0 → Y 到通道末端 → Z 抬高 → X/Y 对准 → 抓取 → 高位返回 → Z 降低 → Y 回 P0。',
                  wraplength=1000).pack(anchor='w', pady=8)
        ttk.Label(self.auto_tab, text='每段运动校验软限位和到位回包。停止后整次流程终止，必须显式解除停止并重新启动。',
                  wraplength=1000).pack(anchor='w', pady=8)
        row = ttk.Frame(self.auto_tab)
        row.pack(fill='x', pady=20)
        ttk.Label(row, text='件数').pack(side='left')
        self.item_count = tk.StringVar(value='6')
        ttk.Spinbox(row, from_=1, to=100, textvariable=self.item_count, width=7).pack(side='left', padx=8)
        ttk.Button(row, text='启动自动抓放', command=self.start_auto).pack(side='left')
        ttk.Button(row, text='停止并锁存', command=self.stop_all).pack(side='left', padx=8)
        ttk.Button(row, text='新批次 / 清空盒映射', command=self.reset_batch).pack(side='left')
        self.auto_detail = tk.StringVar(value='先连接控制器、解除停止、确认 P0 和标定，再设置转盘零点。')
        ttk.Label(self.auto_tab, textvariable=self.auto_detail, wraplength=1100).pack(anchor='w', pady=14)
        ttk.Label(self.auto_tab, text='比赛屏必须使用 codex 显示端才能确认“文字与图片已刷新”。松爪成功仅代表执行完成，未安装落盒传感器时无法独立证明货物已落盒。',
                  foreground='#58677e', wraplength=1100).pack(anchor='w', pady=8)

    def build_config(self):
        buttons = ttk.Frame(self.config_tab)
        buttons.pack(fill='x')
        for name, fn in [('读取当前配置', self.refresh_config), ('应用并保存', self.apply_config),
                         ('导入旧标定与限位', self.import_calibration), ('记录标定点', self.teach_point),
                         ('求解 X/Y 标定', self.solve_calibration)]:
            ttk.Button(buttons, text=name, command=fn).pack(side='left', padx=3)
        ttk.Label(self.config_tab, text='四点归一化为 0~1；limits 为绝对脉冲范围；API Key 只放环境变量。修改配置需先停止自动流程并断开控制。',
                  foreground='#506078').pack(anchor='w', pady=7)
        self.config_text = tk.Text(self.config_tab, wrap='none', font=('Consolas', 10), undo=True)
        self.config_text.pack(fill='both', expand=True)
        self.refresh_config()

    def build_eval(self):
        ttk.Label(self.eval_tab, text='同一批物块，对比本地和云端', font=('Microsoft YaHei UI', 19, 'bold')).pack(anchor='w', pady=12)
        ttk.Label(self.eval_tab, text='1. 在视觉页保存样本。\n2. 在样本 labels.json 填写实际形状、颜色、镂空状态。\n'
                  '3. 选择标签文件，分别测量成功率和耗时。失败请求计入错误；没有人工真值时不计算准确率。\n'
                  '云端模式会发送物块裁剪图到配置的服务；API 调用可能计费。', justify='left').pack(anchor='w', pady=8)
        row = ttk.Frame(self.eval_tab)
        row.pack(fill='x', pady=16)
        self.eval_backend = tk.StringVar(value='geometry,local,cloud')
        ttk.Entry(row, textvariable=self.eval_backend, width=32).pack(side='left')
        ttk.Label(row, text='后端列表，逗号分隔').pack(side='left', padx=8)
        ttk.Button(row, text='选择标签并开始评测', command=self.run_benchmark).pack(side='left')
        self.eval_output = tk.Text(self.eval_tab, font=('Consolas', 11), wrap='word')
        self.eval_output.pack(fill='both', expand=True)

    def task(self, fn):
        if self.busy:
            self.note('另一个操作正在运行')
            return
        self.busy = True
        def run():
            try:
                fn()
            except Exception as exc:
                self.events.put(('error', str(exc)))
            finally:
                self.events.put(('task_done', None))
        threading.Thread(target=run, daemon=True).start()

    def require_robot(self):
        if self.robot is None or not self.robot.online:
            raise RobotError('请先连接机器人')
        return self.robot

    def begin_connect(self):
        endpoint, simulated = self.endpoint.get(), self.simulated.get()
        self.task(lambda: self.connect_robot(endpoint, simulated))

    def connect_robot(self, endpoint, simulated):
        self.sorter = None
        if self.robot:
            self.robot.close()
        cfg = self.cfg['robot']
        cfg['endpoint'] = endpoint
        cfg['simulated'] = simulated
        if cfg['simulated'] and not cfg['limits']:
            cfg = demo_robot_config()
            self.cfg['robot'] = cfg
        if not cfg['simulated']:
            cfg['reference_confirmed'] = False
        cls = SimRobot if cfg['simulated'] else RobotClient
        self.robot = cls(cfg, log=self.note)
        self.robot.connect()
        self.note('控制连接完成；点“显式解除停止”后才能操作')

    def disconnect_robot(self):
        if self.busy:
            self.stop_all()
            self.note('等待当前任务退出后再断开连接')
            return
        if self.robot:
            self.robot.close()
            self.robot = None
        self.cfg['robot']['reference_confirmed'] = False
        self.sorter = None
        self.note('控制已断开')

    def stop_all(self):
        self.live = False
        if self.robot:
            self.robot.stop()
        self.note('已停止并锁存；正在执行的自动流程会退出')

    def manual_move(self, axis, sign):
        try:
            amount = float(self.jog_values[axis].get())
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError('点动量必须为正数')
        except ValueError as exc:
            self.note(exc)
            return
        self.task(lambda: self.require_robot().move_abs(axis, self.require_robot().position(axis) + sign * amount))

    def confirm_reference(self):
        if self.busy:
            self.note('运动期间不能修改基准')
            return
        try:
            robot = self.require_robot()
            r = self.cfg['robot']['route']
            for axis, expected in ((1, r['z_low']), (4, r['p0_x']), (5, r['p0_y'])):
                if abs(robot.position(axis) - expected) > 25:
                    raise RobotError(f'轴 {axis} 回读不在配置 P0；请先人工对齐机械与编码器基准')
            if messagebox.askyesno('确认机械基准', '请确认机构实际位于 P0、编码器零点与配置一致。此按钮不会移动或清零电机。'):
                self.cfg['robot']['reference_confirmed'] = True
                self.note('本次上电位置基准已确认')
        except Exception as exc:
            self.note(exc)

    def get_frames(self, automatic=False):
        top = self.streams['top'].get() if 'top' in self.streams else self.static['top']
        side = self.streams['side'].get() if 'side' in self.streams else self.static['side']
        if top is None:
            raise ValueError('顶视画面不可用或过期')
        if automatic and self.robot and not self.robot.simulated and 'top' not in self.streams:
            raise RobotError('真机自动抓放必须使用实时相机，不能使用离线图片')
        return top.copy(), side.copy() if side is not None else None

    def _recognize(self, automatic=False):
        top, side = self.get_frames(automatic=automatic)
        partial = lambda items, ms: self.events.put(('partial', (items, ms, top, side)))
        items, ms = self.pipeline.recognize(top, side, on_partial=partial)
        self.events.put(('recognized', (items, ms, top, side)))
        return items

    def recognize(self):
        if self.rec_busy or self.busy:
            return
        self.rec_busy = True
        def run():
            try:
                self._recognize()
            except Exception as exc:
                self.events.put(('error', str(exc)))
            finally:
                self.events.put(('rec_done', None))
        threading.Thread(target=run, daemon=True).start()

    def toggle_live(self):
        self.live = not self.live
        self.note('连续识别已开启' if self.live else '连续识别已停止')

    def change_backend(self):
        if self.busy or self.rec_busy:
            self.backend.set(next(k for k, v in BACKENDS.items() if v == self.cfg['vision']['backend']))
            self.note('请等待识别或自动流程结束后切换模型')
            return
        selected = BACKENDS[self.backend.get()]
        if selected == 'cloud' and not messagebox.askyesno('云端识别', '会将物块裁剪图发送到配置的云端服务，调用可能计费。启用云端识别？'):
            self.backend.set(next(k for k, v in BACKENDS.items() if v == self.cfg['vision']['backend']))
            return
        self.cfg['vision']['backend'] = selected
        self.pipeline = VisionPipeline(self.cfg)
        self.items = []
        self.note('识别模式已切换；历史结果已清除')

    def open_cameras(self):
        if self.busy:
            return
        self.close_cameras()
        self.demo = False
        self.static = {'top': None, 'side': None}
        self.items = []
        for view in ('top', 'side'):
            self.streams[view] = make_stream(self.cfg['camera'], view)
        self.note('相机采集已启动')

    def close_cameras(self):
        if self.busy and not self.closing:
            self.note('请先停止并等待任务退出，再关闭相机')
            return
        self.live = False
        for stream in self.streams.values():
            stream.stop()
        self.streams = {}

    def import_image(self, view):
        if self.busy or self.rec_busy:
            return
        path = filedialog.askopenfilename(filetypes=[('图像', '*.png *.jpg *.jpeg *.bmp')])
        if path:
            try:
                self.close_cameras()
                self.static[view] = read_image(path)
                self.items = []
                self.pipeline = VisionPipeline(self.cfg)
                self.demo = False
                self.note(f'已导入 {view}：{Path(path).name}')
            except Exception as exc:
                self.note(exc)

    def select_roi(self, view):
        if self.busy or self.rec_busy:
            return
        self.roi_view, self.roi_clicks = view, []
        self.live = False
        self.note('依次点击托盘左上、右上、右下、左下；Esc 取消')
        self.bind('<Escape>', lambda event: self.cancel_roi())

    def cancel_roi(self):
        self.roi_view, self.roi_clicks = None, []

    def clear_roi(self, view):
        if self.busy or self.rec_busy:
            return
        self.cfg['camera'][view + '_points'] = []
        self.cfg['robot']['calibration_confirmed'] = False
        self.pipeline = VisionPipeline(self.cfg)
        self.items = []

    def canvas_click(self, event, view):
        if self.busy or self.rec_busy:
            return
        mapping = self.canvas_maps.get(view)
        if not mapping:
            return
        scale, ox, oy, width, height = mapping
        x, y = (event.x - ox) / scale, (event.y - oy) / scale
        if not 0 <= x < width or not 0 <= y < height:
            return
        if self.roi_view == view:
            self.roi_clicks.append([x / width, y / height])
            if len(self.roi_clicks) == 4:
                self.cfg['camera'][view + '_points'] = self.roi_clicks[:]
                self.cfg['robot']['calibration_confirmed'] = False
                self.pipeline = VisionPipeline(self.cfg)
                self.items = []
                self.cancel_roi()
                self.note('托盘四点已更新；X/Y 标定须复核，在配置页保存')
            return
        if view == 'top':
            for item in self.items:
                bx, by, bw, bh = item.bbox
                if bx <= x <= bx + bw and by <= y <= by + bh:
                    iid = str(item.track_id)
                    if self.table.exists(iid):
                        self.table.selection_set(iid)
                    self.note(item.label + '；' + item.note)
                    break

    def capture_background(self, view):
        if self.busy or self.rec_busy:
            return
        try:
            top, side = self.get_frames()
            frame = top if view == 'top' else side
            if frame is None:
                raise ValueError('该路没有画面')
            if not messagebox.askyesno('采集空背景', '请先移除托盘内所有物块，保持光照和机位不变。保存当前画面为空背景？'):
                return
            self.pipeline.backgrounds[view] = frame.copy()
            save_image(RUNTIME / f'background-{view}.png', frame)
            self.note('空背景已保存并生效；灰黑白物块可使用背景差分生成候选')
        except Exception as exc:
            self.note(exc)

    def save_sample(self):
        try:
            if self.last_top is None:
                raise ValueError('先识别一次，再保存同一时刻的图片和结果')
            folder = capture_sample(self.last_top, self.last_side, self.items)
            self.note(f'样本已保存：{folder}；请填写 labels.json 的实际形状/颜色/孔洞状态')
        except Exception as exc:
            self.note(exc)

    def start_auto(self):
        if self.busy or self.rec_busy:
            return
        try:
            robot = self.require_robot()
            robot.check_cancel()
            count = int(self.item_count.get())
            if not 1 <= count <= 100:
                raise ValueError('件数必须为 1~100')
            if robot.simulated and not self.demo:
                raise RobotError('模拟自动抓放请用 --demo 启动，避免将真实图片当作可取走物块')
            if not robot.simulated and not messagebox.askyesno('开始真机自动抓放', '将按已确认的标定和路线执行运动。确认机械位于 P0、转盘 1 号盒零点已设置，开始本批次？'):
                return
            self.live = False
            self.cancel_roi()
            if self.sorter is None:
                self.sorter = AutoSorter(robot, self.cfg['robot'], self.capture_for_auto, log=self.note,
                                         on_state=lambda s: self.events.put(('auto_state', s)),
                                         on_placed=self.on_placed)
            self.task(lambda: self.sorter.run(count))
        except Exception as exc:
            self.note(exc)

    def capture_for_auto(self):
        items = self._recognize(automatic=True)
        if self.robot and not self.robot.simulated:
            # Inference can take seconds. Re-observe the scene before trusting old coordinates.
            top, _ = self.get_frames(automatic=True)
            fresh = self.pipeline.segmenter.detect(top, self.cfg['camera']['top_points'],
                                                   self.pipeline.backgrounds.get('top'))
            used = set()
            for item in items:
                matches = [(i, t) for i, t in enumerate(fresh) if i not in used
                           and t.color == item.color and t.uv is not None and item.uv is not None
                           and math.dist(t.uv, item.uv) < .012
                           and .85 < (t.bbox[2]*t.bbox[3]) / max(1, item.bbox[2]*item.bbox[3]) < 1.18]
                if len(matches) == 1:
                    i, current = matches[0]
                    used.add(i)
                    item.uv = current.uv
                    item.measured_at = current.measured_at
                else:
                    item.stable = False
        return items

    def on_placed(self, item):
        if self.demo:
            frame = self.static['top'].copy()
            # Expand mask slightly to remove antialiased boundaries from the demo scene.
            mask = cv2.dilate(item.mask, np.ones((5, 5), np.uint8))
            frame[mask > 0] = (155, 155, 155)
            self.static['top'] = frame

    def reset_batch(self):
        if self.busy:
            self.note('先停止并等流程退出，再重置批次')
            return
        self.sorter = None
        self.auto_state.set('IDLE')
        self.auto_detail.set('新批次：盒映射为空；转盘仍需正确零点')

    def refresh_config(self):
        self.config_text.delete('1.0', 'end')
        self.config_text.insert('1.0', json.dumps(self.cfg, ensure_ascii=False, indent=2))

    def apply_config(self):
        if self.busy or self.rec_busy or (self.robot and self.robot.online):
            self.note('请先等待任务结束并断开控制，再应用配置')
            return
        try:
            raw = json.loads(self.config_text.get('1.0', 'end'))
            from .settings import DEFAULTS, merge
            cfg = merge(DEFAULTS, raw)
            for axis, limit in cfg['robot']['limits'].items():
                if len(limit) != 2 or not all(math.isfinite(float(v)) for v in limit) or limit[0] >= limit[1]:
                    raise ValueError(f'轴 {axis} 限位无效')
            for name in ('counts_per_pulse', 'position_age', 'move_timeout'):
                value = float(cfg['robot'][name])
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f'{name} 必须为正数')
            cfg['robot']['reference_confirmed'] = False
            save(cfg)
            self.cfg = cfg
            self.pipeline = VisionPipeline(cfg)
            self.items = []
            self.sorter = None
            self.backend.set(next(k for k, v in BACKENDS.items() if v == cfg['vision']['backend']))
            self.endpoint.set(cfg['robot']['endpoint'])
            self.simulated.set(cfg['robot']['simulated'])
            self.note(f'配置已保存：{CONFIG}')
        except Exception as exc:
            self.note(f'配置未应用：{exc}')

    def import_calibration(self):
        if self.busy or self.rec_busy or (self.robot and self.robot.online):
            self.note('先断开控制，再导入标定')
            return
        path = filedialog.askopenfilename(title='选择旧 tray_calib.json 或 limits.json', filetypes=[('JSON', '*.json')])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            if data.get('unit') != 'pulse':
                raise ValueError('只接受明确 unit=pulse 的标定和限位')
            if 'affine' in data:
                self.cfg['robot']['affine'] = data['affine']
                residual = data.get('residual') or []
                self.note('已导入仿射；最大残差：' + str(max((max(abs(r['dx_pulse']), abs(r['dy_pulse'])) for r in residual), default=0)))
            else:
                self.cfg['robot']['limits'] = {k: [v['min'], v['max']] for k, v in data.items()
                                                if k.isdigit() and isinstance(v, dict)}
            self.cfg['robot']['calibration_confirmed'] = False
            self.refresh_config()
            self.note('已导入但未确认；核对全场范围、路线和四点坐标，再设置 calibration_confirmed 并保存')
        except Exception as exc:
            self.note(exc)

    def selected_item(self):
        selected = self.table.selection()
        if len(selected) != 1:
            raise ValueError('先在视觉结果表选中一个目标')
        return next(t for t in self.items if str(t.track_id) == selected[0])

    def teach_point(self):
        if self.busy:
            return
        try:
            item = self.selected_item()
            if time.time() - item.measured_at > 5:
                raise ValueError('标定目标已过期，请在当前位置重新识别')
            if item.uv is None:
                raise ValueError('先框选托盘四点并重新识别')
            robot = self.require_robot()
            if not robot.simulated and not messagebox.askyesno('标定点', '请确认夹爪中心已手动对准所选物块中心。记录当前 X/Y 与图像 u/v？'):
                return
            self.teach_points.append({'u': item.uv[0], 'v': item.uv[1],
                                      'x': robot.position(4), 'y': robot.position(5)})
            self.note(f'已记录 {len(self.teach_points)} 个标定点；至少 4 个，分散覆盖托盘')
        except Exception as exc:
            self.note(exc)

    def solve_calibration(self):
        if self.busy or self.rec_busy:
            return
        try:
            if len(self.teach_points) < 4:
                raise ValueError('至少记录 4 个分散标定点')
            a = np.array([[p['u'], p['v'], 1] for p in self.teach_points])
            b = np.array([[p['x'], p['y']] for p in self.teach_points])
            solution, _, rank, _ = np.linalg.lstsq(a, b, rcond=None)
            if rank != 3 or np.linalg.cond(a) > 100:
                raise ValueError('标定点退化或过度集中')
            errors = np.abs(a @ solution - b)
            if np.max(errors) > 100:
                raise ValueError(f'最大残差 {np.max(errors):.1f} 脉冲超过 100；不覆盖标定')
            self.cfg['robot']['affine'] = dict(zip(('a', 'b', 'tx', 'c', 'd', 'ty'), solution.T.flatten().tolist()))
            self.cfg['robot']['calibration_confirmed'] = False
            self.refresh_config()
            self.note(f'已求解标定；最大残差 {np.max(errors):.1f} 脉冲。仍需检查工作范围并保存配置')
        except Exception as exc:
            self.note(exc)

    def run_benchmark(self):
        if self.busy or self.rec_busy:
            return
        path = filedialog.askopenfilename(title='选择填写真值后的 labels.json', filetypes=[('JSON', '*.json')])
        if not path:
            return
        backends = [x.strip() for x in self.eval_backend.get().split(',') if x.strip()]
        if any(x not in ('geometry', 'local', 'cloud') for x in backends) or not backends:
            self.note('评测后端只能为 geometry,local,cloud')
            return
        if 'cloud' in backends and not messagebox.askyesno('联网评测', '本批物块图片会发送至配置云端，可能产生调用费用。开始？'):
            return
        def run():
            output = RUNTIME / ('benchmark-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
            rows = benchmark(copy.deepcopy(self.cfg), path, backends)
            summary = write_report(rows, output)
            self.events.put(('benchmark', json.dumps(summary, ensure_ascii=False, indent=2) + '\n\n结果：' + str(output)))
        self.task(run)

    def update_table(self):
        selected = self.table.selection()
        for iid in self.table.get_children():
            self.table.delete(iid)
        for t in self.items:
            uv = '未标定' if t.uv is None else f'{t.uv[0]:.3f} / {t.uv[1]:.3f}'
            self.table.insert('', 'end', iid=str(t.track_id), values=(t.track_id, t.shape, t.color,
                t.hollow, '稳定' if t.stable else '观察中', t.source, f'{t.model_ms:.0f} ms', uv))
        if selected and self.table.exists(selected[0]):
            self.table.selection_set(selected[0])

    def render(self, view, frame):
        canvas = self.canvases[view]
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        canvas.delete('all')
        if frame is None:
            error = self.streams[view].error if view in self.streams else '打开相机或导入图片'
            canvas.create_text(width / 2, height / 2, text=error[:160], fill='#b4c6df', width=width - 25,
                               font=('Microsoft YaHei UI', 11))
            return
        h, w = frame.shape[:2]
        scale = min(width / w, height / h)
        dw, dh = max(1, round(w * scale)), max(1, round(h * scale))
        ox, oy = (width - dw) / 2, (height - dh) / 2
        self.canvas_maps[view] = (scale, ox, oy, w, h)
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize((dw, dh))
        photo = ImageTk.PhotoImage(image)
        self.photos[view] = photo
        canvas.create_image(ox, oy, anchor='nw', image=photo)
        if view == 'top' and self.items and time.time() - min(t.measured_at for t in self.items) < self.cfg['vision']['stale_seconds']:
            for item in self.items:
                x, y, bw, bh = item.bbox
                color = '#55e0b4' if item.stable else '#ffcb73'
                x0, y0, x1, y1 = ox + x * scale, oy + y * scale, ox + (x + bw) * scale, oy + (y + bh) * scale
                canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=2)
                text = f'#{item.track_id} {item.color}'
                canvas.create_text(x0, max(oy + 5, y0 - 20), text=text, anchor='nw', fill=color,
                                   font=('Microsoft YaHei UI', 11, 'bold'))
        points = self.roi_clicks if self.roi_view == view else self.cfg['camera'][view + '_points']
        coords = [(ox + p[0] * w * scale, oy + p[1] * h * scale) for p in points]
        for i, (x, y) in enumerate(coords):
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill='#6ad6ff', outline='')
            canvas.create_text(x + 7, y + 7, text=str(i + 1), fill='#6ad6ff', anchor='nw')
        if len(coords) > 1:
            canvas.create_line(*(sum(([x, y] for x, y in coords), [])), fill='#6ad6ff', width=2)

    def pump(self):
        if self.closing:
            return
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind in ('log', 'error'):
                    line = time.strftime('%H:%M:%S') + '  ' + str(data)
                    self.logs.configure(state='normal')
                    self.logs.insert('end', line + '\n')
                    self.logs.see('end')
                    self.logs.configure(state='disabled')
                    self.footer.set(str(data)[:170])
                    if kind == 'error':
                        self.live = False
                elif kind == 'task_done':
                    self.busy = False
                elif kind == 'rec_done':
                    self.rec_busy = False
                elif kind in ('recognized', 'partial'):
                    self.items, ms, self.last_top, self.last_side = data
                    self.last_result_monotonic = time.monotonic()
                    self.update_table()
                    self.footer.set(f'{len(self.items)} 个目标　整次识别 {ms:.0f} ms　{self.cfg["vision"]["backend"]}')
                    if kind == 'partial':
                        self.footer.set(f'已显示颜色和外形，正在补充立体分类 · {ms:.0f} ms')
                        continue
                    RUNTIME.mkdir(exist_ok=True)
                    tmp = RUNTIME / 'targets.tmp'
                    tmp.write_text(json.dumps({'ts': time.time(), 'targets': [t.public() for t in self.items]},
                                              ensure_ascii=False, indent=2), encoding='utf-8')
                    tmp.replace(RUNTIME / 'targets.json')
                elif kind == 'auto_state':
                    self.auto_state.set(data)
                    if self.sorter:
                        self.auto_detail.set(f'完成抓放 {self.sorter.placed} 件；已执行松爪 {self.sorter.released} 件；盒映射 {self.sorter.boxes}')
                elif kind == 'benchmark':
                    self.eval_output.delete('1.0', 'end')
                    self.eval_output.insert('1.0', data)
        except queue.Empty:
            pass
        for view in ('top', 'side'):
            frame = self.streams[view].get() if view in self.streams else self.static[view]
            self.render(view, frame)
        if self.robot:
            mode = '模拟' if self.robot.simulated else '真机'
            self.status.set(mode + (' · 在线' if self.robot.online else ' · 已断开') + (' · 已停止' if self.robot.latched else ' · 可操作'))
            for axis, variable in self.axis_positions.items():
                try:
                    variable.set(f'回读 {self.robot.position(axis):+.1f} 脉冲')
                except RobotError:
                    variable.set('回读缺失 / 已过期')
        else:
            self.status.set('控制未连接')
        now = time.monotonic()
        if self.live and not self.busy and not self.rec_busy and now - self.last_live >= self.cfg['vision']['interval']:
            self.last_live = now
            self.recognize()
        self.after(100, self.pump)

    def smoke(self):
        self.update_idletasks()
        RUNTIME.mkdir(exist_ok=True)
        try:
            x, y = self.winfo_rootx(), self.winfo_rooty()
            ImageGrab.grab((x, y, x + self.winfo_width(), y + self.winfo_height())).save(RUNTIME / 'gui-smoke.png')
            print('GUI smoke screenshot:', RUNTIME / 'gui-smoke.png', flush=True)
        finally:
            self.close()

    def close(self):
        self.closing = True
        self.stop_all()
        self.close_cameras()
        if self.robot:
            self.robot.close()
        self.destroy()
