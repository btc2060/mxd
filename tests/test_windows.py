"""模拟 Win32 枚举，防止终端、启动器和附属窗口被算作第二个游戏。"""
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lionbot.windows import GameWindow, select_game_window


TITLE = "冒险岛怀旧服"


def item(hwnd, title=TITLE, class_name="MapleStoryClass", size=(1366, 768), owner=0, visible=True):
    return SimpleNamespace(hwnd=hwnd, title=title, class_name=class_name, size=size,
                           owner=owner, visible=visible)


class FakeGui:
    def __init__(self, *windows):
        self.windows = {window.hwnd: window for window in windows}
        self.enumerations = 0

    def EnumWindows(self, callback, data):
        self.enumerations += 1
        for hwnd in list(self.windows):
            callback(hwnd, data)

    def IsWindowVisible(self, hwnd):
        return self.windows[hwnd].visible

    def IsWindow(self, hwnd):
        return hwnd in self.windows

    def GetWindowText(self, hwnd):
        return self.windows[hwnd].title

    def GetClassName(self, hwnd):
        return self.windows[hwnd].class_name

    def GetClientRect(self, hwnd):
        return (0, 0, *self.windows[hwnd].size)

    def GetWindow(self, hwnd, command):
        assert command == 4
        return self.windows[hwnd].owner if hwnd in self.windows else 0


class WindowSelectionTests(unittest.TestCase):
    def test_console_with_game_in_title_is_not_a_second_game(self):
        for terminal in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
            gui = FakeGui(item(101), item(202, class_name=terminal))
            self.assertEqual(select_game_window(gui, TITLE).hwnd, 101)

    def test_exact_title_excludes_browser_with_title_suffix(self):
        gui = FakeGui(item(101, class_name="CustomGameClass"),
                      item(202, title=TITLE + " - GitHub - Chrome", class_name="Chrome_WidgetWin_1"))
        self.assertEqual(select_game_window(gui, TITLE).hwnd, 101)

    def test_saved_client_size_distinguishes_game_from_same_named_launcher(self):
        gui = FakeGui(item(101, class_name="CustomGameClass"),
                      item(202, class_name="LauncherClass", size=(500, 300)))
        self.assertEqual(select_game_window(gui, TITLE, (1366, 768)).hwnd, 101)

    def test_native_game_class_takes_priority_over_exact_named_browser(self):
        gui = FakeGui(item(101, title=TITLE + " - 频道1"),
                      item(202, class_name="Chrome_WidgetWin_1"))
        self.assertEqual(select_game_window(gui, TITLE).hwnd, 101)

    def test_owned_dialogs_do_not_count_as_independent_games_even_with_indirect_owner(self):
        gui = FakeGui(item(101, class_name="CustomGameClass"),
                      item(202, class_name="#32770", owner=101),
                      item(303, title="Intermediate", class_name="Helper", owner=101),
                      item(404, class_name="Helper", owner=303))
        self.assertEqual(select_game_window(gui, TITLE).hwnd, 101)

    def test_game_owned_by_unmatched_launcher_is_not_discarded(self):
        gui = FakeGui(item(101, owner=202), item(202, title="Launcher", class_name="Helper"))
        self.assertEqual(select_game_window(gui, TITLE).hwnd, 101)

    def test_native_game_or_saved_size_beats_same_named_launcher_owner(self):
        for game_class in ("MapleStoryClass", "CustomGameClass"):
            gui = FakeGui(item(101, owner=202, class_name=game_class),
                          item(202, class_name="Launcher", size=(500, 300)))
            self.assertEqual(select_game_window(gui, TITLE, (1366, 768)).hwnd, 101)

    def test_hidden_and_zero_size_helpers_are_excluded(self):
        gui = FakeGui(item(101), item(202, visible=False), item(303, size=(0, 0)))
        self.assertEqual(select_game_window(gui, TITLE).hwnd, 101)

    def test_two_real_games_stay_ambiguous_and_error_lists_both_handles_and_classes(self):
        gui = FakeGui(item(101), item(202))
        with self.assertRaises(RuntimeError) as error:
            select_game_window(gui, TITLE, (1366, 768))
        message = str(error.exception)
        for detail in ("0x65", "0xCA", TITLE, "MapleStoryClass", "1366x768", "剩余 2 个候选"):
            self.assertIn(detail, message)

    def test_empty_keyword_and_no_match_have_clear_errors(self):
        gui = FakeGui(item(101))
        with self.assertRaisesRegex(ValueError, "window_title 不能为空"):
            select_game_window(gui, "")
        with self.assertRaisesRegex(RuntimeError, "没有标题匹配"):
            select_game_window(gui, "Different game")

    def test_terminal_only_match_never_becomes_game_and_is_explained(self):
        gui = FakeGui(item(101, class_name="ConsoleWindowClass"))
        with self.assertRaisesRegex(RuntimeError, "终端窗口，已排除"):
            select_game_window(gui, TITLE)


class PinnedWindowTests(unittest.TestCase):
    def environment(self, stack, gui):
        stack.enter_context(patch("lionbot.windows.require_windows"))
        stack.enter_context(patch("ctypes.windll", SimpleNamespace(user32=Mock()), create=True))
        screen = Mock()
        mss = SimpleNamespace(mss=Mock(return_value=screen))
        stack.enter_context(patch.dict("sys.modules", {"win32gui": gui, "mss": mss}))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return mss, screen

    def test_monitor_pin_never_reenumerates_after_second_matching_window_appears(self):
        gui = FakeGui(item(101))
        with ExitStack() as stack:
            mss, screen = self.environment(stack, gui)
            main = GameWindow(TITLE, frame_size=(1366, 768))
            gui.windows[202] = item(202)  # 主循环选好后，又出现另一个相同标题。
            monitor = GameWindow(TITLE, hwnd=main.hwnd)
            self.assertEqual(monitor.hwnd, main.hwnd)
            self.assertEqual(gui.enumerations, 1)
            self.assertEqual(mss.mss.call_count, 2)  # 每个调用方创建自己的截图实例。
            monitor.close()
            main.close()
            self.assertEqual(screen.close.call_count, 2)

    def test_dead_or_reused_handle_does_not_fall_back_to_another_matching_window(self):
        for previous in (None, item(101, title="Other app")):
            gui = FakeGui(item(202))
            if previous:
                gui.windows[101] = previous
            with ExitStack() as stack:
                mss, _ = self.environment(stack, gui)
                with self.assertRaisesRegex(RuntimeError, "原游戏窗口.*已关闭或标题改变"):
                    GameWindow(TITLE, hwnd=101)
                self.assertEqual(gui.enumerations, 0)
                mss.mss.assert_not_called()


if __name__ == "__main__":
    unittest.main()
