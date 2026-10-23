"""protocol.py 的自检用例（仅依赖标准库 unittest）。

运行：``python -m unittest discover -s tests -v``（在 ghr/10.15/上位机 下）
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import protocol as P  # noqa: E402


class TestChecksum(unittest.TestCase):
    """文档中的示例校验值必须逐字节可复现。"""

    def test_request_example_from_spec(self):
        payload = "12,motor_speed,1,0,60,1"
        self.assertEqual(P.checksum_text(payload), "4A")
        self.assertEqual(
            P.encode_request(12, "motor_speed", 1, 0, 60, 1),
            b"$12,motor_speed,1,0,60,1*4A\n",
        )

    def test_reply_example_from_spec(self):
        self.assertEqual(
            P.encode_ok(12, addr=1, rpm=60),
            b"#12,ok,addr=1,rpm=60*60\n",
        )

    def test_checksum_detects_single_byte_error(self):
        """规范选了异或校验，对任意单字节错误必须 100% 检出。"""
        good = "motor_speed,1,0,60,1"
        for pos, original in enumerate(good):
            for delta in (1, 0x20, 0x41):
                broken = list(good)
                broken[pos] = chr(ord(original) ^ delta)
                # 0x00 与 '!' 在本协议里另有含义，跳过不属于“字节翻转”的取值
                if broken[pos] in ("\x00", "!"):
                    continue
                self.assertNotEqual(
                    P.checksum_text(good),
                    P.checksum_text("".join(broken)),
                    f"第 {pos} 字节被翻转成 {broken[pos]!r} 后校验值居然相同",
                )

    def test_checksum_known_weakness_byte_swap(self):
        """已知弱点（钉成用例，防止误以为它能发现换位）：异或满足交换律，
        因此 exchanged bytes 不改变校验值。所以校验只能作为“单字节错误”防线，
        真正决定执行与否的仍是命令白名单 + 参数范围校验。
        """
        self.assertEqual(P.checksum_text("ab"), P.checksum_text("ba"))

    def test_non_ascii_rejected(self):
        with self.assertRaises(P.ProtocolError) as ctx:
            P.checksum_text("12,速度")
        self.assertEqual(ctx.exception.code, "E02")


class TestRoundTrip(unittest.TestCase):
    def test_request_round_trip(self):
        raw = P.encode_request(7, "motor_pos", 5, 1, 120, 1, 2048, "rel")
        (inbound,) = P.FramingBuffer().feed(raw)
        self.assertTrue(inbound.ok)
        frame = inbound.frame
        assert frame is not None
        self.assertTrue(frame.is_request)
        self.assertEqual(frame.req_id, 7)
        self.assertEqual(frame.cmd, "motor_pos")
        self.assertEqual(frame.args, ("5", "1", "120", "1", "2048", "rel"))

    def test_reply_round_trip(self):
        raw = P.encode_ok(12, addr=1, rpm=60)
        (inbound,) = P.FramingBuffer().feed(raw)
        frame = inbound.frame
        assert frame is not None
        self.assertEqual(frame.kind, "ok")
        self.assertEqual(frame.pairs, {"addr": "1", "rpm": "60"})

    def test_error_reply(self):
        raw = P.encode_err(9, "E03")
        (inbound,) = P.FramingBuffer().feed(raw)
        frame = inbound.frame
        assert frame is not None
        self.assertEqual(frame.kind, "err")
        self.assertEqual(frame.error_code, "E03")

    def test_event_and_telemetry_use_id_zero(self):
        for raw in (P.encode_event("task", id=3, state="sent"), P.encode_telemetry(addr=1, rpm=60)):
            (inbound,) = P.FramingBuffer().feed(raw)
            frame = inbound.frame
            assert frame is not None
            self.assertEqual(frame.req_id, P.ID_EVENT)

    def test_trailing_cr_tolerated(self):
        raw = P.encode_request(3, "ping").rstrip(b"\n") + b"\r\n"
        (inbound,) = P.FramingBuffer().feed(raw)
        self.assertTrue(inbound.ok)


class TestFraming(unittest.TestCase):
    def test_split_across_packets(self):
        """一条命令逐字节发送也能分帧。"""
        buf = P.FramingBuffer()
        raw = P.encode_request(1, "status")
        results = []
        for byte in raw:
            results.extend(buf.feed(bytes([byte])))
        self.assertEqual(len(results), 1)
        frame = results[0].frame
        assert frame is not None
        self.assertEqual(frame.cmd, "status")

    def test_coalesced_frames(self):
        """多条命令一次发送也能正确分帧。"""
        raw = P.encode_request(1, "ping") + P.encode_request(2, "status")
        results = P.FramingBuffer().feed(raw)
        self.assertEqual([r.frame.cmd for r in results if r.frame], ["ping", "status"])

    def test_64_byte_boundary_split(self):
        """模拟 64 字节 Bulk 包切分：payload 恰好跨包。"""
        raw = P.encode_request(1234, "motor_pos", 5, 1, 120, 1, 2048, "abs")
        self.assertGreater(len(raw), 30)
        buf = P.FramingBuffer()
        head, tail = raw[:64], raw[64:]
        results = buf.feed(head) + buf.feed(tail)
        self.assertEqual(len(results), 1)
        frame = results[0].frame
        assert frame is not None
        self.assertEqual(frame.req_id, 1234)

    def test_oversize_discarded_then_recovers(self):
        buf = P.FramingBuffer()
        junk = b"$" + b"1" * 200 + b"\n"
        results = buf.feed(junk)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].error.code, "E06")
        # 恢复解析：后面的正常帧仍能识别
        results = buf.feed(P.encode_request(5, "ping"))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(buf.dropped_oversize, 1)

    def test_junk_before_start_char_ignored(self):
        buf = P.FramingBuffer()
        results = buf.feed(b"garbage" + P.encode_request(5, "ping"))
        self.assertEqual([r.frame.cmd for r in results if r.frame], ["ping"])
        self.assertEqual(buf.dropped_junk, len(b"garbage"))

    def test_bad_checksum_reported_not_executed(self):
        raw = b"$12,ping*00\n"
        (inbound,) = P.FramingBuffer().feed(raw)
        self.assertFalse(inbound.ok)
        self.assertEqual(inbound.error.code, "E07")

    def test_frame_length_limit(self):
        with self.assertRaises(P.ProtocolError) as ctx:
            P.build_line("$", "x" * (P.MAX_PAYLOAD_BYTES + 1))
        self.assertEqual(ctx.exception.code, "E06")
        line = P.build_line("$", "x" * P.MAX_PAYLOAD_BYTES)
        self.assertEqual(len(line) + 1, P.MAX_FRAME_BYTES)

    def test_crlf_and_empty_lines(self):
        buf = P.FramingBuffer()
        results = buf.feed(b"\r\n" + P.encode_request(1, "ping") + b"\n\n")
        self.assertEqual(len(results), 1)


class TestValidation(unittest.TestCase):
    def _parse(self, raw: bytes) -> P.ParsedRequest:
        (inbound,) = P.FramingBuffer().feed(raw)
        assert inbound.frame is not None, inbound.error
        return P.validate_request(inbound.frame)

    def test_valid_commands(self):
        cases = [
            ("hello", (1, 42)),
            ("ping", ()),
            ("motor_enable", (1, 1)),
            ("motor_speed", (1, 0, 60, 1)),
            ("motor_pos", (3, 1, 120, 1, 2048, "rel")),
            ("motor_stop", (5,)),
            ("stop_all", ()),
            ("motor_stall_clear", (4,)),
            ("motor_zero", (1,)),
            ("motor_query", (1, "pos")),
            ("status", ()),
            ("gripper", ("grab",)),
            ("gripper", ("ready",)),
            ("gripper", ("open",)),
            ("gripper", ("step_grab",)),
            ("gripper", ("step_release",)),
            ("servo_angle", (135,)),
            ("param_get", ("speed_rpm",)),
            ("param_get", ("gripper_ready_deg",)),
            ("param_set", ("speed_rpm", 120)),
            ("param_set", ("gripper_hold_max_ms", 60000)),
            ("tele_on", (1, 200)),
            ("tele_off", ()),
            ("stop_clear", ()),
        ]
        for cmd, args in cases:
            parsed = self._parse(P.encode_request(1, cmd, *args))
            self.assertEqual(parsed.cmd, cmd)

    def test_gripper_ready_action_accepted_and_garbage_rejected(self):
        parsed = self._parse(P.encode_request(1, "gripper", "ready"))
        self.assertEqual(parsed.values["action"], "ready")
        with self.assertRaises(P.ProtocolError) as ctx:
            self._parse(P.encode_request(1, "gripper", "prepare"))
        self.assertEqual(ctx.exception.code, "E02")
        # 旧写法 step 不再合法（协议枚举里只有 step_grab/step_release）
        with self.assertRaises(P.ProtocolError):
            self._parse(P.encode_request(1, "gripper", "step"))

    def test_axis_whitelist(self):
        for bad in (0, 2, 6, 255):
            with self.assertRaises(P.ProtocolError) as ctx:
                self._parse(P.encode_request(1, "motor_stop", bad))
            self.assertEqual(ctx.exception.code, "E03", f"addr={bad} 应被拒绝")

    def test_unknown_command(self):
        with self.assertRaises(P.ProtocolError) as ctx:
            self._parse(P.encode_request(1, "motor_boost", 1))
        self.assertEqual(ctx.exception.code, "E01")

    def test_wrong_arg_count(self):
        with self.assertRaises(P.ProtocolError) as ctx:
            self._parse(P.encode_request(1, "motor_speed", 1, 0, 60))
        self.assertEqual(ctx.exception.code, "E02")

    def test_speed_must_be_positive(self):
        with self.assertRaises(P.ProtocolError) as ctx:
            self._parse(P.encode_request(1, "motor_speed", 1, 0, 0, 1))
        self.assertEqual(ctx.exception.code, "E02")

    def test_request_id_zero_rejected(self):
        (inbound,) = P.FramingBuffer().feed(b"$0,ping*00\n")
        self.assertFalse(inbound.ok)

    def test_non_integer_rejected(self):
        with self.assertRaises(P.ProtocolError) as ctx:
            self._parse(P.encode_request(1, "servo_angle", "abc"))
        self.assertEqual(ctx.exception.code, "E02")


class TestSessionCache(unittest.TestCase):
    def test_new_duplicate_conflict(self):
        cache = P.SessionCache(capacity=2)
        response = P.encode_ok(10, addr=1)
        self.assertEqual(cache.check(10, "10,motor_stop,1"), P.SessionCache.NEW)
        cache.record(10, "10,motor_stop,1", response)
        self.assertEqual(cache.check(10, "10,motor_stop,1"), P.SessionCache.DUPLICATE)
        self.assertEqual(cache.cached_response(10), response)
        self.assertEqual(cache.check(10, "10,motor_stop,3"), P.SessionCache.CONFLICT)

    def test_capacity_eviction(self):
        cache = P.SessionCache(capacity=2)
        for i in (1, 2, 3):
            cache.record(i, f"{i},ping", P.encode_ok(i))
        self.assertEqual(len(cache), 2)
        self.assertEqual(cache.check(1, "1,ping"), P.SessionCache.NEW)

    def test_id_pool_skips_in_flight(self):
        pool = P.RequestIdPool(start=1)
        first = pool.acquire()
        second = pool.acquire()
        self.assertNotEqual(first, second)
        pool.release(first)
        self.assertEqual(pool.in_flight, 1)


class TestParamDefaults(unittest.TestCase):
    def test_defaults_match_spec(self):
        self.assertEqual(P.PARAM_DEFAULTS["speed_rpm"], 60)
        self.assertEqual(P.PARAM_DEFAULTS["speed_rpm_max"], 300)
        self.assertEqual(P.PARAM_DEFAULTS["gripper_open_deg"], 180)
        self.assertEqual(P.PARAM_DEFAULTS["gripper_ready_deg"], 150)
        self.assertEqual(P.PARAM_DEFAULTS["gripper_grab_deg"], 135)
        self.assertEqual(P.PARAM_DEFAULTS["gripper_step_deg"], 45)
        # 固件参数表 value/min/max 是 uint16_t，所以 hold 上限是 60000 不是 600000
        self.assertEqual(P.PARAM_DEFAULTS["gripper_hold_max_ms"], 0)
        self.assertLessEqual(P.PARAM_DEFAULTS["gripper_hold_max_ms"], 60000)

    def test_axis_labels(self):
        self.assertEqual(P.AXIS_LABELS[1], "Z(1)")
        self.assertEqual(P.AXIS_LABELS[3], "托盘(3)")
        self.assertEqual(P.AXIS_LABELS[4], "X(4)")
        self.assertEqual(P.AXIS_LABELS[5], "Y(5)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
