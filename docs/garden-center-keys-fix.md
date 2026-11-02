# Garden：击退后的攻击中心与定时键兼容修复

`attack_range_px` 只决定以脚底 X 为中心的左右射程，以及相应的局部搜索／瞬移覆盖判断；修改276到270不会直接改变脚底中心。紫红色框为实际攻击带，左右同宽；青色框包含完整怪物模板所需余量，不代表实际攻击范围。画面边缘也会裁切看不见的区域。

旧 garden 定位会优先使用名牌，以及基于上一帧估计结果更新地形参考。旧地图留下的名牌脚底偏移可能不适合当前角色位置；小地图像素取整、击退与镜头平移估计误差也可能通过逐帧参考积累。

现在 garden 只从手动保存的固定脚底／地形参考计算本帧位置，不用继承名牌偏移来重设参考，不把本帧推算结果继续作为下一帧的绝对原点。这个改动只作用于 garden；其他入口的地图跟踪保持原行为。预览增加黄色中心十字与 `CENTER / L / R` 像素提示。参考匹配失败时停止战斗定位，不用旧名牌猜测中心；小地图导航仍可继续。

更新包：[攻击中心和定时键修复](https://github.com/btc2060/mxd/raw/refs/heads/singlehost-cutnet/downloads/garden-center-keys-fix.zip)。解压覆盖项目根目录；不带本地配置、WAV 或校准图片。

先补一次当前脚底参考，无需修改射程或重做怪物样本：

```powershell
python.exe -m lionbot.garden --anchor
python.exe -m lionbot.garden --observe --show
```

站稳后点击角色两脚落地的中点，避开宠物、武器和名牌。观察黄色十字是否随角色移动、被击退后能回到脚底，紫红色框的左右宽度是否一致。若旧参考来自错误的手动点击，或比例 `world_pixels_per_x` 本身不准，仍需重新校准相应参考；本次没有从低分辨率截图猜测或直接修改用户校准值。

定时键完整格式见 [garden-timed-keys.md](garden-timed-keys.md)。现在兼容大小写 `f`／`F`、`h`／`H`、`f1`／`F1`，按键后默认等待0.8秒再继续动作。`interval_seconds: 0` 仍表示关闭；首次等满间隔再发，不是在启动时立即按。

例如将原 `timed_keys` 四组修改为下面内容（其他 JSON 字段保留）：

```json
"timed_keys": [
  {"key": "f", "interval_seconds": 300, "hold_seconds": 0.1, "settle_seconds": 0.8},
  {"key": "h", "interval_seconds": 600, "hold_seconds": 0.1, "settle_seconds": 0.8},
  {"key": "F1", "interval_seconds": 0, "hold_seconds": 0.1},
  {"key": "4", "interval_seconds": 0, "hold_seconds": 0.1}
]
```

要验证字母键可先临时设10／15秒，游戏前台观察“已发送定时按键”，再恢复正常间隔；修改文件后需重启程序。配置只是指定要按的键，不会替你修改游戏内技能绑定。F8/F10/F12 继续保留为控制热键。

离线验证包括：旧名牌偏移不覆盖手动脚底、模拟多次击退不累计中心误差、射程变更保持同一中心、字母和大写功能键进入键盘后端、技能等待不被攻击打断。没有在用户实际 Windows 客户端上运行，也未读取用户的校准图片；需要按以上观察步骤验证。
