"""Preload short PCM WAV alerts before monitoring; playback never performs TTS."""
import io
from pathlib import Path
import wave


def voice_number(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ValueError("报警声音编号必须是正整数")
    return str(int(value))


def _unknown_data_size(data):
    offset = 12
    while offset + 8 <= len(data):
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        if kind == b"data":
            return size == 0xffffffff
        offset += 8 + size + (size & 1)
    return False


def prepare_alert_sound(selection="beep", root=Path(".")):
    from .lie_alert import beep_alert
    if selection == "beep":
        return beep_alert
    root = Path(root).resolve()
    if selection.isascii() and selection.isdecimal():
        selection = voice_number(selection)
        candidates = (root / "sounds" / f"{selection}.wav", root / "lionbot" / "sounds" / f"{selection}.wav",
                      Path(__file__).parent / "sounds" / f"{selection}.wav")
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise ValueError(f"未找到编号 {selection} 的报警声音，请添加文件：{candidates[0]}")
        path = path.resolve()
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
            width = params.nchannels * params.sampwidth
            frames = audio.readframes(min(audio.getnframes(), len(data) // width + 1))
            streaming = _unknown_data_size(data)
            if not frames or len(frames) % width or (not streaming and len(frames) != params.nframes * width):
                raise ValueError("报警 WAV 数据不完整")
        params = params._replace(nframes=len(frames) // width)
        resampled = params.framerate not in (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000)
        if resampled:
            import numpy as np
            dtype = np.dtype('u1' if params.sampwidth == 1 else '<i2')
            samples = np.frombuffer(frames, dtype=dtype).reshape(-1, params.nchannels)
            count = max(1, round(params.nframes * 44100 / params.framerate))
            target = np.arange(count) * params.framerate / 44100
            converted = np.column_stack([np.interp(target, np.arange(params.nframes), samples[:, channel])
                                         for channel in range(params.nchannels)])
            frames = np.rint(converted).astype(dtype).tobytes()
            params = params._replace(framerate=44100, nframes=count)
        # Strip optional metadata chunks for the in-memory WinMM fallback.
        canonical = io.BytesIO()
        with wave.open(canonical, "wb") as audio:
            audio.setparams(params)
            audio.writeframes(frames)
        data = canonical.getvalue()
    except (OSError, wave.Error, EOFError) as error:
        raise ValueError(f"无法读取报警声音 {path}：{error}") from error
    print(f"报警声音文件：{path}；{params.nchannels} 声道／{params.sampwidth * 8} 位 PCM／{params.framerate}Hz", flush=True)
    if streaming or resampled:
        print("已在内存中兼容 WAV 未知长度头部或非常用采样率，原文件未修改。", flush=True)
    warned = False
    def play():
        nonlocal warned
        errors = []
        try:
            import winsound
            if streaming or resampled:
                try:
                    winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
                    return
                except (OSError, RuntimeError) as error:
                    errors.append(f"兼容音频播放 {type(error).__name__}: {error}")
            # Native file mode first: do not rely on SND_MEMORY being usable.
            try:
                winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
                return
            except (OSError, RuntimeError) as error:
                errors.append(f"文件播放 {type(error).__name__}: {error}")
            if not (streaming or resampled):
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
