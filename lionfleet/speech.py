from __future__ import annotations

import base64
import os
import subprocess
import threading
import time
from pathlib import Path


def play_notice(text, speech=True, wav=""):
    if os.name != "nt":
        print("\a" + text, flush=True)
        return
    import winsound

    # Use the already proven Beep; do not require winsound.SND_SYNC.
    try:
        winsound.Beep(1400, 180)
        winsound.Beep(900, 180)
    except RuntimeError:
        winsound.MessageBeep()
    if wav:
        winsound.PlaySound(str(Path(wav).resolve()), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
    elif speech:
        # Message is UTF-8 base64 DATA, never interpolated as PowerShell source.
        data = base64.b64encode(text.encode("utf-8")).decode("ascii")
        script = ("$ErrorActionPreference='Stop';"
                  "$s=New-Object -ComObject SAPI.SpVoice;"
                  "$found=$false;foreach($v in $s.GetVoices()){if($v.GetAttribute('Language') -match '(^|;)804(;|$)'){$s.Voice=$v;$found=$true;break}};"
                  "if(!$found){throw 'No Chinese SAPI voice; configure alarm_wav instead'};"
                  f"$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{data}'));"
                  "[void]$s.Speak($t)")
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                       check=True, timeout=12, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Announcer:
    """Network/UI never waits for speech; one worker fairly cycles through active alarms."""

    def __init__(self, store, root, play=play_notice):
        self.store, self.root, self.play = store, Path(root), play
        self.stop_event = threading.Event()
        self.last = {}
        self.tests = []
        self.lock = threading.Lock()
        self.error = ""
        self.thread = threading.Thread(target=self._run, name="fleet-announcer", daemon=True)

    def start(self):
        self.thread.start()

    def test(self, machine_id):
        machine = self.store.registered[machine_id]
        with self.lock:
            self.tests.append({"machine": machine, "text": machine.machine_name + "测试报警，接收端声音正常"})

    def next_notice(self, now):
        with self.lock:
            if self.tests:
                return self.tests.pop(0)
        active = self.store.notifications()
        keys = {item["key"] for item in active}
        self.last = {key: value for key, value in self.last.items() if key in keys}
        eligible = [item for item in active if now - self.last.get(item["key"], float("-inf")) >= self.store.c.repeat_alarm_seconds]
        if not eligible:
            return None
        item = min(eligible, key=lambda item: self.last.get(item["key"], float("-inf")))
        self.last[item["key"]] = now
        return item

    def _run(self):
        while not self.stop_event.is_set():
            item = self.next_notice(time.monotonic())
            if item:
                try:
                    wav = str(self.root / item["machine"].alarm_wav) if item["machine"].alarm_wav else ""
                    self.play(item["text"], self.store.c.speech_enabled, wav)
                    self.error = ""
                except Exception as error:
                    self.error = f"语音／WAV 失败，普通提示音及面板仍保留：{error}"
                    print(self.error, flush=True)
            self.stop_event.wait(.1)

    def stop(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=.5)
