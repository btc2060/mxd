"""python -m lionbot.pig --setup / --run: original 2.py route with alarm interlock."""
import argparse
from dataclasses import replace
from pathlib import Path
import threading
import time

from .brain import Action
from .config import Config
from .controls import InputCancelled, InputExecutor
from .garden_sound import prepare_alert_sound, voice_number
from .pig_cycle import PigConfig, PigCycle, load_pig, save_pig


class PigExecutor(InputExecutor):
    def execute(self, action):
        if action.kind=='walk':return super().execute(Action('walk',action.direction))
        self.stop()
        pressed=self.clock()
        sent=False
        try:
            self.check()
            if action.kind=='wait':return pressed
            if action.kind in ('attack','jump'):
                self.press(action.direction);self.delay(.04)
                if action.kind=='attack':self.release(action.direction)
            pressed=self.press('space' if action.kind=='jump' else action.key)
            sent=True
            self.delay(action.hold)
            return pressed
        except InputCancelled:
            if sent:return pressed
            raise
        finally:self.stop()


def setup(path):
    import cv2
    from .calibrate import fresh, select_roi
    from .vision import minimap_position
    from .windows import GameWindow
    config=load_pig(path)
    if not path.exists():
        from .garden_cycle import load_garden
        previous=path.parent/'config.garden.local.json'
        if previous.exists():
            old=load_garden(previous)
            config=replace(config,**{key:getattr(old,key) for key in ('window_title','lie_alert_template','lie_alert_interval','lie_alert_threshold','lie_alert_sound')})
    window=GameWindow(config.window_title)
    try:
        frame=fresh(window,'手动站在 pig 平台；此步骤只截图，不发送按键。')
        print('框选 pig 小地图定位区域。原 2.py 使用窄条，归一化坐标与框选范围相关；框选后核对 X/Y 和边界。')
        roi=select_roi('Pig minimap / Enter confirm',frame)
        config=replace(config,frame_size=tuple(frame.shape[1::-1]),minimap_roi=roi)
        config.validate(ready=True)
        if minimap_position(frame,roi) is None:raise ValueError('未找到唯一黄点，请避开小地图标题和黄色按钮')
        save_pig(path,config);print(f'已保存 {path}，无需角色、怪物或地形采样。')
    finally:window.close();cv2.destroyAllWindows()


