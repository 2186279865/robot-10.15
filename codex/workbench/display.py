"""Optional Pi HDMI display. Acknowledge only after text and image have been applied."""
from __future__ import annotations

import base64
import io
import json
import queue
import socketserver
import threading
import tkinter as tk

from PIL import Image, ImageTk


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        line = self.rfile.readline(1024 * 1024 + 1)
        if len(line) > 1024 * 1024 or not line.endswith(b'\n'):
            return
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                return
            done = threading.Event()
            self.server.inbox.put((data, done), timeout=.5)
            if done.wait(3):
                self.wfile.write(b'OK\n')
        except (ValueError, OSError, queue.Full):
            return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_display(port=5059, windowed=False):
    root = tk.Tk()
    root.title('分拣结果')
    root.configure(bg='#101b2d')
    root.geometry('1024x600')
    root.attributes('-fullscreen', not windowed)
    root.bind('<Escape>', lambda e: root.destroy())
    index = tk.Label(root, text='等待分拣', font=('Microsoft YaHei', 30), bg='#101b2d', fg='white')
    index.pack(pady=12)
    name = tk.Label(root, text='', font=('Microsoft YaHei', 32, 'bold'), bg='#101b2d', fg='#61dfcf')
    name.pack()
    image = tk.Label(root, bg='#101b2d')
    image.pack(pady=6)
    detail = tk.Label(root, text='等待上位机连接', font=('Microsoft YaHei', 20), bg='#101b2d', fg='white')
    detail.pack()
    inbox = queue.Queue(maxsize=16)
    server = Server(('0.0.0.0', port), Handler)
    server.inbox = inbox
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def pump():
        try:
            while True:
                data, done = inbox.get_nowait()
                try:
                    index.config(text=f'第 {data.get("item", "—")} 件')
                    name.config(text=data.get('name', '不确定'))
                    detail.config(text=f'储物盒 {data.get("box", "—")}　完成抓放 {data.get("placed", 0)} 件')
                    if data.get('image_b64'):
                        raw = base64.b64decode(data['image_b64'], validate=True)
                        photo = ImageTk.PhotoImage(Image.open(io.BytesIO(raw)).resize((240, 240)))
                        image.config(image=photo)
                        image.photo = photo
                    root.update_idletasks()
                    done.set()
                except Exception:
                    pass
        except queue.Empty:
            pass
        root.after(50, pump)
    root.after(50, pump)
    try:
        root.mainloop()
    finally:
        server.shutdown()
        server.server_close()
