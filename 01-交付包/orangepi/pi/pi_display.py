"""派端比赛显示屏（智能分拣赛项）——按赛题要求只做“显示”。

赛题要求（原文，02-资料库/01-赛题/newpdftext.txt）：
  1) 分拣装置上需安装一块仅具有显示功能的高亮显示屏，支持各种格式的图片播放，
     并能够显示分拣的各种信息及数据，如序号、图片及相关信息等；
  2) 每分拣一件并入盒后，屏上须显示：序号、货物名称、图片、分拣成功总数量；
  3) 超过 15 s 没有动作或没有显示本次分拣信息 → 本轮结束（屏必须一直活着、一直更新）。

界面（1024×600 横屏）：
  ┌ 顶栏：标题 · 状态 · 计时 · 已完成 n/N · 连接灯 ┐
  │ 表格：序号 | 货物名称 | 图片 | 分拣成功总数量   │（最近 4 行，最新一行高亮）
  │ 盒位：6 个储物盒的件数                          │
  └ 底栏：最近事件 + 报警（红）                      ┘

用法（派上）：
    python3 pi_display.py                 # 全屏、监听 TCP 5059
    python3 pi_display.py --windowed      # 窗口模式（调试/截图）
    python3 pi_display.py --fake          # 假数据自演示
    python3 pi_display.py --port 5059 --screen 1024x600

协议（上位机 → 派，NDJSON 每行一条，字段都可缺省）：
    {"kind":"state","state":"running","elapsed_s":123.4,"done":7,"total":12,
     "alerts":[],"connected":true,"last_event":"..."}
    {"kind":"item","seq":7,"name":"圆锥","color":"蓝","box":3,
     "thumb_b64":"<PNG base64>","total_done":7}
    {"kind":"item_done","seq":7,"ok":true,"total_done":7}
    {"kind":"alarm","text":"DeepSeek 超时，已回退几何判据"}

兼容旧格式：{"state":"DISPLAY","item":3,"total":12,"name":"五棱柱|蓝","box":2,
            "placed":3,"message":"..."} 也能显示（自动映射成 item 事件）。
"""
import argparse
import base64
import os
import json
import queue
import socket
import threading
import time

import tkinter as tk

BG = '#0b0d10'
PANEL = '#14181d'
FG = '#f2f5f8'
DIM = '#8b97a3'
ACCENT = '#39d353'
HILITE = '#14361f'
WARN = '#ff8c00'
BAD = '#e5484d'
LINE = '#252b33'

STATE_TEXT = {'idle': '待机', 'running': '运行中', 'paused': '暂停',
              'timeout': '超时', 'done': '完成'}
STATE_COLOR = {'idle': DIM, 'running': ACCENT, 'paused': WARN,
               'timeout': BAD, 'done': '#4ea1ff'}

UI_FONT = 'Microsoft YaHei UI'
MONO = 'Consolas'