def run_pig(config,path,send_keys=False,show=False):
    import cv2
    from .windows import GameWindow, Keyboard, require_windows
    from .lie_alert import LieAlertMonitor, load_detector
    from .vision import minimap_position
    require_windows();config.validate(ready=True)
    from pynput.keyboard import Key, Listener
    detector=load_detector(config,path.parent) if config.lie_alert_enabled else None
    sound=prepare_alert_sound(config.lie_alert_sound,path.parent) if detector else None
    window=GameWindow(config.window_title,frame_size=config.frame_size)
    stopped=threading.Event();paused=threading.Event();ack=threading.Event()
    executor=guard=monitor=listener=None
    brain=PigCycle(config,start_alt=send_keys)
    threads=cv2.getNumThreads();preview=False
    try:
        cv2.setNumThreads(1)
        def on_alarm(reason):
            paused.set()
            if executor:executor.stop()
            print(f'警报：{reason}！已松开全部键并暂停。F8静音；手动处理、标题消失后再F8恢复。',flush=True)
        if detector:
            monitor=LieAlertMonitor(detector,config.frame_size,lambda:GameWindow(config.window_title,hwnd=window.hwnd),on_alarm,
                                    config.lie_alert_interval,sound=sound,diagnostic_dir=path.parent/'debug-output/pig/lie-alert')
        if send_keys:
            keyboard=Keyboard()
            for name in ('ctrl','shift','tab','enter','home','end','insert','delete','page_up','page_down',*(f'f{i}' for i in range(1,13))):
                keyboard.special[name]=getattr(Key,name)
            executor=PigExecutor(keyboard,Config(),active=lambda:window.focused() and not paused.is_set() and not stopped.is_set()
                                 and (monitor is None or monitor.state.allows_input()))
            guard=executor.start_guard(stopped)
        def release(key):
            if key==Key.f12:stopped.set()
            elif key==Key.f8:
                if monitor and monitor.state.is_latched():ack.set()
                elif paused.is_set():paused.clear()
                else:
                    paused.set()
                    if executor:executor.stop()
            elif key==Key.f10 and monitor:monitor.request_snapshot()
        listener=Listener(on_release=release);listener.start()
        if monitor:monitor.start()
        print('Pig：'+('发送按键' if send_keys else '仅观察，不发送按键')+'；不识别怪物。F8暂停/恢复，F10诊断，F12退出。',flush=True)
        print(f'攻击键={config.attack_key}；正常左端={config.left_normal}，最左={config.left_full}，右端={config.right_turn}；Alt={config.alt_interval}s',flush=True)
        print('4组定时键：'+str(config.timed_keys)+'；间隔0关闭，按实际发送时间重新计时。',flush=True)
        logged=scan_log=float('-inf')
        while not stopped.is_set():
            now=time.monotonic()
            if ack.is_set():
                ack.clear()
                if monitor and monitor.state.acknowledge(focused=window.focused()):
                    paused.clear();print('测谎标题已连续两帧消失，已人工恢复。',flush=True)
                else:print('已静音，尚不能恢复；处理完后再F8。',flush=True)
            allowed=window.focused() and not paused.is_set() and (monitor is None or monitor.state.allows_input())
            if monitor and monitor.state.is_latched():paused.set();allowed=False
            if not allowed:
                if executor:executor.stop()
                brain.suspend()
                if now-logged>=2:print('暂停／失焦／测谎监控未就绪，保持松键。',flush=True);logged=now
                stopped.wait(.05);continue
            image=window.capture_region(config.minimap_roi,config.frame_size)
            position=minimap_position(image,(0,0,image.shape[1],image.shape[0]))
            action=brain.decide(position,now)
            if now-logged>=1:
                print(f'pos={position} direction={brain.direction} left_count={brain.left_count} {action.kind}: {action.reason}',flush=True);logged=now
            if monitor and now-scan_log>=5:print(monitor.status(),flush=True);scan_log=now
            if executor:
                try:brain.executed(action,executor.execute(action))
                except InputCancelled:brain.suspend()
            if show:
                preview=True;cv2.imshow('Pig minimap',image)
                if cv2.waitKey(1)&0xff in (27,ord('q')):stopped.set()
            stopped.wait(max(.001,1/config.fps-(time.monotonic()-now)))
    finally:
        stopped.set()
        try:
            if executor:executor.stop()
        finally:
            if monitor:monitor.stop()
            if guard:guard.join(timeout=.5)
            if listener:listener.stop()
            window.close();cv2.setNumThreads(threads)
            if preview:cv2.destroyAllWindows()


def main(argv=None):
    parser=argparse.ArgumentParser(description='Pig单层巡逻：小地图定位、定点攻击、跳坑、4组定时键与测谎停键报警')
    parser.add_argument('--config',type=Path,default=Path('config.pig.local.json'))
    group=parser.add_mutually_exclusive_group()
    for flag in ('setup','run','observe','calibrate-alert','test-alert-sound'):
        group.add_argument('--'+flag,action='store_true')
    parser.add_argument('--show',action='store_true')
    parser.add_argument('--image',type=Path)
    parser.add_argument('--alert-voice',type=voice_number)
    args=parser.parse_args(argv);path=args.config.resolve()
    if args.image and not args.calibrate_alert:parser.error('--image 仅用于 --calibrate-alert')
    try:
        if args.setup:setup(path)
        elif args.calibrate_alert:
            from .lie_alert import calibrate_alert
            calibrate_alert(path,args.image,loader=load_pig,saver=save_pig)
        else:
            config=load_pig(path)
            if args.alert_voice:config=replace(config,lie_alert_sound=args.alert_voice)
            if args.test_alert_sound:
                sound=prepare_alert_sound(config.lie_alert_sound,path.parent)
                for _ in range(3):sound();time.sleep(.3)
            else:
                config.validate(ready=True)
                run_pig(config,path,args.run,args.show)
        return 0
    except KeyboardInterrupt:return 0
    except (ValueError,TypeError,OSError,RuntimeError) as error:
        print(f'错误：{error}',flush=True);return 1


if __name__=='__main__':raise SystemExit(main())
