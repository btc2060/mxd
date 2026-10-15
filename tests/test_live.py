"""模拟窗口事件，检查失焦/暂停分支仍处理预览关闭，不调用系统按键。"""
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from lionbot.__main__ import live
from lionbot.brain import Observation
from lionbot.config import Config


class LivePreviewTests(unittest.TestCase):
    def exercise_preview(self, pause=False, close_error=False):
        window = Mock()
        focused = [True]
        window.focused.side_effect = lambda: focused[0]
        frame = np.zeros((480, 640, 3), np.uint8)
        window.capture.return_value = frame
        vision = Mock()
        vision.observe.return_value = Observation((.3, .759), (400, 500))
        vision.draw.return_value = frame
        listener = Mock()
        keyboard_module = ModuleType("pynput.keyboard")
        keyboard_module.Key = SimpleNamespace(f8="f8", f12="f12")
        keyboard_module.Listener = Mock(return_value=listener)

        def display(*args):
            if pause:
                keyboard_module.Listener.call_args.kwargs["on_release"]("f8")
            else:
                focused[0] = False

        waits = [-1, ord("q")]
        config = replace(Config(), world_pixels_per_x=2000, auto_route=False)
        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"pynput": ModuleType("pynput"),
                                                          "pynput.keyboard": keyboard_module}))
            stack.enter_context(patch("lionbot.windows.require_windows"))
            stack.enter_context(patch("lionbot.windows.GameWindow", return_value=window))
            keyboard = stack.enter_context(patch("lionbot.windows.Keyboard"))
            stack.enter_context(patch("lionbot.__main__.Vision", return_value=vision))
            stack.enter_context(patch("lionbot.__main__.time.sleep"))
            stack.enter_context(patch("builtins.print"))
            for name in ("namedWindow", "resizeWindow", "destroyAllWindows"):
                stack.enter_context(patch("lionbot.__main__.cv2." + name))
            stack.enter_context(patch("lionbot.__main__.cv2.imshow", side_effect=display))
            wait = stack.enter_context(patch("lionbot.__main__.cv2.waitKey", side_effect=waits))
            import cv2

            properties = [1, cv2.error("window closed")] if close_error else [1, 1]
            stack.enter_context(patch("lionbot.__main__.cv2.getWindowProperty", side_effect=properties))
            live(config, Path("."), "bottom", run=False, show=True)
        self.assertEqual(wait.call_count, 2)
        window.capture.assert_called_once()
        window.close.assert_called_once()
        listener.stop.assert_called_once()
        vision.reset.assert_called_once()
        keyboard.assert_not_called()

    def test_preview_quits_after_game_loses_focus(self):
        self.exercise_preview()

    def test_preview_quits_while_paused(self):
        self.exercise_preview(pause=True)

    def test_closed_preview_property_error_is_normal_exit(self):
        self.exercise_preview(close_error=True)
