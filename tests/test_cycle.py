"""用真实 Brain 和 InputExecutor 驱动简化游戏物理，验证挂机循环能前进。

含小地图像素量化、两击死亡、受击漏检、起跳抓绳、持续上下键和自然下落；
只验证控制流程，不声称替代游戏内命中与爬绳测试。
"""
import math
import random
import unittest
from dataclasses import replace

from lionbot.brain import Brain, Mob, Observation
from lionbot.config import Config
from lionbot.controls import InputExecutor


class Game:
    def __init__(self, config, *, fail_grab=False, stall=False, ignore_top_f=False):
        self.c = config
        self.now = 0.
        self.x, self.y = .3, config.bottom.y
        self.held = set()
        self.facing = "left"
        self.motion = "bottom"
        self.jump_at = 0.
        self.grab_sustained = False
        self.fail_grab = fail_grab
        self.stall = stall
        self.stall_recovered = False
        self.ignore_top_f = ignore_top_f
        self.grabs = 0
        self.attacks = []
        self.alt_times = []
        self.targets = []
        self.entrance_spawned = False
        self.new_wave()

    def new_wave(self):
        self.entrance_spawned = False
        self.targets += [[x, floor, 0, -1.] for x, floor in
                         ((.26, "bottom"), (.35, "bottom"), (.51, "bottom"),
                          (.09, "top"), (.15, "top"))]

    def time(self):
        return self.now

    def sleep(self, seconds):
        end = self.now + seconds
        while self.now < end:
            dt = min(.005, end - self.now)
            self.now += dt
            if self.motion in ("bottom", "top"):
                dx = int("right" in self.held) - int("left" in self.held)
                self.x += dx * .1 * dt
                if self.motion == "bottom":
                    self.x = min(self.c.bottom.right, max(self.c.bottom.left, self.x))
                elif self.x < self.c.top_drop_x:
                    self.motion = "fall"
            elif self.motion == "rope":
                if "down" in self.held:
                    self.stall_recovered = True
                    self.y = min(self.c.bottom.y, self.y + .16 * dt)
                    if self.y == self.c.bottom.y:
                        self.motion = "bottom"
                elif "up" in self.held:
                    if not (self.stall and not self.stall_recovered and self.y <= .55):
                        self.y = max(self.c.top.y, self.y - .16 * dt)
            elif self.motion in ("grab_air", "jump_bottom", "jump_top"):
                elapsed = self.now - self.jump_at
                floor = "top" if self.motion == "jump_top" else "bottom"
                self.y = getattr(self.c, floor).y - .06 * math.sin(math.pi * min(1, elapsed / .7))
                if self.motion == "grab_air" and elapsed >= .2:
                    self.motion = "rope" if self.grab_sustained and "up" in self.held else "jump_bottom"
                elif elapsed >= .7:
                    self.motion = floor
                    self.y = getattr(self.c, floor).y
            elif self.motion == "fall":
                self.y = min(self.c.bottom.y, self.y + .35 * dt)
                if self.y == self.c.bottom.y:
                    self.motion = "bottom"

    def press(self, key):
        if key in ("left", "right"):
            self.facing = key
        elif key == "space":
            self.jump_at = self.now
            if self.motion == "rope" and self.y <= self.c.top.y + .012:
                self.motion = "jump_top"
            elif self.motion == "bottom" and "up" in self.held:
                self.grabs += 1
                if abs(self.x - self.c.ladder_x) > .008:
                    raise AssertionError("角色未对齐就起跳抓绳")
                self.motion = "jump_bottom" if self.fail_grab and self.grabs == 1 else "grab_air"
                self.grab_sustained = True
        elif key == "f" and self.c.teleport_max_dx is not None:
            if not (self.ignore_top_f and self.motion == "top"):
                # 配置是测量后的总位移上限，留有按方向键步行和采样余量。
                self.x += .7 * self.c.teleport_max_dx * (1 if self.facing == "right" else -1)
        elif key == "d":
            if self.held & {"up", "down", "left", "right"}:
                raise AssertionError("攻击时仍按着移动键")
            if self.motion not in ("bottom", "top"):
                raise AssertionError("爬绳或空中插入了攻击")
            self.attacks.append(self.now)
            for target in self.targets:
                x, floor, hits, _ = target
                # 蓝独角狮有身体宽度，贴身重叠时不能建模成零宽度单点。
                ahead = x - self.x <= .012 if self.facing == "left" else x - self.x >= -.012
                if floor == self.motion and hits < 2 and ahead and abs(x - self.x) <= self.c.attack_range:
                    target[2], target[3] = hits + 1, self.now
        elif key == "alt":
            if self.motion not in ("bottom", "top"):
                raise AssertionError("爬绳或空中插入了 Alt")
            self.alt_times.append(self.now)
        self.held.add(key)

    def release(self, key):
        if key == "up" and self.motion == "grab_air":
            self.grab_sustained = False
        self.held.discard(key)

    def observe(self, brain):
        if brain.route == "ladder_clear" and not self.entrance_spawned:
            self.entrance_spawned = True
            self.targets += [[self.c.ladder_x + dx, "bottom", 0, -1.] for dx in (-.035, .035)]
        mobs = []
        if brain.needs_monsters:
            for x, floor, hits, hurt_at in self.targets:
                if hits >= 2 or self.now - hurt_at < .45:
                    continue
                screen_x = 400 + (x - self.x) * self.c.world_pixels_per_x
                screen_y = 500 + (getattr(self.c, floor).y - self.y) * 1000
                if 0 <= screen_x <= 800:
                    mobs.append(Mob((screen_x, screen_y)))
        pos = (round(self.x * 137) / 137, round(self.y * 103) / 103)
        return Observation(pos, (400, 500), tuple(mobs), location_source="terrain")


