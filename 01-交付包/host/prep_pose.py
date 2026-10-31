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

import protocol as P

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


#: 实测转速上限：drivers 侧封顶约 2740 圈/分（protocol.GOTO_RPM_BY_AXIS 注释同源）。
#: 以前这里写 50（早期"rpm 写多大都差不多"的错误结论），导致 estimate_timeout_ms
#: 把 Z 满行程估成 122 s：现场 02:39:37 Z 抬顶早就回执了，上位机却一直等到 02:41:37。
#: 真实值：2400 圈/分满行程 152000 脉冲实测 1.49 s（含加减速）→ 有效约 1910 圈/分。
#: 单步位移硬上限（脉冲）：只用来拦"位置基准错了还闷头走"的撞机，不是行程。
#: 实测 Y 从抓取位回 P0 要 ~5.6 万脉冲，所以 Y 上限按 12 万给（真正的行程由 limits.json 拦）。
MAX_STEP_PULSES = {AXIS_Z: 200000, AXIS_Y: 120000, 4: 30000, 3: 6000}
EFFECTIVE_RPM_CAP = 2740

#: X/Y **并行**走位后的残差保护阈值（脉冲）。
#: 2026-10-31 现场反馈：并行后报「偏离目标 +11394 脉冲（>3000）」时爪子其实已经到位，
#: 残差来自 telemetry 旧帧 —— Y 1600 RPM ≈ 8.5 万脉冲/s，晚 130 ms 就差 1.1 万脉冲。
#: 因此 3000 不再是「中止线」，而是「不补走、只告警、继续本件」的线：
#:   * ≤ 60         ：正常，不动；
#:   * 60 ~ 3000    ：顺序补一次（相对量，等回执）；
#:   * 3000 ~ 40000 ：**只告警、不补走**，继续本件（现场已确认）；
#:   * > 40000      ：≈400 mm，任何回读滞后都解释不了 → 中止等人工，防真撞机。
XY_MISS_TOL_PULSES = 60
XY_MISS_SKIP_PULSES = 3000
XY_MISS_ABORT_PULSES = 40000

#: 指令「被接纳」的回执超时（毫秒）。原来一律 1500 ms，但现场链路一拥塞（图传占满无线），
#: 回执迟到 1~3 s 是常态 —— 超时会让我们误判「结果未知/没被接纳」，进而重发或中止本件。
#: 真正的到位判定不靠这个超时（走 event/goto 闭环），所以放大没有副作用。
ACK_MS = 3000
#: 只回一个值的参数查询（param_get）超时。
PARAM_GET_MS = 1500
#: 参数写入（param_set）超时。
PARAM_SET_MS = 2000

#: 各轴**实测**有效转速（圈/分）。指令值不等于实跑值，超时与收口都按实测估：
#:   Z：指令 2400/3000 都只跑出 ≈1800~1900（93 趟实测 avg 1585 ms / 152009 脉冲）；
#:   Y：指令 2000 实测 ≈690（8 万脉冲约 2 s）；X：300 就是 300。
EFFECTIVE_RPM_BY_AXIS = {AXIS_Z: 1900, AXIS_Y: 690, 4: 300, 3: 300}

#: 遥测 counts 的最大可用龄期（毫秒）。超过它就不再拿这份读数当"现在的位置"。
#: 现场 2026-10-31 16:04:54：Y 的 counts 冻在 -803218（托盘位）三秒多，
#: ⑦ 已经发过 +26090 回 P1、预走又按这份旧值发 +26076 —— 同一段位移发了两遍，
#: 把 Y 顶到轨道尽头（E14 + 客户端等待超时 + E09 goto_busy，整件白失败）。
COUNTS_MAX_AGE_MS = 800.0

#: 并行走位/残差收口的轮询参数。
MOTION_POLL_S = 0.03          # counts 轮询间隔
ARRIVE_NO_PROGRESS_S = 0.25   # 这么久 counts 完全没变化 → 认为指令没生效，提前收口


