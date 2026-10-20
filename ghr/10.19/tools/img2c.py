#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
img2c.py  ——  把任意图片转成 ST7735S 液晶屏能直接显示的 C 数组

用法（在工程根目录 10.19 下执行）：
    python tools\\img2c.py 图片路径 [输出路径] [--w 128] [--h 160] [--name gImage_pic]

例如：
    python tools\\img2c.py D:\\photo\\cat.jpg MY\\pic.h
    python tools\\img2c.py cat.jpg MY\\pic.h --w 160 --h 128      # 横屏时

规则：
    * 图片会被“等比缩放 + 居中裁剪”到 W x H，不会拉伸变形；
    * 每个像素转成 RGB565（16 位色），高字节在前，正好对应
      LCD_ShowPicture() 里 LCD_WR_DATA8(pic[k*2]); LCD_WR_DATA8(pic[k*2+1]);
    * 生成的 .h 里是一个 const unsigned char 数组，放在 Flash 里，不占 RAM。

对应显示代码（main.c）：
    #include "pic.h"
    LCD_ShowPicture(0, 0, LCD_W, LCD_H, gImage_pic);   // 左上角开始，满屏显示
"""
import argparse
import os
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("缺少 Pillow，请先执行： pip install pillow")


def cover_crop(img, w, h):
    """等比缩放后居中裁剪到 w x h。"""
    sw, sh = img.size
    scale = max(float(w) / sw, float(h) / sh)
    nw, nh = max(w, int(sw * scale + 0.5)), max(h, int(sh * scale + 0.5))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def to_rgb565_bytes(img):
    """RGB888 -> RGB565，大端（高字节在前），返回 bytes。"""
    w, h = img.size
    px = img.convert("RGB").load()
    data = bytearray(w * h * 2)
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            c = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            data[i] = (c >> 8) & 0xFF
            data[i + 1] = c & 0xFF
            i += 2
    return bytes(data)


def emit_header(path, name, data, w, h, src_desc):
    lines = []
    lines.append("#ifndef __PIC_H")
    lines.append("#define __PIC_H")
    lines.append("")
    lines.append("/* 由 tools/img2c.py 自动生成，请勿手工修改")
    lines.append(" * 源图片 : %s" % src_desc)
    lines.append(" * 尺寸   : %d x %d 像素" % (w, h))
    lines.append(" * 格式   : RGB565，每像素 2 字节，高字节在前")
    lines.append(" * 显存   : %d 字节（放在 Flash，不占 RAM）" % len(data))
    lines.append(" * 用法   : LCD_ShowPicture(0, 0, %d, %d, %s);" % (w, h, name))
    lines.append(" */")
    lines.append("")
    lines.append("const unsigned char %s[%d] = {" % (name, len(data)))
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        lines.append("    " + " ".join("0X%02X," % b for b in chunk))
    lines.append("};")
    lines.append("")
    lines.append("#endif")
    lines.append("")
    text = "\r\n".join(lines)
    # 工程里的源文件都是 GBK（Keil 中文环境），这里也写 GBK，注释不会乱码
    with open(path, "w", encoding="gbk", errors="replace", newline="") as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="输入图片（jpg/png/bmp/gif 等）")
    ap.add_argument("output", nargs="?", default="MY/pic.h", help="输出 .h（默认 MY/pic.h）")
    ap.add_argument("--w", type=int, default=128)
    ap.add_argument("--h", type=int, default=160)
    ap.add_argument("--name", default="gImage_pic")
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        sys.exit("找不到图片：%s" % args.image)

    img = Image.open(args.image)
    img = cover_crop(img, args.w, args.h)
    data = to_rgb565_bytes(img)
    emit_header(args.output, args.name, data, args.w, args.h, os.path.basename(args.image))
    print("已生成 %s ： %d x %d，%d 字节" % (args.output, args.w, args.h, len(data)))


if __name__ == "__main__":
    main()
