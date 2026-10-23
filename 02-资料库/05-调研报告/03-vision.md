# 03 · new_eye 双相机视觉子系统

> 调研范围：`new_eye/`（12 个 Python 文件 + 6 篇文档 + 配置/运行时 JSON）。只读调研，证据一律用 `相对路径:行号` 标注（行号为 2026-10-21 实测文件行号）。二进制未阅读：`new_eye/models/sam_b.pt`(357 MB)、`runtime/*.png`、`__pycache__/*.pyc`。

## 0. 一句话定位

new_eye 是整机的"眼睛"：**两路 USB 相机（顶视 + 副摄）→ 各自四点透视校正到统一 700×700 托盘坐标系 → SAM 实例分割 → 规则特征分类 + 双摄融合 → 输出托盘归一化坐标 `(u,v)` 与类别**，写 `new_eye/runtime/latest_targets.json`、可选 TCP 推送。它**从不直接驱动电机**——`motion_authorized` 恒为 `false`（`new_eye/gui.py:780`）。

- 赛项背景：2027 工创赛"智能+工程创新赛道"含智能搬运/智能分拣/智能救援（`pdftext.txt:15-18`），本工程对应**智能分拣**（`new_eye/交接文档.md:5`）。
- 规则约束：允许与笔记本电脑通讯，但比赛中不能触碰笔记本、不允许任何形式遥控（`pdftext.txt:29-30`）。因此"**笔记本跑视觉 + STM32 跑运动**"是合规且现实的分工（`new_eye/交接文档.md:392`）。
- 为什么"抓取前一次性识别"：三爪柔性夹爪不能旋转，且抓起物料后爪体遮挡物料（`new_eye/交接文档.md:9-12`）；顶视相机在正上方，机械臂下去就遮视野（`new_eye/联调与通讯方案.md:74-75`）。所以协议是"看一眼 → 冻结目标 → 运动"。

## 1. 数据流（端到端）

```text
USB cam0(顶视) ─┐                      ┌─ 700×700 校正图 → 前景候选 → SAM → mask/bbox/contour
                ├─ Camera.open(CAP_DSHOW→CAP_MSMF, 1920×1080@15)     │
USB cam1(副摄) ─┘                      └─ 反投影回原图 bbox_orig / center_orig
                                                    ↓
                                    classify（HSV 中值颜色 + 圆度/顶点/比例/实心度形状）
                                                    ↓
                        fusion（同一托盘 u/v 坐标，严格一对一配对，views=top+side/top/side）
                                                    ↓
                  runtime/latest_targets.json (schema_version=5, motion_authorized=false)
                                                    ↓
                     TCP 5055 每行一条 JSON（可选）+ UDP 5056 beacon 自动发现
                                                    ↓
                   香橙派/上位机 → 读 u,v × 托盘长宽 → 二维仿射 → X/Y 脉冲 → STM32
```

## 2. 相机与配置文件（`new_eye/config.json`，74 行）

| 键 | 当前值 | 含义 | 换机/换位置是否必须重标 |
|---|---|---|---|
| `top_camera_index` / `side_camera_index` | 1 / 2 | OpenCV 相机索引（按枚举顺序，插拔 USB 会变） | 换 USB 口必改（`new_eye/config.json:2-3`） |
| `width`/`height`/`fps` | 1920 / 1080 / 15 | 采集参数，Camera 里 set 后预读 5 帧（`new_eye/camera.py:9-20`） | 一般固定 |
| `frames_for_vote` | 5 | 单次识别投票帧数，实际被夹到 2~5（`new_eye/gui.py:1184-1185`） | 否 |
| `min_area_px`/`max_area_px` | 250 / 250000 | 旧版 `vision.py` 的面积门限（`new_eye/vision.py:55-117`）；SAM 链不用 | 否 |
| `pixels_per_mm` | **null** | **未被任何代码使用**（见第 4 节） | — |
| `tray_background` | "dark" | 托盘底色，旧版分割阈值分支用（`new_eye/vision.py:55-117`） | 换托盘要重评 |
| `top_points` | 4 点，见下 | 顶视四点框选（原图像素坐标） | **必重标**（`new_eye/config.json:24-41`） |
| `top_rotation` | 180 | 顶视显示旋转，只影响 GUI 坐标换算 | 相机装反才改（`:42`） |
| `side_points` | 4 点 | 副摄四点框选 | **必重标**（`:51-68`） |
| `side_rotation` | 180 | 副摄显示旋转（`:69`） | 同上 |
| `empty_background` | 绝对路径 `...\runtime\empty_background.png` | 空托盘灰度背景（`:70`） | **四点或托盘一动必重采** |
| `class_map` | 6 条 `形状|颜色` → 类别号 | 规则分类查表（`:43-50`），登记样本时会自动回写 | 否，随登记增长 |
| `class_names` | `{"五棱体":"五棱体"}` | 类别号 → 中文名（`:71-73`） | 否 |
| `sam_model` | `models/sam_b.pt` | SAM 权重相对路径（`:22-23`），实际由 AutoSegmenter 解析（`new_eye/auto_segment.py:21-33`） | 否 |

