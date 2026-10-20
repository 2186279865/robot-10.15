# final/ — 机器人视觉分拣（视觉 + 控制 一体化）

一个窗口里同时有 **视觉** 和 **10.15 上位机控制**：

```
┌─ 视觉 ─┬─ 四轴控制 ─┬─ X/Y 标定 ─┬─ 舵机夹爪 ─┬─ 参数 ─┬─ 动作序列 ─┬─ 视觉目标 ─┬─ 原始日志 ─┐
```

- **视觉**（new_eye）：画面上方是相机/视角/识别按钮，左侧两路画面**上下叠放**，右侧是四轴/夹爪紧凑控制 + 文字面板
- **X/Y 标定**：独立标签页，只保留“视觉点标注”模式（u/v ↔ X/Y 脉冲），「视觉目标」页的“标定向导”按钮会切到这一页
- **视觉目标**：读取视觉输出的 `latest_targets.json`，勾选目标一键生成抓取序列，填进「动作序列」页
- 其余页：四轴控制 / 一键回 0 / 舵机夹爪 / 参数 / 序列编辑与运行 / 原始日志

> 现场操作手册：`new_eye/标定操作手册.md`（X/Y 标定一步步版）
> 开发交接：`new_eye/交接文档.md`、`docs/交接文档.md`
> 通讯协议：`docs/EMM_V5_串口协议与工程指令总结.md`

---

## 1. 目录结构

```
final/
├─ main.py                 一体化入口（视觉 tab + 控制 tab）
├─ run.bat                 双击启动（依次找 final\.venv → ..\Qwen\.venv → PATH 里的 python）
├─ setup.bat               首次部署：建 .venv、装依赖
├─ requirements.txt        依赖清单（cv2 / torch / ultralytics / pillow / pyserial）
├─ README.md               本文件
├─ docs/                   比赛/协议/环境/交接文档 + 测试图 crop_test.jpg
├─ new_eye/                视觉（相机、SAM、分类、融合、标定标注、GUI、模型）
│   ├─ models/sam_b.pt     SAM-B 权重（375 MB，已包含）
│   ├─ runtime/            运行产物：latest_targets.json、calib_marks.json、empty_background.png
│   ├─ config.json         相机编号/四点/旋转/类别表等
│   └─ samples.json        现场登记的分类样本（首次登记后生成）
└─ ghr/10.15/              机器人固件 + 上位机
    ├─ 上位机/             主程序、vision_import、vision_grasp、calib_wizard、calib_dialog、calib_tab
    │   ├─ vision_grasp.json   示教参数（Z 高度/放料点/转盘盒位，默认 0 = 未示教）
    │   └─ tray_state.json     转盘当前盒位记忆（自动生成）
    └─ Drive/My/Project/...  固件工程（Keil）
```

**所有路径都是相对 final/ 自洽的**：把整个 `final` 文件夹拷到别的电脑，路径依然正确
（`new_eye/runtime`、`new_eye/tray_calib.json`、`ghr/10.15/上位机/vision_grasp.json` 等都不需要改代码）。
Ultralytics 的设置/缓存也会写到 `new_eye/runtime/ultralytics/`，不会碰 `%APPDATA%`。

**只有 Python 依赖需要另外装**（torch / ultralytics / opencv / pillow / pyserial）：跑一次 `setup.bat`
在 final 内生成本地 `.venv` 即可；没有 `.venv` 时 `run.bat` 会退回用 `..\Qwen\.venv` 或系统 python。

---

## 2. 首次部署

1. 安装 Python 3.10~3.12（勾选 *Add python.exe to PATH*）。
2. 双击 `setup.bat`（建 `.venv` 并装依赖；有 NVIDIA 显卡可按提示换 CUDA 版 torch）。
3. 双击 `run.bat` 启动。

命令行启动：

```
cd final
python main.py                 # 用系统 python
.\.venv\Scripts\python.exe main.py

python main.py --sim           # 控制侧走模拟后端（不接硬件也能试界面/序列）
python main.py --port COM5 --connect
python main.py --no-vision     # 只开控制（视觉依赖装不上时的兜底）
python main.py --selftest      # 自检：建界面、切页、截图到 runtime/
```

---

## 3. 一次完整流程（简版）

### 开机：先回基准（每次上电都要做）

> 固件没有限位/回零，且 `motor_pos(abs)` 实际是相对移动，所以“0 基准”必须每次开机重新建立。

1. 断电重启后，**手动把龙门推到机架上的固定“开机位”**（X/Y 都对齐标记）。
2. 打开 `main.py` → **连接** → 顶栏点 **「开机回基准」**（确认框里会列出固定路线）。
3. 机器按 `ghr/10.15/上位机/home_route.json` 的 `steps` 依次相对走（默认 X 3000 → Y 3000 → Z 抬 1000），
   到终点后对上 `zero_axes`（默认 X/Y）发清零 → 软位置置 0，右下角显示 **基准：已建立 时间**。
4. 之后再视觉标定/生成抓取序列。没建立基准时生成序列会弹窗警告（坐标可能整体偏移）。

