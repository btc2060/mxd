# 单机 garden 自定义报警声音

更新文件只涉及 garden 的声音选择，保留原校准和本地配置。默认仍是 Beep，多机版未改。内置 `lionbot/sounds/1.wav`、`2.wav`、`3.wav` 分别朗读“一号、二号、三号”，使用离线合成声音，PCM 16 位、单声道、22050Hz。

每台电脑只选自己的编号，检测测谎后会持续重复该编号，F8 静音及人工恢复规则沿用原逻辑。

试听，不操作游戏：

```powershell
python.exe -m lionbot.garden --test-alert-sound --alert-voice 1
python.exe -m lionbot.garden --test-alert-sound --alert-voice 2
python.exe -m lionbot.garden --test-alert-sound --alert-voice 3
```

使用“1号”启动：

```powershell
python.exe -m lionbot.garden --run --alert-voice 1
```

其他电脑把编号改为 2 或 3。此参数只覆盖本次运行，不修改 JSON，也无需重做校准。`--show` 可照常追加。

如要保存默认声音，在 `config.garden.local.json` 添加一个字段：

```json
"lie_alert_sound": "1"
```

取值 `"beep"` 为原报警，`"1"`／`"2"`／`"3"` 为内置声音。也支持自定义 WAV 相对于配置文件目录的路径，例如 `"sounds/my-alert.wav"`；需单／双声道、8／16 位、未压缩 PCM WAV，最大 10MB。路径按 JSON 规则转义或使用 `/`。

声音在挂机启动前读入内存并验证，报警时不联网、不做语音合成、不再读磁盘，声音线程与识别线程分开。短 WAV 不会因为是语音就明显增加触发延迟；实际声卡、Windows 播放设备或 UU 音频转发可能有缓冲，首次播放速度需在实际电脑试听确认。停止按键不等待语音播完。

播放设备出错时回退原 Beep；路径或格式错误在开始挂机前提示。现有 `--cutnet` 仍按其原规则运行：命中测谎断网停机，不播放该语音。

WAV 由 eSpeak NG 的 Mandarin 声音离线生成，未分发语音合成工具，也不需要在挂机电脑安装语音工具。
