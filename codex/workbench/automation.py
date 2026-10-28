from __future__ import annotations

import base64
import json
import math
import socket
import time
from pathlib import Path

import cv2

from .robot import RobotError
from .settings import RUNTIME


def target_xy(item, cfg):
    if item.uv is None or len(item.uv) != 2 or not all(math.isfinite(x) and 0 <= x <= 1 for x in item.uv):
        raise RobotError('目标缺少已标定托盘坐标')
    k = cfg.get('affine')
    if not k:
        raise RobotError('没有 X/Y 仿射标定')
    u, v = item.uv
    x, y = k['a'] * u + k['b'] * v + k['tx'], k['c'] * u + k['d'] * v + k['ty']
    if not all(math.isfinite(q) for q in (x, y)):
        raise RobotError('标定结果不是有限数值')
    return x, y


def preflight(robot, cfg, item):
    robot.check_cancel()
    if not cfg['reference_confirmed']:
        raise RobotError('本次上电位置基准尚未确认')
    if not cfg['calibration_confirmed']:
        raise RobotError('标定和路线尚未确认，先完成配置页验收')
    if not robot.angle_referenced:
        raise RobotError('转盘角度零点尚未设置')
    if time.time() - item.measured_at > 5 or item.measured_at > time.time() + 1:
        raise RobotError('目标结果过期，请重新识别')
    if not item.stable or item.shape == '不确定' or item.color == '不确定':
        raise RobotError('目标分类未稳定，需至少两次一致识别')
    if not robot.simulated and (item.source == 'geometry' or item.visibility != '完整'):
        raise RobotError('真机自动分拣需要模型确认完整立体形状；几何截面结果可用于手动检查')
    if not robot.simulated and item.hollow == '不确定':
        raise RobotError('目标是否镂空尚未确定，需要补充视角后再自动分盒')
    if cfg['display_required'] and not cfg.get('display_host'):
        raise RobotError('开始前请配置比赛屏地址，或明确关闭比赛屏要求')
    x, y = target_xy(item, cfg)
    r = cfg['route']
    targets = {1: [r['z_low'], r['z_high'], r['z_pick']],
               4: [r['p0_x'], x], 5: [r['p0_y'], r['p1_y'], y]}
    # Validate the whole path before the first physical command.
    for axis, values in targets.items():
        limit = cfg['limits'].get(str(axis))
        if not limit or len(limit) != 2 or limit[0] >= limit[1]:
            raise RobotError(f'轴 {axis} 没有有效的软限位')
        for value in [robot.position(axis)] + values:
            if not math.isfinite(value) or not limit[0] <= value <= limit[1]:
                raise RobotError(f'轴 {axis} 路线或目标 {value:.0f} 超出软限位')
    tolerance = max(5, cfg['tolerance_counts'] / cfg['counts_per_pulse'] * 2)
    for axis, expected in ((1, r['z_low']), (4, r['p0_x']), (5, r['p0_y'])):
        if abs(robot.position(axis) - expected) > tolerance:
            raise RobotError(f'轴 {axis} 不在 P0，自动流程不能猜测通道起点')
    return x, y


class DisplaySender:
    def __init__(self, cfg):
        self.cfg = cfg

    def push(self, payload, crop):
        cfg = self.cfg
        host = cfg.get('display_host')
        if not host:
            if cfg['display_required']:
                raise RobotError('未配置比赛屏地址，不能开始下一件')
            return
        image = cv2.resize(crop, (240, 240))
        ok, encoded = cv2.imencode('.jpg', image)
        if ok:
            payload = dict(payload, image_b64=base64.b64encode(encoded).decode())
        try:
            with socket.create_connection((host, int(cfg['display_port'])), timeout=2) as sock:
                sock.settimeout(3)
                sock.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
                # New display endpoint acknowledges after the UI has applied the update.
                response = sock.recv(64)
                if response.strip() != b'OK':
                    raise RobotError('比赛屏没有确认显示结果，请使用 codex 的显示端')
        except (OSError, RobotError):
            if cfg['display_required']:
                raise RobotError('比赛屏显示失败；流程停止，已松爪件需人工核对') from None


