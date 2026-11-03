import unittest

from lionbot.brain import Mob
from lionbot.config import Config
from lionbot.garden_cycle import GardenCycle
from tests.test_garden import config,observation


class GardenSpeedTests(unittest.TestCase):
    def test_garden_allows_point_six_and_other_entries_keep_existing_limit(self):
        config(attack_interval=.6,teleport_settle=.1).validate(ready=True)
        config(attack_interval=.3,teleport_settle=.05).validate(ready=True)
        with self.assertRaises(ValueError):Config(attack_interval=.6).validate()
        for values in ({'attack_interval':.2},{'attack_interval':.6,'attack_hold':.6},{'teleport_settle':.01}):
            with self.assertRaises(ValueError):config(**values).validate()

    def test_four_shots_keep_point_six_gap_without_face_delay_between_each(self):
        brain=GardenCycle(config(attack_interval=.6,attack_burst_shots=4,face_hold=.05,teleport_enabled=False),start_alt=False)
        obs=observation(mobs=[Mob((390,300))])
        face=brain.decide(obs,0);self.assertEqual(face.kind,'face');brain.executed(face,0,obs)
        for index,now in enumerate((.1,.7,1.3,1.9)):
            if index:self.assertEqual(brain.decide(obs,now-.02).kind,'wait')
            action=brain.decide(obs,now+1e-8)
            self.assertEqual(action.kind,'attack');self.assertEqual(action.direction,'right')
            brain.executed(action,now,obs)
        self.assertEqual(brain.shots,0)

    def test_teleport_settle_controls_attack_readiness_not_next_f_cooldown(self):
        c=config(teleport_settle=.1,teleport_cooldown=1.5,face_hold=.05)
        brain=GardenCycle(c,start_alt=False)
        before=observation(x=.3)
        brain.decide(before,0)
        teleport=brain.walk(.3,0,'test',can_teleport=True)
        self.assertEqual(teleport.kind,'teleport')
        brain.executed(teleport,0,before)
        after=observation(x=.38,mobs=[Mob((390,300))])
        self.assertEqual(brain.decide(after,.09).kind,'wait')
        attack=brain.decide(after,.10001);self.assertEqual(attack.kind,'attack')
        self.assertEqual(brain.next_teleport,1.5)

    def test_short_settle_waits_for_delayed_minimap_without_false_f_failure(self):
        c=config(teleport_settle=.1,face_hold=.05)
        brain=GardenCycle(c,start_alt=False)
        before=observation(x=.3);brain.decide(before,0)
        teleport=brain.walk(.3,0,'test',can_teleport=True);brain.executed(teleport,0,before)
        self.assertEqual(brain.decide(before,.11).kind,'wait')
        self.assertIsNotNone(brain.teleport_origin)
        landed=observation(x=.38,mobs=[Mob((390,300))])
        self.assertEqual(brain.decide(landed,.2).kind,'attack')
        self.assertEqual(brain.teleport_misses,0)
