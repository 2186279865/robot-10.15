"""自动抓放状态机（骨架版，可假后端单测）。

设计原则（来自 `02-资料库/方案-省赛冲刺总纲-2026-10-26.md`）：
- 主控在 PC：本文件不含视觉算法、不含串口细节，只编排"什么时候让谁做什么"；
- 每一步都"等事件"而不是 sleep：`backend.goto()` / `backend.angle()` / `backend.gripper()`
  自己阻塞到回包（真 backend 用 `wait_reply`，假 backend 立即返回）；
- 失败一律进 `FAULT`，带 `reason` 与已完成计数，绝不带病往下走；
- 软限位：累计脉冲超出 `limits.json` 就拒绝并进 FAULT（`E15`），防止撞机械限位。

状态流转（单件）：

    IDLE → CAPTURE → PLAN → CORRIDOR_OUT → ALIGN → DESCEND → GRASP
         → LIFT → RETREAT → CORRIDOR_BACK → RELEASE → DISPLAY
         → (还有货 → CAPTURE) / (没货 → DONE)

`CORRIDOR_OUT` 固定"先 Y 让开、再抬 Z"（因为初始位上方有遮挡，机构不能改）；
`CORRIDOR_BACK` 严格反序取反。
"""
import json
import time
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
RUNTIME = PKG_ROOT / 'runtime'

IDLE = 'IDLE'
CAPTURE = 'CAPTURE'
PLAN = 'PLAN'
CORRIDOR_OUT = 'CORRIDOR_OUT'
ALIGN = 'ALIGN'
DESCEND = 'DESCEND'
GRASP = 'GRASP'
LIFT = 'LIFT'
RETREAT = 'RETREAT'
CORRIDOR_BACK = 'CORRIDOR_BACK'
RELEASE = 'RELEASE'
DISPLAY = 'DISPLAY'
DONE = 'DONE'
FAULT = 'FAULT'

AXIS_NAME = {1: 'Z', 3: '托盘', 4: 'X', 5: 'Y'}


class RunnerError(RuntimeError):
    """后端返回失败/软限位越界/非法配置。"""


def _now():
    return time.strftime('%H:%M:%S')


