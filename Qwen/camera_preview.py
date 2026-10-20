"""Local camera preview and asynchronous Qwen identification experiment."""
import json
import queue
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
import numpy as np
from cv2_enumerate_cameras import enumerate_cameras
from recognize import recognize, warm_model
from object_segmentation import separate_objects, object_crop, annotate
from world_detector import WorldDetector, DEFAULT_PROMPTS

ROOT = Path(__file__).resolve().parent


def valid_polygon(points, shape):
    height, width = shape[:2]
    p = np.array(points, dtype=np.float32) * (width - 1, height - 1)
    def cross(a, b, c):
        return float((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))
    def intersects(a, b, c, d):
        return cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0
    if any(np.linalg.norm(p[i]-p[j]) < 4 for i in range(4) for j in range(i)):
        return False
    if intersects(p[0],p[1],p[2],p[3]) or intersects(p[1],p[2],p[3],p[0]):
        return False
    return abs(cv2.contourArea(p.astype(np.float32))) >= 64


def crop_polygon(frame, points):
    height, width = frame.shape[:2]
    vertices = np.rint(np.array(points) * (width - 1, height - 1)).astype(np.int32)
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(mask, [vertices], 255)
    masked = np.full_like(frame, 255)
    masked[mask > 0] = frame[mask > 0]
    x, y, w, h = cv2.boundingRect(vertices)
    return masked[y:y+h, x:x+w].copy(), vertices.tolist()