def estimate_timeout_ms(pulses: int, rpm: int, extra_ms: int = 8000, axis=None) -> int:
    """按真实有效转速估客户端等待上限（单程时间 × 2 倍余量 + extra）。

    历史上这里硬压 `min(rpm, 50)`（"实际最多 50 圈/分"是早期误判），于是
    Z 满行程 152000 脉冲被估成 122 s —— 回执其实早就到了，后台线程还在等超时
    （现场 02:39:37 已回 `#265,ok`，02:41:37 才报超时，就是这条）。
    真实数据：Z 2400 圈/分走 152000 脉冲实测 1.49 s，有效约 1910 圈/分。
    """
    revs = abs(int(pulses)) / float(PULSES_PER_REV)
    speed = max(1, min(int(rpm), EFFECTIVE_RPM_CAP))
    if axis is not None:
        # 指令转速 ≠ 实跑转速（Y 给 2000 实际只跑 ≈690）。超时按实测估，
        # 才不会出现「固件还在正常闭环，客户端却先判超时」。
        eff = EFFECTIVE_RPM_BY_AXIS.get(int(axis))
        if eff:
            speed = max(1, min(speed, int(eff)))
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

#: 「按遥测判到位」的容差下限（counts）。每轴再和该轴闭环容差 `GOTO_TOL_BY_AXIS` 取较大者 ×2：
#: Z 1500 counts（≈0.044 mm）→ 3000 counts ≈0.09 mm；Y 350 → 800 counts ≈0.4 mm。
ARRIVE_COUNTS_MIN = 800.0
#: 判到位需要连续这么多次遥测都对得上（间隔 `ARRIVE_GAP_S`），防止“正好路过”被当成到位。
ARRIVE_NEED = 2
ARRIVE_GAP_S = 0.08


def arrive_tol_counts(axis: int) -> float:
    """该轴「遥测判到位」的容差（counts）。"""
    try:
        base = float(P.GOTO_TOL_BY_AXIS.get(int(axis), 350))
    except Exception:                                 # noqa: BLE001
        base = 350.0
    return max(ARRIVE_COUNTS_MIN, base * 2.0)


def tele_counts(client, axis: int):
    """从客户端最新遥测里取某轴 counts；没有就 None（离线/未订阅时返回 None）。"""
    last = getattr(client, 'last_counts', None)
    if not isinstance(last, dict):
        return None
    ent = last.get(int(axis))
    if isinstance(ent, dict):
        try:
            v = float(ent.get('counts'))
        except (TypeError, ValueError):
            return None
        return None if v != v else v                      # 过滤 NaN
    try:
        return float(ent)
    except (TypeError, ValueError):
        return None


def await_goto(client, req, axis: int, want_counts: int, timeout_ms: int,
               waiter, log=None, poll_ms: int = 60) -> tuple:
    """等一条 `motor_goto` 结算：**回执优先，回执迟到时按遥测 counts 判到位**。

    为什么需要：现场（2026-10-31 04:38~04:55）固件 1.0~1.6 s 就走完、并把 counts 停到
    目标点，但回执要 2~15 s 才回来（图传把派端/无线挤满，串口桥的应答积压）。
    旧代码死等回执 → 表现为「Z 降到底后停顿很久才开始夹紧/抬升」。
    这里改成：一边等回执，一边看遥测 counts —— 连续两次都在容差内就认定到位、
    先继续动作；迟到的回执照常结算（调用方已经用 `extend_deadline` 把它的截止时间推后）。

    返回 `(rep, how)`：`how='reply'` 正常回执；`how='counts'` 遥测判到位；`how='timeout'` 超时。
    """
    say = log or (lambda _t: None)
    tol = arrive_tol_counts(axis)
    t0 = time.monotonic()
    deadline = t0 + max(1.0, float(timeout_ms) / 1000.0)
    first_hit_at = None
    hits = 0
    while True:
        rep = waiter(req, poll_ms)
        if rep is not None:
            return rep, 'reply'
        now = time.monotonic()
        cur = tele_counts(client, axis)
        if cur is not None and abs(cur - float(want_counts)) <= tol:
            if first_hit_at is None:
                first_hit_at = now
            hits += 1
            if hits >= ARRIVE_NEED and (now - first_hit_at) >= ARRIVE_GAP_S:
                return {}, 'counts'                 # 连续若干次都对得上 → 到位
        else:
            first_hit_at = None
            hits = 0
        if now >= deadline:
            return None, 'timeout'
        time.sleep(0.02)                        # waiter 立刻返回时别空转烧 CPU