代码里还有一批 `config.json` **没写、靠 `.get` 兜底**的键：`link_port`(5055)、`link_enabled`、`side_flip`(0=自动)、`side_axis`(0=自动)、`side_every`(2)、`gray_objects`(false)、`background_diff`(26)、`sample_similarity`(0.88)、`side_shape_refine`。它们只在 GUI 里改，最后统一由"保存配置"落盘。

实测四点（原图像素）：

- 顶视 `top_points`：(734.9,545.2) (783.9,97.0) (217.5,83.6) (300.0,551.9) —— 注意这是**旋转 180° 显示前**的原始帧坐标，所以数值顺序看起来"从右下开始"（`new_eye/config.json:24-41`）。
- 副摄 `side_points`：(969.0,614.3) (1105.1,170.6) (540.9,175.0) (558.7,627.7)，宽度只有约 560 px（`:51-68`），说明副摄看的是托盘一条边。

## 3. 四点透视标定（单应矩阵）

- **原理**：把一个任意四边形映射成 700×700 正方形，`cv2.getPerspectiveTransform(p, dst)` + `warpPerspective`（`new_eye/gui.py:648-653`）。`dst=[[0,0],[700,0],[700,700],[0,700]]`，与**托盘左上/右上/右下/左下**一一对应。
- **点序强制**：界面标题直接写"依次点左上、右上、右下、左下；右键撤销"（`new_eye/gui.py:252`）；README 同（`new_eye/README.md:20-28`）。点序错则 u/v 全歪，融合必然失效。
- **采集**：`point_click()` 用 `canvas_to_frame`（含旋转/缩放/平移反算）把画布坐标换回原图像素（`new_eye/gui.py:503-524`，换算实现 `new_eye/gui.py:143-156`）。满 4 点即校验：多边形面积 `< 1000` 或自交都拒绝（`new_eye/gui.py:516-519`，自交判定 `:526-530`）。右键撤销（`:532-545`）。
- **双向变换**：每帧同时算 `H` 与 `inv=np.linalg.inv(H)`；检测在 warp 图里做，结果用 `perspectiveTransform` 反投影回原图得 `bbox_orig`/`center_orig`/`contour_orig`（`new_eye/gui.py:666-681`）。所以 JSON 里 `bbox_warped` 是校正图坐标、`bbox` 是原图坐标。
- **空背景必须同源**：`empty_background.png` 是**按当前四点 warp 后的 700×700 灰度图**（`new_eye/gui.py:591-607`），识别时再加载、用同一个 H 重投影（`:656-661`）。改任一点位/托盘位置，背景立即失效（代码里有"背景过期保护"：内区差分中位数 ≤18 才启用，`new_eye/auto_segment.py:66`）。
- **旋转独立于单应**：`View.angle` 只影响显示与点击反算（`new_eye/gui.py:99-156`），但四点保存的是**原始帧坐标**，所以旋转前后点的位置不冲突；`top_rotation` 默认 180（`new_eye/config.json:42`）。

## 4. 像素↔毫米、`pixels_per_mm` 与 Z 轴高度（**重要澄清**）

这一块设计与代码有落差，必须写清楚，避免导读误导：

1. **`pixels_per_mm` 是未使用的残留**：`new_eye/config.json:11` 为 `null`；全仓库对 `pixels_per_mm` 的引用只有一句设计说明——"托盘长宽实际毫米数、相机到托盘高度（影响 `pixels_per_mm`，但目前 u/v 方案不需要）"（`new_eye/联调与通讯方案.md:128`）。`gui.py`/`vision.py`/`auto_segment.py` 都没有读取它，`grep` 可验证。
2. **实际替代方案是"归一化托盘坐标"**：每个目标输出 `center_tray=[u,v]`（0~1，`new_eye/fusion.py:21`），`u` 沿四点顺序第 1→2 点方向、`v` 沿 1→4 点方向（`new_eye/联调与通讯方案.md:47`）。机器人侧"乘托盘实际长宽即得抓取点"（`new_eye/联调与通讯方案.md:29`、`:401-402`）。好处是**免去像素当量标定**，只需量托盘长宽 + 一次 u/v→脉冲的二维仿射。
3. **二维仿射（u,v）→（X/Y 脉冲）** 才是联调所需的标定量：`PX=a*u+b*v+tx`、`PY=c*u+d*v+ty`，3 个分散点即可解 6 个未知数，最小二乘实现 `new_eye/tray_calib.py:51-73`，用法 `uv_to_pulse`（`:85-94`），落盘 `new_eye/tray_calib.json`（`:97-107`）。
4. **Z 轴高度完全不在 new_eye 里**：`new_eye/tray_calib.py:11` 明确写"Z 轴高度、夹爪角度、放料位置这些'示教量'不在这里，见 `vision_grasp.json`"。真正来源是上位机 `ghr/10.15/上位机/vision_grasp.json` 的 `z.safe / z.grasp.<类别> / z.lift / z.place / drop.x / drop.y`，全部人工示教填脉冲值（`new_eye/标定操作手册.md:198-214`）。
5. new_eye 里唯一和"高度"沾边的量是 `height_ratio = 副摄 bbox 高 / 700`（`new_eye/gui.py:777`），它只是特征/显示用，**不是物理高度**，也不参与抓取。

## 5. 识别链

