"""视觉结果通讯：笔记本（视觉）→ 上位机/香橙派/单片机。

三种链路，按现场条件选一个：
1. 同一局域网（WiFi 或网线直连）：TCP + JSON Lines，最省事，本模块 `TargetServer`。
2. USB 网卡/网线直连：同上，只是把对方 IP 设成固定地址（如 192.168.137.x / 169.254.x）。
3. 串口（香橙派 ↔ STM32/Arduino）：本模块 `pack_uart()` 给出 0xAA55 二进制帧，
   Pi 收到 TCP 帧后原样转发给单片机即可。

协议（TCP，每行一个 JSON，UTF-8，`\\n` 结尾）：
    {"seq": 12, "ts": 1730000000.123, "fps": 0.8, "n": 3,
     "targets": [{"id": 1, "u": 0.45, "v": 0.38, "class_id": "1",
                  "class_name": "圆柱黄", "angle": 0.0, "area": 0.025,
                  "views": "top+side"}],
     "fusion": {"matched": 4, "axis": "u", "flip": true}}

u 是托盘归一化横坐标（0~1，从左到右），v 是纵坐标（0~1，从上到下）；
机器人把 u,v 乘托盘实际尺寸（或标定好的像素当量）就得到抓取点。

串口帧（香橙派 → 单片机）：
    0xAA 0x55 | len(2B, 小端) | payload(lenB, 就是上面那行 JSON 的 UTF-8) | crc16(2B, 小端)
    crc16 = CRC-16/MODBUS，多项式 0xA001，初值 0xFFFF，覆盖 len+payload。
"""
import json
import socket
import threading
import time

MAGIC = b'\xAA\x55'
DISCOVER_PORT = 5056
DISCOVER_MAGIC = 'NEWEYE1'


# ----------------------------------------------------------------- 串口帧格式
def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def pack_uart(payload):
    """payload: dict 或 bytes。返回可直接 write 到串口的帧。"""
    body = payload if isinstance(payload, (bytes, bytearray)) else \
        json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf8')
    head = len(body).to_bytes(2, 'little')
    crc = crc16(head + body)
    return MAGIC + head + body + crc.to_bytes(2, 'little')


def unpack_uart(buf):
    """从字节流里取出一个完整帧。返回 (payload_bytes, 消耗长度) 或 (None, 0)。"""
    i = buf.find(MAGIC)
    if i < 0:
        return None, max(0, len(buf) - 1)      # 丢掉明显不是帧头的前缀
    if len(buf) - i < 6:
        return None, i
    n = int.from_bytes(buf[i + 2:i + 4], 'little')
    if len(buf) - i < 6 + n:
        return None, i
    body = buf[i + 4:i + 4 + n]
    crc = int.from_bytes(buf[i + 4 + n:i + 6 + n], 'little')
    if crc != crc16(buf[i + 2:i + 4] + body):
        return None, i + 2                     # 校验错，跳过帧头继续找
    return bytes(body), i + 6 + n


# ----------------------------------------------------------------- UDP 自动发现
def _local_ips():
    """本机可能用于局域网通讯的 IPv4 地址（用来告诉机器人该连哪个）。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
    except OSError:
        pass
    return ips


def _beacon_loop(stop_evt, data_port, discover_port, interval=.8):
    """每 0.8s 广播一次"我是视觉端"。

    关键：只往 255.255.255.255 发时，Windows 只会走默认路由那块网卡——笔记本
    同时开着 VPN / 有线上网时，热点网段（192.168.137.x）根本收不到。所以给
    每个本地 IPv4 各绑一个 socket 分别广播，热点/有线哪个网段都能收到。
    """
    socks = []
    try:
        for ip in _local_ips() or [None]:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if ip:
                    s.bind((ip, 0))
                socks.append(s)
            except OSError:
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass
        if not socks:                       # 一个都绑不上就退回默认路由
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            socks.append(s)
        while not stop_evt.wait(interval):
            msg = json.dumps({'app': DISCOVER_MAGIC, 'port': data_port,
                              'ips': _local_ips()}).encode('utf8')
            for s in socks:
                try:
                    s.sendto(msg, ('255.255.255.255', discover_port))
                except OSError:
                    pass
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


def discover(timeout=4.0, discover_port=DISCOVER_PORT):
    """在局域网里找视觉端（笔记本）。返回 (ip, port) 或 None。

    视觉端开启“网络推送”后会每隔 0.8s 往 UDP 5056 广播自己的 IP 和端口，
    所以机器人端不需要知道笔记本 IP，换网络/换 IP 都能自己找到。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('', discover_port))
    except OSError:
        return None
    s.settimeout(timeout)
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                return None
            try:
                d = json.loads(data.decode('utf8'))
            except ValueError:
                continue
            if d.get('app') == DISCOVER_MAGIC:
                return addr[0], int(d.get('port', 5055))
        return None
    finally:
        s.close()


