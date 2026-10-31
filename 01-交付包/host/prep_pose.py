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

#: 2026-10-31 17:30 之后的定版：**X/Y 横移是全开环**（只发不判、不补走，
#: 见 move_xy_fast 的 docstring）。下面这组残差阈值只剩 `move_abs`/历史调用在用，
#: 新的 X/Y 路径不再产生「并行后偏离目标」这类判断。
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
    """X/Y **一步并行**相对位移：**全开环**（2026-10-31 现场定版，用户要求）。

    为什么不再判到位 / 不再补走：逐帧核对证明**每段执行本身是准的**，之前所有
    "走错 / 乱走" 都是确认环节拿错读数把位移又重放了一次 ——
      * 16:04 冻结遥测把 Y 又推了 +26076 脉冲，直接顶到轨道尽头（E14 → 超时 → E09）；
      * 16:31 冻结遥测把账本覆盖回 P1，回程只发了 -19 脉冲；
      * 16:54 "并行收口"按旧账本把 -472 算成 -8668，X 一路跑到 -90 mm。
    所以这里只做四件事：
      1. 两轴背靠背各发一条 `motor_pos`（毫秒内同批发出，固件并行执行）；
      2. 只按**实测有效转速**（EFFECTIVE_RPM_BY_AXIS）等预计用时，不读任何 counts；
      3. 固件是单槽：哪条回执是 E09（上一条还在跑）就 0.10 s 后重发那一轴，最多 3 次；
      4. 返回 {axis: 实发脉冲}，由调用方自己累加账本（不再碰遥测 / 编码器 / 补走）。

    counts / tol_pulses / timeout_s / counts_age_fn 只为兼容旧调用保留，本函数不读。
    """
    say = log or (lambda _t: None)
    rpm_of = ((lambda a: int(rpm.get(int(a), 1600))) if isinstance(rpm, dict)
              else (lambda a: int(rpm)))
    targets = {int(k): int(round(float(v))) for k, v in targets.items()
               if v is not None and int(round(float(v))) != 0}
    if not targets:
        return {}

    if speed_setup:
        # 一批只抬一次限速：Y@2000 需要 speed_rpm_max >= 3200（固件默认 300）。
        # 只发不等；真被拒的话下面的 E09 分支会重发。
        try:
            client.send('param_set', 'speed_rpm_max', 3200, timeout_ms=PARAM_SET_MS)
            say('X/Y 并行：speed_rpm_max 抬到 3200（一批只做一次）')
        except Exception:                              # noqa: BLE001
            pass

    def _trip_s(axis: int) -> float:
        speed = max(1.0, float(EFFECTIVE_RPM_BY_AXIS.get(axis, 300)) * 3200.0 / 60.0)
        return abs(targets[axis]) / speed

    def _send(axis: int) -> int:
        d = int(targets[axis])
        return client.send('motor_pos', axis, 1 if d < 0 else 0, rpm_of(axis),
                           int(acc), abs(d), 'rel', timeout_ms=ACK_MS)

    rids = {}
    for axis in sorted(targets):
        rids[axis] = _send(axis)
        say('X/Y 并行：%s %+d 脉冲（%d RPM）'
            % (AXIS_LABEL.get(axis, axis), targets[axis], rpm_of(axis)))
    t_wait = min(9.0, max(_trip_s(a) for a in targets) * 1.06 + 0.10)
    say('X/Y 并行：%d 轴已同批发出，按预计 %.2f s 等（不判读数、不补走）'
        % (len(targets), t_wait))

    for attempt in range(3):
        time.sleep(t_wait)
        busy = []
        for axis, rid in rids.items():
            rep = None
            try:
                rep = client.done_of(rid)
            except Exception:                          # noqa: BLE001
                rep = None
            if rep and rep.get('kind') == 'err':
                code = str(rep.get('code') or '')
                if code == 'E09' or 'E09' in str(rep.get('text') or ''):
                    busy.append(axis)
        if not busy:
            break
        say('X/Y 并行：%s 被固件拒绝（E09 忙）→ 0.10 s 后重发第 %d 次'
            % (', '.join(AXIS_LABEL.get(a, str(a)) for a in busy), attempt + 1))
        time.sleep(0.10)
        for axis in busy:
            rids[axis] = _send(axis)

    return {a: targets[a] for a in sorted(targets)}
