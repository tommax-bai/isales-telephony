# Windows 安装指南 — D1 PoC 跑前准备

本文档是 [README.md](README.md) 的姐妹篇，专门讲**从一台干净 Windows PC 到三个
PoC 能跑起来**之间的所有安装步骤。README.md 假设环境已就位；本文档不假设。

目标读者：第一次在这台 Windows PC 上做 D1 PoC 实测的人。

## 总览

四类东西要装：

| # | 项 | 装一次 / 每次 | 下载方式 |
|---|---|---|---|
| 1 | 系统级运行环境（Python / VC Build Tools / VC Redist / 可选 Git） | 装一次 | 公网下载 |
| 2 | PoC 脚本本身 | 每次更新拉一次 | GitHub clone 或 zip 拷贝 |
| 3 | Python 包（sounddevice / PySide6 等） | 每次新建 venv 装一次 | pip / PyPI |
| 4 | 阿里 ARTC SDK Windows Python（仅 PoC 3 用） | 装一次 | **阿里云控台**（需登录账号） |

预计**首次安装总耗时 ≈ 45 分钟**（下载 + VS Build Tools 装得最久 ≈ 15 min）；之后
重新进 venv 跑 PoC ≈ 5 分钟。

## 第 1 步：系统级运行环境

### 1.1 Python 3.12

1. 浏览器打开 https://www.python.org/downloads/windows/
2. 找最新 **Python 3.12.x**（不要 3.13；qasync / PySide6 当前对 3.12 适配最稳）
3. 下载 **「Windows installer (64-bit)」** —— 文件名形如
   `python-3.12.x-amd64.exe`
4. 双击运行安装器，**第一屏必须勾选两项**：
   - ☑ Add python.exe to PATH （否则后面 `python` 命令找不到）
   - ☑ Install launcher for all users（可选，建议勾上）
5. 点 「Install Now」用默认设置安装即可
6. 安装完成后，**新开**一个 PowerShell 窗口（关键：旧窗口不会重读 PATH），
   验证：

   ```powershell
   python --version
   # 应输出 Python 3.12.x
   pip --version
   # 应输出 pip 23.x 或更新
   ```

### 1.2 Visual Studio Build Tools 2022

为什么需要：`sounddevice` / `pywin32` / `Pillow` 在 PyPI 上有 Windows 预编译
wheel 多数情况能直接装，但少数 Python 子版本组合下 pip 会回退到 sdist 源码编
译，缺 MSVC 就当场失败。装一次省后顾之忧。

1. 浏览器打开 https://visualstudio.microsoft.com/visual-cpp-build-tools/
2. 点 「Download Build Tools」下载安装器（文件名 `vs_BuildTools.exe`）
3. 双击运行，**在「工作负载」标签页勾选 ☑ Desktop development with C++**
   （右栏自动选中：MSVC v143 + Windows 11 SDK + CMake tools 等）
4. 右下角「Install」开装，**约 6-10 GB 磁盘 + 10-15 分钟下载**
5. 装完不需要重启，但**新开 PowerShell** 窗口让环境变量生效

> 如果你已经装过 Visual Studio 2019/2022（社区版 / 专业版），可以跳过这步 ——
> 已经包含同样的 MSVC 工具链。

### 1.3 Microsoft Visual C++ Redistributable

为什么需要：PoC 3 跑 PyInstaller 打包出来的 `.exe` 时，里面的 C 扩展（含
ARTC SDK 的 `.dll`）依赖 VC++ Runtime。Build Tools 装好后 dev 机器自带，但
**目标用户机器**默认没有；先在本机装一份确认 PoC 3 frozen exe 能跑。

1. 浏览器打开 https://aka.ms/vs/17/release/vc_redist.x64.exe （直链 .exe）
2. 双击运行，下一步下一步默认装即可（约 30 MB）
3. 装完无感（不会提示）

### 1.4 Git for Windows（可选）

只在你想用 `git clone` 拉 isales-telephony 仓时需要。如果用 zip 拷贝方案（见
第 2 步），可以跳过。

1. 浏览器打开 https://git-scm.com/download/win
2. 自动开始下载安装器
3. 双击运行，**所有选项保持默认**即可（一路 Next）
4. 装完验证：

   ```powershell
   git --version
   # 应输出 git version 2.4x.x
   ```

## 第 2 步：把 PoC 脚本搞到 Windows 机上

三个 PoC 脚本 + spec + README 都在 isales-telephony 仓的 `scripts/d1_poc/`
目录下。三选一：

### 方案 A：git clone（推荐，便于拉更新）

```powershell
cd C:\Users\<your-name>\Desktop
git clone https://github.com/tommax-bai/isales-telephony.git
cd isales-telephony
```

如果 https clone 提示要登录而你没配 token：用 SSH key（在 dev 机生成 + 加到
GitHub）或换方案 B。

### 方案 B：从 Mac dev 机 zip 拷贝（最简单，免 GitHub 配置）

在你的 Mac 上：

