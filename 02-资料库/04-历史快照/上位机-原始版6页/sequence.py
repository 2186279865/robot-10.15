"""动作序列：数据模型 + 执行器。

设计约束（来自 `上位机方案-v3-TypeC-CDC.md` 第 6 节）：

* 序列只是一串"协议命令 + 等待条件"，执行器不直接碰串口，只调用 `HostClient.send()`。
* `ok` 只代表固件接纳，**不代表到位**；只有收到 `#0,event,task,...state=reached` 才算到位，
  且只有接了回读的轴才可能报到位。未接回读的轴用"到时即过"（wait=ms）而不是假装到位。
* 位置类指令只发一次；超时不自动重放（`HostClient` 已把运动类超时标成"结果未知"）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence as TSequence

import protocol as P

#: 等待条件
WAIT_OK = "ok"            # 收到 ok 即进入下一步
WAIT_TIME = "ms"          # 收到 ok 后再等固定时间
WAIT_REACHED = "reached"  # 等到位事件（仅回读轴可用）

ON_TIMEOUT_ABORT = "abort"
ON_TIMEOUT_CONTINUE = "continue"

#: 步骤类型 -> (显示名, 参数说明) —— 参数顺序与协议命令一致
STEP_KINDS: Dict[str, tuple] = {
    "motor_enable": ("使能电机", "轴, on(0/1)"),
    "motor_speed": ("速度模式", "轴, 方向(0正/1负), 转速, 加速度"),
    "motor_pos": ("位置模式", "轴, 方向, 转速, 加速度, 脉冲数, rel/abs"),
    "motor_stop": ("单轴停止", "轴"),
    "stop_all": ("停止全部", ""),
    "motor_stall_clear": ("解除堵转", "轴"),
    "motor_zero": ("位置清零", "轴"),
    "gripper": ("夹爪动作", "grab/open/step_grab/step_release"),
    "servo_angle": ("舵机角度", "角度 0~270"),
    "wait_ms": ("等待时间", "毫秒"),
    "wait_reached": ("等到位", "轴, 超时毫秒"),
    "note": ("注释", "文本"),
}


@dataclass
class Step:
    """一步动作。`args` 直接就是协议命令的参数列表。"""

    kind: str
    args: List[object] = field(default_factory=list)
    wait: str = WAIT_OK
    wait_ms: int = 0
    timeout_ms: int = 5000
    on_timeout: str = ON_TIMEOUT_ABORT
    note: str = ""

    def validate(self, readback_axes: TSequence[int] = ()) -> List[str]:
        problems: List[str] = []
        if self.kind not in STEP_KINDS:
            problems.append(f"未知动作 {self.kind}")
            return problems
        if self.on_timeout not in (ON_TIMEOUT_ABORT, ON_TIMEOUT_CONTINUE):
            problems.append("on_timeout 只能是 abort/continue")
        if self.kind in ("motor_speed", "motor_pos", "motor_stop", "motor_enable",
                         "motor_stall_clear", "motor_zero"):
            if not self.args or int(self.args[0]) not in P.VALID_AXES:
                problems.append(f"{self.kind} 的轴必须是 {'/'.join(map(str, P.VALID_AXES))}")
        if self.kind == "wait_reached":
            addr = int(self.args[0]) if self.args else 0
            if addr not in readback_axes:
                problems.append(
                    f"轴 {addr} 没有回读能力，wait_reached 无法判定，请改用 wait_ms"
                )
        if self.wait == WAIT_REACHED and self.kind != "wait_reached":
            addr = int(self.args[0]) if self.args and self.kind.startswith("motor") else 0
            if addr and addr not in readback_axes:
                problems.append(f"轴 {addr} 没有回读能力，不能以到位作为完成条件")
        if self.wait_ms < 0 or self.timeout_ms <= 0:
            problems.append("时间参数必须为正")
        return problems

    def describe(self) -> str:
        name = STEP_KINDS.get(self.kind, (self.kind, ""))[0]
        detail = ",".join(str(a) for a in self.args)
        if self.wait == WAIT_TIME:
            detail = f"{detail} 等待{self.wait_ms}ms".strip()
        return f"{name} {detail}".strip()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        return cls(
            kind=str(data.get("kind", "")),
            args=list(data.get("args", [])),
            wait=str(data.get("wait", WAIT_OK)),
            wait_ms=int(data.get("wait_ms", 0)),
            timeout_ms=int(data.get("timeout_ms", 5000)),
            on_timeout=str(data.get("on_timeout", ON_TIMEOUT_ABORT)),
            note=str(data.get("note", "")),
        )


@dataclass
class Sequence:
    name: str = "未命名序列"
    steps: List[Step] = field(default_factory=list)

    def validate(self, readback_axes: TSequence[int] = ()) -> List[str]:
        problems: List[str] = []
        for index, step in enumerate(self.steps, 1):
            for problem in step.validate(readback_axes):
                problems.append(f"第 {index} 步：{problem}")
        return problems

    def to_dict(self) -> dict:
        return {"name": self.name, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: dict) -> "Sequence":
        return cls(
            name=str(data.get("name", "未命名序列")),
            steps=[Step.from_dict(s) for s in data.get("steps", [])],
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "Sequence":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------

IDLE = "idle"
RUNNING = "running"
WAITING = "waiting"
PAUSED = "paused"
DONE = "done"
ABORTED = "aborted"


class SequenceRunner:
    """一步一步把序列推给 `HostClient`，并据此推进状态。

    界面必须把收到的 `UiEvent` 转发给 `notify()`，否则"到位/超时"永远判不出来。
    """

    def __init__(
        self,
        client,
        now_ms: Callable[[], int],
        on_update: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.client = client
        self._now = now_ms
        self.on_update = on_update
        self.sequence: Optional[Sequence] = None
        self.index = 0
        self.state = IDLE
        self._resume_state = IDLE
        self.message = ""
        self.last_error = ""
        self._req_id: Optional[int] = None
        self._deadline = 0
        self._reach_deadline = 0
        self._reach_addr = 0
        self._reach_task = ""
        self._started_ms = 0

    # -- 控制 ---------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.state in (RUNNING, WAITING, PAUSED)

    def start(self, sequence: Sequence) -> List[str]:
        problems = sequence.validate(getattr(self.client, "readback_axes", ()))
        if problems:
            return problems
        self.sequence = sequence
        self.index = 0
        self.last_error = ""
        self._started_ms = self._now()
        self.state = RUNNING
        self._inform("开始执行")
        self._advance()
        return []

    def pause(self) -> None:
        if self.active and self.state != PAUSED:
            self._resume_state = self.state
            self.state = PAUSED
            self._inform("已暂停")

    def resume(self) -> None:
        if self.state == PAUSED:
            # 恢复暂停前的状态，不重新下发当前这一步（位置指令禁止重复下发）。
            self.state = self._resume_state if self._resume_state in (RUNNING, WAITING) else RUNNING
            self._inform("继续执行")
            self.tick()

    def abort(self, reason: str = "用户中止") -> None:
        if not self.active:
            return
        self.state = ABORTED
        self.last_error = reason
        self._inform(f"已中止：{reason}")
        self._req_id = None

    # -- 驱动 ---------------------------------------------------------------
    def tick(self) -> None:
        if self.state not in (RUNNING, WAITING):
            return
        now = self._now()
        if self.state == WAITING and now >= self._deadline:
            step = self._current()
            if step is not None and (step.kind == "wait_reached" or step.wait == WAIT_REACHED):
                self._timeout("等到位超时")
            else:
                self._next()
            return
        if self.state == RUNNING and self._req_id is not None and now >= self._deadline:
            self._timeout("等待固件应答超时")

    def notify(self, event) -> None:
        """界面把 `UiEvent` 喂进来：ok / err / timeout / 到位事件都在这里闭环。"""
        if self._req_id is not None and event.data.get("req_id") == self._req_id:
            if event.kind == "err":
                self._fail(f"固件拒绝：{event.data.get('code', '')}")
                return
            if event.kind == "timeout":
                self._timeout("固件无应答（结果未知，不重发）")
                return
            if event.kind == "reply":
                step = self._current()
                if step is None:
                    return
                # 固件在 ok 里回带 task=<id>，到位事件用同一个 id 关联，
                # 因此这里记住它，后面只认这一个任务的到位事件。
                task_id = (event.data.get("pairs") or {}).get("task")
                if task_id is not None:
                    self._reach_task = str(task_id)
                if step.wait == WAIT_TIME:
                    self.state = WAITING
                    self._deadline = self._now() + max(0, step.wait_ms)
                    self._inform(f"第 {self.index + 1} 步已下发，等待 {step.wait_ms}ms")
                elif step.wait == WAIT_REACHED:
                    self.state = WAITING
                    self._deadline = self._now() + step.timeout_ms
                    self._inform(f"第 {self.index + 1} 步已下发，等待到位")
                else:
                    self._next()
                return
        if self.state == WAITING and event.kind == "event":
            data = event.data
            if data.get("type") != "task" or data.get("state") != P.EVIDENCE_REACHED:
                return
            if self._reach_task and str(data.get("id")) != self._reach_task:
                return
            addr = data.get("addr")
            if self._reach_addr and addr not in (None, self._reach_addr):
                return
            self._next()

    # -- 内部 ---------------------------------------------------------------
    def _current(self) -> Optional[Step]:
        if self.sequence is None or self.index >= len(self.sequence.steps):
            return None
        return self.sequence.steps[self.index]

    def _advance(self) -> None:
        if self.state == PAUSED:
            return
        step = self._current()
        if step is None:
            self.state = DONE
            self._inform("执行完成")
            return
        now = self._now()
        if step.kind == "wait_ms":
            self.state = WAITING
            self._reach_addr = 0
            self._deadline = now + max(0, int(step.args[0] if step.args else step.wait_ms))
            self._inform(f"等待 {max(0, int(step.args[0] if step.args else step.wait_ms))}ms")
            return
        if step.kind == "wait_reached":
            self.state = WAITING
            self._reach_addr = int(step.args[0]) if step.args else 0
            self._reach_task = ""
            self._deadline = now + max(1, int(step.args[1] if len(step.args) > 1 else step.timeout_ms))
            self._inform(f"等待轴 {self._reach_addr} 到位")
            return
        if step.kind == "note":
            self._inform(str(step.args[0]) if step.args else step.note)
            self._next()
            return
        # 普通协议命令
        self.state = RUNNING
        try:
            self._req_id = self.client.send(step.kind, *step.args, timeout_ms=step.timeout_ms)
        except Exception as exc:
            self._fail(f"发送失败：{exc}")
            return
        self._deadline = now + step.timeout_ms
        self._inform(f"第 {self.index + 1} 步：{step.describe()}")

    def _next(self) -> None:
        self._req_id = None
        self._reach_addr = 0
        self._reach_task = ""
        self.index += 1
        if self.state == WAITING:
            self.state = RUNNING
        self._advance()

    def _timeout(self, reason: str) -> None:
        step = self._current()
        if step is not None and step.on_timeout == ON_TIMEOUT_CONTINUE:
            self._inform(f"{reason}：按设置跳过")
            self._next()
        else:
            self._fail(reason)

    def _fail(self, reason: str) -> None:
        self.state = ABORTED
        self.last_error = reason
        self._inform(f"中止：{reason}")

    def _inform(self, message: str) -> None:
        self.message = message
        if self.on_update is not None:
            self.on_update(message)