def parse_line(raw):
    """把一行字节解析成 dict；非法返回 None（显示器不能因为坏报文崩）。"""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode('utf-8', 'replace')
        except Exception:                            # noqa: BLE001
            return None
    text = str(raw).strip()
    if not text or not text.startswith('{'):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class Pump:
    """TCP 行服务器：收到 JSON 就塞进队列，界面用 after() 取。"""

    def __init__(self, port, sink):
        self.port = int(port)
        self.sink = sink
        self.online = False
        self.sock = None
        self.thread = None
        self._stop = threading.Event()

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', self.port))
        self.sock.listen(4)
        self.sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.online = True
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
            self.online = False
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
        self.items = []          # 最近 4 件
        self.boxes = {i: 0 for i in range(1, 7)}
        self.state = 'idle'
        self.elapsed = 0.0
        self.done = 0
        self.total = 0
        self.alerts = []
        self.online = False
        self.last_event = '等待开始…'
        self._row_widgets = []
        self.log = bool(os.environ.get('DISPLAY_LOG'))

        root.title('智能分拣 · 显示')
        root.configure(bg=BG)
        if fullscreen:
            root.attributes('-fullscreen', True)
        else:
            root.geometry('%dx%d' % (self.W, self.H))
        root.resizable(False, False)
        self.sx = self.W / 1024.0
        self.sy = self.H / 600.0
        self._build()

    def x(self, px):
        return int(round(px * self.sx))

    def y(self, px):
        return int(round(px * self.sy))

    def f(self, px):
        return max(8, int(round(px * min(self.sx, self.sy))))

    # ------------------------------------------------------------ 布局
    def _lab(self, parent, text='', fg=FG, bg=BG, size=20, bold=True, mono=False):
        return tk.Label(parent, text=text, bg=bg, fg=fg,
                        font=((MONO if mono else UI_FONT), -self.f(size),
                              'bold' if bold else 'normal'),
                        anchor='w')

    def _build(self):
        W, H = self.W, self.H
        self.top = tk.Frame(self.root, bg=PANEL)
        self.top.place(x=0, y=0, width=W, height=self.y(58))
        self.lb_title = self._lab(self.top, '智能分拣', size=27)
        self.lb_title.place(x=self.x(18), y=self.y(12))
        self.lb_state = self._lab(self.top, '● 待机', fg=DIM, size=22)
        self.lb_state.place(x=self.x(168), y=self.y(16))
        self.lb_time = self._lab(self.top, '00:00', size=30, mono=True)
        self.lb_time.place(x=self.x(330), y=self.y(10))
        self.lb_prog = self._lab(self.top, '已完成 0 / 0', size=23)
        self.lb_prog.place(x=self.x(470), y=self.y(15))
        self.lb_link = self._lab(self.top, '● 未连接', fg=WARN, size=20, bold=False)
        self.lb_link.place(x=W - self.x(160), y=self.y(18))

        self.head = tk.Frame(self.root, bg=BG)
        self.head.place(x=0, y=self.y(64), width=W, height=self.y(34))
        for text, cx, cw in (('序号', 18, 80), ('货物名称', 110, 330),
                             ('图片', 480, 160), ('分拣成功总数量', 660, 340)):
            self._lab(self.head, text, fg=DIM, size=19).place(
                x=self.x(cx), y=self.y(4), width=self.x(cw), height=self.y(28))
        tk.Frame(self.root, bg=LINE).place(x=self.x(14), y=self.y(100),
                                           width=W - self.x(28), height=2)

        self.bottom = tk.Frame(self.root, bg=PANEL)
        self.bottom.place(x=0, y=H - self.y(60), width=W, height=self.y(60))
        self.lb_event = self._lab(self.bottom, '等待开始…', size=19, bold=False)
        self.lb_event.place(x=self.x(16), y=self.y(16))
        self.lb_alert = self._lab(self.bottom, '', fg=BAD, size=19)
        self.lb_alert.place(x=W - self.x(450), y=self.y(16))

        self.boxbar = tk.Frame(self.root, bg=PANEL)
        self.boxbar.place(x=0, y=H - self.y(136), width=W, height=self.y(76))
        self._lab(self.boxbar, '储物盒', fg=DIM, size=18).place(x=self.x(16), y=self.y(26))
        self.box_labels = {}
        for i in range(1, 7):
            lb = self._lab(self.boxbar, '%d号 0 件' % i, fg=DIM, size=19)
            lb.place(x=self.x(104 + (i - 1) * 148), y=self.y(26))
            self.box_labels[i] = lb

        avail = self.H - self.y(104) - self.y(136) - self.y(60)
        self.row_h = max(self.y(52), avail // 4)
        self.body = tk.Frame(self.root, bg=BG)
        self.body.place(x=0, y=self.y(104), width=W, height=self.row_h * 4)
        self.redraw()

    # ------------------------------------------------------------ 数据
    def apply(self, msg):
        if self.log:
            print('[disp] ' + json.dumps(msg, ensure_ascii=False), flush=True)
        kind = msg.get('kind')
        if kind is None and 'state' in msg:          # 兼容旧格式
            kind = 'item' if ('item' in msg and msg.get('item')) else 'state'
            if kind == 'item':
                nm = str(msg.get('name') or '')
                name, _, color = nm.partition('|')
                msg = {'kind': 'item', 'seq': int(msg.get('item') or 0),
                       'name': name or nm, 'color': color,
                       'box': int(msg.get('box') or 0),
                       'total_done': int(msg.get('placed') or 0),
                       'last_event': msg.get('message')}
            else:
                msg = {'kind': 'state', 'state': 'running',
                       'done': int(msg.get('placed') or 0),
                       'total': int(msg.get('total') or 0)}
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
                self.last_event = str(msg.get('last_event'))
        elif kind == 'item':
            seq = int(msg.get('seq') or (len(self.items) + 1))
            thumb = None
            b64 = msg.get('thumb_b64')
            if b64:
                try:
                    thumb = tk.PhotoImage(data=base64.b64decode(b64))
                except Exception:                    # noqa: BLE001
                    thumb = None
            self.items.append({'seq': seq, 'name': msg.get('name') or '—',
                               'color': msg.get('color') or '', 'thumb': thumb,
                               'done': int(msg.get('total_done') or self.done),
                               'ok': True})
            self.items = self.items[-4:]
            self.last_item_ts = time.time()
            box = msg.get('box')
            if isinstance(box, int) and 1 <= box <= 6:
                self.boxes[box] = self.boxes.get(box, 0) + 1
            self.last_event = str(msg.get('last_event') or ('第 %d 件：%s%s%s' % (
                seq, msg.get('name') or '—',
                ('·' + str(msg.get('color'))) if msg.get('color') else '',
                (' → %d 号盒' % box) if box else '')))
        elif kind == 'item_done':
            self.done = int(msg.get('total_done') or self.done)
            for it in self.items:
                if it['seq'] == int(msg.get('seq') or -1):
                    it['ok'] = bool(msg.get('ok', True))
                    it['done'] = self.done
        elif kind == 'alarm':
            self.alerts.append(str(msg.get('text') or '报警'))
            self.alerts = self.alerts[-2:]
        self.redraw()

    # ------------------------------------------------------------ 渲染
    def redraw(self):
        st = self.state if self.state in STATE_TEXT else 'idle'
        self.lb_state.configure(text='● ' + STATE_TEXT[st], fg=STATE_COLOR[st])
        m, sec = divmod(int(self.elapsed), 60)
        self.lb_time.configure(text='%02d:%02d' % (m, sec))
        self.lb_prog.configure(text='已完成 %d / %d' % (self.done, self.total or 0))
        self.lb_link.configure(text='● 已连接' if self.online else '● 未连接',
                               fg=ACCENT if self.online else WARN)

        for w in self._row_widgets:
            w.destroy()
        self._row_widgets = []
        rows = list(reversed(self.items))
        row_h = self.row_h
        for r, it in enumerate(rows):
            top = r * row_h
            bg = HILITE if r == 0 else BG
            strip = tk.Frame(self.body, bg=bg)
            strip.place(x=self.x(14), y=top + self.y(2),
                        width=self.W - self.x(28), height=row_h - self.y(4))
            self._row_widgets.append(strip)
            cell = ' '.join(v for v in (it['name'], it['color']) if v)
            for text, cx, cw, size, mono in ((str(it['seq']), 26, 80, 32, True),
                                            (cell, 118, 340, 27, False),
                                            (str(it.get('done', '')), 672, 300, 34, True)):
                lb = self._lab(self.body, text, fg=FG, bg=bg, size=size, mono=mono)
                lb.place(x=self.x(cx), y=top + self.y(10),
                         width=self.x(cw) - self.x(8), height=row_h - self.y(20))
                self._row_widgets.append(lb)
            holder = tk.Frame(self.body, bg=bg)
            side = max(self.y(40), row_h - self.y(22))
            holder.place(x=self.x(496), y=top + (row_h - side) // 2,
                         width=side, height=side)
            if it.get('thumb') is not None:
                lb = tk.Label(holder, image=it['thumb'], bg=bg, bd=0)
                lb.image = it['thumb']
            else:
                lb = self._lab(holder, '▣', fg=DIM, bg=bg, size=30)
            lb.place(relx=0.5, rely=0.5, anchor='center')
            self._row_widgets.append(holder)

        for i, lb in self.box_labels.items():
            n = self.boxes.get(i, 0)
            lb.configure(text='%d号  %d 件' % (i, n), fg=FG if n else DIM)
        self.lb_event.configure(text=self.last_event)
        self.lb_alert.configure(text=('⚠ ' + self.alerts[-1]) if self.alerts else '')


def demo(root, scr):
    """假数据演示：每 1.8 s 加一件，用于审版式。"""
    names = ['正方体', '长方体', '正三棱柱', '正五棱柱', '正六棱柱',
             '圆柱', '圆锥', '正四面体', '球']
    colors = ['红', '黄', '蓝', '绿', '紫', '橙']
    scr.apply({'kind': '_connected'})
    scr.apply({'kind': 'state', 'state': 'running', 'total': 12,
               'last_event': '演示数据（未连接真实流程）'})
    seq = [0]
    t0 = time.time()

    def tick():
        seq[0] += 1
        n = seq[0]
        box = (n - 1) % 6 + 1
        scr.apply({'kind': 'item', 'seq': n,
                   'name': names[(n * 3) % len(names)],
                   'color': colors[n % len(colors)], 'box': box,
                   'total_done': n})
        scr.apply({'kind': 'item_done', 'seq': n, 'ok': True, 'total_done': n})
        scr.apply({'kind': 'state', 'state': 'running', 'total': 12,
                   'done': n, 'elapsed_s': time.time() - t0})
        if seq[0] >= 12:
            scr.apply({'kind': 'state', 'state': 'done', 'done': 12,
                       'total': 12, 'elapsed_s': time.time() - t0,
                       'last_event': '分拣完成：共 12 件'})
            return
        root.after(1800, tick)

    root.after(900, tick)


def main():
    ap = argparse.ArgumentParser(description='派端比赛显示（只显示）')
    ap.add_argument('--port', type=int, default=5059)
    ap.add_argument('--screen', default='1024x600')
    ap.add_argument('--windowed', action='store_true')
    ap.add_argument('--fake', action='store_true', help='假数据演示')
    args = ap.parse_args()
    w, h = (int(x) for x in str(args.screen).lower().split('x'))
    try:                                     # Windows: 关掉 DPI 虚拟缩放，保证 1024x600 是物理像素
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                        # noqa: BLE001
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
