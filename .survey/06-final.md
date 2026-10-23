# 06 · final/ —— 一体化交付快照调研

> 调研对象：`final/`（393.7 MB，170+ 文件）。
> 方法：目录枚举 → 同名文件 MD5 逐字节比对 → 对真正不同的文件逐行读源码/文档；未做递归 diff 大目录、未执行 git 命令、未运行任何程序、未读 `*.pt`（只取大小）。
> 一句话定位：**final 是 2026-10-21 01:06 冻结的「视觉（new_eye）+ 四轴控制（ghr/10.15/上位机）」一体化自包含交付包**。它不是纯拷贝——视觉 GUI 做了嵌入化改造、上位机新增了「开机回基准 / X/Y 标定页」，这些改动**只存在于 final**；但 10-21 之后根目录继续演进（无线组网 + 固件重建），所以 final 是**交付快照，不是以后的主线**。

---

## 0. 调研边界与对初始假设的修正

| 初始假设 | 实际情况 |
| --- | --- |
| 存在 `final/config.json` | **不存在**。所有配置在 `final/new_eye/config.json`；`final/main.py:8` 的“用最终 config.json”指的就是它 |
| 存在 `final/models/` | **不存在**。唯一权重是 `final/new_eye/models/sam_b.pt`（375,042,383 B）；`final/README.md:32` 与 `final/使用说明.md:62` 都把它写成 `new_eye/models/sam_b.pt` |
| final 只是根目录各子项目的副本 | 拷贝成分很大（`docs/` 7/7、固件源码全同），但 `new_eye/gui.py` 与上位机 6 个文件是**只在 final 出现的新代码** |
| final 里 `__pycache__` 是虚指 | 实有 2 个目录 18 个 `.pyc`（`final/new_eye/__pycache__` 6 个、`final/ghr/10.15/上位机/__pycache__` 12 个），含 `calib_tab/home_route` 的 pyc，是“新增模块确实被跑过”的硬证据 |

未覆盖：`final/new_eye/models/sam_b.pt` 内容（357.7 MiB）、`final/ghr/10.15/Project/Objects` 的二进制产物（只统计与比对 hash）、`crop_test.jpg` 之外的图片（只抽看 2 张截图）。

---

## 1. 目录结构与体积

| 子项 | 文件数 | 体积 | 内容 |
| --- | --- | --- | --- |
| `final/new_eye/` | 21 顶层 + 模型 1 + runtime 2 + pycache 6 | 358.2 MB | 视觉全套；其中 `models/sam_b.pt` 单体 **357.7 MiB**，占整个 final 的 **90.9%** |
| `final/ghr/10.15/` | 160+ | 34.9 MB | 固件 Keil 工程 + 上位机 + 4 个空 backup 目录 + 84 个构建产物 |
| `final/runtime/` | 5 | 0.5 MB | 验收截图（第 7 节） |
| `final/docs/` | 7 | 0.2 MB | 根目录散落文档的收纳副本 |
| 顶层 | 6 | 0.038 MB | `main.py` / `使用说明.md` / `README.md` / `requirements.txt` / `run.bat` / `setup.bat` |

顶层只有 4 个目录（`docs ghr new_eye runtime`），**没有 `.venv`、没有 `.gitignore`/`.gitattributes`**（`ghr/10.15/.gitattributes` 随固件一起拷进来了）。
“打包痕迹”明显：两处 `__pycache__`、`final/ghr/10.15/上位机/logs/`（诊断截屏 549 KB + 10-17 readback jsonl）、`Project/Objects/` 84 个 `.o/.axf/.hex`、4 个**空的** `backup_before_*` 目录（`pa0_pulldown`、`inline_gripper`、`toggle`、`accfix`）——空是因为 git 不跟踪空目录，两个副本里都是空的。

---

## 2. 与根目录的对应关系（hash 矩阵）

### 2.1 纯拷贝：逐字节相同

