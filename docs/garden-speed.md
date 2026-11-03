# Garden 攻击频率和瞬移后响应

原 garden 复用通用 Config 校验，继承了至少0.9秒的攻击间隔。现在 garden 单独允许至少0.3秒，且 `attack_hold < attack_interval`；其他入口保留原限制。未修改用户配置或默认速度。

[下载修复包](https://github.com/btc2060/mxd/raw/refs/heads/singlehost-cutnet/downloads/garden-speed-fix.zip)，解压覆盖原项目根目录。包不含本地 JSON、校准或音频；之前的定位、定时键、测谎报警修复均保留。

在原 `config.garden.local.json` 中修改对应字段（保留其他内容）：

```json
"attack_interval": 0.6,
"attack_burst_shots": 4,
"attack_hold": 0.08,
"face_hold": 0.05,
"teleport_settle": 0.10,
"teleport_cooldown": 1.5,
"fps": 12,
"clear_seconds": 0.25,
"clear_frames": 3
```

- `attack_interval`：相邻 D 按下事件的最短间隔，包含本次按住时间，不是先按住再额外等0.6秒。每组不会为每一击重复转身。
- `attack_burst_shots`：已确认目标后同方向连续攻击的组大小；需要3～4下时可设4。完成一组后仍有怪，会继续识别和攻击，不是打满4下就离开。
- `attack_hold`：单次按住 D 的时长，通常保持0.08，不把它设成0.6。
- `teleport_settle`：按 F 后的最低落点等待，默认0.3，本次可缩短到0.10（允许>=0.05）。它主要影响 F 后恢复判断的速度。
- `teleport_cooldown`：两次 F 的最短间隔，不直接决定落地后何时按 D。先保留1.5，不需要为了加快 D 起手一起调低。
- `face_hold`：需要换方向时的方向键短按时间，0.05可比0.08更短；方向本来正确时直接攻击。
- `fps`：每秒观察／决策次数，10fps约每0.1秒一次；12fps约0.083秒一次。更高会增加图像识别 CPU 开销。
- `clear_seconds`／`clear_frames`：目标消失后确认清场的时间和连续帧数，影响打完怪后继续走的速度，不是每次 D 的间隔。

如果四组 `timed_keys` 里也配置了字母 `f`，那次独立定时按 F 后等待的是该组 `settle_seconds`（默认0.8），不受 `teleport_settle` 控制。正常巡逻瞬移已经会按 F，无需再把 f 配成定时瞬移键；若确实另有用途，按实际游戏绑定设置。

落点最低等待结束后，若黄点已确认位移，立即继续识别攻击；黄点还未更新时会短暂继续等待（通常到 F 后0.35秒内），避免提前把成功的 F 记成失败。不取消真实落点、怪物连续确认或测谎停键检查，因此修改0.10不代表 F 后刚好0.10秒就一定按 D。

实际有效攻击速度仍受游戏技能／武器动作、服务器响应和识别耗时影响。脚本允许0.6秒不等于每次必定有伤害；先观察连续几组 D 的实际伤害，再调整。修改后重启程序生效。
