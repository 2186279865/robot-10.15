# 队友电脑部署指南（写给"队友那边的 AI"看的，照做即可）

> 目标：在一台**全新的 Windows 电脑**上，把本仓库的「视觉识别」跑起来，能
> ① 只读看机器人上的实时画面；② 用本地样本图离线跑识别、改算法、做回归。
> **本指南不涉及控制机器人**（那是 A 机的事）。全部步骤按顺序执行，每步都有验收判据。

---

## 0. 你的身份与铁律（先读，违反会破坏 A 机的调试）

1. 你只能改 `01-交付包/vision/` 下的代码与 `01-交付包/vision/config.json`。
2. **禁止**改 `01-交付包/host/`、`01-交付包/stm32/`、`01-交付包/main.py`、`common/wire.py`。
3. **禁止**连接串口桥端口 `5058`（那是控制通道，会和 A 机抢会话）。
4. 只允许连图传端口 `5057`（只读拉帧）。
5. 禁止 `git push` 到 `main`；一律开分支 `feat/vision-xxx`。
6. 识别结果文件 `vision/runtime/latest_targets.json` 的 **schema 不能变**，
   且 `motion_authorized` 必须恒为 `false`。
7. 每做完一个步骤，把**实际输出**贴给用户；失败就停下报告，不要自己猜着改。

---

## 1. 前置检查（全部满足才继续）

在 PowerShell 里执行：

```powershell
$PSVersionTable.PSVersion.Major           # 期望 >= 5
python --version                          # 期望 Python 3.10 / 3.11 / 3.12
where.exe python                          # 必须有一条 64 位安装路径
Get-PSDrive C | Select-Object Free        # C 盘剩余 > 8 GB
```

验收：
- `python --version` 输出 3.10–3.12；
- 若报 `python 不是内部或外部命令` → 让用户重装 Python 3.12（勾 `Add python.exe to PATH`），或给出 python 绝对路径后继续（下文用 `$PY` 表示）。

```powershell
$PY = (where.exe python | Select-Object -First 1)
& $PY -c "import sys; print(sys.version); print(sys.maxsize > 2**32)"
```
最后一行必须是 `True`（64 位）。

---

## 2. 拿到代码（二选一）

### 2a. 有网 / 有 GitHub 权限
```powershell
cd D:\
git clone git@github.com:2186279865/robot-10.15.git
cd D:\robot-10.15
```
（若 SSH 不通，用 `https://github.com/2186279865/robot-10.15.git`）

### 2b. 没网（A 机用 U 盘/局域网共享拷 `01-交付包` 整个目录）
```powershell
# 假设拷到 D:\robot-10.15\01-交付包
Test-Path D:\robot-10.15\01-交付包\main.py     # 必须 True
```

验收（两种方式都要过）：
```powershell
Test-Path D:\robot-10.15\01-交付包\vision\gui.py
Test-Path D:\robot-10.15\01-交付包\host\host_gui.py
Test-Path D:\robot-10.15\01-交付包\tools
```

---

## 3. 放识别模型（必须单独拷，不在 git 里）

目标路径：
```
D:\robot-10.15\01-交付包\vision\models\sam_b.pt
```

来源：A 机 `01-交付包\vision\models\sam_b.pt`（357 MB）。拷完校验：

```powershell
$m = Get-Item D:\robot-10.15\01-交付包\vision\models\sam_b.pt
"{0:N0} bytes" -f $m.Length      # 期望 375,042,383
```
- 小于 300 MB → 没拷完，重拷；
- 没有这个文件 → 停下找 A 机要。

---

## 4. 建 Python 环境（10–20 分钟，torch 比较大）

```powershell
cd D:\robot-10.15\01-交付包
cmd /c setup.bat
```

说明：`setup.bat` 会
1. 创建 `.venv`；
2. 升级 pip；
3. 用清华镜像 + 30 s 超时装 `requirements.txt`（numpy / opencv / pillow / torch / torchvision / ultralytics / pyserial）。

验收：
```powershell
Test-Path .venv\Scripts\python.exe
.\.venv\Scripts\python.exe -c "import cv2, numpy, torch, ultralytics, PIL; print('cv2', cv2.__version__); print('torch', torch.__version__); print('ultralytics', ultralytics.__version__)"
```

失败排查：
- `pip` 卡住超过 20 分钟 → Ctrl+C，改用：
  ```powershell
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt --timeout 30 -i https://pypi.tuna.tsinghua.edu.cn/simple
  ```
- 装 torch 时报磁盘不足 → 清理 C 盘或换 D 盘。
- **不需要 CUDA**：CPU 版 torch 也能跑识别（只是慢，1.5–4 s/帧）。