| final 路径 | 对应根目录 | 结论 |
| --- | --- | --- |
| `final/docs/*`（README.md、交接文档.md、环境复刻流程.md、EMM_V5 协议总结.md、pdftext.txt、newpdftext.txt、crop_test.jpg） | 仓库根目录同名散文件 | **7/7 hash 全同**。根目录没有 `docs/` 目录，final 只是把它们收进一个文件夹 |
| `final/ghr/10.15/` 顶层 9 个文档（交接文档-20261016、交接文档-上位机开工-TypeC、上位机方案 v1/v2/v3、新手联合开发完整教程、CONTRIBUTING、keilkilll.bat、.gitattributes） | `ghr/10.15/` | **9/9 全同** |
| `final/ghr/10.15/{MY, Drive, Test, USB, STM32}` | 同名目录 | **0 差异**（MY 5、Drive 19、Test 8、USB 29、STM32 154 个文件全部相同） |
| `final/ghr/10.15/Project`（非构建文件） | `ghr/10.15/Project` | 仅 `STM32F407.uvguix.rs` 差 1 字节（Keil 窗口状态，无意义） |
| `final/new_eye/` 14 个文件 | `new_eye/` | `vision.py camera.py camera_probe.py classify.py fusion.py tray_calib.py tray_calib.json samples.json main.py requirements.txt run_gui_as_admin.* 标定操作手册.md 联调与通讯方案.md` 全同 |
| `final/new_eye/runtime/empty_background.png` | `new_eye/runtime/empty_background.png` | 125,929 B，hash 相同 |

### 2.2 有实质差异（真正的“新代码”在这里）

| 文件 | 根目录 | final | 差异性质 |
| --- | --- | --- | --- |
| `new_eye/gui.py` | 55,834 B / 1240 行 / 10-21 20:33 | **63,861 B / 1421 行 / 10-21 01:00** | final **+181 行**：`embedded`/`ctrl` 嵌入参数 + 右侧四轴/夹爪控制块（第 4 节）；其余部分与根目录逐行一致，是**纯增量** |
| `ghr/10.15/上位机/host_gui.py` | 47,659 B / 10-20 20:35 | **51,562 B / 1165 行 / 10-21 00:40** | final 新增「开机回基准」整条链路（顶栏按钮、状态栏、`home_route` 状态机） |
| `ghr/10.15/上位机/vision_import.py` | 8,759 B / 183 行 | **9,988 B / 210 行 / 10-21 00:41** | final 新增：基准未建立时弹窗拦截 + “标定向导”改为切标签页 |
| `ghr/10.15/上位机/calib_tab.py` | — | **18,926 B（新）** | final 独有：X/Y 标定页（`CalibTab`） |
| `ghr/10.15/上位机/home_route.py` | — | **7,122 B（新）** | final 独有：开机回基准配置/状态的读写 |
| `ghr/10.15/上位机/home_route.json` | — | **588 B（新）** | final 独有：固定路线配置 |
| `ghr/10.15/上位机/tests/test_home_route.py` | — | **3,973 B（新）** | 回基准单元测试 |
| `new_eye/config.json` | 1472 B | **1423 B** | `empty_background` 由绝对路径 `D:\study\...\new_eye\runtime\empty_background.png` 改为相对 `runtime/empty_background.png`（`final/new_eye/config.json:70`），唯一目的：换机器不改配置 |
| `new_eye/auto_segment.py` | 12,044 B | **12,369 B** | final 多 7 行：`ROOT = Path(__file__).resolve().parent`（`final/new_eye/auto_segment.py:16`）与 `os.environ.setdefault('YOLO_CONFIG_DIR', .../runtime/ultralytics)`（`:21`），把 Ultralytics 缓存从 `%APPDATA%` 钉进 final 树内 |
| `new_eye/link.py` | 12,408 B / 10-21 20:33 | 11,326 B / 10-18 17:28 | **反向落后**：根目录新版把 UDP beacon 改成“每个本地 IPv4 各绑一个 socket 分别广播”，修掉 VPN/多网卡下热点网段收不到的问题；final 仍是单 socket 广播 255.255.255.255 |
| `new_eye/交接文档.md` | 53,912 B / 10-21 20:34 | 53,464 B / 10-20 20:43 | 根目录多 5 行：网卡广播修复记录 + 指向 `无线组网联调.md` |
| `new_eye/README.md` | 7,824 B / 10-21 20:34 | 7,604 B / 10-20 19:46 | 根目录多 4 行：`link_demo.py` 自检 + 无线组网说明 |
| `new_eye/link_robot_example.py` | 2,202 B | 2,202 B（hash 不同） | 仅 1 行打印文案 `[!]` → `⚠` |
| `new_eye/无线组网联调.md`、`new_eye/link_demo.py` | 7,142 B / 4,529 B | **缺失** | final 快照之后才产生的文件 |
| `new_eye/runtime/calib_marks.json` | 631 B / 10-20 20:53 | **缺失** | 标定标注点没有随交付带走（求解结果 `tray_calib.json` 在），见 7.2 |
| `new_eye/runtime/{calib_rb_proof,panel_proof}.png` | 75 KB / 402 KB | **缺失** | 开发期截图，未带 |

