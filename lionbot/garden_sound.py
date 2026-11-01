"""Preload short PCM WAV alerts before monitoring; playback never performs TTS."""
import io
from pathlib import Path
import wave


def prepare_alert_sound(selection="beep", root=Path(".")):
    from .lie_alert import beep_alert
    if selection == "beep":
        return beep_alert
    path = (Path(__file__).parent / "sounds" / f"{selection}.wav" if selection in ("1", "2", "3")
            else Path(root) / selection)
    try:
        data = path.read_bytes()
        if len(data) > 10_000_000:
            raise ValueError("报警 WAV 不得超过 10MB")
        with wave.open(io.BytesIO(data), "rb") as audio:
            if audio.getcomptype() != "NONE" or audio.getnchannels() not in (1, 2) or audio.getsampwidth() not in (1, 2):
                raise ValueError("报警音请使用单声道或双声道、8/16 位 PCM WAV")
            if not audio.getnframes() or not 8000 <= audio.getframerate() <= 96000:
                raise ValueError("报警 WAV 为空或采样率不支持")
    except (OSError, wave.Error, EOFError) as error:
        raise ValueError(f"无法读取报警声音 {path}：{error}") from error
    warned = False
    def play():
        nonlocal warned
        try:
            import winsound
            # Synchronous playback runs only in the existing dedicated sound thread.
            winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except (ImportError, OSError, RuntimeError):
            if not warned:
                print("自定义报警音播放失败，已回退原 Beep 报警。", flush=True)
                warned = True
            beep_alert()
    return play
