# iSales Windows 边缘客户端部署 RUNBOOK

> **范围**：v1.0 D1 商用 Windows 边缘客户端 — 员工自带 PC + 自插 GSM modem + 自助激活。
>
> **目标读者**：
> - 员工 / 终端用户（首次安装小节）
> - 运维 / boss 视角（签发激活码、排障小节）

---

## 系统要求

| 项目 | 要求 |
|---|---|
| OS | Windows 10 21H2+ (x64) 或 Windows 11 (x64) |
| RAM | ≥ 4 GB |
| 磁盘 | ≥ 200 MB 可用空间（程序 + ARTC SDK + 日志） |
| 网络 | 出站可访 `isales.<your-region>.aliyuncs.com:443` + 阿里 RTC UDP 段 |
| 权限 | 普通用户即可（无需管理员） |
| 硬件 | 1 个 USB 口接 GSM modem（华为 / 中兴 / SIMCom / Quectel 主流） |

不支持：Windows ARM64、Windows Server、Windows 10 LTSC（v1.0 范围外）。

## 首次部署（员工视角，10 步）

1. 在 iSales 控制台 / 微信群 / 短信里拿到一份 **EDGE_DEVICE_TOKEN**（一长串字符）。
2. 下载发布包 `isales-telephony-<timestamp>.zip`（约 100–150 MB）。
3. 把 zip **解压到任意目录**（桌面 / 下载都行，安装脚本会复制到正式位置）。
4. 把 USB GSM modem **插到 PC 的 USB 口**，Windows 自动识别驱动；如果设备管理器里没有显示新 COM 端口，按厂商说明装一次驱动。
5. 在解压目录里 **右键** `install.ps1` → "用 PowerShell 运行"。
   - 弹"无法运行未签名脚本"时，开 PowerShell（菜单搜 PowerShell），运行：
     ```powershell
     pwsh -ExecutionPolicy Bypass -File install.ps1
     ```
