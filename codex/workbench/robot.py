"""Single I/O owner, acknowledged commands, latched cancellation, no automatic unlock."""
from __future__ import annotations

import queue
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse


class RobotError(RuntimeError):
    pass


class Cancelled(RobotError):
    pass


def encode_frame(rid, cmd, *args):
    body = ','.join(str(x) for x in (rid, cmd, *args))
    if any(ch in body for ch in ('$', '#', '!', '*', '\r', '\n')):
        raise ValueError('命令中包含协议控制字符')
    data = body.encode('ascii')
    checksum = 0
    for byte in data:
        checksum ^= byte
    frame = b'$' + data + f'*{checksum:02X}\n'.encode('ascii')
    if len(frame) > 96:
        raise ValueError('命令长度超过固件上限')
    return frame


def decode_frame(raw):
    if not raw.startswith(b'#') or len(raw) > 96:
        raise ValueError('错误的回复起始符或长度')
    body, checksum = raw[1:].strip().rsplit(b'*', 1)
    check = 0
    for byte in body:
        check ^= byte
    if len(checksum) != 2 or check != int(checksum, 16):
        raise ValueError('回复校验错误')
    parts = body.decode('ascii').split(',')
    pairs = dict(p.split('=', 1) for p in parts[2:] if '=' in p)
    return int(parts[0]), parts[1], parts[2:], pairs


@dataclass
class Pending:
    event: threading.Event = field(default_factory=threading.Event)
    reply: dict | None = None


