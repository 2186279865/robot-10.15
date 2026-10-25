"""auto_runner 状态机单测（假后端，不碰硬件）。"""
import unittest

import auto_runner as AR


def make_runner(targets=None, route=None, limits=None, max_items=50):
    bench = AR.FakeBackend(targets=targets)
    runner = AR.AutoRunner(
        bench,
        route=route if route is not None else {
            'corridor': [{'axis': 5, 'pulses': -800, 'rpm': 300},
                         {'axis': 1, 'pulses': 9000, 'rpm': 300}],
            'rpm': 300, 'z_low': 0, 'z_pick': 2000, 'z_high': 9000,
        },
        limits=limits if limits is not None else {},
        max_items=max_items,
    )
    return runner, bench


class RunnerTests(unittest.TestCase):
    def test_one_item_reaches_done(self):
        runner, bench = make_runner()
        state = runner.run()
        self.assertEqual(state, AR.DONE)
        self.assertEqual(runner.placed, 1)
        self.assertEqual(runner.done_reason, '抓取区没有目标了')
        kinds = [c[0] for c in bench.calls]
        # 关键顺序：capture → goto(通道出) → goto(对准) → grab → lift → 通道回 → open
        self.assertIn('capture', kinds)
        self.assertIn('gripper', kinds)
        grab = kinds.index('gripper')
        self.assertEqual(bench.calls[grab][1], 'grab')
        opened = [c for c in bench.calls if c[0] == 'gripper' and c[1] == 'open']
        self.assertEqual(len(opened), 1)

    def test_corridor_out_then_back_reversed(self):
        runner, bench = make_runner()
        runner.run()
        gotos = [c for c in bench.calls if c[0] == 'goto']
        corridor = [(5, -800), (1, 9000)]
        first_two = [(gotos[0][1], gotos[0][2]), (gotos[1][1], gotos[1][2])]
        self.assertEqual(first_two, corridor)
        last_two = [(gotos[-1][1], gotos[-1][2]), (gotos[-2][1], gotos[-2][2])]
        self.assertEqual(last_two, [(5, 800), (1, -9000)])

    def test_soft_limit_fault(self):
        runner, bench = make_runner(limits={'5': {'min': -100, 'max': 100}})
        state = runner.run()
        self.assertEqual(state, AR.FAULT)
        self.assertIn('E15', runner.reason)
        self.assertIn('Y', runner.reason)

    def test_capture_empty_is_done(self):
        runner, bench = make_runner(targets=[])
        self.assertEqual(runner.run(), AR.DONE)
        self.assertEqual(runner.placed, 0)
        self.assertIn('没有目标', runner.done_reason)

    def test_max_items_stops(self):
        targets = [
            {'class_name': 'A|蓝', 'center_tray': [0.1, 0.1], 'area_px': 100},
            {'class_name': 'B|红', 'center_tray': [0.2, 0.2], 'area_px': 200},
        ]
        runner, bench = make_runner(targets=targets, max_items=1)
        self.assertEqual(runner.run(), AR.DONE)
        self.assertEqual(runner.placed, 1)

    def test_gripper_failure_faults(self):
        runner, bench = make_runner()
        bench.gripper = lambda action: action != 'grab'
        self.assertEqual(runner.run(), AR.FAULT)
        self.assertIn('夹爪夹紧失败', runner.reason)

    def test_missing_corridor_faults(self):
        runner, bench = make_runner(route={'rpm': 300})
        self.assertEqual(runner.run(), AR.FAULT)
        self.assertIn('通道', runner.reason)

    def test_state_callback_sequence(self):
        seen = []
        bench = AR.FakeBackend()
        runner = AR.AutoRunner(
            bench,
            route={'corridor': [{'axis': 5, 'pulses': -100}], 'z_pick': 100, 'z_high': 200},
            on_state=lambda state, info: seen.append(state),
        )
        runner.run()
        for state in (AR.CAPTURE, AR.PLAN, AR.CORRIDOR_OUT, AR.ALIGN, AR.DESCEND,
                      AR.GRASP, AR.LIFT, AR.RETREAT, AR.CORRIDOR_BACK, AR.RELEASE, AR.DISPLAY):
            self.assertIn(state, seen)


if __name__ == '__main__':
    unittest.main()
