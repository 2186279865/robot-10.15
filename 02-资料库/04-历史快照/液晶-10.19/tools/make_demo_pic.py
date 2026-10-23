#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_demo_pic.py —— 画一张 128x160 的示例图片（日落风景），并生成 MY/pic.h

为什么自己画：这样 10.19 工程里不用带别人的照片（避免版权问题），
而且渐变/纯色块多，正好用来验证屏幕的色彩和伽马。

想换成自己的照片：直接用
    python tools\\img2c.py 你的图片.jpg MY\\pic.h
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from img2c import cover_crop, to_rgb565_bytes, emit_header

W, H = 128, 160
SS = 4                       # 先用 4 倍分辨率画，再缩小，边缘更细腻
BW, BH = W * SS, H * SS


def lerp(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def draw_sunset():
    img = Image.new("RGB", (BW, BH), (0, 0, 0))
    d = ImageDraw.Draw(img)

    horizon = int(BH * 0.60)
    # 1) 天空渐变：深蓝夜空 -> 橙红晚霞
    sky_top = (16, 22, 74)
    sky_mid = (110, 60, 130)
    sky_low = (255, 132, 60)
    for y in range(horizon):
        t = y / float(horizon)
        if t < 0.55:
            c = lerp(sky_top, sky_mid, t / 0.55)
        else:
            c = lerp(sky_mid, sky_low, (t - 0.55) / 0.45)
        d.line([(0, y), (BW, y)], fill=c)

    # 2) 太阳光晕（几层半透明圆）
    cx, cy = BW // 2, int(horizon - BH * 0.03)
    glow = Image.new("RGB", (BW, BH), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i, r in enumerate(range(int(BH * 0.26), int(BH * 0.05), -8)):
        a = 0.16
        col = tuple(int(v) for v in lerp((255, 170, 70), (255, 246, 200), min(1.0, i / 6.0)))
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    glow = glow.filter(ImageFilter.GaussianBlur(BH // 40))
    img = Image.blend(img, Image.blend(img, glow, 1.0), 0.0) if False else img
    img = Image.composite(glow, img, glow.convert("L").point(lambda v: min(255, v * 2)))
    d = ImageDraw.Draw(img)

    # 3) 太阳本体
    sr = int(BH * 0.075)
    d.ellipse([cx - sr, cy - sr, cx + sr, cy + sr], fill=(255, 250, 215))

    # 4) 远山（紫色）与近山（深蓝紫）
    far = [(0, int(horizon * 0.98))]
    far += [(-10, int(BH * 0.44)), (int(BW * 0.18), int(BH * 0.50)), (int(BW * 0.34), int(BH * 0.42)),
            (int(BW * 0.52), int(BH * 0.50)), (int(BW * 0.70), int(BH * 0.43)), (int(BW * 0.86), int(BH * 0.49)),
            (BW + 10, int(BH * 0.45))]
    far += [(BW, int(horizon)), (0, int(horizon))]
    d.polygon(far, fill=(74, 52, 96))

    near = [(-10, int(BH * 0.62)), (int(BW * 0.12), int(BH * 0.545)), (int(BW * 0.30), int(BH * 0.615)),
            (int(BW * 0.46), int(BH * 0.55)), (int(BW * 0.66), int(BH * 0.63)), (int(BW * 0.84), int(BH * 0.565)),
            (BW + 10, int(BH * 0.61)), (BW + 10, int(horizon) + int(BH * 0.02)),
            (-10, int(horizon) + int(BH * 0.02))]
    d.polygon(near, fill=(38, 30, 62))

    # 5) 水面 + 太阳倒影（用加法混合，水面上叠加一层暖色亮带）
    water_top = int(horizon) + int(BH * 0.02)
    for y in range(water_top, BH):
        t = (y - water_top) / float(max(1, BH - water_top))
        c = lerp((46, 34, 84), (8, 10, 34), t)
        d.line([(0, y), (BW, y)], fill=c)
    d.polygon([(-10, water_top), (BW + 10, water_top), (BW + 10, water_top + int(BH * 0.02)),
               (-10, water_top + int(BH * 0.02))], fill=(90, 60, 110))

    refl = Image.new("RGB", (BW, BH), (0, 0, 0))
    rd = ImageDraw.Draw(refl)
    rw = int(sr * 1.6)
    span = int(BH * 0.16)
    for k in range(0, span, 5):
        y = water_top + k
        wdt = int(rw * (1 - k / float(span)))
        col = lerp((255, 205, 130), (120, 90, 150), k / float(span))
        rd.line([(cx - wdt, y), (cx + wdt, y)], fill=tuple(int(v * 0.55) for v in col))
    refl = refl.filter(ImageFilter.GaussianBlur(BH // 55))
    from PIL import ImageChops
    img = ImageChops.add(img, refl)
    d = ImageDraw.Draw(img)

    # 6) 一只小船的剪影（有趣一点，也让纯黑更明显）
    bx, by = int(BW * 0.72), int(water_top + BH * 0.05)
    d.polygon([(bx - int(BW * 0.055), by), (bx + int(BW * 0.055), by),
               (bx + int(BW * 0.03), by + int(BH * 0.018)), (bx - int(BW * 0.03), by + int(BH * 0.018))],
              fill=(230, 220, 235))
    d.line([(bx, by - int(BH * 0.002)), (bx, by - int(BH * 0.055))], fill=(230, 220, 235), width=2)
    d.polygon([(bx, by - int(BH * 0.052)), (bx + int(BW * 0.035), by - int(BH * 0.020)),
               (bx, by - int(BH * 0.012))], fill=(240, 232, 245))

    return img


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_h = os.path.join(root, "MY", "pic.h")
    small = cover_crop(draw_sunset(), W, H)
    data = to_rgb565_bytes(small)
    emit_header(out_h, "gImage_pic", data, W, H, "demo_sunset (tools/make_demo_pic.py)")
    small.save(os.path.join(root, "tools", "demo_preview.png"))
    print("已生成 %s" % out_h)
    print("预览图 %s" % os.path.join(root, "tools", "demo_preview.png"))


if __name__ == "__main__":
    main()
