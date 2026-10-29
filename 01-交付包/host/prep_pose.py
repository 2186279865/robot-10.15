"""「一键走出固定路线」：从初始位（P0，障碍区）走到工作位（P1）。

为什么需要它
------------
初始位在上方有障碍，X 轴不能动。必须先让 Y 让开、再把 Z 抬起来，才允许在
抓取区自由移动 X/Y。这一段就是"固定通道"，重复度要求很高，所以做成一条命令，
而不是让人手动点。

默认动作（可改 `runtime/prep_pose.json`）
-----------------------------------------
1. Y 轴走 -1220 脉冲（≈ -25,000 counts ≈ 12.5 mm，让开上方的障碍）
2. Z 轴抬 -150,000 脉冲（≈ 46.9 圈，抬到接近最高，避开托盘上的货）

两步都用闭环 `motor_goto`：固件自己读遥测 counts 判位，ok 回来时已经走过并校验过；
失败会回 E14（reason=timeout/lost/stopped），本模块把失败原样报上去，不做重试。

安全
----
- 只动 Y、Z 两根轴，**不动 X**（初始位 X 一栋都不能动）；
- 上位机模式下若固件处于停止锁存，先发一次 `stop_clear`（只解锁，不产生运动）；
- Z 抬升方向用负数（向上），与 `limits.json` 的口径一致。
"""
from __future__ import annotations
import time

import json
from pathlib import Path
from typing import Dict, List, Optional

PKG_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = PKG_ROOT / 'runtime' / 'prep_pose.json'

PULSES_PER_REV = 3200
AXIS_Z = 1
AXIS_Y = 5

DEFAULTS: Dict[str, object] = {
    'version': 1,
    'y_pulses': -13144,     # 出通道（= -269,194 counts，用户实测的固定路线终点）
    'y_rpm': 300,
    'z_pulses': -152000,    # 抬起（负数=向上；-165762 是全行程顶，这里离顶留约 8 mm）
    'z_rpm': 1500,
    'acc': 0,               # 加速度档：0 = 直接到速度（最快）；数值越大越慢（100 ≈ 爬行）
}

#: 每步顺序固定：先 Y 让开，再抬 Z（反了就撞上方障碍）
STEPS = (('y', AXIS_Y, 'Y 让开障碍'), ('z', AXIS_Z, 'Z 抬到工作高度'))


def default_path() -> Path:
    import os
    env = os.environ.get('ROBOT_PREP_POSE')
    return Path(env) if env else DEFAULT_PATH


def _clean(raw: Dict[str, object]) -> Dict[str, object]:
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key == 'version':
            continue
        if key in raw:
            try:
                out[key] = int(raw[key])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
    out['y_pulses'] = max(-15000, min(15000, int(out['y_pulses'])))       # type: ignore[arg-type]
    out['z_pulses'] = max(-160000, min(160000, int(out['z_pulses'])))     # type: ignore[arg-type]
    out['y_rpm'] = max(1, min(6000, int(out['y_rpm'])))                  # type: ignore[arg-type]
    out['z_rpm'] = max(1, min(6000, int(out['z_rpm'])))                  # type: ignore[arg-type]
    return out


def load(path: Optional[Path] = None) -> Dict[str, object]:
    """读配置；文件缺失/损坏一律回落默认值（不抛）。"""
    p = Path(path) if path else default_path()
    try:
        raw = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            return dict(DEFAULTS)
        from units import check_unit, counts_to_pulses
        if check_unit(raw, name='prep_pose.json') == 'counts':
            for key in ('y_pulses', 'z_pulses'):
                if key in raw:
                    raw[key] = int(round(counts_to_pulses(raw[key])))
            raw['unit'] = 'pulse'
        return _clean(raw)
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(cfg: Dict[str, object], path: Optional[Path] = None) -> Path:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = _clean(cfg)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(p)
    return p


