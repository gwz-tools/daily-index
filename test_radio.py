import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import radio_support as radio
import refresh as r


class RadioTests(unittest.TestCase):
    def test_audio_probe_accepts_audio_without_video(self):
        class Response:
            def __init__(self, output=b'', returncode=0):
                self.stdout = output
                self.returncode = returncode
        def mock_run(args, **kwargs):
            if args[0] == 'ffprobe':
                return Response(json.dumps({
                    'streams': [{'codec_type': 'audio', 'codec_name': 'mp3'}]
                }).encode())
            if args[0] == 'ffmpeg':
                return Response()
            raise AssertionError(args)
        with patch.object(radio.subprocess, 'run', side_effect=mock_run):
            self.assertTrue(radio.check_audio_sample(b'\x11' * 4096))

    def test_video_only_is_rejected_as_radio(self):
        class Response:
            stdout = b'{"streams":[{"codec_type":"video"}]}'
            returncode = 0
        with patch.object(radio.subprocess, 'run', return_value=Response()):
            with self.assertRaisesRegex(r.Reject, 'radio_missing_audio'):
                radio.check_audio_sample(b'\x11' * 4096)

    def test_collect_from_radio_directory_without_tv_video_check(self):
        content = (
            '#EXTM3U\n'
            '#EXTINF:-1 group-title="Russia" radio="true",Europa Plus\n'
            'https://example.org/ep.mp3\n'
            '#EXTINF:-1 group-title="Russia" radio="true",Rock FM\n'
            'https://example.org/rock.aac\n'
        )
        class FakeFetcher:
            def __init__(self, *args, **kwargs):
                pass
            def get(self, url, **kwargs):
                if url.endswith('ru.m3u'):
                    return content.encode(), url
                return b'\x11' * 4096, url
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'order.txt'
            path.write_text('[📻 Радио]\nEuropa Plus\nRock FM\n')
            with patch.object(r, 'Fetcher', FakeFetcher), patch.object(
                radio, 'check_audio_sample', return_value=True
            ):
                stations, report = radio.collect_radio(
                    'https://example.org/ru.m3u', str(path),
                    max_stations=2, max_probes=2
                )
        self.assertEqual(report['working'], 2)
        self.assertEqual({e.name for e in stations}, {'Europa Plus', 'Rock FM'})
        self.assertTrue(all(e.group == '📻 Радио' for e in stations))

    def test_render_radio_metadata_and_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'order.txt'
            path.write_text('[🏒 Хоккей]\nКХЛ\n[📻 Радио]\nKHL Radio\n')
            entry = r.Entry('KHL Radio', 'https://example.org/live.mp3',
                            group='Russia', language='Russian',
                            source='[radio] AlonDrilich Russia')
            text = r.render({'radio::khl': entry}, str(path))
            self.assertIn('radio="true"', text)
            self.assertIn('group-title="📻 Радио"', text)
            self.assertNotIn('group-title="🏒 Хоккей"', text)


if __name__ == '__main__':
    unittest.main()