class AutoRunner:
    """一次比赛流程：拍照 → 规划 → 走通道 → 对准 → 抓 → 回 → 放 → 显示。"""

    def __init__(self, backend, cfg=None, route=None, limits=None, log=None,
                 max_items=50, on_state=None):
        self.backend = backend
        self.cfg = cfg or {}
        self.route = route or {}
        self.limits = limits or {}
        self.log = log or (lambda text: None)
        self.max_items = int(max_items)
        self.on_state = on_state or (lambda state, info: None)

        self.state = IDLE
        self.reason = ''
        self.item = 0
        self.placed = 0
        self.done_reason = ''
        self.target = None
        self.box = 0
        self.pos = {axis: 0 for axis in AXIS_NAME}
        self.p1 = {axis: 0 for axis in AXIS_NAME}   # 通道出口位置（软记账）

    # ---------- 对外 ----------

    def start(self):
        self.state = IDLE
        self.reason = ''
        self.item = 0
        self.placed = 0
        self.done_reason = ''
        self.log('自动流程开始')
        self._set(CAPTURE)

    def run(self):
        """一路跑到 DONE/FAULT（假后端单测用；真机也这样跑，因为每步都阻塞等回包）。"""
        self.start()
        guard = 0
        while self.state not in (DONE, FAULT):
            guard += 1
            if guard > 400:
                self._fail('状态机空转超过 400 步，放弃')
                break
            self.step()
        return self.state

    def step(self):
        """推进一个状态（幂等：可在界面定时器里反复调用）。"""
        handler = getattr(self, '_st_' + self.state.lower(), None)
        if handler is None:
            self._fail('未知状态 %s' % self.state)
            return
        try:
            handler()
        except RunnerError as exc:
            self._fail(str(exc))
        except Exception as exc:                     # noqa: BLE001 —— 兜底进 FAULT
            self._fail('%s: %s' % (type(exc).__name__, exc))

    def summary(self):
        return {
            'state': self.state, 'reason': self.reason, 'item': self.item,
            'placed': self.placed, 'done_reason': self.done_reason,
        }

    # ---------- 状态处理 ----------

    def _st_idle(self):
        self._set(CAPTURE)

    def _st_capture(self):
        if self.placed >= self.max_items:
            self.done_reason = '已放满 %d 件' % self.max_items
            return self._set(DONE)
        targets = self.backend.capture() or []
        targets = [t for t in targets if t.get('center_tray')]
        if not targets:
            self.done_reason = '抓取区没有目标了'
            return self._set(DONE)
        self._pending_targets = targets
        self.log('拍到 %d 个目标' % len(targets))
        self._set(PLAN)

    def _st_plan(self):
        targets = getattr(self, '_pending_targets', [])
        if not targets:
            return self._set(CAPTURE)
        # 排序：先按"离通道口近"（v 小），再按面积大（好抓）
        def key(t):
            u, v = t['center_tray']
            return (round(v, 2), -float(t.get('area_px') or 0))
        targets.sort(key=key)
        self.target = targets[0]
        try:
            self.box = int(self.backend.box_for(self.target))
        except Exception:                            # 盒映射不可用就别硬来
            raise RunnerError('盒子分配失败')
        self.log('第 %d 件：%s → 盒 %d' % (self.item + 1,
                                         self._label(self.target), self.box))
        self.item += 1
        self._set(CORRIDOR_OUT)

    def _st_corridor_out(self):
        for step in self._corridor():
            self._goto(step['axis'], step['pulses'], step.get('rpm'))
        self.p1 = dict(self.pos)
        self._set(ALIGN)

    def _st_align(self):
        dx, dy = self.backend.xy_for(self.target)
        if dx:
            self._goto(4, int(dx))
        if dy:
            self._goto(5, int(dy))
        self._set(DESCEND)

    def _st_descend(self):
        z_low, z_pick = self._z_low(), self._z_pick()
        self._goto(1, int(z_pick - z_low), self._rpm())
        self._set(GRASP)

    def _st_grasp(self):
        if not self.backend.gripper('grab'):
            raise RunnerError('夹爪夹紧失败')
        self._set(LIFT)

    def _st_lift(self):
        z_high = self._z_high()
        self._goto(1, int(z_high - self._z_pick()), self._rpm())
        self._set(RETREAT)

    def _st_retreat(self):
        for axis in (4, 5):
            delta = int(self.p1.get(axis, 0) - self.pos.get(axis, 0))
            if delta:
                self._goto(axis, delta)
        self._set(CORRIDOR_BACK)

    def _st_corridor_back(self):
        for step in reversed(self._corridor()):
            self._goto(step['axis'], -int(step['pulses']), step.get('rpm'))
        self._set(RELEASE)

    def _st_release(self):
        deg = self.backend.box_angle(self.box)
        if deg is not None:
            if not self.backend.angle(3, float(deg)):
                raise RunnerError('转盘转到盒 %d 失败' % self.box)
        if not self.backend.gripper('open'):
            raise RunnerError('夹爪松开失败')
        self.backend.gripper('ready')
        self.placed += 1
        self._set(DISPLAY)

    def _st_display(self):
        text = '第 %d 件：%s → 盒 %d' % (self.placed, self._label(self.target), self.box)
        try:
            self.backend.display(text, self.summary())
        except Exception:                            # 显示失败不该中断分拣
            pass
        self.log(text)
        self._set(CAPTURE)

    # ---------- 工具 ----------

    def _set(self, state):
        self.state = state
        self.on_state(state, self.summary())

    def _fail(self, reason):
        self.state = FAULT
        self.reason = reason
        self.log('FAULT：%s' % reason)
        self.on_state(FAULT, self.summary())

    def _label(self, target):
        return str(target.get('class_name') or target.get('class_id') or '未知')

    def _corridor(self):
        steps = self.route.get('corridor') or []
        if not steps:
            raise RunnerError('没有配置通道（route.json 的 corridor）')
        return steps

    def _rpm(self):
        return int(self.route.get('rpm') or 300)

    def _z_low(self):
        return int(self.route.get('z_low') or 0)

    def _z_pick(self):
        return int(self.route.get('z_pick') or 0)

    def _z_high(self):
        return int(self.route.get('z_high') or 0)

    def _goto(self, axis, pulses, rpm=None):
        axis = int(axis)
        pulses = int(pulses)
        if axis not in AXIS_NAME:
            raise RunnerError('不支持的轴 %d' % axis)
        if pulses == 0:
            return
        target = self.pos.get(axis, 0) + pulses
        lim = self.limits.get(str(axis)) or self.limits.get(axis) or {}
        lo, hi = lim.get('min'), lim.get('max')
        if lo is not None and target < int(lo):
            raise RunnerError('E15 %s 软限位：%d < %d' % (AXIS_NAME[axis], target, int(lo)))
        if hi is not None and target > int(hi):
            raise RunnerError('E15 %s 软限位：%d > %d' % (AXIS_NAME[axis], target, int(hi)))
        result = self.backend.goto(axis, pulses, rpm if rpm is not None else self._rpm())
        if isinstance(result, dict):
            if not result.get('ok', False):
                raise RunnerError('%s 走位失败：%s' % (AXIS_NAME[axis], result.get('err')))
        elif not result:
            raise RunnerError('%s 走位失败' % AXIS_NAME[axis])
        self.pos[axis] = target