class Preview:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('USB HD Camera + Qwen - 视觉识别实验')
        self.root.geometry('1000x780')
        self.frames = queue.Queue(maxsize=2)
        self.stop_event = None
        self.worker = None
        self.latest = None
        self.photo = None
        self.closed = False
        self.open_pending = False
        self.results = queue.Queue()
        self.recognizing = False
        self.snapshot_photo = None
        self.roi = None
        self.roi_points = []
        self.image_bounds = None
        self.rotation = 0
        self.background = None
        self.devices = list(enumerate_cameras(cv2.CAP_DSHOW))
        bar = ttk.Frame(self.root, padding=10)
        bar.pack(fill='x')
        self.selection = ttk.Combobox(bar, state='readonly', width=46,
            values=[f'{d.name}  (index={d.index})' for d in self.devices])
        self.selection.pack(side='left', padx=4)
        self.selection.bind('<<ComboboxSelected>>', lambda event: self.open_camera())
        ttk.Button(bar, text='打开摄像头', command=self.open_camera).pack(side='left', padx=4)
        ttk.Button(bar, text='拍照保存', command=self.save).pack(side='left', padx=4)
        self.recognize_button = ttk.Button(bar, text='识别当前画面', command=self.identify)
        self.recognize_button.pack(side='left', padx=4)
        ttk.Button(bar, text='手工框选识别', command=lambda: self.start_detection(manual=True)).pack(side='left', padx=4)
        ttk.Button(bar, text='清除区域', command=self.clear_roi).pack(side='left', padx=4)
        settings = ttk.Frame(self.root, padding=8)
        settings.pack(fill='x')
        ttk.Label(settings, text='分类列表（逗号分隔，留空自由分类）：').pack(side='left')
        self.categories = ttk.Entry(settings, width=40)
        self.categories.pack(side='left', padx=4)
        self.automatic = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings, text='连续识别（完成后间隔3秒）', variable=self.automatic).pack(side='left')
        view_bar = ttk.Frame(self.root, padding=5)
        view_bar.pack(fill='x')
        ttk.Button(view_bar, text='向左旋转90°', command=lambda: self.rotate_view(-90)).pack(side='left', padx=4)
        ttk.Button(view_bar, text='向右旋转90°', command=lambda: self.rotate_view(90)).pack(side='left', padx=4)
        self.rotation_text = tk.StringVar(value='画面方向：0°')
        ttk.Label(view_bar, textvariable=self.rotation_text).pack(side='left', padx=8)
        self.high_detail = tk.BooleanVar(value=False)
        ttk.Checkbutton(view_bar, text='高清识别（保留更多细节，速度较慢）', variable=self.high_detail).pack(side='left')
        self.geometry_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(view_bar, text='分析可见面和中间棱', variable=self.geometry_mode).pack(side='left', padx=8)
        segmentation_bar = ttk.Frame(self.root, padding=5)
        segmentation_bar.pack(fill='x')
        ttk.Label(segmentation_bar, text='模型：').pack(side='left')
        self.model = ttk.Combobox(segmentation_bar, state='readonly', width=17,
                                 values=['qwen2.5vl:3b', 'qwen2.5vl:7b'])
        self.model.set('qwen2.5vl:3b')
        self.model.pack(side='left', padx=4)
        ttk.Button(segmentation_bar, text='采集空台面背景', command=self.capture_background).pack(side='left', padx=4)
        ttk.Button(segmentation_bar, text='清除背景', command=self.clear_background).pack(side='left', padx=4)
        ttk.Button(segmentation_bar, text='预览物体分离', command=self.preview_objects).pack(side='left', padx=4)
        ttk.Button(segmentation_bar, text='预热当前模型', command=self.warm_current_model).pack(side='left', padx=4)
        ttk.Label(segmentation_bar, text='阈值：').pack(side='left')
        self.threshold = ttk.Spinbox(segmentation_bar, from_=5, to=200, width=5)
        self.threshold.set('30')
        self.threshold.pack(side='left')
        ttk.Label(segmentation_bar, text='最小面积%：').pack(side='left')
        self.min_area = ttk.Spinbox(segmentation_bar, from_=0.05, to=20, increment=.1, width=5)
        self.min_area.set('0.3')
        self.min_area.pack(side='left')
        self.detector = WorldDetector()
        detection_bar = ttk.Frame(self.root, padding=5)
        detection_bar.pack(fill='x')
        self.backend = ttk.Combobox(detection_bar, state='readonly', width=14,
                                   values=['台面增强', 'YOLO-World', '台面分离'])
        self.backend.set('台面增强')
        self.backend.pack(side='left')
        ttk.Label(detection_bar, text='英文提示词：').pack(side='left')
        self.prompts = ttk.Entry(detection_bar, width=65)
        self.prompts.insert(0, DEFAULT_PROMPTS)
        self.prompts.pack(side='left', fill='x', expand=True)
        ttk.Label(detection_bar, text='置信度：').pack(side='left')
        self.confidence = ttk.Spinbox(detection_bar, from_=.01, to=.95, increment=.05, width=5)
        self.confidence.set('.15')
        self.confidence.pack(side='left')
        self.background_text = tk.StringVar(value='未采集背景：仅提取彩色物体；白/灰/黑物体请先采集空台面。')
        ttk.Label(self.root, textvariable=self.background_text, padding=4).pack(fill='x')
        self.model.bind('<<ComboboxSelected>>', lambda event: self.result_text.set(
            '3B适合实时逐件识别；7B更适合难例复核，单件推理可能明显更慢。'))
        panel = ttk.LabelFrame(self.root, text='最近一次识别的截图与结果', padding=8)
        panel.pack(side='bottom', fill='x', padx=8, pady=8)
        self.snapshot = ttk.Label(panel)
        self.snapshot.pack(side='left', padx=8)
        self.result_text = tk.StringVar(value='把一件物品放在画面中央，点击“识别当前画面”。\n结果对应左侧截图，不代表最新实时画面。')
        self.result_box = tk.Text(panel, height=7, width=60, wrap='word', state='disabled')
        self.result_box.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(panel, command=self.result_box.yview)
        scroll.pack(side='right', fill='y')
        self.result_box.configure(yscrollcommand=scroll.set)
        self.result_text.trace_add('write', self.refresh_result_text)
        self.refresh_result_text()
        self.status = tk.StringVar(value='正在准备摄像头…')
        ttk.Label(self.root, textvariable=self.status, padding=8).pack(fill='x')
        self.roi_text = tk.StringVar(value='沿区域边缘顺时针或逆时针点击4个顶点；右键撤销。未选择时识别全画面。')
        ttk.Label(self.root, textvariable=self.roi_text, padding=4).pack(fill='x')
        self.display = tk.Canvas(self.root, background='#202020', highlightthickness=0)
        self.display.pack(fill='both', expand=True)
        self.display.bind('<ButtonPress-1>', self.add_roi_point)
        self.display.bind('<ButtonPress-3>', self.undo_roi_point)
        self.display.bind('<Configure>', lambda event: self.render_frame())
        ttk.Label(self.root, text='本地千问识别实验 · 不发送机械运动指令 · 识别照片和记录保存在 Qwen 内',
                  padding=8).pack(fill='x')
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        if self.devices:
            preferred = next((i for i, d in enumerate(self.devices)
                              if 'usb hd camera' in d.name.lower()), 0)
            self.selection.current(preferred)
            self.root.after(100, self.open_camera)
        else:
            self.status.set('未找到摄像头，请检查 USB 连接和 Windows 摄像头权限，再重新打开窗口。')
        self.root.after(40, self.update)
        self.root.after(3000, self.auto_identify)

    def auto_identify(self):
        if self.closed:
            return
        if self.automatic.get() and not self.recognizing and self.latest is not None and (not self.roi_points or self.roi is not None):
            self.identify()
        self.root.after(3000, self.auto_identify)

    def refresh_result_text(self, *args):
        self.result_box.configure(state='normal')
        self.result_box.delete('1.0', 'end')
        self.result_box.insert('1.0', self.result_text.get())
        self.result_box.configure(state='disabled')

    def capture_background(self):
        if self.latest is None:
            return
        if not messagebox.askokcancel('采集空台面', '请先移走所有待识别物品，保持相机和夹爪位置不动，再点确定。'):
            return
        self.background = self.view_frame().copy()
        self.background_text.set('空台面背景已采集：相机、光照、夹爪或方向变化后请重新采集。')
        (ROOT / 'data').mkdir(exist_ok=True)
        cv2.imencode('.png', self.background)[1].tofile(str(ROOT / 'data/background.png'))

    def clear_background(self):
        self.background = None
        self.background_text.set('未采集背景：仅提取彩色物体；白/灰/黑物体请先采集空台面。')

    def detect_current(self):
        if self.latest is None:
            raise ValueError('请先打开摄像头。')
        if self.roi_points and self.roi is None:
            raise ValueError('请先点满4个顶点，或清除区域。')
        threshold, area = float(self.threshold.get()), float(self.min_area.get())
        if not 5 <= threshold <= 200 or not .05 <= area <= 20:
            raise ValueError('阈值应在5~200，最小面积应在0.05~20%。')
        frame = self.view_frame().copy()
        polygon = list(self.roi) if self.roi else None
        objects, mask = separate_objects(frame, polygon, self.background, threshold, area)
        return frame, polygon, objects, mask, threshold, area

    def show_snapshot(self, frame):
        scale = min(300 / frame.shape[1], 220 / frame.shape[0], 1)
        thumb = cv2.resize(frame, (max(1, round(frame.shape[1]*scale)), max(1, round(frame.shape[0]*scale))))
        self.snapshot_photo = tk.PhotoImage(data=cv2.imencode('.png', thumb)[1].tobytes())
        self.snapshot.configure(image=self.snapshot_photo)

    def preview_objects(self):
        self.start_detection(preview=True)

    def warm_current_model(self):
        if self.recognizing:
            return
        model = self.model.get()
        if not (ROOT / 'models/manifests/registry.ollama.ai/library/qwen2.5vl' / model.split(':')[-1]).exists():
            self.result_text.set(f'{model} 尚未下载完成。')
            return
        self.recognizing = True
        self.recognize_button.configure(state='disabled')
        self.result_text.set(f'正在预热 {model}，首次需要等待模型加载；完成后识别会明显变快。')
        threading.Thread(target=self.run_warm, args=(model,), daemon=True).start()

    def run_warm(self, model):
        try:
            warm_model(model, '30m')
            self.results.put({'kind': 'warm', 'model': model})
        except Exception as exc:
            self.results.put(str(exc))

    def clear_roi(self):
        self.roi = None
        self.roi_points = []
        self.roi_text.set('沿区域边缘顺时针或逆时针点击4个顶点；右键撤销。未选择时识别全画面。')
        self.render_frame()

    def rotate_view(self, angle):
        self.rotation = (self.rotation + angle) % 360
        self.rotation_text.set(f'画面方向：{self.rotation}°')
        self.clear_roi()
        self.clear_background()

    def view_frame(self):
        if self.latest is None:
            return None
        code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
                270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(self.rotation)
        return self.latest if code is None else cv2.rotate(self.latest, code)

    def image_point(self, event, clamp=False):
        if self.image_bounds is None:
            return None
        x, y, width, height = self.image_bounds
        if not clamp and not (x <= event.x <= x + width and y <= event.y <= y + height):
            return None
        return (max(0, min(1, (event.x - x) / width)),
                max(0, min(1, (event.y - y) / height)))

    def add_roi_point(self, event):
        if self.latest is None:
            return
        point = self.image_point(event)
        if point is None:
            return
        if len(self.roi_points) == 4:
            self.roi_points = []
        self.roi = None
        self.roi_points.append(point)
        if len(self.roi_points) == 4:
            if valid_polygon(self.roi_points, self.view_frame().shape):
                self.roi = list(self.roi_points)
                self.roi_text.set('四点区域已生效，只识别绿色四边形内部。左键重新选点，右键撤销。')
            else:
                self.roi_points.pop()
                self.roi_text.set('第4点无效：区域交叉、过小或顶点重合，请沿边缘重新点击。')
        else:
            self.roi_text.set(f'已选择 {len(self.roi_points)}/4 个顶点，请继续沿边缘点击；选完前暂停识别。')
        self.render_frame()

    def undo_roi_point(self, event=None):
        if self.roi_points:
            self.roi_points.pop()
        self.roi = None
        self.roi_text.set(f'已选择 {len(self.roi_points)}/4 个顶点；右键撤销，左键继续。')
        self.render_frame()

    def render_frame(self):
        if self.latest is None or self.closed:
            return
        frame = self.view_frame()
        height, width = frame.shape[:2]
        cw, ch = max(1, self.display.winfo_width()), max(1, self.display.winfo_height())
        scale = min(cw / width, ch / height, 1)
        sw, sh = max(1, int(width * scale)), max(1, int(height * scale))
        x, y = (cw - sw) / 2, (ch - sh) / 2
        self.image_bounds = (x, y, sw, sh)
        shown = cv2.resize(frame, (sw, sh))
        self.photo = tk.PhotoImage(data=cv2.imencode('.png', shown)[1].tobytes())
        self.display.delete('all')
        self.display.create_image(x, y, anchor='nw', image=self.photo)
        points = [(x + px * sw, y + py * sh) for px, py in self.roi_points]
        if len(points) >= 2:
            line = points + ([points[0]] if self.roi is not None else [])
            self.display.create_line(*[v for point in line for v in point], fill='#36ff70', width=3)
        for number, (px, py) in enumerate(points, 1):
            self.display.create_oval(px-5, py-5, px+5, py+5, fill='#36ff70')
            self.display.create_text(px+12, py-12, text=str(number), fill='#36ff70')

    def identify(self):
        self.start_detection(preview=False)

    def start_detection(self, preview=False, manual=False):
        if self.recognizing:
            return
        try:
            if self.latest is None or (self.roi_points and self.roi is None):
                raise ValueError('请打开摄像头并完成或清除四点区域。')
            frame = self.view_frame().copy()
            polygon = list(self.roi) if self.roi else None
            threshold, area = float(self.threshold.get()), float(self.min_area.get())
            confidence = float(self.confidence.get())
            if not 5 <= threshold <= 200 or not .05 <= area <= 20:
                raise ValueError('阈值应在5~200，最小面积应在0.05~20%。')
            model = self.model.get()
            if not preview and not (ROOT / 'models/manifests/registry.ollama.ai/library/qwen2.5vl' / model.split(':')[-1]).exists():
                raise ValueError(f'{model} 尚未下载完成。')
            meta = dict(batch_id=datetime.now().strftime('%Y%m%d-%H%M%S-%f'), model=model,
                        captured_at=datetime.now().astimezone().isoformat(),
                        rotation_degrees=self.rotation, roi_normalized=polygon,
                        frame_size_px=[frame.shape[1], frame.shape[0]],
                        camera=self.selection.get(), coordinate_space='rotated_full_frame',
                        input_source='live_camera',
                        coordinate_origin='top_left', axis_directions='x_right_y_down',
                        bbox_format='xywh', segmentation=self.backend.get(), threshold=threshold,
                        min_area_percent=area, geometry=bool(self.geometry_mode.get()))
            options = dict(preview=preview, prompts=self.prompts.get(), confidence=confidence,
                           max_side=1280 if self.high_detail.get() else 640,
                           categories=[x.strip() for x in self.categories.get().replace('，', ',').split(',') if x.strip()])
            if manual:
                from box_editor import select_boxes
                self.recognizing = True
                try:
                    options['manual_boxes'] = select_boxes(self.root, frame)
                finally:
                    self.recognizing = False
                if not options['manual_boxes']:
                    return
                polygon = None
                meta['roi_normalized'] = None
                meta['segmentation'] = 'manual'
            background = self.background.copy() if self.background is not None else None
            self.recognizing = True
            self.recognize_button.configure(state='disabled')
            self.result_text.set('正在检测；首次加载模型和提示词可能较慢…')
            threading.Thread(target=self.run_detection, args=(frame, polygon, background, meta, options), daemon=True).start()
        except Exception as exc:
            self.automatic.set(False)
            self.result_text.set(f'准备失败：{exc}')

    def run_detection(self, frame, polygon, background, meta, options):
        pipeline_started = time.perf_counter()
        try:
            if meta['segmentation'] == 'manual':
                from box_editor import candidates_from_boxes
                objects = candidates_from_boxes(options['manual_boxes'])
                mask = np.zeros(frame.shape[:2], np.uint8)
                for obj in objects:
                    cv2.drawContours(mask, [obj['contour']], -1, 255, cv2.FILLED)
            elif meta['segmentation'] == '台面增强':
                from tabletop_detector import detect_tabletop
                objects, mask = detect_tabletop(frame, polygon, background, meta['threshold'], meta['min_area_percent'])
                meta['background_used'] = background is not None
            elif meta['segmentation'] == 'YOLO-World':
                objects, mask, timing = self.detector.detect(frame, polygon, options['prompts'], options['confidence'])
                meta.update(timing)
            else:
                objects, mask = separate_objects(frame, polygon, background, meta['threshold'], meta['min_area_percent'])
                meta['segmentation'] = 'background_difference' if background is not None else 'color'
            for obj in objects:
                x,y,w,h = obj['bbox']
                obj.setdefault('center_px', [x+(w-1)/2, y+(h-1)/2])
            overview = annotate(frame, objects)
            self.results.put(dict(kind='detected', frame=overview, count=len(objects),
                                  preview=options['preview'], seconds=round(time.perf_counter()-pipeline_started,3),
                                  advice=('未采空台面：灰白物体可能漏检；同色相接物体可能合框，可用手工框选。' if meta['segmentation']=='台面增强' and background is None else '请检查框是否覆盖完整单件物体。')))
            if options['preview']:
                return
            folder = ROOT / 'samples' / meta['batch_id']
            folder.mkdir(parents=True, exist_ok=True)
            (ROOT / 'data').mkdir(exist_ok=True)
            for name, picture in [('frame.jpg',frame), ('overview.jpg',overview), ('mask.png',mask)]:
                cv2.imencode(Path(name).suffix, picture)[1].tofile(str(folder / name))
            jobs = []
            for obj in objects:
                crop = object_crop(frame, obj, polygon, options['max_side'])
                target = folder / f"object-{obj['object_id']:02d}.jpg"
                cv2.imencode('.jpg',crop,[cv2.IMWRITE_JPEG_QUALITY,95])[1].tofile(str(target))
                jobs.append({k:v for k,v in obj.items() if k != 'contour'} | {'image':str(target)})
            self.run_batch(jobs, options['categories'], meta, pipeline_started)
        except Exception as exc:
            self.results.put(str(exc))

    def run_batch(self, jobs, categories, meta, pipeline_started=None):
        records = []
        started = time.perf_counter()
        try:
            startup = subprocess.run(
                ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'start.ps1')],
                cwd=ROOT, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
            if startup.returncode:
                raise RuntimeError('本地模型服务启动失败，请查看 Qwen/logs。')
            for job in jobs:
                if self.closed:
                    return
                self.results.put({'kind': 'progress', 'records': list(records), 'total': len(jobs),
                                  'current': job['object_id'], 'model': meta['model']})
                try:
                    record = recognize(job['image'], categories or None, model=meta['model'],
                                       geometry=meta.get('geometry', False))
                    record.update(meta)
                    record.update(job)
                except Exception as exc:
                    record = dict(meta, **job, error=str(exc))
                records.append(record)
                with (ROOT / 'data/recognitions.jsonl').open('a', encoding='utf-8') as output:
                    output.write(json.dumps(record, ensure_ascii=False) + '\n')
            batch = dict(meta, objects=records,
                         recognition_batch_seconds=round(time.perf_counter()-started, 3),
                         elapsed_seconds=round(time.perf_counter()-(pipeline_started if pipeline_started is not None else started), 3))
            (ROOT / 'samples' / meta['batch_id'] / 'results.json').write_text(
                json.dumps(batch, ensure_ascii=False, indent=2), encoding='utf-8')
            # Atomic pixel-only handoff. The host never interprets these as motor commands.
            handoff = dict(batch, schema_version=1, motion_authorized=False,
                           calibration_status='missing', grasp_point_status='bbox_center_only')
            temporary = ROOT / 'data/vision_targets.tmp'
            temporary.write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(ROOT / 'data/vision_targets.json')
            self.results.put(dict(batch, kind='batch'))
        except Exception as exc:
            self.results.put(str(exc))

    @staticmethod
    def format_objects(records):
        lines = []
        for record in records:
            prefix = f"#{record['object_id']}"
            if 'error' in record:
                lines.append(f"{prefix} 识别失败：{record['error']}")
                continue
            obj = record['recognition']
            flag = '待复核' if obj['uncertain'] else '模型未报告不确定'
            edge = '；触及选区边缘' if record.get('touches_roi') else ''
            lines.append(f"{prefix} {obj['name']} ｜ {obj['category']} ｜ {obj.get('color', '未知')} ｜ {obj.get('geometry_type', '未知')} ｜ {flag}{edge}\n"
                         f"中心像素：{record.get('center_px')}；依据：{obj['reason']} （{record['elapsed_seconds']}秒）")
        return '\n'.join(lines)

    def open_camera(self):
        if self.selection.current() < 0:
            return
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status.set('正在切换摄像头…')
            if not self.open_pending:
                self.open_pending = True
                self.root.after(100, self.finish_switch)
            return
        while not self.frames.empty():
            self.frames.get_nowait()
        self.latest = None
        self.clear_background()
        self.image_bounds = None
        self.display.delete('all')
        self.clear_roi()
        device = self.devices[self.selection.current()]
        self.stop_event = threading.Event()
        self.status.set(f'正在打开 {device.name}…')
        self.worker = threading.Thread(target=self.capture,
                                       args=(device, self.stop_event), daemon=True)
        self.worker.start()

    def finish_switch(self):
        if self.closed:
            return
        if self.worker and self.worker.is_alive():
            self.root.after(100, self.finish_switch)
            return
        self.open_pending = False
        self.open_camera()

    def publish(self, value):
        try:
            self.frames.put_nowait(value)
        except queue.Full:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self.frames.put_nowait(value)
            except queue.Full:
                pass

    def capture(self, device, stop):
        cap = cv2.VideoCapture(device.index, cv2.CAP_DSHOW)
        try:
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(device.index, cv2.CAP_MSMF)
            if not cap.isOpened():
                self.publish(f'{device.name} 打开失败（DSHOW/MSMF）：请检查占用、USB连接及Windows摄像头权限。')
                return
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            while not stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    self.publish('读取失败：摄像头可能已断开或被其他软件占用。')
                    return
                self.publish(frame)
        except Exception as exc:
            self.publish(f'摄像头错误：{exc}')
        finally:
            cap.release()

    def update(self):
        if self.closed:
            return
        try:
            result = self.results.get_nowait()
        except queue.Empty:
            result = None
        if result is not None:
            if isinstance(result, dict) and result.get('kind') == 'progress':
                self.result_text.set(f"{result['model']} 正在识别 #{result['current']}/{result['total']}…\n"
                                     + self.format_objects(result['records']))
            elif isinstance(result, dict) and result.get('kind') == 'detected':
                self.show_snapshot(result['frame'])
                self.result_text.set(f"检测到 {result['count']} 个候选；检测耗时 {result['seconds']} 秒。\n{result.get('advice', '')}\n框中心仅是像素位置，需标定和抓取点复核。")
                if result['preview']:
                    self.recognizing = False
                    self.recognize_button.configure(state='normal')
            elif isinstance(result, dict) and result.get('kind') == 'warm':
                self.recognizing = False
                self.recognize_button.configure(state='normal')
                self.result_text.set(f"{result['model']} 已预热并保持驻留约30分钟；现在点击识别当前画面即可。")
            else:
                self.recognizing = False
                self.recognize_button.configure(state='normal')
                if isinstance(result, str):
                    self.automatic.set(False)
                    self.result_text.set(f'识别失败：{result}\n连续识别已暂停。')
                else:
                    if any('error' in r for r in result['objects']):
                        self.automatic.set(False)
                    self.result_text.set(f"{result['model']} ｜ {len(result['objects'])}个候选 ｜ "
                        f"总耗时{result['elapsed_seconds']}秒\n" + self.format_objects(result['objects']))
        item = None
        while not self.frames.empty():
            item = self.frames.get_nowait()
        if isinstance(item, str):
            self.latest = None
            self.image_bounds = None
            self.display.delete('all')
            self.status.set(item)
        elif item is not None:
            self.latest = item
            height, width = item.shape[:2]
            self.render_frame()
            self.status.set(f'实时预览中 · {width} × {height} · 关闭窗口即可释放摄像头')
        self.root.after(40, self.update)

    def save(self):
        if self.latest is None:
            messagebox.showinfo('拍照', '请先打开摄像头并等待画面。')
            return
        folder = ROOT / 'samples'
        folder.mkdir(exist_ok=True)
        target = folder / f'camera-{datetime.now():%Y%m%d-%H%M%S-%f}.jpg'
        ok, data = cv2.imencode('.jpg', self.view_frame())
        if ok:
            data.tofile(str(target))
            messagebox.showinfo('照片已保存', str(target))

    def close(self):
        self.closed = True
        if self.stop_event:
            self.stop_event.set()
        self.root.destroy()


if __name__ == '__main__':
    app = Preview()
    app.root.mainloop()
