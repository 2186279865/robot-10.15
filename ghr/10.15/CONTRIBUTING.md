# 10.15 工程 —— 联合开发教程

> 🔰 **第一次参与、还没有 GitHub 账号**：请看 [新手联合开发完整教程.md](新手联合开发完整教程.md)，
> 那份从注册账号开始一步步讲，零基础也能跟。
>
> 本文是**精简版**，适合已经配好环境、只需要查协作规范和项目铁律的人。

> 仓库：https://github.com/2186279865/robot-10.15
> 适用：STM32F407ZGT6 核心板（鹿小班 LXB407ZG-P1）+ Keil MDK + ST-Link
>
> 本文写给**队友**：按第 1~5 章做完就能开始写代码；第 6 章之后的"三条铁律"和"编码约定"**务必先看**，否则很容易把工程改坏。

---

## 1. 协作方式（先确定这一条）

### 推荐：加为 Collaborator，双方都能直接 push

1. 仓库负责人打开 https://github.com/2186279865/robot-10.15/settings/access
2. `Add people` → 输入队友的 GitHub 用户名 → 角色选 **Write**（能推代码，不能改仓库设置）
3. 队友在邮箱里接受邀请

**注意**：这个仓库用的是**个人账号**（用户名 `2186279865`），不是 GitHub Organization。
如果你们共用这个账号，那就等于共用身份，无法区分谁提交了什么——**建议队友注册自己的 GitHub 账号**，再按上面加为协作者。

### 备选：Fork + Pull Request

适合队友暂时不想被加进来、或者你想审核每一处改动：

1. 队友在仓库页面点 `Fork`
2. 改完后往自己 fork 推，再开 Pull Request 给你合并

小团队、节奏快的话，**直接用 Collaborator 更省事**。

---

## 2. 队友的首次环境准备

### 2.1 需要装的软件

| 软件 | 版本 | 说明 |
|---|---|---|
| Keil MDK-ARM | V5.39（与现有工程一致即可） | 编译器 **ARMCC V5.06 update 6**，不要换 AC6 |
| ST-Link 驱动 | 任意较新版 | 下载调试用 |
| Git for Windows | 2.x | 含 Git Bash |
| STM32F4xx_DFP | 3.1.1 | Keil 里按需安装器件支持包 |

### 2.2 配置 Git 身份并生成 SSH 密钥

> 队友在自己电脑上执行，**不要用负责人的密钥**。

```bash
git config --global user.name  "队友的名字或GitHub用户名"
git config --global user.email "队友的邮箱"
git config --global core.quotepath false      # 中文文件名正常显示

ssh-keygen -t ed25519 -C "队友的邮箱"          # 一路回车，口令可留空
cat ~/.ssh/id_ed25519.pub                      # 复制输出的整行
```

把公钥贴到 **自己的 GitHub**：https://github.com/settings/ssh/new → `Add SSH key`。

验证：

```bash
ssh -T git@github.com
# 出现 "Hi 队友用户名! You've successfully authenticated" 即成功
```

### 2.3 克隆工程

```bash
cd 你放工程的目录
git clone git@github.com:2186279865/robot-10.15.git
cd robot-10.15
```

---

## 3. 第一次编译前要做的两件事

仓库里**故意不包含**两类文件，首次打开工程需要自己补：

### 3.1 Keil 的调试/下载设置是本机私有的

以下文件被 `.gitignore` 排除，因为每台电脑的 ST-Link 序列号、调试配置都不同：

```
Project/STM32F407.uvoptx        ← 每个队友各不相同，不入库
Project/STM32F407.uvguix.*      ← 界面布局
Project/DebugConfig/            ← 调试器配置
Project/JLinkSettings.ini
```

所以队友第一次打开 `Project/STM32F407.uvprojx` 后，要自己设一次：

1. `Options for Target` → `Debug` → 选 **ST-Link Debugger** → `Settings` → 确认能识别到芯片
2. `Utilities` → 勾 `Use Debug Driver` → `Settings` → 确认 Flash Download 里有 **STM32F4xx_1024** 算法
3. `Options for Target` → `Output` → 确认 **Create HEX File** 勾上

设完这些是**本机生效**，不会提交，也不会和负责人冲突。

### 3.2 编译产物本来就不入库

`Objects/`、`Listings/`、`*.o`、`*.hex`、`*.axf` 等全部被忽略。第一次 `Rebuild` 会自动生成。
**看到这些文件显示为"未跟踪"不要去 add 它们**（其实 `.gitignore` 已经挡住了，正常状态下 `git status` 里根本看不到）。

---

## 4. 日常开发流程（每天照这个做）