```bash
cd ~/codes/isales-telephony
zip -r /tmp/d1_poc.zip scripts/d1_poc/
# 然后用任意方式发到 Windows：
#  - 微信传文件 / 阿里云盘 / U 盘 / scp / 局域网共享
```

在 Windows 上解压后，目录看起来应该是：

```
C:\Users\<you>\Desktop\d1_poc\
├── README.md
├── WINDOWS_SETUP.md        (本文件)
├── poc_wasapi_latency.py
├── poc_tray_qasync.py
└── poc_pyinstaller_artc\
    ├── hello_artc.py
    └── hello_artc.spec
```

### 方案 C：直接下载 GitHub 上的 zip

浏览器打开
https://github.com/tommax-bai/isales-telephony/archive/refs/heads/main.zip
下载整个仓的 zip，解压后取 `scripts/d1_poc/` 目录即可。

## 第 3 步：建 venv 装 Python 依赖

无论上一步用哪种方案，下面的命令都在**含 PoC 脚本的目录**里跑（如方案 A 是
`isales-telephony` 根目录；方案 B/C 是解压后含 `poc_*.py` 的目录）。

```powershell
# 新建一个 venv（隔离依赖，不污染系统 Python）
python -m venv .venv

# 激活 venv（提示符前会出现 (.venv) 前缀）
.\.venv\Scripts\activate

# 升级 pip
python -m pip install --upgrade pip

# 装 PoC 用的所有包（约 80 MB 下载，含 PySide6 60+ MB）
python -m pip install sounddevice numpy pystray qasync PySide6 Pillow pyinstaller
```

### 国内网络慢？换阿里云镜像

如果上面这条 pip install 卡在 PySide6 / numpy 等大包上不动 / 超时，换镜像：

```powershell
python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ ^
  sounddevice numpy pystray qasync PySide6 Pillow pyinstaller
```

（注意 `^` 是 PowerShell 续行符。也可以一行写。）

### 验证装好了

```powershell
python -c "import sounddevice, pystray, qasync, PySide6, PIL, PyInstaller; print('all OK')"
# 应输出: all OK
```

## 第 4 步：阿里 ARTC SDK Windows Python（仅 PoC 3 用）

> **PoC 1 / PoC 2 不需要这一步**；PoC 3 没有 SDK 也能跑 "PARTIAL 模式"（只
> 验证 PyInstaller 打包 + frozen exe 能不能找到非 SDK 依赖），但要拿到 PASS
> 必须有 SDK。如果只是想先把 PoC 1 / 2 跑了，可以跳过这步。

### 4.1 登录阿里云控台

浏览器打开 https://rtc.console.aliyun.com/ —— 用你已有的阿里云账号登录（D1
和 A2 共用同一个账号，因为 RTC AppId 后面 ECS engine 也要用）。

### 4.2 找 Windows Python SDK 下载入口

阿里云控台菜单结构可能随时间变化；下面三条路任一可达：

- **路径 A**（推荐）：左侧菜单 → 「SDK 下载」/「Downloads」→ 找
  「Windows」标签 → 找 **「Python SDK」** 子项
- **路径 B**：左侧菜单 → 「文档」/「Help」→ 「服务端 SDK」→ Windows Python
- **路径 C**：直接 Google「阿里云 RTC Windows Python SDK 下载」找到官方文档
  页，文档页内会有控台 deeplink

下载文件通常是 `.zip` 或 `.tar.gz`，大小 ≈ 50-100 MB，文件名形如：
`AliRTCSDK_Windows_Python_x.y.z.zip`。

### 4.3 解压到本地目录

解压到任一目录，例如：

```
C:\Users\<you>\Desktop\aliyun-artc-windows-python\
```

解压后里面应有：

- 一组 `.dll` 文件（如 `AliRtcSDK.dll`、`AliRtcRoom.dll` 等）
- 一组 `.pyd` 文件（Python C 扩展）
- 一组 `.py` 文件（Python wrapper，含 `__init__.py` / `aliyun_rtc_sdk.py` 等）
- 可能附带 `README.md` / `examples/` 目录

记下这个目录的**绝对路径**，PoC 3 跑前要设环境变量指向它。

### 4.4 找不到 Windows Python SDK 怎么办

阿里云控台某些时段 / 某些区域只显示 C++ / Electron / iOS / Android SDK，没
Python。两个补救方向：

1. **提工单**：控台右上角「工单」→ 提工单内容用 meta repo 里
   `openspec/v1-roadmap-aliyun-rtc-poc.md` § 9 的三题工单稿（其中第三题就是问
   Windows Python SDK 可获取性）
2. **PoC 3 跑 PARTIAL 模式**：不设 `ISALES_RTC_SDK_WINDOWS_PATH`，跑出来
   verdict 是 `PARTIAL`（PyInstaller 打包 + frozen exe 启动 + 非 SDK 依赖
   都验证 OK，只是 ARTC SDK 加载未实测）。这个结果对 D1 7.x design 决策**
   是足够的**（PARTIAL = PyInstaller 路径基本可用，等 SDK 拿到后补一次完整
   测即可）

