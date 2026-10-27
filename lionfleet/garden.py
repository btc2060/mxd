"""Independent multi-host Garden entry; does not modify lionbot's single-host runtime."""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from .config import load_machine
from .transport import FleetClient


def check_or_test(machine, machine_path, test=False, clear_test=False):
    # No OpenCV, game window, mouse or keyboard controller is imported on this path.
    client = FleetClient(machine, machine_path.parent)
    try:
        client.start()
        if not client.wait_ready():
            raise RuntimeError(f"未收到接收端确认：{client.last_error}；检查 IP、端口、防火墙、机号登记")
        print(f"连接成功：{machine.machine_id} {machine.machine_name} KVM {machine.kvm_port} → "
              f"{machine.receiver_ip}:{machine.receiver_port}", flush=True)
        client.set_phase("observing")
        if test:
            client.raise_alarm("test", "人工运行测试通知，不是游戏测谎；未操作游戏")
            if not client.flush(5):
                raise RuntimeError("测试通知尚未收到回执，已保留测试事件，可重试或 --clear-test")
            print("接收端已确认测试报警，请听机号播报。3 秒后发送测试结束。", flush=True)
            time.sleep(3)
            client.clear_alarms(("test",))
            if not client.flush(5):
                raise RuntimeError("测试结束通知尚未确认，重新连接后会通过心跳补发")
        elif clear_test:
            client.clear_alarms(("test",))
            if not client.flush(5):
                raise RuntimeError("测试解除通知未确认，请重试")
            print("仅解除测试事件，未解除真实测谎或故障。", flush=True)
        if client.pending_alarms():
            print("本机仍保存有待处理报警；正式运行后须在本机检查并按 F8 人工恢复。", flush=True)
    finally:
        client.close()


