import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from lionbot.brain import (Action, Brain, Mob, Observation, attack_vertical_bounds,
                           monster_vertical_bounds, target_status, within_attack_height)
from lionbot.config import Config, Floor, load_config


def observation(x=.30, y=.759, mobs=(), focused=True, feet=(400, 500)):
    return Observation((x, y), feet, tuple(mobs), focused)


def prepared(config=None, mode="auto", obs=None, direction="right"):
    brain = Brain(config or replace(Config(), world_pixels_per_x=2000,
                                     teleport_max_dx=.045, auto_route=False), mode, start_alt=False)
    for now in (0, .05, .10):
        brain.decide(obs or observation(), now)
    brain.next_random = float("inf")
    if direction is not None:
        brain.direction = direction
    return brain


def confirm_empty(brain, obs, start):
    actions = [brain.decide(obs, start + offset) for offset in (0, .3, .6, .9)]
    return actions[-1]


class CombatTests(unittest.TestCase):
    def test_face_forward_then_three_attacks_use_actual_press_interval(self):
        brain = prepared()
        obs = observation(mobs=[Mob((550, 500))])
        face = brain.decide(obs, 1)
        self.assertEqual((face.kind, face.direction), ("face", "right"))
        brain.executed(face, 1.02, obs)
        self.assertEqual(brain.decide(obs, 1.10).kind, "wait")
        for press in (1.23, 2.131, 3.032):
            action = brain.decide(obs, press)
            self.assertEqual(action.kind, "attack")
            brain.executed(action, press, obs)
            self.assertEqual(brain.decide(obs, press + .899).kind, "wait")
        self.assertEqual(brain.decide(obs, 3.933).kind, "attack")

    def test_hurt_or_missing_nameplate_does_not_interrupt_three_shot_burst(self):
        brain = prepared()
        brain.facing = "right"
        first = observation(mobs=[Mob((550, 500))])
        brain.executed(brain.decide(first, 1), 1, first)
        lost_visual = observation(feet=None)
        for now in (1.91, 2.82):
            action = brain.decide(lost_visual, now)
            self.assertEqual((action.kind, action.direction), ("attack", "right"))
            brain.executed(action, now, lost_visual)
        self.assertEqual(brain.decide(lost_visual, 3.73).kind, "wait")
        self.assertIsNone(brain.last_clear_at)
        brain.decide(lost_visual, 5)
        self.assertEqual(brain.decide(lost_visual, 5.1).kind, "walk")
        self.assertIsNone(brain.last_clear_at)  # 无定位恢复巡逻，不伪造清场成功。

    def test_persistent_monster_keeps_attacking_without_forced_walking(self):
        brain = prepared()
        brain.facing = "right"
        obs = observation(mobs=[Mob((550, 500))])
        for now in (1, 2, 3, 4, 5):
            action = brain.decide(obs, now)
            self.assertEqual(action.kind, "attack")
            brain.executed(action, now, obs)
        self.assertTrue(brain.clearing)
        self.assertEqual(brain.direction, "right")

    def test_rear_monsters_are_cleared_without_changing_patrol_direction(self):
        for direction, behind in (("right", 250), ("left", 550)):
            brain = prepared(direction=direction)
            brain.facing = direction
            obs = observation(mobs=[Mob((behind, 500))])
            self.assertEqual(target_status(obs.mobs[0], obs, brain.c, direction), "ready")
            action = brain.decide(obs, 1)
            other = "left" if direction == "right" else "right"
            self.assertEqual((action.kind, action.direction), ("face", other))
            brain.executed(action, 1, obs)
            self.assertEqual(brain.decide(obs, 1.13).kind, "attack")
            self.assertEqual(brain.direction, direction)

    def test_two_populated_sides_alternate_bursts_without_starving_rear_targets(self):
        brain = prepared()
        brain.facing = "right"
        obs = observation(mobs=[Mob((510, 500))])
        brain.executed(brain.decide(obs, 1), 1, obs)
        both = observation(mobs=[Mob((250, 500)), Mob((510, 500))])
        for now in (2, 3):
            action = brain.decide(both, now)
            self.assertEqual((action.kind, action.direction), ("attack", "right"))
            brain.executed(action, now, both)
        action = brain.decide(both, 4)
        self.assertEqual((action.kind, action.direction), ("face", "left"))
        brain.executed(action, 4, both)
        self.assertEqual((brain.decide(both, 4.13).kind, brain.facing), ("attack", "left"))
        self.assertEqual(brain.direction, "right")

    def test_resume_original_route_only_after_multiple_empty_frames_and_time(self):
        brain = prepared()
        brain.facing = "right"
        brain.completed_round_trips = 2
        brain.lap_started = brain.right_reached = True
        obs = observation(mobs=[Mob((550, 500))])
        for now in (1, 2, 3):
            brain.executed(brain.decide(obs, now), now, obs)
        for now in (4, 4.3, 4.6):
            self.assertEqual(brain.decide(observation(), now).kind, "wait")
        action = brain.decide(observation(), 4.9)
        self.assertEqual((action.kind, action.direction), ("walk", "right"))
        self.assertFalse(brain.clearing)
        self.assertEqual(brain.completed_round_trips, 2)
        self.assertTrue(brain.lap_started and brain.right_reached)

    def test_reappearing_candidate_resets_empty_evidence(self):
        brain = prepared()
        pending = observation(mobs=[Mob((450, 500), confirmed=False)])
        brain.decide(pending, 1)
        brain.decide(observation(), 1.1)
        brain.decide(observation(), 1.4)
        self.assertEqual(brain.decide(pending, 1.7).kind, "wait")
        self.assertEqual(brain.clear_count, 0)
        for now in (1.8, 2.1, 2.4):
            self.assertEqual(brain.decide(observation(), now).kind, "wait")
        self.assertEqual(brain.decide(observation(), 2.7).kind, "walk")

    def test_unreachable_high_or_distant_monsters_do_not_block_empty_confirmation(self):
        brain = prepared()
        brain.decide(observation(mobs=[Mob((450, 500), confirmed=False)]), 1)
        unreachable = observation(mobs=[Mob((450, 400)), Mob((650, 500))])
        self.assertEqual(confirm_empty(brain, unreachable, 2).kind, "walk")

    def test_height_and_range_still_gate_first_detected_attack(self):
        for mob in (Mob((450, 435)), Mob((450, 523)), Mob((573, 500))):
            with self.subTest(mob=mob):
                brain = prepared()
                brain.facing = "right"
                self.assertNotEqual(brain.decide(observation(mobs=[mob]), 1).kind, "attack")
        brain = prepared()
        brain.facing = "right"
        self.assertEqual(brain.decide(observation(mobs=[Mob((572, 500))]), 1).kind, "attack")

    def test_height_uses_main_screen_feet_not_minimap_y_or_head_box(self):
        config = replace(Config(), bottom=Floor(.8107, .03, .97),
                         world_pixels_per_x=2000, auto_route=False)
        high = Mob((700, 467))
        obs = observation(x=.4, y=.8107, feet=(655, 587), mobs=[high])
        brain = prepared(config, obs=obs)
        brain.facing = "right"
        self.assertEqual(target_status(high, obs, config, "right"), "height")
        self.assertNotEqual(brain.decide(obs, 1).kind, "attack")
        obs = replace(obs, mobs=(high, Mob((745, 580))))
        self.assertEqual(brain.decide(obs, 2).kind, "attack")

    def test_step_monsters_in_player_body_band_are_attackable_with_head_only_boxes(self):
        config = replace(Config(), world_pixels_per_x=4000, auto_route=False)
        # 对照用户截图：角色脚底、较低台阶怪脚底、仅头部的识别框。
        scenes = [((145, 164), (423, 105), (401, 52, 41, 28)),
                  ((85, 148), (197, 92), (175, 38, 42, 30)),
                  ((313, 137), (413, 79), (392, 21, 42, 29)),
                  ((315, 111), (71, 109), (50, 57, 42, 30))]
        for player, feet, box in scenes:
            with self.subTest(player=player, feet=feet):
                mob = Mob(feet, box=box)
                obs = observation(feet=player, mobs=[mob])
                direction = "right" if feet[0] >= player[0] else "left"
                self.assertTrue(within_attack_height(player, feet, config, box))
                self.assertEqual(target_status(mob, obs, config, direction), "ready")
                brain = prepared(config, obs=obs, direction=direction)
                brain.facing = direction
                self.assertEqual(brain.decide(obs, 1).kind, "attack")

    def test_vertical_overlap_uses_body_span_instead_of_head_center_or_feet_alone(self):
        c = replace(Config(), world_pixels_per_x=2000)
        obs = observation()
        self.assertEqual(attack_vertical_bounds(obs.player_feet, c), (436, 500))
        # 头框在攻击带上方，但实际脚底落在角色头顶处，可以命中。
        head_above = Mob((450, 440), box=(430, 395, 40, 25))
        self.assertEqual(monster_vertical_bounds(head_above.feet, head_above.box), (395, 440))
        self.assertEqual(target_status(head_above, obs, c, "right"), "ready")
        # 怪物脚底稍低，但其身体与攻击带相交，同样通过。
        lower_body = Mob((450, 520), box=(430, 485, 40, 25))
        self.assertEqual(target_status(lower_body, obs, c, "right"), "ready")
        for mob in (Mob((450, 435), box=(430, 390, 40, 25)),
                    Mob((450, 550), box=(430, 501, 40, 25))):
            self.assertEqual(target_status(mob, obs, c, "right"), "height")
        self.assertEqual(target_status(head_above, obs, c, "left"), "ready")

    def test_measured_player_height_controls_vertical_bounds_not_legacy_tolerance(self):
        c = replace(Config(), player_height_px=50, feet_tolerance_px=999)
        player = (400, 500)
        for y, expected in ((449, False), (450, True), (500, True), (501, False)):
            self.assertEqual(within_attack_height(player, (450, y), c), expected)
        self.assertFalse(within_attack_height(None, (450, 500), c))

    def test_tentative_front_detection_pauses_for_confirmation(self):
        brain = prepared()
        obs = observation(mobs=[Mob((450, 500), confirmed=False)])
        self.assertEqual(brain.decide(obs, 1).kind, "wait")
        self.assertEqual(brain.decide(obs, 1.31).kind, "wait")
        brain.facing = "right"
        self.assertEqual(brain.decide(observation(mobs=[Mob((450, 500))]), 1.4).kind, "attack")

    def test_recovered_detection_cannot_start_new_burst(self):
        brain = prepared()
        obs = observation(mobs=[Mob((550, 500), recovered=True)])
        self.assertEqual(brain.decide(obs, 1).kind, "wait")
        self.assertIsNone(brain.burst)

    def test_focus_or_yellow_loss_clears_burst_but_nameplate_loss_does_not(self):
        for lost in (Observation(None, (400, 500)), observation(focused=False)):
            brain = prepared()
            brain.facing = "right"
            obs = observation(mobs=[Mob((550, 500))])
            brain.executed(brain.decide(obs, 1), 1, obs)
            self.assertEqual(brain.decide(lost, 1.4).kind, "wait")
            self.assertIsNone(brain.burst)
        brain = prepared()
        self.assertEqual(brain.decide(observation(feet=None), 1).kind, "walk")

    def test_large_displacement_cancels_burst(self):
        brain = prepared()
        brain.facing = "right"
        obs = observation(mobs=[Mob((550, 500))])
        brain.executed(brain.decide(obs, 1), 1, obs)
        self.assertNotEqual(brain.decide(observation(x=.4), 2).kind, "attack")

    def test_random_attacks_require_walking_and_are_bounded_forward(self):
        brain = prepared()
        brain.next_random = 2
        brain.facing = "right"
        obs = observation(feet=None)
        self.assertEqual(brain.decide(obs, 3).kind, "walk")
        for x in (.33, .36):
            brain.executed(Action("walk", "right"), 3, obs)
            obs = observation(x=x, feet=None)
            brain.decide(obs, 3.1)
        # 决策可建议随机攻击；未执行的建议不增加次数。
        action = brain.decide(obs, 3.2)
        self.assertEqual((action.kind, action.direction), ("attack", "right"))
        count = brain.burst.limit
        self.assertIn(count, (1, 2))
        for index in range(count):
            now = 3.2 + index
            action = brain.decide(obs, now)
            self.assertEqual(action.kind, "attack")
            brain.executed(action, now, obs)
        self.assertNotEqual(brain.decide(obs, 3.2 + count).kind, "attack")
        self.assertGreater(brain.next_random, 3.2 + count)

    def test_alt_starts_once_then_every_190_seconds_even_without_nameplate(self):
        brain = Brain(replace(Config(), world_pixels_per_x=2000, auto_route=False))
        obs = observation(feet=None)
        for now in (0, .05):
            self.assertEqual(brain.decide(obs, now).kind, "wait")
        first = brain.decide(obs, .1)
        self.assertEqual(first.kind, "alt")
        brain.executed(first, .12, obs)
        brain.reset_observation()
        for now in (1, 1.1, 1.2, 190.119):
            self.assertNotEqual(brain.decide(obs, now).kind, "alt")
        brain.busy_until = 190.5
        self.assertEqual(brain.decide(obs, 190.2).kind, "wait")
        action = brain.decide(obs, 190.6)
        self.assertEqual(action.kind, "alt")
        brain.executed(action, 190.62, obs)
        self.assertAlmostEqual(brain.next_alt, 380.62)


