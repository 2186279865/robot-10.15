"""motor_goto 闭环自测：烧完新固件后，用这个脚本验证“真的走到位了”。

它会做这些事（只碰你指定的那根轴，没有别的动作）：
  1. 连上链路（派：tcp://<IP>:5058；直连：COM6）→ hello → 等在线
  2. 打印固件版本、声称接了回读的轴、counts_per_rev / goto_tol_counts / goto_rpm
  3. 若上次是异常断开（stop 锁存），先发一次 stop_clear（只解锁存，不产生运动）
  4. 按 --rounds 发 motor_goto：正负交替（来回走），每次打印 err / passes / elapsed
  5. 收尾打印成功率与累计偏差

用法（在 01-交付包 目录下）：
    .venv\\Scripts\\python.exe tools\\goto_check.py                       # 默认 tcp://127.0.0.1:5058
    .venv\\Scripts\\python.exe tools\\goto_check.py --port COM6
    .venv\\Scripts\\python.exe tools\\goto_check.py --port tcp://192.168.100.198:5058 --axis 4 --pulses 3200 --rounds 5
    .venv\\Scripts\\python.exe tools\\goto_check.py --port COM6 --dry-run  # 只看参数，不动电机

安全：脚本自己不 enable 四轴（motor_goto 内部会让固件按需使能），不发速度模式，
不碰夹爪；出错/超时会调用 stop_all。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent
for _p in (PKG / "host", PKG / "common"):
    sys.path.insert(0, str(_p))

import host_client  # noqa: E402
import protocol as P  # noqa: E402
import serial_link  # noqa: E402


def pump(hc, link, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data = link.read()
        except Exception:
            time.sleep(0.05)
            continue
        if data:
            hc.feed_bytes(data)
        else:
            time.sleep(0.01)
        hc.tick()


def main() -> int:
    ap = argparse.ArgumentParser(description="motor_goto 闭环自测")
    ap.add_argument("--port", default="tcp://127.0.0.1:5058",
                    help="链路：tcp://<派IP>:5058 或 COM6（默认派在本机 5058）")
    ap.add_argument("--axis", type=int, default=4, help="轴地址：1=Z 3=托盘 4=X 5=Y（默认 4=X）")
    ap.add_argument("--pulses", type=int, default=3200, help="每段脉冲数（默认 3200 = 1 圈）")
    ap.add_argument("--rounds", type=int, default=3, help="来回次数（默认 3）")
    ap.add_argument("--tol", type=int, default=None, help="容差 counts（默认用固件参数 goto_tol_counts）")
    ap.add_argument("--rpm", type=int, default=None, help="首段转速（默认用固件参数 goto_rpm）")
    ap.add_argument("--no-stop-clear", action="store_true", help="不上发 stop_clear")
    ap.add_argument("--dry-run", action="store_true", help="只连接+读参数，不发 motor_goto")
    args = ap.parse_args()

    link = serial_link.open_best_link(args.port, 115200)
    hc = host_client.HostClient(link)
    if not hc.connect():
        print(f"打不开链路：{hc.last_error}")
        return 1
    pump(hc, link, 2.5)
    print(f"链路={link.describe()}  在线={hc.online}  固件={hc.fw_version}  boot={hc.boot_id}")
    print(f"声称接了回读的轴={hc.readback_axes}")
    if not hc.online:
        print("没有握手成功，检查固件是否在跑 / 端口是否正确")
        return 1

    def pget(name: str):
        rid = hc.send("param_get", name, timeout_ms=800)
        r = hc.wait_reply(rid, timeout_ms=1500)
        if not r or r["kind"] != "reply":
            return None
        pairs = r["pairs"] or {}
        return pairs.get("value", pairs)

    counts_per_rev = pget("counts_per_rev")
    tol = args.tol if args.tol is not None else pget("goto_tol_counts")
    rpm = args.rpm if args.rpm is not None else pget("goto_rpm")
    passed = pget("goto_max_passes")
    print(f"counts_per_rev={counts_per_rev}  goto_tol_counts={tol}  goto_rpm={rpm}  goto_max_passes={passed}")

    if not args.no_stop_clear:
        # 无条件清一次：固件在「3 秒没收到上位机字节」时会自动锁存停止，
        # 而 hc.latched 要等收到 status 才更新，靠它判断经常漏（经验：每次连上先清）。
        print("先发 stop_clear（只解锁存，不产生任何运动）")
        rid = hc.send("stop_clear", timeout_ms=800)
        hc.wait_reply(rid, timeout_ms=1500)
        pump(hc, link, 0.2)

    if args.dry_run:
        print("--dry-run：到此为止，没有发运动命令")
        hc.disconnect("dry-run 结束")
        return 0

    ok = 0
    errs = []
    drift = 0
    for i in range(args.rounds):
        sign = 1 if i % 2 == 0 else -1
        pulses = sign * abs(args.pulses)
        t0 = time.monotonic()
        rid = hc.goto(args.axis, pulses, tol=tol, rpm=rpm)
        r = hc.wait_reply(rid, timeout_ms=max(8000, int(abs(pulses) / 3.2 / (int(rpm or 300)))) * 2)
        dt = (time.monotonic() - t0) * 1000.0
        if r is None:
            print(f"[{i + 1}/{args.rounds}] {pulses:+d} → 没有应答（{dt:.0f} ms 超时）")
            errs.append("no-reply")
            hc.stop_all()
            pump(hc, link, 0.5)
            break
        pairs = r["pairs"] or {}
        err_counts = pairs.get("err")
        passes = pairs.get("passes")
        elapsed = pairs.get("elapsed_ms")
        if r["kind"] == "reply":
            ok += 1
            try:
                drift += int(err_counts)
            except (TypeError, ValueError):
                pass
            print(f"[{i + 1}/{args.rounds}] {pulses:+d} → ok  err={err_counts} counts  passes={passes}  "
                  f"elapsed={elapsed} ms  往返={dt:.0f} ms")
        else:
            errs.append(f"{r.get('code')}: {r['text']}")
            print(f"[{i + 1}/{args.rounds}] {pulses:+d} → {r['kind']} {r.get('code')} {r['text']}")

    print("\n小结：")
    print(f"  成功 {ok}/{args.rounds}，累计残余 err={drift} counts"
          f"（≈ {drift / float(counts_per_rev or 65536) * 360.0:.2f}°）")
    if errs:
        print(f"  失败：{errs}")
        print("  常见原因：01 E12/E14 → 没烧新固件或没接回读线；E10 → 先点『清除停止锁存』")
    hc.stop_all()
    pump(hc, link, 0.3)
    hc.disconnect("自测结束")
    return 0 if ok == args.rounds else 2


if __name__ == "__main__":
    raise SystemExit(main())