GUI 是**唯一生产入口**；`main.py` 是早期无界面原型，`vision.py` 只被 `main.py` 引用（`new_eye/main.py:5`），`gui.py` 完全不 import `vision`（`new_eye/gui.py:14-18`）。这一点 `交接文档.md:28` 也明确写了："`vision.py` 中仍保留旧的颜色和几何分割函数，但 `gui.py` 的'单次识别'入口使用 `auto_segment.py` 的 SAM 后端"。

### 5.1 采集/显示（`gui.py:29-157`）

- `Stream`：每路一个后台线程，`grab()/retrieve()` 循环，`fps()` 按累计帧/时间算，`get()` 返回 copy 避免撕裂（`new_eye/gui.py:29-67`）。
- `View`：Canvas 封装，拖拽平移、滚轮 zoom 0.25~4（`new_eye/gui.py:70-97`），`draw()` 负责旋转、缩放、叠加绿色四边形与点号（`:99-127`）。

### 5.2 单路检测 `App.detect_targets()`（`gui.py:643-694`）

```text
warp(700×700) → (顶视) 读背景并 warp → segmenter.segment(warp, bg, cfg)
→ 每个 mask: bbox 反投影、contour 反投影、center 反投影
→ 两路都先量一次颜色（供融合判同物） → 返回 out, warp
```
- 计时点：`top.warp / top.sam / top.post`（`new_eye/gui.py:689-693`）。
- 颜色异常兜底成 `未知/0.0`（`:687-688`）。

### 5.3 `App.detect_and_fuse()`（`gui.py:696-729`）

- 顶视必跑；副摄可选，且支持 `side_every` 隔帧复用 `side_cache`（`new_eye/gui.py:700-713`，默认 2，实测把整帧 1.46 s → 1.00 s，`交接文档.md:285-287`）。
- 融合 `fuse(..., flip_mode=cfg.side_flip, axis_mode=cfg.side_axis, info=info)`（`new_eye/gui.py:716-717`），诊断字典存 `self.fuse_info` 并进 JSON。
- 融合后统一分类 `classify_fused(fused, tw, sw)`（`new_eye/gui.py:740-746`）：有顶视就走完整分类（副摄只作形状修正），只有副摄则 `side_only=True`。

### 5.4 SAM 后端 `auto_segment.py`（247 行，核心创新点）

旧版的痛点是"空托盘也报目标"（5×5 网格盲撒点会出幻影 mask），现方案是**先用轻量前景找候选，再只在候选内部撒 SAM 提示点**（`new_eye/auto_segment.py:1-10`）：

1. `_foregrounds()`：颜色掩膜 `sat>45 & val>35` + 形态学（`new_eye/auto_segment.py:52`）；灰/黑物料走 `gray_objects` 分支（`:60`）；背景差分分支有防过期保护（内区差分中位数 ≤18 才启用，`:66`），阈值 `max(30, background_diff=26)` 且 `sat<55`（`:67`）。
2. `_blobs()`：`min_a=max(600,h*w*0.0025)`、`max_a=h*w*0.30`、填充率 ≥0.30、长宽比 ≤6、贴边（≤2 px）一律丢（`new_eye/auto_segment.py:76-100`）。
3. `_prompts()`：每 blob 中心 + 4×4 网格（0.2/0.4/0.6/0.8），落点必须在 mask 内且互距 ≥8 px，上限 40 点（`new_eye/auto_segment.py:103-128`）。
4. `segment()`：`self.model.predict(frame, points=pts, labels=[1]*len(pts), verbose=False)`（`new_eye/auto_segment.py:153`）——注意代码注释强调 **ultralytics 8.3 的 `SAM.predict()` 不接受 `multimask_output`**（`:151-152`），传了会 `SyntaxError`。
5. 过滤：面积 `<max(600,h*w*0.004)` 或 `>h*w*0.45` 丢（`:168`）；贴边且 `>h*w*0.15` 丢（`:172`）；前景覆盖率 <0.30（`:177`）、`fg_ratio<0.35`（`:181`）、"物体性" `sat_mean<32 且 diff_mean<20`（`:186`）丢。
6. 去重：`IoU>=0.45` 或包含比 `>=0.72`（`:201-203`）；另有 `_near()` 处理"同一物体顶面 mask / 整体 mask"这种 IoU 只有 0.2~0.4 的情况——中心距 `<0.35×短边` 且面积比 `>0.25` 视为同一物，留大的（`:235-247`，背景说明 `交接文档.md:365-369`）。
7. 输出 `{mask, contour, bbox, center_px, center_tray, area_px, confidence}`（`:191-194`），按托盘 `(v,u)` 排序（`:208`）。

### 5.5 双摄融合 `fusion.py`（143 行）

