# Garden 多机版安装与使用

接收电脑运行报警面板；每台挂机电脑独立运行游戏和 Garden。所有电脑在同一局域网，接收端不需要接入 KVM，也不依赖 UU 传声音。测谎由人手动处理，接收端不发送游戏操作。

## 文件在哪里

- GitHub 分支：`multihost-garden`。
- 接收端独立包：`downloads/mxd-fleet-receiver.zip`。
- 挂机端独立包：`downloads/mxd-fleet-agent.zip`。
- 每个包解压后，找到同时包含 `lionfleet` 和 `scripts` 的目录；下面命令都在这个目录执行。
- 也可以下载整个分支的 ZIP，两端使用相同源文件，按各自步骤配置。
- 原单机版保存在标签 `singlehost-garden-20261027`，原 `python.exe -m lionbot.garden --run` 入口保留。不要在同一台电脑同时运行两个挂机程序。

接收端所需：Python 3.11／3.12 64 位（Windows 安装器勾选加入 PATH，保留默认 Tcl/Tk）、`lionfleet/`、接收端配置和接收端脚本。**不需要安装 requirements.txt 或游戏识别依赖。**

挂机端所需：`lionfleet/`、原 `lionbot/`、`requirements.txt`、两份本机配置、原 `calibration/` 以及挂机端脚本。

## 0. 保留当前可用单机版

在实际游戏电脑上将当前整个项目文件夹复制为另一个文件夹，例如 `mxd-single-backup`，再把多机包解压到新的 `mxd-multi`。Git 不包含你本机的怪物模板和地形参考。

在新文件夹里复制原来的：

```text
config.garden.local.json
calibration/                 整个目录，含测谎标题、怪物、地形和旧角色名牌引用
```

不要只复制花的几张图，也不要把机号写进 Garden 配置。不要把 `.venv`、`state` 或 `logs` 复制到其他电脑；每台另建自己的运行环境。旧单机目录可以继续保留，用于回退。

## 1. 设置接收电脑

1. 安装 Python，打开 CMD，运行 `python.exe --version` 确认可用。
2. 运行 `ipconfig`，找到正在联网的以太网／Wi-Fi 的 **IPv4 地址**，例如 `192.168.1.10`。不要填 `127.0.0.1`、`0.0.0.0`、VPN 地址或网关地址。建议在路由器中为接收电脑保留这个地址。
3. 复制示例为本机配置（只在文件不存在时复制）：

```powershell
if (-not (Test-Path config.receiver.local.json)) { Copy-Item config.receiver.example.json config.receiver.local.json }
```

接收端默认监听全部网卡 UDP `27676`，已登记 M01～M08，对应 1～8 号 KVM 口；未使用的机号显示“尚未连接”，不会一直叫。你只用两台也可以保留八条。

```json
{
  "bind_ip": "0.0.0.0",
  "port": 27676,
  "offline_seconds": 10.0,
  "repeat_alarm_seconds": 8.0,
  "speech_enabled": true,
  "state_file": "state/receiver.json",
  "log_file": "logs/receiver.jsonl",
  "machines": [
    {"machine_id": "M01", "machine_name": "1号机", "kvm_port": 1, "alarm_wav": ""},
    {"machine_id": "M02", "machine_name": "2号机", "kvm_port": 2, "alarm_wav": ""}
  ]
}
```

