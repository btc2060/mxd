import unittest
from dataclasses import replace

from lionbot.brain import Action, Brain, Mob, Observation
from lionbot.config import Config


def observation(x=.30, y=.759, mobs=(), focused=True):
    return Observation((x, y), (400, 500), tuple(mobs), focused)


def prepared(config=None, mode="auto", obs=None):
    brain = Brain(config or replace(Config(), world_pixels_per_x=2000,
                                     teleport_max_dx=.045, auto_route=False), mode)
    for now in (0, .05, .10):
        brain.decide(obs or observation(), now)
    return brain


class CombatTests(unittest.TestCase):
    def test_face_then_attack_and_actual_press_interval(self):
        brain = prepared()
        obs = observation(mobs=[Mob((550, 500))])
        action = brain.decide(obs, 1)
        self.assertEqual((action.kind, action.direction), ("face", "right"))
        brain.executed(action, 1.02, obs)
        self.assertEqual(brain.decide(obs, 1.10).kind, "wait")
        action = brain.decide(obs, 1.2)
        self.assertEqual(action.kind, "attack")
        brain.executed(action, 1.23, obs)
        self.assertEqual(brain.decide(obs, 2.12).kind, "wait")
        self.assertEqual(brain.decide(obs, 2.131).kind, "attack")

    def test_not_blindly_send_second_attack_after_target_disappears(self):
        brain = prepared()
        brain.facing = "right"
        obs = observation(mobs=[Mob((550, 500))])
        action = brain.decide(obs, 1)
        brain.executed(action, 1, obs)
        self.assertNotEqual(brain.decide(observation(), 2).kind, "attack")

    def test_persistent_target_can_receive_more_than_two_attacks(self):
        brain = prepared()
        brain.facing = "right"
        obs = observation(mobs=[Mob((550, 500))])
        for now in (1, 2, 3):
            action = brain.decide(obs, now)
            self.assertEqual(action.kind, "attack")
            brain.executed(action, now, obs)

    def test_range_is_minimap_distance_not_pixel_fraction(self):
        brain = prepared()
        brain.facing = "right"
        self.assertEqual(brain.decide(observation(mobs=[Mob((572, 500))]), 1).kind, "attack")
        self.assertNotEqual(brain.decide(observation(mobs=[Mob((573, 500))]), 2).kind, "attack")

    def test_different_floor_not_attacked(self):
        brain = prepared()
        brain.facing = "right"
        self.assertNotEqual(brain.decide(observation(mobs=[Mob((450, 450))]), 1).kind, "attack")

    def test_tentative_detection_prevents_false_empty_but_no_attack(self):
        brain = prepared()
        brain.empty_legs = 4
        brain.facing = "right"
        action = brain.decide(observation(mobs=[Mob((450, 500), confirmed=False)]), 10)
        self.assertNotEqual(action.kind, "attack")
        self.assertEqual(brain.empty_legs, 0)
        self.assertEqual(brain.last_mob_at, 10)

    def test_missing_position_or_player_or_focus_releases_input(self):
        for obs in (Observation(None, (400, 500)), Observation((.3, .759), None),
                    observation(focused=False)):
            brain = prepared()
            brain.empty_legs = 4
            action = brain.decide(obs, 10)
            self.assertEqual(action.kind, "wait")
            self.assertEqual(brain.empty_legs, 0)

    def test_alt_first_at_190_and_never_during_cast(self):
        brain = prepared()
        self.assertNotEqual(brain.decide(observation(), 189.9).kind, "alt")
        brain.busy_until = 190.5
        self.assertEqual(brain.decide(observation(), 190).kind, "wait")
        action = brain.decide(observation(), 190.6)
        self.assertEqual(action.kind, "alt")
        brain.executed(action, 190.62, observation())
        self.assertAlmostEqual(brain.next_alt, 380.62)

    def test_middle_floor_is_not_misclassified_as_top(self):
        brain = prepared()
        for now in (1, 2, 3):
            self.assertEqual(brain.decide(observation(y=.4), now).kind, "wait")


class MovementTests(unittest.TestCase):
    def test_far_from_boundaries_uses_teleport(self):
        brain = prepared()
        action = brain.decide(observation(), 1)
        self.assertEqual((action.kind, action.direction), ("teleport", "right"))

    def test_no_uncalibrated_teleport(self):
        brain = prepared(replace(Config(), world_pixels_per_x=2000, auto_route=False))
        self.assertEqual(brain.decide(observation(), 1).kind, "walk")

    def test_no_teleport_near_boundary(self):
        brain = prepared(obs=observation(x=.59))
        self.assertEqual(brain.decide(observation(x=.59), 1).kind, "walk")

    def test_near_target_uses_walking(self):
        brain = prepared()
        # .09 超出射程 .086，但 .09 < 瞬移上限 .07 + 接近余量 .025。
        brain.c = replace(brain.c, teleport_max_dx=.07)
        self.assertEqual(brain.decide(observation(mobs=[Mob((580, 500))]), 1).kind, "walk")

    def test_unexpected_large_teleport_disables_f(self):
        brain = prepared()
        obs = observation()
        action = brain.decide(obs, 1)
        brain.executed(action, 1.04, obs)
        brain.last_scan_x = .4
        brain.decide(observation(x=.4), 1.5)
        self.assertTrue(brain.teleport_fault)
        self.assertNotEqual(brain.decide(observation(x=.4), 2).kind, "teleport")

    def test_stationary_scan_does_not_attack_blindly(self):
        brain = prepared()
        action = brain.decide(observation(x=.36), 1)
        self.assertEqual(action.kind, "scan")

    def test_blocked_teleports_do_not_prevent_stuck_detection(self):
        brain = prepared()
        obs = observation()
        found = False
        for index in range(100):
            now = 1 + index * .2
            action = brain.decide(obs, now)
            brain.executed(action, now, obs)
            if "卡住" in action.reason:
                found = True
                break
        self.assertTrue(found)
        self.assertEqual(brain.direction, "left")

    def test_top_uses_separate_boundaries(self):
        brain = prepared(obs=observation(x=.15, y=.130))
        self.assertEqual(brain.floor, "top")
        self.assertEqual(brain.direction, "left")
        self.assertEqual(brain.decide(observation(x=.3, y=.130), 1).kind, "wait")


