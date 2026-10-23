"""动作序列的模型与执行器测试（不依赖 tkinter，不依赖硬件）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 系统临时目录在某些沙箱/只读环境不可写，测试产物落到包内 tests/_tmp
TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp')
os.makedirs(TMP_ROOT, exist_ok=True)

import protocol as P  # noqa: E402
import sequence as S  # noqa: E402
from host_client import UiEvent  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.t = 0

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


class FakeClient:
    """只记录发出去的请求，起 id 并返回，模拟固件的部分由测试自己喂事件。"""

    def __init__(self, clock: Clock, readback_axes=(1,)) -> None:
        self.clock = clock
        self.readback_axes = tuple(readback_axes)
        self.sent = []

    def now_ms(self) -> int:
        return self.clock()

    def send(self, cmd, *args, timeout_ms=600, req_id=None):
        rid = len(self.sent) + 1
        self.sent.append({"id": rid, "cmd": cmd, "args": args, "timeout_ms": timeout_ms})
        return rid

    @property
    def last(self):
        return self.sent[-1]


class SequenceModelTest(unittest.TestCase):
    def test_validate_rejects_wait_reached_on_axis_without_readback(self):
        seq = S.Sequence(steps=[S.Step("wait_reached", [4, 5000])])
        problems = seq.validate(readback_axes=(1,))
        self.assertTrue(problems)
        self.assertIn("没有回读能力", problems[0])

    def test_validate_accepts_normal_sequence(self):
        seq = S.Sequence(
            steps=[
                S.Step("motor_speed", [1, 0, 60, 1], wait=S.WAIT_TIME, wait_ms=500),
                S.Step("stop_all", []),
            ]
        )
        self.assertEqual(seq.validate(readback_axes=(1,)), [])

    def test_validate_rejects_bad_axis(self):
        seq = S.Sequence(steps=[S.Step("motor_speed", [2, 0, 60, 1])])
        self.assertTrue(any("轴必须是" in p for p in seq.validate(readback_axes=(1,))))

    def test_save_load_roundtrip(self):
        seq = S.Sequence(
            name="首次抓取",
            steps=[S.Step("gripper", ["grab"]), S.Step("wait_ms", [200])],
        )
        # 不用 tempfile：部分沙箱里 mkdtemp 出来的目录写不进去，直接写包内 tests/_tmp
        path = os.path.join(TMP_ROOT, 'seq_roundtrip.json')
        seq.save(path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertEqual(raw["name"], "首次抓取")
            self.assertEqual(S.Sequence.load(path), seq)
        finally:
            if os.path.exists(path):
                os.unlink(path)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = FakeClient(self.clock)
        self.messages = []
        self.runner = S.SequenceRunner(self.client, self.client.now_ms, self.messages.append)

    def reply(self, rid, **pairs):
        event = UiEvent(kind="reply", text="ok", data={"req_id": rid, "pairs": pairs})
        self.runner.notify(event)

    def event(self, **data):
        self.runner.notify(UiEvent(kind="event", text="", data=data))

    def test_ok_reply_advances_then_time_wait_then_finish(self):
        seq = S.Sequence(
            steps=[
                S.Step("motor_speed", [1, 0, 60, 1], wait=S.WAIT_TIME, wait_ms=200),
                S.Step("stop_all", []),
            ]
        )
        self.assertEqual(self.runner.start(seq), [])
        self.assertEqual(self.client.last["cmd"], "motor_speed")
        self.reply(1)
        self.assertEqual(self.runner.state, S.WAITING)
        self.clock.advance(199)
        self.runner.tick()
        self.assertEqual(len(self.client.sent), 1, "等待时间没到就不能提前下发下一步")
        self.clock.advance(1)
        self.runner.tick()
        self.assertEqual(self.client.last["cmd"], "stop_all")
        self.reply(2)
        self.assertEqual(self.runner.state, S.DONE)

    def test_wait_reached_needs_matching_task_event(self):
        seq = S.Sequence(
            steps=[
                S.Step("motor_pos", [1, 0, 60, 1, 3200, "rel"], wait=S.WAIT_REACHED, timeout_ms=1000)
            ]
        )
        self.runner.start(seq)
        self.reply(1, task=7)
        self.assertEqual(self.runner.state, S.WAITING)
        self.event(type="task", id=8, state=P.EVIDENCE_REACHED)
        self.assertEqual(self.runner.state, S.WAITING, "别的任务的到位事件不能算数")
        self.event(type="task", id=7, state=P.EVIDENCE_REACHED)
        self.assertEqual(self.runner.state, S.DONE)

    def test_wait_reached_timeout_aborts(self):
        seq = S.Sequence(
            steps=[
                S.Step("motor_pos", [1, 0, 60, 1, 3200, "rel"], wait=S.WAIT_REACHED, timeout_ms=1000)
            ]
        )
        self.runner.start(seq)
        self.reply(1, task=7)
        self.clock.advance(1001)
        self.runner.tick()
        self.assertEqual(self.runner.state, S.ABORTED)
        self.assertIn("到位", self.runner.last_error)

    def test_on_timeout_continue_skips_step(self):
        seq = S.Sequence(
            steps=[
                S.Step(
                    "motor_pos",
                    [1, 0, 60, 1, 3200, "rel"],
                    wait=S.WAIT_REACHED,
                    timeout_ms=1000,
                    on_timeout=S.ON_TIMEOUT_CONTINUE,
                ),
                S.Step("stop_all", []),
            ]
        )
        self.runner.start(seq)
        self.reply(1, task=7)
        self.clock.advance(1001)
        self.runner.tick()
        self.assertEqual(self.client.last["cmd"], "stop_all")
        self.reply(2)
        self.assertEqual(self.runner.state, S.DONE)

    def test_err_reply_aborts(self):
        seq = S.Sequence(steps=[S.Step("motor_speed", [1, 0, 60, 1])])
        self.runner.start(seq)
        self.runner.notify(
            UiEvent(kind="err", text="", data={"req_id": 1, "code": "E02"})
        )
        self.assertEqual(self.runner.state, S.ABORTED)

    def test_no_ok_timeout_aborts_and_does_not_resend(self):
        seq = S.Sequence(steps=[S.Step("motor_pos", [1, 0, 60, 1, 3200, "rel"], timeout_ms=500)])
        self.runner.start(seq)
        self.clock.advance(501)
        self.runner.tick()
        self.assertEqual(self.runner.state, S.ABORTED)
        self.assertEqual(len(self.client.sent), 1, "运动类超时不得自动重发")

    def test_pause_blocks_progress_and_resume_continues(self):
        seq = S.Sequence(
            steps=[
                S.Step("motor_speed", [1, 0, 60, 1], wait=S.WAIT_TIME, wait_ms=100),
                S.Step("stop_all", []),
            ]
        )
        self.runner.start(seq)
        self.reply(1)
        self.runner.pause()
        self.clock.advance(500)
        self.runner.tick()
        self.assertEqual(len(self.client.sent), 1)
        self.runner.resume()
        self.runner.tick()
        self.assertEqual(self.client.last["cmd"], "stop_all")

    def test_start_refuses_invalid_sequence(self):
        seq = S.Sequence(steps=[S.Step("wait_reached", [4, 1000])])
        problems = self.runner.start(seq)
        self.assertTrue(problems)
        self.assertEqual(self.client.sent, [])


if __name__ == "__main__":
    unittest.main()
