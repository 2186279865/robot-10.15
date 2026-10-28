import copy
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from workbench.automation import AutoSorter, DisplaySender, preflight
from workbench.cameras import demo_frame, RemoteCamera, REQUEST, HEADER, recv_exact
from workbench.dataset import benchmark, write_report
from workbench.models import VisualModel, parse_result
from workbench.robot import Cancelled, RobotClient, RobotError, SimRobot, decode_frame, encode_frame
from workbench.settings import DEFAULTS, demo_robot_config, load, save
from workbench.vision import VisionPipeline, color_name, geometric_shape, perspective_points


def reply(body):
    raw = body.encode('ascii')
    check = 0
    for b in raw:
        check ^= b
    return b'#' + raw + f'*{check:02X}\n'.encode()


class ProtocolTests(unittest.TestCase):
    def test_camera_binary_protocol_roundtrip(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        errors = []
        def serve():
            try:
                conn, _ = server.accept()
                with conn:
                    req = REQUEST.unpack(recv_exact(conn, REQUEST.size))
                    if req != (b'RQ', 1, 960, 720, 80):
                        errors.append(req)
                    _, jpeg = cv2.imencode('.jpg', demo_frame())
                    conn.sendall(HEADER.pack(b'IM', 1, 960, 720, 1, 1000, len(jpeg)) + jpeg.tobytes())
            except Exception as exc: errors.append(str(exc))
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        camera = RemoteCamera('127.0.0.1', server.getsockname()[1], 1, 960, 720)
        try:
            self.assertEqual(camera.read().shape, (720, 960, 3))
        finally:
            camera.close()
            server.close()
            thread.join(1)
        self.assertEqual(errors, [])

    def test_xor_and_corruption(self):
        frame = encode_frame(15, 'motor_goto', 4, -200, 200, 120)
        self.assertEqual(decode_frame(b'#' + frame[1:])[2], ['4', '-200', '200', '120'])
        with self.assertRaises(ValueError):
            decode_frame(reply('1,ok,x=1').replace(b'x=1', b'x=2'))

    def test_injection_and_frame_limit(self):
        for value in ['!', 'a\nb', 'a*b', 'x' * 100]:
            with self.assertRaises(ValueError):
                encode_frame(1, value)


class VisionTests(unittest.TestCase):
    def test_demo_stability_holes_and_coordinates(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg['camera']['top_points'] = [[0, 0], [1, 0], [1, 1], [0, 1]]
        pipeline = VisionPipeline(cfg)
        first, _ = pipeline.recognize(demo_frame())
        second, _ = pipeline.recognize(demo_frame())
        self.assertEqual(len(second), 5)
        self.assertFalse(any(x.stable for x in first))
        self.assertTrue(all(x.stable for x in second))
        self.assertEqual(len({x.track_id for x in second}), 5)
        self.assertEqual(sum(x.hollow == '镂空' for x in second), 1)
        self.assertTrue(all(0 <= q <= 1 for x in second for q in x.uv))

    def test_red_hue_wrap(self):
        hsv = np.zeros((60, 60, 3), np.uint8)
        hsv[:, :, 1:] = 220
        hsv[:30, :, 0] = 1
        hsv[30:, :, 0] = 178
        self.assertEqual(color_name(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), np.full((60, 60), 255, np.uint8))[0], '红')

    def test_rotated_rectangles(self):
        for angle in (0, 15, 45, 75, 110):
            mask = np.zeros((260, 260), np.uint8)
            cv2.fillConvexPoly(mask, cv2.boxPoints(((130, 130), (150, 60), angle)).astype(np.int32), 255)
            self.assertEqual(geometric_shape(mask)[0], '矩形截面体')

    def test_invalid_roi(self):
        with self.assertRaises(ValueError):
            perspective_points([[0, 0], [1, 1], [1, 0], [0, 1]], demo_frame())

    def test_model_parse_rejects_unknown_names(self):
        self.assertEqual(parse_result('```json\n{"shape":"菱面体","hollow":"unknown"}\n```', DEFAULTS['vision']['shapes'])['hollow'], '不确定')
        with self.assertRaises(ValueError):
            parse_result('{"shape":"随便猜"}', DEFAULTS['vision']['shapes'])

    def test_model_failure_preserves_detection_but_blocks_classification(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg['vision']['backend'] = 'local'
        p = VisionPipeline(cfg)
        with patch.object(p.model, 'classify', side_effect=RuntimeError('model unavailable')):
            items, _ = p.recognize(demo_frame())
        self.assertEqual(len(items), 5)
        self.assertTrue(all(t.shape == '不确定' and t.confidence == 0 for t in items))
        self.assertTrue(all(t.color != '不确定' for t in items))

    def test_local_and_cloud_wire_format(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size):
                answer = json.dumps({'shape': '三棱柱', 'hollow': '实心', 'visibility': '完整', 'color': '蓝'})
                return json.dumps({'message': {'content': answer}, 'choices': [{'message': {'content': answer}}]}).encode()
        for backend in ('local', 'cloud'):
            with patch.dict('os.environ', {'DASHSCOPE_API_KEY': 'unit-test-only'}), patch('urllib.request.urlopen', return_value=Response()) as http:
                result = VisualModel(DEFAULTS['vision']).classify(demo_frame(), demo_frame(), backend)
                payload = json.loads(http.call_args.args[0].data)
                if backend == 'local':
                    self.assertEqual(len(payload['messages'][0]['images']), 2)
                    self.assertEqual(payload['format']['properties']['shape']['enum'], DEFAULTS['vision']['shapes'])
                    self.assertFalse(payload['think'])
                else:
                    self.assertEqual(len(payload['messages'][0]['content']), 3)
                self.assertEqual(result['shape'], '三棱柱')


class FastRobot(SimRobot):
    def move_abs(self, axis, target):
        self.check_cancel()
        self.calls.append(('move', axis, target))
        self.positions[axis] = target


class AutomationTests(unittest.TestCase):
    def test_display_requires_positive_acknowledgement(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        self.cfg.update(display_required=True, display_host='127.0.0.1', display_port=server.getsockname()[1])
        def serve():
            conn, _ = server.accept()
            with conn:
                data = b''
                while not data.endswith(b'\n'): data += conn.recv(65536)
                conn.sendall(b'NO\n')
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with self.assertRaises(RobotError): DisplaySender(self.cfg).push({'name': 'test'}, demo_frame())
        finally:
            server.close()
            thread.join(1)

    def setUp(self):
        self.cfg = demo_robot_config()
        self.robot = FastRobot(self.cfg)
        self.robot.connect()
        self.robot.unlock()
        self.robot.angle_zero()
        cfg = copy.deepcopy(DEFAULTS)
        cfg['camera']['top_points'] = [[0, 0], [1, 0], [1, 1], [0, 1]]
        p = VisionPipeline(cfg)
        p.recognize(demo_frame())
        self.items, _ = p.recognize(demo_frame())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sorter = AutoSorter(self.robot, self.cfg, lambda: [])
        self.sorter.log_path = Path(self.tmp.name) / 'events.jsonl'

    def test_absolute_tray_angles_and_corridor_order(self):
        for item in (self.items[0], self.items[1], self.items[0]):
            self.sorter.pick_one(item)
        self.assertEqual([x[1] for x in self.robot.calls if x[0] == 'angle'], [0, 60, 0])
        moves = [x for x in self.robot.calls if x[0] == 'move'][:10]
        self.assertEqual(moves[:2], [('move', 5, self.cfg['route']['p1_y']), ('move', 1, self.cfg['route']['z_high'])])
        self.assertEqual(moves[-2:], [('move', 1, 0), ('move', 5, 0)])
        self.assertEqual(self.sorter.placed, 3)

    def test_whole_path_limit_before_first_motion(self):
        self.cfg['limits']['1'] = [-100, 100]
        with self.assertRaises(RobotError): self.sorter.pick_one(self.items[0])
        self.assertEqual(self.robot.calls, [])

    def test_stale_and_unconfirmed_rejected(self):
        self.items[0].measured_at -= 10
        with self.assertRaises(RobotError): preflight(self.robot, self.cfg, self.items[0])
        self.items[0].measured_at = time.time()
        self.cfg['reference_confirmed'] = False
        with self.assertRaises(RobotError): preflight(self.robot, self.cfg, self.items[0])

    def test_cancel_latches_without_unlock(self):
        def stop_on_capture():
            self.robot.stop()
            return self.items
        self.sorter.capture = stop_on_capture
        with self.assertRaises(Cancelled): self.sorter.run(1)
        self.assertEqual(self.sorter.state, 'FAULT')
        self.assertTrue(self.robot.latched)
        self.assertEqual(self.robot.calls, [])

    def test_display_required_before_motion(self):
        self.cfg['display_required'] = True
        with self.assertRaises(RobotError): self.sorter.pick_one(self.items[0])
        self.assertEqual(self.robot.calls, [])

    def test_geometry_blocks_real_automatic_classification(self):
        self.robot.simulated = False
        with self.assertRaises(RobotError): preflight(self.robot, self.cfg, self.items[0])


class FakeFirmware:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.endpoint = 'tcp://127.0.0.1:' + str(self.listener.getsockname()[1])
        self.stop = threading.Event()
        self.commands = []
        self.age_ms = 0
        self.readback = 1
        self.counts = {1: 0, 3: 0, 4: 0, 5: 0}
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            conn, _ = self.listener.accept()
            with conn:
                conn.settimeout(.02)
                buf = b''
                while not self.stop.is_set():
                    try:
                        data = conn.recv(4096)
                        if not data: break
                        buf += data.replace(b'!', b'')
                        while b'\n' in buf:
                            line, buf = buf.split(b'\n', 1)
                            rid, cmd, args, _ = decode_frame(b'#' + line[1:])
                            self.commands.append(cmd)
                            extra = ',fw=test,boot=1,readback=1/3/4/5' if cmd == 'hello' else ''
                            if cmd == 'motor_goto':
                                axis, pulses = map(int, args[:2])
                                self.counts[axis] += round(pulses * 20.48)
                            conn.sendall(reply(f'{rid},ok{extra}'))
                    except socket.timeout:
                        pass
                    for axis, counts in self.counts.items():
                        conn.sendall(reply(f'0,tele,addr={axis},readback={self.readback},counts={counts},age_ms={self.age_ms}'))
                    time.sleep(.02)
        except OSError:
            pass

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(1)


class TransportTests(unittest.TestCase):
    def test_stop_during_unlock_cannot_rearm(self):
        client = RobotClient(demo_robot_config())
        client.online = True
        client.request = lambda *args, **kwargs: client.stop()
        with self.assertRaises(Cancelled): client.unlock()
        self.assertTrue(client.latched)
        self.assertTrue(client.cancel.is_set())

    def test_demo_calibration_cannot_connect_real_hardware(self):
        with self.assertRaises(RobotError): RobotClient(demo_robot_config()).connect()

    def test_closed_loop_readback_and_latched_stop(self):
        firmware = FakeFirmware()
        cfg = demo_robot_config()
        cfg['endpoint'] = firmware.endpoint
        cfg.pop('simulation_only')  # This test uses a loopback firmware emulator.
        client = RobotClient(cfg)
        try:
            client.connect()
            self.assertTrue(client.latched)
            with self.assertRaises(Cancelled): client.move_abs(4, 100)
            client.unlock()
            time.sleep(.1)
            client.move_abs(4, 100)
            self.assertAlmostEqual(client.position(4), 100, places=1)
            client.stop()
            with self.assertRaises(Cancelled): client.grip('open')
            self.assertEqual(firmware.commands.count('stop_clear'), 1)
            firmware.age_ms = 8000
            time.sleep(.15)
            with self.assertRaises(RobotError): client.position(4)
            firmware.age_ms, firmware.readback = 0, 0
            time.sleep(.15)
            with self.assertRaises(RobotError): client.position(4)
        finally:
            client.close()
            firmware.close()


class ConfigTests(unittest.TestCase):
    def test_restart_does_not_restore_reference_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = copy.deepcopy(DEFAULTS)
            cfg['robot'] = demo_robot_config()
            path = Path(tmp) / 'config.json'
            save(cfg, path)
            self.assertFalse(load(path)['robot']['reference_confirmed'])

    def test_failed_api_counts_as_wrong_and_no_truth_is_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = write_report([dict(backend='cloud', error='no key', elapsed_ms=1, expected_shape='三棱柱')], Path(tmp) / 'report.json')
            self.assertEqual(summary['cloud']['shape_accuracy'], 0)
            self.assertIsNone(summary['cloud']['color_accuracy'])


if __name__ == '__main__':
    unittest.main()