```bash
# ① 开工前：先拉最新代码，避免冲突
git pull

# ② 写代码、在 Keil 里 Rebuild、下载到板子验证

# ③ 看自己改了什么
git status
git diff

# ④ 只提交源码，提交信息写清楚"改了什么 + 为什么"
git add -A
git commit -m "按键改为PA0下降沿触发，增加20ms消抖"

# ⑤ 推上去，队友才能看到
git push
```

**关键习惯**：

- **开工前先 `git pull`**，收工后 `git push`。两个人同时改同一个文件就一定会冲突。
- **一个可用的状态就提交一次**。以前手建的 `backup_before_xxx` 目录不再需要了，git 就是你的备份。
- **提交信息写现状和原因**，不要写"更新""修改"这种废话。

---

## 5. 冲突处理

两个人改了同一个文件，`git pull` 时会提示冲突。处理步骤：

```bash
git pull                     # 提示 CONFLICT

git status                   # 会列出 "both modified: xxx.c"

# 打开冲突文件，会看到这样的标记：
#   <<<<<<< HEAD
#   你的版本
#   =======
#   队友的版本
#   >>>>>>> origin/main
# 手动改成最终想要的代码，删掉 <<<<<<< ======= >>>>>>> 这三行标记

git add xxx.c
git commit -m "合并：解决 xxx.c 的按键逻辑冲突"
git push
```

**建议**：改之前先在群里说一句"我要改 `MY/main.c`"，比事后解冲突省事得多。

---

## 6. ⚠️ 三条铁律（这个工程特有的坑）

### 铁律一：`main.c` 里的两行 `#include` 是模块的实现，别乱动

看 `MY/main.c` 开头：

```c
#include "gripper.h"
#include "key.h"
#include "stepper.h"
...
#include "gripper_inline.h"    // ← 这两行才是"实现代码"
#include "key_inline.h"
#include "stepper_inline.h"
```

**为什么这么设计**：Keil 会在保存时用它内存里的旧文件列表覆盖 `Project/STM32F407.uvprojx`，导致从外部加进去的 `.c` 文件不参与编译、链接时报

```
Error: L6218E: Undefined symbol Gripper_Grab (referred from main.o)
```

所以现在把模块实现放在 `Drive/Include/*_inline.h` 里，由 `main.c` 编译时带进来，**不需要改工程文件**。

**后果**：`Drive/Source/` 下的 `gripper.c`、`stepper.c` 是**参考副本，当前不参与编译**。

👉 **队友注意**：
- 不要顺手把 `gripper.c` / `stepper.c` 加进 Keil 工程 —— 一旦它们参与编译，会和 `*_inline.h` 里的实现**重复定义**，链接报错。
- 也不要删掉 `main.c` 里的 `*_inline.h` 那几行，否则 `Gripper_*` / `Stepper_*` 全部变成 undefined。
- 真要改成"独立 .c 文件"的正式结构，**先在群里说**，需要同时做三件事：Keil 里加文件 + 删掉 `main.c` 的 inline include + 补 `stepper.c` 的 `#include "servo.h"`。这几步要一次做完，不能分两次提交。

### 铁律二：不要在没有沟通的情况下改动 Keil 工程文件

`Project/STM32F407.uvprojx`（目标配置、文件列表、优化等级）和 `.uvoptx`（本机调试设置）**都入了库**。

- **一边开着 Keil、一边在别处改 `.uvprojx`，你的修改会被 Keil 覆盖掉。**
- 要加新文件进工程，**必须通过 Keil 界面**：右键分组 → `Add Existing Files to Group ...`，然后立刻提交这个 `.uvprojx` 改动。
- 发现 `.uvprojx` 出现莫名其妙的 diff，先别提交，在群里确认是不是有人同时在改。

### 铁律三：源文件是**混合编码**，编辑时保持原样

工程里两种编码同时存在（中文注释）：

| 文件 | 编码 |
|---|---|
| `Drive/Source/delay.c`、`led.c`、`Drive/Include/delay.h`、`led.h`、`MY/stm32f4xx_it.c` | **GBK** |
| `MY/main.c`、`Drive/Include/servo.h`、`gripper*.h`、`key*.h`、`stepper*.h`、`Drive/Source/servo.c`、`gripper.c`、`stepper.c` | **UTF-8** |

ARMCC 对两种编码都能编译，**所以不要为了"统一"去批量转码**，那会把所有人的 diff 变成整个文件重写，历史全乱。

👉 **队友注意**：
- **用 Keil 编辑最安全**，它保持文件原有编码。
- 用 VS Code 时**不要用"Save with Encoding"换编码**；建议在设置里固定 `"files.encoding": "gbk"` 或对 GBK 文件用右下角的编码指示器确认。
- 提交前用 `git diff` 扫一眼：**如果整个文件都变红了，说明编码被改了，撤销重来**。
- 新增文件建议统一用 **UTF-8**，避免继续混下去。

