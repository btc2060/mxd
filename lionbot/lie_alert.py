"""只检测测谎标题、停键报警并等待人工恢复，不操作测谎内容。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import cv2
import numpy as np


class TitleDetector:
    def __init__(self, image, threshold=.90, roi=None):
        self.color = image
        self.gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        h, w = self.gray.shape
        self.edges = cv2.Canny(self.gray, 60, 140)
        if (not 40 <= w <= 400 or not 12 <= h <= 100 or self.gray.std() < 10
                or np.count_nonzero(self.edges) < 40):
            raise ValueError("测谎样本请只框选完整标题文字及少量底色，不选空白、倒计时或移动图案")
        self.threshold, self.roi = threshold, roi

    def find(self, frame):
        x, y, w, h = self.roi or (0, 0, frame.shape[1], frame.shape[0])
        if min(x, y) < 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
            raise ValueError("测谎搜索区域超出截图，请恢复窗口尺寸或重新采集")
        image = frame[y:y + h, x:x + w]
        th, tw = self.gray.shape
        if h < th or w < tw:
            return None
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        _, score, _, (px, py) = cv2.minMaxLoc(cv2.matchTemplate(gray, self.gray, cv2.TM_CCOEFF_NORMED))
        if score < self.threshold:
            return None
        patch = image[py:py + th, px:px + tw]
        color = float(cv2.matchTemplate(patch, self.color, cv2.TM_CCOEFF_NORMED)[0, 0])
        edges = cv2.Canny(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), 60, 140).astype(np.float32)
        edge_score = float(np.sum(edges * self.edges)) / max(1., float(np.linalg.norm(edges) * np.linalg.norm(self.edges)))
        if color < self.threshold or edge_score < .45:
            return None
        return min(score, color), (x + px, y + py, tw, th)


def load_detector(config, root):
    from .vision import read_image

    path = root / config.lie_alert_template
    if not path.is_file():
        raise ValueError("尚未采集测谎标题，已禁止挂机按键。请先运行 python -m lionbot.simple "
                         "--calibrate-alert --image <原始测谎截图路径>；也可省略 --image 从游戏采集")
    return TitleDetector(read_image(path), config.lie_alert_threshold, config.lie_alert_roi)


class AlertState:
    def __init__(self, max_age=.75, clock=time.monotonic):
        self.lock = threading.RLock()
        self.clock, self.max_age = clock, max_age
        self.latched = self.silenced = False
        self.last_scan = float("-inf")
        self.clear_frames = 0
        self.reason = "等待首次测谎检查"

    def publish(self, hit=False, error=""):
        with self.lock:
            self.last_scan = self.clock() if not error else float("-inf")
            self.clear_frames = self.clear_frames + 1 if not hit and not error else 0
            new_alarm = bool(hit or error) and not self.latched
            if hit or error:
                self.latched = True
                self.reason = error or "检测到测谎窗口"
                if new_alarm:
                    self.silenced = False
            elif not self.latched:
                self.reason = "测谎监控正常"
            return new_alarm

    def unavailable(self):
        with self.lock:
            self.last_scan = float("-inf")
            self.clear_frames = 0

    def allows_input(self):
        with self.lock:
            return not self.latched and self.clock() - self.last_scan <= self.max_age

    def is_latched(self):
        with self.lock:
            return self.latched

    def should_ring(self):
        with self.lock:
            return self.latched and not self.silenced

    def acknowledge(self, focused=True):
        with self.lock:
            self.silenced = True
            if (focused and self.latched and self.clear_frames >= 2
                    and self.clock() - self.last_scan <= self.max_age):
                self.latched = False
                self.reason = "人工确认恢复"
                return True
            return False


def sound_alert():
    try:
        import winsound

        winsound.Beep(1400, 180)
        winsound.Beep(900, 180)
    except (ImportError, OSError, RuntimeError):
        print("\a", end="", flush=True)


class LieAlertMonitor:
    def __init__(self, detector, frame_size, window_factory, on_alarm, interval=.25, sound=sound_alert):
        self.detector, self.frame_size = detector, tuple(frame_size)
        self.window_factory, self.on_alarm, self.interval, self.sound = window_factory, on_alarm, interval, sound
        self.state = AlertState(max_age=max(.5, 3 * interval))
        self.stopped = threading.Event()
        self.threads = []

    def start(self):
        self.threads = [threading.Thread(target=self._scan, name="lie-alert-scan", daemon=True),
                        threading.Thread(target=self._ring, name="lie-alert-sound", daemon=True)]
        for thread in self.threads:
            thread.start()

    def stop(self):
        self.stopped.set()
        for thread in self.threads:
            thread.join(timeout=1)

    def _publish(self, hit=False, error=""):
        if self.state.publish(hit, error):
            self.on_alarm(self.state.reason)

    def _scan(self):
        window = None
        try:
            # MSS 截图实例在使用它的线程内创建，不能复用主循环的实例。
            window = self.window_factory()
            while not self.stopped.is_set():
                started = time.monotonic()
                try:
                    if not window.focused():
                        self.state.unavailable()
                    else:
                        frame = window.capture()
                        if tuple(frame.shape[1::-1]) != self.frame_size:
                            raise ValueError("游戏客户区尺寸改变，请恢复原尺寸")
                        if window.focused():
                            self._publish(hit=self.detector.find(frame) is not None)
                        else:
                            self.state.unavailable()  # 截图中途切到其他程序，不把覆盖画面当作游戏。
                except Exception as error:
                    self._publish(error=f"测谎监控异常，已停键：{error}")
                self.stopped.wait(max(.01, self.interval - (time.monotonic() - started)))
        except Exception as error:
            self._publish(error=f"测谎截图未能启动，已停键：{error}")
        finally:
            if window is not None:
                window.close()

    def _ring(self):
        while not self.stopped.is_set():
            if self.state.should_ring():
                self.sound()
                self.stopped.wait(.65)
            else:
                self.stopped.wait(.05)


def calibrate_alert(path: Path, image_path=None):
    from dataclasses import replace
    from .calibrate import fresh, select_roi
    from .simple import save_simple
    from .simple_cycle import load_simple
    from .vision import crop, read_image, write_image
    from .windows import GameWindow

    config = load_simple(path)
    window = None
    try:
        if image_path is not None:
            frame = read_image(image_path)
        else:
            window = GameWindow(config.window_title)
            frame = fresh(window, "请让测谎窗口标题完整可见；这一步只截图，不发送按键。")
        print("只框选固定的‘谎言探测仪’标题文字和少量底色，不选倒计时、星星或说明正文。")
        roi = select_roi("Select LIE DETECTOR TITLE text only / Enter confirm", frame)
        title = crop(frame, roi)
        TitleDetector(title)
        write_image(path.parent / config.lie_alert_template, title)
        save_simple(path, replace(config, lie_alert_enabled=True))
        print("测谎标题已保存，站位和攻击参数保留。请先 --test-alert-sound 检查声音，再启动 --run。")
    finally:
        if window is not None:
            window.close()
        cv2.destroyAllWindows()