def run_garden(config, garden_path, machine, machine_path, send_keys=False, show=False):
    import cv2
    from lionbot.controls import InputCancelled, InputExecutor
    from lionbot.garden_cycle import GardenCycle
    from lionbot.garden_vision import GardenVision
    from lionbot.lie_alert import LieAlertMonitor, beep_alert, load_detector
    from lionbot.windows import GameWindow, Keyboard, require_windows

    require_windows()
    from pynput.keyboard import Key, Listener

    config.validate(ready=True)
    if not config.lie_alert_enabled:
        raise ValueError("多机入口需要 lie_alert_enabled=true；继续复用原测谎标题模板")
    detector = load_detector(config, garden_path.parent)
    stopped, paused, acknowledge = (threading.Event() for _ in range(3))
    executor = listener = monitor = guard = window = None
    preview_open = False
    original_threads = cv2.getNumThreads()
    title = f"{machine.machine_id} Garden / F8 pause / F10 snapshot / F12 exit"

    def emergency(kind, reason):
        paused.set()
        if executor:
            try:
                executor.stop()
            except Exception as error:
                print(f"松键异常：{error}", flush=True)
        print(f"[{machine.machine_name}／KVM {machine.kvm_port}] {reason}；保持暂停，本机 F8 人工处理／恢复。", flush=True)

    client = FleetClient(machine, machine_path.parent, on_emergency=emergency, sound=beep_alert)

    def preview_events():
        if preview_open:
            try:
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27) or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                    stopped.set()
            except cv2.error:
                stopped.set()

    try:
        client.start()
        print(f"[{machine.machine_name}] 正在连接接收端 {machine.receiver_ip}:{machine.receiver_port}，成功前不发送游戏按键……", flush=True)
        if not client.wait_ready():
            raise RuntimeError(f"接收端未确认，未启动挂机：{client.last_error}")
        cv2.setNumThreads(1)
        window = GameWindow(config.window_title, frame_size=config.frame_size)
        vision = GardenVision(config, garden_path.parent)
        brain = GardenCycle(config, start_alt=send_keys)

        def on_alarm(reason):
            # Stop locally BEFORE any file/network work, including a broken LAN.
            paused.set()
            if executor:
                try:
                    executor.stop()
                except Exception as error:
                    print(f"松键异常：{error}", flush=True)
            kind = "lie_detector" if reason == "检测到测谎窗口" else "monitor_fault"
            client.raise_alarm(kind, reason)

        monitor = LieAlertMonitor(detector, config.frame_size,
                                  lambda: GameWindow(config.window_title, hwnd=window.hwnd), on_alarm,
                                  config.lie_alert_interval, sound=lambda: None,
                                  diagnostic_dir=garden_path.parent / "debug-output" / "lie-alert")
        if send_keys:
            executor = InputExecutor(Keyboard(), config.vision_config(),
                                     active=lambda: window.focused() and not paused.is_set() and not stopped.is_set()
                                     and client.allows_input() and monitor.state.allows_input())
            guard = executor.start_guard(stopped)

        def on_press(key):
            if key == Key.f12:
                stopped.set()

        def on_release(key):
            if key == Key.f8:
                if monitor.state.is_latched() or client.pending_alarms():
                    acknowledge.set()
                else:
                    paused.clear() if paused.is_set() else paused.set()
            elif key == Key.f10:
                monitor.request_snapshot()

        listener = Listener(on_press=on_press, on_release=on_release)
        listener.start()
        monitor.start()
        client.arm_runtime()
        print(f"多机 Garden：{machine.machine_id} {machine.machine_name}／KVM {machine.kvm_port}；"
              + ("发送游戏按键" if send_keys else "仅观察，不发送游戏按键"), flush=True)
        print(f"保留原参数：D 每组 {config.attack_burst_shots} 次，Alt {config.alt_interval:g}s，"
              f"原地超时 {config.stationary_timeout_seconds:g}s；F8 暂停／静音／人工恢复，F10 诊断，F12 退出。", flush=True)
        print("网络回执和接收端静音都不会恢复挂机；旧报警在进程重启后保留。请切回游戏。", flush=True)
        last_log, last_reason = float("-inf"), ""
        while not stopped.is_set():
            started = time.monotonic()
            client.touch_runtime(scan_at=monitor.state.last_scan)
            preview_events()
            if stopped.is_set():
                break
            if acknowledge.is_set():
                acknowledge.clear()
                client.mute_local()
                expected = {item["id"] for item in client.pending_alarms()}
                # A new detector hit must not race with clearing the old alarm.
                with monitor.state.lock:
                    if monitor.state.is_latched():
                        monitor.state.acknowledge(focused=window.focused())
                    recovered = window.focused() and monitor.state.allows_input() and client.connected()
                    if recovered:
                        client.clear_alarms(expected_ids=expected)
                if recovered and not client.pending_alarms():
                    paused.clear()
                    brain.suspend()
                    vision.reset()
                    print("本机人工恢复成功，已向接收端发送解除事件。", flush=True)
                else:
                    print("本地报警已静音；测谎仍在、截图无效、失焦或接收端失联，继续暂停。", flush=True)
            focused = window.focused()
            if not client.connected() or client.pending_alarms() or paused.is_set() or not focused or not monitor.state.allows_input():
                if executor:
                    executor.stop()
                brain.suspend()
                vision.reset()
                phase = "paused" if paused.is_set() or client.pending_alarms() else "unfocused" if not focused else "monitor_wait"
                client.set_phase(phase)
                if started - last_log >= 3:
                    print(f"已松键：{phase}；接收端={'在线' if client.connected() else '失联'}；"
                          f"待处理报警={len(client.pending_alarms())}。", flush=True)
                    last_log = started
                time.sleep(.05)
                continue
            client.set_phase("running" if send_keys else "observing")
            try:
                frame = window.capture()
                obs = vision.observe(frame, focused=window.focused(), now=time.monotonic())
                was_recovering = brain.recovering
                action = brain.decide(obs, time.monotonic())
                if brain.recovering != was_recovering:
                    vision.reset()
                if action.reason != last_reason or started - last_log >= 2:
                    position = str(tuple(round(v, 4) for v in obs.position)) if obs.position else "黄点丢失"
                    print(f"[{machine.machine_id}] pos={position} laps={brain.completed_round_trips} "
                          f"{action.kind}:{action.direction} | {action.reason}", flush=True)
                    last_reason, last_log = action.reason, started
                if show:
                    if not preview_open:
                        cv2.namedWindow(title, cv2.WINDOW_NORMAL)
                        cv2.resizeWindow(title, 640, round(frame.shape[0] * 640 / frame.shape[1]))
                        preview_open = True
                    cv2.imshow(title, vision.draw(frame, obs, f"{machine.machine_id} | {action.kind}", brain.direction))
                    preview_events()
                if executor:
                    try:
                        brain.executed(action, executor.execute(action), obs)
                        if action.kind == "teleport":
                            vision.reset()
                    except InputCancelled:
                        brain.suspend()
                        vision.reset()
            except (OSError, ValueError, RuntimeError, cv2.error) as error:
                emergency("runtime_fault", str(error))
                client.raise_alarm("runtime_fault", f"截图／挂机异常：{error}")
            time.sleep(max(.001, 1 / config.fps - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        if client.connected():
            client.raise_alarm("runtime_fault", f"多机程序未启动：{error}")
            client.flush(1)
        raise
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
        if window:
            window.close()
        client.close()
        cv2.setNumThreads(original_threads)
        if preview_open:
            cv2.destroyAllWindows()


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立多机 Garden；原 lionbot.garden 单机入口保留")
    parser.add_argument("--config", type=Path, default=Path("config.garden.local.json"))
    parser.add_argument("--machine-config", type=Path, default=Path("config.machine.local.json"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-connection", action="store_true", help="只验证接收端回执，不操作游戏")
    modes.add_argument("--test-alert", action="store_true", help="发送机号测试报警，不操作游戏")
    modes.add_argument("--clear-test", action="store_true", help="只解除中断后残留的测试事件")
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--observe", action="store_true", help="默认仅观察")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args(argv)
    try:
        machine_path = args.machine_config.resolve()
        machine = load_machine(machine_path)
        if args.check_connection or args.test_alert or args.clear_test:
            check_or_test(machine, machine_path, args.test_alert, args.clear_test)
        else:
            from lionbot.garden_cycle import load_garden
            path = args.config.resolve()
            run_garden(load_garden(path), path, machine, machine_path, args.run, args.show)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"多机端未能运行：{error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