#: 实测转速上限（EMM 驱动器侧限速，rpm 写多大都差不多）：约 50 圈/分。
#: 单步位移硬上限（脉冲）：只用来拦"位置基准错了还闷头走"的撞机，不是行程。
#: 实测 Y 从抓取位回 P0 要 ~5.6 万脉冲，所以 Y 上限按 12 万给（真正的行程由 limits.json 拦）。
MAX_STEP_PULSES = {AXIS_Z: 200000, AXIS_Y: 120000, 4: 30000, 3: 6000}
EFFECTIVE_RPM_CAP = 50


def estimate_timeout_ms(pulses: int, rpm: int, extra_ms: int = 8000) -> int:
    """按"实际速度上限 50 圈/分"估客户端等待上限（rpm 参数再大也不快）。"""
    revs = abs(int(pulses)) / float(PULSES_PER_REV)
    speed = max(1, min(int(rpm), EFFECTIVE_RPM_CAP))
    ms = revs / (speed / 60.0) * 1000.0
    return int(ms * 2.0) + int(extra_ms)


def goto_budget_ms(pulses: int, rpm: int) -> int:
    """固件侧 `goto_max_ms` 该设多大（给驱动器留 1.8 倍 + 5 s）。"""
    revs = abs(int(pulses)) / float(PULSES_PER_REV)
    speed = max(1, min(int(rpm), EFFECTIVE_RPM_CAP))
    ms = revs / (speed / 60.0) * 1000.0
    return int(ms * 1.8) + 5000


COUNTS_PER_PULSE = 65536.0 / PULSES_PER_REV   # 20.48


def counts_of(pulses: int) -> int:
    """脉冲 → 编码器 counts（界面“回读：… 脉冲”那个数字其实是 counts）。"""
    return int(round(int(pulses) * COUNTS_PER_PULSE))


def describe(cfg: Dict[str, object]) -> str:
    """人话描述，同时给脉冲和 counts —— 两个单位都写出来，避免看岔。"""
    y = int(cfg['y_pulses'])
    z = int(cfg['z_pulses'])
    return ('Y %+d 脉冲 (= %s counts, %d RPM)  →  Z %+d 脉冲 (= %s counts, %d RPM)'
            % (y, format(counts_of(y), ','), int(cfg['y_rpm']),
               z, format(counts_of(z), ','), int(cfg['z_rpm'])))


AXIS_LABEL = {1: 'Z', 3: '托盘', 4: 'X', 5: 'Y'}