class PatrolTests(unittest.TestCase):
    def test_start_in_middle_seeks_left_inset_before_first_round_trip(self):
        brain = prepared(direction=None)
        self.assertEqual(brain.decide(observation(feet=None), 1).direction, "left")
        low = brain.c.bottom.left + brain.c.turn_inset
        self.assertGreater(low, brain.c.bottom.left)
        brain.decide(observation(x=low), 2)
        self.assertEqual(brain.direction, "right")
        self.assertEqual(brain.completed_round_trips, 0)

    def test_three_complete_round_trips_then_go_to_ladder(self):
        config = replace(Config(), world_pixels_per_x=2000, teleport_max_dx=.045)
        brain = prepared(config, direction=None)
        low, high = config.bottom.left + config.turn_inset, config.bottom.right - config.turn_inset
        brain.decide(observation(x=low), 1)
        for lap in range(3):
            # 怪物不会重置往返计数，观察建议也不能虚增计数。
            brain.decide(observation(x=high), 2 + 2 * lap)
            brain.decide(observation(x=low), 3 + 2 * lap)
            self.assertEqual(brain.completed_round_trips, lap + 1)
            self.assertEqual(brain.route, "align" if lap == 2 else "")
        self.assertEqual(brain.direction, "right")

    def test_partial_leg_and_repeated_edge_frames_do_not_count_as_round(self):
        brain = prepared(direction=None)
        low, high = brain.c.bottom.left + brain.c.turn_inset, brain.c.bottom.right - brain.c.turn_inset
        brain.decide(observation(x=high), 1)
        brain.decide(observation(x=low), 2)
        for now in (3, 4, 5):
            brain.decide(observation(x=low), now)
        self.assertEqual(brain.completed_round_trips, 0)
        brain.decide(observation(x=high), 6)
        brain.decide(observation(x=low), 7)
        self.assertEqual(brain.completed_round_trips, 1)

    def test_pause_preserves_completed_laps_but_invalidates_partial_leg(self):
        brain = prepared()
        brain.completed_round_trips = 2
        brain.lap_started = brain.right_reached = True
        brain.reset_observation()
        self.assertEqual(brain.completed_round_trips, 2)
        self.assertFalse(brain.lap_started)
        self.assertFalse(brain.right_reached)

    def test_explicit_bottom_mode_keeps_patrolling_after_three_rounds(self):
        brain = prepared(replace(Config(), world_pixels_per_x=2000, teleport_max_dx=.045), mode="bottom")
        brain.completed_round_trips = 3
        brain.decide(observation(), 1)
        self.assertEqual(brain.route, "")

    def test_boundary_turn_waits_until_surroundings_are_cleared(self):
        brain = prepared()
        brain.facing = "right"
        x = brain.c.bottom.right - brain.c.turn_inset
        action = brain.decide(observation(x=x, mobs=[Mob((550, 500))]), 1)
        self.assertEqual((action.kind, brain.direction), ("attack", "right"))
        # 同一点把整组攻击完成，清场确认结束后才按原路线掉头。
        obs = observation(x=x, mobs=[Mob((550, 500))])
        for now in (1, 2, 3):
            brain.executed(brain.decide(obs, now), now, obs)
        action = confirm_empty(brain, observation(x=x), 4)
        self.assertEqual(brain.direction, "left")
        self.assertEqual(action.kind, "scan")
        self.assertNotEqual(action.kind, "attack")

    def test_stuck_walk_reverses_without_counting_incomplete_round(self):
        brain = prepared()
        obs = observation()
        for index in range(30):
            now = 1 + index * .1
            action = brain.decide(obs, now)
            brain.executed(action, now, obs)
            if "卡住" in action.reason:
                break
        else:
            self.fail("连续步行未触发卡住检测")
        self.assertEqual(brain.direction, "left")
        self.assertEqual(brain.completed_round_trips, 0)

    def test_middle_or_wrong_floor_does_not_move(self):
        for y in (.4, .130):
            brain = prepared(mode="bottom")
            for now in (1, 2, 3):
                self.assertEqual(brain.decide(observation(y=y, feet=None), now).kind, "wait")

    def test_patrol_teleport_remains_occasional_and_safe_with_legacy_cooldown(self):
        brain = prepared()
        brain.c = replace(brain.c, teleport_cooldown=.65)
        self.assertEqual(brain.decide(observation(), 1).kind, "walk")
        brain.walked_since_teleport = 1
        action = brain.decide(observation(), 13)
        self.assertEqual(action.kind, "teleport")
        brain.executed(action, 13.04, observation())
        self.assertAlmostEqual(brain.next_teleport, 25.04)
        brain.decide(observation(x=.4), 13.5)
        self.assertTrue(brain.teleport_fault)


