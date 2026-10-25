"""把 new_eye 的视觉目标变成 10.15 上位机的抓取序列。

流程（与机器人机械结构对应）：

    顶视识别 -> 选中目标 -> 本模块算 X/Y 脉冲 -> 生成 Sequence
    Z 抬到安全高度 -> X/Y 到位 -> Z 下降 -> 夹紧 -> Z 抬起
    -> X/Y 回到固定放料工位 -> 转盘转到该类别对应的储物盒 -> Z 下降 -> 松开 -> Z 抬起

约定：
* X/Y/Z 没接回读时一律用 `wait_ms` 等，**不假装到位**（`sequence.Step.validate` 会检查）。
* `motor_pos` 的脉冲数必须是**非负数**，方向单独放第 2 个参数（协议 E02，空跑时已踩过）。
* 托盘转盘（地址 3）按**相对角度**转，只转最短路径；当前盒子序号记在序列内部。
* 所有示教量（Z 高度、放料点、每类对应的盒子、转速）都在 `vision_grasp.json`，
  代码里不写死；`0` 表示还没示教，会给出 warning。

序列里轴号：1=Z、3=托盘转盘、4=X、5=Y（`protocol.VALID_AXES`）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import sequence as S
import box_map

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / 'vision_grasp.json'
#: 转盘现在停在哪个盒子（序列生成后更新，下次从上次停的位置继续，避免反复空转一圈）
STATE_PATH = ROOT / 'tray_state.json'

AXIS_Z, AXIS_TRAY, AXIS_X, AXIS_Y = 1, 3, 4, 5

DEFAULTS = {
    # 方向字段的"正"取值：上位机单轴面板里 正=0、负=1（与固件 STEPPER_DIR_POSITIVE=0 一致）。
    # 仿真器/个别固件把 1 当正，这时把它改成 1 即可，序列其余部分不用动。
    'dir_positive': 0,
    'speed': {'rpm': 40, 'acc': 30, 'travel_rpm': 120},
    'xy': {'timeout_ms': 9000, 'settle_ms': 300},
    'z': {
        'safe': 0,          # Z 安全高度（移动 X/Y 时停在这里），单位脉冲
        'grasp': {},        # 类别名 -> 下爪高度脉冲（绝对值）
        'lift': 0,          # 抓取后抬起高度脉冲（绝对值）
        'place': 0,         # 放料时下降高度脉冲（绝对值）
        'settle_ms': 300,
    },
    'drop': {'x': 0, 'y': 0},          # 固定放料工位（X/Y 脉冲绝对值）
    'tray': {
        'pulses_per_rev': 3200,        # 转盘一圈脉冲（1 圈 = 360°）
        'box_angle_deg': [0, 60, 120, 180, 240, 300],
        'class_to_box': {},            # 类别名/编号 -> 盒子序号（1..6），空=按类别编号
        'auto_assign': 1,             # 1=没登记的类别自动占空盒（现场无实物时靠它）
        'start_box': 1,                # 程序启动时转盘停在哪个盒子
        'settle_ms': 800,              # 转盘到位后等多久（无回读）
        'invert': 0,                   # 1 = 正脉冲是反向
    },
    'gripper': {'grab_settle_ms': 500, 'open_settle_ms': 400,
                #: 松开（180°）后等多久自动回预备位（150°），每件都做
                'ready_settle_ms': 500},
}


def _merge(base, extra):
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path=CFG_PATH):
    path = Path(path)
    if path.exists():
        return _merge(DEFAULTS, json.loads(path.read_text(encoding='utf-8')))
    return dict(DEFAULTS)


def save_config(cfg, path=CFG_PATH):
    path = Path(path)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def load_state(path=STATE_PATH, box_count=6):
    """转盘当前停在哪个盒子（1..box_count）。文件缺失/损坏时按盒 1。"""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        box = int(data.get('current_box'))
    except (OSError, ValueError, TypeError):
        return {'current_box': 1}
    return {'current_box': max(1, min(int(box_count), box))}


def save_state(box, path=STATE_PATH):
    """记下序列结束时转盘停在哪个盒子；下次生成序列从它继续。"""
    path = Path(path)
    payload = {'current_box': int(box), 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def _class_key(target):
    """类别的稳定标识：优先 class_name，其次 class_id。"""
    for key in ('class_name', 'class_id'):
        value = target.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in ('0', 'None', '未知'):
            return text
    return ''


def ensure_boxes(targets, cfg=None):
    """给还没登记盒子的类别分配空盒；手动映射（class_to_box）优先，不覆盖。"""
    cfg = cfg or load_config()
    if not cfg['tray'].get('auto_assign', 1):
        return {}
    manual = {str(k) for k in (cfg['tray'].get('class_to_box') or {})}
    known = box_map.load()
    assigned = {}
    for target in targets:
        key = _class_key(target)
        if not key or key in manual or key in known:
            continue
        try:
            box = box_map.assign(key)
        except ValueError:
            continue
        known[key] = box
        assigned[key] = box
    return assigned


def _box_of(target, cfg):
    """目标属于哪个储物盒（1..6）。手动映射 > 动态登记 > 按 class_id 兜底。"""
    mapping = cfg['tray'].get('class_to_box') or {}
    for key in (str(target.get('class_id') or ''), str(target.get('class_name') or '')):
        if key in mapping:
            try:
                return int(mapping[key])
            except (TypeError, ValueError):
                pass
    key = _class_key(target)
    if key:
        box = box_map.lookup(key)
        if box:
            return max(1, min(len(cfg['tray']['box_angle_deg']), int(box)))
    try:
        box = int(str(target.get('class_id') or '1'))
    except (TypeError, ValueError):
        box = 1
    return max(1, min(len(cfg['tray']['box_angle_deg']), box))


def _angle_of(box, cfg):
    angles = cfg['tray']['box_angle_deg']
    return float(angles[(int(box) - 1) % len(angles)])


def _tray_delta(from_box, to_box, cfg):
    """从 from_box 转到 to_box 的最短角度：返回带符号脉冲（负=反向）。"""
    if from_box == to_box:
        return 0.0
    a0, a1 = _angle_of(from_box, cfg), _angle_of(to_box, cfg)
    diff = (a1 - a0) % 360.0
    if diff > 180.0:
        diff -= 360.0
    pulses = diff * float(cfg['tray']['pulses_per_rev']) / 360.0
    if cfg['tray'].get('invert'):
        pulses = -pulses
    return pulses


def _motor(axis, pulses, rpm, acc, mode='abs', timeout_ms=8000, pos_dir=0):
    """位置模式：脉冲数非负、方向单独给（负脉冲会 E02，空跑已踩过）。

    `pos_dir` 是"正"在该固件里的取值（默认 0，与上位机单轴面板一致）。
    """
    direction = int(pos_dir) if pulses >= 0 else 1 - int(pos_dir)
    return S.Step('motor_pos', [axis, direction, rpm, acc, int(round(abs(pulses))), mode],
                  wait=S.WAIT_OK, timeout_ms=timeout_ms)


def _pulse_from_calib(u, v, calib):
    affine = calib.get('affine', calib)
    a, b, c, d = (float(affine[key]) for key in ('a', 'b', 'c', 'd'))
    tx, ty = float(affine['tx']), float(affine['ty'])
    return a * u + b * v + tx, c * u + d * v + ty


def sort_key(target, cfg):
    u, v = target['center_tray']
    return (_box_of(target, cfg), round(v, 2), round(u, 2))


def ordered_targets(targets, cfg=None):
    """按储物盒分组排序（同盒先 v 后 u），减少转盘来回转。"""
    cfg = cfg or load_config()
    ensure_boxes(targets, cfg)
    return sorted(targets, key=lambda target: sort_key(target, cfg))


def final_box(targets, cfg=None):
    """生成序列后转盘会停在哪个盒子（用于写 tray_state.json）。"""
    cfg = cfg or load_config()
    ordered = ordered_targets(targets, cfg)
    return _box_of(ordered[-1], cfg) if ordered else 1


def build_sequence(targets, calib, cfg=None, sequence_name='视觉抓取', start_box=None):
    """targets: new_eye 的 targets 列表（需含 center_tray / class_id / class_name）。

    返回 (Sequence, warnings, meta)：meta 里有 start_box（本次开始时的盒子）和
    final_box（结束时停在的盒子），调用方可以据此更新 `tray_state.json`。
    `start_box` 为 None 时用 cfg['tray']['start_box']。
    """
    cfg = cfg or load_config()
    ensure_boxes(targets, cfg)
    warnings = []
    speed = cfg['speed']
    z = cfg['z']
    xy = cfg['xy']
    tray = cfg['tray']
    grip = cfg['gripper']

    if not targets:
        raise ValueError('没有选中任何目标')

    # 按储物盒分组，减少转盘来回转；同盒内按位置排序，路径更短。
    ordered = ordered_targets(targets, cfg)

    steps = []
    steps.append(S.Step('note', [f'视觉抓取：{len(ordered)} 个目标（来自 new_eye）']))
    steps.append(S.Step('note', ['前置条件：X/Y/Z 已回零（位置参考有效）；abs 位置模式在没回零时会 E13']))
    if z['safe'] == 0:
        warnings.append('Z 安全高度 z.safe 还没示教（=0），确认机械不动或先示教')
    if z['lift'] == 0:
        warnings.append('z.lift 还没示教（=0），抓完可能抬不起来')
    for axis in (AXIS_Z, AXIS_X, AXIS_Y):
        steps.append(S.Step('motor_enable', [axis, 1], timeout_ms=3000))

    # 开始前先把夹爪放到预备位（150°）：上电是 180° 松开位，抓取前统一到 150°。
    steps.append(S.Step('gripper', ['ready'], timeout_ms=3000))

    current_box = int(start_box if start_box is not None else tray.get('start_box', 1))
    start_box = current_box
    pos_dir = int(cfg.get('dir_positive', 0) or 0)

    def motor(axis, pulses, rpm, acc, mode='abs', timeout_ms=8000):
        return _motor(axis, pulses, rpm, acc, mode, timeout_ms, pos_dir)

    for index, target in enumerate(ordered, 1):
        u, v = [float(x) for x in target['center_tray']]
        px, py = _pulse_from_calib(u, v, calib)
        box = _box_of(target, cfg)
        cls = target.get('class_name') or target.get('class_id') or '未知'
        grasp_z = z['grasp'].get(cls, z['grasp'].get(str(target.get('class_id') or ''), None))
        if grasp_z is None:
            grasp_z = z['lift'] or 0
            warnings.append(f'{cls} 没有单独示教下爪高度，暂用 z.lift')

        steps.append(S.Step('note', [f'#{index} 目标 {target.get("id")} {cls} '
                                     f'u={u:.3f} v={v:.3f} -> X={px:.0f} Y={py:.0f} 盒{box}']))
        # 1) Z 抬到安全高度，再动 X/Y（防止刮到托盘）
        if z['safe']:
            steps.append(motor(AXIS_Z, z['safe'], speed['rpm'], speed['acc'],
                                timeout_ms=xy['timeout_ms']))
            steps.append(S.Step('wait_ms', [z['settle_ms']]))
        # 2) X/Y 到目标
        steps.append(motor(AXIS_X, px, speed['travel_rpm'], speed['acc'],
                            timeout_ms=xy['timeout_ms']))
        steps.append(motor(AXIS_Y, py, speed['travel_rpm'], speed['acc'],
                            timeout_ms=xy['timeout_ms']))
        steps.append(S.Step('wait_ms', [xy['settle_ms']]))
        # 3) 下爪 -> 夹紧 -> 抬起
        steps.append(motor(AXIS_Z, grasp_z, speed['rpm'], speed['acc'],
                            timeout_ms=xy['timeout_ms']))
        steps.append(S.Step('wait_ms', [z['settle_ms']]))
        steps.append(S.Step('gripper', ['grab'], timeout_ms=3000))
        steps.append(S.Step('wait_ms', [grip['grab_settle_ms']]))
        steps.append(motor(AXIS_Z, z['lift'], speed['rpm'], speed['acc'],
                            timeout_ms=xy['timeout_ms']))
        steps.append(S.Step('wait_ms', [z['settle_ms']]))
        # 4) 回到固定放料工位
        steps.append(motor(AXIS_X, cfg['drop']['x'], speed['travel_rpm'], speed['acc'],
                            timeout_ms=xy['timeout_ms']))
        steps.append(motor(AXIS_Y, cfg['drop']['y'], speed['travel_rpm'], speed['acc'],
                            timeout_ms=xy['timeout_ms']))
        steps.append(S.Step('wait_ms', [xy['settle_ms']]))
        # 5) 转盘转到该类别盒子（相对最短路径）
        signed = _tray_delta(current_box, box, cfg)
        if signed:
            steps.append(S.Step('note', [f'转盘 {current_box} -> 盒{box}（{abs(signed):.0f} 脉冲）']))
            steps.append(motor(AXIS_TRAY, signed, speed['rpm'], speed['acc'],
                               mode='rel', timeout_ms=xy['timeout_ms']))
            steps.append(S.Step('wait_ms', [tray['settle_ms']]))
            current_box = box
        # 6) 放料
        steps.append(motor(AXIS_Z, z['place'], speed['rpm'], speed['acc'],
                            timeout_ms=xy['timeout_ms']))
        steps.append(S.Step('wait_ms', [z['settle_ms']]))
        steps.append(S.Step('gripper', ['open'], timeout_ms=3000))
        steps.append(S.Step('wait_ms', [grip['open_settle_ms']]))
        steps.append(motor(AXIS_Z, z['lift'], speed['rpm'], speed['acc'],
                            timeout_ms=xy['timeout_ms']))
        steps.append(S.Step('wait_ms', [z['settle_ms']]))
        # 每件放完都把夹爪收回预备位（150°）：先抬到安全高度再收口，
        # 否则 180°→150° 是在盒子里收的，容易蹭到物块或盒壁。
        steps.append(S.Step('wait_ms', [int(grip.get('ready_settle_ms', 500))]))
        steps.append(S.Step('gripper', ['ready'], timeout_ms=3000))

    steps.append(S.Step('note', ['视觉抓取结束（如需回零/换盒请另接步骤）']))
    meta = {'start_box': start_box, 'final_box': current_box,
            'targets': len(ordered), 'warnings': len(warnings)}
    return S.Sequence(name=sequence_name, steps=steps), warnings, meta
