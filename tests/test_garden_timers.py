from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from lionbot.brain import Action
from lionbot.controls import InputCancelled
from lionbot.garden_cycle import GardenCycle,load_garden,save_garden
from lionbot.timed_keys import TimedKeys,TimedInputExecutor,bind_timed_special_keys
from types import SimpleNamespace
from unittest.mock import Mock,call
from tests.test_garden import config,observation
from tests.test_controls import Clock,Keyboard


def slots():
    return tuple({'key':str(i+1),'interval_seconds':0 if i==0 else 10*(i+1),'hold_seconds':.1} for i in range(4))


class GardenTimerTests(unittest.TestCase):
    def test_zero_disabled_and_missed_intervals_not_replayed(self):
        timer=TimedKeys(slots());self.assertIsNone(timer.action(0))
        self.assertIsNone(timer.action(19))
        self.assertEqual(timer.action(20).direction,'2')
        timer.executed('2',20)
        for key in ('2','3','4'):
            self.assertEqual(timer.action(1000).direction,key)
            timer.executed(key,1000)
        self.assertIsNone(timer.action(1000))

    def test_valid_floor_and_focus_are_required_and_resume_preserves_due(self):
        c=config(timed_keys=slots(),teleport_enabled=False,stationary_timeout_seconds=100)
        brain=GardenCycle(c,start_alt=False)
        brain.decide(observation(),0)
        self.assertNotEqual(brain.decide(observation(focused=False),25).kind,'timed_key')
        self.assertNotEqual(brain.decide(observation(y=.2),25).kind,'timed_key')
        action=brain.decide(observation(),25)
        self.assertEqual((action.kind,action.direction),('timed_key','2'))
        brain.executed(action,25,observation());brain.suspend()
        self.assertEqual(brain.timed_keys.next_due[1],45)

    def test_timer_does_not_interrupt_teleport_settle_or_recovery(self):
        brain=GardenCycle(config(timed_keys=slots()),start_alt=False)
        brain.decide(observation(),0)
        brain.busy_until=30
        self.assertEqual(brain.decide(observation(),25).kind,'wait')
        brain.busy_until=0;brain.recovery_origin_x=.5
        self.assertNotEqual(brain.decide(observation(),25).kind,'timed_key')

    def test_key_tap_releases_walking_and_records_actual_press_on_interrupt(self):
        clock=Clock();keyboard=Keyboard(clock);active=[True]
        def sleep(dt):clock.sleep(dt);active[0]=False
        c=config()
        executor=TimedInputExecutor(keyboard,c.vision_config(),timed_keys=slots(),clock=clock.time,sleep=sleep,active=lambda:active[0])
        executor.press('left')
        stamp=executor.execute(Action('timed_key','2'))
        self.assertEqual(stamp,0)
        self.assertFalse(keyboard.held)
        self.assertEqual([e[:2] for e in keyboard.events],[('press','left'),('release','left'),('press','2'),('release','2')])
        before=len(keyboard.events)
        with self.assertRaises(InputCancelled):executor.execute(Action('timed_key','2'))
        self.assertEqual(len(keyboard.events),before)

    def test_validation_and_existing_config_roundtrip(self):
        c=config(timed_keys=slots())
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'config.garden.local.json';save_garden(p,c)
            self.assertEqual(load_garden(p).timed_keys,c.timed_keys)
        for values in ((),({'key':'f8','interval_seconds':30,'hold_seconds':.1},)*4,
                       ({'key':'alt','interval_seconds':30,'hold_seconds':.1},)*4,
                       ({'key':'1','interval_seconds':-1,'hold_seconds':.1},)*4):
            with self.assertRaises(ValueError):replace(c,timed_keys=values).validate()

    def test_frequent_timers_do_not_erase_existing_stationary_watch(self):
        brain=GardenCycle(config(timed_keys=slots()),start_alt=False)
        brain.decide(observation(),0)
        brain.executed(Action('timed_key','2'),4,observation())
        self.assertAlmostEqual(brain.stationary_at,.9)
        self.assertEqual(brain.recover_position(.5,6).kind,'walk')
        self.assertTrue(brain.recovering)

    def test_letters_and_uppercase_function_keys_reach_keyboard_backend(self):
        from lionbot.windows import Keyboard as RealKeyboard
        values=({'key':' F ','interval_seconds':10,'hold_seconds':.1},
                {'key':'H','interval_seconds':20,'hold_seconds':.1},
                {'key':'F1','interval_seconds':30,'hold_seconds':.1},
                {'key':'a','interval_seconds':0,'hold_seconds':.1})
        c=config(timed_keys=values);c.validate()
        keyboard=object.__new__(RealKeyboard);keyboard.controller=Mock();keyboard.special={}
        key_f1=object();bind_timed_special_keys(keyboard,SimpleNamespace(f1=key_f1),values)
        timer=TimedKeys(values);timer.action(0)
        clock=Clock();clock.now=30
        executor=TimedInputExecutor(keyboard,c.vision_config(),timed_keys=values,clock=clock.time,sleep=clock.sleep)
        for key in ('f','h','f1'):
            action=timer.action(30);self.assertEqual(action.direction,key)
            timer.executed(key,executor.execute(action))
        self.assertEqual(keyboard.controller.press.call_args_list,[call('f'),call('h'),call(key_f1)])
        self.assertFalse(executor.held)

    def test_timed_skill_settle_blocks_attack_until_animation_finishes(self):
        brain=GardenCycle(config(timed_keys=slots(),stationary_timeout_seconds=100),start_alt=False)
        brain.decide(observation(),0)
        action=brain.decide(observation(),20);brain.executed(action,20,observation())
        self.assertAlmostEqual(brain.busy_until,20.9)
        self.assertEqual(brain.decide(observation(),20.5).kind,'wait')