class RouteTests(unittest.TestCase):
    def route_brain(self, obs=None):
        direction = "left" if obs and obs.position[1] == .130 else "right"
        return prepared(replace(Config(), world_pixels_per_x=2000, teleport_max_dx=.045),
                        obs=obs, direction=direction)

    def ready_grab(self, brain, start=10, reset_route=True):
        if reset_route:
            brain.route = "align"
        obs = observation(x=brain.c.ladder_x)
        for offset in (0, .3, .6, .9, 1.0, 1.1):
            action = brain.decide(obs, start + offset)
        self.assertEqual(action.kind, "grab")
        return action, obs, start + 1.1

    def begin_grab(self, brain, start=10):
        action, obs, now = self.ready_grab(brain, start)
        brain.executed(action, now + .04, obs)
        self.assertEqual(brain.route, "grab_check")
        return now + .04

    def test_ladder_approach_clears_both_sides_and_keeps_route(self):
        brain = self.route_brain()
        brain.route = "align"
        brain.completed_round_trips = 3
        brain.facing = "right"
        obs = observation(x=.1, mobs=[Mob((250, 500)), Mob((550, 500))])
        for now in (30, 31, 32):
            action = brain.decide(obs, now)
            self.assertEqual((action.kind, action.direction), ("attack", "right"))
            brain.executed(action, now, obs)
        face = brain.decide(obs, 33)
        self.assertEqual((face.kind, face.direction), ("face", "left"))
        brain.executed(face, 33, obs)
        for now in (33.2, 34.2, 35.2):
            action = brain.decide(obs, now)
            self.assertEqual((action.kind, action.direction), ("attack", "left"))
            brain.executed(action, now, obs)
        action = confirm_empty(brain, observation(x=.1), 36.2)
        self.assertEqual((action.kind, action.direction), ("walk", "right"))
        self.assertEqual((brain.route, brain.completed_round_trips), ("align", 3))
        self.assertEqual(brain.decide(observation(x=.212), 37.3).kind, "wait")
        self.assertEqual(brain.route, "ladder_clear")

    def test_even_exact_ladder_x_requires_clearing_rear_targets_first(self):
        brain = self.route_brain()
        brain.route = "align"
        brain.facing = "right"
        obs = observation(x=.212, mobs=[Mob((250, 500))])
        face = brain.decide(obs, 1)
        self.assertEqual((face.kind, face.direction), ("face", "left"))
        self.assertEqual(brain.route, "ladder_clear")
        brain.executed(face, 1, obs)
        for now in (1.13, 2.13, 3.13):
            action = brain.decide(obs, now)
            self.assertEqual(action.kind, "attack")
            brain.executed(action, now, obs)
        self.assertEqual(brain.decide(obs, 4.14).kind, "attack")
        self.assertEqual(brain.grab_attempts, 0)

    def test_clear_then_short_nudges_and_three_stable_aligned_frames(self):
        brain = self.route_brain()
        brain.route = "align"
        obs = observation(x=.212 - .015)
        action = confirm_empty(brain, obs, 10)
        self.assertEqual((action.kind, action.direction), ("nudge", "right"))
        brain.executed(action, 10.91, obs)
        obs = observation(x=.212 + .012)
        action = brain.decide(obs, 11.1)
        self.assertEqual((action.kind, action.direction), ("nudge", "left"))
        brain.executed(action, 11.1, obs)
        for now, x in ((11.3, .216), (11.4, .208)):
            self.assertEqual(brain.decide(observation(x=x), now).kind, "wait")
        self.assertEqual(brain.decide(observation(x=.214), 11.5).kind, "grab")

    def test_respawn_during_final_alignment_returns_to_clearing(self):
        brain = self.route_brain()
        brain.route = "align"
        confirm_empty(brain, observation(x=.212), 10)
        self.assertEqual(brain.route, "ladder_align")
        action = brain.decide(observation(x=.212, mobs=[Mob((250, 500))]), 11)
        self.assertIn(action.kind, ("face", "attack"))
        self.assertEqual((brain.route, brain.ladder_aligned_frames), ("ladder_clear", 0))
        self.assertEqual(brain.grab_attempts, 0)

    def test_old_wide_tolerance_is_tightened_to_near_minimap_pixel_center(self):
        config = replace(Config(), world_pixels_per_x=2000, teleport_max_dx=.045,
                         minimap_roi=(24, 73, 137, 103), ladder_tolerance=.012)
        brain = prepared(config)
        brain.route = "align"
        obs = observation(x=config.ladder_x - .009)
        self.assertLess(abs(obs.position[0] - config.ladder_x), config.ladder_tolerance)
        action = confirm_empty(brain, obs, 10)
        self.assertEqual((action.kind, action.direction), ("nudge", "right"))
        self.assertLess(brain.ladder_alignment_tolerance(), 1 / 137)
        brain.executed(action, 10.91, obs)
        for now in (11.1, 11.2):
            self.assertEqual(brain.decide(observation(x=config.ladder_x - .0005), now).kind, "wait")
        self.assertEqual(brain.decide(observation(x=config.ladder_x - .0005), 11.3).kind, "grab")

    def test_missing_screen_position_cannot_fake_clearance_at_ladder(self):
        brain = self.route_brain()
        brain.route = "align"
        for now in (10, 10.5, 11, 15):
            action = brain.decide(observation(x=.212, feet=None), now)
            self.assertEqual(action.kind, "wait")
        self.assertEqual(brain.route, "ladder_clear")
        self.assertEqual(brain.grab_attempts, 0)
        self.assertIsNone(brain.last_clear_at)

    def test_small_jump_inside_broad_floor_tolerance_is_not_ground_for_nudging(self):
        brain = self.route_brain()
        brain.route, brain.ladder_align_started = "ladder_align", 10
        for now in (10, 10.1, 10.2):
            action = brain.decide(observation(x=.19, y=.73), now)
            self.assertEqual(action.kind, "wait")
        self.assertEqual(brain.ladder_aligned_frames, 0)

    def test_whole_confirmed_climb_top_stability_exit_and_left_teleport(self):
        brain = self.route_brain()
        pressed = self.begin_grab(brain)
        # 抓绳后无需名牌；必须有延迟后的多个上升观测，单次跳跃不够。
        for elapsed, y in ((.26, .73), (.66, .70), (.86, .67)):
            self.assertEqual(brain.decide(observation(x=.212, y=y, feet=None), pressed + elapsed).kind, "climb")
        self.assertEqual(brain.route, "climb")
        for now in (13, 13.1):
            self.assertEqual(brain.decide(observation(x=.212, y=.130, feet=None), now).kind, "climb")
        action = brain.decide(observation(x=.212, y=.130, feet=None), 13.2)
        self.assertEqual(action.kind, "climb_exit")
        brain.executed(action, 13.22, observation(x=.212, y=.130, feet=None))
        self.assertEqual(brain.decide(observation(x=.212, y=.130, feet=None), 13.5).kind, "wait")
        action = brain.decide(observation(x=.212, y=.130, feet=None), 13.9)
        self.assertEqual((action.kind, action.direction), ("top_teleport", "left"))
        brain.executed(action, 13.94, observation(x=.212, y=.130, feet=None))
        brain.decide(observation(x=.17, y=.130, feet=None), 14.4)
        self.assertEqual((brain.floor, brain.route, brain.direction), ("top", "", "left"))

    def test_jump_only_is_rejected_then_reclear_realign_and_retry_can_succeed(self):
        brain = self.route_brain()
        pressed = self.begin_grab(brain)
        for elapsed, y in ((.26, .70), (.56, .68), (.86, .74), (1.06, .759), (1.26, .759)):
            brain.decide(observation(x=.212, y=y), pressed + elapsed)
            self.assertEqual(brain.route, "grab_check")
        action = brain.decide(observation(x=.212), pressed + 2.21)
        self.assertEqual((action.kind, brain.route), ("wait", "grab_retry"))
        self.assertEqual(brain.grab_attempts, 1)
        action, obs, now = self.ready_grab(brain, pressed + 2.4, reset_route=False)
        brain.executed(action, now + .04, obs)
        self.assertEqual(brain.grab_attempts, 2)
        for elapsed, y in ((.3, .73), (.7, .70), (.9, .67)):
            brain.decide(observation(x=.212, y=y), now + .04 + elapsed)
        self.assertEqual(brain.route, "climb")

    def test_failed_grabs_have_bounded_attempts_not_assumed_success(self):
        brain = self.route_brain()
        start = 10
        for attempt in range(1, 4):
            action, obs, now = self.ready_grab(brain, start, reset_route=attempt == 1)
            pressed = now + .04
            brain.executed(action, pressed, obs)
            for elapsed in (.3, .7, 1.0):
                brain.decide(obs, pressed + elapsed)
                self.assertEqual(brain.route, "grab_check")
            action = brain.decide(obs, pressed + 2.21)
            self.assertEqual(action.kind, "wait")
            self.assertEqual(brain.grab_attempts, attempt)
            if attempt < 3:
                self.assertEqual(brain.route, "grab_retry")
                self.assertFalse(brain.route_disabled)
            start = pressed + 2.4
        self.assertTrue(brain.route_disabled)
        self.assertEqual(brain.route, "")

    def test_lateral_drift_retries_without_nudging_in_air(self):
        brain = self.route_brain()
        pressed = self.begin_grab(brain)
        action = brain.decide(observation(x=.25, y=.70), pressed + .3)
        self.assertEqual((action.kind, brain.route), ("wait", "grab_retry"))
        self.assertEqual(brain.decide(observation(x=.25, y=.70), pressed + .6).kind, "wait")

    def test_repeated_micro_alignment_timeouts_are_bounded(self):
        brain = self.route_brain()
        brain.route = "align"
        obs = observation(x=.19)
        now = 10
        for attempt in range(1, 4):
            action = confirm_empty(brain, obs, now)
            self.assertEqual(action.kind, "nudge")
            now += .9 + brain.c.ladder_align_timeout + .01
            action = brain.decide(obs, now)
            self.assertEqual(action.kind, "wait")
            self.assertEqual(brain.ladder_align_failures, attempt)
            now += .1
        self.assertTrue(brain.route_disabled)
        self.assertEqual(brain.grab_attempts, 0)

    def test_shallow_confirmed_climb_is_not_mistaken_for_returning_to_ground(self):
        brain = self.route_brain()
        pressed = self.begin_grab(brain)
        for elapsed, y in ((.3, .75), (.7, .74), (.9, .73), (1.0, .727)):
            action = brain.decide(observation(x=.212, y=y), pressed + elapsed)
            self.assertEqual(action.kind, "climb")
        self.assertEqual(brain.route, "climb")

    def test_top_clears_both_sides_then_walks_off_and_resets_rounds(self):
        brain = self.route_brain(observation(x=.15, y=.130))
        brain.facing = "left"
        both = observation(x=.15, y=.130, mobs=[Mob((250, 500)), Mob((550, 500))])
        for now in (1, 2, 3):
            action = brain.decide(both, now)
            self.assertEqual((action.kind, action.direction), ("attack", "left"))
            brain.executed(action, now, both)
        action = brain.decide(both, 4)
        self.assertEqual((action.kind, action.direction), ("face", "right"))
        brain.executed(action, 4, both)
        for now in (4.2, 5.2, 6.2):
            action = brain.decide(both, now)
            self.assertEqual((action.kind, action.direction), ("attack", "right"))
            brain.executed(action, now, both)
        action = confirm_empty(brain, observation(x=.15, y=.130), 7.2)
        self.assertEqual((action.kind, action.direction), ("walk", "left"))
        brain.completed_round_trips = 3
        action = brain.decide(observation(x=.049, y=.130, feet=None), 8.3)
        self.assertEqual(action.kind, "drop_walk")
        brain.executed(action, 8.3, observation(x=.049, y=.130, feet=None))
        self.assertEqual(brain.decide(observation(x=.04, y=.4, feet=None), 9).kind, "wait")
        for now in (10, 10.1, 10.2):
            brain.decide(observation(x=.04, feet=None), now)
        self.assertEqual((brain.floor, brain.route, brain.completed_round_trips), ("bottom", "", 0))

    def test_climb_returning_to_ground_retries_instead_of_declaring_top(self):
        brain = self.route_brain()
        brain.route, brain.route_started = "climb", 10
        brain.grab_attempts, brain.grab_origin_y = 1, .759
        brain.climb_y, brain.climb_progress_at = .6, 10
        action = brain.decide(observation(x=.212), 11)
        self.assertEqual((action.kind, brain.route), ("wait", "grab_retry"))

    def test_ladder_stuck_or_timeout_does_not_invent_top_arrival(self):
        for now in (12, 30):
            brain = self.route_brain()
            brain.route, brain.route_started = "climb", 10
            brain.climb_progress_at, brain.climb_y = 10, .6
            action = brain.decide(observation(x=.212, y=.6, feet=None), now)
            self.assertEqual(action.kind, "wait")
            self.assertTrue(brain.route_disabled)
            self.assertEqual(brain.floor, "bottom")

    def test_early_fall_during_top_alignment_is_handled(self):
        brain = self.route_brain(observation(x=.15, y=.130))
        brain.route = "drop_align"
        brain.decide(observation(x=.055, y=.25, feet=None), 11)
        self.assertEqual(brain.route, "fall")


class ConfigTests(unittest.TestCase):
    def test_old_two_shot_json_upgrades_without_changing_templates_or_map(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"attack_burst_shots": 2, "player_template": "keep.png",
                                        "bottom": {"y": .8107, "left": .0182, "right": .9745}}))
            config = load_config(path)
            self.assertEqual(config.attack_burst_shots, 3)
            self.assertEqual(config.patrol_round_trips, 3)
            self.assertEqual(config.player_template, "keep.png")
            self.assertEqual(config.bottom.y, .8107)

    def test_runtime_no_longer_requires_nameplate_offset(self):
        config = replace(Config(), minimap_roi=(0, 0, 100, 100), combat_roi=(0, 100, 640, 300),
                         frame_size=(640, 480), world_pixels_per_x=2000)
        config.require_calibration()
