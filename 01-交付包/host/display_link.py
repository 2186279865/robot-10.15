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