- 关键约束：副摄是**水平**看托盘，只有一个水平方向能与托盘某根轴对应（`new_eye/fusion.py:40-43`）。所以融合只有 1 维信息。
- 配对规则（`new_eye/fusion.py:46-75`）：**严格一对一**——每块副摄侧影最多配一个顶视目标；颜色明显不同（惩罚 0.10）直接跳过；贪心 `score=|Δ| + 颜色惩罚`。方案演进原因见 `交接文档.md:309-317`（旧版允许多个顶视目标共用一块侧影，导致"同一物体两画面两个类"）。
- 颜色惩罚 `_color_penalty`：一致 0 / 未知 0.03 / 不同 0.10（`new_eye/fusion.py:27-32`）。
- 窗口 `MAX_DIST=0.18`（700 px 图约 126 px，`new_eye/fusion.py:23`）；`AXIS_NAMES={'u':'左右(u)','v':'前后(v)'}`（`:24`）。
- 自动判定：`flip_mode` 0 自动试翻/不翻、`axis_mode` 0 自动试 u/v，取"(配对数, -代价)"最大（`new_eye/fusion.py:78-143`）。
- 产出 `views ∈ {top+side, top, side}`；`info` 回填 `axis/axis_name/flip/flip_mode/axis_mode/n_top/n_side/matched/mean_dist/unmatched_top/unmatched_side/auto` 直接写进 JSON 顶层 `fusion`（`new_eye/gui.py:715-718`、`new_eye/runtime/latest_targets.json:9-22`）。
- `color_mismatch` 仍保留但已基本不可能出现（`new_eye/fusion.py:118`，说明 `交接文档.md:316`）。

### 5.6 分类 `classify.py`（298 行，不训练模型）

**先排除两个常见误解**：本子系统**没有 YOLO / YOLO-World**，也**没有调用 Qwen 视觉大模型**。全仓 grep 结果：`YOLO`/`yolo` 零命中；`Qwen` 只出现在启动命令的 venv 路径 `Qwen\.venv\Scripts\python.exe`（`new_eye/README.md:9`、`new_eye/交接文档.md:51`、`new_eye/标定操作手册.md:27`）和 "旧版读 `Qwen/data/vision_targets.json`" 一句历史说明（`new_eye/联调与通讯方案.md:13`）。深度学习部分**只有 SAM**（`new_eye/auto_segment.py:20`），分类是纯 OpenCV/NumPy 规则。

用户定案"规则特征 + 现场登记"（`交接文档.md:253`）：

- **颜色**：掩膜先腐蚀（核 `min(h,w)*0.12`，调奇数），剔除 `V<=35` 与 `V>=250` 的过曝/死黑像素，取 HSV 中值分 7 桶：黑 / 浅色白 / 红 / 黄橙 / 绿 / 蓝浅蓝 / 紫其他，并给纯度置信度（`new_eye/classify.py:50-97`，桶定义 `:24`）。
- **形状**：`approxPolyDP(0.035*peri)` 顶点数 → 3 三棱(0.82) / 4 正方或长方(0.80，比例<1.25) / 5 五棱(0.78)；`(圆度>0.80 且 实心度>0.93)` 或 `(顶点≥6 且圆度>0.74 且实心度>0.93 且面积占比<0.84)` → 圆柱/圆形；否则其他几何体(0.55)（`new_eye/classify.py:111-138`）。
- **球 vs 圆柱**：副摄侧影顶部收窄比 `min(前3行占空)/max(后2行占空) < 0.78` 判"球"（`new_eye/classify.py:141-152`）——这是双摄真正互补的地方。
- **特征向量**：12+4+4 维 HSV 直方图 + 7 维 Hu 矩（/10）+ `[圆度, 比例, 顶点/6, 实心度, 面积占比, 侧影收窄]`，L2 归一化（`new_eye/classify.py:156-167`）。
- **查表顺序**（`new_eye/classify.py:224-248`）：`config.class_map`（source=`rule`, conf 0.95）→ `samples.json` 同键（`sample-key`, 0.9）→ 余弦最近邻阈值 `sample_similarity=0.88`（`nearest`）→ 都没有则 `待登记` + `class_source=unknown` + `need_review=true`（**不硬猜**）。
- **现场登记** `register()`：写 `samples.json`（余弦>0.985 去重），同时把 `形状|颜色 → 类别号` 回写 `class_map`、名字写 `class_names`，并立即 `save()`（`new_eye/classify.py:252-288`，GUI 侧 `new_eye/gui.py:1107-1134`）。
- 副摄独有目标（side-only）只给颜色，`class_name='副摄待确认'`、`class_source='side-only'`，且**不能登记**（`new_eye/classify.py:185-221`、`new_eye/gui.py:1108-1110`）。

### 5.7 单次识别（多帧投票）`recognize()`

- 至少 2 帧、最多 5 帧（`new_eye/gui.py:1184-1185`），每帧 `detect_and_fuse` 一次、间隔 80 ms（`:1192-1201`）。
- `vote_runs()`：以第一帧为基准目标列表，其余帧按托盘 `(u,v)` 曼哈顿距离 ≤0.06 找同一目标，对 `class_key/shape/color` 多数投票，`class_key` 胜出后再 `_lookup` 一次刷新 class_id/name，票数记 `class_votes`（如 `5/5`）（`new_eye/gui.py:1137-1173`）。
- 结束时打印一行汇总：目标数（顶视/副摄）、n 帧投票、总耗时与各阶段累计（`new_eye/gui.py:1229-1232`）。

### 5.8 实时分割 `live_loop()`

- 独立后台线程持续"取最新帧 → detect_and_fuse(side_every) → 写 JSON"，UI 侧只 `sync_live()` 取状态字符串（`new_eye/gui.py:917-956`；状态同步 `:905-915`）。
- 连续失败 5 次自动停（`new_eye/gui.py:933-940`）；停止时报告平均 s/帧与 fps（`:886-903`）。
- 分割 fps 用 EMA 平滑（`:944-945`）。

