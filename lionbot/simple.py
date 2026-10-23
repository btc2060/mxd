"""最简定点入口：python -m lionbot.simple --setup / --run。"""
from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

from .config import Config
from .controls import InputCancelled, InputExecutor
from .simple_cycle import SimpleConfig, SimpleCycle, load_simple
from .vision import minimap_position


def save_simple(path, config):
    from .calibrate import save_json

    config.validate()
    save_json(path, asdict(config))


def setup(path):
    from .calibrate import fresh, select_roi
    from .windows import GameWindow
    import cv2

    config = load_simple(path)
    if not path.exists():
        # 只复用同窗口的小地图框，站位重新读取，不导入旧攻击/识别配置。
        source = path.parent / "config.plants.local.json"
        if source.exists():
            old = json.loads(source.read_text(encoding="utf-8"))
            config = replace(config, window_title=old.get("window_title", config.window_title),
                             frame_size=tuple(old["frame_size"]) if old.get("frame_size") else None,
                             minimap_roi=tuple(old["minimap_roi"]) if old.get("minimap_roi") else None)
    window = GameWindow(config.window_title)
    try:
        frame = fresh(window, "手动站到新的挂机点；只记录小地图黄点 X，无需采集角色或怪物图片。")
        size = tuple(frame.shape[1::-1])
        if config.minimap_roi is None or config.frame_size != size:
            print("框选小地图内部，排除标题、按钮和黄色图标。")
            config = replace(config, minimap_roi=select_roi("Select minimap INSIDE / Enter confirm", frame))
        config = replace(config, frame_size=size)
        config.validate()
        position = minimap_position(frame, config.minimap_roi)
        if position is None:
            raise ValueError("没有找到唯一黄点，请检查小地图框；原配置未修改")
        config = replace(config, station_x=position[0])
        save_simple(path, config)
        print(f"新的挂机 X={config.station_x:.4f}，已保存 {path.name}。运行中 F9 可随时重设。")
    finally:
        window.close()
        cv2.destroyAllWindows()


