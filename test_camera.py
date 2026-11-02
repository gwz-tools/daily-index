import json
import tempfile
import unittest
from unittest.mock import patch

import refresh as r


class CameraTests(unittest.TestCase):
    def test_camera_category_routing(self):
        self.assertTrue(r.is_camera_entry(r.Entry(
            "Пушкин Екатерининский дворец", "https://example.org/a.m3u8",
            group="📷 КАМЕРЫ (ONLINE)"
        )))
        self.assertTrue(r.is_camera_entry(r.Entry(
            "Выборг", "https://example.org/cam.m3u8",
            group="Webcams"
        )))
        self.assertFalse(r.is_camera_entry(r.Entry(
            "Мой фильм", "https://example.org/movie.m3u8",
            group="🎬 Кино"
        )))

    def test_silent_camera_video_decodes_without_audio(self):
        class Result:
            def __init__(self, data=b'', ret=0):
                self.stdout = data
                self.returncode = ret
        def run(args, **kw):
            if args[0] == "ffprobe":
                return Result(json.dumps({
                    "streams": [{"codec_type": "video", "codec_name": "h264"}]
                }).encode())
            if args[0] == "ffmpeg":
                self.assertIn("-frames:v", args)
                self.assertNotIn("0:a:0", args)
                return Result()
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(r.subprocess, "run", side_effect=run):
                r.probe_camera_bytes(b'\x47' * 4096, tmp)

    def test_audio_only_is_rejected_as_camera(self):
        class Result:
            stdout = b'{"streams":[{"codec_type":"audio"}]}'
            returncode = 0
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(r.subprocess, "run", return_value=Result()):
                with self.assertRaisesRegex(r.Reject, 'camera_missing_video'):
                    r.probe_camera_bytes(b'\x47' * 4096, tmp)

    def test_camera_flag_selects_camera_probe(self):
        class Fetcher:
            def get(self, url, **kwargs):
                return b'\xff\xd8' + b'\x00' * 2048 + b'\xff\xd9', url
        with patch.object(r, "probe_camera_bytes") as camera, patch.object(
            r, "probe_bytes"
        ) as tv:
            self.assertEqual(
                r.check_stream("https://example.org/cam.jpg", Fetcher(), camera=True),
                "https://example.org/cam.jpg"
            )
            camera.assert_called_once()
            tv.assert_not_called()

    def test_camera_sort_and_group_without_order_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            order = Path(tmp) / "order.txt"
            order.write_text(
                "[📷 Камеры]\nШвейцария Железная дорога Кур–Ароза\n"
                "[🎬 Кино]\nФильм\n", encoding="utf-8"
            )
            camera = r.Entry("Выборг", "https://example.org/camera.m3u8",
                             group="Webcams", language="Russian")
            playlist = r.render({"camera": camera}, str(order))
            self.assertIn('group-title="📷 Камеры",Выборг', playlist)


if __name__ == "__main__":
    unittest.main()
