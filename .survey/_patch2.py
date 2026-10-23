import io
p = 'host_gui.py'
t = io.open(p, encoding='utf-8').read()

# 1) 在 send_gripper 之前插入 journey 面板与方法
anchor = '    def send_gripper(self, action: str) -> None:'
block = '''    # -- 回上电初始位：自动记录本次上电后的全部移动 ---------------------------
    def _build_journey(self, parent: ttk.Frame, row: int) -> None:
        box = ttk.LabelFrame(
            parent, text="回上电初始位（自动记录本次上电后的全部移动，四轴都记）")
        box.grid(row=row, column=0, columnspan=2, sticky="we", padx=6, pady=(0, 6))
        line = ttk.Frame(box)
        line.pack(fill="x", padx=6, pady=(4, 2))
        self.journey_status = ttk.Label(line, text=J.describe(self.journey))
        self.journey_status.pack(side="left")
        ttk.Button(line, text="清空记录", command=self.journey_clear).pack(side="right")
        ttk.Button(line, text="原路返回初始位",
                   command=self.journey_return).pack(side="right", padx=6)
        ttk.Label(
            box,
            text="按记录倒序、方向取反重放一遍。开环无回读，返回后请目视确认确实回到上电位置。",
            foreground="#666666").pack(fill="x", padx=6, pady=(0, 6))
        self.journey_refresh()

    def journey_refresh(self) -> None:
        try:
            self.journey_status.configure(text=J.describe(self.journey))
        except (AttributeError, tk.TclError):      # pragma: no cover - 无显示环境
            pass

    def _journey_add(self, axis: int, direction: int, rpm: int, acc: int, pulses: int) -> None:
        """记一段真实发生的位移；返回动作自己引起的位移不记（否则会越滚越多）。"""
        if self._journey_running:
            return
        try:
            J.append(self.journey, axis, direction, rpm, acc, pulses)
        except ValueError:
            return
        try:
            J.save(self.journey)
        except OSError as exc:
            self.note(f"移动记录写盘失败（不影响运动）：{exc}")
        self.journey_refresh()

    def journey_clear(self) -> None:
        if not (self.journey.get("steps") or []):
            self.note("移动记录本来就是空的")
            return
        if not messagebox.askyesno(
                "清空移动记录",
                "清空本次上电后的移动记录？\\n\\n"
                "清空后就只能回到“现在这个位置”，不能再原路返回更早的位置。"):
            return
        J.clear(self.journey)
        try:
            J.save(self.journey)
        except OSError:
            pass
        self.journey_refresh()
        self.note("已清空移动记录（从当前位置重新开始记）")

    def journey_return(self) -> None:
        """原路返回：把记录倒序 + 方向取反，逐段走回去。"""
        if not self.client.online:
            self.note("未连接：先连上控制板")
            return
        if self._journey_running or self._approach_running:
            self.note("还有动作在跑，等它跑完再返回")
            return
        problems = J.validate(self.journey)
        if problems:
            self.note("不能返回：" + "；".join(problems))
            return
        steps = J.reverse_steps(self.journey)
        if not messagebox.askyesno(
                "原路返回初始位",
                f"将按记录倒序走 {len(steps)} 段，回到本次上电的初始位置。\\n\\n"
                f"预计 {J.estimate_ms(self.journey) / 1000:.1f} 秒。\\n\\n"
                "确认路径上无人无障碍、急停可达吗？"):
            return
        if self.client.latched:
            try:
                self.client.send("stop_clear", timeout_ms=1000)
            except Exception:                     # noqa: BLE001 —— 尽力而为
                pass
        self._journey_queue = list(steps)
        self._journey_running = True
        self.journey_refresh()
        self.note(f"开始原路返回：{len(steps)} 段（倒序、方向取反）")
        self.root.after(120, self._journey_step)

    def _journey_step(self) -> None:
        if not self._journey_running:
            return
        if not self.client.online:
            self._journey_finish("连接断了，原路返回中断")
            return
        if not self._journey_queue:
            self._journey_finish("已按原路返回（现在应在整机上电的初始位）", clear=True)
            return
        step = self._journey_queue.pop(0)
        try:
            self.client.send("motor_enable", step["axis"], 1, timeout_ms=1000)
            self.client.send("motor_pos", step["axis"], step["dir"], step["rpm"],
                             step["acc"], step["pulses"], "rel", timeout_ms=1000)
        except Exception as exc:                  # noqa: BLE001
            self._journey_finish(f"发送失败，原路返回中断：{exc}")
            return
        self.journey_refresh()
        self.root.after(J.step_ms(step), self._journey_step)

    def _journey_finish(self, message: str, clear: bool = False) -> None:
        self._journey_running = False
        self._journey_queue = []
        if clear:
            # 回到起点了，记录已用完：清空，避免再点一次又往反方向走
            J.clear(self.journey)
            try:
                J.save(self.journey)
            except OSError:
                pass
        self.journey_refresh()
        self.note(message)

'''
if anchor not in t:
    raise SystemExit('anchor missing')
t = t.replace(anchor, block + anchor, 1)

# 2) 急停也要清 journey 队列
old = '''        self._approach_queue = []
        self._approach_running = False
        self._approach_cb = None
        self.approach_refresh()'''
new = '''        self._approach_queue = []
        self._approach_running = False
        self._approach_cb = None
        self.approach_refresh()
        self._journey_queue = []
        self._journey_running = False
        self.journey_refresh()'''
if old not in t:
    raise SystemExit('emergency anchor missing')
t = t.replace(old, new, 1)

# 3) 固件重启（boot_id 变）→ 清空记录，重新从这次上电开始记
old2 = '''        self.fw_label.configure(
            text=f"固件 {self.client.fw_version or '—'} 启动 {self.client.boot_id or '—'}"
        )'''
new2 = '''        self.fw_label.configure(
            text=f"固件 {self.client.fw_version or '—'} 启动 {self.client.boot_id or '—'}"
        )
        # 固件重启（boot_id 变了）说明这是新的一次上电：移动记录从头开始
        boot = self.client.boot_id
        if state == STATE_ONLINE and boot and boot != self._journey_boot:
            self._journey_boot = boot
            self.journey = J.empty(boot)
            try:
                J.save(self.journey)
            except OSError:
                pass
            self.journey_refresh()
            self.note(f"检测到固件启动号 {boot}：移动记录已清零，从现在开始记")'''
if old2 not in t:
    raise SystemExit('refresh anchor missing')
t = t.replace(old2, new2, 1)

io.open(p, 'w', encoding='utf-8', newline='\n').write(t)
print('journey ui + stop + boot reset ok')
