"""暗影花园底层：python -m lionbot.garden --setup / --run --show。"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

import cv2

from .controls import InputCancelled, InputExecutor
from .garden_cycle import GardenCycle, load_garden, save_garden
from .garden_vision import GardenVision


def run_garden(config, path, send_keys=False, show=False):
    from .lie_alert import LieAlertMonitor, beep_alert, load_detector
    from .windows import GameWindow, Keyboard, require_windows

    require_windows()
    from pynput.keyboard import Key, Listener

    config.validate(ready=True)
    detector = load_detector(config, path.parent) if config.lie_alert_enabled else None
    window = GameWindow(config.window_title, frame_size=config.frame_size)
    stopped, paused, acknowledge = (threading.Event() for _ in range(3))
    executor = listener = monitor = guard = None
    original_threads = cv2.getNumThreads()
    preview_open = False
    title = "Dark garden / F8 pause / F10 alert snapshot / F12 exit"

    def preview_events():
        if preview_open:
            try:
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27) or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                    stopped.set()
            except cv2.error:
                stopped.set()

    try:
        cv2.setNumThreads(1)
        vision = GardenVision(config, path.parent)
        brain = GardenCycle(config, start_alt=send_keys)

        def on_alarm(reason):
            paused.set()
            if executor:
                executor.stop()
            print(f"警报：{reason}！已停键，持续 Beep 报警；F8 静音，手动处理完、窗口消失后再 F8 恢复。", flush=True)

        if detector is not None:
            monitor = LieAlertMonitor(detector, config.frame_size,
                                      lambda: GameWindow(config.window_title, hwnd=window.hwnd), on_alarm,
                                      config.lie_alert_interval, sound=beep_alert,
                                      diagnostic_dir=path.parent / "debug-output" / "lie-alert")
        if send_keys:
            executor = InputExecutor(Keyboard(), config.vision_config(),
                                     active=lambda: window.focused() and not paused.is_set() and not stopped.is_set()
                                     and (monitor is None or monitor.state.allows_input()))
            guard = executor.start_guard(stopped)

        def on_press(key):
            if key == Key.f12:
                stopped.set()

        def on_release(key):
            if key == Key.f8:
                if monitor is not None and monitor.state.is_latched():
                    acknowledge.set()
                else:
                    paused.clear() if paused.is_set() else paused.set()
            elif key == Key.f10 and monitor is not None:
                monitor.request_snapshot()

        listener = Listener(on_press=on_press, on_release=on_release)
        listener.start()
        if monitor:
            monitor.start()
        print("暗影花园底层：" + ("发送按键" if send_keys else "仅观察，不发送按键、不累计攻击"))
        print(f"边界 X={config.left_x:.4f}～{config.right_x:.4f}，只在已采集底层往返；"
              f"只按 D，每组 {config.attack_burst_shots} 次，间隔 {config.attack_interval:g}s。")
        print(f"附近两侧清完后继续原路线；像素射程 {config.attack_range_px:.0f}px，高度为脚底到头顶 "
              f"{config.player_height_px:.0f}px；局部模板＋紫红花头/绿叶识别，{config.fps:g}fps。")
        print(f"挂机开始 Alt 一次，此后每 {config.alt_interval:g}s 一次；没有 A、跳跃、爬绳或上层路线。")
        if config.teleport_enabled and config.teleport_max_dx is not None:
            print(f"中段方向键＋F，冷却 {config.teleport_cooldown:g}s；边缘只步行，瞬移后等待落点重新识别。")
            if config.teleport_max_dx * config.world_pixels_per_x > 1.8 * config.attack_range_px:
                print("F 位移上限超过识别射程重叠范围，已保持步行；请核对 D 射程或重新 --measure-teleport。")
        else:
            print("当前全程步行；如需 F，运行 --measure-teleport 并确认 teleport_enabled=true。")
        print("F8 暂停/恢复；F10 保存测谎诊断；F12 退出。游戏须保持前台，预览不要遮住游戏。")
        print("测谎监控：" + ("开启，沿用同一游戏窗口和原始 Beep 双音。" if monitor else "已由配置关闭。"))
        last_log = alert_log = float("-inf")
        last_reason = ""
        while not stopped.is_set():
            started = time.monotonic()
            preview_events()
            if stopped.is_set():
                break
            if monitor and started - alert_log >= 5:
                print(monitor.status(), flush=True)
                alert_log = started
            if acknowledge.is_set():
                acknowledge.clear()
                if monitor and monitor.state.acknowledge(focused=window.focused()):
                    paused.clear()
                    brain.suspend()
                    vision.reset()
                    print("测谎窗口已连续两帧未检出，人工恢复。")
                else:
                    print("报警已静音；窗口仍在或画面无效，保持暂停。")
            unavailable = monitor and (monitor.state.is_latched() or not monitor.state.allows_input())
            if paused.is_set() or not window.focused() or unavailable:
                if executor:
                    executor.stop()
                brain.suspend()
                vision.reset()
                if started - last_log >= 2:
                    reason = ("测谎报警，等待手动处理" if monitor and monitor.state.is_latched() else
                              "F8 暂停" if paused.is_set() else "游戏失焦或测谎画面待更新")
                    print(f"已松键：{reason}。")
                    last_log = started
                time.sleep(.05)
                continue
            frame = window.capture()
            obs = vision.observe(frame, focused=window.focused(), now=time.monotonic())
            action = brain.decide(obs, time.monotonic())
            if action.reason != last_reason or started - last_log >= 2:
                position = f"({obs.position[0]:.4f},{obs.position[1]:.4f})" if obs.position else "黄点丢失"
                print(f"pos={position} move={brain.direction} face={brain.facing or '?'} "
                      f"mobs={sum(m.confirmed for m in obs.mobs)}/{len(obs.mobs)} screen={obs.location_source} "
                      f"laps={brain.completed_round_trips} F={'fault-walk' if brain.teleport_fault else 'ok'} "
                      f"{action.kind}:{action.direction} | {action.reason}")
                last_reason, last_log = action.reason, started
            if show:
                if not preview_open:
                    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
                    cv2.resizeWindow(title, 640, round(frame.shape[0] * 640 / frame.shape[1]))
                    preview_open = True
                cv2.imshow(title, vision.draw(frame, obs, f"D ONLY | {action.kind} | LAPS {brain.completed_round_trips}", brain.direction))
                preview_events()
            if executor:
                try:
                    brain.executed(action, executor.execute(action), obs)
                    if action.kind == "teleport":
                        vision.reset()  # 不把瞬移前的检测当成落点处的当前怪物。
                except InputCancelled:
                    brain.suspend()
                    vision.reset()
            time.sleep(max(.001, 1 / config.fps - (time.monotonic() - started)))
    finally:
        stopped.set()
        if executor:
            executor.stop()
        if monitor:
            monitor.stop()
        if guard:
            guard.join(timeout=.5)
        if listener:
            listener.stop()
        window.close()
        cv2.setNumThreads(original_threads)
        if preview_open:
            cv2.destroyAllWindows()


def main(argv=None):
    parser = argparse.ArgumentParser(description="暗影花园底层局部识别，D 清怪、左右巡逻，安全水平 F，Alt 和测谎预警")
    parser.add_argument("--config", type=Path, default=Path("config.garden.local.json"))
    actions = parser.add_mutually_exclusive_group()
    for name, help_text in (("setup", "首次采集当前地图、角色身高、食人花、边界和水平 F"),
                            ("select-area", "只重选主画面并补脚底参考，保留小地图和其他校准"),
                            ("record-route", "重设底层左右掉头点，不手输坐标"),
                            ("measure-teleport", "重测水平 F 位移，不改其他采样"),
                            ("anchor", "只点脚底，补当前地图地形参考"),
                            ("sample", "补一张完整紫红食人花姿态，自动镜像"),
                            ("calibrate-alert", "采集测谎固定标题，已有模板可以复用"),
                            ("test-alert-sound", "只测试原始 Beep 报警，不操作游戏"),
                            ("observe", "只观察识别，不发送按键（默认）"),
                            ("run", "发送 D、方向键、水平 F 和定时 Alt")):
        actions.add_argument("--" + name, action="store_true", help=help_text)
    parser.add_argument("--show", action="store_true", help="显示缩小预览，须放在游戏旁边")
    parser.add_argument("--image", type=Path, help="只配合 --calibrate-alert 使用已保存原始截图")
    args = parser.parse_args(argv)
    if args.image and not args.calibrate_alert:
        parser.error("--image 只配合 --calibrate-alert")
    path = args.config.resolve()
    try:
        if args.test_alert_sound:
            from .lie_alert import beep_alert

            print("播放三次原始 Beep 双音；远程使用时在本机确认能否听到。")
            for _ in range(3):
                beep_alert()
                time.sleep(.3)
        elif args.calibrate_alert:
            from .lie_alert import calibrate_alert

            calibrate_alert(path, args.image, loader=load_garden, saver=save_garden)
        elif args.setup or args.select_area or args.record_route or args.measure_teleport or args.anchor or args.sample:
            from .garden_calibration import setup_garden, record_route, measure_teleport, record_anchor, sample_plant, select_area

            task = setup_garden if args.setup else select_area if args.select_area else record_route if args.record_route else (
                measure_teleport if args.measure_teleport else record_anchor if args.anchor else sample_plant)
            task(path)
        else:
            config = load_garden(path)
            config.validate(ready=True)
            run_garden(config, path, args.run, args.show)
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, cv2.error) as error:
        print(f"错误：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
