"""Preload short PCM WAV alerts before monitoring; playback never performs TTS."""
import io
from pathlib import Path
import wave


def prepare_alert_sound(selection="beep", root=Path(".")):
    from .lie_alert import beep_alert
    if selection == "beep":
        return beep_alert
    root = Path(root).resolve()
    if selection in ("1", "2", "3"):
        candidates = (root / "sounds" / f"{selection}.wav", root / "lionbot" / "sounds" / f"{selection}.wav",
                      Path(__file__).parent / "sounds" / f"{selection}.wav")
        path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0]).resolve()
    else:
        path = (root / selection).resolve()
    try:
        data = path.read_bytes()
        if len(data) > 10_000_000:
            raise ValueError("报警 WAV 不得超过 10MB")
        with wave.open(io.BytesIO(data), "rb") as audio:
            if audio.getcomptype() != "NONE" or audio.getnchannels() not in (1, 2) or audio.getsampwidth() not in (1, 2):
                raise ValueError("报警音请使用单声道或双声道、8/16 位 PCM WAV")
            if not audio.getnframes() or not 8000 <= audio.getframerate() <= 96000:
                raise ValueError("报警 WAV 为空或采样率不支持")
            params = audio.getparams()
            frames = audio.readframes(audio.getnframes())
            if len(frames) != params.nframes * params.nchannels * params.sampwidth:
                raise ValueError("报警 WAV 数据不完整")
        # Strip optional metadata chunks for the in-memory WinMM fallback.
        canonical = io.BytesIO()
        with wave.open(canonical, "wb") as audio:
            audio.setparams(params)
            audio.writeframes(frames)
        data = canonical.getvalue()
    except (OSError, wave.Error, EOFError) as error:
        raise ValueError(f"无法读取报警声音 {path}：{error}") from error
    print(f"报警声音文件：{path}；{params.nchannels} 声道／{params.sampwidth * 8} 位 PCM／{params.framerate}Hz", flush=True)
    warned = False
    def play():
        nonlocal warned
        errors = []
        try:
            import winsound
            # Native file mode first: do not rely on SND_MEMORY being usable.
            try:
                winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
                return
            except (OSError, RuntimeError) as error:
                errors.append(f"文件播放 {type(error).__name__}: {error}")
            try:
                winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
                return
            except (OSError, RuntimeError) as error:
                errors.append(f"内存播放 {type(error).__name__}: {error}")
        except ImportError as error:
            errors.append(f"无法加载 winsound：{error}")
        if errors:
            if not warned:
                print(f"自定义报警音播放失败：{path}；" + "；".join(errors) + "；已回退 Beep。", flush=True)
                warned = True
            beep_alert()
    return play