class AutoSorter:
    def __init__(self, robot, cfg, capture, log=None, on_state=None, on_placed=None):
        self.robot, self.cfg, self.capture = robot, cfg, capture
        self.log = log or (lambda text: None)
        self.on_state = on_state or (lambda state: None)
        self.on_placed = on_placed or (lambda item: None)
        self.boxes = {}
        self.placed = 0
        self.released = 0
        self.state = 'IDLE'
        self.display = DisplaySender(cfg)
        self.log_path = RUNTIME / ('sort-' + time.strftime('%Y%m%d-%H%M%S') + '.jsonl')

    def set_state(self, state, **fields):
        self.state = state
        self.log(state)
        self.on_state(state)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(ts=time.time(), state=state, placed=self.placed,
                                         released=self.released, **fields), ensure_ascii=False) + '\n')

    def pick_one(self, item):
        x, y = preflight(self.robot, self.cfg, item)
        if item.class_key not in self.boxes:
            if len(self.boxes) >= 6:
                raise RobotError('出现第七种类别；请核对分类或开始新的分拣批次')
            self.boxes[item.class_key] = len(self.boxes) + 1
        box = self.boxes[item.class_key]
        r, robot = self.cfg['route'], self.robot
        self.set_state('TRAY', target=item.public(), box=box)
        robot.angle((box - 1) * 60)  # Absolute target, never a delta.
        robot.grip('ready')
        self.set_state('CORRIDOR_OUT')
        robot.move_abs(5, r['p1_y'])
        robot.move_abs(1, r['z_high'])
        self.set_state('ALIGN')
        robot.move_abs(4, x)
        robot.move_abs(5, y)
        self.set_state('DESCEND')
        robot.move_abs(1, r['z_pick'])
        self.set_state('GRASP')
        robot.grip('grab')
        robot.move_abs(1, r['z_high'])
        if self.cfg['verify_pick'] and not robot.simulated:
            self.set_state('VERIFY_PICK')
            observed = self.capture()
            if any(t.uv is not None and math.dist(t.uv, item.uv) < .045 for t in observed):
                raise RobotError('抓取复核失败：原位置仍有目标，不继续落货')
        self.set_state('RETURN')
        robot.move_abs(4, r['p0_x'])
        robot.move_abs(5, r['p1_y'])
        robot.move_abs(1, r['z_low'])
        robot.move_abs(5, r['p0_y'])
        self.set_state('RELEASE')
        robot.grip('open')
        self.released += 1
        robot.grip('ready')
        self.set_state('DISPLAY')
        self.display.push({'state': 'DISPLAY', 'item': self.placed + 1,
                           'total': self.cfg['max_items'], 'name': item.label,
                           'box': box, 'placed': self.placed + 1,
                           'message': '已完成抓放；落盒无独立传感器确认'}, item.crop)
        self.placed += 1
        self.on_placed(item)
        self.set_state('ITEM_DONE', box=box, class_key=item.class_key)

    def run(self, count=None):
        count = int(count or self.cfg['max_items'])
        if not 1 <= count <= 100:
            raise ValueError('件数须为 1~100')
        try:
            for _ in range(count):
                self.robot.check_cancel()
                self.set_state('CAPTURE')
                items = self.capture()
                if not items:
                    self.set_state('DONE_EMPTY')
                    return
                eligible = [t for t in items if t.stable and t.uv is not None
                            and t.shape != '不确定' and t.color != '不确定']
                if not eligible:
                    items = self.capture()
                    eligible = [t for t in items if t.stable and t.uv is not None
                                and t.shape != '不确定' and t.color != '不确定']
                if not eligible:
                    raise RobotError('两次识别后仍没有稳定可抓目标')
                item = max(eligible, key=lambda t: t.confidence)
                self.pick_one(item)
            self.set_state('DONE')
        except Exception as exc:
            self.robot.stop()
            self.set_state('FAULT', reason=str(exc))
            raise
