import io
p = r'.survey\journey_smoke.py'
t = io.open(p, encoding='utf-8').read()
old = """sent.clear()
with mock.patch.object(host_gui.messagebox, 'askyesno', return_value=True):
    app.journey_return()
check('进入返回态', app._journey_running)
check('返回跑完', wait_until(lambda: not app._journey_running, 20.0))
pump(0.4)

moves = [(a[0], a[1], a[4]) for c, a in sent if c == 'motor_pos']
expect = [(1, 0, moves[0][2] if moves else 0), (3, 1, 533), (5, 0, 1600), (4, 1, 3200)]
"""
new = """before = list(app.journey['steps'])
expect = [(s['axis'], 1 - s['dir'], s['pulses']) for s in reversed(before)]
sent.clear()
with mock.patch.object(host_gui.messagebox, 'askyesno', return_value=True):
    app.journey_return()
check('进入返回态', app._journey_running)
check('返回跑完', wait_until(lambda: not app._journey_running, 25.0))
pump(0.4)

moves = [(a[0], a[1], a[4]) for c, a in sent if c == 'motor_pos']
"""
if old not in t:
    raise SystemExit('anchor missing')
t = t.replace(old, new, 1)
t = t.replace("check('每段前先使能', len([1 for c, a in sent if c == 'motor_enable']) == 4)",
              "check('每段前先使能', len([1 for c, a in sent if c == 'motor_enable']) == len(expect))")
io.open(p, 'w', encoding='utf-8').write(t)
print('smoke patched')