def move_abs(client, targets, order=None, rpm=50, acc=0, log=None, wait=None,
             soft=None, known=None, counts=None,
             prep: bool = True, skip_tol: float = 2.0) -> List[Dict[str, object]]:
    """按 `{轴号: 目标脉冲}` 依次闭环走到目标（增量由回读 counts / 软位置算出）。

    给「回 P1」「Z 抬顶」「Z 降底」「走通道」「退通道」这类固定位置按钮用：
    - 先 `stop_clear`（只解锁，不动）；长行程放宽固件 goto 超时；
    - 有回读 counts（3000 ms 内）优先按 counts 算增量，否则用软位置；
    - 两种位置来源都没有 → `POS_UNKNOWN` 拒绝（绝不按目标值盲走）；
    - 单步超过该轴行程 1.5 倍 → `RANGE` 拒绝；
    - 已在目标位（|Δ| ≤ skip_tol 脉冲，默认 2）→ 跳过（重复点按钮安全）。
    """
    say = log or (lambda _t: None)
    waiter = wait or (lambda rid, ms: client.wait_reply(rid, timeout_ms=ms))
    results: List[Dict[str, object]] = []

    if prep:                       # 一次动作序列里只需准备一次，省 3 次往返（约 0.15-0.25 s）
        try:
            client.send('stop_clear', timeout_ms=ACK_MS)
        except Exception:                             # noqa: BLE001
            pass
        try:
            client.send('param_set', 'goto_max_ms', 120000, timeout_ms=ACK_MS)
        except Exception:                             # noqa: BLE001
            pass
        try:
            client.send('param_set', 'acc', max(0, min(255, int(acc))), timeout_ms=ACK_MS)
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
            if abs(pulses) <= skip_tol:
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
            if abs(pulses) <= skip_tol:
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
            req = client.goto(axis, pulses, rpm=rpm_i,
                              tol=P.GOTO_TOL_BY_AXIS.get(int(axis)))
            rep, how = await_goto(client, req, axis, counts_of(target),
                                  estimate_timeout_ms(pulses, rpm_i), waiter, say)
        except Exception as exc:                      # noqa: BLE001
            item['code'] = 'EXC'
            item['text'] = '%s: %s' % (type(exc).__name__, exc)
            results.append(item)
            say('%s：异常 %s' % (label, item['text']))
            return results
        if how == 'counts':
            # 回执还在路上：把它的截止时间推后，迟到的回执照常结算（不再刷「结果未知」）。
            try:
                client.extend_deadline(req, extra_ms=30000)
            except Exception:                         # noqa: BLE001
                pass
            item['arrived_by'] = 'counts'
            say('%s：回执还没到，但遥测 counts 已到位（%d），先按到位继续'
                % (label, counts_of(target)))
            rep = {'kind': 'reply', 'code': '', 'text': '', 'pairs': {'arrived_by': 'counts'}}
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
        reqs.append((axis, client.goto(axis, pulses, rpm=rpm_i,
                                       tol=P.GOTO_TOL_BY_AXIS.get(int(axis))),
                     pulses, rpm_i))
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
        client.send('stop_clear', timeout_ms=ACK_MS)
    except Exception as exc:                          # noqa: BLE001
        say('stop_clear 失败：%s' % (exc,))
    # 长行程要放宽固件的 goto 超时（默认按 rpm 估，而实际速度被限在 ~50 圈/分）
    try:
        client.send('param_set', 'goto_max_ms', 120000, timeout_ms=ACK_MS)
    except Exception:                                 # noqa: BLE001
        pass
    # 加速度档：0 = 直接到速度（最快）。默认档位（1）下 Z 只有 ~32 圈/分，抬一次要十几秒。
    try:
        client.send('param_set', 'acc', max(0, min(255, int(conf.get('acc', 0)))), timeout_ms=ACK_MS)
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
            req = client.goto(axis, pulses, rpm=rpm,
                              tol=P.GOTO_TOL_BY_AXIS.get(int(axis)))
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
                 tol_pulses=8, timeout_s=3.5, acc=0, wait=None,
                 speed_setup: bool = True, counts_age_fn=None):
    """X/Y **一步并行**走位：两轴各发一条 motor_pos（固件独立执行，同时动）。

    收口策略（关键）：GUI 的 telemetry 约 100~200 ms 一次，运动中按 counts 轮询会被旧值误导，
    因此这里不靠"运动中实时判到位"，而是：
      1) 用行程/转速估算运动时间 t_est（驱动器实际封顶≈1240 rpm）；
      2) 等 t_est*1.02+0.1 s 后用"连续两次读数几乎不变（间隔 50 ms）"判断停稳（最多再等 2 s）；
      3) 用停稳后的读数算残差，只有 > tol_pulses 的轴才顺序 goto 补一次。
    speed_setup=False：跳过 param_get/param_set speed_rpm_max（一批只做一次，省 2 次往返）。
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

    def _snd(name, *args, timeout_ms=ACK_MS):
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

    def fresh_axis(axis, min_gap=0.12, max_tries=8, tol=24.0):
        """单轴新鲜读数：连续两次（间隔 min_gap）差 ≤ tol 才认。

        用于"残差要不要补走"之前的复核：telemetry 旧帧会让残差虚高上万脉冲，
        拿新鲜读数重算一次，避免把滞后当成真的没到位（补走会多走一整段）。
        取不到就返回 None，调用方按原值处理。
        """
        prev = read_counts().get(axis)
        for _i in range(max_tries):
            time.sleep(min_gap)
            now = read_counts().get(axis)
            if prev is not None and now is not None and abs(now - prev) <= tol:
                return now
            prev = now if now is not None else prev
        return prev

    # 调用方（auto_grab.move_xy）已做过"新鲜+稳定"校验，这里直接取一次即可，
    # 避免每步再多等 0.3~1.5 s。
    cur = read_counts()
    if not all(a in cur for a in targets):
        say('X/Y 并行：没有编码器读数，退回顺序走位')
        _c = dict(counts) if counts else None
        return move_abs(client, targets, order=sorted(targets), rpm=rpm, acc=acc,
                        log=log, wait=wait, counts=_c, prep=True)

    # 1) 放开 speed_rpm_max（一批只做一次；固件参数是持久的，没必要每步都问一遍）
    #    **不再在函数末尾还原**：2026-10-31 现场 —— 首件把它从 300 抬到 3200，
    #    finally 立刻又改回 300，之后所有 Y@2000 全部被固件拒成 reason=speed_limit，
    #    退化成「X 先走、Y 补走」，每件还多花 0.5~1 s（02:37 那批 12 次全被拒）。
    need_rpm = max([rpm_of(a) for a in targets] + [1])
    if speed_setup:
        try:
            pr = _pairs(_snd('param_get', 'speed_rpm_max', timeout_ms=PARAM_GET_MS)) or {}
            old_max = pr.get('value')
            old_max = int(float(old_max)) if old_max not in (None, '') else None
            if old_max is None or old_max < need_rpm:
                _snd('param_set', 'speed_rpm_max', max(need_rpm, 3200), timeout_ms=PARAM_SET_MS)
                say('X/Y 并行：speed_rpm_max %s → %d（之后不再还原）'
                    % (old_max if old_max is not None else '未知', max(need_rpm, 3200)))
        except Exception:                                   # noqa: BLE001
            pass

    # 遥测卡住的轴：本次 move_xy_fast 里"指令已发、读数整段没动过"，残差判定不可信。
    blind: Dict[int, bool] = {}

    def _write_pos(axis, delta, rpm_i):
        """**只下发**一条 motor_pos，立刻返回请求 id（不等回执）。

        2026-10-31 现场：X→Y 成对的 motor_pos 里有 8% 相隔 ≥1 s（最坏 3 s），
        因为以前是"发一条等一条回执"（链路被图传挤时回执迟到 1~3 s）。
        结果 X 早就走完停住，Y 的命令才发出去 —— 肉眼就是「X/Y 分开走」。
        固件对两轴是独立执行的，先发后发差 1~2 ms 没有任何语义差别，
        所以这里改成"两轴连发、回执统一收"。
        """
        return client.send('motor_pos', int(axis), 1 if delta < 0 else 0, int(rpm_i),
                           int(acc), abs(int(delta)), 'rel', timeout_ms=ACK_MS)

    def _collect(rids, timeout_ms=ACK_MS):
        """统一收一组 motor_pos 的回执，返回 {rid: (pairs, reason, code)}。

        code 也带出来：`E09`（goto_busy，固件单槽还在跑）要特殊处理，
        不能当普通失败把整件判死（2026-10-31 16:05 现场）。
        """
        wf = wait if wait is not None else getattr(client, 'wait_reply', None)
        out = {}
        for rid in rids:
            rep = None
            if wf is not None:
                try:
                    rep = wf(rid, timeout_ms)
                except Exception:                       # noqa: BLE001
                    rep = None
            pr = _pairs(rep) or {}
            code = str((rep or {}).get('code') or '') if isinstance(rep, dict) else ''
            out[rid] = (pr, str(pr.get('reason') or ''), code)
        return out

    def _stale1(axis):
        """该轴当前读数是否已过期（取不到 age 就当"新鲜"，保持调用方语义）。"""
        if counts_age_fn is None:
            return False
        try:
            return float(counts_age_fn(int(axis))) > COUNTS_MAX_AGE_MS
        except Exception:                               # noqa: BLE001
            return False

    def _pick_base(axis):
        """算相对位移用的"当前位置"：**调用方给的位置基准（指令账本）优先**。

        为什么不再优先遥测 counts：驱动器只在轴停下后才刷新采样，运动期间遥测帧里的
        counts 会连续几秒冻在"出发前的位置"（16:04 冻 3 s、16:31 又一次）。
        用冻结值算相对位移 → 同一段位移被重复下发（Y 顶到轨道尽头）或干脆少走一整段
        （16:31 回程只发 -19 脉冲，本该 +83162）。
        账本（motion_hook 下发时累加）代表"指令走完之后的位置"，且 host_gui 只在轴静止
        ≥1.2 s 时才用遥测回写账本，所以运动中它以账本为准最安全。
        没有账本（未知/刚清零）时才退回新鲜遥测。
        """
        base = counts or {}
        b = base.get(axis) if hasattr(base, 'get') else None
        if b is not None:
            return float(b)
        cur = read_counts()
        c = cur.get(axis)
        if c is not None and not _stale1(axis):
            return c
        return c

    def _wait_arrive(plans_g, t_est_g):
        """按 counts 到位即走（替代"先睡满 t_est 再判两次读数不变"）。

        判据（先到先用）：
          * 所有轴 |counts - 目标| ≤ arrive_tol_counts(轴)  → 到位，立刻继续；
          * 0.30 s 后最近 ARRIVE_NO_PROGRESS_S 内 counts 完全没变化 → 指令没生效，
            提前交给残差逻辑重发（不再白等到客户端超时）；
          * 硬上限 t_est*1.3+0.5 s（≥1 s，≤9 s）。
        读数过期（age > COUNTS_MAX_AGE_MS）时不做"到位/无进展"判断，只等。
        """
        axes = [a for a, _t, _d in plans_g]
        hard = time.monotonic() + max(1.0, min(9.0, t_est_g * 1.3 + 0.5))
        t0 = time.monotonic()
        prev = read_counts()
        last_change = t0
        changed = False
        while True:
            now_t = time.monotonic()
            cur = read_counts()
            stale = any(_stale1(a) for a in axes)
            if cur and not stale:
                if prev and any(abs(cur.get(a, 1e9) - prev.get(a, 0.0)) > 24 for a in axes):
                    last_change = now_t
                    changed = True
                arrived = all(
                    abs(cur.get(a, 1e9) - counts_of(t)) <= arrive_tol_counts(a)
                    for a, t, _d in plans_g)
                if arrived:
                    say('X/Y 并行：counts 已到位（%.2f s）' % (now_t - t0))
                    return changed or True
                prev = cur
                # 读数整段没变化 = 遥测卡住（不是"指令没生效"）。按预计走位放行：
                # 走完 t_est*1.02+0.10 就继续，别白等到硬上限，也别让 Z 提前往下压。
                if not changed and (now_t - t0) >= max(0.6, t_est_g * 1.02 + 0.10):
                    say('X/Y 并行：读数整段没变化（遥测卡住），按预计走位 %.2f s 继续'
                        % (t_est_g * 1.02 + 0.10))
                    return False
                # 动过但又停住 ARRIVE_NO_PROGRESS_S 没进展 → 交给残差逻辑收口
                if (changed and now_t - t0 > max(0.6, t_est_g * 0.6)
                        and now_t - last_change > ARRIVE_NO_PROGRESS_S):
                    say('X/Y 并行：%.2f s 内读数没有变化（指令可能没生效）→ 交给残差逻辑收口'
                        % (now_t - t0))
                    return True
            if now_t >= hard:
                say('X/Y 并行：等到 %.2f s 仍未到位 → 交给残差逻辑收口' % (now_t - t0))
                return changed
            time.sleep(MOTION_POLL_S)

    def _send_round(send_map):
        """把 {axis: 绝对目标脉冲} **同批并行**下发。

        1) 先把所有轴的 motor_pos 背靠背写出去（中间不插任何等待）；
        2) 再统一收这组回执：reason=speed_limit → 抬限速后**整批重写**一次；
        3) 按 counts 到位即走收口（`_wait_arrive`）。
        返回 (plans, bad)。
        """
        plans_g, bad_g, t_est_g, todo = [], [], 0.0, []
        for axis, tgt in send_map.items():
            c = _pick_base(axis)
            if c is None:
                bad_g.append(axis)
                continue
            now_p = int(round(float(c) / COUNTS_PER_PULSE))
            delta = int(tgt) - now_p
            if delta == 0:
                continue
            eff = min(float(rpm_of(axis)),
                      float(EFFECTIVE_RPM_BY_AXIS.get(int(axis), 1240.0)), 1240.0)
            sec = abs(delta) / max(1.0, eff * 3200.0 / 60.0)
            t_est_g = max(t_est_g, sec)
            todo.append((axis, int(tgt), delta))
        if not todo:
            return plans_g, bad_g

        # --- 关键：两轴连发（不插等待），回执最后统一收 ---
        rids = {}
        for axis, tgt, delta in todo:
            rids[axis] = _write_pos(axis, delta, rpm_of(axis))
            say('%s 并行：%+d 脉冲（%d RPM）' % (AXIS_LABEL.get(axis, axis), delta, rpm_of(axis)))
        say('X/Y 并行：%d 轴已同批发出（%s），等 counts 到位'
            % (len(todo), '/'.join(AXIS_LABEL.get(a, str(a)) for a, _t, _d in todo)))
        acks = _collect(list(rids.values()))

        limited = [a for a in rids if acks.get(rids[a], ({}, '', ''))[1] == 'speed_limit']
        if limited:
            hi = max([int(rpm_of(a)) for a in rids] + [3200])
            first = acks.get(rids[limited[0]], ({}, '', ''))[0] or {}
            say('%s 并行：被限速拒绝（reason=speed_limit, max=%s）→ 把 speed_rpm_max 抬到 %d 后整批重发'
                % ('/'.join(AXIS_LABEL.get(a, str(a)) for a in limited),
                   first.get('max', '?'), hi))
            try:
                _snd('param_set', 'speed_rpm_max', hi, timeout_ms=PARAM_SET_MS)
            except Exception:                           # noqa: BLE001
                pass
            rids = {}
            for axis, tgt, delta in todo:
                rids[axis] = _write_pos(axis, delta, rpm_of(axis))
            acks = _collect(list(rids.values()))

        # E09（goto_busy）= 固件只有一个闭环槽、上一条 goto 还在跑。**不判失败**：
        # 等 0.30 s 让它收尾，再把这几轴重发一次（2026-10-31 16:05 现场：
        # 旧代码超时后立刻重发 → E09 → 第 4 件白失败；其实机械臂就在目标附近）。
        busy = [a for a in rids
                if acks.get(rids[a], ({}, '', ''))[2] == 'E09'
                or acks.get(rids[a], ({}, '', ''))[1] == 'goto_busy']
        if busy:
            say('%s 并行：固件忙（E09 goto_busy，上一条还在跑）→ 等 0.30 s 后只重发这几轴'
                % '/'.join(AXIS_LABEL.get(a, str(a)) for a in busy))
            time.sleep(0.30)
            redo = {}
            for axis, tgt, _d in todo:
                if axis not in busy:
                    continue
                c = _pick_base(axis)
                if c is None:
                    continue
                left = int(tgt) - int(round(float(c) / COUNTS_PER_PULSE))
                if abs(left) <= max(XY_MISS_TOL_PULSES, int(tol_pulses)):
                    say('%s 并行：忙过之后复查已在目标位（差 %+d 脉冲），不再重发'
                        % (AXIS_LABEL.get(axis, axis), left))
                    acks[rids[axis]] = ({'ok': '1'}, '', '')   # 上一条已经把它带到目标
                    continue
                redo[axis] = (int(tgt), left)
            if redo:
                rids2 = {}
                for axis, (_t, left) in redo.items():
                    rids2[axis] = _write_pos(axis, left, rpm_of(axis))
                    say('%s 并行：忙过之后重发 %+d 脉冲'
                        % (AXIS_LABEL.get(axis, axis), left))
                acks2 = _collect(list(rids2.values()))
                for axis, rid in rids2.items():
                    acks[rid] = acks2.get(rid, ({}, '', ''))
                rids.update(rids2)
                for axis, (tgt, _left) in redo.items():
                    for i, (a, _t, _d) in enumerate(todo):
                        if a == axis:
                            todo[i] = (axis, tgt, _left)

        for axis, tgt, delta in todo:
            pr, reason, code = acks.get(rids[axis], ({}, '', ''))
            if code == 'E09' or reason == 'goto_busy':
                say('%s 并行：忙过之后仍被拒（code=%s）→ 交给残差逻辑收口'
                    % (AXIS_LABEL.get(axis, axis), code or reason))
                continue
            if reason:
                say('%s 并行：被拒绝（reason=%s, 应答=%s）'
                    % (AXIS_LABEL.get(axis, axis), reason, pr))
                bad_g.append(axis)
                continue
            plans_g.append((axis, int(tgt), delta))
        if plans_g:
            moved = _wait_arrive(plans_g, t_est_g)
            for axis, _t, _d in plans_g:
                if moved:
                    blind.pop(int(axis), None)
                else:
                    blind[int(axis)] = True
        return plans_g, bad_g

    def _residual_fixes(plans_in):
        """算残差，返回还要收口的 {axis: (绝对目标, 残差或 None)}。

        先用"连续两次读数一致"的新鲜读数收口：运动中/刚停时的旧帧会把残差放大成上万脉冲，
        那正是"偏离目标 +11394 脉冲"误报的来源（2026-10-31 现场确认爪子已在位）。
        容差内 / 读数滞后 / 超上限 / 超可解释范围的处理都在这里，**绝不在这里单轴补走**。
        """
        fin = fresh_counts() or read_counts()
        out = {}
        # 读数过期就不许据此补走：16:04 现场就是这么把同一段位移发了两遍。
        stale_axes = [a for a, _t, _d in plans_in if _stale1(a)]
        if stale_axes:
            t_end = time.monotonic() + 0.35
            while time.monotonic() < t_end and any(_stale1(a) for a in stale_axes):
                time.sleep(0.05)
            fin = read_counts() or fin
        for axis, tgt, delta in plans_in:
            # 先看"知道过期"的（有 age），再看"推断卡住"的（整段没变化）：
            # 两种情况都**不按残差补走、更不 abort** —— 读了不可信的值就绝不据此动轴。
            if _stale1(axis):
                say('%s 并行：读数过期（>%d ms），跳过补走（避免按旧值重复下发）'
                    % (AXIS_LABEL.get(axis, axis), int(COUNTS_MAX_AGE_MS)))
                continue
            if blind.get(int(axis)):
                say('%s 并行：整段读数没变化（遥测卡住）→ 不按残差判定，继续下一步'
                    % AXIS_LABEL.get(axis, axis))
                continue
            c = fin.get(axis)
            if c is None:
                continue
            miss = tgt - int(round(c / COUNTS_PER_PULSE))
            d = abs(int(delta))
            if d >= 200 and (0.60 * d) <= abs(miss) <= (1.15 * d):
                say('%s 并行：读数疑似滞后（残差 %+d ≈ 位移 %+d），跳过补走'
                    % (AXIS_LABEL.get(axis, axis), miss, delta))
                continue
            if abs(miss) <= max(XY_MISS_TOL_PULSES, int(tol_pulses)):
                continue
            fresh_c = fresh_axis(axis)
            if fresh_c is not None:
                new_miss = tgt - int(round(fresh_c / COUNTS_PER_PULSE))
                if new_miss != miss:
                    say('%s 并行残差复核：%+d → %+d 脉冲（用新鲜读数）'
                        % (AXIS_LABEL.get(axis, axis), miss, new_miss))
                    miss = new_miss
            # 复核后可能已经落回容差内（07:39 就是这样：-49332 → 0）
            if abs(miss) <= max(XY_MISS_TOL_PULSES, int(tol_pulses)):
                say('%s 并行残差复核后已在容差内（%+d 脉冲），不补走'
                    % (AXIS_LABEL.get(axis, axis), miss))
                continue
            if abs(miss) > XY_MISS_ABORT_PULSES:
                raise RuntimeError(
                    '%s 并行后偏离目标 %+d 脉冲（>%d），超出回读滞后可解释范围，'
                    '不做补走，请人工检查'
                    % (AXIS_LABEL.get(axis, axis), miss, XY_MISS_ABORT_PULSES))
            if abs(miss) > XY_MISS_SKIP_PULSES:
                # 现场要求（2026-10-31）：爪子已到位时不要停，只告警并继续本件。
                say('%s 并行后偏离目标 %+d 脉冲（>%d）：判定为回读滞后/已在位，'
                    '不做补走，继续本件（不停机）'
                    % (AXIS_LABEL.get(axis, axis), miss, XY_MISS_SKIP_PULSES))
                continue
            out[axis] = (tgt, miss)
        return out

    try:
        # 2) 两轴同时下发 + 判停稳
        plans, bad = _send_round(targets)
        # 3) 残差收口：**整对并行重发**（现场硬要求：X/Y 必须一起走，绝不单轴补走）
        fixes = _residual_fixes(plans)
        for axis in bad:                  # 被拒的轴也放进这一轮，一起并行重发
            fixes.setdefault(axis, (targets[axis], None))
        for _round in range(2):
            if not fixes:
                break
            desc = ', '.join(
                ('%s %+d' % (AXIS_LABEL.get(a, a), m)) if m is not None
                else ('%s 被拒重发' % AXIS_LABEL.get(a, a))
                for a, (_t, m) in fixes.items())
            say('X/Y 并行收口（第 %d 轮，%d 轴一起重发）：%s'
                % (_round + 1, len(fixes), desc))
            send_map = {a: t for a, (t, _m) in fixes.items()}
            plans2, bad2 = _send_round(send_map)
            fixes = _residual_fixes(plans2)
            for axis in bad2:
                fixes.setdefault(axis, (send_map[axis], None))
        if fixes:
            say('X/Y 并行收口后仍有偏差（%s）：按现场要求继续本件，不再单轴补走'
                % ', '.join('%s %+d' % (AXIS_LABEL.get(a, a), (m if m is not None else 0))
                            for a, (_t, m) in fixes.items()))
        return [{'axis': a, 'pulses': targets[a], 'ok': True, 'code': '', 'text': '', 'pairs': {}}
                for a in sorted(targets)]
    finally:
        # 这里**故意什么都不还原**（见上面 speed_rpm_max 注释）：
        # 限速一旦抬起就保持整批有效，固件参数本身是持久的。
        pass


