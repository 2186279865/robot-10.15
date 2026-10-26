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
            say('%s：走 %+d 脉冲（%d RPM）' % (title, pulses, rpm))
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