## 6. 输出 JSON（`schema_version=5`）与安全设计

落盘在 `new_eye/runtime/latest_targets.json`，**同目录 tmp 文件 + `Path.replace()` 原子替换**（`new_eye/gui.py:788-790`），避免上位机读到半截。

顶层（`new_eye/gui.py:780-787`，实测样本 `new_eye/runtime/latest_targets.json:1-25`）：

| 字段 | 说明 |
|---|---|
| `schema_version` | 5（历史 1=像素原型、4=加 views、5=加分类） |
| `motion_authorized` | **恒 false**（`new_eye/gui.py:780`） |
| `points_used` | 是否用了四点区域 |
| `max_classes` | 6（初赛 ≥6 类的要求，`交接文档.md:257`） |
| `seq` / `ts` | 帧序号 / Unix 时间戳（上位机判过期用） |
| `classification_logic` | 一句话说明"规则特征+SAM 掩膜+现场登记" |
| `fusion` | 融合诊断（axis/flip/matched/mean_dist/…） |
| `class_summary` | `{类别名: 数量}`（`new_eye/classify.py:291-298`） |
| `targets[]` | 见下 |

每个 target（实测样本 `new_eye/runtime/latest_targets.json:26-65`）：`id`、`views`、`confidence`、`bbox`（原图）、`bbox_warped`（700²）、`center_px`（原图）、`area_px`、`center_tray`（**归一化 u/v，机器人真正要的**）、`shape/shape_conf`、`color/color_conf`、`class_id/class_name/class_key/class_conf/class_source`、`need_review`、`color_mismatch`、`class_votes`；双摄都看到时附 `side_bbox`、`side_center_px`、`height_ratio`（`new_eye/gui.py:773-777`）。

`motion_authorized=false` 是贯穿设计的安全契约，四层落实：

1. 视觉端只写"候选"，从不发运动指令（`new_eye/README.md` 首段、`new_eye/gui.py:780`）；
2. 上位机侧校验该字段后才允许生成序列（`new_eye/联调与通讯方案.md:150`）；
3. 机器人端必须做"2 秒无新帧就停车"（`new_eye/link_robot_example.py:52-54`，约定 `new_eye/联调与通讯方案.md:33`、`交接文档.md:422-425`）；
4. 文档明确"最后才将 `motion_authorized` 改为受控授权"（`new_eye/交接文档.md:175`）。

## 7. 通讯（`link.py` / `link_demo.py` / `link_robot_example.py`）

三种链路在文件头注释里写清（`new_eye/link.py:1-22`）：TCP+JSON Lines（最省事）/ 网线固定 IP / 串口 0xAA55 帧给单片机。

- **TCP（主用）**：`TargetServer(port=5055, host='0.0.0.0')`，多客户端、自动踢死连接（`new_eye/link.py:165-250`，默认值 `:168`）；每行一条 JSON（`\n` 结尾）。`TargetClient` 断线 1 s 重连，`host='auto'` 时先自动发现（`:254-329`）。
- **帧格式**：`{"seq","ts","fps","n","targets":[{"id","u","v","class_id","class_name","angle","area","views"}],"fusion"}`，`control_frame()` 从完整 payload 压缩成这个精简结构（`new_eye/link.py:332-343`）。一帧 300~800 B、1 fps 量级（`new_eye/交接文档.md:404`）。
- **UDP 自动发现**：`DISCOVER_PORT=5056`、`DISCOVER_MAGIC='NEWEYE1'`（`new_eye/link.py:28-30`）；beacon 每 0.8 s 广播 `{app,port,ips}`（`:90-130`），**每个本地 IPv4 各绑一个 socket** 分别发（`:99-112`）——这是 2026-10-21 修的真实 bug：`255.255.255.255` 在开 VPN（Radmin）的笔记本上只走默认路由，热点网段收不到（`new_eye/交接文档.md:466-467`、`new_eye/无线组网联调.md:158-159`）。`discover(timeout=4.0)`（`:133-161`）。
- **串口帧**：`0xAA 0x55 | len(2B LE) | JSON(UTF-8) | crc16(2B LE)`，CRC-16/MODBUS（多项式 0xA001、初值 0xFFFF、覆盖 len+payload）（`new_eye/link.py:21`、`:34-49`）；`unpack_uart` 坏 CRC 时从 `i+2` 重新找帧头（`:52-66`）。
- **机器人端示例**：`link_robot_example.py` argv = host/port/串口/波特率，收到帧打印 `#id class u= v=`，有串口就 `pack_uart` 转发，2 s 无帧告警（`new_eye/link_robot_example.py:44`、`:52-54`）。
- **无相机自检**：`link_demo.py` 发 2 个假目标（`new_eye/link_demo.py:28-31`），`--selftest` 起服务端+自动发现客户端，10 s 收不到就报"检查 UDP 5056"（`:77-104`）；本机实测通过（`new_eye/无线组网联调.md:156-157`）。
- **无线组网手册**：热点网段 `192.168.137.0/24`、TCP 5055 / UDP 5056、防火墙两条 netsh、验收清单（`new_eye/无线组网联调.md:8-17`、`:33-40`、`:93-99`）。文档明确"1 fps 数据不能做闭环"（`new_eye/交接文档.md:425`）。

## 8. GUI 页面与操作流程（`gui.py`，1240 行）

