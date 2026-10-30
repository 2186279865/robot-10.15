"""把"当前分拣状态"推给派端全屏显示页（TCP 5059，JSON 行）。

用法：

    from display_link import DisplayLink
    ui = DisplayLink('192.168.100.198')
    ui.push(item=2, total=12, name='五棱柱|蓝', box=3, placed=2)

设计：
- 后台线程 + 队列，永不阻塞主流程；对方不在线就丢弃（显示只是"锦上添花"）；
- 断线自动重连（2 s 一次）；只写不读；
- 只依赖标准库。
"""
import json
import queue
import socket
import threading
import time

DEFAULT_PORT = 5059


class DisplayLink:
    def __init__(self, host, port=DEFAULT_PORT, timeout=1.5, verbose=False):
        self.host = host or ''
        self.port = int(port)
        self.timeout = float(timeout)
        self.verbose = verbose
        self._q = queue.Queue(maxsize=64)
        self._sock = None
        self._stop = threading.Event()
        self.online = False
        self.thread = None

    def start(self):
        if not self.host:
            return
        self.thread = threading.Thread(target=self._run, name='display-link', daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._close()

    def push(self, **fields):
        """非阻塞：队列满直接丢最旧的一条。"""
        if not self.host:
            return
        try:
            self._q.put_nowait(fields)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(fields)
            except queue.Full:
                pass

    def push_text(self, message, **fields):
        fields['message'] = message
        self.push(**fields)

    # ---------- 赛题版（NDJSON v2，给 pi_display.py 的六列界面） ----------

    def state(self, done=None, total=None, elapsed_s=None, status='running',
              last_event=None, current_box=None, current_label=None):
        msg = {'kind': 'state', 'state': status}
        if done is not None:
            msg['done'] = int(done)
        if total is not None:
            msg['total'] = int(total)
        if elapsed_s is not None:
            msg['elapsed_s'] = round(float(elapsed_s), 1)
        if last_event:
            msg['last_event'] = str(last_event)[:80]
        if current_box:
            msg['current_box'] = int(current_box)
        if current_label:
            msg['current_label'] = str(current_label)[:40]
        self.push(**msg)

    def item(self, seq, name, color='', box=0, box_label='', total_done=None,
             last_event=None, thumb_b64=None, bbox=None, cam=None):
        msg = {'kind': 'item', 'seq': int(seq), 'name': str(name)[:24],
               'color': str(color)[:8], 'box': int(box)}
        if box_label:
            msg['box_label'] = str(box_label)[:40]
        if total_done is not None:
            msg['total_done'] = int(total_done)
        if last_event:
            msg['last_event'] = str(last_event)[:80]
        if thumb_b64:
            msg['thumb_b64'] = thumb_b64
        # 派端屏自己抓“放入瞬间”的实时帧时用：bbox 是 700×700 校正图里的归一化框
        if bbox is not None:
            try:
                msg['bbox'] = [round(float(v), 4) for v in bbox][:4]
            except (TypeError, ValueError):
                pass
        if cam is not None:
            msg['cam'] = int(cam)
        self.push(**msg)

    def item_done(self, seq, total_done, ok=True, last_event=None):
        msg = {'kind': 'item_done', 'seq': int(seq), 'total_done': int(total_done),
               'ok': bool(ok)}
        if last_event:
            msg['last_event'] = str(last_event)[:80]
        self.push(**msg)

    def alarm(self, text):
        self.push(kind='alarm', message=str(text)[:80])

    def start_keepalive(self, interval_s=5.0, provider=None):
        """赛题要求“超 15 s 无动作/无显示 → 本轮结束”，这里定时刷状态保活。

        provider() 返回 dict(done=, total=, elapsed_s=, status=, last_event=)；
        为 None 时只发一个“运行中”的状态。
        """
        if not self.host or getattr(self, '_ka_thread', None):
            return
        self._ka_stop = threading.Event()

        def _loop():
            while not self._ka_stop.is_set():
                try:
                    info = provider() if provider else {}
                except Exception:                     # noqa: BLE001
                    info = {}
                try:
                    self.state(**info)
                except Exception:                     # noqa: BLE001
                    pass
                self._ka_stop.wait(interval_s)

        self._ka_thread = threading.Thread(target=_loop, name='display-keepalive',
                                           daemon=True)
        self._ka_thread.start()

    def stop_keepalive(self):
        ev = getattr(self, '_ka_stop', None)
        if ev is not None:
            ev.set()

    @staticmethod
    def thumb_b64(frame_path, bbox, size=96, pad=0.18):
        """从顶视校正图裁出该目标，缩成 size×size 的 JPEG，返回 base64（失败返回 None）。"""
        try:
            import base64
            import cv2
            import numpy as np
            arr = np.fromfile(str(frame_path), dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None or not bbox:
                return None
            x, y, w, h = [float(v) for v in bbox[:4]]
            pw, ph = w * pad, h * pad
            x0 = max(0, int(x - pw))
            y0 = max(0, int(y - ph))
            x1 = min(img.shape[1], int(x + w + pw))
            y1 = min(img.shape[0], int(y + h + ph))
            if x1 - x0 < 4 or y1 - y0 < 4:
                return None
            crop = img[y0:y1, x0:x1]
            side = max(crop.shape[:2])
            canvas = np.full((side, side, 3), 240, dtype=np.uint8)
            oy = (side - crop.shape[0]) // 2
            ox = (side - crop.shape[1]) // 2
            canvas[oy:oy + crop.shape[0], ox:ox + crop.shape[1]] = crop
            small = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode('.jpg', small, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ok:
                return None
            return base64.b64encode(buf.tobytes()).decode('ascii')
        except Exception:                             # noqa: BLE001
            return None

    # ---------- 内部 ----------

    def _close(self):
        sock, self._sock = self._sock, None
        self.online = False
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass

    def _connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        self.online = True
        if self.verbose:
            print('display online %s:%d' % (self.host, self.port))
        return sock

    def _run(self):
        while not self._stop.is_set():
            try:
                sock = self._sock or self._connect()
            except OSError:
                time.sleep(2.0)
                continue
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            line = json.dumps(item, ensure_ascii=False) + '\n'
            try:
                sock.sendall(line.encode('utf-8'))
            except OSError:
                self._close()
                time.sleep(0.5)
        self._close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='给派端显示页推一条测试消息')
    parser.add_argument('host', nargs='?', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    link = DisplayLink(args.host, args.port, verbose=True)
    link.start()
    for i in range(1, 4):
        link.push(item=i, total=3, name='五棱柱|蓝', box=1, placed=i,
                  message='第 %d 件：五棱柱·蓝 → 1 号盒' % i)
        time.sleep(1.0)
    link.push(state='DONE', placed=3, message='全部分拣完成')
    time.sleep(0.3)
    link.stop()
