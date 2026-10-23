import io
p = 'host_gui.py'
t = io.open(p, encoding='utf-8').read()

start = t.index('            elif cmd == "motor_pos":')
end = t.index('    def _approach_flush_speed(self, addr) -> None:')
new_motion = '''            elif cmd == "motor_pos":
                mode = str(args[5]) if len(args) > 5 else "rel"
                pulses = abs(int(args[4]))
                direction = int(args[1])
                rpm = int(args[2])
                acc = int(args[3])
                negative = bool(direction)
                self.soft_pos[addr] += (-pulses if negative else pulses)
                self.pos_known[addr] = True
                self._last_motion_s = time.monotonic()
                if mode != "rel":
                    # 固件把「绝对位置」也按相对移动执行（control_apply_position(..., absolute=0)），
                    # 所以实际走的就是 pulses；记账必须按真实动作，不能按目标位置差。
                    self.note("%s『绝对走到此』：固件按相对执行，实际走 %d 脉冲（%s向）"
                              % (AXIS_LABELS.get(addr, addr), pulses, "负" if negative else "正"))
                self._journey_add(addr, direction, rpm, acc, pulses)
                if self._approach_recording and not self._approach_running and addr in AR.AXES:
                    self._approach_record(addr, direction, rpm, acc, pulses)
            elif cmd == "motor_speed":
                # 「常用走 / 连续走」：没有终点，记下开始时间，等停下（motor_stop / stop_all）再折算
                self.pos_known[addr] = False
                self._last_motion_s = time.monotonic()
                self._speed_t0[addr] = (time.monotonic(), int(args[1]), int(args[2]), int(args[3]))
            elif cmd == "motor_stop":
                self._approach_flush_speed(addr)
                self._last_motion_s = time.monotonic()
        except (IndexError, TypeError, ValueError):
            pass

'''
t = t[:start] + new_motion + t[end:]

old_flush_start = t.index('    def _approach_flush_speed(self, addr) -> None:')
old_flush_end = t.index('    def send_gripper(self, action: str) -> None:')
new_flush = '''    def _approach_flush_speed(self, addr) -> None:
        """把「速度模式走了多久」折算成一段（addr=None 表示所有轴一起收尾）。"""
        if not self._speed_t0:
            return
        targets = list(self._speed_t0) if addr is None else [addr]
        for axis in targets:
            started = self._speed_t0.pop(axis, None)
            if started is None:
                continue
            t0, direction, rpm, acc = started
            pulses = int(max(1, rpm) * AR.PULSES_PER_REV / 60.0 * (time.monotonic() - t0))
            if pulses <= 0:
                continue
            self._journey_add(axis, direction, rpm, acc, pulses)
            if self._approach_recording and not self._approach_running and axis in AR.AXES:
                self._approach_record(axis, direction, rpm, acc, pulses)

'''
t = t[:old_flush_start] + new_flush + t[old_flush_end:]
io.open(p, 'w', encoding='utf-8', newline='\n').write(t)
print('patched motion+flush ok')