单窗口 `1680×920`（`new_eye/gui.py:163`），布局 = 顶栏 + 左双画面（PanedWindow 各半）+ 右 540 px 文字面板（`new_eye/gui.py:203-296`）。

| 区域 | 控件 | 作用/实现 |
|---|---|---|
| 顶栏 | 顶视/侧视相机下拉 + **扫描相机** | 遍历 0..9 试开，成功即列入（`new_eye/gui.py:412-423`） |
| 顶栏 | **打开双画面 / 停止** | 起两个 Stream 线程 / 关流（`:425-473`） |
| 顶栏 | 含灰/黑物料 | 置 `gray_objects`，影响 SAM 前景分支（`:217-220`） |
| 顶栏 | **保存配置** | 把相机索引、四点、旋转写回 `config.json`（`:580-589`），**注意不写 pixels_per_mm/link_port** |
| 顶栏 | **网络推送** | 起/停 `link.TargetServer(5055)`，日志打出本机所有 IP（`:620-640`） |
| 底部 | 顶视/侧视 ↶↷90°/复位 | 只改显示角度（`:547-556`） |
| 底部 | **顶视四点框选 / 副摄四点框选** | 进入 `point_mode`，按序点 4 个角（`:495-524`） |
| 底部 | 副摄方向 自动/同向/镜像 | `side_flip` 0/1/2（`:566-571`） |
| 底部 | 对齐轴 自动/左右/前后 | `side_axis` 0/1/2（`:573-578`） |
| 底部 | **采集空托盘** | 顶视 warp 后存灰度 `runtime/empty_background.png` 并写配置（`:591-607`） |
| 底部 | 清除定位 | 清四点/融合结果（`:485-493`） |
| 底部 | **单次识别** | 多帧投票识别 + 写 JSON（`:1175-1232`） |
| 底部 | **实时分割** | 启停后台连续识别线程（`:867-903`） |
| 底部 | **登记样本** | 进入登记模式，点目标→弹窗输类别→写 samples.json/class_map（`:959-969`、`:1107-1134`） |
| 底部 | **标注点 / 清空标注** | 标定用：点画面存 u/v（`:1027-1105`） |
| 右侧面板 | 12 号 Consolas 文本 | 【运行】【帧率】【状态】【分割】【标注】【融合】【耗时】【类别】【目标明细】【最近消息】（`_panel_text` `:305-393`，签名比对避免闪烁 `:395-409`） |

**典型操作顺序**（`new_eye/README.md:20-28` 与 `new_eye/标定操作手册.md:30-34` 一致）：扫描相机 → 打开双画面 → 顶视四点框选 → 副摄四点框选（按托盘同一方位）→ 设方向/对齐轴 → 采集空托盘 → 单次识别或实时分割 → 看右侧面板/JSON →（标定时）标注点。

## 9. 标定操作手册要点（`标定操作手册.md`，263 行）

手册有 10 步 + 排查表 + 完成标准，与 new_eye 直接相关的是第 1/3 步：

- **第 1 步**：启动 GUI → 顶视选 1、副摄选 2 → 打开双画面 → 顶视四点框选 → 单次识别 → 记下目标 `u,v` 与 `#编号`（`new_eye/标定操作手册.md:30-34`）。判读规则："u 越大越靠画面右，v 越大越靠画面下；若明显不符说明四点反变换不对，先别标定"（`:36-37`）。
- **第 3A 步「视觉点标注法」（推荐）**：new_eye 点「标注点」→ 在画面点 3~6 个分散点（青色 P1/P2…，右键撤销）→「结束标注」自动存 `runtime/calib_marks.json` → 上位机向导「刷新标注点」→ 用单轴页把夹爪中心移到该物理点 →「记录爪位」→ 全部记完「求解并保存」写 `tray_calib.json`（`new_eye/标定操作手册.md:75-91`）。优点：不用沿固定方向走轴、不需回 0、不会三点共线报奇异（`:86-87`）。
- **第 3B 步「手动走轴 + 视觉记录」（旧法）**：每次移动后单次识别 → 向导「① 读取 u/v」「② 记录坐标」→ 3~5 点后「③ 求解并保存」，脉冲来自上位机软位置记账（`new_eye/标定操作手册.md:93-113`）。
- **求解判定**：需要 `✓` 方向检查、残差 **<100 脉冲**（最好 <50），出现 ⚠"X 主要影响 v"先别抓取（`new_eye/标定操作手册.md:163-165`）。
- **第 9 步示教 Z/放料**：`vision_grasp.json` 的 `z.safe / z.grasp.<类别名> / z.lift / z.place / drop.x / drop.y / speed / tray.start_box / dir_positive`，类别名必须与 new_eye 的 `class_map` 一致，否则 fallback 到 `z.lift` 并 warning（`new_eye/标定操作手册.md:198-214`）。
- **完成标准**：`tray_calib.json` 存在且有 ✓、残差 <100、第 7 步验证误差 <0.02、软位置非"未知"、示教值非 0、空跑无碰撞（`new_eye/标定操作手册.md:255-263`）。

## 10. 当前完成度与实测数据

时间线（全部来自 `交接文档.md` 的分节更新记录）：