def load_json(path, default):
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict(default)


def load_route(path=None):
    return load_json(path or (RUNTIME / 'route.json'), {})


def load_limits(path=None):
    return load_json(path or (PKG_ROOT / 'host' / 'limits.json'), {})


def main(argv=None):
    """CLI：`python host/auto_runner.py --dry-run` 打印将要做什么（不连硬件）。"""
    import argparse

    parser = argparse.ArgumentParser(description='自动抓放状态机（骨架）')
    parser.add_argument('--dry-run', action='store_true', help='只打印配置，不连硬件')
    args = parser.parse_args(argv)
    route = load_route()
    limits = load_limits()
    print('route  =', json.dumps(route, ensure_ascii=False))
    print('limits =', json.dumps(limits, ensure_ascii=False))
    if args.dry_run:
        runner = AutoRunner(FakeBackend(), route=route, limits=limits, log=print)
        print('state =', runner.run(), runner.summary())
    return 0


class FakeBackend:
    """假后端：不连硬件，给自测/演练用。"""

    def __init__(self, targets=None, box_angles=None):
        self.calls = []
        self._targets = targets if targets is not None else [
            {'class_name': '五棱柱|蓝', 'center_tray': [0.3, 0.4], 'area_px': 9000},
        ]
        self._boxes = {}
        self._box_angles = box_angles or {}

    def capture(self):
        """每次拍照返回还没抓的那批里最靠前的一个（模拟"抓走一个就少一个"）。"""
        self.calls.append(('capture',))
        if not self._targets:
            return []
        target = self._targets.pop(0)
        return [target]

    def box_for(self, target):
        key = str(target.get('class_name') or '未知')
        if key not in self._boxes:
            self._boxes[key] = len(self._boxes) + 1
        self.calls.append(('box_for', key, self._boxes[key]))
        return self._boxes[key]

    def box_angle(self, box):
        return self._box_angles.get(box, (int(box) - 1) * 60.0)

    def xy_for(self, target):
        # 假标定：归一化坐标 → 脉冲，只为把流程走通
        u, v = target['center_tray']
        return int((u - 0.5) * 20000), int((v - 0.5) * 20000)

    def goto(self, axis, pulses, rpm=300):
        self.calls.append(('goto', int(axis), int(pulses), int(rpm)))
        return {'ok': True}

    def angle(self, axis, deg):
        self.calls.append(('angle', int(axis), float(deg)))
        return True

    def gripper(self, action):
        self.calls.append(('gripper', str(action)))
        return True

    def display(self, text, state=None):
        self.calls.append(('display', str(text)))


if __name__ == '__main__':
    raise SystemExit(main())
