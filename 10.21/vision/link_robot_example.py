"""机器人端接收示例（跑在香橙派 / 树莓派 / 另一台电脑上）。

用法：
    python link_robot_example.py                      # 自动发现视觉端（推荐，无线/有线都行）
    python link_robot_example.py 192.168.137.1 5055   # 指定 IP（发现失败时用）
    python link_robot_example.py 192.168.137.1 5055 COM3 115200   # 同时转发给单片机

视觉端（笔记本）GUI 顶栏勾上“网络推送”后会每隔 0.8s 往 UDP 5056 广播，所以机器人端
不用知道笔记本 IP，换 WiFi、换网段都能自己找到。
单片机串口转发用 link.pack_uart()：0xAA55 + 长度 + JSON + CRC16。
"""
import sys
import time

import link

try:
    import serial
except ImportError:
    serial = None


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else 'auto'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5055
    ser = None
    if len(sys.argv) > 4 and serial is not None:
        ser = serial.Serial(sys.argv[3], int(sys.argv[4]), timeout=.05)
        print('串口已打开', sys.argv[3])

    last = [0.0]

    def on_frame(f):
        now = time.time()
        dt = now - last[0] if last[0] else 0
        last[0] = now
        parts = []
        for t in f.get('targets', []):
            parts.append('#%s %s u=%.3f v=%.3f' % (t.get('id'), t.get('class_name'),
                                                   t.get('u', 0), t.get('v', 0)))
        print('[帧 %d] %d 个目标  %s  (间隔 %.2fs)' % (f.get('seq', 0), f.get('n', 0),
                                                     ' | '.join(parts), dt))
        if ser is not None:
            ser.write(link.pack_uart(f))

    cli = link.TargetClient(host, port, on_frame=on_frame, on_log=print)
    cli.start()
    try:
        while True:
            time.sleep(.2)
            # 简单超时保护：2 秒没有新帧就当作视觉断开
            if last[0] and time.time() - last[0] > 2.0:
                print('⚠ 超过 2 秒没有新帧，机器人应停止等待')
                last[0] = time.time()
    except KeyboardInterrupt:
        cli.stop()
        if ser is not None:
            ser.close()


if __name__ == '__main__':
    main()
