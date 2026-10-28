"""自动抓一件：视觉识别 → 算 X/Y → 抓 → 抬 → 回 P1 → 降 → 退 P0 → 转盘 → 松爪。

设计原则
--------
* **只用手工验证过的原语**：`prep_pose.move_abs`（绝对位置、带单步上限、优先用
  回读 counts 算增量）负责所有平移，`client.angle` 负责转盘，
  `client.send('gripper', ...)` 负责夹爪。自动流程与手动按按钮走同一套代码。
* **不猜位置**：`move_abs` 拿不到回读 counts、也没有软位置时会直接拒绝
  （POS_UNKNOWN），本模块不做任何"盲走"兜底 —— 撞一次机的代价太大。
* **顺序固定**（现场验证过的 10 步）：
  Z 抬顶 → X/Y 到物块上方 → Z 降底 → 夹紧 → Z 抬顶 → X/Y 回 P1
  → Z 降底 → Y 退 P0 → 转盘 60° → 松爪 180 → 500 ms 后回 158。
* 视觉只给 `center_tray` 的 (u, v)，X/Y 脉冲由 `vision/tray_calib.json` 的仿射
  （a/b/c/d/tx/ty）反算，所以换相机或挪支架必须重新标定。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import box_map as BM
import prep_pose

PKG_ROOT = Path(__file__).resolve().parent.parent
TARGETS_PATH = PKG_ROOT / 'vision' / 'runtime' / 'latest_targets.json'
CALIB_PATH = PKG_ROOT / 'vision' / 'tray_calib.json'

AXIS_Z = prep_pose.AXIS_Z          # 1
AXIS_TRAY = 3
AXIS_X = 4
AXIS_Y = 5

#: 现场实测：Z 底部 0 = 抓取位、Z 顶部 −152,000 = 抬起位、通道末端 Y = −13,144
Z_DOWN = 0
Z_UP = -152000
P1_Y = -13144
P0_Y = 0
TRAY_DEG = 60.0

SETTLE_GRAB_MS = 700       # 夹紧后等舵机走完
SETTLE_OPEN_MS = 700       # 松爪后等货物落盒
SETTLE_READY_MS = 400      # 回预备位

RPM = {AXIS_Z: 1500, AXIS_X: 120, AXIS_Y: 300, AXIS_TRAY: 300}


#: 每个盒位之间的角度（6 个盒子均分一圈）
_ACC_DEFAULT = 0            # 加速度档：0 最快（现场实测 acc 越大越慢）
TRAY_RPM = 5              # 转盘转速（现场实测：5 刚好，太快会甩货）
BOX_STEP_DEG = 60.0
BOX_COUNT = 6


def class_key(target: Dict) -> str:
    """物块的类别键：'形状|颜色'（与视觉 class_key 的写法一致）。"""
    shape = str(target.get('shape') or target.get('class_name') or '未知').strip()
    color = str(target.get('color') or '').strip()
    return '%s|%s' % (shape, color) if color else shape


class GrabError(RuntimeError):
    """自动抓一件失败（原因直接给人看）。"""


def load_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:                          # noqa: BLE001 —— 缺文件就吃默认值
        return default


def load_targets(path: Path = TARGETS_PATH) -> List[dict]:
    data = load_json(path, {}) or {}
    out: List[dict] = []
    for t in (data.get('targets') or []):
        if not isinstance(t, dict):
            continue
        uv = t.get('center_tray')
        if not uv or len(uv) != 2:
            continue
        out.append(t)
    return out


def pick_target(targets: Iterable[dict]) -> dict:
    """挑一件：置信度高优先，其次面积大（大件更稳），再其次 v 小（离通道口近）。"""
    def key(t: dict):
        try:
            conf = float(t.get('confidence') or 0.0)
        except Exception:                      # noqa: BLE001
            conf = 0.0
        try:
            area = float(t.get('area_px') or 0.0)
        except Exception:                      # noqa: BLE001
            area = 0.0
        u, v = t['center_tray']
        return (-conf, -area, float(v), float(u))
    items = list(targets)
    if not items:
        raise GrabError('视觉没有识别到物块（先点「单次识别」确认画面里有货）')
    return sorted(items, key=key)[0]


def target_xy(target: dict, calib: Optional[dict] = None,
              calib_path: Path = CALIB_PATH) -> tuple:
    """把视觉 (u, v) 换成 X/Y 绝对脉冲。"""
    calib = calib or load_json(calib_path)
    if not calib:
        raise GrabError('缺少 %s：先在「X/Y 标定」页求解并保存' % calib_path.name)
    affine = calib.get('affine') or calib
    try:
        a, b, c, d = (float(affine[k]) for k in ('a', 'b', 'c', 'd'))
        tx, ty = float(affine['tx']), float(affine['ty'])
    except Exception as exc:                   # noqa: BLE001
        raise GrabError('标定文件格式不对（缺 a/b/c/d/tx/ty）：%s' % exc)
    u, v = (float(x) for x in target['center_tray'])
    return a * u + b * v + tx, c * u + d * v + ty


def describe(target: dict) -> str:
    u, v = target['center_tray']
    name = target.get('class_name') or target.get('shape') or '未知'
    color = target.get('color') or ''
    return '%s %s u=%.2f v=%.2f' % (name, color, float(u), float(v))


def run_once(client, target: dict, log=print, wait=None, calib=None,
             pos_source: Optional[Callable[[], tuple]] = None,
             drop: bool = True, tray_deg: float = TRAY_DEG) -> Dict[str, object]:
    """按固定顺序走一遍，成功返回 dict，失败抛 GrabError。

    drop=True  抓完继续「转盘 60° → 松爪 → 回预备」；
    drop=False 抓完停在 P0（手里还夹着），交给人工决定要不要放。
    pos_source() 返回 (soft, known, counts) 三个 dict，给 move_abs 判断当前位置；
    缺省时从 client 的软位置里取（GUI 应传入自己的面板数据，能拿到回读 counts）。
    """
    if not client.online:
        raise GrabError('还没连上控制板（右上角要显示「在线」）')
    x_pulse, y_pulse = target_xy(target, calib=calib)
    log('目标：%s → X %+.0f / Y %+.0f 脉冲' % (describe(target), x_pulse, y_pulse))
    if abs(x_pulse) > 30000 or abs(y_pulse) > 200000:
        raise GrabError('算出的目标超出合理范围（X %+.0f / Y %+.0f 脉冲），先检查标定'
                        % (x_pulse, y_pulse))

    def say(text: str) -> None:
        log('[自动抓] ' + text)

    def pos():
        if pos_source is None:
            return None, None, None
        try:
            return pos_source()
        except Exception:                      # noqa: BLE001
            return None, None, None

    def move(targets: Dict[int, float], order: List[int]) -> None:
        soft, known, counts = pos()
        results = prep_pose.move_abs(client, targets, order=order, rpm=RPM, acc=0,
                                     log=say, wait=wait, soft=soft, known=known,
                                     counts=counts)
        bad = [r for r in results if not r.get('ok')]
        if bad:
            first = bad[0]
            raise GrabError('%s 没走到位：%s %s'
                            % (prep_pose.AXIS_LABEL.get(first.get('axis'), ''),
                               first.get('code') or '', first.get('text') or ''))

    t0 = time.perf_counter()
    # 固定路线的硬规矩：**穿通道时 Z 必须在底部**（通道净高不够），
    # 所以出去是"先 Y 后 Z"、回来是"先 Z 后 Y"。这里第一下必须是 Y，
    # 不能在初始位先把 Z 抬起来（会顶到托盘/横梁）。
    move({AXIS_Y: P1_Y}, [AXIS_Y])                      # 1 Y 进通道（Z 仍在底）
    move({AXIS_Z: Z_UP}, [AXIS_Z])                      # 2 在通道末端抬到顶
    move({AXIS_X: x_pulse, AXIS_Y: y_pulse}, [AXIS_X, AXIS_Y])   # 3 到物块上方
    move({AXIS_Z: Z_DOWN}, [AXIS_Z])                    # 4 Z 降底 = 抓取位
    say('夹紧 135°')
    client.send('gripper', 'grab', timeout_ms=2000)     # 5 夹紧
    time.sleep(SETTLE_GRAB_MS / 1000.0)
    move({AXIS_Z: Z_UP}, [AXIS_Z])                      # 6 抬起
    move({AXIS_X: 0, AXIS_Y: P1_Y}, [AXIS_X, AXIS_Y])   # 7 只动 X/Y 回 P1（高位）
    move({AXIS_Z: Z_DOWN}, [AXIS_Z])                    # 8 在 P1 先降到底
    move({AXIS_Y: P0_Y}, [AXIS_Y])                      # 9 再退 Y 回 P0
    if not drop:
        say('已回到 P0，物块还在爪里（按要求不落货）')
        elapsed = time.perf_counter() - t0
        return {'ok': True, 'x': x_pulse, 'y': y_pulse, 'elapsed': elapsed,
                'target': describe(target), 'dropped': False}
    if abs(tray_deg) >= 1.0:
        # 10 空盒转到落料位：闭环 goto + 指定慢转速（现场实测 rpm≈5 合适；motor_angle 需要角度基准，不用）
        _n = int(round(tray_deg / 360.0 * 3200.0))
        say('转盘 %+.0f°（%d 脉冲，rpm %d）' % (tray_deg, _n, TRAY_RPM))
        try:
            client.goto(AXIS_TRAY, _n, rpm=TRAY_RPM)
        except Exception as _exc:                       # noqa: BLE001
            say('转盘闭环失败（%s），退回相对脉冲' % _exc)
            _d = 0 if tray_deg >= 0 else 1
            client.send('motor_pos', AXIS_TRAY, _d, TRAY_RPM, _ACC_DEFAULT, abs(_n), 'rel')
    time.sleep(0.3)
    time.sleep(0.5)
    say('松爪 180° → 等待落盒 → 回预备 158°')
    client.send('gripper', 'open', timeout_ms=2000)     # 11 松爪
    time.sleep(SETTLE_OPEN_MS / 1000.0)
    client.send('gripper', 'ready', timeout_ms=2000)
    time.sleep(SETTLE_READY_MS / 1000.0)

    elapsed = time.perf_counter() - t0
    say('完成，用时 %.1f s' % elapsed)
    return {'ok': True, 'x': x_pulse, 'y': y_pulse, 'elapsed': elapsed,
            'target': describe(target)}


def run_many(client, count: int, recognize: Callable[[], object], log=print, wait=None,
             pos_source: Optional[Callable[[], tuple]] = None, drop: bool = True,
             stop_flag: Optional[Callable[[], bool]] = None,
             current_box: int = 1,
             on_event: Optional[Callable[[dict], None]] = None) -> Dict[str, object]:
    """连续抓 N 件：每件都"重新识别 → 挑目标 → 走一遍 run_once"。

    recognize() 由 GUI 传入（阻塞式单次识别，返回值得能从中读 latest_targets.json），
    本函数每轮调用一次，然后重新读 targets 文件挑目标——这样后一件不会去抓
    已经被拿走的那件（它的 u,v 已经从画面里消失）。

    drop=True 时每件都会执行"转盘 60° + 松爪"，物块按 6 个盒位轮流落；
    stop_flag() 返回 True 时在当前件结束后停下（界面的「停止全部」也能中断）。
    """
    def emit(kind, **kw):
        """给界面/日志用的事件回调（异常一律吞掉，绝不影响抓取）。"""
        if on_event is None:
            return
        try:
            ev = {'kind': kind}
            ev.update(kw)
            on_event(ev)
        except Exception:                          # noqa: BLE001
            pass

    done = 0
    failed = 0
    started = time.perf_counter()
    results: List[Dict[str, object]] = []
    box_now = int(current_box) if 1 <= int(current_box) <= BOX_COUNT else 1
    log('[连续抓] 假设当前落料位的盒子 = %d 号（转盘一圈 6 盒，每盒 60°）' % box_now)
    for i in range(max(0, int(count))):
        if stop_flag is not None and stop_flag():
            log('[连续抓] 收到停止信号，第 %d 件前结束' % (i + 1))
            emit('stopped', i=i + 1)
            break
        log('=== 第 %d/%d 件：单次识别 ===' % (i + 1, count))
        emit('item_start', i=i + 1, n=count)
        try:
            recognize()
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            log('[连续抓] 第 %d 件识别失败：%s' % (i + 1, exc))
            emit('item_fail', i=i + 1, reason='识别失败：%s' % exc)
            break
        targets = load_targets()
        if not targets:
            log('[连续抓] 画面里没有可抓的物块了，结束（已抓 %d 件）' % done)
            emit('item_fail', i=i + 1, reason='没有可抓的物块')
            break
        try:
            target = pick_target(targets)
        except GrabError as exc:
            log('[连续抓] 挑目标失败：%s' % exc)
            emit('item_fail', i=i + 1, reason='挑目标失败：%s' % exc)
            break
        key = class_key(target)
        try:
            box = BM.assign(key) if drop else 0
        except ValueError as exc:
            failed += 1
            log('[连续抓] 分盒失败：%s（可在「连续抓」旁点「分盒清零」重来）' % exc)
            break
        deg = 0.0
        if drop:
            deg = ((int(box) - box_now) % BOX_COUNT) * BOX_STEP_DEG
            log('[连续抓] 第 %d 件类别 %s → %d 号盒（转盘 %+.0f°）'
                % (i + 1, key, box, deg))
        emit('target', i=i + 1, target=describe(target), key=key, box=box, deg=deg)
        try:
            one = run_once(client, target, log=log, wait=wait, pos_source=pos_source,
                           drop=drop, tray_deg=deg)
        except GrabError as exc:
            failed += 1
            log('[连续抓] 第 %d 件失败：%s' % (i + 1, exc))
            emit('item_fail', i=i + 1, reason=str(exc))
            break
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            log('[连续抓] 第 %d 件异常：%s: %s' % (i + 1, type(exc).__name__, exc))
            emit('item_fail', i=i + 1, reason='%s: %s' % (type(exc).__name__, exc))
            break
        done += 1
        if drop:
            box_now = int(box)
        results.append(one)
        log('[连续抓] 第 %d 件完成（累计 %d 件）' % (i + 1, done))
        emit('item_ok', i=i + 1, res=one, box=box)
    elapsed = time.perf_counter() - started
    log('[连续抓] 结束：成功 %d 件、失败 %d 件，总用时 %.1f s；%s'
        % (done, failed, elapsed, BM.describe()))
    emit('finished', done=done, failed=failed, elapsed=elapsed)
    return {'ok': failed == 0, 'done': done, 'failed': failed,
            'elapsed': elapsed, 'items': results}
