from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
from contextlib import ExitStack,redirect_stdout
from types import ModuleType,SimpleNamespace
import io
import threading
import time

from lionbot.pig_cycle import PigConfig,PigCycle,PigAction,load_pig,save_pig
from lionbot.pig import PigExecutor,main,run_pig,record_route
from lionbot.config import Config
from tests.test_controls import Clock,Keyboard


def config(**kwargs):return replace(PigConfig(walk_attack_chance=0,attack_zones=(),alt_interval=0),**kwargs)


class PigTests(unittest.TestCase):
    def test_four_normal_left_turns_then_full_left(self):
        brain=PigCycle(config(edge_attack=False))
        for _ in range(4):
            self.assertEqual(brain.decide((.82,.1),0).direction,'left')
            self.assertEqual(brain.decide((.24,.1),1).direction,'right')
        brain.decide((.82,.1),2)
        self.assertEqual(brain.decide((.24,.1),3).direction,'left')
        self.assertEqual(brain.decide((.15,.1),4).direction,'right')
        self.assertEqual(brain.left_count,0)

    def test_zone_attacks_twice_and_returns_to_walking(self):
        brain=PigCycle(config(attack_zones=((.48,.55),)))
        a=brain.decide((.5,.1),0);self.assertEqual(a.kind,'attack');brain.executed(a,0)
        self.assertEqual(brain.decide((.5,.1),.1).kind,'wait')
        a=brain.decide((.5,.1),.31);self.assertEqual(a.key,'a');brain.executed(a,.31)
        self.assertEqual(brain.decide((.5,.1),.5).kind,'walk')

    def test_pit_entry_attack_then_stalled_walk_jump_with_cooldown(self):
        brain=PigCycle(config())
        a=brain.decide((.37,.7),0);self.assertEqual(a.kind,'attack');brain.executed(a,0)
        kinds=[]
        for i in range(1,16):
            a=brain.decide((.37,.7),i*.1);kinds.append(a.kind);brain.executed(a,i*.1)
        self.assertEqual(kinds.count('jump'),1)

    def test_timed_slots_zero_disabled_and_no_catchup_bursts(self):
        slots=tuple({'key':str(i+1),'interval_seconds':0 if i==0 else (i+1)*10,'hold_seconds':.1} for i in range(4))
        brain=PigCycle(config(timed_keys=slots))
        brain.decide((.5,.1),0)
        self.assertEqual(brain.decide((.5,.1),19).kind,'walk')
        action=brain.decide((.5,.1),20);self.assertEqual((action.key,action.slot),('2',1));brain.executed(action,20)
        action=brain.decide((.5,.1),1000);self.assertEqual(action.key,'2');brain.executed(action,1000)
        action=brain.decide((.5,.1),1000.1);self.assertEqual(action.key,'3');brain.executed(action,1000.1)
        action=brain.decide((.5,.1),1000.2);self.assertEqual(action.key,'4');brain.executed(action,1000.2)
        self.assertEqual(brain.decide((.5,.1),1000.3).kind,'walk')

    def test_no_position_no_attack_or_timer_and_pause_keeps_timers(self):
        brain=PigCycle(config(alt_interval=190))
        self.assertEqual(brain.decide(None,10000).kind,'wait')
        a=brain.decide((.5,.1),10000);self.assertEqual(a.key,'alt');brain.executed(a,10000)
        brain.suspend()
        self.assertEqual(brain.decide((.5,.1),10001).kind,'walk')
        self.assertEqual(brain.decide((.5,.1),10190).key,'alt')

    def test_key_interrupt_releases_all_and_records_actual_press(self):
        clock=Clock();keyboard=Keyboard(clock);active=[True]
        def sleep(t):clock.sleep(t);active[0]=False
        executor=PigExecutor(keyboard,Config(),active=lambda:active[0],clock=clock.time,sleep=sleep)
        sent=executor.execute(PigAction('timed',key='1',hold=.5,slot=0))
        self.assertEqual(sent,0);self.assertFalse(executor.held)
        self.assertEqual([event[:2] for event in keyboard.events],[('press','1'),('release','1')])

    def test_ground_stuck_counts_walk_only_and_position_loss_clears_it(self):
        brain=PigCycle(config(ground_stuck_seconds=.5,ground_jump_attempts=0))
        for i in range(7):
            a=brain.decide((.5,.1),i*.1);brain.executed(a,i*.1)
        self.assertEqual(brain.direction,'left')
        brain.decide(None,20);self.assertEqual(brain.walk_seconds,0)

    def test_right_pit_lip_and_airborne_y_do_not_reverse(self):
        brain=PigCycle(config(right_turn=.735))
        a=brain.decide((.72,.7),0);brain.executed(a,0)
        kinds=[]
        for i in range(1,24):
            # Just outside the old .736 pit limit, with airborne Y fluctuations.
            a=brain.decide((.742,.1 if i%2 else .7),i*.1)
            self.assertEqual(brain.direction,'right')
            kinds.append(a.kind);brain.executed(a,i*.1)
        self.assertGreaterEqual(kinds.count('jump'),2)

    def test_exit_requires_multiple_frames_and_boundary_still_turns(self):
        brain=PigCycle(config())
        a=brain.decide((.72,.7),0);brain.executed(a,0)
        brain.decide((.78,.1),.1);self.assertIsNotNone(brain.pit_index)
        brain.decide((.78,.1),.2);self.assertIsNotNone(brain.pit_index)
        brain.decide((.78,.1),.3);self.assertIsNone(brain.pit_index)
        self.assertEqual(brain.decide((.82,.1),.4).direction,'left')

    def test_unrecognized_pit_tries_jumps_before_reversing(self):
        brain=PigCycle(config(ground_stuck_seconds=.5))
        jumps=0
        for i in range(60):
            a=brain.decide((.77,.7),i*.1)
            if a.kind=='jump':jumps+=1
            brain.executed(a,i*.1)
            if brain.direction=='left':break
        self.assertEqual(jumps,2)
        self.assertEqual(brain.direction,'left')

    def test_jump_keeps_horizontal_key_until_next_walk_or_guard_timeout(self):
        for direction in ('left','right'):
            clock=Clock();keyboard=Keyboard(clock)
            executor=PigExecutor(keyboard,Config(),clock=clock.time,sleep=clock.sleep)
            executor.execute(PigAction('jump',direction,hold=.36))
            self.assertEqual(keyboard.held,{direction})
            executor.execute(PigAction('walk',direction))
            self.assertEqual(keyboard.held,{direction})
            clock.sleep(.26);executor.guard_once()
            self.assertFalse(keyboard.held)

    def test_record_route_preserves_other_parameters_and_rejects_bad_order(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.pig.local.json'
            c=config(frame_size=(640,480),minimap_roi=(0,0,100,50))
            save_pig(path,c)
            with patch('lionbot.windows.GameWindow') as window, patch('lionbot.calibrate.fresh',return_value=np.zeros((480,640,3),dtype=np.uint8)), \
                 patch('lionbot.vision.minimap_position',side_effect=[(.18,.1),(.84,.1)]),redirect_stdout(io.StringIO()):
                record_route(path)
            updated=load_pig(path)
            self.assertEqual((updated.left_full,updated.right_turn),(.18,.84))
            self.assertAlmostEqual(updated.left_normal,.27)
            self.assertEqual(updated.timed_keys,c.timed_keys)
            before=path.read_bytes()
            with patch('lionbot.windows.GameWindow'),patch('lionbot.calibrate.fresh',return_value=np.zeros((480,640,3),dtype=np.uint8)), \
                 patch('lionbot.vision.minimap_position',side_effect=[(.8,.1),(.2,.1)]),redirect_stdout(io.StringIO()),self.assertRaises(ValueError):
                record_route(path)
            self.assertEqual(path.read_bytes(),before)

    def test_config_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'config.pig.local.json';save_pig(p,PigConfig())
            self.assertEqual(load_pig(p).timed_keys,PigConfig().timed_keys)
        for kwargs in ({'timed_keys':()},{'alt_interval':-1},{'attack_key':'f8'},{'jump_hold':2}):
            with self.assertRaises(ValueError):config(**kwargs).validate()
        with patch('lionbot.pig.run_pig') as run:
            self.assertEqual(main(['--config','/tmp/pig-unconfigured-example.json','--run']),1)
            run.assert_not_called()

    def test_runtime_alarm_blocks_all_game_keys_and_minimap_capture(self):
        from lionbot.lie_alert import AlertState
        from lionbot.pig_cycle import KEYS
        clock=Clock();keyboard=Keyboard(clock);keyboard.special={}
        window=Mock();window.focused.return_value=True
        names=set(KEYS)|{'f8','f10','f12'}
        keys=SimpleNamespace(**{key:key for key in names})
        class Listener:
            def __init__(self,on_release):self.callback=on_release
            def start(self):
                def exit_later():time.sleep(.05);self.callback('f12')
                self.thread=threading.Thread(target=exit_later);self.thread.start()
            def stop(self):self.thread.join()
        class Monitor:
            def __init__(self,*args,**kwargs):self.state=AlertState();self.callback=args[3]
            def start(self):self.state.publish(hit=True);self.callback('检测到测谎窗口')
            def stop(self):pass
            def status(self):return 'alarm'
        module=ModuleType('pynput');child=ModuleType('pynput.keyboard');child.Key=keys;child.Listener=Listener;module.keyboard=child
        c=config(frame_size=(640,480),minimap_roi=(0,0,100,50))
        with ExitStack() as stack:
            stack.enter_context(patch.dict('sys.modules',{'pynput':module,'pynput.keyboard':child}))
            stack.enter_context(patch('lionbot.windows.require_windows'))
            stack.enter_context(patch('lionbot.windows.GameWindow',return_value=window))
            stack.enter_context(patch('lionbot.windows.Keyboard',return_value=keyboard))
            stack.enter_context(patch('lionbot.lie_alert.load_detector',return_value=Mock()))
            stack.enter_context(patch('lionbot.lie_alert.LieAlertMonitor',Monitor))
            stack.enter_context(patch('lionbot.pig.prepare_alert_sound',return_value=Mock()))
            stack.enter_context(redirect_stdout(io.StringIO()))
            run_pig(c,Path('/tmp/config.pig.local.json'),True)
        self.assertEqual(keyboard.events,[])
        window.capture_region.assert_not_called();window.close.assert_called_once()