小计（`final/new_eye` 21 个顶层文件）：14 相同 / 7 不同 / 2 缺失。`final/ghr/10.15/上位机`（29 文件）：23 相同 / 2 改 / 4 新。

### 2.3 固件快照是哪一版？

- **源码完全一致**：`Drive/Include`、`Drive/Source`、`MY/`、`USB`、`Test` 与根目录逐字节相同，所以 final 里的固件 = 根目录当前的 10.15 固件源码。
- 构建产物：final 的 `Project/Objects/STM32F429.hex` 80,998 B，时间戳 **2026-10-17 17:39:09**；根目录同名 hex 在 **10-21 20:00:51** 重建（大小完全相同）。24 个 `.o/.axf` 因此 hash 不同——属重建噪声，不是代码差异。
- 版本线索：`backup_before_pa0_pulldown_20261015_205054`、`backup_before_inline_gripper_20261015_211202`、`backup_before_toggle_20261016_135500`、`backup_before_accfix_20261016_135752` 记录了 10/15–10/16 的四轮修复，所以 final 携带的是 **10/16 accfix 之后、10/17 构建的固件**；通信层为 Type-C USB CDC（`final/ghr/10.15/上位机/README.md:7`）。
- 与根目录的 `build/10.19/`（另一份 10-19 的固件构建副本，17 MB）没有交集，final 里没有它。

---

## 3. 一体化入口 `final/main.py`（155 行）

拼接方式很直接：**sys.path 注入两个目录，然后在同一个 Tk 根窗口里先建上位机 App，再往里插标签页**。没有子进程、没有 RPC、没有改两套代码的 import 结构。

1. 路径注入（`final/main.py:23-28`）：`HERE = Path(__file__).resolve().parent`，把 `HERE/new_eye` 和 `HERE/ghr/10.15/上位机` 依次 `sys.path.insert(0, ...)`。因此**只依赖文件位置，不依赖 cwd**；两目录没有同类名冲突（上位机没有 `main.py`，视觉没有 `host_gui.py`）。
2. 视觉页（`final/main.py:34-52`）：`import gui as vision_gui` → `vision_gui.App(frame, embedded=True, ctrl=app)`，`notebook.insert(0, ...)`；**捕获一切异常**，失败时在该页显示红字说明页（缺依赖 / 缺模型 / 相机被占），并明说“控制页不受影响”（`:42-52`）。
3. 标定页（`final/main.py:55-66`）：`import calib_tab` → `calib_tab.build(notebook, app, index=2)`；失败同样降级成说明页。
4. 装配顺序（`final/main.py:80-101`）：先 `host_gui.App(root, args)` 建出 6 页（`final/ghr/10.15/上位机/host_gui.py:449-479`），视觉插 index 0，标定插 index 2，最终 **8 页**：