def move_abs(client, targets, order=None, rpm=50, acc=0, log=None, wait=None,
             soft=None, known=None, counts=None,
             prep: bool = True) -> List[Dict[str, object]]:
    """按 `{轴号: 目标脉冲}` 依次闭环走到目标（增量由回读 counts / 软位置算出）。

    给「回 P1」「Z 抬顶」「Z 降底」「走通道」「退通道」这类固定位置按钮用：
    - 先 `stop_clear`（只解锁，不动）；长行程放宽固件 goto 超时；
    - 有回读 counts（3000 ms 内）优先按 counts 算增量，否则用软位置；
    - 两种位置来源都没有 → `POS_UNKNOWN` 拒绝（绝不按目标值盲走）；
    - 单步超过该轴行程 1.5 倍 → `RANGE` 拒绝；
    - 已在目标位（|Δ| ≤ 2 脉冲）→ 跳过（重复点按钮安全）。
    """
    say = log or (lambda _t: None)
    waiter = wait or (lambda rid, ms: client.wait_reply(rid, timeout_ms=ms))
    results: List[Dict[str, object]] = []

    if prep:                       # 一次动作序列里只需准备一次，省 3 次往返（约 0.15-0.25 s）
        try:
            client.send('stop_clear', timeout_ms=1500)
        except Exception:                             # noqa: BLE001
            pass
        try:
            client.send('param_set', 'goto_max_ms', 120000, timeout_ms=1500)
        except Exception:                             # noqa: BLE001
            pass
        try:
            client.send('param_set', 'acc', max(0, min(255, int(acc))), timeout_ms=1500)
        except Exception:                             # noqa: BLE001
            pass

    def rpm_for(a: int) -> int:
        if isinstance(rpm, dict):
            return int(rpm.get(int(a), 50))
        return int(rpm)

    axes = [int(a) for a in (order or list(targets.keys()))]
    for axis in axes:
        if axis not in targets:
            continue
        target = int(targets[axis])
        rpm_i = rpm_for(axis)
        label = '%s 目标 %+d 脉冲' % (AXIS_LABEL.get(axis, axis), target)
        pulses = target
        cur_counts = None if counts is None else counts.get(axis)
        if cur_counts is not None:
            want = int(round(target * COUNTS_PER_PULSE))
            pulses = int(round((want - int(cur_counts)) / COUNTS_PER_PULSE))
            if abs(pulses) <= 2:
                say('%s：已经在目标位（回读 %d counts），跳过' % (label, int(cur_counts)))
                continue
            say('%s：回读 %d counts → 走 %+d 脉冲（%d RPM）'
                % (label, int(cur_counts), pulses, rpm_i))
        elif soft is not None:
            if not bool((known or {}).get(axis, False)):
                item = {'axis': axis, 'pulses': 0, 'rpm': rpm_i, 'ok': False,
                        'code': 'POS_UNKNOWN', 'text': '当前位置未知', 'pairs': {}}
                results.append(item)
                say('%s：位置未知，拒绝执行（先复位或在四轴页对 Y/Z 点「位置清零」）' % label)
                return results
            pulses = target - int((soft or {}).get(axis, 0))
            if abs(pulses) <= 2:
                say('%s：已经在目标位（%+d 脉冲），跳过' % (label, int((soft or {}).get(axis, 0))))
                continue
            say('%s：现在 %+d → 走 %+d 脉冲（%d RPM）'
                % (label, int((soft or {}).get(axis, 0)), pulses, rpm_i))
        elif pulses == 0:
            say('%s：脉冲 0，跳过' % label)
            continue
        else:
            item = {'axis': axis, 'pulses': 0, 'rpm': rpm_i, 'ok': False,
                    'code': 'POS_UNKNOWN', 'text': '没有位置来源，拒绝盲走', 'pairs': {}}
            results.append(item)
            say('%s：没有位置来源，拒绝执行' % label)
            return results

        max_step = MAX_STEP_PULSES.get(axis, 20000)
        if abs(pulses) > max_step:
            item = {'axis': axis, 'pulses': pulses, 'rpm': rpm_i, 'ok': False,
                    'code': 'RANGE', 'text': '单步超行程 1.5 倍', 'pairs': {}}
            results.append(item)
            say('%s：单步 %+d 脉冲超过 %d，拒绝执行' % (label, pulses, max_step))
            return results

        item = {'axis': axis, 'pulses': pulses, 'rpm': rpm_i, 'ok': False,
                'code': '', 'text': '', 'pairs': {}}
        try:
            req = client.goto(axis, pulses, rpm=rpm_i)
            rep = waiter(req, estimate_timeout_ms(pulses, rpm_i))
        except Exception as exc:                      # noqa: BLE001
            item['code'] = 'EXC'
            item['text'] = '%s: %s' % (type(exc).__name__, exc)
            results.append(item)
            say('%s：异常 %s' % (label, item['text']))
            return results
        if rep is None:
            item['code'] = 'TIMEOUT'
            item['text'] = '客户端等待超时'
            results.append(item)
            say('%s：%s' % (label, item['text']))
            return results
        item.update({k: rep.get(k, '') for k in ('code', 'text')})
        item['pairs'] = rep.get('pairs') or {}
        item['ok'] = (rep.get('kind') == 'reply') and not item['code']
        results.append(item)
        pairs = item['pairs'] or {}
        detail = ' '.join('%s=%s' % (k, pairs[k])
                          for k in ('err', 'passes', 'elapsed_ms', 'tx', 'reason')
                          if k in pairs)
        say('%s：%s %s' % (label, 'ok' if item['ok'] else ('失败 %s' % (item['code'] or '?')),
                          detail))
        if not item['ok']:
            return results
    if not results:
        say('全部轴都已在目标位，不需要动')
    return results



