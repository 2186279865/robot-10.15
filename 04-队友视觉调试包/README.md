# 队友视觉调试包（B 机专用）

> 这个文件夹是给**第二台电脑**（队友那台）用的。
> 目标：让队友（和他的 AI）在**不影响主控机**的前提下，看画面、跑识别、改视觉算法。

---

## 一句话分工

| 机器 | 角色 | 能做什么 | 绝对不能做什么 |
|---|---|---|---|
| **A 机**（主控，我的电脑） | 唯一控制方 | 连机器人、发运动命令、真机抓放 | — |
| **B 机**（队友，本包用户） | 只读感知方 | 拉相机画面、离线跑识别、改 `vision/` 代码、提交分支 | **不许连 5058**（会抢运动控制权）、不许 push `main` |

两套端口要分清：

- **5057 图传（只读，安全）**：随便连，只拉 JPEG。
- **5058 串口桥（控制，危险）**：一旦连上，固件只会把回包给**当前占用会话**的客户端；A 机正在控制时 B 机连它会互相抢，表现为"命令超时 / 机器人不动"。

---

## 三步跑起来

```bat
:: 0) 模型走 Git LFS（358 MB），先装 lfs，否则 clone 下来是个 134 字节的指针文件
git lfs install

:: 1) 拿到代码（在仓库根目录）
git clone git@github.com:2186279865/robot-10.15.git
cd robot-10.15

:: 2) 一键自检（先看自己缺什么）
04-队友视觉调试包\启动自检.bat

:: 3) 按提示装依赖后重新自检，全绿就可以干活
```

自检会逐项告诉你：Python 版本对不对、依赖缺什么、模型在不在（`sam_b.pt` 在仓库里、走 **Git LFS**，clone 完应自动是 **375,042,383 字节**；如果只有 134 字节说明 LFS 没拉下来，跑 `git lfs install && git lfs pull`）、`AutoSegmenter` 接口对不对、样例图能不能跑出识别结果。

---

## 包里有什么

```
04-队友视觉调试包/
├─ README.md                  ← 你正在看的这一页（入口）
├─ 启动自检.bat               ← 双击 = 跑 check_teammate.py
├─ 指南-部署与视觉调试.md      ← 详细指南（10 节，给 AI 看的完整版）
├─ tools/
│  ├─ check_teammate.py       ← 一键自检（19 项）
│  ├─ view_live.py            ← 只读看实时画面（连 5057，安全）
│  └─ vision_offline.py       ← 不连机器人，对一张图跑分割+预览
└─ samples/
   ├─ README.md               ← 样本说明
   ├─ real_cam0.jpg           ← 旧机顶视（俯视托盘）
   ├─ real_cam1.jpg           ← 旧机侧视
   ├─ real_new0.jpg           ← 新机侧视
   └─ real_new1.jpg           ← 新机顶视（俯视托盘）
```

**真正的代码不在这里**：视觉算法在 `01-交付包/vision/`，上位机在 `01-交付包/host/`，协议在 `01-交付包/docs/`。本包只是"入口 + 工具 + 指南"。

---

## 仓库里有什么 / 没有什么（“全部东西”的边界）

**已经在 GitHub 上、clone 就有**：

| 内容 | 说明 |
|---|---|
| `01-交付包/` | 固件源码 + hex、派端服务、视觉算法 + **SAM 权重（Git LFS，358 MB）**、上位机 GUI、脚本、文档 |
| `02-资料库/` | 赛题原文、硬件资料、往届参考、历史快照、调研报告、交接教程、项目日志、全部真机截图 |
| `03-实验台/` | 实验代码（Qwen-VLM 的 py/ps1/md、SAM 上派实测脚本、网络化原型） |
| `04-队友视觉调试包/` | 本包 |

**没有在 GitHub 上（体积原因，和比赛无关）**：

| 内容 | 大小 | 为什么要不到 |
|---|---|---|
| `03-实验台/Qwen-VLM/.venv`、`downloads/`、`models/`、`cache/` | ~27 GB | Ollama 运行时 + qwen2.5vl 模型，属于实验，可按 `Qwen-VLM/setup.ps1` 自己下 |
| `01-交付包/.venv` | ~6 GB | 虚拟环境，`01-交付包/setup.bat` 自己装 |
| `3d/` | 17 MB | 3D 打印件 STL，和视觉调试无关 |
| `Qwen-VLM/data/`、`samples/` | 10 MB | 实验拍的照片，Qwen 目录自己的 .gitignore 排除 |

> 需要 `01-交付包` 的完整可离线版本（含 .venv 与模型），让 A 机打一个 zip 直传即可。

---

## 常用命令（复制即用）

```bat
:: 自检
04-队友视觉调试包\启动自检.bat

:: 看实时画面（把 IP 换成 A 机告诉你的派 IP；只读，安全）
cd 01-交付包
..\01-交付包\.venv\Scripts\python.exe ..\04-队友视觉调试包\tools\view_live.py --host 192.168.100.198

:: 对一张样例图跑分割（不连机器人）
..\01-交付包\.venv\Scripts\python.exe ..\04-队友视觉调试包\tools\vision_offline.py --image ..\04-队友视觉调试包\samples\real_cam0.jpg
```

> 如果你还没建虚拟环境：先 `cd 01-交付包` → `setup.bat`（约 10–20 分钟，CPU 版 torch 就够）。详见 `指南-部署与视觉调试.md` 第 4 节。

---

## 改代码的规矩（就五条）

1. **只改 `01-交付包/vision/`**（要动 `host/` 先问 A 机）。
2. **输出契约不能变**：`AutoSegmenter.segment(frame, background=None, cfg=None)` 必须返回 `list[dict]`，每项含
   `mask / contour / bbox=[x,y,w,h] / center_px=[x,y] / center_tray=[u,v] / area_px / confidence`。
3. **`latest_targets.json` 的 schema 不能改**（`01-交付包/vision/README.md` 有定义），A 机靠它抓取。
4. **开分支干活**：`git checkout -b feat/vision-你的名字`，本地提交、本地测试，**不要 push main**。
5. **严禁连 5058、严禁发运动命令、严禁改动/删除 `01-交付包/vision/models/sam_b.pt`**（它已用 Git LFS 入库，别用普通 `git add` 覆盖）。

---

## 卡住了看哪里

| 现象 | 先看 |
|---|---|
| 自检报缺库 | 指南第 4 节，跑 `setup.bat` |
| 自检报模型不对 | 指南第 3 节，字节数必须 **375,042,383**（若是 134 字节 = LFS 没拉下来，跑 `git lfs pull`） |
| `view_live.py` 连不上 | 派没开机 / 不在同一 WiFi / 防火墙；指南第 6 节有 30 行备用脚本 |
| 识别跑不通 | 指南第 7 节 `vision_offline.py` 的报错原文发给 A 机 |
| 不知道能不能改某个文件 | 指南第 8 节「可改 / 别动」表 |

完整的排错与验收清单在 **`指南-部署与视觉调试.md`**。
