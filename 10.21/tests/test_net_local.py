#!/usr/bin/env python
"""10.21 笔记本端「WiFi 链路」本机自测（不需要树莓派、不需要 pytest）。

跑法（必须用带 cv2 的解释器）：:

    Qwen\\.venv\\Scripts\\python.exe 10.21\\tests\\test_net_local.py
    Qwen\\.venv\\Scripts\\python.exe 10.21\\tests\\test_net_local.py --mock   # 强制用内置 mock

它验证两件事（对应 host/ 与 vision/ 的两条网络路径）：

(a) 图传：``vision/remote_camera.py`` 的 RemoteCamera 经 ``common/wire.py``
    的拉模式协议，从 127.0.0.1:15057 连取 3 帧，尺寸必须是请求的 640x480、
    能被 cv2.imdecode 解码。
(b) 串口桥：``host/serial_link.py::open_best_link('tcp://127.0.0.1:15058')``
    返回 SocketLink；用真实的 ``host/host_client.py::HostClient(service=False)``
    完成 hello 握手（在线），再对 1/3/4/5 四轴各发一次 motor_query 并收到 counts。

(c) 失败不崩：远程不可达时 ``make_camera`` 走 RemoteCamera、``gui.Stream``
    的预览线程不异常退出（对应验收「取帧失败要显示错误而不是崩溃」）。
(d) 错误帧：派端回 ER 包（6 字节错误头 + 文本体）时，客户端必须按
    ``wire.ERR_HDR_LEN`` 收头、毫秒级报错、不断连——防止把 ER 头误当
    24 字节图传头导致“卡 5 s 超时”。

默认优先启动真实目标 ``10.21/pi/pi_agent.py --fake-camera --fake-serial ...``；
若该文件还不存在（或在 12 s 内没把端口监听起来），自动退回本文件内置的
迷你 mock 服务器，并打印 WARN 说明「本次只证明笔记本端正确」。

端口只用 15057/15058/15059，绝不碰真机的 5056-5058。
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent       # 10.21/tests
PKG = HERE.parent                            # 10.21
IMG_PORT = 15057
SER_PORT = 15058
ER_PORT = 15059                              # 只回 ER 错误包的小服务器
PI_AGENT = PKG / 'pi' / 'pi_agent.py'
WIDTH, HEIGHT = 640, 480
AXES = (1, 3, 4, 5)

for _p in (str(PKG / 'common'), str(PKG / 'vision'), str(PKG / 'host')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cv2                                  # noqa: E402
import numpy as np                          # noqa: E402

import wire                                 # noqa: E402
import protocol as P                        # noqa: E402

FAILED: list = []                           # [(name, text)]


def ok(name: str) -> None:
    print('PASS  %s' % name, flush=True)


def fail(name: str, exc: BaseException | str) -> None:
    text = exc if isinstance(exc, str) else ''.join(
        traceback.format_exception_only(type(exc), exc)).strip()
    print('FAIL  %s：%s' % (name, text), flush=True)
    FAILED.append((name, text))


def wait_port(port: int, timeout: float = 12.0, host: str = '127.0.0.1') -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def listen(port: int, backlog: int = 8):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', int(port)))
    srv.listen(backlog)
    srv.settimeout(0.5)
    return srv


# ---------------------------------------------------------------- 内置 mock
def _fake_frame(w: int, h: int) -> bytes:
    """640x480 渐变图，压成 JPEG——足以证明「收字节 → imdecode → 尺寸对」。"""
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:, :, 0] = np.linspace(0, 255, w, dtype=np.uint8)[None, :]
    frame[:, :, 1] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    frame[:, :, 2] = 120
    okc, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not okc:
        raise RuntimeError('mock JPEG 编码失败')
    return buf.tobytes()


def _fake_firmware_line(frame: P.Frame) -> bytes:
    """上位机文本协议的假固件：hello / motor_query / gripper / param_* / ping。"""
    rid = frame.req_id
    cmd = frame.cmd
    args = [str(a) for a in frame.args]
    if cmd == 'hello':
        return P.encode_ok(rid, fw='sim-net', boot='SIMBOOT',
                           axes='1/3/4/5', readback='1/3/4/5')
    if cmd == 'motor_query':
        addr = args[0] if args else '1'
        what = args[1] if len(args) > 1 else 'pos'
        return P.encode_ok(rid, addr=addr, what=what, counts='1234')
    if cmd == 'gripper':
        action = args[0] if args else 'open'
        angle = {'open': '180', 'ready': '150', 'grab': '135',
                 'step_grab': '120', 'step_release': '165'}.get(action, '180')
        return P.encode_ok(rid, action=action, angle=angle, settle_ms='200')
    if cmd == 'param_get':
        name = args[0] if args else ''
        value = {'gripper_ready_deg': '150',
                 'gripper_hold_max_ms': '0'}.get(name, '0')
        return P.encode_ok(rid, name=name, value=value)
    if cmd == 'param_set':
        name = args[0] if args else ''
        value = args[1] if len(args) > 1 else '0'
        return P.encode_ok(rid, name=name, before='0', value=value)
    return P.encode_ok(rid)


class MockPi:
    """50 行左右的「假树莓派」：15057 图传 + 15058 串口桥假固件。"""

    def __init__(self, img_port: int, ser_port: int) -> None:
        self.img_port = img_port
        self.ser_port = ser_port
        self._running = False
        self._srvs: list = []
        self._conns: set = set()

    def start(self) -> None:
        self._running = True
        for port, handler in ((self.img_port, self._img_conn),
                              (self.ser_port, self._ser_conn)):
            srv = listen(port)
            self._srvs.append(srv)
            threading.Thread(target=self._accept, args=(srv, handler),
                             daemon=True).start()

    def stop(self) -> None:
        self._running = False
        for srv in self._srvs:
            try:
                srv.close()
            except OSError:
                pass
        for conn in list(self._conns):
            try:
                conn.close()
            except OSError:
                pass
        self._srvs.clear()
        self._conns.clear()

    def _accept(self, srv, handler) -> None:
        while self._running:
            try:
                conn, _addr = srv.accept()
            except OSError:
                continue
            self._conns.add(conn)
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    def _img_conn(self, conn) -> None:
        conn.settimeout(5.0)
        seq = 0
        try:
            while self._running:
                head = wire.recv_exact(conn, wire.REQ_LEN)
                req = wire.parse_request(head)
                w = int(req['width'] or WIDTH)
                h = int(req['height'] or HEIGHT)
                seq += 1
                conn.sendall(wire.pack_image(
                    req['cam'], w, h, seq, int(time.time() * 1e6),
                    _fake_frame(w, h)))
        except Exception:
            pass
        finally:
            self._conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _ser_conn(self, conn) -> None:
        buf = P.FramingBuffer()
        conn.settimeout(0.5)
        try:
            while self._running:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                for inbound in buf.feed(chunk):
                    if inbound.ok and inbound.frame is not None and inbound.frame.is_request:
                        conn.sendall(_fake_firmware_line(inbound.frame))
        except Exception:
            pass
        finally:
            self._conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------- ER 包服务器
class ErrPacketServer:
    """只回错误包的迷你服务器：验证 ERR_HDR_LEN(6) 与 IMG_HDR_LEN(24) 不能混读。

    协议里图传头 24 字节、错误头 6 字节 + 文本体。若客户端一律按 24 字节收头，
    错误文本会被多吞掉 18 字节，再按 body 长度读就会卡到 socket 超时
    （表现为 `取帧失败：timed out` 而不是「派端报错」）。
    """

    def __init__(self, port: int, text: str = '模拟错误：相机 1 未配置（可选：0）') -> None:
        self.port = int(port)
        self.text = text
        self._srv = None
        self._stop = threading.Event()
        self._threads = []

    def start(self) -> None:
        self._srv = listen(self.port)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=1.0)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _peer = self._srv.accept()
            except (OSError, AttributeError):
                if self._stop.is_set():
                    return
                time.sleep(0.05)
                continue
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._threads.append(t)
            t.start()

    def _serve(self, conn) -> None:
        packet = wire.pack_error(wire.E_NO_CAMERA, self.text)
        try:
            while not self._stop.is_set():
                wire.recv_exact(conn, wire.REQ_LEN)   # 每来一个请求就回一个 ER 包
                conn.sendall(packet)
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------- 真目标
class PiAgent:
    """把 10.21/pi/pi_agent.py 当子进程跑起来（stdout 后台排空，防管道撑死）。"""

    def __init__(self) -> None:
        self.proc = None
        self.lines: list = []

    def start(self) -> bool:
        if not PI_AGENT.is_file():
            print('WARN  10.21/pi/pi_agent.py 还不存在，改用内置 mock', flush=True)
            return False
        cmd = [sys.executable, str(PI_AGENT), '--fake-camera', '--fake-serial',
               '--img-port', str(IMG_PORT), '--ser-port', str(SER_PORT),
               '--no-discovery']
        print('启动：' + ' '.join(cmd), flush=True)
        self.proc = subprocess.Popen(
            cmd, cwd=str(PKG), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1)
        threading.Thread(target=self._drain, daemon=True).start()
        return True

    def _drain(self) -> None:
        try:
            for line in self.proc.stdout:          # type: ignore[union-attr]
                line = line.rstrip()
                if line:
                    self.lines.append(line)
                    if len(self.lines) > 200:
                        del self.lines[:100]
        except Exception:
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def tail(self, n: int = 12) -> str:
        return '\n'.join('    [pi_agent] ' + x for x in self.lines[-n:])

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        finally:
            self.proc = None


# ---------------------------------------------------------------- 测试项
def test_image() -> None:
    name = '(a) 图传：RemoteCamera 连取 3 帧 640x480'
    try:
        from remote_camera import RemoteCamera
        cam = RemoteCamera(0, WIDTH, HEIGHT, 15, '127.0.0.1', IMG_PORT)
        cam.open()
        try:
            for i in range(3):
                frame = cam.read()
                assert frame is not None, '第 %d 帧为空' % (i + 1)
                assert frame.shape == (HEIGHT, WIDTH, 3), \
                    '第 %d 帧尺寸 %r != %r' % (i + 1, frame.shape, (HEIGHT, WIDTH, 3))
                assert frame.dtype == np.uint8, 'dtype=%r' % frame.dtype
                # 再独立解一次 JPEG 语义等价：imdecode 能解即编码完整
                assert frame.size == WIDTH * HEIGHT * 3
            meta = cam.cap.last_meta
            for key in ('cam', 'width', 'height', 'seq', 'n'):
                assert key in meta, '帧头缺字段 %r' % key
            assert int(meta['cam']) == 0, 'cam id 回显错：%r' % meta['cam']
            assert int(meta['width']) == WIDTH and int(meta['height']) == HEIGHT
        finally:
            cam.close()
        ok(name)
    except Exception as exc:                      # noqa: BLE001
        fail(name, exc)


def _pump(client, link, seconds: float, until=None) -> bool:
    """service=False 时手动驱动 HostClient：read → feed_bytes → tick。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        data = link.read()
        if data:
            client.feed_bytes(data)
        client.tick()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