def run_simple(config, path, send_keys):
    from .windows import GameWindow, Keyboard, require_windows
    from .lie_alert import LieAlertMonitor, load_detector

    require_windows()
    from pynput.keyboard import Key, Listener

    detector = load_detector(config, path.parent) if config.lie_alert_enabled else None
    stopped, paused, record, alarm_resume = (threading.Event() for _ in range(4))
    window = GameWindow(config.window_title)
    brain = SimpleCycle(config)
    executor = guard = listener = monitor = None
    try:
        if send_keys:
            input_config = Config(attack_interval=config.attack_interval, attack_hold=config.attack_hold,
                                  face_hold=config.face_hold, ladder_nudge_seconds=config.move_tap_hold)
            executor = InputExecutor(Keyboard(), input_config,
                                     active=lambda: window.focused() and not paused.is_set() and not stopped.is_set()
                                     and (monitor is None or monitor.state.allows_input()))
            guard = executor.start_guard(stopped)

        def on_alarm(reason):
            paused.set()
            if executor:
                executor.stop()
            print(f"警报：{reason}！已锁定暂停，请手动处理。F8 静音；窗口消失后再按 F8 恢复。", flush=True)

        if detector is not None:
            monitor = LieAlertMonitor(detector, config.frame_size, lambda: GameWindow(config.window_title),
                                      on_alarm, config.lie_alert_interval,
                                      diagnostic_dir=path.parent / "debug-output" / "lie-alert")

        def on_press(key):
            if key == Key.f12:
                stopped.set()

        def on_release(key):
            if key == Key.f8:
                if monitor is not None and monitor.state.is_latched():
                    alarm_resume.set()
                else:
                    paused.clear() if paused.is_set() else paused.set()
            elif key == Key.f9:
                record.set()
            elif key == Key.f10 and monitor is not None:
                monitor.request_snapshot()

        listener = Listener(on_press=on_press, on_release=on_release)
        listener.start()
        if monitor:
            monitor.start()
        print("简单定点：" + ("发送按键" if send_keys else "仅观察，不发送按键、不累计攻击次数"))
        print(f"X={config.station_x:.4f}；只向右攻击，D 每组 {config.right_attack_shots} 次，"
              f"单次间隔 {config.attack_interval:g}s，"
              f"组间等 {config.group_pause:g}s。")
        print(f"累计按下 D {config.d_shots_before_a} 次后，切换为 A {config.a_attack_shots} 次，再恢复 D；"
              "回位前补打的 D 也计入，暂停和回位保留换键进度。")
        print(f"X 偏离后向右 D {config.recovery_shots} 次，再步行回位；启动 Alt 一次，此后每 {config.alt_interval:g}s 一次。")
        print(f"累计 D/A 随机 {config.move_attack_min}～{config.move_attack_max} 次后，短按左方向键随机 "
              f"{config.move_left_taps_min}～{config.move_left_taps_max} 次，朝左 D {config.move_attack_shots} 次，"
              "回挂机点后继续原 D/A 循环。")
        print("导航只截图小地图，不加载怪物/名牌/地形识别。F8 暂停/恢复；F9 重设挂机点；F12 退出。")
        if monitor:
            print(f"测谎标题独立监控：每 {config.lie_alert_interval:g}s 检查，命中后停键、持续声音报警；"
                  "窗口消失也不自动恢复，须人工按 F8。请保持游戏前台且无遮挡。")
            print(f"测谎模板={path.parent / config.lie_alert_template}；阈值={config.lie_alert_threshold:.3f}；"
                  f"搜索框={config.lie_alert_roi or '整个游戏客户区'}；F10 保存实际截图与匹配诊断。")
        else:
            print("测谎监控已由配置关闭。")
        last_status, logged_at, alert_logged_at = "", float("-inf"), float("-inf")
        while not stopped.is_set():
            started = time.monotonic()
            if monitor and started - alert_logged_at >= 5:
                print(monitor.status(), flush=True)
                alert_logged_at = started
            if alarm_resume.is_set():
                alarm_resume.clear()
                if monitor and monitor.state.acknowledge(focused=window.focused()):
                    paused.clear()
                    brain.suspend()
                    print("测谎窗口已连续两次未检出，已人工恢复挂机。")
                elif monitor:
                    print("报警已静音；仍检测到窗口或尚无连续有效画面，保持暂停。处理完后再按 F8。")
            if monitor and monitor.state.is_latched():
                if executor:
                    executor.stop()
                record.clear()
                brain.suspend()
                if started - logged_at >= 2:
                    print(f"测谎报警暂停：{monitor.state.reason}；请手动处理，F8 静音/人工恢复。")
                    logged_at = started
                time.sleep(.05)
                continue
            if monitor and window.focused() and not monitor.state.allows_input():
                if executor:
                    executor.stop()
                brain.suspend()
                if started - logged_at >= 2:
                    print("等待有效的测谎监控画面，暂不发送按键。")
                    logged_at = started
                time.sleep(.05)
                continue
            if not window.focused() or (paused.is_set() and not record.is_set()):
                if executor:
                    executor.stop()
                brain.suspend()
                if record.is_set():
                    record.clear()
                    print("F9 未记录：请在游戏前台时按 F9。")
                if started - logged_at >= 2:
                    print("已暂停，F8 恢复。" if paused.is_set() else "游戏失焦，已松键。")
                    logged_at = started
                time.sleep(.1)
                continue
            image = window.capture_region(config.minimap_roi, config.frame_size)
            position = minimap_position(image, (0, 0, image.shape[1], image.shape[0]))
            if record.is_set():
                record.clear()
                if executor:
                    executor.stop()
                if position is None:
                    print("F9 未记录：没有找到唯一黄点。")
                else:
                    updated = replace(config, station_x=position[0])
                    save_simple(path, updated)
                    config = updated
                    brain.set_station(config.station_x)
                    print(f"新挂机 X={config.station_x:.4f} 已保存；取消旧移动并重新计数，保留 D/A 换键进度。")
            if paused.is_set():
                brain.suspend()
                time.sleep(.1)
                continue
            action = brain.decide(position[0] if position else None, time.monotonic(), window.focused())
            current = f"X={position[0]:.4f}" if position else "黄点丢失"
            status = (f"{current} 目标={config.station_x:.4f} D组={brain.d_groups} "
                      f"D累计={brain.d_since_a}/{config.d_shots_before_a} A={brain.a_shots}/{config.a_attack_shots} "
                      f"移动累计={brain.attacks_since_move}/{brain.next_move_at} 阶段={brain.move_phase or '-'} {action.reason}")
            if status != last_status or started - logged_at >= 2:
                print(status)
                last_status, logged_at = status, started
            if executor:
                try:
                    brain.executed(action, executor.execute(action))
                except InputCancelled:
                    brain.suspend()
            time.sleep(max(.001, .1 - (time.monotonic() - started)))
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="朝右 D/A 定点攻击，定期左移补打，用小地图 X 回位")
    parser.add_argument("--config", type=Path, default=Path("config.simple.local.json"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--setup", action="store_true", help="记录新的挂机 X；有旧框时复用小地图，无需怪物采样")
    group.add_argument("--set-x", type=float, help="保存指定的小地图归一化 X，然后退出")
    group.add_argument("--run", action="store_true", help="发送按键")
    group.add_argument("--observe", action="store_true", help="只读取 X，不发送按键（默认）")
    group.add_argument("--calibrate-alert", action="store_true", help="只采集测谎固定标题，保留挂机站位和参数")
    group.add_argument("--check-alert", action="store_true", help="配合 --image 离线检查测谎标题，不发送按键")
    group.add_argument("--watch-alert", action="store_true", help="只监测真实游戏测谎、显示分数和报警，不发送按键")
    group.add_argument("--test-alert-sound", action="store_true", help="测试声音报警，不截图、不发送按键")
    parser.add_argument("--image", type=Path, help="配合 --calibrate-alert 或 --check-alert 使用原始截图")
    args = parser.parse_args(argv)
    if args.image is not None and not (args.calibrate_alert or args.check_alert):
        parser.error("--image 只能配合 --calibrate-alert 或 --check-alert")
    if args.check_alert and args.image is None:
        parser.error("--check-alert 需要 --image <截图路径>")
    path = args.config.resolve()
    try:
        if args.calibrate_alert:
            from .lie_alert import calibrate_alert

            calibrate_alert(path, args.image)
        elif args.test_alert_sound:
            from .lie_alert import sound_alert

            print("播放三次双音警报，请确认扬声器音量。")
            for _ in range(3):
                sound_alert()
                time.sleep(.3)
        elif args.check_alert:
            from .lie_alert import load_detector, save_diagnostic
            from .vision import read_image

            detector = load_detector(load_simple(path), path.parent)
            frame = read_image(args.image)
            report = detector.inspect(frame)
            print(report.describe())
            prefix = save_diagnostic(frame, detector, path.parent / "debug-output" / "lie-alert", "offline")
            print(f"诊断已保存：{prefix}-match.png（绿色框=检出，橙色框=未通过的最佳候选），详情见同名 .json")
            return 0 if report.found else 1
        elif args.watch_alert:
            from .lie_alert import watch_alert

            watch_alert(load_simple(path), path)
        elif args.setup:
            setup(path)
        else:
            config = load_simple(path)
            if args.set_x is not None:
                config = replace(config, station_x=args.set_x)
                save_simple(path, config)
                print(f"已保存挂机 X={config.station_x:.4f}；运行中也可按 F9 记录当前位置。")
            else:
                config.validate(ready=True)
                run_simple(config, path, args.run)
        return 0
    except (ValueError, TypeError, OSError, RuntimeError) as error:
        print(f"错误：{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