class RobotClient:
    simulated = False

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda text: None)
        self.transport = None
        self.online = False
        self.latched = True
        self.firmware = ''
        self.boot = ''
        self.counts = {}
        self.readback_axes = set()
        self.angle_referenced = False
        self.cancel = threading.Event()
        self.shutdown = threading.Event()
        self.cancel.set()
        self.state_lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.pending = {}
        self.next_id = 1
        self.thread = None
        self.last_rx = 0
        self.stop_generation = 0

    def connect(self):
        if self.cfg.get('simulation_only'):
            raise RobotError('当前为演示用标定和限位，禁止连接真机；请重新配置现场机械参数')
        endpoint = self.cfg['endpoint']
        if endpoint.startswith('tcp://'):
            target = urlparse(endpoint)
            if not target.hostname or not target.port:
                raise RobotError('控制地址应为 tcp://IP:5058')
            self.transport = socket.create_connection((target.hostname, target.port), timeout=3)
            self.transport.settimeout(.05)
        else:
            import serial
            self.transport = serial.Serial(endpoint, 115200, timeout=.05, write_timeout=1)
        self.shutdown.clear()
        self.last_rx = time.monotonic()
        self.thread = threading.Thread(target=self._io, daemon=True, name='robot-io')
        self.thread.start()
        try:
            reply = self.request('hello', 1, random.randint(1, 65535), allow_stopped=True)
            self.firmware = reply.get('fw', '')
            self.boot = reply.get('boot', '')
            self.readback_axes = {int(x) for x in reply.get('readback', '').split('/') if x.isdigit()}
            self.online = True
            self.request('tele_on', 1, 100, allow_stopped=True)
            self.log(f'控制已连接：固件 {self.firmware}；需要显式解除停止，运动基准尚未确认')
        except Exception:
            self.close()
            raise

    def _write(self, data):
        with self.write_lock:
            if self.transport is None:
                raise RobotError('控制链路未连接')
            if isinstance(self.transport, socket.socket):
                self.transport.sendall(data)
            else:
                if self.transport.write(data) != len(data):
                    raise RobotError('串口只发送了部分命令')

    def _reserve(self):
        with self.state_lock:
            for _ in range(65535):
                rid = self.next_id
                self.next_id = self.next_id % 65535 + 1
                if rid not in self.pending:
                    pending = Pending()
                    self.pending[rid] = pending
                    return rid, pending
        raise RobotError('请求编号耗尽')

    def request(self, cmd, *args, timeout=4.0, allow_stopped=False):
        # state_lock serializes stop vs command write. Nothing can slip out after stop is latched.
        with self.state_lock:
            if not allow_stopped:
                self.check_cancel()
            if self.shutdown.is_set():
                raise RobotError('控制连接已关闭')
            rid, pending = self._reserve()
            try:
                self._write(encode_frame(rid, cmd, *args))
            except Exception:
                self.pending.pop(rid, None)
                raise
        deadline = time.monotonic() + timeout
        try:
            while not pending.event.wait(.025):
                if not allow_stopped:
                    self.check_cancel()
                if self.shutdown.is_set():
                    raise RobotError('控制连接已中断')
                if time.monotonic() >= deadline:
                    raise RobotError(f'{cmd} 等待回包超时，结果未知')
            reply = pending.reply or {}
            if reply.get('kind') != 'ok':
                raise RobotError(f'{cmd} 失败：{reply.get("error", "未知错误")}')
            if not allow_stopped:
                self.check_cancel()
            return reply.get('pairs', {})
        except Exception:
            if cmd in ('motor_goto', 'motor_angle', 'gripper'):
                self.stop()
            raise
        finally:
            with self.state_lock:
                self.pending.pop(rid, None)

    def _io(self):
        buffer = bytearray()
        next_ping = time.monotonic() + .5
        heartbeat_ids = {}
        try:
            while not self.shutdown.is_set():
                try:
                    data = (self.transport.recv(4096) if isinstance(self.transport, socket.socket)
                            else self.transport.read(4096))
                    if not data and isinstance(self.transport, socket.socket):
                        raise ConnectionError('控制桥关闭连接，可能被其他上位机占用')
                except (socket.timeout, TimeoutError):
                    data = b''
                if data:
                    self.last_rx = time.monotonic()
                    buffer.extend(data)
                    while b'\n' in buffer:
                        line, _, rest = buffer.partition(b'\n')
                        buffer = bytearray(rest)
                        try:
                            rid, kind, fields, pairs = decode_frame(bytes(line))
                        except (ValueError, UnicodeError):
                            continue
                        with self.state_lock:
                            if kind == 'tele':
                                try:
                                    axis = int(pairs.get('addr', pairs.get('axis', 0)))
                                    age_ms = int(pairs.get('age_ms', '-1'))
                                    if pairs.get('readback') != '1' or age_ms < 0:
                                        self.counts.pop(axis, None)
                                    else:
                                        self.counts[axis] = (int(pairs['counts']), time.monotonic() - age_ms / 1000)
                                except (ValueError, KeyError):
                                    pass
                            elif rid and rid in self.pending:
                                p = self.pending[rid]
                                p.reply = {'kind': kind, 'pairs': pairs,
                                           'error': ','.join(fields) if kind == 'err' else ''}
                                p.event.set()
                            elif kind == 'event' and fields:
                                event_type = fields[0]
                                if event_type == 'latch':
                                    self.latched = pairs.get('state') == 'set'
                                    if self.latched:
                                        self.cancel.set()
                                if event_type in ('reset', 'link'):
                                    raise ConnectionError('固件重启或链路失联，必须重新确认基准')
                        if rid in heartbeat_ids:
                            with self.state_lock:
                                self.pending.pop(rid, None)
                            heartbeat_ids.pop(rid, None)
                    if len(buffer) > 8192:
                        buffer.clear()
                now = time.monotonic()
                if self.online and now - self.last_rx > 2.5:
                    raise ConnectionError('2.5 秒未收到固件数据')
                if self.online and now >= next_ping:
                    rid, _ = self._reserve()
                    heartbeat_ids[rid] = now
                    self._write(encode_frame(rid, 'ping'))
                    next_ping = now + .5
                for rid, stamp in list(heartbeat_ids.items()):
                    if now - stamp > 2:
                        heartbeat_ids.pop(rid, None)
                        with self.state_lock:
                            self.pending.pop(rid, None)
        except Exception as exc:
            self.log(f'控制链路停止：{exc}')
        finally:
            self.stop()
            self.online = False
            self.angle_referenced = False
            self.shutdown.set()
            with self.state_lock:
                for pending in self.pending.values():
                    pending.reply = {'kind': 'err', 'error': '连接中断'}
                    pending.event.set()

    def check_cancel(self):
        if self.cancel.is_set() or self.latched:
            raise Cancelled('已停止；只有显式解除停止后才允许下一次操作')
        if not self.online:
            raise RobotError('控制器不在线')

    def stop(self):
        with self.state_lock:
            self.stop_generation += 1
            self.cancel.set()
            self.latched = True
            try:
                self._write(b'!')
            except Exception:
                pass

    def unlock(self):
        if not self.online:
            raise RobotError('控制器不在线')
        with self.state_lock:
            generation = self.stop_generation
        self.request('stop_clear', allow_stopped=True)
        with self.state_lock:
            if self.stop_generation != generation:
                self.stop()
                raise Cancelled('解除停止期间又收到了停止，保持锁存')
            self.cancel.clear()
            self.latched = False

    def position(self, axis):
        with self.state_lock:
            item = self.counts.get(int(axis))
        if item is None or time.monotonic() - item[1] > self.cfg['position_age']:
            raise RobotError(f'轴 {axis} 回读缺失或过期')
        return item[0] / self.cfg['counts_per_pulse']

    def move_abs(self, axis, target):
        self.check_cancel()
        target = float(target)
        limit = self.cfg['limits'].get(str(axis))
        if not limit or not limit[0] <= target <= limit[1]:
            raise RobotError(f'轴 {axis} 目标 {target:.0f} 超出已确认软限位')
        position = self.position(axis)
        delta = round(target - position)
        if abs(delta) <= 2:
            return
        self.log(f'轴 {axis}：{position:.0f} → {target:.0f} 脉冲')
        self.request('motor_goto', int(axis), delta, int(self.cfg['tolerance_counts']),
                     int(self.cfg['rpm'][str(axis)]), timeout=float(self.cfg['move_timeout']))
        # Require a fresh post-reply sample, not a position cached before motion completion.
        stamp = time.monotonic()
        deadline = stamp + 2
        while time.monotonic() < deadline:
            self.check_cancel()
            sample = self.counts.get(int(axis))
            if sample and sample[1] >= stamp:
                error = abs(sample[0] - target * self.cfg['counts_per_pulse'])
                if error > self.cfg['tolerance_counts'] + self.cfg['counts_per_pulse'] * 2:
                    self.stop()
                    raise RobotError(f'轴 {axis} 回读与目标不一致：{error:.0f} counts')
                return
            self.cancel.wait(.025)
        self.stop()
        raise RobotError('闭环完成后没有收到新鲜位置回读')

    def angle_zero(self):
        self.position(3)
        self.request('motor_angle_zero', 3)
        self.angle_referenced = True

    def angle(self, degrees):
        if not self.angle_referenced:
            raise RobotError('先将 1 号盒对准落料位，再设置转盘角度零点')
        self.request('motor_angle', 3, round(float(degrees) * 100) % 36000,
                     int(self.cfg['rpm']['3']), timeout=15)

    def grip(self, action):
        if action not in ('open', 'ready', 'grab'):
            raise ValueError('未知夹爪动作')
        self.request('gripper', action)
        if self.cancel.wait(self.cfg['settle_seconds']):
            raise Cancelled('夹爪等待期间停止')
        self.check_cancel()

    def close(self):
        self.stop()
        self.shutdown.set()
        if self.transport:
            try:
                if isinstance(self.transport, socket.socket):
                    self.transport.shutdown(socket.SHUT_RDWR)
                self.transport.close()
            except OSError:
                pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
        self.transport = None
        self.online = False


