#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `图片/presets/*.png`（512×512 原图）缩成派端屏用的 96/160 预设图。

派端比赛屏（`orangepi/pi/pi_display.py`）按「形状|颜色」从 `presets/index.json`
找到基名，再用 `<基名>_96.png`（列内小图）和 `<基名>_160.png`（底栏大图）显示货物图。
现场 2026-10-31 起**不再实时截取相机画面**，这 6 张图就是屏上唯一的货物图，
所以要改图 / 加类别时：换掉 `图片/presets/<基名>.png` → 跑本脚本 → 跑
`deploy_pi_display.bat` 把 `orangepi/pi/presets/` 传上去。

用法（在 01-交付包 目录下，或用绝对路径）：

    .venv\\Scripts\\python.exe tools\\make_presets.py            # 重新生成 96/160
    .venv\\Scripts\\python.exe tools\\make_presets.py --check     # 只检查，不写文件

检查项：
  1) index.json 里每个基名都有对应的 `_96.png` / `_160.png`；
  2) `图片/presets/` 里每个 png 都至少被 index.json 用到一次（否则屏上永远不显示它）；
  3) 生成出来的尺寸与文件头一致（PNG IHDR）。
"""
import argparse
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)                      # 01-交付包
REPO = os.path.dirname(PKG)                      # 仓库根
DEFAULT_SRC = os.path.join(REPO, '图片', 'presets')
DEFAULT_OUT = os.path.join(PKG, 'orangepi', 'pi', 'presets')
# 屏是 1024×600：列内缩略图 f(52)=52px、底栏大图 f(56)=56px（2026-10-31 现场
# 截图发现 96/160 会被 Label 从左上角裁成"一角"，所以补上 52/56 两档）；
# 96/160 留给更大分辨率/窗口化调试。
SIZES = (52, 56, 96, 160)


def png_size(path):
    """读 PNG 的 IHDR 尺寸（不依赖 PIL，测试/部署环境都能跑）。"""
    with open(path, 'rb') as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        return None
    return struct.unpack('>II', head[16:24])


def load_index(out_dir):
    path = os.path.join(out_dir, 'index.json')
    if not os.path.exists(path):
        return {}, path
    with open(path, encoding='utf-8') as f:
        return dict(json.load(f) or {}), path


def main(argv=None):
    ap = argparse.ArgumentParser(description='生成派端屏预设图（96/160）')
    ap.add_argument('--src', default=DEFAULT_SRC, help='原图目录（默认 图片/presets）')
    ap.add_argument('--out', default=DEFAULT_OUT, help='输出目录（默认 orangepi/pi/presets）')
    ap.add_argument('--check', action='store_true', help='只检查，不写文件')
    args = ap.parse_args(argv)

    if not os.path.isdir(args.src):
        print('[x] 原图目录不存在：%s' % args.src)
        return 2
    os.makedirs(args.out, exist_ok=True)

    bases = sorted(os.path.splitext(n)[0] for n in os.listdir(args.src)
                   if n.lower().endswith('.png'))
    if not bases:
        print('[x] %s 里没有 png' % args.src)
        return 2

    idx, idx_path = load_index(args.out)
    used = set(idx.values())
    problems = []

    try:
        from PIL import Image
    except ImportError:
        Image = None
        if not args.check:
            print('[x] 需要 Pillow 才能缩放（.venv 里已装）：pip install pillow')
            return 2

    for base in bases:
        src = os.path.join(args.src, base + '.png')
        if base not in used:
            problems.append('原图 %s.png 没有任何 index.json 条目引用 → 屏上不会出现' % base)
        for size in SIZES:
            dst = os.path.join(args.out, '%s_%d.png' % (base, size))
            if args.check:
                if not os.path.exists(dst):
                    problems.append('缺文件：%s' % dst)
                    continue
                got = png_size(dst)
                if got != (size, size):
                    problems.append('%s 尺寸 %s（应为 %d×%d）' % (dst, got, size, size))
                continue
            im = Image.open(src).convert('RGB')
            im = im.resize((size, size), Image.LANCZOS)
            im.save(dst, 'PNG', optimize=True)
            print('[ok] %s' % os.path.relpath(dst, PKG))

    for key, base in sorted(idx.items()):
        if base not in bases:
            problems.append('index.json 的「%s」指向 %s，但原图 %s.png 不存在'
                            % (key, base, base))

    if args.check:
        if problems:
            print('[!] %d 个问题：' % len(problems))
            for p in problems:
                print('    - %s' % p)
            return 1
        print('[ok] index.json 与 %d 组预设图一致（未写文件）' % len(bases))
        return 0

    if problems:
        print('[!] 注意（不阻塞生成）：')
        for p in problems:
            print('    - %s' % p)
    print('完成：%s（index.json 由人工维护，%d 个类别键）' % (args.out, len(idx)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
