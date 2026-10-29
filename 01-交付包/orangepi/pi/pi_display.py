"""派端比赛显示屏 v7（智能分拣赛项）——六个储物盒分列显示。

赛题要求（原文，02-资料库/01-赛题/newpdftext.txt）：
  1) 分拣装置上需安装一块仅具有显示功能的高亮显示屏，支持各种格式图片播放，
     显示分拣信息与数据（序号、图片及相关信息）；
  2) 每分拣一件并入盒后，屏上须显示：序号、货物名称、图片、分拣成功总数量；
  3) 超过 15 s 没有动作或没有显示本次分拣信息 → 本轮结束（屏必须一直更新）。

版式（1024×600）：
  ┌ 顶栏 56px：● 状态 / 当前类别 → 盒号          计时  7/12  ● 已连接 ┐
  │ 六列（每列 ~159px）：盒号 · ●类别 · N 件                          │
  │   列内自上而下堆物品卡（缩略图 + #序号），最多 4 张                 │
  ├ 底栏 72px：最新一件（#序号 + 名称）              ⚠ 报警            ┘

用法：
    python3 pi_display.py                  # 全屏，监听 5059
    python3 pi_display.py --windowed       # 窗口（调试/截图）
    python3 pi_display.py --fake           # 假数据演示
    python3 pi_display.py --port 5059 --screen 1024x600

协议（上位机→派，NDJSON 每行一条；字段可缺省）：
    {"kind":"state","state":"running","elapsed_s":123.4,"done":7,"total":12,
     "alerts":[],"connected":true,"last_event":"...","current_box":3,
     "current_label":"正五棱柱 黄"}
    {"kind":"item","seq":7,"name":"正五棱柱","color":"黄","box":3,
     "box_label":"正五棱柱 黄","thumb_b64":"<PNG base64>","total_done":7}
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


class Pump:
    """TCP 行服务器（5059）：收到 JSON 塞队列，坏行静默丢弃，断线不清屏。"""

    def __init__(self, port, sink):
        self.port = int(port)
        self.sink = sink
        self.sock = None
        self._stop = threading.Event()

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', self.port))
        self.sock.listen(4)
        self.sock.settimeout(0.5)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.sink({'kind': '_connected'})
            with conn:
                conn.settimeout(1.0)
                buf = b''
                while not self._stop.is_set():
                    try:
                        chunk = conn.recv(65536)
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
                        if msg:
                            self.sink(msg)
            self.sink({'kind': '_disconnected'})

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass


class Screen:
    def __init__(self, root, w=1024, h=600, fullscreen=True):
        self.root = root
        self.W, self.H = int(w), int(h)
        self.state = 'idle'
        self.elapsed = 0.0
        self.done = 0
        self.total = 0
        self.alerts = []
        self.online = False
        self.last_event = '等待开始…'
        self.current_box = None
        self.current_label = ''
        self.latest = None
        self.boxes = {i: {'label': '', 'color': '', 'items': []} for i in range(1, 7)}
        self.log = bool(os.environ.get('DISPLAY_LOG'))
        self._cards = []

        root.title('智能分拣 · 显示')
        root.configure(bg=BG)
        root.resizable(False, False)
        if fullscreen:
            root.attributes('-fullscreen', True)
        else:
            root.geometry('%dx%d+40+40' % (self.W, self.H))
        root.update_idletasks()
        self.sx = self.W / 1024.0
        self.sy = self.H / 600.0
        self._build()

    # ---------------------------------------------------------- 几何/字体
    def x(self, px):
        return int(round(px * self.sx))

    def y(self, px):
        return int(round(px * self.sy))

    def f(self, px):
        return max(8, int(round(px * min(self.sx, self.sy))))

    def lab(self, parent, text='', fg=None, bg=None, size=20, bold=True,
            mono=False, anchor='w'):
        # 默认色在调用时取模块全局（否则会被定义时的旧主题色绑死）
        return tk.Label(parent, text=text,
                        bg=(BG if bg is None else bg),
                        fg=(FG if fg is None else fg),
                        font=((MONO if mono else UI_FONT), -self.f(size),
                              'bold' if bold else 'normal'),
                        anchor=anchor)

    # ---------------------------------------------------------- 布局
    def _build(self):
        W, H = self.W, self.H
        self.top = tk.Frame(self.root, bg=PANEL)
        self.top.place(x=0, y=0, width=W, height=self.y(56))
        self.dot = tk.Canvas(self.top, width=self.f(14), height=self.f(14),
                             bg=PANEL, highlightthickness=0)
        self.dot.place(x=self.x(18), y=self.y(20))
        self.dot.create_oval(1, 1, self.f(13), self.f(13), fill=ACCENT, outline='')
        self.lb_state = self.lab(self.top, '待机', size=22, bg=PANEL)
        self.lb_state.place(x=self.x(40), y=self.y(14))
        self.lb_cur = self.lab(self.top, '', fg=DIM, size=19, bold=False, bg=PANEL)
        self.lb_cur.place(x=self.x(148), y=self.y(16))
        self.lb_time = self.lab(self.top, '00:00', size=28, mono=True, anchor='e', bg=PANEL)
        self.lb_time.place(x=W - self.x(492), y=self.y(12), width=self.x(150))
        self.lb_prog = self.lab(self.top, '已完成 0 / 0', size=22, anchor='e', bg=PANEL)
        self.lb_prog.place(x=W - self.x(336), y=self.y(15), width=self.x(184))

        self.lb_link = self.lab(self.top, '● 未连接', fg=WARN, size=18, bold=False,
                                anchor='e', bg=PANEL)
        self.lb_link.place(x=W - self.x(146), y=self.y(16), width=self.x(130))

        gap = self.x(8)
        margin = self.x(12)
        colw = (W - margin * 2 - gap * 5) // 6
        top_y = self.y(60)
        colh = self.y(472) - top_y
        self.cols = {}
        for i in range(1, 7):
            x0 = margin + (i - 1) * (colw + gap)
            frame = tk.Frame(self.root, bg=PANEL2, highlightbackground=LINE,
                             highlightthickness=1)
            frame.place(x=x0, y=top_y, width=colw, height=colh)
            hd = tk.Frame(frame, bg=PANEL)
            hd.place(x=1, y=1, width=colw - 2, height=self.y(56))
            no = self.lab(hd, '%d 号盒' % i, size=16, bold=False, fg=DIM, bg=PANEL)
            no.place(x=self.x(8), y=self.y(5))
            cnt = self.lab(hd, '0 件', size=16, bold=False, fg=DIM, anchor='e', bg=PANEL)
            cnt.place(x=self.x(4), y=self.y(5), width=colw - self.x(14))
            name = self.lab(hd, '待放入', size=18, fg=DIM, bg=PANEL)
            name.place(x=self.x(8), y=self.y(27))
            body = tk.Frame(frame, bg=PANEL2)
            body.place(x=1, y=self.y(57), width=colw - 2,
                       height=colh - self.y(57) - 1)
            self.cols[i] = {'frame': frame, 'no': no, 'cnt': cnt, 'name': name,
                            'body': body, 'w': colw}

        self.bottom = tk.Frame(self.root, bg=PANEL)
        self.bottom.place(x=0, y=H - self.y(72), width=W, height=self.y(72))
        self.lb_seq = self.lab(self.bottom, '', size=26, mono=True, bg=PANEL)
        self.lb_seq.place(x=self.x(16), y=self.y(18))
        self.lb_name = self.lab(self.bottom, '等待第一件…', size=22, bg=PANEL)
        self.lb_name.place(x=self.x(96), y=self.y(20))
        self.lb_event = self.lab(self.bottom, '', fg=DIM, size=17, bold=False, bg=PANEL)
        self.lb_event.place(x=self.x(470), y=self.y(24))
        self.lb_alert = self.lab(self.bottom, '', fg=BAD, size=18, anchor='e', bg=PANEL)
        self.lb_alert.place(x=W - self.x(340), y=self.y(22), width=self.x(324))
        self.redraw()

    # ---------------------------------------------------------- 数据
    def apply(self, msg):
        if self.log:
            print('[disp] ' + json.dumps(msg, ensure_ascii=False), flush=True)
        kind = msg.get('kind')
        if kind is None and 'state' in msg:            # 兼容旧格式
            if msg.get('item'):
                nm = str(msg.get('name') or '')
                name, _, color = nm.partition('|')
                msg = {'kind': 'item', 'seq': int(msg.get('item') or 0),
                       'name': name or nm, 'color': color,
                       'box': int(msg.get('box') or 0),
                       'total_done': int(msg.get('placed') or 0),
                       'last_event': msg.get('message')}
                kind = 'item'
            else:
                msg = {'kind': 'state', 'state': 'running',
                       'done': int(msg.get('placed') or 0),
                       'total': int(msg.get('total') or 0)}
                kind = 'state'
        if kind == '_connected':
            self.online = True
        elif kind == '_disconnected':
            self.online = False
        elif kind == 'state':
            self.state = msg.get('state') or self.state
            self.elapsed = float(msg.get('elapsed_s') or self.elapsed or 0)
            self.done = int(msg.get('done') or self.done)
            self.total = int(msg.get('total') or self.total or 0)
            self.alerts = list(msg.get('alerts') or [])
            if msg.get('connected') is not None:
                self.online = bool(msg.get('connected'))
            if msg.get('last_event'):
                self.last_event = str(msg['last_event'])
            if msg.get('current_box'):
                self.current_box = int(msg['current_box'])
            if msg.get('current_label'):
                self.current_label = str(msg['current_label'])
        elif kind == 'item':
            seq = int(msg.get('seq') or (self.done + 1))
            thumb = None
            b64 = msg.get('thumb_b64')
            if b64:
                try:
                    thumb = tk.PhotoImage(data=base64.b64decode(b64))
                except Exception:                     # noqa: BLE001
                    thumb = None
            name = str(msg.get('name') or '—')
            color = str(msg.get('color') or '')
            box = int(msg.get('box') or 0)
            label = str(msg.get('box_label') or (name + (' ' + color if color else '')))
            item = {'seq': seq, 'name': name, 'color': color,
                    'thumb': thumb, 'ok': True}
            if 1 <= box <= 6:
                slot = self.boxes[box]
                slot['label'] = label
                slot['color'] = color
                slot['items'] = (slot['items'] + [item])[-4:]
            self.latest = {'seq': seq, 'name': label, 'box': box,
                           'thumb': thumb, 'color': color}
            self.done = int(msg.get('total_done') or self.done)
            self.current_box = box if box else self.current_box
            self.current_label = label
            self.last_event = str(msg.get('last_event') or
                                  ('第 %d 件 → %d 号盒' % (seq, box) if box
                                   else '第 %d 件' % seq))
        elif kind == 'item_done':
            self.done = int(msg.get('total_done') or self.done)
            for slot in self.boxes.values():
                for it in slot['items']:
                    if it['seq'] == int(msg.get('seq') or -1):
                        it['ok'] = bool(msg.get('ok', True))
        elif kind == 'alarm':
            self.alerts.append(str(msg.get('text') or '报警'))
            self.alerts = self.alerts[-2:]
        self.redraw()

    # ---------------------------------------------------------- 渲染
    def _cur_text(self) -> str:
        """顶栏“当前 …”文案：标签里已带盒号时不再重复拼“→ N 号盒”。"""
        label = (self.current_label or '').strip()
        box = self.current_box or 0
        if not label:
            return ('当前：%d 号盒' % box) if box else '当前：—'
        if '号盒' in label or (box and ('%d号' % box) in label):
            return '当前：%s' % label
        return ('当前：%s → %d 号盒' % (label, box)) if box else ('当前：%s' % label)

    def redraw(self):
        st = self.state if self.state in STATE_TEXT else 'idle'
        self.lb_state.configure(text=STATE_TEXT[st], fg=STATE_COLOR[st])
        self.dot.itemconfigure(1, fill=STATE_COLOR[st])
        if self.current_box:
            self.lb_cur.configure(text=self._cur_text(), fg=FG)
        else:
            self.lb_cur.configure(text=self.last_event, fg=DIM)
        m, sec = divmod(int(self.elapsed), 60)
        self.lb_time.configure(text='%02d:%02d' % (m, sec))
        total = max(1, self.total or 0)
        self.lb_prog.configure(text='已完成 %d / %d' % (self.done, self.total or 0))

        self.lb_link.configure(text='● 已连接' if self.online else '● 未连接',
                               fg=ACCENT if self.online else WARN)

        for w in self._cards:
            w.destroy()
        self._cards = []
        for i in range(1, 7):
            slot = self.boxes[i]
            col = self.cols[i]
            items = slot['items']
            active = (self.current_box == i)
            col['frame'].configure(
                highlightbackground=ACCENT if active else LINE,
                highlightthickness=2 if active else 1)
            col['name'].configure(
                text=(slot['label'] or '待放入'),
                fg=(color_hex(slot['color']) if slot['label'] else DIM))
            col['cnt'].configure(text='%d 件' % len(items),
                                 fg=FG if items else DIM)
            body = col['body']
            cw = col['w'] - 2
            if not items:
                empty = tk.Frame(body, bg=PANEL2, highlightbackground=LINE,
                                 highlightthickness=1)
                empty.place(x=self.x(10), y=self.y(10), width=cw - self.x(20),
                            height=self.y(96))
                hint = self.lab(empty, '待放入', fg=DIM, size=15, bold=False,
                                anchor='center', bg=PANEL2)
                hint.place(x=0, y=self.y(36), width=cw - self.x(20))
                self._cards.extend([empty])
                continue
            for k, it in enumerate(reversed(items)):
                cy = self.y(6) + k * self.y(94)
                card = tk.Frame(body, bg=PANEL)
                card.place(x=self.x(8), y=cy, width=cw - self.x(16),
                           height=self.y(84))
                side = self.f(52)
                if it.get('thumb') is not None:
                    lb = tk.Label(card, image=it['thumb'], bg=PANEL, bd=0)
                    lb.image = it['thumb']
                    lb.place(x=self.x(6), y=self.y(8))
                else:
                    lb = tk.Frame(card, bg=color_hex(it.get('color')),
                                  highlightbackground='#000000',
                                  highlightthickness=1)
                    lb.place(x=self.x(6), y=self.y(8), width=side, height=side)
                seq = self.lab(card, '#%d' % it['seq'], size=17, mono=True)
                seq.place(x=self.x(6) + side + self.x(8), y=self.y(20))
                nm = self.lab(card, it['name'], size=15, bold=False, fg=DIM)
                nm.place(x=self.x(6) + side + self.x(8), y=self.y(46))
                self._cards.extend([card])

        if self.latest:
            self.lb_seq.configure(text='#%d' % self.latest['seq'])
            self.lb_name.configure(text=self.latest['name'],
                                   fg=color_hex(self.latest.get('color')))
        self.lb_event.configure(text=self.last_event)
        self.lb_alert.configure(text=('⚠ ' + self.alerts[-1]) if self.alerts else '')


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
    args = ap.parse_args()
    set_theme(args.theme)
    w, h = (int(v) for v in str(args.screen).lower().split('x'))
    try:                                              # Windows: 物理像素
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                 # noqa: BLE001
        pass
    root = tk.Tk()
    scr = Screen(root, w, h, fullscreen=not args.windowed)
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
        root.after(120, drain)

    root.after(120, drain)
    if args.fake:
        demo(root, scr)
    try:
        root.mainloop()
    finally:
        if pump:
            pump.stop()


if __name__ == '__main__':
    main()