# ----------------------------------------------------------------- TCP 服务端
class TargetServer:
    """在视觉端启动，把每一帧目标结果广播给所有已连接的机器人。"""

    def __init__(self, port=5055, host='0.0.0.0', beacon=True,
                 discover_port=DISCOVER_PORT):
        self.port, self.host = port, host
        self.beacon = beacon
        self.discover_port = discover_port
        self.sock = None
        self.clients = []
        self.lock = threading.Lock()
        self.on = False
        self.thread = None
        self.sent = 0
        self._stop_evt = threading.Event()

    def start(self):
        if self.on:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(4)
        s.settimeout(.5)
        self.sock = s
        self.on = True
        self._stop_evt.clear()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()
        if self.beacon:
            threading.Thread(target=_beacon_loop, daemon=True,
                             args=(self._stop_evt, self.port, self.discover_port)).start()

    def _accept(self):
        while self.on:
            try:
                c, addr = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            c.settimeout(1.0)
            with self.lock:
                self.clients.append((c, addr))

    def send(self, payload):
        """向所有客户端发一帧；返回发送成功的连接数。"""
        if not self.on:
            return 0
        line = (json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf8')
        ok = 0
        dead = []
        with self.lock:
            for c, addr in self.clients:
                try:
                    c.sendall(line)
                    ok += 1
                except OSError:
                    dead.append((c, addr))
            for d in dead:
                self.clients.remove(d)
                try:
                    d[0].close()
                except OSError:
                    pass
        self.sent += 1
        return ok

    def count(self):
        with self.lock:
            return len(self.clients)

    def stop(self):
        self.on = False
        self._stop_evt.set()
        with self.lock:
            for c, _ in self.clients:
                try:
                    c.close()
                except OSError:
                    pass
            self.clients.clear()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# ----------------------------------------------------------------- TCP 客户端
class TargetClient:
    """机器人端：连上视觉端，逐帧回调 on_frame(dict)。断线自动重连。"""

    def __init__(self, host, port=5055, on_frame=None, on_log=None):
        self.host, self.port = host, port
        self.on_frame = on_frame
        self.on_log = on_log or (lambda m: None)
        self.on = False
        self.sock = None
        self.frames = 0
        self.thread = None

    def start(self):
        if self.on:
            return
        self.on = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while self.on:
            host, port = self.host, self.port
            if host in (None, '', 'auto'):
                got = discover(timeout=3.0)
                if not got:
                    self.on_log('局域网里没有找到视觉端（UDP %d 无应答），1s 后重试'
                                % DISCOVER_PORT)
                    if self.on:
                        time.sleep(1.0)
                    continue
                host, port = got
                self.on_log('自动发现视觉端：%s:%d' % (host, port))
            try:
                s = socket.create_connection((host, port), timeout=3)
                s.settimeout(1.0)
                self.sock = s
                self.on_log('已连接 %s:%d' % (host, port))
                buf = b''
                while self.on:
                    try:
                        chunk = s.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        if not line.strip():
                            continue
                        try:
                            f = json.loads(line.decode('utf8'))
                        except ValueError:
                            continue
                        self.frames += 1
                        if self.on_frame:
                            self.on_frame(f)
            except OSError as e:
                self.on_log('连接失败/断开：%s' % e)
            finally:
                if self.sock is not None:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
            if self.on:
                time.sleep(1.0)

    def stop(self):
        self.on = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


def control_frame(payload):
    """把 latest_targets.json 的完整 payload 压成机器人控制用的小帧。"""
    targets = []
    for t in payload.get('targets', []):
        uv = t.get('center_tray') or [0.0, 0.0]
        targets.append({'id': t.get('id'), 'class_id': t.get('class_id'),
                        'class_name': t.get('class_name'), 'views': t.get('views'),
                        'u': round(float(uv[0]), 4), 'v': round(float(uv[1]), 4),
                        'area': t.get('area_px')})
    return {'seq': payload.get('seq', 0), 'ts': payload.get('ts', 0.0),
            'n': len(targets), 'targets': targets,
            'fusion': payload.get('fusion', {})}
