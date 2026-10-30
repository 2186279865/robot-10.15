"""派端比赛显示屏 v8（智能分拣赛项）——六个储物盒分列显示。

与 v7 的区别（见 docs/派端屏改造说明.md）：
  1) **就地刷新**：所有控件只在启动时创建一次（`_build`），之后只改文本/图片/位置，
     不再 `destroy()` 重建 → 屏幕不闪。
  2) **照片实时**：物块照片不再是上位机识别时的旧缩略图，而是收到 item 的那一刻
     从本机 `pi_agent` 的**只读预览口 5060** 取“最新缓存帧”，按 item 里的 bbox
     裁剪 → 得到“放入瞬间”的真实照片。取不到时回退到上位机推的 thumb_b64。
  3) 视觉版式、协议字段与 v7 等价，旧上位机不需改动即可使用（只是照片用旧图）。

赛题要求（原文，02-资料库/01-赛题/newpdftext.txt）：
  1) 分拣装置上需安装一块仅具有显示功能的高亮显示屏，支持各种格式图片播放，
     显示分拣信息与数据（序号、图片及相关信息）；
  2) 每分拣一件并入盒后，屏上须显示：序号、货物名称、图片、分拣成功总数量；
  3) 超过 15 s 没有动作或没有显示本次分拣信息 → 本轮结束（屏必须一直更新）。

版式（1024×600）：
  ┌ 顶栏 56px：● 状态 / 当前类别 → 盒号          计时  7/12  ● 已连接 ┐
  │ 六列（每列 ~159px）：盒号 · ●类别 · N 件                          │
  │   列内自上而下堆物品卡（缩略图 + #序号），最多 4 张                 │
  ├ 底栏 72px：最新一件（缩略图 + #序号 + 名称）        ⚠ 报警        ┘

用法：
    python3 pi_display.py                       # 全屏，监听 5059，预览口取本机 5060
    python3 pi_display.py --windowed            # 窗口（调试/截图）
    python3 pi_display.py --fake                # 假数据演示
    python3 pi_display.py --preview-port 0      # 关闭实时照片（只用上位机缩略图）

协议（上位机→派，NDJSON 每行一条；字段可缺省）：
    {"kind":"state","state":"running","elapsed_s":123.4,"done":7,"total":12,
     "alerts":[],"connected":true,"last_event":"...","current_box":3,
     "current_label":"正五棱柱 黄"}
    {"kind":"item","seq":7,"name":"正五棱柱","color":"黄","box":3,
     "box_label":"正五棱柱 黄","thumb_b64":"<PNG base64>","total_done":7,
     "bbox":[x,y,w,h],"cam":0}          # bbox 归一化(0~1)，托盘校正图坐标
    {"kind":"item_done","seq":7,"ok":true,"total_done":7}
    {"kind":"alarm","text":"DeepSeek 超时，已回退几何判据"}
兼容旧格式 {"state":"DISPLAY","item":3,"total":12,"name":"五棱柱|蓝","box":2,"placed":3}。
"""
import argparse
import base64
import json
import os
import queue
import socket
import struct
import threading
import time

import tkinter as tk

BG = '#0a0c0f'
PANEL = '#12161b'
PANEL2 = '#0f1318'
FG = '#f3f6f9'
DIM = '#7d8894'
LINE = '#242b34'
ACCENT = '#39d353'
WARN = '#ff9f43'
BAD = '#e5484d'
TRACK = '#1d232b'

# ---------------------------------------------------------------- 主题
THEMES = {
    'dark': {'BG': '#0a0c0f', 'PANEL': '#12161b', 'PANEL2': '#0f1318',
             'FG': '#f3f6f9', 'DIM': '#7d8894', 'LINE': '#242b34',
             'ACCENT': '#39d353', 'WARN': '#ff9f43', 'BAD': '#e5484d',
             'TRACK': '#1d232b'},
    'light': {'BG': '#eef1f5', 'PANEL': '#ffffff', 'PANEL2': '#f7f9fc',
              'FG': '#1b2430', 'DIM': '#5f6b7a', 'LINE': '#d3dae3',
              'ACCENT': '#12874a', 'WARN': '#c2761a', 'BAD': '#c62828',
              'TRACK': '#e3e8ef'},
    'blue': {'BG': '#0d1b2a', 'PANEL': '#132538', 'PANEL2': '#10202f',
             'FG': '#eef4fa', 'DIM': '#8fa3b8', 'LINE': '#22394f',
             'ACCENT': '#4ecb71', 'WARN': '#ffb347', 'BAD': '#ff6b6b',
             'TRACK': '#1a3147'},
}


