"""只检测测谎标题、停键报警并等待人工恢复，不操作测谎内容。"""
from __future__ import annotations

import io
import json
import threading
import time
import wave
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class TitleMatch:
    score: float
    edge_score: float
    scale: float
    box: tuple[int, int, int, int] | None
    threshold: float

    @property
    def found(self):
        return bool(self.box is not None and self.score >= self.threshold and self.edge_score >= .65)

    def describe(self):
        result = "检出测谎标题" if self.found else "未检出测谎标题"
        return (f"{result}：分数={self.score:.3f}/{self.threshold:.3f}，"
                f"文字边缘={self.edge_score:.3f}/0.650，缩放={self.scale:.3f}，位置={self.box}")


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
        self.last_report = TitleMatch(0., 0., 1., None, threshold)
        self.variants = {}
        # 缩小画面筛选位置/比例，只有少数候选在原图核验，避免多次全尺寸匹配。
        self.stride = max(1, min(3, h // 7))
        self.scales = tuple(round(.75 + i * .05, 3) for i in range(26))
        for scale in (1., *self.scales):
            self._variant(scale)

    def _variant(self, scale):
        scale = round(scale, 4)
        if scale not in self.variants:
            h, w = self.gray.shape
            size = (round(w * scale), round(h * scale))
            gray = cv2.resize(self.gray, size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
            blurred = cv2.GaussianBlur(gray, (3, 3), .7)
            edges = cv2.Canny(gray, 60, 140)
            small = cv2.resize(blurred, (max(1, size[0] // self.stride), max(1, size[1] // self.stride)),
                               interpolation=cv2.INTER_AREA)
            self.variants[scale] = gray, blurred, edges, small
        return self.variants[scale]

    def _verify(self, gray, blurred, scale, x, y, origin):
        template, smooth, edges, _ = self._variant(scale)
        h, w = template.shape
        patch = gray[y:y + h, x:x + w]
        smooth_patch = blurred[y:y + h, x:x + w]
        score = max(float(cv2.matchTemplate(patch, template, cv2.TM_CCOEFF_NORMED)[0, 0]),
                    float(cv2.matchTemplate(smooth_patch, smooth, cv2.TM_CCOEFF_NORMED)[0, 0]))
        actual_edges = cv2.Canny(patch, 60, 140)
        # 允许抗锯齿或压缩导致的一像素偏差，仍要求双向文字结构重合。
        kernel = np.ones((3, 3), np.uint8)
        forward = np.count_nonzero((edges > 0) & (cv2.dilate(actual_edges, kernel) > 0)) / max(1, np.count_nonzero(edges))
        backward = np.count_nonzero((actual_edges > 0) & (cv2.dilate(edges, kernel) > 0)) / max(1, np.count_nonzero(actual_edges))
        overlap = float(min(forward, backward))
        return TitleMatch(score, overlap, scale, (origin[0] + x, origin[1] + y, w, h), self.threshold)

    def find(self, frame):
        report = self.inspect(frame)
        return (report.score, report.box) if report.found else None

    def inspect(self, frame):
        x, y, w, h = self.roi or (0, 0, frame.shape[1], frame.shape[0])
        if min(x, y) < 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
            raise ValueError("测谎搜索区域超出截图，请恢复窗口尺寸或重新采集")
        image = frame[y:y + h, x:x + w]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (3, 3), .7)
        best = TitleMatch(0., 0., 1., None, self.threshold)
        small = cv2.resize(blurred, (max(1, w // self.stride), max(1, h // self.stride)),
                           interpolation=cv2.INTER_AREA)
        candidates = []
        for scale in (1., *(s for s in self.scales if s != 1.)):
            template = self._variant(scale)[3]
            sh, sw = template.shape
            if sh > small.shape[0] or sw > small.shape[1]:
                continue
            scores = cv2.matchTemplate(small, template, cv2.TM_CCOEFF_NORMED)
            # 同一比例保留两个不同位置，防止边框的最高峰挡住真实文字。
            for _ in range(2):
                _, score, _, (px, py) = cv2.minMaxLoc(scores)
                candidate = (score, scale, round(px * w / small.shape[1]), round(py * h / small.shape[0]))
                candidates.append(candidate)
                if scale == 1.:
                    report = self._refine(gray, blurred, candidate, (x, y))
                    if report is not None and report.score > best.score:
                        best = report
                    if report is not None and report.found:
                        self.last_report = report
                        return report
                scores[max(0, py - sh // 2):py + sh // 2 + 1,
                       max(0, px - sw // 2):px + sw // 2 + 1] = -1
        for _, scale, px, py in sorted(candidates, reverse=True)[:6]:
            for refined in (scale, round(scale - .025, 3), round(scale + .025, 3)):
                report = self._refine(gray, blurred, (0., refined, px, py), (x, y))
                if report is None:
                    continue
                if report.score > best.score:
                    best = report
                if report.found:
                    self.last_report = report
                    return report
        self.last_report = best
        return best

    def _refine(self, gray, blurred, candidate, origin):
        _, scale, px, py = candidate
        _, template, _, _ = self._variant(scale)
        th, tw = template.shape
        h, w = gray.shape
        left, top = max(0, px - 5), max(0, py - 5)
        right, bottom = min(w, px + tw + 6), min(h, py + th + 6)
        patch = blurred[top:bottom, left:right]
        if patch.shape[0] < th or patch.shape[1] < tw:
            return None
        _, _, _, (dx, dy) = cv2.minMaxLoc(cv2.matchTemplate(patch, template, cv2.TM_CCOEFF_NORMED))
        return self._verify(gray, blurred, scale, left + dx, top + dy, origin)


def load_detector(config, root):
    from .vision import read_image

    path = root / config.lie_alert_template
    if not path.is_file():
        raise ValueError("尚未采集测谎标题，已禁止挂机按键。请先运行 python -m lionbot.simple "
                         "--calibrate-alert --image <原始测谎截图路径>；也可省略 --image 从游戏采集")
    return TitleDetector(read_image(path), config.lie_alert_threshold, config.lie_alert_roi)


def save_diagnostic(frame, detector, directory, reason="manual"):
    """只在人工请求或首次报警时保存证据，不逐帧写盘。"""
    from .vision import write_image

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    report = detector.last_report
    marked = frame.copy()
    if detector.roi:
        x, y, w, h = detector.roi
        cv2.rectangle(marked, (x, y), (x + w - 1, y + h - 1), (255, 255, 0), 2)
    if report.box:
        x, y, w, h = report.box
        color = (0, 255, 0) if report.found else (0, 165, 255)
        cv2.rectangle(marked, (x, y), (x + w - 1, y + h - 1), color, 2)
    label = f"{'HIT' if report.found else 'MISS'} score={report.score:.3f} edge={report.edge_score:.3f} scale={report.scale:.3f}"
    cv2.putText(marked, label, (5, min(22, marked.shape[0] - 2)), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 0, 255), 1, cv2.LINE_AA)
    write_image(Path(f"{prefix}-screen.png"), frame)
    write_image(Path(f"{prefix}-match.png"), marked)
    write_image(Path(f"{prefix}-template.png"), detector.color)
    metadata = {**asdict(report), "found": report.found, "reason": reason,
                "search_roi": detector.roi, "frame_size": list(frame.shape[1::-1]),
                "template_size": list(detector.color.shape[1::-1]), "description": report.describe()}
    Path(f"{prefix}.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return prefix


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


@lru_cache(maxsize=1)
def alert_wave():
    """普通 PCM 音频经默认播放设备输出，便于远程软件采集系统声音。"""
    rate = 22050
    count = round(rate * .18)
    samples = np.arange(count, dtype=np.float64)
    fade = np.minimum(1., np.minimum(samples, count - 1 - samples) / (rate * .008))
    first = np.sin(2 * np.pi * 1400 * samples / rate) * fade
    second = np.sin(2 * np.pi * 900 * samples / rate) * fade
    pcm = (np.concatenate((first, np.zeros(round(rate * .04)), second)) * 16000).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(pcm.tobytes())
    return output.getvalue()


def sound_alert():
    try:
        import winsound

        try:
            # 不设置 SND_ASYNC 时默认同步播放；较旧 Python 没有 SND_SYNC 常量。
            winsound.PlaySound(alert_wave(), winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except (OSError, RuntimeError):
            # 普通音频设备不可用时仍尝试旧的本机蜂鸣提醒。
            winsound.Beep(1400, 180)
            winsound.Beep(900, 180)
    except (ImportError, OSError, RuntimeError):
        print("\a", end="", flush=True)


def beep_alert():
    """保留用户已验证可经 UU 远程转发的原始 Beep 双音。"""
    try:
        import winsound

        winsound.Beep(1400, 180)
        winsound.Beep(900, 180)
    except (ImportError, OSError, RuntimeError):
        print("\a", end="", flush=True)


class LieAlertMonitor:
    def __init__(self, detector, frame_size, window_factory, on_alarm, interval=.25, sound=sound_alert,
                 diagnostic_dir=None):
        self.detector, self.frame_size = detector, tuple(frame_size)
        self.window_factory, self.on_alarm, self.interval, self.sound = window_factory, on_alarm, interval, sound
        self.state = AlertState(max_age=max(.5, 3 * interval))
        self.stopped = threading.Event()
        self.save_requested = threading.Event()
        self.diagnostic_dir = diagnostic_dir
        self.status_line = "测谎监控：等待首次截图"
        self.threads = []

    def status(self):
        return self.status_line

    def request_snapshot(self):
        self.save_requested.set()

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
            return True
        return False

    def _save(self, frame, reason):
        self.save_requested.clear()
        if self.diagnostic_dir is not None:
            try:
                prefix = save_diagnostic(frame, self.detector, self.diagnostic_dir, reason)
                print(f"测谎诊断已保存：{prefix}-screen.png、-match.png、-template.png 和 .json", flush=True)
            except Exception as error:
                print(f"测谎诊断保存失败（报警和停键仍生效）：{error}", flush=True)

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
                        self.status_line = "测谎监控：游戏失焦，未执行匹配；请切回游戏，F10 诊断请求会保留"
                    else:
                        frame = window.capture()
                        if tuple(frame.shape[1::-1]) != self.frame_size:
                            raise ValueError("游戏客户区尺寸改变，请恢复原尺寸")
                        if window.focused():
                            found = self.detector.find(frame)
                            report = getattr(self.detector, "last_report", None)
                            detail = report.describe() if isinstance(report, TitleMatch) else str(found)
                            self.status_line = f"测谎监控：{detail}，耗时={(time.monotonic() - started) * 1000:.0f}ms"
                            new_alarm = self._publish(hit=found is not None)
                            if self.save_requested.is_set() or new_alarm:
                                self._save(frame, "alarm" if new_alarm else "F10")
                        else:
                            self.state.unavailable()  # 截图中途切到其他程序，不把覆盖画面当作游戏。
                            self.status_line = "测谎监控：截图中途游戏失焦，已丢弃该帧，等待切回游戏"
                except Exception as error:
                    self.status_line = f"测谎监控异常：{error}"
                    self._publish(error=f"测谎监控异常，已停键：{error}")
                self.stopped.wait(max(.01, self.interval - (time.monotonic() - started)))
        except Exception as error:
            self.status_line = f"测谎截图未能启动：{error}"
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


def watch_alert(config, path):
    """独立验证真实游戏截图、匹配和声音，不创建键盘控制器。"""
    from .windows import GameWindow, require_windows

    require_windows()
    from pynput.keyboard import Key, Listener

    if config.frame_size is None:
        raise ValueError("缺少游戏尺寸，请先 --setup；已有挂机配置无需重做")
    detector = load_detector(config, path.parent)
    stopped, acknowledge = threading.Event(), threading.Event()
    monitor = LieAlertMonitor(detector, config.frame_size,
                              lambda: GameWindow(config.window_title, frame_size=config.frame_size),
                              lambda reason: print(f"警报：{reason}！请手动处理，F8 静音。", flush=True),
                              config.lie_alert_interval, diagnostic_dir=path.parent / "debug-output" / "lie-alert")

    def on_release(key):
        if key == Key.f12:
            stopped.set()
        elif key == Key.f8:
            acknowledge.set()
        elif key == Key.f10:
            monitor.request_snapshot()

    listener = Listener(on_release=on_release)
    try:
        listener.start()
        monitor.start()
        print("仅监测测谎，不发送任何游戏按键。请切回游戏；每秒显示分数，F10 保存诊断，F8 静音，F12 退出。", flush=True)
        while not stopped.is_set():
            if acknowledge.is_set():
                acknowledge.clear()
                monitor.state.acknowledge()
            print(monitor.status(), flush=True)
            stopped.wait(1.)
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        monitor.stop()
        listener.stop()


def calibrate_alert(path: Path, image_path=None, *, loader=None, saver=None):
    from dataclasses import replace
    from .calibrate import fresh, select_roi
    from .simple import save_simple
    from .simple_cycle import load_simple
    from .vision import crop, read_image, write_image
    from .windows import GameWindow

    config = (loader or load_simple)(path)
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
        (saver or save_simple)(path, replace(config, lie_alert_enabled=True))
        print("测谎标题已保存，站位和攻击参数保留。请先 --test-alert-sound 检查声音，再启动 --run。")
    finally:
        if window is not None:
            window.close()
        cv2.destroyAllWindows()