---

## 5. 自检（必须 19/19）

```powershell
.\.venv\Scripts\python.exe CHECK.py
```

验收：最后一行类似 `19/19 通过`。
失败时按它报的项修（缺文件 / 批处理编码 / 模型过小 / 入口文件缺失）。

再做一次"离线回归测试"（不需要机器人）：
```powershell
cmd /c selftest.bat /quick
```
验收：`RESULT: ALL CHECKS PASSED`。

---

## 6. 看实时画面（只读，不影响 A 机）

前提：机器人那台派（Orange Pi）已开机并连上同一个 WiFi，A 机会告诉你 IP（例如 `192.168.100.198`）。

```powershell
cd D:\robot-10.15\01-交付包
.\.venv\Scripts\python.exe tools\view_live.py --host 192.168.100.198 --cams 0,1 --fps 10 --width 960 --height 540
```

按键：`q` 退出、`s` 存图、`p` 暂停。窗口标题显示 `cam0 / cam1` 和实时 KB/fps。

若 `tools\view_live.py` 不存在（A 机还没给），用下面这段临时替代，存为 `tools\view_simple.py` 再运行：

```python
# tools/view_simple.py —— 最小只读看图（不连控制）
import sys, time
from pathlib import Path
PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "common"))
import cv2, numpy as np, wire

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.100.198"
CAMS = [0, 1]
while True:
    for cam in CAMS:
        try:
            s = wire.open_tcp(HOST, wire.IMG_PORT, timeout=4.0)
            s.sendall(wire.pack_request(cam, 960, 540, 75))
            magic = wire.recv_exact(s, 2)
            if magic == b"ER":
                h = wire.ERR_HDR.unpack(wire.recv_exact(s, wire.ERR_HDR_LEN))
                print("cam", cam, "派端报错", wire.recv_exact(s, h[2])); s.close(); continue
            rest = wire.recv_exact(s, wire.IMG_HDR_LEN - 2)
            head = wire.parse_image_header(magic + rest)
            body = wire.recv_exact(s, head["n"]); s.close()
            img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                cv2.imshow("cam%d" % cam, img)
        except Exception as exc:
            print("cam", cam, "失败:", exc)
    if (cv2.waitKey(30) & 0xFF) in (27, ord("q")):
        break
cv2.destroyAllWindows()
```

故障排查：
| 现象 | 原因 | 处理 |
|---|---|---|
| `取帧失败: timed out` | 派没开机 / 不在同一 WiFi / IP 变了 | 让 A 机确认派在线：`ping 192.168.100.198` |
| `拒绝访问` / WinError 10061 | 派端 agent 没跑 | 找 A 机 | 
| 画面卡顿 | 带宽不够 | 加 `--fps 6 --width 640 --height 360` |
| `cv2.imshow` 报错 | 没有桌面会话 | 必须在交互式桌面里运行，不能是服务/SSH 会话 |

---

## 7. 离线跑识别（改算法的核心：不用真机、不用联网）

### 7.1 准备样本
把 A 机给你的样本图放到：
```
D:\robot-10.15\01-交付包\vision\samples_frames\*.jpg
```
（要求：至少 10 张，包含散放 / 挨着 / 各类货物 / 不同光照）

### 7.2 离线分割预览（先用这个确认"看得见"）
把下面脚本存为 `tools\vision_offline.py`：

```python
# tools/vision_offline.py —— 离线跑分割，输出可视化 + JSON（不连机器人）
import argparse, json, sys
from pathlib import Path
PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "vision"))
import cv2
from auto_segment import AutoSegmenter
import json as _json

ap = argparse.ArgumentParser()
ap.add_argument("--frames", default=str(PKG / "vision" / "samples_frames"))
ap.add_argument("--out", default=str(PKG / "runtime" / "offline"))
ap.add_argument("--model", default=str(PKG / "vision" / "models" / "sam_b.pt"))
ap.add_argument("--limit", type=int, default=0)
args = ap.parse_args()

out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
seg = AutoSegmenter(args.model)
files = sorted(Path(args.frames).glob("*.jpg")) + sorted(Path(args.frames).glob("*.png"))
if args.limit:
    files = files[: args.limit]
print("样本数:", len(files))
rows = []
for f in files:
    img = cv2.imread(str(f))
    if img is None:
        print("跳过", f.name); continue
    res = seg.segment(img)          # 返回 dict 或 list/dict 结构：见 auto_segment.py
    items = res if isinstance(res, list) else res.get("items", [res])
    vis = img.copy()
    for it in items:
        c = it.get("center_px")
        if c:
            cv2.circle(vis, (int(c[0]), int(c[1])), 6, (0, 255, 0), 2)
    cv2.imwrite(str(out / (f.stem + "_mask.jpg")), vis)
    rows.append({"file": f.name, "n": len(items),
                 "items": [{"area_px": it.get("area_px"), "center_px": it.get("center_px"),
                            "confidence": it.get("confidence")} for it in items]})
    print(f.name, "->", len(items), "个目标")

(out / "summary.json").write_text(_json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("输出目录:", out)
```