6. 安装脚本会：(a) 把程序拷到 `%LOCALAPPDATA%\Programs\isales\` (b) 写注册表登录自启 (c) 启动客户端。
7. **第一次启动**会弹出"iSales 激活"对话框，要求粘贴激活码 + 确认云端地址（默认就行）。
8. 点 **确定** → 等几秒 → 任务栏托盘出现绿色小图标 = 激活成功，云端已连通。
9. 任务栏托盘图标右键，看到 4 项：打开诊断窗口 / 重新激活 / 查看日志 / 退出。
10. 关掉 PowerShell 窗口；客户端会一直在托盘后台跑，**下次开机自动启动**。

## 常见问题

### Q1 Windows Defender / SmartScreen 拦截 `isales-telephony.exe`

**原因**：D1 阶段 exe 没有 EV 签名（D3 引入），SmartScreen 默认拦截未签名可执行。

**绕过步骤**：

1. 右键 `isales-telephony.exe` → 属性 → 底部勾选"取消阻止"→ 确定。
2. 或：SmartScreen 弹窗里点"更多信息" → "仍要运行"。
3. 或：临时关闭 Defender 实时保护，安装完成后再开。

**长期解决**：D3 `windows-installer-and-ota` 引入 EV Authenticode 代码签名后默认不再拦。

### Q2 找不到 modem 音频通道（SerialPcm）

**症状**：托盘图标红色不转绿，或绿了但没声音。

**机制**：v1.0 主 SKU（SIM7600G-H）在 Windows 上**不注册** USB Audio Class endpoint；客户端通过 modem 的 audio COM 串口（SIMCom MI_04，典型 pyserial description 含 "Audio"）以 8 kHz int16 LE 帧 read / write PCM 字节流，per-call 由 `AT+CPCMREG=1/0` 启停（详见 `windows-client-core/design.md` Decision 3 + 8）。

**排查**：

1. 控制面板 → 设备管理器 → 端口（COM 和 LPT），**确认 modem 暴露多个 COM 端口**（典型 SIMCom 5 个：AT / Audio / Diagnostics / NMEA / Modem）。
2. PowerShell 跑 `python -c "import serial.tools.list_ports as lp; [print(p.device, p.description) for p in lp.comports()]"`，确认能看到 description 含 "Audio" 的 COM。
3. 重新插拔 modem，让 Windows 重新枚举（COM 号是动态分配，不能 hardcode 数字）。
4. 客户端日志 `tray 右键 → 查看日志`，搜 `serial_pcm:` / `cpcmreg_`：前者是 backend open 错误（端口冲突 / 不存在），后者是 AT 控制面失败（`CPCMREG_ENABLE` HardwareAlert 上报云端）。
5. 极端情况：发邮件给运维，附上日志目录最近一份 `telephony.log` + 设备管理器截图。

### Q3 多 COM 端口 modem 识别异常

**症状**：modem 插上 PC 后 Windows 设备管理器出 3 个 COM 端口，客户端识别到的可能不对。

**机制**：D1 客户端用 USB VID:PID 白名单 + AT 命令试探联合判定 AT 通道。白名单见 `isales_telephony/modem_controller/udev_watcher.py::GSM_MODEM_WHITELIST`。

**排查**：

1. 拔 modem 看 COM 端口列表少了哪几个 → 确认 modem 对应的 COM 范围。
2. 客户端日志 `tray 右键 → 查看日志`，搜 `identify_modem`，找哪个 COM 试探到了 `OK`。
3. 如果白名单没命中 modem 但 modem 本身正常（厂商太新），客户端会走 AT 试探慢路径（每 COM 每分钟限 1 次），可能等 1–2 分钟才识别。
4. 极端情况：发邮件给运维，附上 `查看日志` 出来的日志目录最近一份 `telephony.log` + 设备管理器截图。

### Q4 重新激活 / 换 PC / 换 modem

- **换激活码**（云端把旧 token 吊销）：托盘右键 → "重新激活" → 粘贴新 token。
- **换 PC**（员工换设备）：在新 PC 上从第 1 步走一遍即可，旧 PC 跑 `uninstall.ps1` 清理。
- **换 modem / SIM 卡**：直接拔旧的插新的，客户端会自动重新识别（不需要重新激活）。

### Q5 卸载

`%LOCALAPPDATA%\Programs\isales\` 下面有 `uninstall.ps1`：

```powershell
pwsh -ExecutionPolicy Bypass -File uninstall.ps1
# 想彻底清空数据（token + sqlite + logs）：
pwsh -ExecutionPolicy Bypass -File uninstall.ps1 -PurgeAppData
```

---

## 运维 / boss 视角

### 签发激活码（A2 / D1 时点）

A2 + D1 阶段 v1.0 单租户 single-boss，激活码 = cloud-edge 静态 bearer token。生成方式：

```bash
# 云端 isales-api 仓库（或 isales-common 的命令行工具）
python -m isales_api.scripts.mint_edge_token --tenant default --label "<员工姓名>-<PC标识>"
# 输出 64 字符随机字符串 + 在云端登记
```

把字符串通过微信 / 内部 IM 发给员工即可。

> **未来**：C1 `boss-console` 在管理界面加"签发边缘 token"按钮；C2 `multi-tenant-roles-and-leads` 引入"激活码 → 服务端兑换 token + seat 绑定"完整体系。D1 阶段是 minimum viable。

### 远程排障

每台边缘 PC `%APPDATA%\isales\logs\telephony.log` 是滚动日志（D2 `hardware-observability` 会加"一键诊断包"上传 OSS）。D1 阶段员工手动发日志给运维。

边缘到云端是 bidi gRPC，云端 isales-api 端能看到所有在线边缘机的连接状态（A2 已实现），用 `isales-api` 的 `/admin/edge/devices` endpoint 查。

### Windows Defender 企业级 AV 拦得严

如果客户公司装了企业级 AV（CrowdStrike / Carbon Black 等）整体拦未签名 exe：

- 短期 fallback：给该客户走 macOS Mac mini 工程师部署路径（`impl-deploy-macos` 已 ship）
- 长期：D3 EV 签名彻底解决

---

## 升级流程（D1 / D2）

D1 / D2 阶段没有 OTA：

1. 运维下发新版本 zip。
2. 员工在解压目录里重新跑一次 `install.ps1`（脚本会 idempotent 升级、保留现有 token）。
3. 客户端会重启，激活态自动恢复。

D3 `windows-installer-and-ota` 引入 MSI + OTA 后变成"无感后台升级"。

---

## 参考

- 完整 spec：`openspec/changes/archive/2026-MM-DD-windows-client-core/`
- 设计权衡：`openspec/changes/windows-client-core/design.md`
- 故障注入测试：`tasks.md § 9.4`（D1 PoC week 4 手动验收）
- 后续：D2 `hardware-observability`（三态色 + 一键诊断 + SIM 余额）/ D3 `windows-installer-and-ota`（MSI + EV 签名 + OTA）
