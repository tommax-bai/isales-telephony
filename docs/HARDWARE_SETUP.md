# 硬件接入手册（stage 6）

USB GSM modem 接入 isales 主机的步骤、常见问题与排错。覆盖 v1 三种推荐型号：
**Simcom A7670**（首选，库存最稳）/ **SIMCom 800C** / **Quectel UC20 / EC25**。

> 仅 Linux + ALSA + udev。macOS / Windows 不支持真硬件；只能用 fake-modem
> 走 pty 跑单元测试 + IPC 联调（见 `tests/` 与 spec § udev 自动检测流程
> 中关于 `ISALES_SKIP_UDEV` 的说明）。

## 0. 准备

- 一台 Linux 主机（推荐 Ubuntu 22.04 LTS 或 Debian 12）
- 一根 USB-A 转 micro-B 线缆 + USB GSM modem（A7670 / SIM800C / UC20 任一）
- 一张可拨打 SIM 卡（先在普通手机上验证能正常打电话）
- isales-telephony 已部署且 PostgreSQL 可达（连接串可走 `ISALES_DATABASE_URL`）

## 1. 系统包

```bash
sudo apt-get update
sudo apt-get install -y libasound2-dev libudev1 udev
```

## 2. 安装 modem-controller

```bash
cd /opt && sudo git clone <isales-telephony repo> isales-telephony
cd isales-telephony
sudo python3 -m venv /opt/isales/venv
sudo /opt/isales/venv/bin/pip install -e ".[hardware]"
```

`[hardware]` extra 装 pyserial / pyserial-asyncio / pyalsaaudio（仅 Linux）。
注意 macOS / Windows 装这些会因为缺 libasound 失败——别在非 Linux 装 hardware extra。

## 3. udev rules + systemd

仓里 `deploy/install-modem-controller.sh` 是幂等安装脚本，按序：

```bash
sudo bash deploy/install-modem-controller.sh
```

它做的事：
- apt 装系统依赖
- 创建 `isales` 用户 + 加入 `dialout / audio / plugdev` 组
- 创建 `/var/lib/isales/recordings/`（录音落盘点）
- 拷 `deploy/99-isales-modem.rules` → `/etc/udev/rules.d/`，reload udev
- 拷 `deploy/isales-modem-controller.service` → `/etc/systemd/system/`，reload systemd

## 4. 配置文件

写一份 `/etc/isales/modem-controller.env`，systemd unit 会从这里读 env：

```ini
ISALES_DATABASE_URL=postgresql+asyncpg://isales:secret@db:5432/isales
ISALES_REDIS_URL=redis://redis:6379/0
ISALES_API_BASE_URL=http://api:8000
ISALES_MODEM_AUTH_TOKEN=<service-account JWT>
# 设备路径（udev rules 提供的稳定别名）
ISALES_MODEM_SERIAL_PATH=/dev/ttyUSB-isales-modem
ISALES_MODEM_ALSA_CARD=hw:1,0   # 看 `aplay -l` 实际输出
ISALES_MODEM_DRIVER=            # 留空 → AT+GMI/AT+GMM 自动识别
ISALES_MODEM_RECORDING_DIR=/var/lib/isales/recordings
```

## 5. 启动 + 验证

```bash
sudo systemctl enable --now isales-modem-controller
journalctl -fu isales-modem-controller
```

健康检查清单（按这个顺序看日志）：

1. `ipc_server listening on /var/run/isales/modem.sock`（IPC 起来了）
2. udev add 事件触发 `POST /devices`（modem-controller → telephony API）
3. modem.status 经 `detected → registered → idle` 翻转
4. 每 30s 一次 `PATCH /devices/<id>/heartbeat 204`（心跳活）

## 6. 常见问题

### Q1. ALSA 找不到 modem

```
$ aplay -l
**** List of PLAYBACK Hardware Devices ****
card 0: ...
```

modem 不在 list 里：

- 拔插试一下，`dmesg | tail` 看 USB 侧有没有 enumerate
- 一些 modem 默认是 NCM (network) mode，要先 AT 切到 audio profile（A7670 用 `AT+CUSBPIDSWITCH=9001,1,1` reboot）
- 检查 udev rules 里 vendor/product ID 是否正确：`lsusb` 看真实 `idVendor/idProduct`，对不上要在 `99-isales-modem.rules` 里加一行

### Q2. udev rule 没加载

```
$ ls -l /dev/ttyUSB-isales-modem
ls: cannot access ...: No such file or directory
```

`udevadm control --reload` 后没 trigger，重新插一下 USB 即可；或者 `udevadm trigger`。

### Q3. serial 权限

```
PermissionError: [Errno 13] Permission denied: '/dev/ttyUSB-isales-modem'
```

isales 用户没在 dialout 组里：

```bash
sudo usermod -aG dialout isales
# 重启进程
sudo systemctl restart isales-modem-controller
```

### Q4. AT+GMI 返回空 / 自动识别失败

显式设 `ISALES_MODEM_DRIVER=a7670`（或 `sim800c` / `quectel_uc20`）。

### Q5. signal_strength 一直 99

99 = 未知（spec § AT+CSQ）。多半是没注网：

```bash
# 进入 AT 终端：
sudo socat - /dev/ttyUSB-isales-modem,raw,echo=0
AT+CREG?
+CREG: 0,1   ← 1 表示在 home 网络；0 / 2 / 3 表示未注或漫游故障
AT+CSQ
+CSQ: 18,99  ← 18 是 RSSI，> 10 一般可用
```

无信号通常是 SIM 没插紧 / 天线松 / 卡停机。

### Q6. ISALES_LIVE_PROVIDER_TESTS

跟 modem-controller 无关，是 stage 5（Provider 真接口）用的。stage 6 不要开。

## 7. 拔出测试（验收清单 11.5）

```bash
# 通话进行中 → 拔 USB
journalctl -fu isales-modem-controller | grep -E "udev|device_error"
# 期望：
#   udev: action=remove
#   device_error code=device_lost session_id=...
#   device.status=offline
```

## 8. 进程崩溃测试（验收清单 11.6）

```bash
sudo systemctl stop isales-modem-controller
# 等 120s 后到 isales-api 看：
psql -c "SELECT id, status, last_seen_at FROM device WHERE imei='...';"
# 期望：status='offline'（worker watchdog 干的）
```

## 9. 真硬件验收顺序（清单 11.x）

1. 11.1 插 modem → udev → device 注册 → status=idle
2. 11.2 主仓投线索 → engine 真打自己手机
3. 11.3 ≥ 3 轮真实对话（greeting + 2 轮 user-AI）
4. 11.4 录音 wav 上传 OSS 成功，recording_url 写回 call_record
5. 11.5 拔 modem → udev → status=offline；in-call session ABNORMAL_END
6. 11.6 kill modem-controller → 30s 后心跳停 → 120s watchdog 标 offline