def set_theme(name):
    """切换模块级配色（BG/PANEL/FG/...）。"""
    pal = THEMES.get(str(name).lower(), THEMES['light'])
    g = globals()
    for k, v in pal.items():
        g[k] = v
    return pal


UI_FONT = 'Microsoft YaHei UI'
MONO = 'Consolas'

STATE_TEXT = {'idle': '待机', 'running': '运行中', 'paused': '暂停',
              'timeout': '超时', 'done': '完成'}
STATE_COLOR = {'idle': DIM, 'running': ACCENT, 'paused': WARN,
               'timeout': BAD, 'done': '#4ea1ff'}

# 颜色名 → 展示色（类别色点 / 缩略图占位）
COLOR_HEX = {
    '红': '#e04b3f', '橙': '#f08a2c', '黄': '#efc632', '黄绿': '#a8cf3f',
    '绿': '#4aa85a', '深绿': '#2f6b3d', '浅绿': '#b9d36a', '蓝': '#3b7dd8',
    '浅蓝': '#6fc3e0', '深蓝': '#2a4f8f', '紫': '#8a6cd1', '白': '#e6e6e6',
    '黑': '#3a3f45', '粉': '#e58fb1', '灰': '#9aa3ab',
}


def color_hex(name):
    if not name:
        return DIM
    key = str(name).strip()
    return COLOR_HEX.get(key, COLOR_HEX.get(key[:1], DIM))


def parse_line(raw):
    if isinstance(raw, bytes):
        try:
            raw = raw.decode('utf-8', 'replace')
        except Exception:                            # noqa: BLE001
            return None
    text = str(raw).strip()
    if not text.startswith('{'):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- 实时照片
IMG_MAGIC = b'IM'
ERR_MAGIC = b'ER'


def fetch_frame(host, port, cam=0, timeout=0.9):
    """从 pi_agent 的只读预览口取“最新缓存帧”的 JPEG 字节；失败返回 None。"""
    if not port:
        return None
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.sendall(('%d\n' % int(cam)).encode('ascii'))
            head = _recv_exact(sock, 2)
            if head == ERR_MAGIC:
                rest = _recv_exact(sock, 4)
                (_m, _code, n) = struct.unpack('<2sHH', head + rest)
                if n:
                    _recv_exact(sock, n)
                return None
            if head != IMG_MAGIC:
                return None
            rest = _recv_exact(sock, 22)              # IMG_HDR_LEN = 24
            _m, _cam, _w, _h, _seq = struct.unpack('<2sHHII', head + rest)
            ts_us, n = struct.unpack('<QI', _recv_exact(sock, 12))
            if n <= 0 or n > 8 * 1024 * 1024:
                return None
            return _recv_exact(sock, n)
    except (OSError, struct.error, ValueError):
        return None


def _recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError('连接提前关闭')
        buf += chunk
    return buf


_CROP_CFG = {'points': None, 'tried': False}


def _load_top_points():
    """读上位机的四点框（用于“托盘坐标 → 相机像素”的换算），读不到返回 None。"""
    if _CROP_CFG['tried']:
        return _CROP_CFG['points']
    _CROP_CFG['tried'] = True
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, '..', '..', 'vision', 'config.json'),
        os.path.join(here, '..', 'config.json'),
        os.path.expanduser('~/robot1021/vision/config.json'),
    ]
    for path in cands:
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                cfg = json.load(fh)
            pts = cfg.get('top_points')
            if isinstance(pts, list) and len(pts) == 4:
                _CROP_CFG['points'] = [(float(p[0]), float(p[1])) for p in pts]
                break
        except (OSError, ValueError, TypeError, IndexError):
            continue
    return _CROP_CFG['points']