class RouteTests(unittest.TestCase):
    def route_brain(self, obs=None):
        return prepared(replace(Config(), world_pixels_per_x=2000,
                                teleport_max_dx=.045), obs=obs)

    def test_short_local_empty_does_not_climb(self):
        brain = self.route_brain()
        for now in (1, 2, 10):
            brain.decide(observation(), now)
        self.assertEqual(brain.route, "")

    def test_two_complete_empty_sweeps_required(self):
        brain = self.route_brain()
        for edge in ("right", "left", "right", "left"):
            brain.reach_edge(edge)
        brain.decide(observation(), 10)
        self.assertEqual(brain.route, "")
        brain.reach_edge("right")
        action = brain.decide(observation(), 11)
        self.assertEqual(brain.route, "align")
        self.assertEqual((action.kind, action.direction), ("walk", "left"))

    def test_whole_climb_exit_then_left_teleport(self):
        brain = self.route_brain()
        brain.empty_legs = 4
        obs = observation(x=.212)
        action = brain.decide(obs, 10)
        self.assertEqual(action.kind, "grab")
        brain.executed(action, 10.04, obs)
        self.assertEqual(brain.decide(observation(x=.212, y=.6), 10.6).kind, "climb")
        action = brain.decide(observation(x=.212, y=.130), 11)
        self.assertEqual(action.kind, "climb_exit")
        brain.executed(action, 11, observation(x=.212, y=.130))
        self.assertEqual(brain.decide(observation(x=.212, y=.130), 11.3).kind, "wait")
        action = brain.decide(observation(x=.212, y=.130), 11.7)
        self.assertEqual((action.kind, action.direction), ("top_teleport", "left"))
        brain.executed(action, 11.74, observation(x=.212, y=.130))
        action = brain.decide(observation(x=.17, y=.130), 12.2)
        self.assertEqual(brain.floor, "top")
        self.assertEqual(brain.route, "")

    def test_no_unconfirmed_climb_success(self):
        brain = self.route_brain()
        brain.route, brain.route_started = "climb", 10
        brain.climb_progress_at, brain.climb_y = 10, .6
        action = brain.decide(observation(x=.212, y=.6), 12)
        self.assertEqual(action.kind, "wait")
        self.assertEqual(brain.floor, "bottom")
        self.assertTrue(brain.route_disabled)

    def test_top_walks_off_without_space_and_confirms_bottom(self):
        brain = self.route_brain(observation(x=.15, y=.130))
        brain.empty_legs = 4
        action = brain.decide(observation(x=.15, y=.130), 10)
        self.assertEqual((action.kind, action.direction), ("walk", "left"))
        action = brain.decide(observation(x=.049, y=.130), 11)
        self.assertEqual(action.kind, "drop_walk")
        brain.executed(action, 11, observation(x=.049, y=.130))
        self.assertEqual(brain.decide(observation(x=.04, y=.4), 12).kind, "wait")
        self.assertEqual(brain.route, "fall")
        for now in (13, 13.1, 13.2):
            brain.decide(observation(x=.04, y=.759), now)
        self.assertEqual(brain.floor, "bottom")
        self.assertEqual(brain.route, "")

    def test_early_fall_during_alignment_is_handled(self):
        brain = self.route_brain(observation(x=.15, y=.130))
        brain.route, brain.route_started = "drop_align", 10
        action = brain.decide(observation(x=.055, y=.25), 11)
        self.assertEqual(action.kind, "wait")
        self.assertEqual(brain.route, "fall")

    def test_timeout_disables_route_without_inventing_success(self):
        brain = self.route_brain()
        brain.route, brain.route_started = "climb", 1
        brain.decide(observation(x=.212, y=.4), 30)
        self.assertTrue(brain.route_disabled)
        self.assertEqual(brain.floor, "bottom")

    def test_explicit_floor_mode_never_auto_switches(self):
        brain = prepared(replace(Config(), world_pixels_per_x=2000,
                                 teleport_max_dx=.045), mode="bottom")
        brain.empty_legs = 4
        brain.decide(observation(), 10)
        self.assertEqual(brain.route, "")

    def test_respawn_on_way_to_ladder_cancels_route_and_attacks(self):
        brain = self.route_brain()
        brain.route, brain.route_started = "align", 10
        brain.facing = "right"
        action = brain.decide(observation(mobs=[Mob((550, 500))]), 11)
        self.assertEqual(brain.route, "")
        self.assertEqual(brain.empty_legs, 0)
        self.assertEqual(action.kind, "attack")

    def test_f_disabled_does_not_prevent_walking_down_from_top(self):
        brain = self.route_brain(observation(x=.15, y=.130))
        brain.teleport_fault = True
        brain.empty_legs = 4
        action = brain.decide(observation(x=.15, y=.130), 10)
        self.assertEqual(brain.route, "drop_align")
        self.assertEqual((action.kind, action.direction), ("walk", "left"))


if __name__ == "__main__":
    unittest.main()