| # | 页签 | 建立位置 | 数据/依赖 |
| --- | --- | --- | --- |
| 0 | 视觉 | `final/main.py:91`（插 0），`final/new_eye/gui.py:160` | 相机 + SAM，`new_eye/runtime/latest_targets.json` |
| 1 | 四轴控制 | `host_gui.py:453-460` | 串口 client / simulator |
| 2 | X/Y 标定 | `final/main.py:96`（插 2），`calib_tab.py:388` | `runtime/calib_marks.json` + `tray_calib.json` |
| 3 | 舵机夹爪 | `host_gui.py:461-463` | 固件参数表 `protocol.py` |
| 4 | 参数 | `host_gui.py:465-467` | 同上 |
| 5 | 动作序列 | `host_gui.py:469-471` | `sequence.py` |
| 6 | 视觉目标 | `host_gui.py:473-475` → `vision_import.py:67` | `latest_targets.json` + `vision_grasp.json` |
| 7 | 原始日志 | `host_gui.py:477-479` | 收发帧 |

5. 尺寸覆盖（`final/main.py:100-101`）：上位机自己 `geometry("1080x760")`，入口在 App 之后改成 `1760x980`。
6. 切页补刷新（`final/main.py:102-113`）：绑 `<<NotebookTabChanged>>`，切到视觉页调 `vision.refresh()`（`final/new_eye/gui.py:611`），切到标定页调 `calib.on_marks_refresh()`——解决隐藏页 canvas 尺寸为 1px、标注点不刷新的老问题。
7. 自检（`final/main.py:128-148`）：`--selftest` 下 2.5 s 后回调，打印页签列表、切到「视觉目标」「X/Y 标定」各截一张图（`final_app_*.png`）再退出，**不进入 mainloop**。
8. 参数（`final/main.py:71-77`）：`--port / --sim / --connect(dest=autoconnect) / --no-vision / --selftest`；`--sim/--port/--autoconnect` 恰好是 `host_gui.App.__init__` 读的三个字段（`final/ghr/10.15/上位机/host_gui.py:365-398`），所以 `args` 能直接透传。

**耦合风险**：`ghr/10.15/上位机/vision_import.py:21-23` 用 `ROOT = Path(__file__).resolve().parents[3]` 反推 final 根，再拼 `new_eye/runtime/latest_targets.json`。这个 `parents[3]` **只在这个三层嵌套下成立**；把 `上位机/` 挪走一层，视觉目标页就找不到 JSON。

---

## 4. 视觉页的嵌入化改造（final/new_eye/gui.py）

这是 final 里最实质的改动：原 `new_eye/gui.py` 是“自己是 `tk.Tk()` 的独立程序”，final 版改成“可以被别人塞进 Frame 的组件”。

- 构造函数（`final/new_eye/gui.py:160-166`）：`def __init__(self, r, embedded=False, ctrl=None)`；只有非嵌入模式才 `r.title(...)`/`r.geometry('1680x920')`；`:205-206` 只有非嵌入时才 `after(300, self.start)` 自动开相机。**默认参数保证 `python final/new_eye/gui.py` 仍可独立运行**，向后兼容。
- 「宿主」是上位机 App：`self.ctrl = ctrl` 就是 `host_gui.App`，`ctrl` 非空时启动 800 ms 定时器 `_ctrl_tick`（`final/new_eye/gui.py:203-204`）。
- 布局重排（`:233-244`）：右侧固定 440px 宽列放控制区 + “目标详情 / 状态（所有文字信息）”文本面板；左列两路画面上下叠放（截图见第 7 节）。
- 控制块 `_build_ctrl`（`:312-357`）：X/Y/Z/托盘四行 ±点动、脉冲数（默认 500）、使能 / 回0 / **开机回基准** / 停止、X 取反 / Y 取反、夹爪四键（夹紧/松开/收紧一步/松开一步）、舵机角度下发、连接状态红字。
- 通讯不经过视觉模块自己的 link，而是**直接借用上位机的串口客户端**：`_ctrl_client()` 取 `self.ctrl.client`（`:359-360`），`_ctrl_ready()` 检查 `client.online` 并提示“先在顶栏点连接”（`:362-367`）；点动 = `send('motor_enable', axis, 1)` + 120 ms 后 `send('motor_pos', axis, direction, 60, 100, pulses, 'rel')`（`:387-389`）；回 0 / 回基准是**调用上位机的方法** `ctrl.home_all()` / `ctrl.home_route()`（`:408-430`），因此逻辑只有一份，视觉页不复制运动状态机。
- 软位置刷新：`_ctrl_tick` 每 800 ms 读 `ctrl.soft_pos` / `ctrl.pos_known`，未知显示“未知”红字（`:451-467`）。
- 视觉侧仍保留 `note()` 统一消息入口（`:470-475`），视觉日志与上位机日志各记各的，不做合流。

