"""pi_link 的单元测试：把「派」当成一个可选端口。

不碰真实网络：只测文本解析、去重、环境变量与「上次连过的 IP」落盘。
（discover() 只断言"不抛异常、返回 list"，超时给 50 ms。）
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = HERE.parent
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

import pi_link  # noqa: E402


class PiLinkTests(unittest.TestCase):
    def test_label_contains_host_and_ports(self):
        ep = pi_link.PiEndpoint('192.168.100.198')
        text = ep.label()
        self.assertIn('192.168.100.198', text)
        self.assertIn('5057', text)
        self.assertIn('5058', text)
        self.assertTrue(text.startswith('派 '))

    def test_device_is_tcp_endpoint(self):
        ep = pi_link.PiEndpoint('10.0.0.7', ser_port=5058)
        self.assertEqual(ep.device, 'tcp://10.0.0.7:5058')

    def test_endpoint_from_text(self):
        ep = pi_link.endpoint_from_text('tcp://192.168.100.198:5058')
        self.assertIsNotNone(ep)
        self.assertEqual(ep.host, '192.168.100.198')
        self.assertEqual(ep.ser_port, 5058)
        self.assertEqual(ep.source, '手填')
        self.assertIsNone(pi_link.endpoint_from_text('COM6'))
        self.assertIsNone(pi_link.endpoint_from_text(''))
        # 只给 IP 时用默认串口端口
        ep2 = pi_link.endpoint_from_text('tcp://1.2.3.4')
        self.assertEqual(ep2.ser_port, pi_link.DEFAULT_SER_PORT)

    def test_merge_dedups_and_keeps_found_first(self):
        found = [pi_link.PiEndpoint('192.168.100.198', name='robot-pi')]
        last = pi_link.PiEndpoint('192.168.100.198', source='上次')
        other = pi_link.PiEndpoint('192.168.100.199', source='上次')
        merged = pi_link.merge(found, last)
        self.assertEqual([ep.host for ep in merged], ['192.168.100.198'])
        self.assertEqual(merged[0].source, '发现')
        merged2 = pi_link.merge(found, other)
        self.assertEqual([ep.host for ep in merged2],
                         ['192.168.100.198', '192.168.100.199'])
        self.assertEqual(pi_link.merge([], None), [])

    def test_apply_and_clear_env(self):
        keep = dict(**_backup())
        try:
            ep = pi_link.PiEndpoint('192.168.100.198', img_port=5057, ser_port=5058)
            pi_link.apply_env(ep)
            import os
            self.assertEqual(os.environ['ROBOT_PI'], '192.168.100.198')
            self.assertEqual(os.environ['ROBOT_PI_IMG_PORT'], '5057')
            self.assertEqual(os.environ['ROBOT_PI_SER_PORT'], '5058')
            pi_link.clear_env()
            self.assertNotIn('ROBOT_PI', os.environ)
        finally:
            _restore(keep)

    def test_last_endpoint_roundtrip(self):
        old = pi_link.LAST_FILE
        with tempfile.TemporaryDirectory() as tmp:
            pi_link.LAST_FILE = Path(tmp) / 'pi_last.json'
            try:
                self.assertIsNone(pi_link.load_last())
                ep = pi_link.PiEndpoint('192.168.100.198', name='robot-pi')
                pi_link.save_last(ep)
                got = pi_link.load_last()
                self.assertIsNotNone(got)
                self.assertEqual(got.host, '192.168.100.198')
                self.assertEqual(got.ser_port, 5058)
                self.assertEqual(got.source, '上次')
            finally:
                pi_link.LAST_FILE = old

    def test_discover_does_not_raise(self):
        result = pi_link.discover(timeout=0.05)
        self.assertIsInstance(result, list)
        for ep in result:
            self.assertIsInstance(ep, pi_link.PiEndpoint)
            self.assertTrue(ep.host)


def _backup():
    import os
    return {k: os.environ.get(k) for k in
            ('ROBOT_PI', 'ROBOT_PI_IMG_PORT', 'ROBOT_PI_SER_PORT')}


def _restore(keep):
    import os
    for key, value in keep.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


if __name__ == '__main__':
    unittest.main(verbosity=2)