class SimRobot:
    simulated = True

    def __init__(self, cfg, log=None):
        self.cfg, self.log = cfg, log or (lambda text: None)
        self.online = False
        self.latched = True
        self.firmware = 'SIMULATION'
        self.angle_referenced = False
        self.cancel = threading.Event()
        self.cancel.set()
        self.positions = {1: 0., 3: 0., 4: 0., 5: 0.}
        self.calls = []

    def connect(self):
        self.online = True

    def position(self, axis):
        if not self.online:
            raise RobotError('模拟控制器未连接')
        return self.positions[int(axis)]

    def check_cancel(self):
        if self.cancel.is_set() or self.latched:
            raise Cancelled('已停止')
        if not self.online:
            raise RobotError('控制器未连接')

    def unlock(self):
        self.cancel.clear()
        self.latched = False

    def stop(self):
        self.cancel.set()
        self.latched = True

    def request(self, cmd, *args, **kwargs):
        self.check_cancel()
        self.calls.append((cmd, *args))
        return {}

    def move_abs(self, axis, target):
        self.check_cancel()
        limit = self.cfg['limits'].get(str(axis))
        if not limit or not limit[0] <= target <= limit[1]:
            raise RobotError('模拟软限位拒绝')
        self.calls.append(('move', axis, target))
        self.log(f'[模拟] 轴 {axis} → {target:.0f} 脉冲')
        if self.cancel.wait(.06):
            raise Cancelled('模拟运动已停止')
        self.positions[axis] = float(target)

    def angle_zero(self):
        self.check_cancel()
        self.angle_referenced = True

    def angle(self, degrees):
        self.check_cancel()
        if not self.angle_referenced:
            raise RobotError('转盘未设零点')
        self.calls.append(('angle', float(degrees)))
        self.positions[3] = float(degrees) / 360 * 3200

    def grip(self, action):
        self.check_cancel()
        self.calls.append(('grip', action))

    def close(self):
        self.stop()
        self.online = False