---

## 5. 上位机侧新增（final 独有，根目录没有）

### 5.1 `home_route.py`：开机回基准的配置与状态

- 两个文件：`PATH = ROOT / 'home_route.json'`、`STATE_PATH = ROOT / 'home_state.json'`（`final/ghr/10.15/上位机/home_route.py:25-26`）。
- `DEFAULTS` 含 `zero_axes: [4, 5]`（X/Y）与 3 步默认路线（`:31-41`）；`normalize()` 校验轴号/方向/脉冲（负数直接拒绝，`:59-98`）；另有 `load/save/write_default_if_missing/step_ms/describe/load_state/save_state/state_text`（`:103/115/125/132/140/153/162/178`）。
- 落盘 `home_route.json` 的实际内容：`axis 4 dir 0 3000 pulses 60rpm` → `axis 5 dir 0 3000 60rpm` → `axis 1 dir 1 1000 40rpm`，`zero_axes [4,5]`，`settle_ms 800`，`updated_at 2026-10-21 00:41:07`。即：X、Y 各正向 3000 脉冲（≈1 圈内，3200 脉冲/圈）→ Z 负向抬 1000 → X/Y 清零。

### 5.2 `host_gui.py`：把回基准做成顶栏一条链路

`import home_route as HR`（`final/ghr/10.15/上位机/host_gui.py:28`）→ App 初始化读上次状态（`:384`）→ 顶栏按钮 `开机回基准`（`:424`）→ 状态栏标签显示“基准：已建立/未建立”（`:658`）→ `home_route()` 弹确认框列出路线（`:817-845`）→ 逐步相对走、每步回调 `_home_route_step`（`:847-864`）→ 终点对 `zero_axes` 清零、写 `home_state.json`、刷新状态栏（`:866-882`）。

### 5.3 `calib_tab.py`：X/Y 标定从“对话框”升级成“标签页”

`class CalibTab.__init__(self, parent, app)`（`final/ghr/10.15/上位机/calib_tab.py:24`），`build(notebook, app, index=2)` 插页（`:388`）；内部复用 `calib_wizard.py` 的仿射/简化求解，流程是 XY 点动 + 记录爪位 + 求解（`on_jog/on_marks_refresh/on_marks_record/on_marks_solve`，`:159/:302/:322/:367`）。旧 `calib_dialog.py`（39 KB）保留，“仍可独立打开”。

### 5.4 `vision_import.py`：两处行为变化

- 生成抓取序列前先读 `home_state.json`，`ok` 为假就弹“还没建立基准 / 坐标可能整体偏移”确认框（`final/ghr/10.15/上位机/vision_import.py:139-142`）。
- “标定向导”不再开对话框，而是切到 `app.calib_tab` 标签页并刷新标注点（`:178-182`）。
- 数据源仍是 `ROOT/new_eye/runtime/latest_targets.json`（`:21-22`）与 `ROOT/new_eye/tray_calib.json`（`:23`），过期阈值 8 s（`:27`）。

### 5.5 文档没跟上

`final/ghr/10.15/上位机/README.md` 与根目录 **hash 完全相同**，仍写着“75 个自动化测试 / tkinter 五页（四轴、夹爪、参数、序列、日志）”，没提 `home_route`、`calib_tab`；全 final 只有 `README.md`（9 处）和 `使用说明.md`（12 处）提到这两个新功能。

---

## 6. 使用说明要点（`final/使用说明.md`，401 行）

