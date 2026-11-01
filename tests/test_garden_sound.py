import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lionbot.garden import main
from lionbot.garden_sound import prepare_alert_sound


class GardenSoundTests(unittest.TestCase):
    def test_all_three_builtin_waves_use_native_file_playback(self):
        winsound = SimpleNamespace(PlaySound=Mock(), SND_FILENAME=0x20000, SND_MEMORY=4, SND_NODEFAULT=2)
        with patch.dict('sys.modules', winsound=winsound):
            for selection in ('1', '2', '3'):
                play = prepare_alert_sound(selection)
                play(); play()
                data, flags = winsound.PlaySound.call_args.args
                self.assertTrue(Path(data).is_file())
                self.assertEqual(flags, 0x20002)
        self.assertEqual(winsound.PlaySound.call_count, 6)

    def test_custom_wave_is_cached_and_playback_failure_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'sound.wav'
            path.write_bytes((Path(__file__).parent.parent/'lionbot/sounds/1.wav').read_bytes())
            winsound = SimpleNamespace(PlaySound=Mock(side_effect=RuntimeError('audio device unavailable')), SND_FILENAME=0x20000, SND_MEMORY=4, SND_NODEFAULT=2)
            with patch('lionbot.lie_alert.beep_alert') as beep, patch.dict('sys.modules', winsound=winsound):
                play = prepare_alert_sound('sound.wav', Path(folder))
                path.unlink()
                play()
                beep.assert_called_once()

    def test_root_sounds_override_package_file_and_memory_fallback_is_canonical(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'sounds'/'1.wav'; path.parent.mkdir()
            path.write_bytes((Path(__file__).parent.parent/'lionbot/sounds/1.wav').read_bytes())
            def playback(value, flags):
                if isinstance(value, str):
                    self.assertEqual(Path(value), path)
                    raise RuntimeError('file mode failed')
                self.assertTrue(value.startswith(b'RIFF'))
                self.assertEqual(flags, 6)
            winsound = SimpleNamespace(PlaySound=Mock(side_effect=playback), SND_FILENAME=0x20000, SND_MEMORY=4, SND_NODEFAULT=2)
            with patch.dict('sys.modules', winsound=winsound), patch('lionbot.lie_alert.beep_alert') as beep:
                play = prepare_alert_sound('1', Path(folder))
                path.unlink()
                play()
                self.assertEqual(winsound.PlaySound.call_count, 2)
                beep.assert_not_called()

    def test_invalid_file_fails_during_preparation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'bad.wav'; path.write_text('not audio')
            with self.assertRaises(ValueError):
                prepare_alert_sound('bad.wav', Path(folder))

    def test_cli_voice_override_uses_three_plays_without_game_input(self):
        play = Mock()
        with tempfile.TemporaryDirectory() as folder, patch('lionbot.garden_sound.prepare_alert_sound', return_value=play) as prepare, \
             patch('lionbot.garden.time.sleep'), patch('lionbot.windows.GameWindow') as window:
            self.assertEqual(main(['--config',str(Path(folder)/'config.garden.local.json'), '--test-alert-sound','--alert-voice','2']),0)
            self.assertEqual(prepare.call_args.args[0],'2')
            self.assertEqual(play.call_count,3)
            window.assert_not_called()
