# 本地千问视觉识别

## YOLO-World → Qwen → 上位机联调

以管理员身份运行 `camera_preview.cmd`（本机 USB HD Camera 普通权限曾返回访问被拒绝）。
默认选择“台面增强”和 `qwen2.5vl:3b`。检测、裁剪和识别在后台串行运行，不阻塞实时预览。
台面增强按颜色分别提取前景；灰白物体需要先采集空台面背景，同色相接物体仍可能合框。
YOLO-World 仍可在下拉框选择，但现有积木样本漏检明显，不再作为默认入口。

自动框不完整时点击“手工框选识别”：在固定截图上左键拖框逐件添加，右键撤销，
点击“按这些框逐件识别”。此模式使用全图坐标并忽略原ROI，只对手选框内物体识别；
取消窗口不会发起识别。尽量框完整物体，不包含旁边物体。最多20件。
模型返回空内容或非法JSON时自动重试一次，并将原始回复保存在 `logs/recognition-invalid-*.json`。

1. 选择摄像头，必要时旋转画面，再沿台面边缘依次点击四个顶点。
2. 输入英文检测提示词，点击“预览物体分离”。确认每个目标完整地对应一个框。
3. 点击“识别当前画面”，输出名称、分类、颜色、几何类型、像素中心和不确定提示。
4. 打开 `../ghr/10.15/上位机/run_host.cmd`，进入“视觉目标”，点击“读取最新视觉结果（不运动）”。

YOLO-World 是文字提示检测器，不能保证检测所有未知物体。默认提示词只是起点；
本机现有积木照片存在明显漏检、局部框和合并框，不能把检测数量视为实际物体数量。
降低置信度会增加误检。可切换“台面分离”比较效果：采集空台面背景可检测中性颜色物体；
不采集背景时仅按颜色提取候选。相机、光照或夹爪改变后应重新采背景。

每批使用一张固定截图，识别期间相机预览继续变化；底部结果只对应这张截图。
物体或相机移动后须重新识别。连续模式会等待当前批次完成，不堆积请求。
旋转或切换相机将清除选区及背景。四点未完成时不开始新识别。
默认裁剪最长边640像素；可开启高清到1280，或开启可见面/棱分析，二者可能增加耗时。

### 输出与坐标

批次目录 `samples/<batch_id>/` 保存原图 `frame.jpg`、带框/中心的 `overview.jpg`、
候选掩码 `mask.png`、每个物体的 `object-*.jpg` 和 `results.json`。
Qwen 读取无诊断线条的裁剪图，输出记录另追加到 `data/recognitions.jsonl`。
YOLO 框内可能包含邻近物体；这不是像素级实例分割。

`data/vision_targets.json` 是原子替换的最新批次数据，上位机手动读取，视觉程序不打开串口。
字段包括：`schema_version=1`、`captured_at`（含时区）、相机、图像宽高、旋转、ROI、
`bbox=[x,y,w,h]`、`center_px=[x,y]`、检测提示词/置信度，以及 Qwen 结果。
坐标原点为**旋转后完整图像左上角**，x向右、y向下；ROI检测坐标会加回裁剪偏移。
上位机验证坐标范围并标记旧截图、碰边、不确定和离线样本。

框中心仅为定位候选，凹形物体的框中心可能落在空洞，不能直接作为夹持点。
当前导出始终为 `motion_authorized=false`、`calibration_status=missing`，没有自动抓取入口。
实际抓取还缺相机到台面的标定、X/Y毫米与运动脉冲换算、机械原点、行程范围、
安全Z及物体高度/抓取深度、夹爪姿态与放置位置。编码器回读计数与运动脉冲不是同一单位。

### 运行环境与验证

所有视觉依赖位于 `.venv`，模型位于 `models/yolo`、`models`，文字编码器位于 `cache/clip`。
YOLO 使用 `ultralytics==8.3.228`、`yolov8s-worldv2.pt`；RTX 5060 使用
`torch==2.7.1+cu128` 与 `torchvision==0.22.1+cu128`。提示词改变才重新编码，
完成后释放文字编码器，YOLO权重/提示向量复用，为Qwen留出显存。

