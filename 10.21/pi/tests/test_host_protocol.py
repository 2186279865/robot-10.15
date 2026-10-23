#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""10.21/pi/tests/test_host_protocol.py —— 用**真实上位机代码**验收派端假固件。

它 import `10.21/host/protocol.py` 与 `10.21/host/host_client.py`（只读，不修改），
通过串口桥 TCP 端口把上位机真实会发的命令逐条打过去，检查回包是否被真实解析器
接受、状态机是否按预期变化。这比手写字符串断言更严格：格式/校验/字段名错了，
HostClient 会直接把它当坏帧。

先起服务::

    python 10.21/pi/pi_agent.py --fake-camera --fake-serial \
        --img-port 15057 --ser-port 15058 --no-discovery
    python 10.21/pi/tests/test_host_protocol.py --ser-port 15058

覆盖：
  握手 hello / ping / status / motor_enable / motor_speed / motor_pos(rel)
  motor_query(pos) / gripper(open) / param_get / param_set / tele_on+遥测
  stop_all → 锁存 → 运动命令被拒 E10 → stop_clear 解锁
  多客户端并发 + 会话去重（同 id 同内容回缓存）+ 同 id 冲突 E12 + 未知命令 E01
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 10.21/pi/tests
PI_DIR = HERE.parent                            # 10.21/pi
COMMON_DIR = PI_DIR.parent / 'common'           # 10.21/common
HOST_DIR = PI_DIR.parent / 'host'               # 10.21/host
sys.path.insert(0, str(HOST_DIR))
sys.path.insert(0, str(COMMON_DIR))

import protocol as P        # noqa: E402  （真实协议实现）
import host_client as HC    # noqa: E402  （真实上位机客户端）

FAILS = []
PASSES = []


def check(name, ok, detail=''):
    print('[%s] %s%s' % ('PASS' if ok else 'FAIL', name,
                         ('   (%s)' % detail) if detail else ''), flush=True)
    (PASSES if ok else FAILS).append(name)


class TcpLink:
    """和 host/serial_link.py 同形的链路对象：open/close/describe/write/read/connected。"""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.connected = False

    def describe(self):
        return 'tcp://%s:%d' % (self.host, self.port)

    def open(self):
        if self.sock is not None:      # 幂等：重复 open 不能泄漏旧连接
            self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=3.0)
        self.sock.settimeout(0.0)      # 非阻塞：read() 没数据返回 b''
        self.connected = True

    def close(self):
        self.connected = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def write(self, data):
        if not self.connected:
            return False
        try:
            self.sock.sendall(data)
            return True
        except OSError:
            self.connected = False
            return False

    def read(self):
        if not self.connected:
            return b''
        try:
            data = self.sock.recv(65536)
        except (BlockingIOError, socket.timeout, InterruptedError):
            return b''
        except OSError:
            self.connected = False
            return b''
        if not data:
            self.connected = False
        return data


def pump(client, link, recorder, seconds=0.02):
    end = time.time() + seconds
    while True:
        if link.connected:
            data = link.read()
            if data:
                client.feed_bytes(data)
        client.tick()
        recorder.extend(client.poll_events())
        if time.time() >= end:
            break
        time.sleep(0.005)