class CycleTests(unittest.TestCase):
    def drive(self, *, cycles=1, measured=True, fail_grab=False, stall=False,
              pause_on_rope=False, ignore_top_f=False):
        config = replace(Config(), minimap_roi=(24, 73, 137, 103), world_pixels_per_x=2000,
                         attack_burst_shots=6, teleport_max_dx=.045 if measured else None)
        config.validate()
        brain = Brain(config, rng=random.Random(1))
        game = Game(config, fail_grab=fail_grab, stall=stall, ignore_top_f=ignore_top_f)
        executor = InputExecutor(game, config, clock=game.time, sleep=game.sleep)
        history = []
        completed, paused = 0, False
        try:
            while game.now < 400:
                start = game.now
                obs = game.observe(brain)
                if pause_on_rope and not paused and brain.route == "climb" and game.y < .5:
                    action = brain.decide(replace(obs, focused=False), game.now)
                    executor.execute(action)
                    self.assertFalse(game.held)
                    game.sleep(4)
                    paused = True
                    continue
                before = brain.route
                action = brain.decide(obs, game.now)
                history.append((round(game.now, 2), brain.route, action.kind, action.direction,
                                tuple(round(v, 4) for v in obs.position), brain.completed_round_trips))
                if action.kind == "grab":
                    self.assertEqual(brain.completed_round_trips, 3)
                    self.assertTrue(all(hits >= 2 for _, floor, hits, _ in game.targets if floor == "bottom"))
                pressed = executor.execute(action)
                brain.executed(action, pressed, obs)
                game.sleep(max(.001, 1 / 15 - (game.now - start)))
                executor.guard_once()
                if before == "fall" and not brain.route and brain.floor == "bottom":
                    completed += 1
                    self.assertTrue(all(hits >= 2 for _, _, hits, _ in game.targets))
                    self.assertEqual(brain.completed_round_trips, 0)
                    if completed == cycles:
                        break
                    game.new_wave()
                if brain.route_disabled:
                    self.fail(f"换层意外停止：{action.reason}\n{history[-20:]}")
            else:
                self.fail(f"挂机循环未完成：{history[-20:]}")
        finally:
            executor.stop()
        self.assertFalse(game.held)
        self.assertTrue(game.attacks)
        self.assertTrue(all(b - a >= .9 - 1e-8 for a, b in zip(game.attacks, game.attacks[1:])))
        self.assertTrue(all(b - a >= 190 - 1e-8 for a, b in zip(game.alt_times, game.alt_times[1:])))
        return game, history

    def test_two_complete_cycles_with_six_shots_hurt_sprites_and_entrance_respawns(self):
        game, history = self.drive(cycles=2)
        self.assertEqual(game.grabs, 2)
        self.assertEqual(sum(item[2] == "top_teleport" for item in history), 2)
        self.assertNotIn("scan", {item[2] for item in history})

    def test_failed_first_grab_retries_and_completes_without_teleport_calibration(self):
        game, history = self.drive(measured=False, fail_grab=True)
        self.assertEqual(game.grabs, 2)
        self.assertIn("grab_retry", {item[1] for item in history})
        self.assertFalse(any(item[2] in ("teleport", "top_teleport") for item in history))

    def test_climb_stall_descends_then_retries_to_complete_cycle(self):
        game, history = self.drive(stall=True)
        self.assertEqual(game.grabs, 2)
        self.assertIn("descend", {item[1] for item in history})

    def test_pause_on_rope_rechecks_position_and_finishes_without_extra_jump(self):
        game, _ = self.drive(pause_on_rope=True)
        self.assertEqual(game.grabs, 1)

    def test_ignored_top_teleport_walks_away_from_rope_and_finishes(self):
        game, history = self.drive(ignore_top_f=True)
        self.assertEqual(game.grabs, 1)
        self.assertEqual(sum(item[2] == "top_teleport" for item in history), 1)
