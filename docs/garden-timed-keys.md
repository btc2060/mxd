# 单机 garden：4组固定定时键

在原 `config.garden.local.json` 对象中增加 `timed_keys` 字段，其他校准和参数保留，不要用下方片段替换整个配置：

```json
"timed_keys": [
  {"key": "1", "interval_seconds": 300, "hold_seconds": 0.1},
  {"key": "2", "interval_seconds": 600, "hold_seconds": 0.1},
  {"key": "3", "interval_seconds": 0,   "hold_seconds": 0.1},
  {"key": "4", "interval_seconds": 0,   "hold_seconds": 0.1}
]
```

每300秒按1，每600秒按2，另外两组关闭。保持4组；`interval_seconds` 是间隔秒数，0关闭，启用时至少1秒；`hold_seconds` 是按住秒数，允许0.02～1秒。未添加该字段时四组默认全部关闭，原配置仍可运行。原 `alt_interval` 独立保留，不要在四组内再启用 Alt。

按键支持字母、数字、`f1`～`f12` 中的非控制热键，以及 ctrl、shift、space、tab、enter、home、end、insert、delete、page_up、page_down。大小写和首尾空格会规范化：`F` 是字母 f，`H` 是字母 h，`F1`／`f1` 都是功能键 F1。只支持单个按键，不是组合键。F8/F10/F12 保留为程序热键；启用的四组中不可重复同一个键。

可选 `"settle_seconds": 0.8` 设置按键松开后的施法等待，默认0.8秒，允许0～5秒。此时不继续 D 攻击或移动，测谎监控仍运行，避免技能尚未完成就被后续操作打断。实际按下后会输出“已发送定时按键”。按键是否绑定技能、MP是否足够及游戏冷却仍需在游戏里确认。

首次有效运行后等满一个间隔再执行，不在启动时一次按完四组。几个键同时到期时逐一执行；暂停、失焦、黄点丢失、离开底层、测谎报警、瞬移落点等待和强制脱离卡住位置期间不执行这些键。恢复后每个到期键最多执行一次，不补发暂停期间漏掉的多次；按照实际发送按下事件的时间计算下一次。

按定时键前释放行进键，执行后松键，继续原巡逻。中断立即松键，按下已经发送则计入本次执行。定时键持有时间不作为原地卡住时间，不改变原 D 攻击组计数。按 F8 暂停／恢复不重置定时进度；重启程序后重新等待首次间隔。

安装：[garden 定时键补丁](https://github.com/btc2060/mxd/raw/refs/heads/singlehost-cutnet/downloads/garden-timed-keys-update.zip)，解压覆盖到原项目根目录。没有 WAV、本地 JSON 或校准图片。修改原配置后重启：

```powershell
python.exe -m lionbot.garden --run --alert-voice 1
```

声音编号按原设置选择；与旧 --cutnet 模式配合时仍按其原规则运行。只改 garden 单机入口，多机版和 pig 的定时实现保持原样。