```powershell
.\.venv\Scripts\python.exe -B test_vision_pipeline.py
.\.venv\Scripts\python.exe benchmark_vision.py samples/camera-20261017-195903-607466.jpg --recognize
```

基准结果保存到 `samples/yolo-validation/benchmark.json`。这是离线单图测试，不能代表
现场准确率或整条识别链的帧率；多物体Qwen耗时随候选数量累加。

本目录集中保存 Ollama 便携程序、Qwen2.5-VL 3B 模型、Python 独立环境、日志和识别记录。
使用现有 Python 3.12 创建 `.venv`，不向系统 Python 安装依赖，不修改系统 PATH。

## 目录

- `runtime/`：Ollama 官方 Windows AMD64 便携程序，固定 v0.40.5，仅提取 CUDA 13、CPU 和公共文件，适配本机 NVIDIA 驱动。
- `models/`：官方 `qwen2.5vl:3b` 模型，模型层约 3.2 GB。
- `.venv/`：本项目 Python 环境，识别脚本仅使用标准库。
- `downloads/`：下载缓存与可恢复分段，部署过程需要额外磁盘空间。
- `home/`：Ollama 进程使用的用户目录。
- `logs/`：服务日志。
- `samples/`：测试照片。
- `data/recognitions.jsonl`：每次识别结果；仅表示识别，不表示抓取成功。

以上运行目录已在本目录 `.gitignore` 排除，不提交大模型、程序或识别照片。

## 安装或恢复下载

在本目录打开 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

首次下载需要联网。分段可恢复；模型下载后验证 SHA256，程序文件提取时验证对应 ZIP 条目的 CRC。
程序下载采用官方 ZIP 的 HTTP Range，只取本机所需后端；`downloads/ollama-portable-complete.zip`
是用于提取的稀疏包，未下载的后端区域留空，不应作为完整安装包分发。
安装完成后，图片推理只访问本机接口，不上传到云端。

## 启动服务

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

服务在后台运行，只监听 `127.0.0.1:11434`，未设置开机自启。
启动脚本在子进程中设置模型、临时文件和用户目录，不修改 Windows 的永久环境变量。
若端口已有其他 Ollama 服务，不自动接管；须核对它的模型目录。

## 识别单件物品照片

已完成 GPU 图片推理验证，详见 `deployment-status.md`。可先运行自带样图：

```powershell
.\recognize.cmd .\samples\dog-448.jpg --categories 动物 工具 文具 生活用品
```

先把一件物品拍清楚，背景尽量简单；一张图包含多个物品时，应先裁剪为单件图片。

```powershell
.\recognize.cmd "D:\path\item.jpg"
.\recognize.cmd "D:\path\item.jpg" --categories 工具 文具 生活用品
```

类别应替换成比赛规定的类别。未指定时输出通用类别。
返回名称、类别、不确定标记、可见依据和耗时，自动追加保存到 `data/recognitions.jsonl`。
模型的“不确定”判断不等于校准后的概率，也不保证所有未知物品都能正确识别。

## 接入龙门架

视觉程序可以导入 `recognize.py` 的 `recognize(image, categories)`。
必须在后台线程或独立进程中调用，避免阻塞上位机心跳和界面。
本模块只识别物品；分割、像素到机械坐标标定、抓取点与运动控制需要另外实现。

## 查看 GPU 和服务

```powershell
.\runtime\ollama.exe ps
Get-Content .\logs\server.stderr.log -Tail 30
```

## 停止模型或服务

释放显存、保留服务：

```powershell
.\runtime\ollama.exe stop qwen2.5vl:3b
```

停止本目录启动的整个服务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\stop.ps1
```

## 来源

- https://github.com/ollama/ollama/releases/tag/v0.40.5
- https://ollama.com/library/qwen2.5vl:3b

实际部署验证结果见 `deployment-status.md`；仅有下载文件不表示模型已可用。