| 日期 | 内容 | 状态 |
|---|---|---|
| 10-18 早 | 双相机 GUI + 四点 + SAM 分割，schema 1 | 完成（`交接文档.md:16-28`） |
| 10-18 | 实时分割 / 去误检（候选内撒点）/ 双摄融合，schema 4；实测 0.4~1.5 fps（GPU 0.66 s/帧单路）（`交接文档.md:243-249`） | 完成 |
| 10-18 | 规则特征分类 + 现场登记，schema 5；实测 5 物体全 `top+side`、`det_fps≈0.56`（`交接文档.md:251-262`） | 完成 |
| 10-18 | 右侧文字面板重构 1680×920（`交接文档.md:264-275`） | 完成 |
| 10-18 | 耗时埋点 + 副摄隔帧：整帧 1.46 s→1.00 s，0.53→0.98 fps（`交接文档.md:277-287`） | 完成 |
| 10-18 | 严格一对一融合，修"一物两类"（`交接文档.md:300-322`）；实测 6 融合项 = 4×top+side + 1×top + 1×side（`:321`） | 完成 |
| 10-18 | 副摄方向 + 融合诊断 + JSON `fusion` 字段（`交接文档.md:325-349`） | 完成 |
| 10-18 | 对齐轴 + 重复框合并 + 标签避让（`交接文档.md:352-379`） | 完成 |
| 10-18/21 | 通讯 link.py + 无线方案 + VPN 广播修复（`交接文档.md:382-467`） | 完成并自测 |
| 10-18~20 | X/Y 标定向导并存 v5/标注点法，`tray_calib.py` 落地（`交接文档.md:469-520`、`:645-707`、`:688-707`） | 代码完成，**数据待验证** |
| 10-18 | 空跑 3 目标 → 69 步全 ok，转盘只转 60°（`交接文档.md:494`、`联调与通讯方案.md:186-191`） | 仿真通过 |

性能实测（同一台机、crop_test 图 + 合成副摄）：

- SAM 是绝对大头：整帧 1.06 s 中顶视 821 ms（其中 SAM 799 ms），分类 ~17 ms、融合 <1 ms、写 JSON ~1 ms（`new_eye/交接文档.md:322`、`:282-283`）。
- 单次识别 2 帧合计 2.51 s（`new_eye/交接文档.md:304`）；实时 12 帧平均 1.29 s/帧（`:306`）。
- 副摄隔帧复用后 0.98 fps（`:287`）。
- 结论：**不能用视觉闭环**，只能"看一眼→规划→动"（`new_eye/交接文档.md:425`）。

## 11. 未验证 / 风险 / 未解决问题（按重要性）

1. **X/Y 标定数据不合格（最硬的未完成项）**：`new_eye/tray_calib.json` 的 `residual` 实测 `dy_pulse` 达到 **-28960 / +28396 / +28063 / -32451**，`dx_pulse` 也有 1047（`new_eye/tray_calib.json:44-75`）；而验收标准是残差 <100 脉冲（`new_eye/标定操作手册.md:164`、`:258`）。也就是说这份 affine（`new_eye/tray_calib.json:4-11`，a=10404.4、d=-12371.8、tx=-9263.4、ty=-50656.1）**不能用于真抓**，必须重标。5 个采样点来自 2026-10-20 20:53（`new_eye/runtime/calib_marks.json:2`），u/v 与脉冲明显不自洽。
2. **视野覆盖不足**：顶视看不到整盘 6 个盒，当前只针对"当前视野内的目标"生成序列；"分盒扫描（转 60°→识别→累积）"流程**还没做**（`new_eye/交接文档.md:530-533`、`new_eye/联调与通讯方案.md:222`）。
3. **分类仍可能误判**：六棱柱可能被归入"圆柱/圆形"（`extent<0.84` 兜底误收）；同色同形不同大小的物体规则特征无能为力；强反光/暖光下颜色漂移；side-only 目标只给颜色（`new_eye/交接文档.md:262`）。当前 `samples.json` 只登记了 1 个样本（`new_eye/samples.json:2-51`），`class_map` 6 条里已有 4 条的类别名是纯数字 `"5" "2" "6" "4"`（`new_eye/config.json:43-50`），语义模糊。
4. **分类/融合未在真实物料上整链验证**：所有实测都是 `crop_test.jpg` 静态图 + 合成副摄影像（`new_eye/交接文档.md:261`、`:321`、`:347`）；"一件物料一个掩膜"仍需真实托盘确认（`交接文档.md:167-169`）。
5. **副摄不做三维定位**：只有 1 维水平信息 + `height_ratio` 特征，没有真正的立体/Z 估计（`new_eye/交接文档.md:249`、`new_eye/fusion.py:40-43`）。
6. **半圈/遮挡类物理限制**：副摄画面里被裁切/贴边的物体不参与融合（`new_eye/交接文档.md:249`）；同列前后重叠物体会叠成一块侧影（融合已改一对一，但代价是"副摄未印证"变多，`:310-315`）。
7. **依赖未声明**：`requirements.txt` 只有 3 行（opencv-python / numpy / pillow，`new_eye/requirements.txt:1-3`），**没有 torch / ultralytics**，实际依赖 `Qwen\.venv`；README 和交接文档都用该 venv 解释器启动（`new_eye/README.md:8-9`、`new_eye/交接文档.md:49-52`）。换机/重装环境必须知道这一点。
8. **`sam_b.pt` 357 MB 在仓库工作区里**（`new_eye/models/sam_b.pt`），首次加载数秒（`new_eye/gui.py:882` 提示"首次加载 SAM 约需数秒"），`auto_segment.py:35` 缺权重时的报错是"未找到SAM权重，请把sam_b.pt放入new_eye/models/"。
9. **权限坑**：部分机器上 USB 相机会因权限打不开，需管理员启动（`new_eye/run_gui_as_admin.ps1:1-6`、`new_eye/run_gui_as_admin.cmd`，均以 `Start-Process -Verb RunAs` 起 `Qwen\.venv\Scripts\python.exe gui.py`）。
10. **配置漂移**：`config.json` 缺 `link_port/side_flip/side_axis/side_every/gray_objects`（`new_eye/config.json:1-74` 全文无这些键），只有点过"保存配置"才会写入；换机时这些参数会静默回落默认值。

