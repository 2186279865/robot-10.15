"""host_client + simulator 的联调用例。

对应方案 10 节步骤 1 的验收：无硬件下验证分帧、校验、超长恢复、请求去重及 GUI 连接
（GUI 连接在测试里等价为“握手后进入在线状态并可下发命令”）。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import host_client as HC  # noqa: E402
import protocol as P  # noqa: E402
import simulator as SIM  # noqa: E402


class FakeClock:
    """可控时钟：让超时/心跳测试不依赖真实等待。"""

    def __init__(self, start_s: float = 1000.0) -> None:
        self.t = start_s

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms / 1000.0


class DeadLink:
    """只写不读的哑链路：模拟“命令发出去了，永远没有回包”。"""

    is_simulated = True

    def __init__(self) -> None:
        self.written = []
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def connected(self) -> bool:
        return self._open

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def read(self) -> bytes:
        return b""

    def describe(self) -> str:
        return "哑链路（只写不读）"


class ClientTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.device = SIM.SimulatedDevice(clock=self.clock)
        self.link = SIM.SimulatedLink(self.device)
        self.client = HC.HostClient(self.link, clock=self.clock)

    def pump(self) -> None:
        """把模拟设备产生的应答与“随时间产生”的事件喂回客户端。

        等价于服务线程读到链路数据；`poll()` 对应固件主动上报的到位事件与遥测。
        """
        if hasattr(self.link, "poll"):
            self.link.poll()
        data = self.link.read()
        if data:
            self.client.feed_bytes(data)

    def connect(self) -> None:
        self.assertTrue(self.client.connect())
        self.pump()

    def polled(self, kind: str):
        return [e for e in self.client.poll_events(1000) if e.kind == kind]


class TestHandshake(ClientTestBase):
    def test_connect_enters_handshaking_then_online(self):
        self.assertTrue(self.client.connect())
        self.assertEqual(self.client.state, HC.STATE_HANDSHAKING)
        self.pump()
        self.assertEqual(self.client.state, HC.STATE_ONLINE)
        self.assertEqual(self.client.fw_version, SIM.FIRMWARE_VERSION)
        self.assertEqual(self.client.boot_id, f"{SIM.BOOT_ID + 1:08X}")
        self.assertEqual(self.client.axis_list, P.VALID_AXES)
        self.assertEqual(self.client.readback_axes, SIM.READBACK_AXES)

    def test_hello_uses_protocol_version(self):
        self.client.connect()
        raw = self.device.command_log[0]
        self.assertIn(f"hello,{P.PROTOCOL_VERSION}", raw)

    def test_protocol_version_mismatch_reports_e02(self):
        self.connect()
        self.client.poll_events(100)
        self.client.send("hello", 99, 1)
        self.pump()
        notes = [e for e in self.client.poll_events(100) if e.kind == "err"]
        self.assertTrue(notes)
        self.assertEqual(notes[0].data.get("code"), "E02")

    def test_status_and_ping_round_trip(self):
        self.connect()
        self.client.send("ping", timeout_ms=HC.TIMEOUT_QUERY_MS)
        self.pump()
        self.assertEqual(self.client.pending, {})
        self.client.poll_events(100)

        self.client.send("status", timeout_ms=HC.TIMEOUT_QUERY_MS)
        self.pump()
        replies = [e for e in self.client.poll_events(100) if e.kind == "reply"]
        self.assertTrue(replies)
        self.assertEqual(replies[-1].data["pairs"]["latch"], "0")


class TestRetryAndDedupe(ClientTestBase):
    def test_lost_reply_is_retried_with_same_id_and_body(self):
        self.connect()
        self.client.poll_events(100)
        rid = self.client.send("ping", timeout_ms=HC.TIMEOUT_QUERY_MS)
        pending = self.client.pending[rid]
        first_raw = pending.raw

        # 固件已执行并缓存应答，但应答在“回程”丢了。
        self.device.feed(first_raw)
        self.link.read()

        self.clock.advance(HC.TIMEOUT_QUERY_MS + 10)
        self.client.tick()
        self.assertEqual(self.client.pending[rid].attempts, 2)
        self.assertEqual(self.client.pending[rid].raw, first_raw)

        self.pump()  # 固件识别为重复请求，重发缓存应答
        self.assertNotIn(rid, self.client.pending)
        replies = [e for e in self.client.poll_events(100) if e.kind == "reply"]
        self.assertTrue(replies)

    def test_motion_timeout_is_unknown_and_not_resent(self):
        dead = DeadLink()
        client = HC.HostClient(dead, clock=self.clock)
        client.connect()
        client.poll_events(100)
        written_before = len(dead.written)

        rid = client.send("motor_speed", 1, 0, 60, 1, timeout_ms=100)
        self.clock.advance(200)
        client.tick()

        self.assertEqual(len(dead.written), written_before + 1, "运动类超时不得自动重发")
        self.assertNotIn(rid, client.pending, "结果未知的请求不得留在在途表里")
        notes = [e for e in client.poll_events(100) if e.kind == "timeout"]
        self.assertTrue(notes)
        self.assertIn("结果未知", notes[0].text)

    def test_query_gives_up_after_three_attempts(self):
        dead = DeadLink()
        client = HC.HostClient(dead, clock=self.clock)
        client.connect()
        client.send("motor_query", 1, "pos", timeout_ms=HC.TIMEOUT_QUERY_MS)
        for _ in range(4):
            self.clock.advance(HC.TIMEOUT_QUERY_MS + 10)
            client.tick()
        timeouts = [e for e in client.poll_events(200) if e.kind == "timeout"]
        self.assertTrue(timeouts)
        self.assertIn("无应答", timeouts[-1].text)


class TestFramingRobustness(ClientTestBase):
    def test_bad_checksum_reported_and_not_executed(self):
        self.connect()
        self.client.poll_events(100)
        before = len(self.device.command_log)
        self.client.feed_bytes(b"$9,stop_all*00\n")
        notes = [e for e in self.client.poll_events(100) if e.kind == "note"]
        self.assertTrue(any("E07" in n.text for n in notes))
        self.assertEqual(len(self.device.command_log), before)
        self.assertEqual(self.client.bad_frame_count, 1)

    def test_oversize_then_recovers(self):
        self.client.feed_bytes(b"$" + b"1" * 200 + b"\n")
        self.assertGreaterEqual(self.client.oversize_count, 1)
        # 超长之后仍能继续正确分帧并进入在线状态。
        self.connect()
        self.assertEqual(self.client.state, HC.STATE_ONLINE)

    def test_split_packets_across_reads(self):
        raw = P.encode_ok(5, addr=1)
        self.client.feed_bytes(raw[:4])
        self.client.feed_bytes(raw[4:])
        notes = [e for e in self.client.poll_events(100) if e.kind == "note"]
        self.assertTrue(any("无对应请求的应答 id=5" in n.text for n in notes), notes)

    def test_unknown_request_id_is_noted(self):
        self.client.feed_bytes(P.encode_ok(77, addr=1))
        notes = [e for e in self.client.poll_events(100) if e.kind == "note"]
        self.assertTrue(any("无对应请求" in n.text for n in notes))


class TestEventsAndSafety(ClientTestBase):
    def test_task_event_reports_sent_then_reached(self):
        self.connect()
        self.client.poll_events(100)
        self.client.send("motor_pos", 1, 1, 300, 1, 2048, "rel", timeout_ms=500)
        self.pump()
        sent = [e for e in self.client.poll_events(100) if e.kind == "event"]
        self.assertTrue(any(e.data.get("state") == P.EVIDENCE_SENT for e in sent))

        self.clock.advance(5000)
        self.pump()
        reached = [e for e in self.client.poll_events(100) if e.kind == "event"]
        self.assertTrue(any(e.data.get("state") == P.EVIDENCE_REACHED for e in reached))

    def test_stop_all_latches(self):
        self.connect()
        self.client.poll_events(100)
        self.client.stop_all()
        self.pump()
        self.assertTrue(self.client.latched)
        self.assertTrue(self.device.stop_latched)

    def test_e10_recovers_by_clearing_latch_and_resending(self):
        """派端看门狗造成的 E10 不该直接把命令判死：自动 stop_clear 后换新 id 重发。

        现场 10-31 03:14 就是因为这条整件失败（派端串口桥 1.5 s 静默注入停车帧 → E10）；
        注意必须换 id：重复 req_id 会被固件/模拟器当成重传，回放的还是那条 E10。
        """
        self.connect()
        self.client.poll_events(100)
        self.device.latch_stop("pi-watchdog")  # 协议只允许 ASCII   # 不是人按停止
        self.pump()
        rid = self.client.send("motor_speed", 1, 0, 60, 1, timeout_ms=200)
        for _ in range(4):
            self.pump()
        notes = [e.text for e in self.client.poll_events(1000) if e.kind == "note"]
        self.assertTrue(any("E10" in t for t in notes), notes)
        self.assertFalse(self.client.latched, "自动 stop_clear 之后锁存应解除")
        self.assertFalse(self.device.stop_latched)
        self.assertNotIn(rid, self.client.pending)
        done = self.client.done_of(rid)
        self.assertIsNotNone(done, "恢复成功后结果要记在原请求 id 上（调用方只看原 id）")
        self.assertEqual(done["kind"], "reply")

    def test_e10_by_user_stop_is_not_auto_cleared(self):
        """人按「停止全部」造成的锁存绝不能被 E10 恢复悄悄清掉（安全优先）。"""
        self.connect()
        self.client.poll_events(100)
        self.client.stop_all()
        self.pump()
        self.client.poll_events(1000)
        rid = self.client.send("motor_speed", 1, 0, 60, 1, timeout_ms=200)
        for _ in range(3):
            self.pump()
        errs = [e for e in self.client.poll_events(1000) if e.kind == "err"]
        self.assertTrue(any(e.data.get("code") == "E10" for e in errs), errs)
        self.assertNotIn(rid, self.client.pending)
        self.assertTrue(self.client.latched, "人工停止的锁存必须留着，等人来解")
        self.assertTrue(self.device.stop_latched)

    def test_e10_gives_up_after_retry_cap(self):
        """恢复次数到上限后仍然报 E10 —— 不能无限重发掩盖真故障。"""
        self.connect()
        self.client.poll_events(100)
        self.device.latch_stop("pi-watchdog")  # 协议只允许 ASCII
        self.pump()
        rid = self.client.send("motor_speed", 1, 0, 60, 1, timeout_ms=200)
        self.client.pending[rid].attempts = HC.E10_MAX_RETRIES + 1
        for _ in range(3):
            self.pump()
        errs = [e for e in self.client.poll_events(1000) if e.kind == "err"]
        self.assertTrue(any(e.data.get("code") == "E10" for e in errs), errs)
        self.assertNotIn(rid, self.client.pending)

    def test_query_counts_are_cached(self):
        """motor_query pos 的 counts 会缓存到 client.last_counts，供标定向导记录手推后的脉冲。"""
        self.connect()
        self.client.poll_events(100)
        rid = self.client.send("motor_query", 4, "pos", timeout_ms=HC.TIMEOUT_QUERY_MS)
        self.pump()
        self.assertNotIn(rid, self.client.pending)
        item = self.client.last_counts.get(4)
        self.assertIsNotNone(item)
        self.assertEqual(item["counts"], 0)
        self.assertIn("ts", item)

    def test_stop_latch_exempts_queries_and_clear(self):
        self.connect()
        self.device.latch_stop("test")
        self.client.poll_events(100)
        self.client.send("status", timeout_ms=HC.TIMEOUT_QUERY_MS)
        self.pump()
        self.assertFalse([e for e in self.client.poll_events(100) if e.kind == "err"])
        self.client.send("stop_clear", timeout_ms=HC.TIMEOUT_QUERY_MS)
        self.pump()
        self.assertFalse(self.device.stop_latched)

    def test_heartbeat_silence_triggers_link_lost(self):
        self.connect()
        self.client.poll_events(100)
        self.clock.advance(HC.LINK_TIMEOUT_MS + 200)
        self.client.tick()
        self.assertEqual(self.client.state, HC.STATE_LINK_LOST)
        self.assertEqual(self.client.pending, {})
        notes = [e for e in self.client.poll_events(100) if e.kind == "note"]
        self.assertTrue(any("运动状态未知" in n.text for n in notes))

    def test_heartbeat_keeps_link_alive(self):
        """有应答时心跳不该把链路判死。"""
        self.connect()
        self.client.poll_events(100)
        self.clock.advance(HC.HEARTBEAT_MS)
        self.client.tick()  # 发心跳
        self.pump()  # 固件应答，刷新 last_rx_ms
        self.clock.advance(HC.HEARTBEAT_MS)
        self.client.tick()
        self.pump()
        self.assertEqual(self.client.state, HC.STATE_ONLINE)

    def test_disconnect_resets_state(self):
        self.connect()
        self.client.disconnect("测试断开")
        self.assertEqual(self.client.state, HC.STATE_DISCONNECTED)
        self.assertFalse(self.link.connected)
        self.assertFalse(self.client.pending)


class TestReadbackMask(ClientTestBase):
    """readback_mask 决定哪些轴接了回读线；模拟后端必须和固件一致（默认四轴都接）。"""

    def test_default_mask_is_all_four_axes(self):
        self.connect()
        self.assertEqual(self.client.readback_axes, P.VALID_AXES)
        self.assertEqual(self.device.readback_list(), "1/3/4/5")
        self.assertEqual(self.device.params["readback_mask"], 15)

    def test_mask_switches_readback_axes(self):
        self.connect()
        self.client.send("param_set", "readback_mask", 1)
        self.pump()
        self.assertEqual(self.device.readback_list(), "1")
        self.assertTrue(self.device.axes[1].readback_capable)
        for addr in (3, 4, 5):
            self.assertFalse(self.device.axes[addr].readback_capable)

        # 重新握手后 hello 要如实报出新的名单
        self.client.send("hello", P.PROTOCOL_VERSION, 1)
        self.pump()
        self.assertEqual(self.client.readback_axes, (1,))

    def test_mask_out_of_range_is_refused(self):
        self.connect()
        self.client.send("param_set", "readback_mask", 16)
        self.pump()
        errs = self.polled("err")
        self.assertTrue(errs)
        self.assertEqual(errs[-1].data.get("code"), "E02")
        self.assertEqual(self.device.readback_list(), "1/3/4/5")

    def test_unwired_axis_query_reports_e11(self):
        self.connect()
        self.client.send("param_set", "readback_mask", 1)
        self.pump()
        self.client.send("motor_query", 4, "pos")
        self.pump()
        errs = self.polled("err")
        self.assertTrue(errs)
        self.assertEqual(errs[-1].data.get("code"), "E11")

    def test_reset_restores_default_mask(self):
        self.connect()
        self.client.send("param_set", "readback_mask", 1)
        self.pump()
        self.device.reset()
        self.assertEqual(self.device.readback_list(), "1/3/4/5")


class TestTelemetryNotDuplicated(ClientTestBase):
    """遥测帧只发 tele 事件，不再额外发一条 rx。

    以前 `_dispatch` 对每一帧都 `emit("rx")`，界面把 rx 和 tele 各记一行，
    5 Hz×四轴时日志就是 40 行/秒再翻倍 —— 读写都在挤串口/无线。
    """

    def _subscribe(self) -> None:
        self.connect()
        self.client.send("tele_on", 1, 20)
        self.pump()
        self.client.poll_events(1000)

    def test_tele_frames_do_not_emit_rx(self):
        self._subscribe()
        for _ in range(3):
            self.clock.advance(100)
            self.pump()
        tele = self.polled("tele")
        self.assertTrue(tele, "订阅后应该收到遥测帧")
        rx = self.polled("rx")
        self.assertFalse([e for e in rx if ",tele," in str(e.text)],
                         "遥测帧不应再额外发 rx 事件")

    def test_reply_frames_still_emit_rx(self):
        self._subscribe()
        self.client.send("status")
        self.pump()
        rx = self.polled("rx")
        self.assertTrue([e for e in rx if ",ok," in str(e.text)],
                        "普通应答仍要走 rx（日志/抓包还靠它）")


if __name__ == "__main__":
    unittest.main()