def test_serial() -> None:
    name = '(b) 串口桥：SocketLink + HostClient 握手 + 四轴 motor_query + 夹爪 ready/新参数'
    link = None
    try:
        import serial_link
        from host_client import HostClient

        link = serial_link.open_best_link('tcp://127.0.0.1:%d' % SER_PORT)
        assert isinstance(link, serial_link.SocketLink), \
            'open_best_link 没返回 SocketLink：%r' % (link,)
        desc = link.describe()
        assert desc.startswith('TCP'), 'describe()=%r' % desc
        assert link.is_simulated is False

        client = HostClient(link, service=False)
        assert client.connect() is True, 'connect() 返回 False'
        assert _pump(client, link, 5.0, lambda: client.online), \
            '5 s 内没完成 hello 握手（state=%s）' % client.state
        assert client.fw_version, 'hello 应答没带 fw'
        assert client.readback_axes, 'hello 应答没带 readback'

        for axis in AXES:
            client.send('motor_query', axis, 'pos')
            assert _pump(client, link, 3.0,
                         lambda a=axis: a in client.last_counts), \
                '轴 %d 的 motor_query 没拿到 counts（last_counts=%r）'
            entry = client.last_counts[axis]
            assert entry['counts'] == 1234 or isinstance(entry['counts'], int)

        def _reply_pairs(cmd, *cmd_args, seconds=3.0):
            """发一条命令并等待同一 cmd 的 ok 应答（返回 pairs dict）。"""
            client.send(cmd, *cmd_args)
            found = None
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and found is None:
                for ev in client.poll_events(200):
                    if ev.kind == 'reply' and ev.data.get('cmd') == cmd:
                        found = ev.data.get('pairs') or {}
                _pump(client, link, 0.02)
            return found

        # 夹爪三档：经串口桥发 ready，假固件回 action=ready,angle=150,settle_ms=200
        grip = _reply_pairs('gripper', 'ready')
        assert grip is not None, 'gripper ready 没收到 ok 应答'
        assert grip.get('action') == 'ready', grip
        assert grip.get('angle') == '150', grip
        assert grip.get('settle_ms') == '200', grip
        # 新参数：预备位默认 150，hold_max_ms 可 set 到 5000
        ready_deg = _reply_pairs('param_get', 'gripper_ready_deg')
        assert ready_deg is not None and ready_deg.get('value') == '150', ready_deg
        hold_set = _reply_pairs('param_set', 'gripper_hold_max_ms', 5000)
        assert hold_set is not None and hold_set.get('value') == '5000', hold_set
        if link is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        ok(name)
    except Exception as exc:                      # noqa: BLE001
        fail(name, exc)
    finally:
        if link is not None:
            try:
                link.close()
            except Exception:
                pass