## 12. 依赖与运行

- 解释器：`Qwen\.venv\Scripts\python.exe`（非仓库内，路径写死在启动脚本里，`new_eye/run_gui_as_admin.ps1:3`）。
- 关键第三方：`opencv-python`、`numpy`、`pillow`（声明，`new_eye/requirements.txt:1-3`）+ **ultralytics(SAM)**（`new_eye/auto_segment.py:20`）+ torch（由 ultralytics 带入，未声明）+ tkinter（标准库）。
- 纯标准库模块可独立跑：`link.py` / `link_robot_example.py` 部署到香橙派最少只需这两个文件（`new_eye/无线组网联调.md:150-151`）；`tray_calib.py` 纯标准库且有自检（`python new_eye/tray_calib.py`，`new_eye/tray_calib.py:120-132`）。
- `run_gui_as_admin.cmd` 会先 `cd /d "%~dp0.."`（仓库根）再以管理员起 GUI，保证相对路径 `new_eye/config.json`、`models/sam_b.pt`、`runtime/` 正确解析（`gui.py:20-22` 用 `Path(__file__).parent`，其实对 cwd 不敏感；`auto_segment.py:22-33` 同时按 cwd 与 new_eye 目录找权重）。

## 13. 与其他子系统的边界（Lead 汇总时可交叉引用）

- **`ghr/10.15/上位机/`**：`vision_import.py` 读 `new_eye/runtime/latest_targets.json`（schema 4/5，>8 s 视为过期，校验 `motion_authorized`），`vision_grasp.py` 把它变成 `sequence.Sequence`，`calib_wizard.py`/`calib_dialog.py` 做 u/v→脉冲标定（`new_eye/交接文档.md:469-494`、`new_eye/联调与通讯方案.md:143-153`）。
- **`ghr/10.15/Drive`、`MY`（STM32 固件）**：接收上位机 CDC 行协议，四轴 EMM 驱动器 + 舵机夹爪；已知固件问题：`motor_pos` 的 `mode` 参数被忽略、`abs` 实际是相对（`new_eye/交接文档.md:651-653`）。
- **接口契约**：`(u,v) ∈ [0,1]`、左上为原点（`new_eye/fusion.py:21`）；`views` 三态语义（`top+side`/`top`/`side`）；`fusion.matched==0` 且两路都有目标时的告警文案（`new_eye/gui.py:342-345`）。

## 14. 关键文件索引（阅读顺序建议）

| 文件 | 行数 | 读它为了 |
|---|---|---|
| `new_eye/gui.py` | 1240 | 全部业务编排：UI、四点、检测、融合、写 JSON、通讯开关（`:203-296` 建 UI、`:643-792` 识别主链） |
| `new_eye/auto_segment.py` | 247 | SAM 后端：候选撒点、过滤、去重（`:131-209`） |
| `new_eye/classify.py` | 298 | 颜色/形状特征与查表（`:185-288`） |
| `new_eye/fusion.py` | 143 | 一对一融合与自诊断（`:78-143`） |
| `new_eye/link.py` | 343 | TCP/UDP/串口协议（`:28-66` 常量与帧、`:90-161` 发现、`:165-343` 收发） |
| `new_eye/tray_calib.py` | 132 | u/v→X/Y 脉冲二维仿射（`:51-107`） |
| `new_eye/camera.py` | 1212 B | 相机打开与降级 |
| `new_eye/vision.py` | 145 | **旧版**阈值/颜色/形状，仅 `main.py:5` 引用 |
| `new_eye/main.py` | 68 | 早期无界面原型，schema 1 |
| `new_eye/交接文档.md` | 767 | 完成度、实测数据、已知问题的唯一权威记录 |
| `new_eye/标定操作手册.md` | 263 | 从标定到示教到空跑的 10 步 + 排查表 |
| `new_eye/联调与通讯方案.md` | 222 | 链路设计、仿射原理、序列模板、安全约束 |
| `new_eye/无线组网联调.md` | 159 | 热点/防火墙/自动发现/验收清单 |

**一句话给导读**：new_eye 的"看得见、算得出、说得清"三件事已经打通（双摄 SAM + 规则分类 + 融合 + JSON/TCP），"动得准"还差最后一公里——X/Y 标定残差超标（`new_eye/tray_calib.json:44-75`）且只在静态图/合成副摄上验证过，`motion_authorized` 因此仍恒 `false`。
