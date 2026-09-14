import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import refresh as r


class PolicyTests(unittest.TestCase):
    def test_redirect_to_signed_url_is_never_requested(self):
        class Opener:
            def open(self, request, **kwargs):
                raise HTTPError(request.full_url, 302, 'redirect', {'Location': 'https://example.org/video.m3u8?token=x'}, None)
        with patch.object(r.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), patch.object(r, 'build_opener', return_value=Opener()) as factory:
            with self.assertRaisesRegex(r.Reject, 'query_parameters'):
                r.Fetcher().get('https://example.org/clean.m3u8')
            self.assertEqual(factory.call_count, 1)

    def test_dns_private_address_is_never_requested(self):
        with patch.object(r.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('192.168.1.1', 443))]), patch.object(r, 'build_opener') as factory:
            with self.assertRaisesRegex(r.Reject, 'nonpublic_address'):
                r.Fetcher().get('https://example.org/a')
            factory.assert_not_called()

    def test_plain_domain_and_ip(self):
        for url in ['https://example.org/live/index.m3u8', 'http://8.8.8.8:8080/channel/12', 'https://example.org/segment_1758000000.ts']:
            r.url_policy(url)

    def test_secrets_and_nonpublic_rejected(self):
        for tail in ['?token=x', '?key=x', '?foo=x', '#x', '?sig=x']:
            with self.subTest(tail=tail), self.assertRaises(r.Reject):
                r.url_policy('https://example.org/a.m3u8' + tail)
        for url in ['https://user:pass@example.org/a', 'https://example.org/token/foo/live.m3u8', 'https://example.org/%2574oken/foo/a', 'https://example.org/1234567890abcdef1234567890abcdef/a', 'https://example.org/live/user/pass/12.ts', 'http://127.0.0.1/a', 'http://10.0.0.1/a', 'http://[::1]/a', 'file:///etc/passwd', 'https://example.org/a|User-Agent=foo']:
            with self.subTest(url=url), self.assertRaises(r.Reject):
                r.url_policy(url)

    def test_keys_are_rejected_without_removing_them(self):
        with self.assertRaisesRegex(r.Reject, 'encrypted_hls'):
            r.hls_plan('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:6,\na.ts', 'https://example.org/a.m3u8')

    def test_late_token_segment_is_rejected(self):
        with self.assertRaisesRegex(r.Reject, 'query_parameters'):
            r.hls_plan('#EXTM3U\n#EXT-X-TARGETDURATION:6\na.ts\nb.ts\nc.ts\nsecret.ts?auth=x', 'https://example.org/a.m3u8')

    def test_clean_hls_relative_segments(self):
        variants, segments, init = r.hls_plan('#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\na.ts\n#EXTINF:6,\nb.ts', 'https://example.org/live/index.m3u8')
        self.assertEqual(segments, ['https://example.org/live/a.ts', 'https://example.org/live/b.ts'])
        self.assertFalse(variants)
        self.assertIsNone(init)

    def test_external_audio_fails_closed(self):
        with self.assertRaisesRegex(r.Reject, 'external_rendition'):
            r.hls_plan('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=400000\nvideo.m3u8', 'https://example.org/master.m3u8')

    def test_parse_quoted_comma_and_custom_header(self):
        entries = list(r.parse_m3u('#EXTM3U\n#EXTINF:-1 group-title="Кино, сериалы" tvg-language="Russian",Первый канал HD\n#EXTVLCOPT:http-user-agent=foo\nhttps://example.org/a.m3u8'))
        self.assertEqual(entries[0].name, 'Первый канал HD')
        self.assertEqual(entries[0].group, 'Кино, сериалы')
        self.assertTrue(entries[0].restricted)

    def test_language_and_time_shifts(self):
        index = r.LanguageIndex({'первый канал': 'Первый канал'})
        self.assertEqual(index.identify(r.Entry('Первый канал HD', 'x'))[0], 'первый канал')
        self.assertIsNone(index.identify(r.Entry('Український канал', 'x')))
        self.assertIsNone(index.identify(r.Entry('Первый канал', 'x', language='English')))
        self.assertNotEqual(r.norm('Первый канал +2'), r.norm('Первый канал'))

    def test_render_sanitizes_and_deduplicates(self):
        out = r.render({'a': r.Entry('Test\n#fake', 'https://example.org/a'), 'b': r.Entry('Duplicate', 'https://example.org/a')})
        self.assertEqual(out.count('#EXTINF:'), 1)
        self.assertNotIn('\n#fake', out)

    def test_discovery(self):
        self.assertEqual(r.discover('<a href="/test.m3u">test</a>', 'https://example.org/'), ['https://example.org/test.m3u'])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / 'test.ts'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '5', '-c:v', 'mpeg2video', '-c:a', 'mp2', '-metadata:s:a:0', 'language=rus', '-f', 'mpegts', str(path)], check=True)
        cls.media = path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_video_and_audio_decode(self):
        r.probe_bytes(self.media, self.temp.name)

    def test_html_200_is_not_a_working_stream(self):
        class Fake:
            def get(self, url, **kw):
                return b'<html>Pay for subscription</html>', url
        with self.assertRaisesRegex(r.Reject, 'not_direct'):
            r.check_stream('https://example.org/a', Fake())

    def test_manifest_and_media_are_both_checked(self):
        media = self.media
        calls = []
        class Fake:
            def get(self, url, **kw):
                r.url_policy(url)
                calls.append(url)
                if url.endswith('.m3u8'):
                    return b'#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\na.ts\n#EXTINF:5,\nb.ts\n', url
                return media, url
        self.assertEqual(r.check_stream('https://example.org/index.m3u8', Fake()), 'https://example.org/index.m3u8')
        self.assertIn('https://example.org/a.ts', calls)
        self.assertIn('https://example.org/b.ts', calls)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {'sources': [{'name': 'source', 'url': 'https://example.org/source.m3u'}], 'language_reference': 'https://example.org/rus.m3u', 'russian_aliases': {'первый': 'Первый'}, 'output': str(self.root/'out.m3u'), 'state': str(self.root/'state.json'), 'report': str(self.root/'report.json'), 'workers': 1}
        self.path = self.root/'config.json'
        self.path.write_text(json.dumps(self.config))

    def tearDown(self):
        self.temp.cleanup()

    def test_dead_primary_replaced_and_token_never_probed(self):
        data = '#EXTM3U\n#EXTINF:-1,Первый\nhttps://example.org/0.m3u8\n#EXTINF:-1,Первый\nhttps://example.org/1.m3u8\n#EXTINF:-1,Первый\nhttps://example.org/2.m3u8?key=x\n'
        calls = []
        def check(url, fetcher):
            calls.append(url)
            if url.endswith('0.m3u8'):
                raise r.Reject('decode_failed')
            return url
        with patch.object(r.Fetcher, 'get', return_value=(data.encode(), 'https://example.org/source.m3u')), patch.object(r, 'check_stream', side_effect=check):
            self.assertEqual(r.main(['--config', str(self.path)]), 0)
        self.assertEqual(len(calls), 2)
        out = Path(self.config['output']).read_text()
        self.assertIn('1.m3u8', out)
        self.assertNotIn('key=', out)
        self.assertNotIn('0.m3u8', out)

    def test_outage_does_not_publish_empty_or_claim_success(self):
        out = Path(self.config['output'])
        out.write_text('previous bytes')
        with patch.object(r.Fetcher, 'get', side_effect=r.Reject('network_failed')):
            self.assertEqual(r.main(['--config', str(self.path)]), 1)
        self.assertEqual(out.read_text(), 'previous bytes')
        self.assertEqual(json.loads(Path(self.config['report']).read_text())['status'], 'failed_no_verified_streams')


if __name__ == '__main__':
    unittest.main()
