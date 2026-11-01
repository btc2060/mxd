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

新增声音无需改代码：支持任意正整数编号，如 `sounds/4.wav`、`sounds/10.wav`、`sounds/23.wav`，对应 `--alert-voice 4`、`--alert-voice 10`、`--alert-voice 23`。文件使用不带前导零的编号；参数 `004` 会规范为 `4`。0、负数、小数和非数字参数会被拒绝。未找到文件会明确列出应添加的路径，不会改播其他编号。

如要保存默认声音，在 `config.garden.local.json` 添加一个字段：

```json
"lie_alert_sound": "1"
```

取值 `"beep"` 为原报警，`"1"`、`"4"`、`"15"` 等正整数字符串为编号声音。编号优先读取**配置文件所在目录的 `sounds/编号.wav` 文件**，再读取该目录下的 `lionbot/sounds`，最后检查程序自带声音目录。启动／测试时会打印实际读取的绝对路径和音频格式，便于确认替换生效。

也支持自定义 WAV 相对于配置文件目录的路径，例如 `"sounds/my-alert.wav"`；需单／双声道、8／16 位、未压缩 PCM WAV，最大 10MB。路径按 JSON 规则转义或使用 `/`。

声音在挂机启动前读入内存并验证，同时准备无额外元数据的 PCM 内存版本。报警先调用 Windows 文件播放方式，失败后尝试内存版本，不联网、不做语音合成；声音线程与识别线程分开。短 WAV 文件读取开销通常很小；实际声卡、Windows 播放设备或 UU 音频转发可能有缓冲，首次播放速度需在实际电脑试听确认。停止按键不等待语音播完。

两种播放方式都失败时，打印具体异常及路径并回退原 Beep；路径或格式错误在开始挂机前提示。`播放修复包` 不含 WAV，覆盖代码不会替换用户自己的声音。现有 `--cutnet` 仍按其原规则运行：命中测谎断网停机，不播放该语音。

兼容 RIFF／data 长度为 `0xFFFFFFFF` 的流式 PCM WAV：使用实际可读音频长度在内存中重建头部，原文件保持原样。非常用采样率（如 40000Hz）在内存中转换为 44100Hz；这种情况优先播放修正后的内存音频。有限长度头部与实际数据不符、半帧数据等真正截断仍会报错。处理只在启动时进行，不需要安装音频转换工具。

WAV 由 eSpeak NG 的 Mandarin 声音离线生成，未分发语音合成工具，也不需要在挂机电脑安装语音工具。
