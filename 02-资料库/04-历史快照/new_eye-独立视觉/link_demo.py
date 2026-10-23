"""不依赖相机和 GUI 的“视觉端假帧发送器”，用来先把笔记本 ↔ 香橙派的链路跑通。

什么时候用：
- 相机/识别还没就绪，但想先验证热点、防火墙、自动发现、接收端脚本；
- 换热点/换路由器后，快速确认 UDP 5056 自动发现还能用；
- 给单片机联调时，先喂固定节奏的假目标，不依赖视觉出帧。

用法（在仓库根目录）：
    python new_eye/link_demo.py                  # 1s 一帧，发到 5055，Ctrl+C 停
    python new_eye/link_demo.py --interval .5    # 2 fps
    python new_eye/link_demo.py --count 5        # 发 5 帧就退出
    python new_eye/link_demo.py --selftest       # 本机自检：起服务端 + 客户端，验证发现和收帧

接收端（香橙派）：
    python3 new_eye/link_robot_example.py            # 自动发现
    python3 new_eye/link_robot_example.py 192.168.137.1 5055
"""
import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import link  # noqa: E402

# 和真实视觉帧同 schema：u/v 是托盘归一化坐标
TARGETS = [
    {'id': 1, 'class_id': '5', 'class_name': '圆柱黄', 'u': 0.32, 'v': 0.41, 'area': 0.021},
    {'id': 2, 'class_id': '3', 'class_name': '五棱体红', 'u': 0.68, 'v': 0.55, 'area': 0.017},
]


def fake_frame(seq, phase=0.0):
    targets = []
    for i, t in enumerate(TARGETS):
        d = dict(t)
        d['u'] = round(max(.05, min(.95, t['u'] + .06 * math.sin(phase + i))), 4)
        d['v'] = round(max(.05, min(.95, t['v'] + .04 * math.cos(phase + i))), 4)
        targets.append(d)
    return {'seq': seq, 'ts': round(time.time(), 3), 'fps': 1.0,
            'n': len(targets), 'targets': targets,
            'fusion': {'matched': len(targets), 'axis': 'u', 'flip': True}}


def _hotspot_hint(ips):
    for ip in ips:
        if ip.startswith('192.168.137.'):
            return ip
    return ips[0] if ips else '127.0.0.1'


def serve(port=5055, interval=1.0, count=None):
    srv = link.TargetServer(port)
    srv.start()
    ips = link._local_ips() or ['<未获取到>']
    print('视觉端演示已启动：TCP 端口 %d，UDP 发现端口 %d' % (port, link.DISCOVER_PORT))
    print('本机 IP：%s' % ', '.join(ips))
    print('机器人手输地址时用热点网段那个：tcp://%s:%d' % (_hotspot_hint(ips), port))
    print('等待机器人连接（自动发现会自动找到这里）... Ctrl+C 退出')
    seq = 0
    try:
        while count is None or seq < count:
            t0 = time.time()
            seq += 1
            p = fake_frame(seq, seq * 0.5)
            n = srv.send(p)
            print('第 %d 帧 → %d 个客户端在线' % (seq, n))
            time.sleep(max(0.0, interval - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        print('演示结束')


def selftest(port=5099):
    """同一台机器上验证：能监听、能广播、客户端能自动发现并解析出一条完整帧。"""
    try:
        srv = link.TargetServer(port)
        srv.start()
    except OSError as e:
        print('自检失败：TCP %d 启动不了（端口被占用？）：%s' % (port, e))
        return 1
    got = []
    cli = link.TargetClient('auto', port, on_frame=lambda f: got.append(f),
                            on_log=lambda m: print('[客户端]', m))
    cli.start()
    t0 = time.time()
    try:
        while time.time() - t0 < 10 and not got:
            srv.send(fake_frame(1))
            time.sleep(.5)
    finally:
        cli.stop()
        srv.stop()
    if not got:
        print('自检失败：10 秒内没收到帧。检查 UDP %d 是否被其它程序占用、防火墙是否拦截。'
              % link.DISCOVER_PORT)
        return 1
    f = got[-1]
    print('自检通过：收到第 %s 帧，%d 个目标，u/v = %s'
          % (f.get('seq'), f.get('n'), [(t['u'], t['v']) for t in f['targets']]))
    return 0


def main():
    ap = argparse.ArgumentParser(description='视觉端假帧演示 / 通讯链路自检')
    ap.add_argument('--port', type=int, default=5055)
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--count', type=int, default=None)
    ap.add_argument('--selftest', action='store_true', help='本机自检后退出')
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    serve(a.port, a.interval, a.count)
    return 0


if __name__ == '__main__':
    sys.exit(main())