def wait_online(client, link, recorder, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        pump(client, link, recorder, 0.02)
        if client.online:
            return True
    return False


def wait_reply(client, link, recorder, cursor, rid, timeout=2.5):
    """等某请求的应答/拒绝事件（从 cursor 往后看，避免误取旧事件）。"""
    end = time.time() + timeout
    while time.time() < end:
        pump(client, link, recorder, 0.02)
        while cursor[0] < len(recorder):
            event = recorder[cursor[0]]
            cursor[0] += 1
            if event.kind in ('reply', 'err') and event.data.get('req_id') == rid:
                return event
    return None


def send(client, link, recorder, cursor, cmd, *args, timeout=2.5):
    rid = client.send(cmd, *args)
    return rid, wait_reply(client, link, recorder, cursor, rid, timeout)


def main(argv=None):
    # 重定向到管道/文件时强制 UTF-8（Windows 管道默认 GBK，会让中文和 ✓ 乱码/报错）
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser(description='真实上位机协议 ↔ 派端假固件 互操作测试')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--ser-port', type=int, default=15058)
    args = ap.parse_args(argv)

    recorder = []
    cursor = [0]
    print('真实上位机协议互操作测试：目标 %s:%d' % (args.host, args.ser_port))

    # ================================================================ 主链路
    link = TcpLink(args.host, args.ser_port)
    client = HC.HostClient(link=link, service=False)
    # 注意：HostClient.connect() 自己会调 link.open()，这里不要再手工 open。
    if not client.connect():
        print('无法连接串口桥 %s:%d，请先启动 pi_agent --fake-serial'
              % (args.host, args.ser_port))
        return 1
    ok = wait_online(client, link, recorder)
    check('握手进入 online', ok, 'state=%s' % client.state)
    check('固件版本可解析 fw=pi-sim-0.1', client.fw_version == 'pi-sim-0.1',
          client.fw_version)
    check('启动标识 boot 非空', bool(client.boot_id), client.boot_id)
    check('轴列表 = 1/3/4/5', client.axis_list == (1, 3, 4, 5), str(client.axis_list))
    check('回读轴 = 1/3/4/5', client.readback_axes == (1, 3, 4, 5),
          str(client.readback_axes))

    # ---------------------------------------------------------------- 基本命令
    rid, ev = send(client, link, recorder, cursor, 'ping')
    check('ping ok', ev is not None and ev.kind == 'reply',
          ev.text if ev else 'timeout')

    rid, ev = send(client, link, recorder, cursor, 'status')
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('status ok 且 latch=0',
          ev is not None and ev.kind == 'reply' and pairs.get('latch') == '0',
          str(pairs))

    rid, ev = send(client, link, recorder, cursor, 'motor_enable', 1, 1)
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('motor_enable addr=1 使能',
          ev is not None and ev.kind == 'reply' and pairs.get('addr') == '1'
          and pairs.get('enabled') == '1', str(pairs))

    rid, ev = send(client, link, recorder, cursor, 'motor_speed', 1, 1, 300, 1)
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('motor_speed addr=1 rpm=300 被接纳',
          ev is not None and ev.kind == 'reply' and pairs.get('rpm') == '300'
          and 'task' in pairs, str(pairs))

    rid, ev = send(client, link, recorder, cursor, 'motor_query', 1, 'pos')
    pairs = ev.data.get('pairs', {}) if ev else {}
    counts = client.last_counts.get(1, {}).get('counts')
    check('motor_query pos 回 addr/counts/what',
          ev is not None and ev.kind == 'reply' and pairs.get('addr') == '1'
          and 'counts' in pairs and pairs.get('what') == 'pos', str(pairs))
    check('HostClient 记录 last_counts[1]', counts is not None,
          'counts=%s' % counts)

    rid, ev = send(client, link, recorder, cursor, 'motor_query', 4, 'pos')
    check('motor_query addr=4 可查', ev is not None and ev.kind == 'reply',
          str(ev.data.get('pairs') if ev else 'timeout'))

    rid, ev = send(client, link, recorder, cursor, 'gripper', 'open')
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('gripper open ok angle=180',
          ev is not None and ev.kind == 'reply' and pairs.get('angle') == '180',
          str(pairs))

    rid, ev = send(client, link, recorder, cursor, 'gripper', 'ready')
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('gripper ready ok action=ready angle=150 settle_ms=200',
          ev is not None and ev.kind == 'reply'
          and pairs.get('action') == 'ready'
          and pairs.get('angle') == '150'
          and pairs.get('settle_ms') == '200', str(pairs))

    rid, ev = send(client, link, recorder, cursor, 'param_get', 'gripper_ready_deg')
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('param_get gripper_ready_deg = 150',
          ev is not None and ev.kind == 'reply' and pairs.get('value') == '150',
          str(pairs))

    rid, ev = send(client, link, recorder, cursor, 'param_set', 'gripper_hold_max_ms', 5000)
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('param_set gripper_hold_max_ms 回 before=0 value=5000',
          ev is not None and ev.kind == 'reply' and pairs.get('before') == '0'
          and pairs.get('value') == '5000', str(pairs))

    rid, ev = send(client, link, recorder, cursor, 'param_get', 'speed_rpm_max')
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('param_get speed_rpm_max = 300',
          ev is not None and ev.kind == 'reply' and pairs.get('value') == '300',
          str(pairs))

    rid, ev = send(client, link, recorder, cursor, 'param_set', 'speed_rpm_max', 250)
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('param_set 回 before=300 value=250',
          ev is not None and ev.kind == 'reply' and pairs.get('before') == '300'
          and pairs.get('value') == '250', str(pairs))

    rid, ev = send(client, link, recorder, cursor, 'param_get', 'speed_rpm_max')
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('param_set 生效（再查 = 250）',
          ev is not None and ev.kind == 'reply' and pairs.get('value') == '250',
          str(pairs))

    # ---------------------------------------------------------------- 遥测 + 运动事件
    rid, ev = send(client, link, recorder, cursor, 'tele_on', 4, 100)
    check('tele_on ok', ev is not None and ev.kind == 'reply',
          ev.text if ev else 'timeout')
    end = time.time() + 0.6
    while time.time() < end:
        pump(client, link, recorder, 0.02)
    tele_events = [e for e in recorder if e.kind == 'tele']
    check('收到遥测 tele（>=1 条）', len(tele_events) >= 1,
          '共 %d 条，样例 %s' % (len(tele_events),
                                 tele_events[0].text if tele_events else '-'))

    rid, ev = send(client, link, recorder, cursor, 'motor_pos', 4, 1, 500, 1, 60000, 'rel')
    pairs = ev.data.get('pairs', {}) if ev else {}
    check('motor_pos rel 被接纳（mode=rel）',
          ev is not None and ev.kind == 'reply' and pairs.get('mode') == 'rel',
          str(pairs))
    task_id = pairs.get('task', '')
    end = time.time() + 1.0
    while time.time() < end:
        pump(client, link, recorder, 0.02)
        reached = [e for e in recorder
                   if e.kind == 'event' and e.data.get('type') == 'task'
                   and e.data.get('state') == 'reached'
                   and str(e.data.get('id')) == str(task_id)]
        if reached:
            break
    check('收到 task reached 事件（到位证据）', bool(reached),
          'task=%s' % task_id)

    # ---------------------------------------------------------------- 急停/锁存/解锁
    rid, ev = send(client, link, recorder, cursor, 'stop_all')
    check('stop_all ok', ev is not None and ev.kind == 'reply',
          str(ev.data.get('pairs') if ev else 'timeout'))
    check('stop_all 后 HostClient.latched=True', client.latched is True,
          'latched=%s' % client.latched)

    rid, ev = send(client, link, recorder, cursor, 'motor_speed', 3, 1, 100, 1)
    check('锁存时运动命令被拒 E10',
          ev is not None and ev.kind == 'err' and ev.data.get('code') == 'E10',
          str(ev.data) if ev else 'timeout')

    rid, ev = send(client, link, recorder, cursor, 'stop_clear')
    check('stop_clear ok latch=0',
          ev is not None and ev.kind == 'reply'
          and ev.data.get('pairs', {}).get('latch') == '0',
          str(ev.data.get('pairs') if ev else 'timeout'))
    check('stop_clear 后 HostClient.latched=False', client.latched is False,
          'latched=%s' % client.latched)

    rid, ev = send(client, link, recorder, cursor, 'tele_off')
    check('tele_off ok', ev is not None and ev.kind == 'reply',
          ev.text if ev else 'timeout')

    check('整段流程中无坏帧', client.bad_frame_count == 0,
          'bad_frame=%d oversize=%d' % (client.bad_frame_count,
                                        client.oversize_count))

    # ================================================================ 第二连接：去重/冲突/未知命令
    print('\n--- 多客户端 + 会话去重 ---')
    link2 = TcpLink(args.host, args.ser_port)
    link2.open()
    framing = P.FramingBuffer()

    def raw_send(data):
        link2.write(data)

    def raw_read(seconds=0.4):
        frames = []
        end = time.time() + seconds
        while time.time() < end:
            data = link2.read()
            if data:
                for inbound in framing.feed(data):
                    if inbound.frame is not None:
                        frames.append(inbound.frame)
            else:
                time.sleep(0.004)
        return frames

    def pick(frames, req_id, kind=None):
        for frame in frames:
            if frame.req_id == req_id and (kind is None or frame.kind == kind):
                return frame
        return None

    raw_send(P.encode_request(900, 'hello', P.PROTOCOL_VERSION, 4242))
    frames = raw_read(0.6)
    hello = pick(frames, 900, 'ok')
    check('第二连接 hello 被接受（多客户端并发）', hello is not None, str(frames))
    if hello is not None:
        check('第二连接 hello pairs 含 axes/readback',
              hello.pairs.get('axes') == '1/3/4/5'
              and hello.pairs.get('readback') == '1/3/4/5', str(hello.pairs))

    dedupe = P.encode_request(901, 'ping')
    raw_send(dedupe)
    raw_send(dedupe)          # 同 id 同内容重发：应复用缓存应答，不报 E12
    frames = raw_read(0.6)
    pings = [f for f in frames if f.req_id == 901 and f.kind == 'ok']
    check('同 id 同内容重发得到 2 次缓冲应答（去重）', len(pings) == 2,
          'count=%d' % len(pings))

    raw_send(P.encode_request(901, 'status'))   # 同 id 不同内容：E12
    frames = raw_read(0.6)
    conflict = pick(frames, 901, 'err')
    check('同 id 不同内容回 E12 冲突',
          conflict is not None and conflict.error_code == 'E12',
          str(frames))

    raw_send(P.encode_request(902, 'nonsense_cmd'))
    frames = raw_read(0.5)
    unknown = pick(frames, 902, 'err')
    check('未知命令回 E01',
          unknown is not None and unknown.error_code == 'E01', str(frames))

    raw_send(b'$5,ping*00\n')     # 校验和错的帧：必须被静默丢弃
    frames = raw_read(0.4)
    check('坏校验帧不应答', pick(frames, 5) is None, str(frames))

    link2.close()

    # 主链路仍然在线（多客户端策略：允许并发，不踢人）
    pump(client, link, recorder, 0.1)
    check('主链路在第二连接活动后仍在线', client.online,
          'state=%s' % client.state)

    client.disconnect('测试结束')
    link.close()

    # ================================================================ 汇总
    print('\n================ 汇总 ================')
    print('通过 %d 项，失败 %d 项' % (len(PASSES), len(FAILS)))
    if FAILS:
        for name in FAILS:
            print('  [FAIL] %s' % name)
        return 1
    print('[OK] 真实上位机协议与派端假固件完全互通')
    return 0


if __name__ == '__main__':
    sys.exit(main())
