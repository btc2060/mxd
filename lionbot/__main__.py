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

    window = GameWindow(config.window_title)
    stopped, paused = threading.Event(), threading.Event()
    executor = None
    listener = None
    guard = None
    preview_open = False
    original_threads = cv2.getNumThreads()
    preview_title = ("Plant" if config.behavior == "stationary" else "Blue lion") + " detector / F8 pause / F12 exit"

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
        if config.behavior == "stationary":
            cv2.setNumThreads(1)
        vision = Vision(config, root)
        if config.behavior == "stationary":
            from .stationary import StationaryBrain

            brain = StationaryBrain(config, start_alt=run)
        else:
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
        if config.behavior == "stationary":
            print(f"散步路2定点：X={config.station[0]:.4f}，Y={config.station[1]:.4f}；左右识别{config.monster_name}。")
            print(f"每组 {config.attack_burst_shots} 次，被击退后先就地清理两侧，清完再连续步行回位；无巡逻、爬绳、F 或随机攻击。")
            print(f"回位连续 {config.station_recover_timeout:g} 秒没有靠近目标才暂停，战斗时间不计入。")
            if config.station_step_recovery:
                print(f"右侧台阶返回：等下层落地→清怪→连续向左走→受阻后左＋空格；最多 {config.station_step_max_attempts} 次。")
            print(f"像素射程 {config.attack_reach_px:.0f}px，攻击间隔至少 {config.attack_interval:.1f}s。")
            print(f"局部识别：左右射程 × 角色身高，{config.stationary_fps:g} 帧/秒；模板自动支持镜像朝向。")
            if vision.color_detector is not None:
                if vision.color_detector.models:
                    print(f"颜色补识别已启用：花头轮廓＋邻近绿叶＋样本尺寸，"
                          f"连续 {max(config.confirmation_frames, config.plant_color_frames)} 帧确认。预览标记 COLOR。")
                else:
                    print("现有模板没有可用的棕色花头，继续模板识别；可补采清晰花头和邻近绿叶以启用颜色补识别。")
        else:
            print(f"黄点导航，停步清理身前身后；底层 {config.patrol_round_trips} 个完整往返后沿途清怪去梯子。")
            print(f"每组最多 {config.attack_burst_shots} 次，周围仍有目标就继续；"
                  f"连续 {config.clear_frames} 帧且至少 {config.clear_seconds:.1f}s 无候选后恢复原路线。")
            print(f"爬绳入口：清怪→微调→对齐→上＋空格并保持上→验证上升；最多尝试 {config.ladder_max_attempts} 次。")
            print("爬绳和下落期间不插入攻击、随机瞬移或 Alt；上爬卡住时先沿绳下返再重试。")
            if config.teleport_max_dx is None:
                print("未测量瞬移：全程使用步行；顶层离绳后向左走开，不影响自动上下楼。")
        if vision.mapper.notice:
            print(vision.mapper.notice)
        if config.behavior != "stationary":
            print(f"攻击距离 X={config.attack_range:.4f}，约 {config.attack_reach_px:.0f}px；"
                  f"间隔 {config.attack_interval:.1f}s。步行为主，巡逻瞬移至少间隔 {max(12, config.teleport_cooldown):.0f}s。")
        print(f"攻击高度：角色脚底向上 {config.player_height_px:.0f}px 到头顶；"
              "怪物头像到标定脚底的身体范围相交即可，两侧目标均参与清理。")
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
            obs = vision.observe(frame, focused=window.focused(), track_mobs=brain.needs_monsters)
            action = brain.decide(obs, time.monotonic())
            if obs.location_source == "unmapped" and previous_source != "unmapped":
                if config.behavior == "stationary":
                    print("主画面位置暂未定位，黄点仍可回位；攻击等待有效画面。"
                          "可在此配置下运行 --record-station 重建站位参考。")
                else:
                    print("主画面位置暂未定位，黄点仍有效；爬绳前需等待有效画面确认清怪。"
                          "可运行 --calibrate-anchor，点一次脚底建立参考。")
            previous_source = obs.location_source
            position = f"({obs.position[0]:.4f},{obs.position[1]:.4f})" if obs.position else "lost"
            ready = sum(target_status(m, obs, config) == "ready" for m in obs.mobs)
            height = sum(target_status(m, obs, config) == "height" for m in obs.mobs)
            status = (f"pos={position} floor={brain.floor} mobs={sum(m.confirmed for m in obs.mobs)}/{len(obs.mobs)} "
                      f"ready={ready} height={height} recovered={sum(m.recovered for m in obs.mobs)} "
                      f"screen={obs.location_source} laps={brain.completed_round_trips}/{config.patrol_round_trips} "
                      f"move={brain.direction if config.behavior == 'patrol' else '-'} face={brain.facing or '?'} clear={int(brain.clearing)} "
                      f"phase={brain.phase} route={brain.route or '-'} grabs={brain.grab_attempts} "
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
                direction = brain.facing if config.behavior == "stationary" else brain.direction
                cv2.imshow(preview_title, vision.draw(frame, obs, status, direction))
                preview_events()
            fps = config.stationary_fps if config.behavior == "stationary" else 15
            time.sleep(max(.001, 1 / fps - (time.monotonic() - loop_start)))
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
        if config.behavior == "stationary":
            cv2.setNumThreads(original_threads)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="蓝独角狮巡逻 / 食人花单站位挂机（默认只观察）")
    parser.add_argument("--config", type=Path, help="配置路径；定点采集默认 config.plants.local.json，其他默认 config.local.json")
    parser.add_argument("--floor", choices=("auto", "bottom", "top"), default="auto")
    parser.add_argument("--show", action="store_true", help="显示识别框；显示窗口不可遮挡游戏")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--calibrate", action="store_true")
    group.add_argument("--calibrate-stationary", action="store_true", help="独立采集散步路2食人花模板、单站位和射程")
    group.add_argument("--record-station", action="store_true", help="只更新固定站位、身高和地形参考，保留食人花模板")
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
    default = "config.plants.local.json" if args.calibrate_stationary or args.record_station else "config.local.json"
    path = (args.config or Path(default)).resolve()
    try:
        if args.calibrate_stationary or args.record_station:
            from .station_calibration import calibrate_stationary, record_station

            (calibrate_stationary if args.calibrate_stationary else record_station)(path)
            return 0
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
        if config.behavior == "stationary" and args.floor != "auto":
            raise ValueError("定点模式不使用 --floor；请省略此参数")
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
