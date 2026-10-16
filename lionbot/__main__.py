from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import cv2

from .brain import Brain, target_status
from .config import load_config
from .controls import InputCancelled, InputExecutor
from .vision import Vision, read_image, write_image


def live(config, root: Path, mode: str, run: bool, show: bool) -> None:
    from .windows import GameWindow, Keyboard, require_windows

    require_windows()
    from pynput.keyboard import Key, Listener

    if run and mode == "auto" and config.auto_route and config.teleport_max_dx is None:
        raise ValueError("自动换层需要 --measure-teleport；也可先 --floor bottom --run 仅测试底层步行识别")
    window = GameWindow(config.window_title)
    stopped, paused = threading.Event(), threading.Event()
    executor = None
    listener = None
    guard = None
    preview_open = False
    preview_title = "Blue lion detector / F8 pause / F12 exit"

    def preview_events():
        if preview_open:
            try:
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    stopped.set()
                if cv2.getWindowProperty(preview_title, cv2.WND_PROP_VISIBLE) < 1:
                    stopped.set()
            except cv2.error:
                # Windows 上关闭预览后，窗口属性查询可能直接抛错。
                stopped.set()

    try:
        vision = Vision(config, root)
        brain = Brain(config, mode, start_alt=run)
        if run:
            executor = InputExecutor(Keyboard(), config,
                                     active=lambda: window.focused() and not paused.is_set() and not stopped.is_set())
            guard = executor.start_guard(stopped)

        def on_press(key):
            if key == Key.f12:
                stopped.set()

        def on_release(key):
            if key == Key.f8:
                if paused.is_set():
                    paused.clear()
                else:
                    paused.set()

        listener = Listener(on_press=on_press, on_release=on_release)
        listener.start()
        print("运行模式：" + ("发送按键" if run else "仅观察，不发送按键"))
        print("F8 暂停/恢复，F12 退出。请将游戏置于前台；失焦立即停止输入。")
        print(f"黄点导航，只打行进方向前方；底层 {config.patrol_round_trips} 个完整往返后沿途清怪去梯子。")
        print(f"识别后每组 {config.attack_burst_shots} 次攻击；步行途中随机插入 1～{config.random_attack_max_shots} 次。")
        if vision.mapper.notice:
            print(vision.mapper.notice)
        print(f"攻击距离 X={config.attack_range:.4f}，约 {config.attack_range * config.world_pixels_per_x:.0f}px；"
              f"间隔 {config.attack_interval:.1f}s。步行为主，巡逻瞬移至少间隔 {max(12, config.teleport_cooldown):.0f}s。")
        print(f"攻击高度：角色脚底向上 {config.player_height_px:.0f}px 到头顶；"
              "怪物头像到标定脚底的身体范围相交即可，BEHIND 表示身后。")
        if run:
            print("有效定位后先按一次 Alt，之后每 190 秒按一次；暂停/失焦后恢复不重复首按。")
        previous_status = ""
        previous_source = ""
        last_log = 0.0
        while not stopped.is_set():
            loop_start = time.monotonic()
            if paused.is_set() or not window.focused():
                if executor:
                    executor.stop()
                vision.reset()
                brain.reset_observation()
                if loop_start - last_log >= 2:
                    print("已暂停，按 F8 恢复。" if paused.is_set() else "游戏失焦：已松键，请点回游戏。")
                    last_log = loop_start
                preview_events()
                time.sleep(.05)
                continue
            frame = window.capture()
            obs = vision.observe(frame, focused=window.focused())
            action = brain.decide(obs, time.monotonic())
            if obs.location_source == "unmapped" and previous_source != "unmapped":
                print("主画面前方/高度暂未定位；黄点导航与随机攻击继续。可运行 --calibrate-anchor，点一次脚底建立参考。")
            previous_source = obs.location_source
            position = f"({obs.position[0]:.4f},{obs.position[1]:.4f})" if obs.position else "lost"
            ready = sum(target_status(m, obs, config, brain.direction) == "ready" for m in obs.mobs)
            height = sum(target_status(m, obs, config, brain.direction) == "height" for m in obs.mobs)
            status = (f"pos={position} floor={brain.floor} mobs={sum(m.confirmed for m in obs.mobs)}/{len(obs.mobs)} "
                      f"ready={ready} height={height} recovered={sum(m.recovered for m in obs.mobs)} "
                      f"screen={obs.location_source} laps={brain.completed_round_trips}/{config.patrol_round_trips} "
                      f"{action.kind}:{action.direction}")
            if action.reason != previous_status or loop_start - last_log >= 2:
                print(f"{status} | {action.reason}")
                previous_status, last_log = action.reason, loop_start
            if executor:
                try:
                    pressed_at = executor.execute(action)
                    brain.executed(action, pressed_at, obs)
                except InputCancelled:
                    brain.reset_observation()
                    vision.reset()
            # 观察模式不伪造攻击/瞬移反馈，因此不会将未发出的按键算作成功。
            if show:
                if not preview_open:
                    cv2.namedWindow(preview_title, cv2.WINDOW_NORMAL)
                    cv2.resizeWindow(preview_title, 640, round(frame.shape[0] * 640 / frame.shape[1]))
                    preview_open = True
                cv2.imshow(preview_title, vision.draw(frame, obs, status, brain.direction))
                preview_events()
            time.sleep(max(.001, 1 / 15 - (time.monotonic() - loop_start)))
    finally:
        stopped.set()
        if executor:
            executor.stop()
        if guard:
            guard.join(timeout=.5)
        if listener:
            listener.stop()
        window.close()
        cv2.destroyAllWindows()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="蓝独角狮识别与底层/顶层巡逻（默认只观察）")
    parser.add_argument("--config", type=Path, default=Path("config.local.json"))
    parser.add_argument("--floor", choices=("auto", "bottom", "top"), default="auto")
    parser.add_argument("--show", action="store_true", help="显示识别框；显示窗口不可遮挡游戏")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--calibrate", action="store_true")
    group.add_argument("--measure-scale", action="store_true")
    group.add_argument("--measure-teleport", action="store_true")
    group.add_argument("--check-position", action="store_true", help="实时显示小地图 X/Y 和黄点红圈，不要求完成校准")
    group.add_argument("--calibrate-map", action="store_true", help="独立校准底层/顶层边界、Y 和梯子，不重采模板")
    group.add_argument("--calibrate-anchor", action="store_true", help="点击一次脚底，建立黄点与主画面地形对应，不重采名牌/怪物")
    group.add_argument("--calibrate-height", action="store_true", help="同一截图点角色脚底、头顶，只校准攻击带身高，不重采模板")
    group.add_argument("--apply-map-profile", action="store_true", help="应用已确认 ROI=(24,73,137,103) 的地图实测值及单位换算")
    group.add_argument("--run", action="store_true", help="明确启用按键发送")
    group.add_argument("--observe", action="store_true", help="只观察（默认）")
    group.add_argument("--image", type=Path, help="离线检测一张客户区截图，不发送按键")
    parser.add_argument("--restart-calibration", action="store_true", help="配合 --calibrate 重新校准，保留旧图和备份")
    parser.add_argument("--reuse-templates", type=Path, help="配合 --calibrate 复用旧采样文件夹，补录丢失的脚底位置")
    parser.add_argument("--bottom-only", action="store_true", help="配合 --calibrate-map 仅记录底层并关闭自动换层")
    parser.add_argument("--output", type=Path, default=Path("debug-output/detections.png"))
    args = parser.parse_args(argv)
    if (args.restart_calibration or args.reuse_templates is not None) and not args.calibrate:
        parser.error("--restart-calibration / --reuse-templates 只能与 --calibrate 一起使用")
    if args.bottom_only and not args.calibrate_map:
        parser.error("--bottom-only 只能与 --calibrate-map 一起使用")
    path = args.config.resolve()
    try:
        if args.calibrate_height:
            from .calibrate import calibrate_height

            calibrate_height(path)
            return 0
        if args.calibrate_anchor:
            from .screen_map import calibrate_anchor

            calibrate_anchor(path)
            return 0
        if args.apply_map_profile:
            from .map_calibration import apply_map_profile

            apply_map_profile(path)
            return 0
        if args.calibrate_map:
            from .map_calibration import calibrate_map

            calibrate_map(path, bottom_only=args.bottom_only)
            return 0
        if args.calibrate or args.measure_scale or args.measure_teleport:
            from .calibrate import calibrate, measure_teleport, update_scale

            if args.calibrate:
                calibrate(path, restart=args.restart_calibration, reuse_templates=args.reuse_templates)
            else:
                function = update_scale if args.measure_scale else measure_teleport
                function(path)
            return 0
        config = load_config(path)
        if args.check_position:
            from .calibrate import check_position

            check_position(config)
            return 0
        config.require_calibration()
        if args.image:
            vision = Vision(config, path.parent)
            frame = read_image(args.image)
            obs = vision.observe(frame)
            # 单张截图只能提供候选，不能满足连续帧确认。
            write_image(args.output, vision.draw(frame, obs, f"single frame: {len(obs.mobs)} candidates"))
            print(f"角色坐标={obs.position}，脚底={obs.player_feet}，候选怪物={len(obs.mobs)}；输出={args.output}")
        else:
            live(config, path.parent, args.floor, args.run, args.show)
        return 0
    except (ValueError, RuntimeError, OSError, cv2.error) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
