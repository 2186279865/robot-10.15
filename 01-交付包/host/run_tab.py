# -*- coding: utf-8 -*-
"""运行台：正式比赛用的操作页（第一页签）。

- 左：主摄实时画面（可 0/90/180/270 旋转显示）+ 识别框/编号叠加
- 右：上面「运行状态」、下面「识别结果」两个日志窗
- 底部大按钮：「▶ 一键启动」= 连续抓取（抓到托盘空为止、抓完落货）
              「⏏ 一键回 P0」= Z 抬顶 → X/Y 回 P1 → Z 降底 → Y 退 0
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageDraw, ImageTk

import auto_grab as AG
import prep_pose as PP

CANVAS_W, CANVAS_H = 640, 360
IMG_W, IMG_H = 1920.0, 1080.0          # 主摄原始分辨率（画框用）


class RunTab(ttk.Frame):
    def __init__(self, parent, app, **kw):
        super().__init__(parent, **kw)
        self.app = app
        self.rot = 0
        self._photo = None
        self._tick_id = None
        self._last_sig = None
        self._busy = False
        self._build()
        self._schedule()
        import os as _os

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(side="top", fill="x", padx=8, pady=(8, 4))
        self.btn_start = ttk.Button(bar, text="▶ 一键启动（抓到空为止）",
                                    command=self.on_start, width=26)
        self.btn_start.pack(side="left")
        self.btn_home = ttk.Button(bar, text="⏏ 一键回 P0",
                                   command=self.on_home, width=14)
        self.btn_home.pack(side="left", padx=(8, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(bar, text="↺ 左旋90", width=9,
                   command=lambda: self.rotate(-90)).pack(side="left")
        ttk.Button(bar, text="↻ 右旋90", width=9,
                   command=lambda: self.rotate(90)).pack(side="left", padx=(6, 0))
        self.lb_rot = ttk.Label(bar, text="0°")
        self.lb_rot.pack(side="left", padx=(6, 0))
        self.lb_fps = ttk.Label(bar, text="")
        self.lb_fps.pack(side="right")

        body = ttk.Frame(self)
        body.pack(side="top", fill="both", expand=True)

        # Tk 的 pack 按顺序分配空间：先把底部日志区 pack 好，再 pack 可扩展的画布
        # 右侧固定宽度日志栏（先 pack，才能保住宽度），左侧画布再 expand
        right = ttk.Frame(body, width=430)
        right.pack(side="right", fill="y", padx=(4, 8), pady=(0, 8))
        right.pack_propagate(False)

        ttk.Label(right, text="运行状态").pack(anchor="w")
        self.txt_state = tk.Text(right, height=8, wrap="none",
                                 bg="#0b0d10", fg="#d7dde5", font=("Consolas", 10))
        self.txt_state.pack(fill="both", expand=True)

        ttk.Label(right, text="识别结果").pack(anchor="w", pady=(6, 0))
        self.txt_items = tk.Text(right, height=10, wrap="none",
                                 bg="#0b0d10", fg="#b8e0a8", font=("Consolas", 10))
        self.txt_items.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(body, width=CANVAS_W, height=CANVAS_H,
                                bg="#111", highlightthickness=1)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=(0, 8))

        self.status("运行台就绪：连上派端后点「▶ 一键启动」")

    # ------------------------------------------------------------- logging
    def _append(self, box: tk.Text, text: str, limit: int = 500) -> None:
        stamp = time.strftime("%H:%M:%S")
        try:
            box.insert("end", "[%s] %s\n" % (stamp, text))
            if int(box.index("end-1c").split(".")[0]) > limit:
                box.delete("1.0", "200.0")
            box.see("end")
        except Exception:                                  # noqa: BLE001
            pass

    def status(self, text: str) -> None:
        self._append(self.txt_state, text)

    def item_log(self, text: str) -> None:
        self._append(self.txt_items, text)

    # ------------------------------------------------------------- actions
    def on_start(self) -> None:
        if self._busy:
            self.status("正在执行，先等它结束（或点顶部「停止全部」）")
            return
        client = getattr(self.app, "client", None)
        if client is None or not getattr(client, "online", False):
            messagebox.showwarning("未连接", "先连上派端（运动控制页或顶栏「连接」）再点一键启动。")
            return
        if not messagebox.askyesno(
                "一键启动",
                "开始连续抓取：\n\n"
                "• 抓到托盘里没有物块为止（件数不限）\n"
                "• 每件都落货（P1 降到底 → 松爪）\n"
                "• 失败会自动重识别重试一次，仍失败则停\n\n"
                "手离开机械臂，随时可点顶部「停止全部」。继续？"):
            return
        self._busy = True
        self.status("▶ 一键启动：连续抓取（抓到空为止）")
        try:
            self.app.calib.on_auto_many(count=0, drop=True)
        except Exception as exc:                          # noqa: BLE001
            self._busy = False
            self.status("启动失败：%s" % exc)
            messagebox.showerror("一键启动失败", str(exc))

    def on_home(self) -> None:
        client = getattr(self.app, "client", None)
        if client is None or not getattr(client, "online", False):
            messagebox.showwarning("未连接", "先连上派端再回 P0。")
            return
        if self._busy:
            self.status("正在执行，先等它结束")
            return
        if not messagebox.askyesno(
                "回 P0",
                "按这条路径回初始位：\n\nZ 抬顶 → X/Y 回 P1 → Z 降底 → Y 退 0\n\n"
                "（已经在 P1 时 X/Y 那步会自动跳过）继续？"):
            return
        self._busy = True
        self.status("⏏ 回 P0：Z 抬顶 → 回 P1 → Z 降底 → 退 0")

        def work():
            log = lambda m: self.app.root.after(0, lambda: self.status(m))
            try:
                Z, X, Y = AG.AXIS_Z, 4, 5
                rpm_z = AG.GOTO_RPM_BY_AXIS.get(Z, 1500) if hasattr(AG, "GOTO_RPM_BY_AXIS") else 1500
                PP.move_abs(client, {Z: AG.Z_UP}, [Z], rpm=rpm_z, log=log)
                PP.move_abs(client, {X: 0, Y: AG.P1_Y}, [X, Y], log=log)
                PP.move_abs(client, {Z: AG.Z_DOWN}, [Z], rpm=rpm_z, log=log)
                PP.move_abs(client, {Y: AG.P0_Y}, [Y], log=log)
                log("已回到 P0（初始位）")
            except Exception as exc:                      # noqa: BLE001
                log("回 P0 失败：%s" % exc)
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()

    def rotate(self, delta: int) -> None:
        self.rot = (self.rot + delta) % 360
        self.lb_rot.config(text="%d°" % self.rot)

    # -------------------------------------------------------------- render
    def _schedule(self) -> None:
        self._tick_id = self.after(120, self._tick)

    def _frames(self):
        vis = getattr(self.app, "vision", None) or getattr(self.app, "vision_app", None)
        if vis is None or getattr(vis, "a", None) is None:
            return None, vis
        try:
            return vis.a.get(), vis
        except Exception:                                 # noqa: BLE001
            return None, vis

    def _tick(self) -> None:
        try:
            frame, vis = self._frames()
            if frame is None:
                self.canvas.delete("all")
                self.canvas.create_text(CANVAS_W // 2, CANVAS_H // 2, fill="#667",
                                        text="主摄未打开（先到「视觉」页点「打开双画面」）")
            else:
                self._draw(frame, vis)
        except Exception:                                 # noqa: BLE001
            pass
        finally:
            self._schedule()

    def _draw(self, bgr, vis) -> None:
        img = Image.fromarray(bgr[:, :, ::-1])            # BGR→RGB
        if self.rot:
            img = img.rotate(-self.rot, expand=True)      # 顺时针为正
        w, h = img.size
        boxes = []
        for idx, it in enumerate(getattr(vis, "fused", []) or [], start=1):
            top = it.get("top") or {}
            bb = top.get("bbox") or top.get("bbox_orig") or it.get("bbox")
            if not bb or len(bb) != 4:
                continue
            boxes.append((idx, bb, "%s%s" % (
                (it.get("class_name") or it.get("shape") or "待登记"),
                (" " + it["color"]) if it.get("color") else "")))
        draw = ImageDraw.Draw(img)
        for idx, bb, label in boxes:
            if self.rot in (90, 270):
                k = w / IMG_H
                x = float(bb[1]) * k
                y = (IMG_W - float(bb[0]) - float(bb[2])) * k
                bw, bh = float(bb[3]) * k, float(bb[2]) * k
            else:
                k = w / IMG_W
                x, y = float(bb[0]) * k, float(bb[1]) * k
                bw, bh = float(bb[2]) * k, float(bb[3]) * k
            draw.rectangle([x, y, x + bw, y + bh], outline=(80, 220, 120), width=3)
            draw.text((x + 4, max(0, y - 14)), "#%d %s" % (idx, label), fill=(255, 235, 120))
        sig = tuple((i, label) for i, _, label in boxes)
        if sig != self._last_sig:
            self._last_sig = sig
            if not sig:
                self.item_log("（画面里没有识别到目标）")
            for i, label in sig:
                self.item_log("#%d %s" % (i, label))
        scale = min(CANVAS_W / float(img.size[0]), CANVAS_H / float(img.size[1]))
        disp = img.resize((max(1, int(img.size[0] * scale)),
                           max(1, int(img.size[1] * scale))), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.create_image(CANVAS_W // 2, CANVAS_H // 2,
                                 image=self._photo, anchor="center")
        try:
            if vis is not None and getattr(vis, "a", None) is not None:
                self.lb_fps.config(text="顶视 %.1f fps" % vis.a.fps())
        except Exception:                                 # noqa: BLE001
            pass
