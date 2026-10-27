# -*- coding: utf-8 -*-
"""只读实时看图工具（给队友/第二台电脑用）。

它只向派端「图传 5057」要帧并在本机弹窗显示，**不连串口桥、不发任何控制命令**，
所以可以和另一台正在控制的电脑同时使用。

用法（在 01-交付包 目录下）：
    .venv\\Scripts\\python.exe tools\\view_live.py --host 192.168.100.198
    .venv\\Scripts\\python.exe tools\\view_live.py --host 192.168.100.198 --cams 0,1 --fps 10 --scale 0.6
    .venv\\Scripts\\python.exe tools\\view_live.py --host 192.168.100.198 --headless --seconds 10

按键：q 或 ESC 退出；p 暂停/继续；s 存一张当前帧到 runtime/view_<cam>_<时间>.jpg
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "common"))
import wire  # noqa: E402


def grab(host, port, cam, w, h, quality, timeout=4.0):
    """拉一帧，返回 (jpeg_bytes, shape_hw) 或抛异常。"""
    sock = wire.open_tcp(host, port, timeout=timeout)
    try:
        sock.sendall(wire.pack_request(cam, w, h, quality))
        magic = wire.recv_exact(sock, 2)
        if magic == b"ER":
            head = wire.ERR_HDR.unpack(wire.recv_exact(sock, wire.ERR_HDR_LEN))
            body = wire.recv_exact(sock, head[2])
            raise RuntimeError("派端报错：" + body.decode("utf-8", "replace"))
        if magic != b"IM":
            raise RuntimeError("不是图像帧（magic=%r）" % (magic,))
        rest = wire.recv_exact(sock, wire.IMG_HDR_LEN - 2)
        head = wire.parse_image_header(magic + rest)
        body = wire.recv_exact(sock, head["n"])
        return body, (head["width"], head["height"], head["seq"])
    finally:
        sock.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="只读实时看图（不碰控制，可与另一台电脑同时用）")
    ap.add_argument("--host", default="192.168.100.198")
    ap.add_argument("--port", type=int, default=wire.IMG_PORT)
    ap.add_argument("--cams", default="0,1", help="派端相机槽位，逗号分隔（本机顶视通常是 0 或 1）")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--quality", type=int, default=75)
    ap.add_argument("--fps", type=float, default=10.0, help="每路每秒拉几帧（越小越省带宽）")
    ap.add_argument("--scale", type=float, default=0.7, help="显示缩放（不影响传输分辨率）")
    ap.add_argument("--headless", action="store_true", help="不弹窗，只统计帧率/带宽（测链路用）")
    ap.add_argument("--seconds", type=float, default=0, help="headless 模式跑多少秒后退出")
    ap.add_argument("--save-dir", default=str(PKG / "runtime"))
    args = ap.parse_args(argv)

    try:
        import cv2  # noqa: WPS433
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        print("需要 opencv：请先跑 setup.bat 装依赖（%s）" % exc)
        return 2

    cams = [int(x) for x in str(args.cams).replace(" ", "").split(",") if x]
    print("派 %s:%d，相机槽位 %s，请求 %dx%d q=%d，每路 %.1f fps"
          % (args.host, args.port, cams, args.width, args.height, args.quality, args.fps))
    print("只读模式：不连串口桥，不会和控制端抢会话。")

    interval = 1.0 / max(0.5, args.fps)
    stats = {c: {"n": 0, "bytes": 0, "err": 0, "t0": time.time()} for c in cams}
    paused = False
    t_end = time.time() + args.seconds if args.seconds else 0
    last = {c: None for c in cams}

    while True:
        if t_end and time.time() > t_end:
            break
        for cam in cams:
            if paused:
                break
            try:
                data, (w, h, seq) = grab(args.host, args.port, cam,
                                         args.width, args.height, args.quality)
                stats[cam]["n"] += 1
                stats[cam]["bytes"] += len(data)
                last[cam] = data
                if not args.headless:
                    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None:
                        if args.scale != 1.0:
                            img = cv2.resize(img, None, fx=args.scale, fy=args.scale,
                                             interpolation=cv2.INTER_AREA)
                        fps_now = stats[cam]["n"] / max(1e-6, time.time() - stats[cam]["t0"])
                        cv2.putText(img, "cam%d  %.1f KB  %.1f fps  seq=%d"
                                    % (cam, len(data) / 1024.0, fps_now, seq),
                                    (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                        cv2.imshow("view cam%d" % cam, img)
            except Exception as exc:  # noqa: BLE001
                stats[cam]["err"] += 1
                print("[cam %d] 取帧失败：%s" % (cam, exc))
                time.sleep(0.5)
        if not args.headless:
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
            if key == ord("p"):
                paused = not paused
                print("暂停" if paused else "继续")
            if key == ord("s"):
                stamp = time.strftime("%H%M%S")
                os.makedirs(args.save_dir, exist_ok=True)
                for cam in cams:
                    if last[cam]:
                        out = Path(args.save_dir) / ("view_%d_%s.jpg" % (cam, stamp))
                        out.write_bytes(last[cam])
                        print("已保存", out)
        else:
            time.sleep(max(0.0, interval - 0.02))
        # 限速：headless 也按 fps 控制
        if args.headless:
            time.sleep(interval)

    if not args.headless:
        cv2.destroyAllWindows()
    print("\n统计：")
    for cam in cams:
        s = stats[cam]
        dt = max(1e-6, time.time() - s["t0"])
        print("  cam%d: %d 帧, %.1f fps, 平均 %.1f KB/帧, 约 %.2f MB/s, 失败 %d"
              % (cam, s["n"], s["n"] / dt, s["bytes"] / max(1, s["n"]) / 1024.0,
                 s["bytes"] / dt / 1048576.0, s["err"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
