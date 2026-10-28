"""Offline reproduction of legacy host issues; never opens a device or socket."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / '01-交付包' / 'host'
sys.path.insert(0, str(HOST))

import prep_pose
from host_client import HostClient
from host_gui import AxisPanel


class FakeClient:
    def __init__(self):
        self.calls = []

    def send(self, *args, **kwargs):
        self.calls.append(args)
        return len(self.calls)

    def goto(self, axis, pulses, **kwargs):
        return self.send('motor_goto', axis, pulses)

    def wait_reply(self, *args, **kwargs):
        return {'kind': 'reply', 'code': '', 'pairs': {}}


def main():
    limits = json.loads((HOST / 'limits.json').read_text(encoding='utf-8'))
    fake = FakeClient()
    result = prep_pose.move_abs(fake, {5: -90000}, counts={5: 0})
    panel = SimpleNamespace(
        app=SimpleNamespace(client=SimpleNamespace(now_ms=lambda: 100000)),
        refresh_readback=lambda: None,
    )
    AxisPanel.update_telemetry(panel, {
        'readback': '1', 'counts': '12345', 'age_ms': '60000',
    })
    client = HostClient()
    client._remember_done(2, 'reply', pairs={'old': 'yes'})
    client.begin_handshake()
    new_id = client.send('motor_query', 5)
    affine = json.loads((ROOT / '01-交付包' / 'vision' / 'tray_calib.json')
                        .read_text(encoding='utf-8'))['affine']
    ys = [affine['c'] * u + affine['d'] * v + affine['ty']
          for u in (0, 1) for v in (0, 1)]
    print(json.dumps({
        'hardware_access': False,
        'out_of_limits_target': -90000,
        'configured_y_limits': limits['5'],
        'out_of_limits_target_accepted': bool(result and result[0]['ok']),
        'commands_recorded_by_fake': fake.calls,
        'firmware_sample_age_ms': 60000,
        'gui_perceived_sample_age_ms': 100000 - panel.last_counts_ms,
        'new_request_id_after_handshake': new_id,
        'old_result_returned_for_new_request': client.done_of(new_id),
        'calibration_y_corner_range': [min(ys), max(ys)],
        'box_1_to_2_to_3_relative_arguments': [60, 60],
        'absolute_angle_interface_targets': [60, 60],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
