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
    try:
        vision = Vision(config, root)
        brain = Brain(config, mode)
        if run:
            executor = InputExecutor(Keyboard(), config,
                                     active=lambda: window.focused() and not paused.is_set() and not stopped.is_set())

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
        print("所有坐标基于已校准的小地图；不处理第二个台阶或中间层。")
        previous_status = ""
        last_log = 0.0
        while not stopped.is_set():
            loop_start = time.monotonic()
            if paused.is_set() or not window.focused():
                if executor:
                    executor.stop()
                vision.confirmer.reset()
                brain.reset_observation()
                brain.last_mob_at = loop_start
                time.sleep(.05)
                continue
            frame = window.capture()
            obs = vision.observe(frame, focused=window.focused())
            action = brain.decide(obs, time.monotonic())
            position = f"({obs.position[0]:.4f},{obs.position[1]:.4f})" if obs.position else "lost"
            ready = sum(target_status(m, obs, config) == "ready" for m in obs.mobs)
            height = sum(target_status(m, obs, config) == "height" for m in obs.mobs)
            status = (f"pos={position} floor={brain.floor} mobs={sum(m.confirmed for m in obs.mobs)}/{len(obs.mobs)} "
                      f"ready={ready} height={height} {action.kind}:{action.direction}")
            if action.reason != previous_status or loop_start - last_log >= 2:
                print(f"{status} | {action.reason}")
                previous_status, last_log = action.reason, loop_start
            if executor:
                try:
                    pressed_at = executor.execute(action)
                    brain.executed(action, pressed_at, obs)
                except InputCancelled:
                    brain.reset_observation()
                    vision.confirmer.reset()
            # 观察模式不伪造攻击/瞬移反馈，因此不会将未发出的按键算作成功。
            if show:
                cv2.imshow("Blue lion detector / F8 pause / F12 exit", vision.draw(frame, obs, status))
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    stopped.set()
            time.sleep(max(.001, 1 / 15 - (time.monotonic() - loop_start)))
    finally:
        if executor:
            executor.stop()
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