运行：
```powershell
.\.venv\Scripts\python.exe tools\vision_offline.py --frames vision\samples_frames --out runtime\offline
```

> **接口已核对（以 `vision/auto_segment.py` 实际代码为准）：**
> - 构造：`AutoSegmenter(model_path=None)`（`None` 时自动找 `models/sam_b.pt`）；
> - 调用：`segment(frame, background=None, cfg=None)`，`frame` 是 BGR ndarray；
> - 返回：**`list[dict]`**，每个 dict 含
>   `mask` / `contour` / `bbox=[x,y,w,h]` / `center_px=[x,y]` /
>   `center_tray=[u,v]`（按该帧宽高归一化）/ `area_px` / `confidence`。
> - 离线预览用**原图**跑，没做四点单应（700×700）；所以这里的 `center_tray` 基准和真机不同。
>   要验证「真机 u/v 是否一致」，必须在 700×700 校正图上跑（视觉页里就是这么做的）。

---

## 8. 改视觉算法的规矩

### 8.1 可改的文件与参数
| 目标 | 文件 | 说明 |
|---|---|---|
| 调阈值/颜色/类别 | `vision/config.json` | `min_area_px`、`max_area_px`、`frames_for_vote`、`color_names`、`class_map`、`sam_model` |
| 改分类规则 | `vision/classify.py` | 输出 `class_key`（形如 `长方体/长方形\|绿`），上位机靠它分盒 |
| 换分割后端 | `vision/auto_segment.py` | 必须保持 `segment()` 输出契约不变 |
| 融合阈值 | `vision/fusion.py` | `MAX_DIST=0.18`（700 图约 126 px） |

### 8.2 硬约束（改完全部要满足）
1. `latest_targets.json` 的 schema 不变：`ts` + `targets[]`，每项含 `class_name`、`class_key`、`center_tray=[u,v]`（0–1 归一化）、`confidence`、`area_px`；
2. `motion_authorized` 永远是 `false`；
3. `host/vision_import.py` 不改也能读你的输出；
4. 分类结果必须稳定（同一堆货连拍 5 次，类别不能跳）。

### 8.3 验收
```powershell
# 1) 离线样本：分类正确率 >= 90%，u/v 抖动 < 2 像素
# 2) 交接文件能被上位机消费（在 01-交付包\host 下跑）
.\.venv\Scripts\python.exe -m unittest discover -s tests -t tests
# 期望：220 tests OK
```

### 8.4 Git 流程
```powershell
git checkout -b feat/vision-<你的主题>
git add 01-交付包/vision
git commit -m "feat(vision): <一句话>"
git push origin feat/vision-<你的主题>
```
然后把分支名告诉 A 机，由 A 机 review + 合并 + 真机验证。

---

## 9. 一键验收清单（部署完成后跑一遍，把结果贴给用户）

```powershell
cd D:\robot-10.15\01-交付包
Test-Path .venv\Scripts\python.exe
(Get-Item vision\models\sam_b.pt).Length
.\.venv\Scripts\python.exe CHECK.py
cmd /c selftest.bat /quick
.\.venv\Scripts\python.exe tools\view_live.py --host 192.168.100.198 --headless --seconds 5
```

全部通过的标准：
- `.venv` 存在；
- 模型 375,042,383 字节；
- `CHECK.py` 19/19；
- `selftest /quick` → `ALL CHECKS PASSED`；
- `view_live --headless` 输出里 cam0/cam1 都有帧率（说明画面通了）。

---

## 10. 严禁事项（再说一遍，别踩）

- ❌ 连 `5058`、点任何"运动/使能/停止"按钮；
- ❌ 改 `host/`、`stm32/`、`main.py`、`common/wire.py`；
- ❌ 改 `latest_targets.json` 的字段名/结构；
- ❌ 把 `motion_authorized` 改成 true；
- ❌ `git push origin main`；
- ❌ 把 357 MB 的模型提交进 git（`.gitignore` 已排除，别用 `-f`）。