def test_error_frame() -> None:
    """(d) 派端 ER 错误帧：按 ERR_HDR_LEN 收包，不超时、不断连、错误信息看得见。"""
    name = '(d) 派端 ER 错误帧：6 字节错误头不误读，不超时不断连'
    server = None
    cam = None
    try:
        from remote_camera import RemoteCamera

        server = ErrPacketServer(ER_PORT)
        server.start()
        assert wait_port(ER_PORT, 5.0), 'ER 测试服务器没起来'

        cam = RemoteCamera(1, WIDTH, HEIGHT, 15, '127.0.0.1', ER_PORT)
        cam.open()

        t0 = time.monotonic()
        got = cam.cap.grab()
        cost = time.monotonic() - t0
        assert got is False, 'ER 包不该被当成成功帧'
        # 修好前这里是 5 s 的 socket 超时；正确实现应在毫秒级返回
        assert cost < 1.0, '收 ER 包耗时 %.2f s（疑似把 6 字节头当 24 字节读）' % cost
        err = cam.cap.last_error
        assert '派端报错' in err, '错误信息没透出：%r' % err
        assert '未配置' in err, '错误文本没解码出来（body 读错位？）：%r' % err
        assert 'timed out' not in err.lower(), '把 ER 包当超时了：%r' % err

        # 服务端不关连接，所以同一个 socket 还能继续用：再拉一次仍快速报错
        t1 = time.monotonic()
        assert cam.cap.grab() is False
        cost2 = time.monotonic() - t1
        assert cost2 < 1.0, '第二次收 ER 包耗时 %.2f s（socket 可能已错位）' % cost2
        assert cam.cap.isOpened() is True, '收 ER 包后不该断开连接'
        ok(name)
    except Exception as exc:                      # noqa: BLE001
        fail(name, exc)
    finally:
        if cam is not None:
            try:
                cam.close()
            except Exception:
                pass
        if server is not None:
            server.stop()