改路线：直接编辑 `home_route.json`（`steps` 里 `axis` 取 1/3/4/5、`dir` 0=正 1=负、`pulses` 非负），
或把 `enabled` 设 false 关掉。每次执行结果写在 `home_state.json`。

### 视觉
1. 「视觉」页：选顶视/侧视相机编号 → **扫描相机** → **打开双画面**。
2. 顶视画面上依次点托盘四角（左上→右上→右下→左下）→ **顶视四点框选**；
   副摄同样点四点 → **副摄四点框选**；方向不对就切「副摄方向」「对齐轴」。
3. **实时分割** 看效果；没框住就点 **采集空托盘**（灰/黑物料再勾「含灰/黑物料」）。
4. 类别：先用 **标注点** 在工作台面上点 3~6 个物理位置（上位机标定向导要用）；
   再用 **登记样本**（现场给盒子里的实物登记类别），登记后同一形状+颜色的物块自动归类。

### 标定（视觉 ↔ 爪子）
1. 「视觉目标/标定向导」→ 模式选 **视觉点标注** → **刷新标注点**。
2. 用「四轴控制」把夹爪移到每个标注点正上方 → 回标定向导点 **记录爪位**。
3. 记录 ≥3 个（分散、不共线）→ **求解并保存**（文件 `new_eye/tray_calib.json`）。
   失败就点 **简化求解（只走 X/Y）**。

### 抓取
1. 视觉页 **单次识别**（结果写 `new_eye/runtime/latest_targets.json`，8 秒内有效）。
2. 「视觉目标」页 → **读取最新视觉结果** → 勾选目标 → **生成抓取序列（选中目标）**。
3. 首次先填 `ghr/10.15/上位机/vision_grasp.json` 的示教值：
   `z.safe` / `z.lift` / `z.place` / `z.grasp.<类别名>` / `drop.x` / `drop.y`（0 = 未示教会告警）。
4. 先 `--sim` 或低速 **空跑** 验证轨迹，再装物块真抓。

> u/v 是**四点框选区域内**的归一化坐标（u 左右、v 前后），不是整幅画面的像素。

---

## 4. 常见问题

| 现象 | 处理 |
| --- | --- |
| 视觉页启动失败/黑屏 | 依赖没装（`pip install -r requirements.txt`）或模型缺失；先看该页红字提示 |
| 相机打不开 | 关掉 OBS/相机预览；「扫描相机」看设备号；相机被别的程序占用时 Windows 只允许一个程序打开 |
| 分割很慢（1.5~4 s/帧） | 装 CUDA 版 torch；或在 `new_eye/config.json` 换更小的 SAM 权重 |
| 标定报“矩阵奇异/共线” | 直接点「简化求解（只走 X/Y）」；或补一个斜向点 |
| 标定报“视觉几乎没动” | 物块被碰走/换错目标编号；单次识别与记录坐标的物块要同一个 |
| 点动没反应 | 先点「使能 X/Y」；看日志里有 ✓/✗ 与错误码（E13 未回零 / E02 参数 / E04 无回读 / 锁存需清） |
| 点了取消使能还是推不动 | 丝杠自锁，属正常；用手推标定不可行，用「视觉点标注」法 |
| 视觉目标页“已过期” | 视觉页再点一次 **单次识别** |
| 开机基准每次都对不上 | 开机位标记要固定（画线/挡块）；路线 `home_route.json` 的脉冲每次必须一样，中途别手推 |
| 生成序列提示“还没建立基准” | 先放开机位点「开机回基准」，或确认后强行生成（坐标会偏） |
| 串口连不上 | 装 pyserial；设备管理器看 COM 号；用 `--port COM5 --connect` |

关掉程序：直接关窗口；`Esc` 是急停（停止全部）。

---

## 5. 打包成 exe（可选）

```
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --windowed ^
  --name RobotVision ^
  --add-data "new_eye\models;new_eye\models" ^
  --add-data "new_eye\config.json;new_eye" ^
  main.py
```

注意：`ultralytics`/`torch` 体积大，`--onedir` 模式（默认）比 `--onefile` 启动快得多。
打包后把 `new_eye/` 的 `runtime/`、`samples.json`、`ghr/10.15/上位机/*.json` 放到 exe 同级目录。

---

## 6. 已验证状态（2026-10-21）

- 一体化窗口启动、8 个标签页：自检截图 `runtime/final_app_proof.png`、
  `runtime/final_app_calib.png`、`runtime/final_app_vision_targets.png`。
- 新版视觉页布局（按钮在画面上方、两路画面上下叠放、右侧四轴/夹爪）：假帧实时分割
  5 个目标，截图 `runtime/layout_proof.png`。
- 控制侧单元测试：`ghr/10.15/上位机` 下 `python -m unittest discover -s tests` → **135 passed**。
- 开机回基准：模拟后端跑通固定路线（3 步 → X/Y 清零 → `home_state.json` ok，
  状态栏“基准：已建立”），截图 `runtime/home_route_proof.png`。
- 视觉侧：`crop_test.jpg` 单次分割 5 个目标、冷启动（含 SAM 加载）≈10 s，
  实时分割稳态 ≈1 s/帧（GPU）。
- 尚未在现场验证：真实相机四点、Z 示教值、真机抓取（需要你按手册示教后跑）。
