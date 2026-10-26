"""通道段（固定路线）测试：Y 走 -25000 脉冲 → Z 抬升。

默认只做"出门"方向；`--back` 会在后面按倒序回走一遍（Z 降回、Y 回 0），
回到出发点后打印净漂移。

用法示例：
  python .survey\\corridor_test.py --dry-run
  python .survey\\corridor_test.py --y -25000 --z -200000
  python .survey\\corridor_test.py --y -25000 --z -200000 --back
"""
import argparse
import sys
import time

sys.path.insert(0, r"D:\study\project\robot-competition\01-交付包\host")

import serial_link          # noqa: E402
import host_client          # noqa: E402

PORT = "tcp://192.168.100.198:5058"
AXIS_Z = 1
AXIS_Y = 5
PPR = 3200                  # 脉冲/圈（标称）
CPR = 65536                 # 编码器 counts/圈


def to_counts(pulses):
    return int(round(pulses * CPR / PPR))


class Rig:
    def __init__(self, port=PORT):
        self.link = serial_link.open_best_link(port, 115200)
        self.hc = host_client.HostClient(self.link)
        self.counts = {}
        if not self.hc.connect():
            raise SystemExit("连接失败: %s" % self.hc.last_error)

    def drain(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            raw = self.link.read()
            if raw:
                self.hc.feed_bytes(raw)
            for ev in self.hc.poll_events():
                data = getattr(ev, "data", {}) or {}
                pairs = data.get("pairs") or {}
                if "counts" in pairs and "addr" in pairs:
                    try:
                        self.counts[int(pairs["addr"])] = int(pairs["counts"])
                    except (TypeError, ValueError):
                        pass
            time.sleep(0.01)

    def call(self, cmd, *args, timeout_ms=5000):
        rid = self.hc.send(cmd, *args, timeout_ms=timeout_ms)
        return self.hc.wait_reply(rid, timeout_ms=timeout_ms + 8000)

    def close(self):
        try:
            self.hc.disconnect("corridor test done")
        except Exception:                                  # noqa: BLE001
            pass


def describe(reply):
    if not reply:
        return "无应答"
    pairs = reply.get("pairs") or {}
    if reply.get("kind") != "reply":
        return "%s %s reason=%s err=%s passes=%s" % (
            reply.get("code"), reply.get("text"), pairs.get("reason"),
            pairs.get("err"), pairs.get("passes"))
    return "ok err=%s passes=%s elapsed=%s" % (
        pairs.get("err"), pairs.get("passes"), pairs.get("elapsed_ms"))


def move(rig, axis, pulses, rpm, name):
    before = rig.counts.get(axis)
    t0 = time.monotonic()
    rid = rig.hc.goto(axis, pulses, tol=None, rpm=rpm)
    reply = rig.hc.wait_reply(rid, timeout_ms=30000)
    wall = time.monotonic() - t0
    rig.drain(0.6)
    after = rig.counts.get(axis)
    delta = None if None in (before, after) else after - before
    print("%-22s pulses=%-9d counts %s -> %s (Δ=%s, 期望≈%+d)  %s  wall=%.2fs"
          % (name, pulses, before, after, delta, to_counts(pulses), describe(reply), wall))
    return reply


def find_top(rig, axis, step, rpm, max_pulses, ratio=0.85):
    """小步向负方向走，counts 变化明显小于指令就认为顶到机械限位，然后回退两步。

    用 motor_pos 而不是 goto：goto 在顶死时要等 max_ms 才判超时（会顶几秒），
    motor_pos 每步只有 step 的距离，接触时间就是一个步长（默认 8000 脉冲 ≈1.5 s）。
    """
    acc = 1
    travelled = 0
    expect = step * CPR / PPR
    while travelled < max_pulses:
        before = rig.counts.get(axis)
        rig.call("motor_pos", axis, 1, rpm, acc, step, "rel")
        rig.drain(0.4 + step / (rpm * PPR / 60.0))
        after = rig.counts.get(axis)
        if None in (before, after):
            print("读不到 counts，放弃找顶")
            return None
        delta = before - after            # 负方向走 → counts 减小
        travelled += step
        print("  走 %7d 脉冲 | Δcounts=%+8d（期望 %+8.0f）| 累计 %d"
              % (step, -delta, -expect, travelled))
        if delta < expect * ratio:
            print("→ 判定到达顶（位移只有期望的 %.0f%%），回退 2 步" % (100.0 * delta / expect))
            rig.call("motor_pos", axis, 0, rpm, acc, step * 2, "rel")
            rig.drain(0.5 + step * 2 / (rpm * PPR / 60.0))
            top = rig.counts.get(axis)
            print("→ 顶部参考 counts = %s（累计走了 %d 脉冲，含回退）" % (top, travelled))
            return travelled
    print("→ 走满 %d 脉冲仍未触顶" % max_pulses)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--y", type=int, default=-1220, help="出门 Y 脉冲（默认 -1220 = -25000 counts ≈ 12.5mm）")
    ap.add_argument("--z", type=int, default=-150000, help="Z 抬升脉冲（默认 -150000；满行程约 -165762 = 100mm）")
    ap.add_argument("--y-rpm", type=int, default=300)
    ap.add_argument("--z-rpm", type=int, default=1500, help="Z 用 2mm 导程，满行程要 51.8 圈，300RPM 要 10s，建议 1500")
    ap.add_argument("--back", action="store_true", help="回程：Z 降回、Y 回 0")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不动")
    ap.add_argument("--find-top", action="store_true",
                    help="找 Z 轴的机械上限（小步 motor_pos 走，位移明显变小即认为到顶，自动回退）")
    ap.add_argument("--find-axis", type=int, default=AXIS_Z, help="找顶用的轴号（默认 1=Z）")
    ap.add_argument("--step", type=int, default=8000, help="找顶每步脉冲（默认 8000）")
    ap.add_argument("--find-rpm", type=int, default=100, help="找顶转速（默认 100）")
    ap.add_argument("--max-pulses", type=int, default=4000000, help="找顶最多走多少脉冲")
    args = ap.parse_args()

    print("计划：Y %+d 脉冲（≈%+d counts，%d RPM）→ Z %+d 脉冲（≈%+d counts，%d RPM）%s"
          % (args.y, to_counts(args.y), args.y_rpm, args.z, to_counts(args.z), args.z_rpm,
             "，然后倒序回程" if args.back else ""))
    if args.dry_run:
        print("（dry-run：未发送任何运动命令）")
        return 0

    rig = Rig(args.port)
    try:
        rig.drain(1.2)
        print("online=%s fw=%s boot=%s latched=%s"
              % (rig.hc.online, rig.hc.fw_version, rig.hc.boot_id, rig.hc.latched))
        rig.call("stop_clear")
        rig.call("tele_on", AXIS_Z, 150)
        rig.call("tele_on", AXIS_Y, 150)
        rig.drain(1.2)
        start = dict(rig.counts)
        print("起始 counts:", start)

        if args.find_top:
            args.find_axis and print("\n=== 找轴 %d 的机械上限（步长 %d 脉冲，%d RPM）==="
                                     % (args.find_axis, args.step, args.find_rpm))
            find_top(rig, args.find_axis, args.step, args.find_rpm, args.max_pulses)
            end = dict(rig.counts)
            print("\n结束 counts:", end)
            return 0

        move(rig, AXIS_Y, args.y, args.y_rpm, "1) 出门 Y")
        move(rig, AXIS_Z, args.z, args.z_rpm, "2) 抬 Z")

        if args.back:
            move(rig, AXIS_Z, -args.z, args.z_rpm, "3) 降 Z 回原高")
            move(rig, AXIS_Y, -args.y, args.y_rpm, "4) 回 Y 到 0")

        end = dict(rig.counts)
        print("\n结束 counts:", end)
        for axis in (AXIS_Y, AXIS_Z):
            if axis in start and axis in end:
                drift = end[axis] - start[axis]
                print("轴%d 净漂移 = %+d counts（%.2f°）" % (axis, drift, drift * 360.0 / CPR))
    finally:
        rig.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