def test_cam_map() -> None:
    """(e) 相机编号映射：本机设备号 1/2 → 派端槽位 0/1（不映射会两路都报"未配置"）。"""
    name = '(e) 相机编号映射：ROBOT_PI_CAM_MAP / remote.cam_map 把 1→槽0、2→槽1'
    old_pi = os.environ.get('ROBOT_PI')
    old_map = os.environ.get('ROBOT_PI_CAM_MAP')
    try:
        import camera_factory as CF

        os.environ['ROBOT_PI_CAM_MAP'] = '1:0,2:1'
        assert CF.cam_map() == {1: 0, 2: 1}, 'cam_map 解析错：%r' % (CF.cam_map(),)

        os.environ['ROBOT_PI'] = '127.0.0.1'
        top = CF.make_camera(1, WIDTH, HEIGHT, 15)
        side = CF.make_camera(2, WIDTH, HEIGHT, 15)
        assert top.index == 0, '顶视应映射到派端槽位 0，实际 %r' % (top.index,)
        assert side.index == 1, '侧视应映射到派端槽位 1，实际 %r' % (side.index,)

        # 没有映射时保持原编号（默认行为不变）
        os.environ.pop('ROBOT_PI_CAM_MAP', None)
        saved = CF._load_config
        CF._load_config = lambda: {}
        try:
            plain = CF.make_camera(2, WIDTH, HEIGHT, 15)
        finally:
            CF._load_config = saved
        assert plain.index == 2, '没有映射时不该改编号，实际 %r' % (plain.index,)
        ok(name)
    except Exception as exc:                      # noqa: BLE001
        fail(name, exc)
    finally:
        for key, val in (('ROBOT_PI', old_pi), ('ROBOT_PI_CAM_MAP', old_map)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def test_failure_is_not_a_crash() -> None:
    """(c) 连不上时：视觉页的预览线程必须活着、不抛、可停止（验收要求的“不崩溃”）。"""
    name = '(c) 远程不可达：make_camera→RemoteCamera，GUI 预览线程不崩'
    old_pi = os.environ.get('ROBOT_PI')
    old_port = os.environ.get('ROBOT_PI_IMG_PORT')
    try:
        os.environ['ROBOT_PI'] = '127.0.0.1'
        os.environ['ROBOT_PI_IMG_PORT'] = '15999'   # 没人监听

        from camera_factory import make_camera
        cam = make_camera(0, WIDTH, HEIGHT, 15)
        assert type(cam).__name__ == 'RemoteCamera', \
            'make_camera 没用远程相机：%r' % (cam,)
        cam.open()                                   # 连不上也不许抛
        assert cam.cap.grab() is False               # 失败返回 False
        try:
            cam.read()
        except RuntimeError as exc:
            assert '读取失败' in str(exc), '错误文案不明确：%s' % exc
        else:
            raise AssertionError('连不上时 read() 应抛 RuntimeError')
        cam.close()

        # 视觉页真正用的路径：gui.Stream（默认相机由 camera_factory 决定）
        from gui import Stream
        stream = Stream(0, WIDTH, HEIGHT, 15)
        stream.start()
        time.sleep(1.5)                              # 让它经历若干次“连不上”
        finally_alive = not hasattr(stream, 'error')  # run() 只有异常退出才写 error
        frames = stream.frames
        stream.close()
        time.sleep(0.2)
        assert finally_alive, '预览线程异常退出：%s' % getattr(stream, 'error', '')
        assert frames == 0, '根本没有图传却收到了 %d 帧' % frames
        ok(name)
    except Exception as exc:                          # noqa: BLE001
        fail(name, exc)
    finally:
        if old_pi is None:
            os.environ.pop('ROBOT_PI', None)
        else:
            os.environ['ROBOT_PI'] = old_pi
        if old_port is None:
            os.environ.pop('ROBOT_PI_IMG_PORT', None)
        else:
            os.environ['ROBOT_PI_IMG_PORT'] = old_port


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='10.21 网络链路本机自测')
    ap.add_argument('--mock', action='store_true', help='强制用内置 mock（不启 pi_agent）')
    args = ap.parse_args(argv)

    print('python :', sys.version.split()[0], '|', sys.executable, flush=True)
    print('cv2    :', cv2.__version__, '| numpy:', np.__version__, flush=True)
    print('wire   : IMG_PORT=%d SER_PORT=%d' % (wire.IMG_PORT, wire.SER_PORT), flush=True)

    agent = None
    mock = None
    real = False
    try:
        if not args.mock:
            agent = PiAgent()
            if agent.start():
                real = wait_port(IMG_PORT) and wait_port(SER_PORT)
                if real:
                    print('pi_agent 已就绪（15057/15058）', flush=True)
                else:
                    print('WARN  pi_agent 12 s 内没监听端口，进程状态=%r'
                          % (None if agent.proc is None else agent.proc.poll()), flush=True)
                    print(agent.tail(), flush=True)
                    agent.stop()
        if not real:
            mock = MockPi(IMG_PORT, SER_PORT)
            mock.start()
            if not (wait_port(IMG_PORT, 5.0) and wait_port(SER_PORT, 5.0)):
                print('FAIL  内置 mock 起不来（端口被占用？）', flush=True)
                return 1
            print('WARN  本次跑的是内置 mock：只证明笔记本端实现正确，'
                  '不代表 pi_agent 联调通过', flush=True)

        test_image()
        test_serial()
        test_error_frame()
        test_cam_map()
        test_failure_is_not_a_crash()
    finally:
        if mock is not None:
            mock.stop()
        if agent is not None:
            agent.stop()

    total = 5
    passed = total - len(FAILED)
    print('\n结果：%d/%d PASS%s' % (
        passed, total, '' if not FAILED else '，失败项：' + '；'.join(n for n, _ in FAILED)),
        flush=True)
    if real:
        print('（本轮用真实 pi/pi_agent.py 联调）', flush=True)
    return 0 if not FAILED else 1


if __name__ == '__main__':
    raise SystemExit(main())
