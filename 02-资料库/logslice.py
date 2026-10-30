"""从现场日志里按时间窗抽关键行（滤掉 tele/ping），方便看动作节拍。"""
import io
import re
import sys

path = sys.argv[1]
start_ts = sys.argv[2] if len(sys.argv) > 2 else None
end_ts = sys.argv[3] if len(sys.argv) > 3 else None
skip = re.compile(r'\btele\b|ping ok|#\d+,ok,ms|#\d+,ok,name|^\[[^\]]*\] #')

out = []
for line in io.open(path, encoding='utf-8', errors='replace'):
    line = line.rstrip('\n')
    m = re.match(r'^\[(\d\d:\d\d:\d\d)\]', line)
    if not m:
        continue
    ts = m.group(1)
    if start_ts and ts < start_ts:
        continue
    if end_ts and ts > end_ts:
        break
    if skip.search(line):
        continue
    out.append(line)
print('\n'.join(out))
print('---- 共 %d 行 ----' % len(out))