---

## 7. 硬件引脚分配（写代码前先对齐）

| 功能 | 引脚 | 外设 | 说明 |
|---|---|---|---|
| 舵机（抓夹） | **PB6** | TIM4_CH1，50Hz PWM | 500~2500us 对应 0~270°。上电停在松开位置 |
| 按键 | **PA0** | GPIO 输入，**内部下拉** | **按下读到高电平**（与例36 一致，注意别按例35 的上拉写法改） |
| 步进电机（EMM V5） | **PA2** | USART2 TX，115200 8N1 | 只发不收；PA3 留空。多台驱动器挂同总线靠**地址**区分（1~4） |
| 板载 LED | **PC13** | GPIO 输出，低电平点亮 | 状态指示 |

**电源**：步进驱动器需要独立的 12~48V 电机电源，并且**必须和 STM32 共地**；舵机用独立 4.8~7.4V。

---

## 8. 新增一个模块的规范

照现有 4 个模块的样子做（以 `stepper` 为例）：

```
Drive/Include/xxx.h          接口声明 + 参数宏（不要放实现）
Drive/Include/xxx_inline.h   实现（当前实际参与编译的就是这个）
Drive/Source/xxx.c           独立实现副本，当前不参与编译，供以后正式拆分
MY/main.c                    只调用接口，不写角度/协议/时序细节
```

具体要求：

1. **接口和实现分开**：`main.c` 里只出现 `Xxx_Init()`、`Xxx_Do()` 这类调用。
2. **参数集中放头文件顶部**，用宏定义，例如 `#define XXX_STEP_DEG 45U`，方便调参不用翻实现。
3. **写清注释**：这个模块占用了哪个外设/引脚、为什么这么写、和哪个示例对照过。
4. **提交前用 armcc 单独编译一次**，确认 `0 error, 0 warning`（Keil 里 Build 也行）。

---

## 9. 常用 Git 命令速查

```bash
git status                      # 现在改了什么
git diff                        # 具体改动内容
git diff --stat                 # 只看改了哪些文件、多少行
git add -A && git commit -m "说明"
git push                        # 推到 GitHub
git pull                        # 拉队友的改动

git log --oneline -10           # 最近 10 次提交
git log --oneline -- MY/main.c  # 只看某个文件的历史
git show <哈希>                  # 看某次提交改了什么

git checkout -- 文件             # 丢弃某个文件的未提交改动
git reset --hard HEAD           # 丢弃所有未提交改动（危险）
git checkout <哈希> -- 文件      # 把某个文件恢复到历史版本
git reset --hard <哈希>          # 整个工程回退到某个提交（危险）

git stash                       # 临时收起改动（要切分支时用）
git stash pop                   # 恢复
```

---

## 10. 提交前自检清单

- [ ] Keil 里 **Rebuild 通过**，`0 Error(s), 0 Warning(s)`
- [ ] 下载到板子**实际验证过**（舵机/按键/电机动作符合预期）
- [ ] `git status` 里**没有** `Objects/`、`*.hex`、`*.axf` 之类的编译产物
- [ ] **没有提交** `*_inline.h` 的重复实现、私钥（`id_ed25519*`）、临时文件
- [ ] `git diff` 里**没有整个文件变红**（编码被改的信号）
- [ ] 提交信息说明了"改了什么 + 为什么"
- [ ] 如果动了 `.uvprojx` 或 `MY/main.c`，**已在群里告知队友**

---

## 附：出问题时的排查顺序

| 现象 | 先查这里 |
|---|---|
| 链接报 `Undefined symbol Gripper_*` / `Stepper_*` | `main.c` 里的 `*_inline.h` include 是否被删了；工程里是不是多了 `gripper.c`/`stepper.c` 参与编译 |
| 链接报 `Multiply defined symbol` | `*_inline.h` 和对应的 `.c` **同时**参与编译了，删掉一个 |
| 中文注释变乱码 | 文件编码被改了，`git checkout -- 文件` 恢复 |
| `git push` 报 `Permission denied (publickey)` | 自己的 SSH 公钥没加到 GitHub，或用了负责人的密钥 |
| `git push` 被拒绝，提示 `non-fast-forward` | 队友先推了，先 `git pull --rebase` 再推 |
| 打开工程后编译一堆报错 | 先 `Rebuild`；再确认 `Options for Target` 里的芯片型号是 **STM32F407ZGTx**、编译器是 **ARMCC V5.06** |
| 板子下载不了 | ST-Link 驱动 / `Debug` 设置 / Flash 算法；这类是本机设置，问队友没用，自己按 3.1 节配 |