- **版本声明**：适用“2026-10-21 快照”，强调目录自包含，唯一外部依赖是 Python + 依赖包（`:3-4`）。
- **安装**：Windows 10/11 64 位、Python 3.10~3.12、磁盘 ≥1.5 GB（`:43-50`）；`setup.bat` 建 `final\.venv` 装 `requirements.txt`（`:54`）；有 NVIDIA 显卡可换 CUDA 版 torch（`:55-60`）。
- **模型**：`new_eye\models\sam_b.pt` 必须存在（“358 MB”）；仓库用 Git LFS 存，克隆后是 LFS 指针就先 `git lfs pull`，或让 Ultralytics 自动下载（`:62-65`）。注意 `final/README.md:32` 写的是“375 MB”——两者分别是 **358 MiB / 375 MB 十进制**，都对，但同一交付物两种口径容易让现场误判。
- **启动矩阵**（`:8-16`、`:69-75`，`final/run.bat:10-12` 的 `final\.venv` → `..\Qwen\.venv` → PATH 回退）：

| 命令 | 效果 | 缺什么会怎样 |
| --- | --- | --- |
| `run.bat` | 正常启动，8 页 | 视觉依赖缺失 → 视觉页变红字说明页，控制可用（`main.py:42-52`） |
| `run.bat --sim` | 控制侧用内置仿真后端 | 无硬件也能点界面、跑序列 |
| `run.bat --port COM5 --connect` | 指定串口并自动连接 | 需要 pyserial + 驱动 |
| `run.bat --no-vision` | 只开控制 | 完全不 import 视觉，torch/cv2 装不上时的兜底 |
| `run.bat --selftest` | 建窗、切页、截图到 `runtime\` 后退出 | 用于验收交付包完整性 |

- **页面与流程**：8 页清单（`:93-103`）；开机回基准（`:108-147`，`home_route.json` 字段表在 `:137-145`）；相机/四点框选/副摄方向/分割（`:151-190`）；分类“标注点 + 登记样本 + class_map”（`:193-221`）；X/Y 标定二维仿射 + 简化求解（`:225-247`，残差 <100 脉冲较好、<50 很好，`:240`）；Z/放料示教字段表（`:251-280`）；抓取序列结构 13 步（`:292`）；通讯（`:298-308`）TCP 5055 NDJSON / UDP 5056 发现 / 串口帧 `0xAA55+长度+JSON+CRC16` / 直接读 JSON 文件。
- **依赖**（`final/requirements.txt:8-16`）：numpy<2.3、opencv-python、pillow、torch≥2.4、torchvision、ultralytics、pyserial；pyinstaller 注释掉为可选。
- **已知限制**（`:372-378`）：无三维测量、规则分类、**固件 `motor_pos(abs)` 被当相对执行**、无硬件回零、香橙派要换 RKNN。
- **验收记录**（`:382-388`）：135 个上位机单测通过、8 页自检截图、`crop_test.jpg` 单次分割 5 目标、开机回基准模拟跑通。

---

## 7. 运行时证据

### 7.1 `final/runtime/` 5 张截图

| 文件 | 尺寸 | 证明什么 |
| --- | --- | --- |
| `final_app_proof.png` | 1760×980 | 一体化窗口真身：顶栏有“开机回基准 / 一键回0 / 停止全部(Esc)”，**8 个页签齐全**（视觉、四轴控制、X/Y 标定、舵机夹爪、参数、动作序列、视觉目标、原始日志），视觉页按钮在画面上方、两路画面上下叠放、右列四轴/夹爪 + 文字面板，右下角“基准：未建立（先放开机位，点『开机回基准』）”，状态栏“未连接 / 发送 0 帧” |
| `layout_proof.png` | 1760×980 | 视觉页布局验收：真 SAM 分割 5 个目标（顶视 #1–#5、副摄 3 个），右侧【融合】“配对 3 个（Δ 0.027，对齐轴 左右(u) 自动，方向 镜像 自动）”，【耗时】整帧 1.00 s / 分割 742 ms |
| `final_app_calib.png`、`final_app_vision_targets.png` | 45 / 76 KB | `--selftest` 自动切到「视觉目标」「X/Y 标定」两页的截图 |
| `home_route_proof.png` | 61 KB | 模拟后端跑通固定路线、状态栏“基准：已建立” |

### 7.2 数据文件清单（哪些配置真的随包交付）

| 文件 | 存在 | 内容/意味 |
| --- | --- | --- |
| `final/new_eye/config.json` | 是 | 相机 1/2、1920×1080@15、四点 top/side 各 4 点（含 `top_rotation 180`）、`class_map` 6 条、`sam_model models/sam_b.pt` |
| `final/new_eye/samples.json` | 是 | 现场登记样本：`五棱体|绿`，feature 向量在 |
| `final/new_eye/tray_calib.json` | 是 | 二维仿射已求解（`a 10404.4 / b −193.9 / c −13171.0 / d −12371.8 / tx −9263.4 / ty −50656.1`），`updated_at 2026-10-20T20:53:16` |
| `final/new_eye/runtime/empty_background.png` | 是 | 空盘背景，125,929 B，与根目录同一文件 |
| `final/new_eye/runtime/latest_targets.json` | 是 | schema 5、`seq 7`、`ts 1792513487.025` = **2026-10-21 00:24:47**，与 `layout_proof.png` 日志里的 00:24:37 同一次运行；`fusion{axis u, flip true, matched 3, mean_dist 0.027}`、`class_summary{待登记 4, 五棱体 1}` 与截图一一对应 |
| `final/new_eye/runtime/calib_marks.json` | **否** | 标定标注点没带；打开「X/Y 标定」页看不到既有标注点，需重新点（求解结果 `tray_calib.json` 仍在，可先沿用） |
| `final/ghr/10.15/上位机/vision_grasp.json` | 是 | **全部示教值为 0**（`z.safe/lift/place/drop.x/drop.y` 全 0，`z.grasp` 六类全 0）→ 交付态就是“未示教”，README`:160` 也确认真机抓取未验证 |
| `final/ghr/10.15/上位机/tray_state.json` | 是 | `current_box 1`，10-20 20:55 |
| `final/ghr/10.15/上位机/home_route.json` | 是 | 见 5.1 |
| `final/ghr/10.15/上位机/home_state.json` | **否** | 回基准结果文件不存在（根目录也没有）→ `home_route_proof.png` 的“已建立”是运行时内存态，状态文件未留存，不影响使用（下次启动显示“未建立”） |

辅证：`final/ghr/10.15/上位机/__pycache__/` 里有 `calib_tab.cpython-312.pyc`（32,848 B）与 `home_route.cpython-312.pyc`（10,846 B）→ 新模块确实被 import 执行过，不是只丢在目录里；`tests/` 7 个测试文件含 `test_home_route.py`；`logs/diagnostic-screen.png`（10-17）是更早的串口诊断现场证据。

---

## 8. final 是“交付快照”还是“以后的主线”？

**判断：交付快照（release snapshot），不是主线。** 依据：

1. **时间线明确分叉**：final 内最晚的时间戳是 `使用说明.md` 10-21 01:05、`README.md` 01:06；而根目录 `new_eye/gui.py`、`link.py`、`README.md`、`交接文档.md` 是 **10-21 20:33–20:35**，固件 hex 在 **10-21 20:00:51** 重建。即 final 冻结后，根目录又继续干了约 19 小时（无线组网 + 固件重建）。final 不可能是“以后的主线”，因为它已经落后一轮。
2. **final 的改造方向是“可交付”而非“可开发”**：`config.json` 绝对路径改相对（`final/new_eye/config.json:70`）、Ultralytics 缓存重定向（`final/new_eye/auto_segment.py:21`）、`parents[3]` 布局假设、`run.bat` 回退到 `..\Qwen\.venv`——都是“装箱”约束，不是长期开发布局。
3. **打包痕迹**：`__pycache__`、`logs/`、`Objects/` 84 个构建产物、4 个空 `backup_before_*` 目录都没清理，说明它是“整目录 copy + 少量改文件”的一次性快照，而不是整理过的发布分支。
4. **体积构成**：393.7 MB 中 357.7 MiB 是 SAM 权重，其余 36 MB 主要是固件 Keil 构建产物——按“能拷到比赛现场电脑上直接跑”组织。
5. **文档自述**：`使用说明.md:3` “适用版本：2026-10-21 快照”；`README.md:149` “已验证状态（2026-10-21）”；`:160` “尚未在现场验证：真实相机四点、Z 示教值、真机抓取”——是交付前自检清单的口吻。
6. **但它也不是“纯拷贝”**：`gui.py` 的嵌入化、`host_gui/vision_import` 的改动、`home_route.*`、`calib_tab.py`、`test_home_route.py` **只在 final 里**，根目录没有。所以 final 同时是**唯一的一体化分支封存点**，删掉它就丢功能。

**实际结论**：`final/` = 比赛交付用的冻结一体化包；仓库根目录（`new_eye/` + `ghr/10.15/`）= 仍在演进的主线。两者已经**双向漂移**：final 有集成功能而主线没有；主线有无线组网修复而 final 没有。

---

## 9. 风险清单与建议

| # | 风险 | 证据 | 建议 |
| --- | --- | --- | --- |
| 1 | 集成代码只有一份，主线再开发会丢 | `calib_tab.py` / `home_route.py` / `home_route.json` / `tests/test_home_route.py` / `host_gui.py` / `vision_import.py` 仅 final 有 | 把 6 个文件回流根目录，跑 `python -m unittest discover -s tests`（使用说明称 135 passed）后再动主线 |
| 2 | 网络修复只在主线 | 根目录 `new_eye/link.py` 多网卡 beacon、`无线组网联调.md`、`link_demo.py` 不在 final | 若近期还拿 final 去现场联网，需把 `link.py` + 文档同步进 final |
| 3 | `parents[3]` 布局强耦合 | `final/ghr/10.15/上位机/vision_import.py:21-23` | 移动 `上位机/` 前先改成显式配置或向上搜索 |
| 4 | 相对 `empty_background` 只被 GUI 兼容 | GUI 在 `final/new_eye/gui.py:835-838` 里 `ROOT / bpath`；但 `final/new_eye/vision.py:63` 直接 `cv2.imread(str(bg_path))`，依赖 cwd | 离线 CLI（`new_eye/main.py`）也要按 `ROOT` 解析，否则空盘背景静默失效（退回自动估背景） |
| 5 | 标定标注点没带 | `new_eye/runtime/calib_marks.json` 根目录有、final 没有 | 交付前补上，或在使用说明里写明“需重新标注 3~6 点” |
| 6 | 交付态未示教 | `vision_grasp.json` 的 `z.*`/`drop.*` 全 0；`README.md:160` 自述未验证真机抓取 | 现场必须按使用说明第 8 节示教，先 `--sim` 空跑 |
| 7 | 文档未同步 | `上位机/README.md` hash 与根目录相同（仍写 75 测试 / 5 页）；`README.md:32` 写 375 MB、`使用说明.md:62` 写 358 MB | 交前统一测试数与模型大小口径（358 MiB = 375 MB 十进制） |
| 8 | 交付包里有可删垃圾 | `__pycache__` 18 个 pyc、`logs/` 2 文件、`Project/Objects` 84 个、4 个空 backup 目录 | 打包时排除，体积可从 393.7 MB 降到约 386 MB（模型仍是大头） |

---

## 10. 一页速查

- **是什么**：2026-10-21 01:06 冻结的“视觉 + 四轴控制”一体化交付快照，393.7 MB，其中 357.7 MiB 是 `sam_b.pt`。
- **怎么拼的**：`final/main.py:24-28` 把 `new_eye/` 与 `ghr/10.15/上位机/` 塞进 `sys.path`，先建 `host_gui.App`，再插「视觉」「X/Y 标定」两页 → 8 页单窗口（`final/main.py:80-101`）。
- **谁在改**：`final/new_eye/gui.py` 加 `embedded/ctrl` 嵌入模式（+181 行，`:160`、`:312-357`）；`host_gui.py` 加开机回基准（`:817-882`）；新增 `home_route.py`、`calib_tab.py`、`test_home_route.py`、`home_route.json`。
- **和根目录**：`docs/` 7/7 相同、固件源码全同；`new_eye` 14 同 / 7 异 / 2 缺；上位机 23 同 / 2 改 / 4 新。
- **谁新谁旧**：final 的集成代码更新（10-21 00:39–00:41）；根目录的 `new_eye` 网络部分更新（10-21 20:33）。
- **结论**：交付快照 + 一体化分支的唯一封存点；主线在根目录，继续开发前先回流这 6 个文件。
