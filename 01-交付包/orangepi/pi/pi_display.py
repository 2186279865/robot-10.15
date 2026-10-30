"""派端比赛显示屏 v8（智能分拣赛项）——六个储物盒分列显示。

与 v7 的区别（见 docs/派端屏改造说明.md）：
  1) **就地刷新**：所有控件只在启动时创建一次（`_build`），之后只改文本/图片/位置，
     不再 `destroy()` 重建 → 屏幕不闪。
  2) **照片用预设图**：货物图显示 `presets/` 下按「形状|颜色」预生成的 52/56/96/160 四档图（按屏上实际像素取用）
     （现场 2026-10-31 要求：稳定、不随光照变化）。旧版是收到 item 时从 5060 预览口
     抓实时帧按 bbox 裁剪——已默认关闭，要恢复就带 `PI_DISPLAY_LIVE_PHOTO=1` 启动。
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
# 现场要求（2026-10-31）：屏上的货物图一律用 `presets/` 里的预设照片，
# **不再实时截取相机画面**（实时截图每件都要多拉一次帧、样子还随光照变）。
# 需要恢复"抓拍放入瞬间"的老行为时，带环境变量启动即可：
#     PI_DISPLAY_LIVE_PHOTO=1 python3 pi_display.py
_USE_LIVE_PHOTO = os.environ.get('PI_DISPLAY_LIVE_PHOTO', '').strip().lower() in (
    '1', 'true', 'yes', 'on')

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
            rest = _recv_exact(sock, 10)              # magic2+cam2+w2+h2+seq4 = 12 字节
            _m, _cam, _w, _h, _seq = struct.unpack('<2sHHHI', head + rest)
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


# 卡片取景：1.0 = 严格截取「运行台画的那个识别框」；>1 会带一点上下文
CROP_PAD = 1.0


def crop_square(jpeg, bbox=None, cam=0):
    """按 bbox（归一化 [x,y,w,h]）严格裁出这块画面，保持宽高比补成正方形后返回 PNG base64。"""
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

    bx, by, bw, bh = 0.37 * w, 0.37 * h, 0.26 * w, 0.26 * h   # 没框时的兜底（画面中心）
    if bbox and len(bbox) == 4:
        try:
            x0, y0, bw_, bh_ = (float(v) for v in bbox)
            if max(abs(x0), abs(y0), abs(bw_), abs(bh_)) > 1.5:   # 兼容像素框
                x0, y0, bw_, bh_ = x0 / w, y0 / h, bw_ / w, bh_ / h
            bw_, bh_ = max(1e-3, bw_), max(1e-3, bh_)
            bx, by, bw, bh = x0 * w, y0 * h, bw_ * w, bh_ * h
        except (TypeError, ValueError):
            pass

    if CROP_PAD != 1.0:                              # 需要留边时按中心外扩
        cx, cy = bx + bw / 2.0, by + bh / 2.0
        bw, bh = bw * CROP_PAD, bh * CROP_PAD
        bx, by = cx - bw / 2.0, cy - bh / 2.0

    x1, y1 = int(round(max(0, bx))), int(round(max(0, by)))
    x2 = int(round(min(w, bx + bw))); y2 = int(round(min(h, by + bh)))
    if x2 - x1 < 16 or y2 - y1 < 16:
        return None
    crop = img[y1:y2, x1:x2]

    # 保持宽高比：补成正方形（补边用托盘色灰，避免黑边太突），再缩到 96×96
    side = max(crop.shape[1], crop.shape[0])
    canvas = np.full((side, side, 3), 150, dtype=np.uint8)
    ox = (side - crop.shape[1]) // 2
    oy = (side - crop.shape[0]) // 2
    canvas[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
    try:
        out = cv2.resize(canvas, (96, 96), interpolation=cv2.INTER_AREA)
        ok, enc = cv2.imencode('.png', out)           # imencode 要 BGR，保持原样
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
# ---------------- 预设示意图片（图片/presets/ 缩放后的 96/160 版） ----------------
_PRESET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'presets')
_preset_index = None
_preset_cache = {}


def _load_preset_index():
    """class_key（形如 '长方体|蓝'）→ 预设图片基名。"""
    global _preset_index
    if _preset_index is not None:
        return _preset_index
    idx = {}
    try:
        with open(os.path.join(_PRESET_DIR, 'index.json'), encoding='utf-8') as f:
            idx = dict(json.load(f) or {})
    except Exception:                                   # noqa: BLE001
        idx = {}
    _preset_index = idx
    return idx


def _shape_family(name):
    """形状 → 预设文件名里的族关键字（cone / pyramid / cylinder / cuboid）。"""
    if '圆锥' in name:
        return 'cone'
    if '棱锥' in name or '棱' in name:
        return 'pyramid'
    if '柱' in name:
        return 'cylinder'
    if '长方' in name or '正方' in name or '菱' in name or '矩形' in name:
        return 'cuboid'
    return ''


def _preset_sizes():
    """扫一遍 presets/ 里有哪些 `<基名>_<边长>.png`，返回 {基名: [边长...]}。"""
    out = {}
    try:
        for n in os.listdir(_PRESET_DIR):
            low = n.lower()
            if not low.endswith('.png') or '_' not in n:
                continue
            base, _, tail = n[:-4].rpartition('_')
            if not tail.isdigit():
                continue
            out.setdefault(base, []).append(int(tail))
    except OSError:
        return {}
    for base in out:
        out[base].sort()
    return out


def _pick_preset_size(sizes, want):
    """挑最接近 want 的已有尺寸；优先 >= want（宁大不小，裁掉几像素比糊好）。"""
    if not sizes:
        return None
    up = [s for s in sizes if s >= want]
    if up:
        return up[0]
    return sizes[-1]


def preset_photo(name, color, size=96):
    """按「形状|颜色」取预设图；取不到返回 None。同一（图, 尺寸）只加载一次。

    查找顺序（现场实测的类别名有「椭圆柱|红」「长方体|紫」这类只差颜色的写法，
    所以必须能退到"形状对得上"）：
      1) 精确 key「形状|颜色」；
      2) 同颜色 + 同形状族（长方体/正方体/菱形 都算长方体）；
      3) 只按形状族（颜色不同也认，屏上同时有大字颜色，不会误认）。

    尺寸：屏是 1024×600，列内缩略图只有 f(52) px、底栏大图 f(56) px。预设图按
    52/56/96/160 四档生成；want 不在档里时取"相近且不小于"的那档，再用
    `PhotoImage.subsample(k)` 整数倍缩小（Tk 只支持整数倍，余下几像素由 Label 裁）。
    这样做的好处：换屏/窗口化导致尺寸变化也不会把图缩成"一角"。
    """
    idx = _load_preset_index()
    if not idx:
        return None
    name = (name or '').strip()
    color = (color or '').strip()
    base = idx.get('%s|%s' % (name, color))
    fam = _shape_family(name)
    if base is None and color and fam:
        for k, v in idx.items():
            if k.endswith('|' + color) and fam in v:
                base = v
                break
    if base is None and fam:
        # 最后一次机会：同形状族（不管颜色）
        for k, v in idx.items():
            if fam in v:
                base = v
                break
    if not base:
        return None
    want = max(8, int(size))
    avail = _preset_sizes().get(base) or []
    pick = _pick_preset_size(avail, want) or want
    subsample = 1
    if pick > want and want > 0:
        subsample = max(1, int(round(float(pick) / float(want))))
    key = (base, pick, subsample)
    if key in _preset_cache:
        return _preset_cache[key]
    try:
        photo = tk.PhotoImage(file=os.path.join(_PRESET_DIR, '%s_%d.png' % (base, int(pick))))
        if subsample > 1:
            photo = photo.subsample(subsample, subsample)
    except Exception:                                   # noqa: BLE001
        photo = None
    _preset_cache[key] = photo
    return photo


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
        self.finished = False       # 本轮抓取是否已结束（结束时显示“总用时”）
        self.total_s = 0.0
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
                                            'key': None, 'thumb': None,
                                            'side': thumb_side})
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
        self.lb_total = self.lab(bot, '', fg=ACCENT, size=18, bg=BG, anchor='e')
        self.lb_total.place(x=W - self.x(150), y=self.y(44), width=self.x(140))

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
            # 计时口径：只有 host 明确 running=True（已开始抓取）才走表；
            # 未开始/待机时每收到一次 state 都把读数钉在 0，避免本地 _tick 数出“0→4 再归零”的锯齿
            if msg.get('running') is True:
                self.finished = False
                self.total_s = 0.0
            if msg.get('finished'):
                # 任务结束：把当前读数冻结成“总用时”
                self.finished = True
                self.total_s = float(msg.get('total_s') or self.elapsed or msg.get('elapsed_s') or 0.0)
                self._tick_anchor = None
                self.elapsed = self.total_s
            if msg.get('running') is False:
                if not self.finished:
                    self.elapsed = 0.0
                self._tick_anchor = None
            elif msg.get('elapsed_s') is not None:
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
            # 现场要求：货物图用 presets/ 里的预设照片（每类一张，稳定好看），
            # 不再实时截取；只有预设里完全没有这个形状时才退回上位机推来的缩略图。
            _thumb = preset_photo(name, color, self.f(52))
            _preset_ok = _thumb is not None
            if _thumb is None:
                _thumb = self._thumb_from_b64(msg.get('thumb_b64'))
            item = {'seq': seq, 'name': name, 'color': color,
                    'thumb': _thumb,
                    'preset': _preset_ok,
                    'key': '%d/%d' % (box, seq),
                    'bbox': msg.get('bbox'), 'cam': msg.get('cam')}
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
            _big = preset_photo(name, color, self.f(56)) or item['thumb']
            self.latest = {'seq': seq, 'name': label or name, 'box': box,
                           'thumb': _big, 'color': color}
            if msg.get('total_done') is not None:
                self.done = int(msg.get('total_done') or 0)
            self.last_event = str(msg.get('last_event') or
                                  ('第 %d 件 → %d 号盒' % (seq, box)))
            self._dirty = True
            if _USE_LIVE_PHOTO:
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
    # 只在 PI_DISPLAY_LIVE_PHOTO=1 时才会被调用；默认走 presets/ 预设图。
    def _fetch_photo_later(self, item, msg):
        if not self.preview_port:
            return
        bbox = msg.get('bbox')
        if not bbox or len(bbox) != 4:
            # 没有目标框就不能裁：盲裁中心会让每一件都长一个样（用户踩过），
            # 直接保留上位机推来的这一件的缩略图（thumb_b64）即可。
            print('[photo] seq=%s skip(no bbox) thumb=%s'
                  % (item.get('seq'), bool(item.get('thumb'))))
            if not item.get('thumb'):
                # 既没有框也没有缩略图 → 只可能是上位机是旧版；在底栏给出明确提示
                self.alerts = (self.alerts + ['派端屏没收到目标框（上位机需重启）'])[-2:]
            return
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
                    cs['thumb'] = None                   # 换了一件：强制重新上图
                    self._set(cs['seq'], '#%d' % int(it.get('seq') or 0), FG)
                    self._set(cs['name'], str(it.get('name') or ''), DIM)
                # 照片是异步取回的：即使 key 没变，只要 thumb 换了也要重画
                thumb = it.get('thumb')
                if thumb is None and cs.get('thumb') is None:
                    thumb = self._solid_thumb(color_hex(it.get('color')), cs['side'])
                if thumb is not None and cs.get('thumb') is not thumb:
                    cs['thumb'] = thumb
                    try:
                        cs['img'].configure(image=thumb, bg=PANEL)
                        cs['img'].image = thumb
                    except tk.TclError:
                        pass

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
        # 任务结束后显示“总用时”，新一轮开始自动清掉
        self._set(self.lb_total,
                  ('总用时 %02d:%02d' % (int(self.total_s) // 60, int(self.total_s) % 60))
                  if self.finished else '', ACCENT)

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