def move_two(client, targets, rpm=50, acc=0, log=None, wait=None,
             soft=None, known=None, counts=None) -> List[Dict[str, object]]:
    """两根轴**同时**发 motor_goto 再一起等回执（用于 X/Y 这类互不干涉的轴）。

    只支持「有回读 counts 或软位置」的情况；任一轴位置未知 → 返回 POS_UNKNOWN。
    """
    say = log or (lambda _t: None)
    waiter = wait or (lambda rid, ms: client.wait_reply(rid, timeout_ms=ms))

    def rpm_for(a):
        return int(rpm.get(int(a), 50)) if isinstance(rpm, dict) else int(rpm)

    plans = []
    for axis, target in targets.items():
        axis = int(axis)
        target = int(target)
        cur = None if counts is None else counts.get(axis)
        rpm_i = rpm_for(axis)
        if cur is not None:
            want = int(round(target * COUNTS_PER_PULSE))
            pulses = int(round((want - int(cur)) / COUNTS_PER_PULSE))
        elif soft is not None and bool((known or {}).get(axis, False)):
            pulses = target - int((soft or {}).get(axis, 0))
        else:
            return [{'axis': axis, 'pulses': 0, 'rpm': rpm_i, 'ok': False,
                     'code': 'POS_UNKNOWN', 'text': '没有位置来源，拒绝盲走', 'pairs': {}}]
        if abs(pulses) > MAX_STEP_PULSES.get(axis, 20000):
            return [{'axis': axis, 'pulses': pulses, 'rpm': rpm_i, 'ok': False,
                     'code': 'RANGE', 'text': '单步超行程 1.5 倍', 'pairs': {}}]
        if abs(pulses) <= 2:
            continue
        plans.append((axis, pulses, rpm_i))

    if not plans:
        return []
    for axis, pulses, rpm_i in plans:
        say('%s 并行 目标 %+d 脉冲（%d RPM）' % (AXIS_LABEL.get(axis, axis), pulses, rpm_i))

    reqs = []
    for axis, pulses, rpm_i in plans:
        reqs.append((axis, client.goto(axis, pulses, rpm=rpm_i), pulses, rpm_i))
    results = []
    for axis, rid, pulses, rpm_i in reqs:
        item = {'axis': axis, 'pulses': pulses, 'rpm': rpm_i, 'ok': False,
                'code': '', 'text': '', 'pairs': {}}
        rep = waiter(rid, estimate_timeout_ms(pulses, rpm_i))
        if rep is None:
            item['code'] = 'TIMEOUT'
        else:
            item.update({k: rep.get(k, '') for k in ('code', 'text')})
            item['pairs'] = rep.get('pairs') or {}
            item['ok'] = (rep.get('kind') == 'reply') and not item['code']
        results.append(item)
        say('%s 并行：%s' % (AXIS_LABEL.get(axis, axis), 'ok' if item['ok'] else ('失败 %s' % item['code'])))
    return results