## 第 5 步：跑 PoC

按 [README.md](README.md) 的「跑 PoC」一节走。摘要：

```powershell
# 确认 venv 还激活（提示符有 (.venv)），否则重新跑 .\.venv\Scripts\activate

# PoC 1 — 约 3 分钟
python scripts\d1_poc\poc_wasapi_latency.py
# → 当前目录生成 poc_wasapi_latency_result.json

# PoC 2 — 整 10 分钟（中途别动鼠标按托盘，看着就行）
python scripts\d1_poc\poc_tray_qasync.py
# → 右下角托盘出现绿点；每 30 秒小窗自动 show/hide；10 分钟后生成
#   poc_tray_qasync_result.json

# PoC 3 — 约 5 分钟（含 PyInstaller 打包）
# 设 SDK 路径（用第 4 步记下的绝对路径）
$env:ISALES_RTC_SDK_WINDOWS_PATH = "C:\Users\<you>\Desktop\aliyun-artc-windows-python"
# 先 venv 直接跑一遍（验证 SDK 本身能 import）
python scripts\d1_poc\poc_pyinstaller_artc\hello_artc.py
# 然后 PyInstaller 打包
pyinstaller scripts\d1_poc\poc_pyinstaller_artc\hello_artc.spec
# 跑 frozen exe，stdout 重定向到 JSON
dist\hello_artc\hello_artc.exe > poc_pyinstaller_artc_result.json
```

> **路径细节**：上面命令假设你当前在含 `scripts\` 目录的根（方案 A
> isales-telephony 根 / 方案 B/C 解压目录）。如果你把 d1_poc 目录单独拷出
> 来，路径里的 `scripts\d1_poc\` 前缀去掉。

跑完三份 JSON：

- `poc_wasapi_latency_result.json`
- `poc_tray_qasync_result.json`
- `poc_pyinstaller_artc_result.json`

把这三份发回给 Claude（或贴到 chat），决定 D1 4.x / 5.x / 6.x / 7.x 是按
现有 design 直接写还是先更新 design.md 再写。

## 排错速查

| 症状 | 排查 |
|---|---|
| `python` 命令找不到 | 第 1.1 步 Python 安装时没勾 PATH；重装勾选，或手工把 `C:\Users\<you>\AppData\Local\Programs\Python\Python312\` 加进系统 PATH |
| `pip install sounddevice` 报缺 `cl.exe` / `Microsoft Visual C++ 14.0` | 第 1.2 步 VS Build Tools 没装 C++ 工作负载；重装勾上 |
| `pip install PySide6` 卡 / 超时 | 用阿里云镜像（第 3 步附录） |
| PoC 1 报 `WASAPI host API not found` | 你不在 Windows 上（macOS / Linux 不行）；或 sounddevice 版本太老（升级 pip 再重装） |
| PoC 1 所有 mode 都 FAIL，xrun 全 -1 | sounddevice 设备未识别；插一个 USB 麦克风或 USB 音频设备再重试 |
| PoC 2 跑 1 分钟就崩 + traceback 含 `QCoreApplication: was not created in the main thread` | 不要在 IDE / Jupyter 里跑 PoC 2，必须从 PowerShell / cmd 直接 `python` |
| PoC 2 右下角看不到托盘图标 | Windows 11 默认折叠所有 tray icon；点托盘的「^」展开 / 系统设置 → 个性化 → 任务栏 → 设「始终显示」 |
| `pyinstaller hello_artc.spec` 报缺模块 | spec 文件里 `hiddenimports` 没覆盖到实际的 SDK 模块名；按 README.md 「跑不出 PASS 时的排查」第 1 条改 |
| frozen `hello_artc.exe` 报缺 DLL | 第 1.3 步 VC++ Redistributable 没装；或 SDK 的 `.dll` 没被 datas 拷进 `_internal\vendor\` |
| 系统弹 Windows Defender SmartScreen「未识别的应用」拦 frozen exe | 点「更多信息」→「仍要运行」（这是 D1 已知问题，D3 阶段 EV 签名解决） |

## 跑完 PoC 后

如果三个 PoC 全 PASS：D1 4.x / 5.x / 6.x / 7.x 按现有 design 直接写，继续
推 D1 实装。

如果任何一个 FAIL / PARTIAL：把对应 JSON 的 `verdict.summary` 字段贴回 chat，
Claude 会按下表更新对应 design 章节再继续写代码：

| PoC | FAIL 触发更新 |
|---|---|
| 2.1 | design Decision 3 + 4 latency budget + Risks 第 1 条 |
| 2.3 | design Decision 1 + Risks 第 4 条 + 第 5 条 |
| 2.4 | design Decision 4 + Risks 第 2 条 |

跑完 PoC 这台 Windows 机就为 D1 后续 4.x-7.x 任务**热身好了**（venv +
环境 + SDK 都在）。等 ARTC SDK 可用 + design 决议清晰后，直接用同一 venv 写
4.x WASAPI / 5.x tray / 6.x main_windows / 7.x PyInstaller 实装代码。
