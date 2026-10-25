"""派端显示页：全屏大字显示"当前分拣到第几件 / 什么 / 进哪个盒"。

比赛要求（赛题）：显示屏必须显示序号 / 名称 / 图片 / 数量，且只做"显示"不做交互。
本文件只用标准库 + tkinter（派上已装），通过 TCP 5059 收上位机推来的 JSON 行：

    {"state": "DISPLAY", "item": 3, "total": 12, "name": "五棱柱|蓝", "box": 2,
     "placed": 3, "message": "第 3 件：五棱柱·蓝 → 2 号盒", "key": ""}

用法（派上）：

    python3 pi_display.py                 # 全屏、监听 5059
    python3 pi_display.py --windowed      # 窗口模式（调试）
    python3 pi_display.py --fake          # 假数据自演示，不连网络
    python3 pi_display.py --port 5059

设计原则：
- 断线不影响显示（保留最后一屏，角标显示"离线"）；
- 任何非法行都被忽略（显示器不该因为一条坏报文崩掉）；
- 字号按屏幕高度自动缩放，1024×600 与 1920×1080 都能看清。
"""
import argparse
import json
import queue
import socket
import threading

import tkinter as tk

BG = '#000000'
FG = '#ffffff'
ACCENT = '#39d353'
WARN = '#ff8c00'


def parse_line(raw):
    """把一行字节解析成 dict；非法返回 None（绝不抛）。"""
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
    """TCP 行服务器：把收到的 JSON dict 塞进队列，界面用 after() 取。"""

    def __init__(self, port, sink):
        self.port = int(port)
        self.sink = sink
        self.sock = None
        self.thread = None
        self.online = False
        self._stop = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self._run, name='display-net', daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass

    def _run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('0.0.0.0', self.port))
        srv.listen(4)
        srv.settimeout(0.5)
        self.sock = srv
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.sink(('status', True))
            self._serve(conn)
            self.sink(('status', False))

    def _serve(self, conn):
        conn.settimeout(1.0)
        buf = b''
        with conn:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    data = parse_line(line)
                    if data is not None:
                        self.sink(('data', data))


class Display(tk.Tk):
    def __init__(self, port=5059, fullscreen=True, fake=False):
        super().__init__()
        self.title('分拣显示')
        self.configure(bg=BG)
        self.attributes('-fullscreen', bool(fullscreen))
        self.config(cursor='none')
        self.queue = queue.Queue()
        self.online = False
        self.last = {}

        w, h = self.winfo_screenwidth(), self.winfo_screenheight()
        base = max(28, int(h / 16))
        self.lbl_index = tk.Label(self, text='准备就绪', bg=BG, fg=FG,
                                  font=('DejaVu Sans', base * 2, 'bold'))
        self.lbl_index.pack(pady=(int(h * 0.06), 0))
        self.lbl_name = tk.Label(self, text='', bg=BG, fg=ACCENT,
                                 font=('DejaVu Sans', base * 2, 'bold'))
        self.lbl_name.pack(pady=(int(h * 0.02), 0))
        self.lbl_box = tk.Label(self, text='', bg=BG, fg=FG,
                                font=('DejaVu Sans', base, 'bold'))
        self.lbl_box.pack(pady=(int(h * 0.02), 0))
        self.lbl_msg = tk.Label(self, text='等待上位机连接…', bg=BG, fg=WARN,
                                font=('DejaVu Sans', int(base * 0.7)))
        self.lbl_msg.pack(side='bottom', pady=int(h * 0.04))
        self.lbl_net = tk.Label(self, text='离线', bg=BG, fg=WARN,
                                font=('DejaVu Sans', int(base * 0.5)))
        self.lbl_net.place(x=10, y=10)
        self.lbl_clock = tk.Label(self, text='', bg=BG, fg='#888888',
                                  font=('DejaVu Sans', int(base * 0.5)))
        self.lbl_clock.place(relx=1.0, y=10, anchor='ne')

        self.pump = None
        if fake:
            self.after(600, self._fake_loop)
        else:
            self.pump = Pump(port, self.queue.put)
            self.pump.start()
        self.after(100, self._drain)
        self.after(1000, self._tick_clock)
        self.bind('<Escape>', lambda _e: self.destroy())

    # ---------- 渲染 ----------

    def apply(self, data):
        self.last.update(data)
        item = data.get('item')
        total = data.get('total')
        placed = data.get('placed')
        name = data.get('name') or data.get('class_name') or ''
        box = data.get('box')
        message = data.get('message') or ''

        if item is not None and total:
            self.lbl_index.config(text='第 %s / %s 件' % (item, total))
        elif placed:
            self.lbl_index.config(text='已完成 %s 件' % placed)
        if name:
            self.lbl_name.config(text=str(name).replace('|', ' · '))
        if box:
            self.lbl_box.config(text='放入 %s 号盒' % box)
        if message:
            self.lbl_msg.config(text=message, fg=WARN)
        elif data.get('state') == 'DONE':
            self.lbl_msg.config(text='全部分拣完成', fg=ACCENT)

    def _drain(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == 'status':
                    self.online = bool(payload)
                    self.lbl_net.config(text='在线' if self.online else '离线',
                                        fg=ACCENT if self.online else WARN)
                else:
                    self.apply(payload)
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _tick_clock(self):
        import time

        self.lbl_clock.config(text=time.strftime('%H:%M:%S'))
        self.after(1000, self._tick_clock)

    def _fake_loop(self):
        import time

        names = ['五棱柱|蓝', '球|红', '立方体|绿', '五棱柱|蓝', '球|红']
        for i, name in enumerate(names, 1):
            self.apply({'state': 'DISPLAY', 'item': i, 'total': len(names),
                        'name': name, 'box': 1 + (i - 1) % 3, 'placed': i,
                        'message': '第 %d 件：%s' % (i, name.replace('|', '·'))})
            self.update()
            time.sleep(1.2)
        self.apply({'state': 'DONE', 'placed': len(names), 'message': '全部分拣完成'})
        self.after(1500, self.destroy)


def main(argv=None):
    parser = argparse.ArgumentParser(description='派端全屏显示页（赛题要求：只显示）')
    parser.add_argument('--port', type=int, default=5059)
    parser.add_argument('--windowed', action='store_true', help='窗口模式（调试）')
    parser.add_argument('--fake', action='store_true', help='假数据自演示')
    args = parser.parse_args(argv)
    app = Display(port=args.port, fullscreen=not args.windowed, fake=args.fake)
    app.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