def run(client, cfg: Optional[Dict[str, object]] = None, log=None, wait=None,
        soft=None, known=None, counts=None) -> List[Dict[str, object]]:
    """依次执行两步闭环移动。返回每步的结果 dict；某步失败即停。

    每步结果：{key, axis, pulses, rpm, ok, code, text, pairs}

    `wait(req_id, timeout_ms)` 可替换默认的阻塞等待：GUI 里传一个"轮询
    `client.done_of(req_id)`"的版本，避免和界面的服务线程抢着读串口。

    `soft`/`known` 是界面记的软位置（脉冲，相对初始位）。给了就按
    "目标位置 - 当前位置" 算增量：已经在目标位就什么都不做（重复点按钮安全），
    位置未知就报 POS_UNKNOWN 让人先复位/清零，绝不闷头乱走。
    """
    conf = cfg if cfg is not None else load()
    say = log or (lambda _t: None)
    waiter = wait or (lambda rid, ms: client.wait_reply(rid, timeout_ms=ms))
    results: List[Dict[str, object]] = []

    # 断开重连后固件的停止锁存可能还在（界面上的 latched 标志也会过期），
    # 所以无条件先清一次：stop_clear 只解锁，不产生任何运动。
    try:
        client.send('stop_clear', timeout_ms=1500)
    except Exception as exc:                          # noqa: BLE001
        say('stop_clear 失败：%s' % (exc,))
    # 长行程要放宽固件的 goto 超时（默认按 rpm 估，而实际速度被限在 ~50 圈/分）
    try:
        client.send('param_set', 'goto_max_ms', 120000, timeout_ms=1500)
    except Exception:                                 # noqa: BLE001
        pass
    # 加速度档：0 = 直接到速度（最快）。默认档位（1）下 Z 只有 ~32 圈/分，抬一次要十几秒。
    try:
        client.send('param_set', 'acc', max(0, min(255, int(conf.get('acc', 0)))), timeout_ms=1500)
    except Exception:                                 # noqa: BLE001
        pass

    for key, axis, title in STEPS:
        target = int(conf[key + '_pulses'])          # type: ignore[index]
        rpm = int(conf[key + '_rpm'])                # type: ignore[index]
        pulses = target
        cur_counts = None if counts is None else counts.get(axis)
        if cur_counts is not None:
            want = int(round(target * COUNTS_PER_PULSE))
            pulses = int(round((want - int(cur_counts)) / COUNTS_PER_PULSE))
            if abs(pulses) <= 2:
                say('%s：已经在目标位（回读 %d counts ≈ %d 脉冲），跳过'
                    % (title, int(cur_counts), target))
                continue
            say('%s：目标 %+d 脉冲(%s counts)、回读 %d counts → 走 %+d 脉冲（%d RPM）'
                % (title, target, format(want, ','), int(cur_counts), pulses, rpm))
        elif soft is not None:
            cur_ok = bool((known or {}).get(axis, False))
            if not cur_ok:
                item = {'key': key, 'axis': axis, 'pulses': 0, 'rpm': rpm, 'ok': False,
                        'code': 'POS_UNKNOWN', 'text': '当前位置未知：先手动回到初始位再点',
                        'pairs': {}}
                results.append(item)
                say('%s：位置未知，拒绝执行（先复位到初始位，或在四轴页对 Y/Z 点「位置清零」）' % title)
                return results
            cur = int((soft or {}).get(axis, 0))
            pulses = target - cur
            if pulses == 0:
                say('%s：已经在目标位（%+d 脉冲），跳过' % (title, cur))
                continue
            say('%s：目标 %+d、现在 %+d → 走 %+d 脉冲（%d RPM）'
                % (title, target, cur, pulses, rpm))
        elif pulses == 0:
            say('%s：跳冲（pulses=0）' % title)
            continue
        else:
            # 既没有回读 counts，也没有软位置 —— 绝不能按"目标值"整段走：
            # 那会在"其实已经走过通道"的机器上再走一遍（2026-10-27 撞机就是这么来的）。
            item = {'key': key, 'axis': axis, 'pulses': 0, 'rpm': rpm, 'ok': False,
                    'code': 'POS_UNKNOWN',
                    'text': '没有位置来源：既不传 counts 也没有软位置，拒绝盲走',
                    'pairs': {}}
            results.append(item)
            say('%s：没有位置来源（counts / 软位置都没给），拒绝执行以免重走整段' % title)
            return results
        # 硬护栏：单步位移不能超过该轴全程行程的 1.5 倍（Z 全程 ~152k 脉冲，Y/X 只有 ~13k）
        max_step = MAX_STEP_PULSES.get(axis, 20000)
        if abs(pulses) > max_step:
            item = {'key': key, 'axis': axis, 'pulses': pulses, 'rpm': rpm, 'ok': False,
                    'code': 'RANGE',
                    'text': '单步 %d 脉冲超过 %d（疑似位置错），拒绝执行' % (pulses, max_step),
                    'pairs': {}}
            results.append(item)
            say('%s：单步 %+d 脉冲超过 %d，拒绝执行（先检查位置）' % (title, pulses, max_step))
            return results
        item: Dict[str, object] = {'key': key, 'axis': axis, 'pulses': pulses, 'rpm': rpm,
                                   'ok': False, 'code': '', 'text': '', 'pairs': {}}
        try:
            req = client.goto(axis, pulses, rpm=rpm)
            rep = waiter(req, estimate_timeout_ms(pulses, rpm))
        except Exception as exc:                      # noqa: BLE001
            item['code'] = 'EXC'
            item['text'] = '%s: %s' % (type(exc).__name__, exc)
            results.append(item)
            say('%s：异常 %s' % (title, item['text']))
            return results
        if rep is None:
            item['code'] = 'TIMEOUT'
            item['text'] = '客户端等待超时（固件可能仍在走，先看状态再决定）'
            results.append(item)
            say('%s：%s' % (title, item['text']))
            return results
        item.update({k: rep.get(k, '') for k in ('code', 'text')})
        item['pairs'] = rep.get('pairs') or {}
        item['ok'] = (rep.get('kind') == 'reply') and not item['code']
        results.append(item)
        pairs = item['pairs'] or {}
        detail = ' '.join('%s=%s' % (k, pairs[k])
                          for k in ('err', 'passes', 'elapsed_ms', 'tx', 'reason')
                          if k in pairs)
        say('%s：%s %s' % (title, 'ok' if item['ok'] else ('失败 %s' % (item['code'] or '?')),
                          detail))
        if not item['ok']:
            return results
    return results