def crop_square(jpeg, bbox=None, cam=0):
    """把整帧 JPEG 按 bbox（托盘归一化坐标）裁成正方形，返回 PNG base64；失败返回 None。"""
    try:
        import numpy as np
        import cv2
    except ImportError:
        return None
    try:
        buf = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:                                # noqa: BLE001
        return None
    if img is None or not getattr(img, 'size', 0):
        return None
    h, w = img.shape[:2]
    side = int(min(h, w) * 0.42)
    cx, cy = w // 2, h // 2
    pts = _load_top_points()
    if bbox and len(bbox) == 4 and pts:
        try:
            x0, y0, bw, bh = (float(v) for v in bbox)
            uc, vc = x0 + bw / 2.0, y0 + bh / 2.0
            src = np.float32(pts)
            dst = np.float32([[0, 0], [699, 0], [699, 699], [0, 699]])
            # 用 (u,v) 在四点上做双线性插值 → 相机像素（够用且不依赖 H 的逆）
            px = _bilinear(src, uc, vc)
            if px is not None:
                cx, cy = int(px[0]), int(px[1])
                side = int(max(bw, bh) * w * 2.2)
        except Exception:                            # noqa: BLE001
            pass
    side = max(64, min(side, int(min(h, w) * 0.9)))
    x1, y1 = max(0, cx - side // 2), max(0, cy - side // 2)
    x2, y2 = min(w, x1 + side), min(h, y1 + side)
    if x2 - x1 < 24 or y2 - y1 < 24:
        return None
    crop = img[y1:y2, x1:x2]
    try:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        ok, enc = cv2.imencode('.png', crop)
    except Exception:                                # noqa: BLE001
        return None
    if not ok:
        return None
    return base64.b64encode(enc.tobytes()).decode('ascii')


def _bilinear(src, u, v):
    """四点（左上/右上/右下/左下）构成的四边形内，按 (u,v)∈[0,1] 双线性取点。"""
    try:
        tl, tr, br, bl = src
        u = min(max(float(u), 0.0), 1.0)
        v = min(max(float(v), 0.0), 1.0)
        tx = tl + (tr - tl) * u
        bx = bl + (br - bl) * u
        pt = tx + (bx - tx) * v
        return float(pt[0]), float(pt[1])
    except Exception:                                # noqa: BLE001
        return None


# ---------------------------------------------------------------- 网络
class Pump:
    """TCP 行服务器（5059）：收到 JSON 塞队列，坏行静默丢弃，断线不清屏。"""

    def __init__(self, port, sink):
        self.port = int(port)
        self.sink = sink
        self.sock = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', self.port))
        s.listen(4)
        s.settimeout(0.5)
        self.sock = s
        self._thread = threading.Thread(target=self._run, name='pump', daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            sock = self.sock
            if sock is None:
                return
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.sink({'kind': '_connected'})
            buf = b''
            try:
                conn.settimeout(0.5)
                while not self._stop.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        msg = parse_line(line)
                        if msg is not None:
                            self.sink(msg)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                self.sink({'kind': '_disconnected'})

    def stop(self):
        self._stop.set()
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------- 屏幕
class Screen:
    """六列（储物盒）分列显示；一切控件建一次，之后就地更新。"""

    MAX_ITEMS = 4

    def __init__(self, root, w=1024, h=600, fullscreen=True,
                 preview_host='127.0.0.1', preview_port=5060, cam=0):
        self.root = root
        self.W, self.H = int(w), int(h)
        self.preview_host = preview_host
        self.preview_port = int(preview_port or 0)
        self.cam = int(cam)
        self.log = bool(os.environ.get('DISPLAY_LOG'))

        self.state = 'idle'
        self.elapsed = 0.0
        self.done = 0
        self.total = 0
        self.alerts = []
        self.online = False
        self.last_event = ''
        self.current_box = 0
        self.current_label = ''
        self.latest = None
        self.boxes = {i: {'label': '', 'color': '', 'items': []} for i in range(1, 7)}

        self._tick_anchor = None
        self._dirty = True
        self._photo_seq = 0
        self._img_refs = {}
        self._card_slots = {}
        self._last = {}
        self._stop = False

        root.title('智能分拣 · 显示')
        root.configure(bg=BG)
        root.resizable(False, False)
        if fullscreen:
            root.attributes('-fullscreen', True)
        else:
            root.geometry('%dx%d+40+40' % (self.W, self.H))
        # 以**实际窗口尺寸**为准：Windows 有 DPI 缩放时 geometry 1024x600 的
        # 客户区可能只有 682x400；派上是 X11 无缩放，两者都会落到真实像素数。
        try:
            root.update_idletasks()
            root.update()
            aw, ah = int(root.winfo_width()), int(root.winfo_height())
            if aw > 50 and ah > 50:
                self.W, self.H = aw, ah
        except Exception:                               # noqa: BLE001
            pass
        self.sx = self.W / 1024.0
        self.sy = self.H / 600.0
        self._build()
        self._tick()
        self._clock()

    # ---------- 几何/字体 ----------
    def x(self, px):
        return int(round(px * self.sx))

    def y(self, px):
        return int(round(px * self.sy))

    def f(self, px):
        return max(8, int(round(px * min(self.sx, self.sy))))

    def lab(self, parent, text='', fg=None, bg=None, size=20, bold=True,
            mono=False, anchor='w'):
        # 字号用**负值=像素**，避免 Windows/Pi 的 DPI 缩放把字放大到溢出
        return tk.Label(parent, text=text, fg=fg or FG, bg=bg or PANEL,
                        font=(MONO if mono else UI_FONT, -self.f(size),
                              'bold' if bold else 'normal'),
                        anchor=anchor)

    # ---------- 建界面（只跑一次） ----------
    def _build(self):
        W, H = self.W, self.H
        # 顶栏
        top = tk.Frame(self.root, bg=BG)
        top.place(x=0, y=0, width=W, height=self.y(56))
        cv = tk.Canvas(top, width=self.x(16), height=self.y(16), bg=BG,
                       highlightthickness=0)
        cv.place(x=self.x(12), y=self.y(17))
        cv.create_oval(2, 2, self.x(14), self.y(14), fill=ACCENT, outline='')
        self.dot = cv
        self.lb_state = self.lab(top, '待机', size=22, bg=BG)
        self.lb_state.place(x=self.x(34), y=self.y(12))
        self.lb_cur = self.lab(top, '等待开始…', fg=DIM, size=19, bg=BG)
        self.lb_cur.place(x=self.x(150), y=self.y(15), width=self.x(380))
        self.lb_time = self.lab(top, '00:00', size=28, mono=True, bg=BG, anchor='e')
        self.lb_time.place(x=W - self.x(600), y=self.y(8), width=self.x(110))
        self.lb_prog = self.lab(top, '完成 0/0', size=21, bg=BG, anchor='e')
        self.lb_prog.place(x=W - self.x(480), y=self.y(13), width=self.x(190))
        self.lb_link = self.lab(top, '● 未连接', fg=WARN, size=18, bg=BG, anchor='e')
        self.lb_link.place(x=W - self.x(140), y=self.y(16), width=self.x(130))

        # 六列
        gap = self.x(8)
        margin = self.x(12)
        colw = (W - margin * 2 - gap * 5) // 6
        top_y = self.y(60)
        colh = self.y(472) - top_y
        self.lb_col = {}
        for i in range(1, 7):
            x0 = margin + (i - 1) * (colw + gap)
            frame = tk.Frame(self.root, bg=PANEL2, highlightthickness=1,
                             highlightbackground=LINE)
            frame.place(x=x0, y=top_y, width=colw, height=colh)
            no = self.lab(frame, '%d 号盒' % i, fg=DIM, size=16)
            no.place(x=self.x(8), y=self.y(8))
            cnt = self.lab(frame, '0 件', fg=DIM, size=16, anchor='e')
            cnt.place(x=colw - self.x(64), y=self.y(8), width=self.x(56))
            name = self.lab(frame, '', fg=FG, size=18)
            name.place(x=self.x(8), y=self.y(30))
            body = tk.Frame(frame, bg=PANEL2)
            body.place(x=0, y=self.y(57), width=colw, height=colh - self.y(57))
            hint = self.lab(body, '待放入', fg=DIM, size=17)
            hint.place(x=self.x(10), y=self.y(22), width=colw - self.x(20),
                       height=self.y(40))
            self._card_slots[i] = []
            for k in range(self.MAX_ITEMS):
                slot = tk.Frame(body, bg=PANEL, highlightthickness=0)
                thumb_side = self.f(52)
                img = tk.Label(slot, bg=PANEL, bd=0)
                img.place(x=self.x(6), y=self.y(10), width=thumb_side,
                          height=thumb_side)
                seq = self.lab(slot, '', size=17, mono=True)
                seq.place(x=self.x(6) + thumb_side + self.x(8), y=self.y(8))
                nm = self.lab(slot, '', fg=DIM, size=15)
                nm.place(x=self.x(6) + thumb_side + self.x(8), y=self.y(34))
                self._card_slots[i].append({'frame': slot, 'img': img,
                                            'seq': seq, 'name': nm,
                                            'key': None, 'side': thumb_side})
            self.lb_col[i] = {'frame': frame, 'no': no, 'cnt': cnt,
                              'name': name, 'hint': hint, 'w': colw}

        # 底栏
        bot = tk.Frame(self.root, bg=BG)
        bot.place(x=0, y=self.y(474), width=W, height=self.y(72))
        self.big = tk.Label(bot, bg=BG, bd=0)
        self.big.place(x=self.x(12), y=self.y(6), width=self.f(56), height=self.f(56))
        self.lb_seq = self.lab(bot, '—', size=26, mono=True, bg=BG)
        self.lb_seq.place(x=self.x(80), y=self.y(8))
        self.lb_name = self.lab(bot, '等待第一件…', size=22, bg=BG)
        self.lb_name.place(x=self.x(170), y=self.y(12), width=self.x(520))
        self.lb_event = self.lab(bot, '', fg=DIM, size=17, bg=BG)
        self.lb_event.place(x=W - self.x(420), y=self.y(18), width=self.x(270),
                            anchor='w')
        self.lb_alert = self.lab(bot, '', fg=BAD, size=17, bg=BG, anchor='e')
        self.lb_alert.place(x=W - self.x(140), y=self.y(18), width=self.x(128))

    # ---------- 消息处理（只改模型 + 标脏，不碰控件） ----------
    def apply(self, msg):
        if not isinstance(msg, dict):
            return
        kind = msg.get('kind')
        if self.log:
            print('[display] %s' % json.dumps(msg, ensure_ascii=False)[:220])
        if kind is None and 'state' in msg:           # 兼容旧格式
            if msg.get('item'):
                name = str(msg.get('name') or '')
                shape, _sep, color = name.partition('|')
                msg = {'kind': 'item', 'seq': msg.get('item'), 'name': shape or name,
                       'color': color, 'box': msg.get('box'),
                       'total_done': msg.get('placed'),
                       'last_event': msg.get('last_event')}
            else:
                msg = {'kind': 'state', 'state': 'running',
                       'done': msg.get('placed'), 'total': msg.get('total')}
            kind = msg['kind']

        if kind == '_connected':
            self.online = True
            self._dirty = True
            return
        if kind == '_disconnected':
            self.online = False
            self._dirty = True
            return
        if kind == 'state':
            self.state = str(msg.get('state') or self.state)
            if msg.get('elapsed_s') is not None:
                try:
                    self.elapsed = max(0.0, float(msg['elapsed_s']))
                    self._tick_anchor = (time.monotonic(), self.elapsed)
                except (TypeError, ValueError):
                    pass
            if msg.get('done') is not None:
                self.done = int(msg.get('done') or 0)
            if msg.get('total') is not None:
                self.total = int(msg.get('total') or 0)
            if msg.get('alerts') is not None:
                self.alerts = list(msg.get('alerts') or [])[-2:]
            if msg.get('connected') is not None:
                self.online = bool(msg.get('connected'))
            if msg.get('last_event'):
                self.last_event = str(msg['last_event'])
            if msg.get('current_box') is not None:
                self.current_box = int(msg.get('current_box') or 0)
            if msg.get('current_label'):
                self.current_label = str(msg['current_label'])
            self._dirty = True
            return
        if kind == 'item':
            seq = int(msg.get('seq') or 0)
            name = str(msg.get('name') or '')
            color = str(msg.get('color') or '')
            box = int(msg.get('box') or 0)
            cls_label = ('%s %s' % (name, color)).strip()
            label = cls_label or str(msg.get('box_label') or '')
            item = {'seq': seq, 'name': name, 'color': color,
                    'thumb': self._thumb_from_b64(msg.get('thumb_b64')),
                    'key': '%d/%d' % (box, seq)}
            if 1 <= box <= 6:
                slot = self.boxes[box]
                if label:
                    slot['label'] = label
                if color:
                    slot['color'] = color
                items = slot['items']
                if not any(it.get('seq') == seq for it in items):
                    items = (items + [item])[-self.MAX_ITEMS:]
                else:
                    for it in items:
                        if it.get('seq') == seq:
                            it.update(item)
                slot['items'] = items
            self.latest = {'seq': seq, 'name': label or name, 'box': box,
                           'thumb': item['thumb'], 'color': color}
            if msg.get('total_done') is not None:
                self.done = int(msg.get('total_done') or 0)
            self.last_event = str(msg.get('last_event') or
                                  ('第 %d 件 → %d 号盒' % (seq, box)))
            self._dirty = True
            self._fetch_photo_later(item, msg)
            return
        if kind == 'item_done':
            if msg.get('total_done') is not None:
                self.done = int(msg.get('total_done') or 0)
            ok = bool(msg.get('ok', True))
            seq = msg.get('seq')
            for i in range(1, 7):
                for it in self.boxes[i]['items']:
                    if it.get('seq') == seq:
                        it['ok'] = ok
            if msg.get('last_event'):
                self.last_event = str(msg['last_event'])
            self._dirty = True
            return
        if kind == 'alarm':
            text = str(msg.get('text') or msg.get('message') or '')
            if text:
                self.alerts = (self.alerts + [text])[-2:]
            self._dirty = True

    def _thumb_from_b64(self, b64):
        if not b64:
            return None
        try:
            return tk.PhotoImage(data=base64.b64decode(b64))
        except Exception:                            # noqa: BLE001
            return None

    # ---------- 实时照片（后台线程取帧，主线程上图） ----------
    def _fetch_photo_later(self, item, msg):
        if not self.preview_port:
            return
        bbox = msg.get('bbox')
        if not bbox or len(bbox) != 4:
            bbox = [0.30, 0.30, 0.40, 0.40]      # 上位机没带 bbox 时裁托盘中心，仍给实时照片
        cam = int(msg.get('cam') or self.cam)
        box = None
        for i in range(1, 7):
            if any(it is item for it in self.boxes[i]['items']):
                box = i
                break
        key = item.get('key') or ''

        def work():
            jpeg = fetch_frame(self.preview_host, self.preview_port, cam)
            print('[photo] seq=%s box=%s bbox=%s jpeg=%s' % (item.get('seq'), box, bbox, len(jpeg or b'')))
            if not jpeg:
                return
            png_b64 = crop_square(jpeg, bbox, cam)
            if not png_b64:
                return
            self.root.after(0, lambda: self._apply_photo(key, png_b64))

        threading.Thread(target=work, name='photo', daemon=True).start()

    def _apply_photo(self, key, png_b64):
        try:
            photo = tk.PhotoImage(data=base64.b64decode(png_b64))
        except Exception:                            # noqa: BLE001
            return
        self._img_refs[key] = photo
        for i in range(1, 7):
            for it in self.boxes[i]['items']:
                if it.get('key') == key:
                    it['thumb'] = photo
        if self.latest and self.latest.get('seq') is not None:
            if key.endswith('/%d' % int(self.latest['seq'])):
                self.latest['thumb'] = photo
        self._dirty = True

    # ---------- 刷新（就地） ----------
    def _set(self, label, text, color=None):
        if label is None:
            return
        if len(self._last) > 400:
            self._last.clear()
        key = str(label)
        if self._last.get(key) != text:
            try:
                label.configure(text=text)
            except tk.TclError:
                return
            self._last[key] = text
        if color is not None:
            self._last.pop(key + '#c', None)
            if self._last.get(key + '#c') != color:
                try:
                    label.configure(fg=color)
                except tk.TclError:
                    return
                self._last[key + '#c'] = color

    def _cur_text(self):
        if self.current_label:
            if '号盒' in self.current_label:
                return '当前：%s' % self.current_label
            if self.current_box:
                return '当前：%s → %d 号盒' % (self.current_label, self.current_box)
            return '当前：%s' % self.current_label
        if self.current_box:
            return '当前：%d 号盒' % self.current_box
        return '当前：—'

    def _flush(self):
        st = self.state if self.state in STATE_TEXT else 'idle'
        self._set(self.lb_state, STATE_TEXT[st], STATE_COLOR[st])
        try:
            self.dot.itemconfigure(1, fill=STATE_COLOR[st])
        except tk.TclError:
            pass
        self._set(self.lb_cur, self._cur_text() if self.current_box or self.current_label
                  else (self.last_event or '等待开始…'), DIM if not self.current_box else FG)
        self._set(self.lb_time, '%02d:%02d' % (int(self.elapsed) // 60,
                                               int(self.elapsed) % 60))
        self._set(self.lb_prog, '完成 %d/%d' % (self.done, self.total))
        self._set(self.lb_link, '● 已连接' if self.online else '● 未连接',
                  ACCENT if self.online else WARN)

        for i in range(1, 7):
            hd = self.lb_col[i]
            slot = self.boxes[i]
            items = slot['items']
            active = (hd.get('frame').cget('highlightbackground') == ACCENT)
            try:
                if items and not active:
                    hd['frame'].configure(highlightbackground=ACCENT, highlightthickness=2)
                elif not items and active:
                    hd['frame'].configure(highlightbackground=LINE, highlightthickness=1)
            except tk.TclError:
                pass
            label = slot['label'] or ''
            self._set(hd['name'], label or '待放入',
                      color_hex(slot['color']) if slot['color'] else DIM)
            self._set(hd['cnt'], '%d 件' % len(items), DIM)
            if items:
                try:
                    hd['hint'].place_forget()
                except tk.TclError:
                    pass
            else:
                try:
                    hd['hint'].place(x=self.x(10), y=self.y(22),
                                     width=hd['w'] - self.x(20), height=self.y(40))
                except tk.TclError:
                    pass
            slots = self._card_slots[i]
            for k in range(self.MAX_ITEMS):
                cs = slots[k]
                it = items[len(items) - 1 - k] if k < len(items) else None
                if it is None:
                    if cs['key'] is not None:
                        try:
                            cs['frame'].place_forget()
                        except tk.TclError:
                            pass
                        cs['key'] = None
                    continue
                try:
                    cs['frame'].place(x=self.x(8), y=self.y(6) + k * self.y(94),
                                      width=hd['w'] - self.x(16), height=self.y(84))
                except tk.TclError:
                    pass
                if cs['key'] != it.get('key'):
                    cs['key'] = it.get('key')
                    thumb = it.get('thumb')
                    if thumb is None:
                        thumb = self._solid_thumb(color_hex(it.get('color')), cs['side'])
                    try:
                        cs['img'].configure(image=thumb, bg=PANEL)
                        cs['img'].image = thumb
                    except tk.TclError:
                        pass
                    self._set(cs['seq'], '#%d' % int(it.get('seq') or 0), FG)
                    self._set(cs['name'], str(it.get('name') or ''), DIM)

        latest = self.latest or {}
        if latest:
            self._set(self.lb_seq, '#%d' % int(latest.get('seq') or 0), FG)
            self._set(self.lb_name, str(latest.get('name') or ''), FG)
            thumb = latest.get('thumb')
            if thumb is not None:
                try:
                    self.big.configure(image=thumb)
                    self.big.image = thumb
                except tk.TclError:
                    pass
        self._set(self.lb_event, self.last_event or '', DIM)
        self._set(self.lb_alert, ('⚠ ' + self.alerts[-1]) if self.alerts else '',
                  BAD)

    def _solid_thumb(self, color, size):
        try:
            photo = tk.PhotoImage(width=max(8, size), height=max(8, size))
            photo.put(color, to=(0, 0, max(8, size), max(8, size)))
            return photo
        except tk.TclError:
            return None

    # ---------- 定时器 ----------
    def _tick(self):
        if self._dirty:
            self._dirty = False
            try:
                self._flush()
            except tk.TclError:
                return
        if not self._stop:
            self.root.after(40, self._tick)

    def _clock(self):
        if self._tick_anchor is not None:
            base, val = self._tick_anchor
            self.elapsed = val + (time.monotonic() - base)
        if self.state in ('running', 'paused', 'done'):
            self._set(self.lb_time, '%02d:%02d' % (int(self.elapsed) // 60,
                                                   int(self.elapsed) % 60))
        if not self._stop:
            self.root.after(200, self._clock)

    def stop(self):
        self._stop = True


def demo(root, scr):
    """假数据：12 件按 6 类入盒，每 1.6 s 一件。"""
    classes = [('正方体', '红'), ('圆锥', '蓝'), ('正五棱柱', '黄'),
               ('长方体', '绿'), ('正六棱柱', '紫'), ('圆柱', '橙')]
    scr.apply({'kind': '_connected'})
    scr.apply({'kind': 'state', 'state': 'running', 'total': 12,
               'last_event': '演示数据（未接真实流程）'})
    state = {'n': 0}
    t0 = time.time()

    def tick():
        state['n'] += 1
        n = state['n']
        name, color = classes[(n - 1) % 6]
        label = '%s %s' % (name, color)
        scr.apply({'kind': 'item', 'seq': n, 'name': name, 'color': color,
                   'box': (n - 1) % 6 + 1, 'box_label': label, 'total_done': n})
        scr.apply({'kind': 'item_done', 'seq': n, 'ok': True, 'total_done': n})
        scr.apply({'kind': 'state', 'state': 'running', 'total': 12, 'done': n,
                   'elapsed_s': time.time() - t0})
        if n >= 12:
            scr.apply({'kind': 'state', 'state': 'done', 'done': 12, 'total': 12,
                       'elapsed_s': time.time() - t0,
                       'last_event': '分拣完成：共 12 件'})
            return
        root.after(1600, tick)

    root.after(700, tick)


def main():
    ap = argparse.ArgumentParser(description='派端比赛显示（只显示）')
    ap.add_argument('--port', type=int, default=5059)
    ap.add_argument('--screen', default='1024x600')
    ap.add_argument('--windowed', action='store_true')
    ap.add_argument('--fake', action='store_true', help='假数据演示')
    ap.add_argument('--theme', default='light',
                    choices=('dark', 'light', 'blue'), help='配色主题')
    ap.add_argument('--preview-host', default='127.0.0.1',
                    help='实时照片来源（本机 pi_agent 只读预览口）')
    ap.add_argument('--preview-port', type=int, default=5060,
                    help='只读预览口端口（0=关闭实时照片）')
    ap.add_argument('--cam', type=int, default=0, help='实时照片取哪路相机')
    args = ap.parse_args()
    set_theme(args.theme)
    w, h = (int(v) for v in str(args.screen).lower().split('x'))
    try:                                              # Windows: 物理像素
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                 # noqa: BLE001
        pass
    root = tk.Tk()
    scr = Screen(root, w, h, fullscreen=not args.windowed,
                 preview_host=args.preview_host, preview_port=args.preview_port,
                 cam=args.cam)
    q = queue.Queue()
    pump = None
    if not args.fake:
        pump = Pump(args.port, q.put)
        try:
            pump.start()
        except OSError as exc:
            scr.apply({'kind': 'alarm', 'text': '端口 %d 打不开：%s' % (args.port, exc)})

    def drain():
        try:
            while True:
                scr.apply(q.get_nowait())
        except queue.Empty:
            pass
        root.after(60, drain)

    root.after(60, drain)
    if args.fake:
        demo(root, scr)
    try:
        root.mainloop()
    finally:
        scr.stop()
        if pump:
            pump.stop()


if __name__ == '__main__':
    main()