4. 将当前家庭／办公局域网的 Windows 网络配置设为“专用”。在**管理员 PowerShell** 中，仅在接收电脑运行一次：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\allow_receiver_firewall.ps1
```

脚本仅放行配置端口的专用网络、局域网来源 UDP 入站；不要关闭整个防火墙。以后更改接收端端口时重新运行脚本。挂机端通常不需要额外入站规则，使用本机发出的 UDP 及其回包。

5. 测试接收电脑声音，再启动面板：

```powershell
python.exe -m lionfleet.receiver --test-sound
python.exe -m lionfleet.receiver --config config.receiver.local.json
```

也可双击 `scripts/test_receiver_sound.cmd`、`scripts/start_receiver.cmd`。启动脚本在配置缺失时会从示例创建一份，不覆盖已有文件。

应听到双音和“一号机测试报警”。面板使用接收电脑的默认声音设备，选择实际可听的扬声器／耳机，不依赖 KVM 当前 HDMI 音源。GUI 中可逐台选择并点击“测试选中机号播报”。

**如果有双音但没有中文语音：** 检查终端及面板下方的语音错误。程序调用 Windows 的中文 SAPI 语音；系统没有可用中文引擎时，面板和普通提示音仍生效。可安装支持 SAPI 的中文语音，或为每个机号准备不同的本地 WAV，在 `alarm_wav` 填 `sounds/M01.wav` 等相对路径。WAV 应能明确说出机号，例如“1号机有报警，请查看面板”；配置 WAV 后不调用语音引擎。只用 Beep 时请结合面板辨认机号。

接收端启动后保持运行，不要休眠。没有图形界面时可用 `--console` 查看状态，但正式使用推荐面板。

## 2. 设置每台挂机电脑

在新多机文件夹安装原来的识别依赖（已有相同目录下可用的 `.venv` 可以直接复用）：

```powershell
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path config.machine.local.json)) { Copy-Item config.machine.example.json config.machine.local.json }
```

`config.machine.local.json` 示例：

```json
{
  "machine_id": "M01",
  "machine_name": "1号机",
  "kvm_port": 1,
  "receiver_ip": "192.168.1.10",
  "receiver_port": 27676,
  "heartbeat_seconds": 2.0,
  "receiver_timeout_seconds": 15.0,
  "startup_timeout_seconds": 20.0,
  "loop_timeout_seconds": 8.0,
  "local_sound": true,
  "state_directory": "state"
}
```

- **每台必须唯一：** 第一台 M01／1号机／KVM 1，第二台 M02／2号机／KVM 2，以此类推。机号和接收端登记对应，不能所有机器都用默认 M01。
- `receiver_ip` 改为步骤 1 查到的接收电脑真实 IPv4，所有主机填同一个接收地址；`receiver_port` 两端一致。
- `local_sound=true` 保留原来的本机 Beep。接收电脑仍会独立报警。
- 原 `config.garden.local.json` 不改：D 每组 1 次、Alt 190 秒、原地超时 5 秒沿用。`lie_alert_enabled` 必须为 true，复用已有 `calibration/lie-alert-title.png`。

如果这台电脑之前没有完成 Garden 校准，仍通过原入口设置：

```powershell
.\.venv\Scripts\python.exe -m lionbot.garden --setup
```

复制其他电脑的参数时，须先确认本机游戏客户区尺寸、DPI、截图区域和模板可用。原生游戏窗口优先固定为当前的 `1366×768`；不要根据合屏器上缩小后的画面重新框选模板。

## 3. 先测通知，不打开游戏也能测

保持接收端面板运行。每台分别执行：

```powershell
.\.venv\Scripts\python.exe -m lionfleet.garden --check-connection
.\.venv\Scripts\python.exe -m lionfleet.garden --test-alert
```

或双击 `scripts/check_connection.cmd`、`scripts/test_notification.cmd`。首次缺少机器配置时脚本打开记事本，先修改保存，再运行一次。

应看到“连接成功”，接收端对应机号出现“测试报警”，并听到该机号。3 秒后测试程序发送测试结束；这两个命令不创建游戏键盘或鼠标控制器，也不触发游戏测谎。

测试程序中断留下测试事件时：

```powershell
.\.venv\Scripts\python.exe -m lionfleet.garden --clear-test
```

只清测试事件，不清真实测谎或程序故障。不要与该机的正式多机挂机同时运行；同机第二个进程会被拒绝。

## 4. 正式开始 Garden

先退出原来的单机挂机，只保留游戏。若游戏以管理员身份运行，挂机端也用管理员终端启动。

先观察，再退出观察进程，正式运行：

```powershell
.\.venv\Scripts\python.exe -m lionfleet.garden --observe --show
.\.venv\Scripts\python.exe -m lionfleet.garden --run
```

或依次使用 `scripts/observe_garden_multi.cmd` 和 `scripts/start_garden_multi.cmd`。不要同时开两个。需要正式运行预览时，在运行命令末尾加 `--show`，预览放在游戏旁边；不显示预览开销更小。

新入口成功连接接收端前不发送游戏按键。连接完成后切回游戏，保持各主机游戏前台、不最小化、不锁屏。每台各自执行现有 D／F／Alt／左右巡逻，网络只发送状态。

接收端显示运行、观察、暂停、失焦、监控等待、报警、程序故障或失联。机号后面带 KVM 口，按物理贴纸切换对应主机即可。

## 5. 出现测谎后的实际操作

1. 检出主机立即松开按键并锁定暂停；随后上报机号。该机本地 Beep 和接收端提示音／语音均会提醒。
2. 你按面板机号切 KVM／UU，手动完成测谎。**关闭 KVM 键鼠同步广播，一次只操作一台。**
3. 本机 F8：测谎仍在、失焦、截图无效或接收端失联时，只静音，继续暂停；标题消失、连续有效检查通过且网络恢复后，再按 F8 人工恢复。
4. 只有本机恢复成功，才发送解除事件。接收端的“静音选中机”只让该次报警不再播报，既不清除待办，也不恢复游戏。新的报警仍会提醒。
5. F10 保存原来的测谎诊断截图；F12 退出该机挂机。接收端退出不发送任何按键或恢复命令。

## 6. 失联、重启和验收

- 心跳默认 2 秒；接收端超过 10 秒没收到新的心跳，就显示并播报失联。旧包／重发包不能冒充新的心跳。
- 挂机端超过 15 秒没收到有效回执，本机停键并报警。网络恢复不自动恢复挂机，检查后在该机按 F8。
- 主循环超过 8 秒未更新也会锁定故障；不会因通讯线程仍活着而显示正常。测谎扫描时间另行显示，原输入守卫继续检查扫描是否新鲜。
- 报警保存在各端 `state/`，接收日志在 `logs/receiver.jsonl`。接收端重启后恢复未处理报警；挂机端重启也不会自行清掉旧测谎。不要删除状态目录来消报警，应按实际情况在本机人工处理后 F8 恢复。
- UDP 状态快照未收到回执时按 0.5／1／2 秒重试，此后每 2 秒重试；心跳持续携带报警和已解除事件。接收端去重，不会把同一报警当成很多条。
- 同时报两台时，面板同时显示，语音轮流播报，默认每个未静音事件间隔至少 8 秒。语音工作不阻塞收包或画面更新。

先用两台验收：两台机号测试 → 同时测试报警 → 关闭接收端超过 15 秒，确认正式挂机端停键 → 重开接收端，确认待办恢复且挂机没有自动恢复 → 分别本机 F8 → KVM 切走十分钟再检查截图和窗口尺寸。再逐台增加主机。

## 常见问题

| 现象 | 检查 |
| --- | --- |
| 没有回执 | 接收程序是否运行；IP 是否为实际局域网 IPv4；端口是否一致；网络是否为专用；防火墙是否已放行；Wi-Fi 是否有客户端隔离 |
| 提示机号未登记 | 在接收配置 machines 登记该 machine_id，重启接收端；不要写进 Garden 配置 |
| 同一机号已有在线进程 | 先退出旧单机／多机／测试进程；检查另一台是否误用同一 machine_id；异常退出后最多等待离线窗口再启动 |
| 声音正常但中文播报失败 | 接收端测试声音，检查中文 SAPI 语音，或配置每机 WAV；面板仍可辨认机号 |
| KVM 切走后截图异常 | 检查每个输入口的 EDID／热插拔保持；恢复固定分辨率；确认游戏没有最小化或停止渲染 |
| 新电脑缺少模板或地形参考 | 复制完整 calibration 及对应配置，或在该电脑按原入口补校准，不要删除其他已采集内容 |
| 正常退出后显示已停止 | 这是正常退出状态，不当作意外失联；未处理报警仍会保留 |

当前验证包括真实本机 UDP 的收件、首个回执丢失后重发、接收端重启、超时锁定、重复机号拒收、报警持久化、轮流播报调度及模拟窗口中的人工恢复。云端不能验证 Windows 中文语音、实际 KVM 和你家局域网，部署时先完成上面的通知测试。