def move_xy_fast(client, targets, rpm=1600, log=None, counts=None, counts_fn=None,
                 tol_pulses=8, timeout_s=3.5, acc=0, wait=None):
    """X/Y **一步并行**走位：两轴各发一条 motor_pos（固件独立执行，同时动）。

    收口策略（关键）：GUI 的 telemetry 约 200 ms 一次，运动中按 counts 轮询会被旧值误导，
    因此这里不靠"运动中实时判到位"，而是：
      1) 用行程/转速估算运动时间 t_est；
      2) 等 t_est*0.9 后，用"连续两次读数几乎不变"判断停稳（最多再等 2 s）；
      3) 用停稳后的读数算残差，只有 > tol_pulses 的轴才顺序 goto 补一次。
    """
    say = log or (lambda _t: None)
    rpm_of = (lambda a: int(rpm.get(int(a), 1600))) if isinstance(rpm, dict) else (lambda a: int(rpm))
    targets = {int(k): int(v) for k, v in targets.items() if v is not None}

    def _pairs(rep):
        """从回执里取 pairs（兼容 dict / {'pairs': dict}）。"""
        if not rep:
            return {}
        if isinstance(rep, dict):
            p = rep.get('pairs')
            return p if isinstance(p, dict) else rep
        return {}

    def _snd(name, *args, timeout_ms=1500):
        """发命令并等回执：优先用传入的 wait()，否则用 client.wait_reply（都没有就只发不等）。"""
        rid = client.send(name, *args, timeout_ms=timeout_ms)
        if wait is not None:
            try:
                return wait(rid, timeout_ms)
            except Exception:                       # noqa: BLE001
                return None
        wr = getattr(client, 'wait_reply', None)
        if wr is None:
            return None
        try:
            return wr(rid, timeout_ms=timeout_ms)
        except Exception:                           # noqa: BLE001
            return None

    def _goto_wait(axis, tgt, rpm_i):
        """闭环补走并**等它跑完**。client.goto() 只发不等，必须自己等回执。"""
        try:
            rid = client.goto(axis, tgt, rpm=rpm_i)
        except Exception as exc:                    # noqa: BLE001
            say('%s 补走下发异常：%s' % (AXIS_LABEL.get(axis, axis), exc))
            return False
        wf = wait if wait is not None else getattr(client, 'wait_reply', None)
        if wf is None:
            say('%s 补走已下发（无回执可等）' % AXIS_LABEL.get(axis, axis))
            return False
        spd = min(max(1, int(rpm_i)), 274)
        tmo = int(abs(int(tgt)) / 3200.0 / spd * 60000 * 3) + 8000
        try:
            rep = wf(rid, timeout_ms=tmo)
        except Exception as exc:                    # noqa: BLE001
            say('%s 补走等待异常：%s' % (AXIS_LABEL.get(axis, axis), exc))
            return False
        pr = _pairs(rep) or {}
        code = str(rep.get('code') or '') if isinstance(rep, dict) else ''
        if not pr and not code:
            say('%s 补走：等回执超时（%d ms）' % (AXIS_LABEL.get(axis, axis), tmo))
            return False
        if code or str(pr.get('reason') or ''):
            say('%s 补走失败：code=%s reason=%s（不再继续下一步）'
                % (AXIS_LABEL.get(axis, axis), code or '-', pr.get('reason') or '-'))
            return False
        detail = ' '.join('%s=%s' % (k, pr[k]) for k in ('err', 'passes', 'elapsed_ms', 'reason') if k in pr)
        say('%s 补走完成：%s' % (AXIS_LABEL.get(axis, axis), detail))
        return True

    def read_counts():
        if counts_fn is None:
            return {}
        try:
            c = counts_fn() or {}
        except Exception:                               # noqa: BLE001
            return {}
        out = {}
        for k, v in c.items():
            try:
                out[int(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    def fresh_counts(min_gap=0.15, max_tries=10, tol=30.0):
        """连续两次读数一致才认为新鲜；避免拿上一条运动的旧 counts 去算 delta（会多走一整段）。"""
        prev = read_counts()
        for _i in range(max_tries):
            time.sleep(min_gap)
            now = read_counts()
            if prev and now and all(abs(now.get(a, 1e9) - prev.get(a, 0.0)) <= tol for a in targets):
                return now
            prev = now or prev
        return prev or {}

    cur = fresh_counts()
    if not all(a in cur for a in targets):
        say('X/Y 并行：没有编码器读数，退回顺序走位')
        _c = dict(counts) if counts else None
        return move_abs(client, targets, order=sorted(targets), rpm=rpm, acc=acc,
                        log=log, wait=wait, counts=_c, prep=True)

    # 1) 放开 speed_rpm_max
    old_max = None
    try:
        pr = _pairs(_snd('param_get', 'speed_rpm_max', timeout_ms=800)) or {}
        if pr.get('value') not in (None, ''):
            old_max = int(float(pr['value']))
        need = max([rpm_of(a) for a in targets] + [int(old_max or 300)])
        if old_max is None or old_max < need:
            _snd('param_set', 'speed_rpm_max', max(need, 3200), timeout_ms=1200)
    except Exception:                                   # noqa: BLE001
        pass

    plans, bad, t_est = [], [], 0.0
    try:
        # 2) 两轴同时下发
        for axis, tgt in targets.items():
            now_p = int(round(cur[axis] / COUNTS_PER_PULSE))
            delta = tgt - now_p
            if delta == 0:
                continue
            r = _snd('motor_pos', axis, 1 if delta < 0 else 0, rpm_of(axis), int(acc),
                     abs(delta), 'rel', timeout_ms=1500)
            pr = _pairs(r) or {}
            reason = str(pr.get('reason') or '')
            if reason:
                say('%s 并行：被拒绝 reason=%s' % (AXIS_LABEL.get(axis, axis), reason))
                bad.append(axis)
                continue
            eff_rpm = min(float(rpm_of(axis)), 1240.0)   # 驱动器实际封顶≈1240 rpm，按指令值估会等不够
            sec = abs(delta) / max(1.0, eff_rpm * 3200.0 / 60.0)
            t_est = max(t_est, sec)
            plans.append((axis, tgt, delta))
            say('%s 并行：%+d 脉冲（%d RPM）' % (AXIS_LABEL.get(axis, axis), delta, rpm_of(axis)))

        # 3) 等停稳：等满估算时长（含减速余量），再要求 **连续 3 次** 读数几乎不变
        if plans:
            t_wait = min(9.0, t_est * 1.35 + 0.50)
            t_end = time.monotonic() + t_wait
            while time.monotonic() < t_end:
                time.sleep(0.05)
            hard = time.monotonic() + 3.0
            prev = read_counts()
            stable = 0
            while time.monotonic() < hard:
                time.sleep(0.15)
                now = read_counts()
                if now and prev and all(
                        abs(now.get(a, 1e9) - prev.get(a, 0.0)) <= 24 for a, _t, _d in plans):
                    stable += 1
                    if stable >= 4:
                        prev = now
                        break
                else:
                    stable = 0
                prev = now or prev
            say('X/Y 并行：等 %.2f s 后判停稳' % t_wait)

        # 4) 残差 > tol 的轴顺序补一次
        fin = read_counts()
        for axis, tgt, delta in plans:
            c = fin.get(axis)
            if c is None:
                continue
            now_p = int(round(c / COUNTS_PER_PULSE))
            miss = tgt - now_p
            # 读数滞后保护：只有"残差≈整段位移"（0.6~1.15 倍）才判遥测没刷新；
            # 残差远大于位移 = 真的没到位，必须补走。
            d = abs(int(delta))
            if d >= 200 and (0.60 * d) <= abs(miss) <= (1.15 * d):
                say('%s 并行：读数疑似滞后（残差 %+d ≈ 位移 %+d），跳过补走'
                    % (AXIS_LABEL.get(axis, axis), miss, delta))
                continue
            if abs(miss) > max(60, int(tol_pulses)):
                # 补走前再确认一次：读数在 250 ms 内还在变就再等，避免"运动未停就补走"叠加位移
                _prev = fin.get(axis)
                for _k in range(4):
                    time.sleep(0.12)
                    _now = read_counts().get(axis)
                    if _prev is None or _now is None or abs(_now - _prev) <= 24:
                        break
                    _prev = _now
                if abs(miss) > 3000:
                    raise RuntimeError('%s 并行后偏离目标 %+d 脉冲（>3000），不做补走，请人工检查'
                                       % (AXIS_LABEL.get(axis, axis), miss))
                say('%s 并行残差 %+d 脉冲，顺序补一次（相对量，等回执）' % (AXIS_LABEL.get(axis, axis), miss))
                if not _goto_wait(axis, miss, rpm_of(axis)):
                    raise RuntimeError('%s 补走未完成（锁存/超时），中止本件' % AXIS_LABEL.get(axis, axis))
        for axis in bad:
            rel = targets[axis] - int(round((cur.get(axis) or 0) / COUNTS_PER_PULSE))
            if not _goto_wait(axis, rel, rpm_of(axis)):
                raise RuntimeError('%s 并行被拒后补走失败' % AXIS_LABEL.get(axis, axis))

        return [{'axis': a, 'pulses': targets[a], 'ok': True, 'code': '', 'text': '', 'pairs': {}}
                for a in sorted(targets)]
    finally:
        if old_max is not None:
            try:
                _snd('param_set', 'speed_rpm_max', int(old_max), timeout_ms=800)
            except Exception:                           # noqa: BLE001
                pass


