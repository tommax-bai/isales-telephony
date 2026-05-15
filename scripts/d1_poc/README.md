# D1 PoC scripts — three技术栈风险点实测

Spec: `openspec/changes/windows-client-core/tasks.md` § 2, `design.md`
Decisions 1 + 3, Risks 列表前三条。

## TL;DR

在一台 Windows 10 21H2+ / Windows 11 PC 上一次性跑完三个脚本，每个输出
一份 JSON。三份 JSON 的 `verdict.pass` 全为 `true` → D1 4.x / 5.x /
6.x / 7.x 按现有 design 落地；任一 `false` → design.md 对应章节的"备
选方案"分支启用，更新 design 再写代码。

## 准备工作

```powershell
# 1. 装 Python 3.12 (https://www.python.org/downloads/)
#    勾选 "Add python.exe to PATH"
# 2. 装 VS Build Tools (C++ 工作负载, 后续 sounddevice / pywin32 编译要用)
#    https://visualstudio.microsoft.com/visual-cpp-build-tools/
# 3. 准备一个干净 venv
cd C:\Users\<you>\Desktop
mkdir isales-d1-poc; cd isales-d1-poc
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install sounddevice numpy pystray qasync PySide6 Pillow pyinstaller
```

把 isales-telephony 仓库 clone 到本地（或者只拷 `scripts/d1_poc/` 这一个
目录过去，三个 PoC 都没有跨文件依赖）。

## PoC 1 — sounddevice WASAPI 延迟（task 2.1）

```powershell
python scripts\d1_poc\poc_wasapi_latency.py --duration 30
# → 输出 poc_wasapi_latency_result.json
```

跑 ≈ 3 分钟（6 组测量 × 30 s）。`verdict.pass` 含义：

- `true`：至少有一组 (mode, blocksize) 满足 P95 jitter ≤ 10 ms + 零
  xrun → 4.1-4.6 按 design Decision 3 落地，shipping 默认值见
  `verdict.recommended_mode` / `recommended_blocksize_ms`
- `false`：design Decision 3 的"备选方案" → 切 Exclusive Mode 默认 /
  评估 PyAudioWPatch / 接受 20 ms+ 底板。需要更新 design + 重新分配
  Decision 4 的 latency budget

### 验收阈值的依据

design.md Decision 3 给出 PortAudio + WASAPI Shared Mode 典型延迟
10–30 ms。10 ms jitter 是这条延迟带的下限——超过它意味着 sounddevice
的 callback 调度本身已经吃掉 latency budget 的一半，端到端 800 ms
budget（Decision 4）就此失守。

## PoC 2 — pystray + qasync + PySide6（task 2.3）

```powershell
python scripts\d1_poc\poc_tray_qasync.py --duration 600
# → 系统托盘出现一个绿点 icon；右键有 "Quit" 菜单
# → 每 30 秒诊断小窗自动 show/hide 一次
# → 跑满 10 分钟（或右键 Quit）后输出 poc_tray_qasync_result.json
```

跑 10 分钟。如果中途出现以下任何一种 → FAIL：

- Python 控制台抛 traceback
- tray icon 消失但 Python 进程仍在跑
- 诊断小窗 show / hide 之后 Qt 主线程卡死

`verdict.pass`：

- `true`：D1 5.x / 6.x 按 design Decision 1 单 Python 栈落地
- `false`：Decision 1 的"备选方案" → 砍 pystray，改用 PySide6 自带的
  `QSystemTrayIcon`（功能略减，但同步性更稳）；需要更新 design Decision
  1 + 重新评估"通知能力"在 D1 是否仍达标

## PoC 3 — PyInstaller + ARTC SDK Windows DLL（task 2.4）

前置：先把阿里云控台下载的 ARTC SDK Windows Python 压缩包解压到磁盘任
一目录，里面应包含 `*.dll` + `*.pyd` + `__init__.py` / wrapper `.py`。

```powershell
# 设 SDK 路径环境变量（spec 自动 pick up）
set ISALES_RTC_SDK_WINDOWS_PATH=C:\Users\<you>\Desktop\aliyun-artc-windows-python

# 先确认 SDK 在 venv 里能直接 import + 创建 engine
python scripts\d1_poc\poc_pyinstaller_artc\hello_artc.py

# 看到 "PASS" 后才打包；如果是 "PARTIAL"（fallback 路径），可以继续
# 打包测 frozen 阶段是否能 import；"FAIL" 则先排查 SDK 自身
pyinstaller scripts\d1_poc\poc_pyinstaller_artc\hello_artc.spec

# 跑 frozen exe
dist\hello_artc\hello_artc.exe
# → 输出 JSON 到 stdout（拷贝出来作为 result）
```

`verdict.pass`（按 hello_artc.py 内嵌的 `verdict` 字段）：

- `PASS`：frozen exe 加载 SDK + 创建 engine + 销毁成功 → D1 7.x
  PyInstaller onedir 路径按 design Decision 4 落地
- `PARTIAL`：frozen exe 工作但 SDK 未 load（脚本没找到 wrapper module
  name 或没设 env 变量）→ 仍可接受，但需要在真正写 4.x 时补 SDK 集成
  实测
- `FAIL`：frozen 后 SDK 加载崩 → Decision 4 备选方案 = 不 freeze，发
  zip + venv 套件（A2 可接受，D3 阶段彻底解决）

### 跑不出 PASS 时的排查

PyInstaller 打包后 DLL 找不到，最常见三种原因：

1. **`hidden_imports` 漏写**：脚本 `import alirtc_sdk` 但 SDK 实际叫
   `AliRtcSdk`（大小写）。把真实模块名加到 `hello_artc.spec` 的
   `hiddenimports` 列表，重打包
2. **DLL 不在 wrapper 同目录**：阿里 SDK 的 wrapper `.py` 用
   `ctypes.WinDLL(os.path.join(here, "AliRtcSDK.dll"))` 找邻接 DLL；
   PyInstaller `datas` 把整个 SDK 目录原样拷过去就够了——确认
   `dist/hello_artc/_internal/vendor/aliyun-artc-windows-python/` 下
   `.dll` 与 `.py` 同级
3. **Microsoft Visual C++ Runtime 缺失**：ARTC SDK 多数是 VC2015+
   编译，frozen exe 跑的目标机要装 [VC++ Redistributable](
   https://aka.ms/vs/17/release/vc_redist.x64.exe)。在 D3 MSI 安装包
   会前置依赖，D1 PoC 阶段先手工装

## 把结果三份 JSON 发回来

跑完三个，把对应目录下的：

- `poc_wasapi_latency_result.json`
- `poc_tray_qasync_result.json`
- frozen exe 跑出来的 PoC 3 stdout（重定向 `> poc_pyinstaller_artc_result.json`）

发给我或贴上来。我根据三个 `verdict.pass` 判断 D1 4.x / 5.x / 6.x /
7.x 是按现有 design 直接写，还是先更新 design.md 再写。

## 不过关时下游 design 章节影响速查

| PoC | `pass=false` 触发更新的 design 章节 | 影响 task 数 |
|---|---|---|
| 2.1 | Decision 3 + Decision 4 latency budget + Risks 第 1 条 | 4.1-4.6（6 个） |
| 2.3 | Decision 1 + Risks 第 4 条 + 第 5 条 | 5.1-5.6（6 个） |
| 2.4 | Decision 4 + Risks 第 2 条 | 7.1-7.6（6 个） |
