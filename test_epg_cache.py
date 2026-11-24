import gzip
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from epg_cache import atomic, build, date, playlist_ids


class EpgCacheTests(unittest.TestCase):
    def test_priority_filter_and_fallback(self):
        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        def guide(channel, title):
            return (f'<tv><channel id="{channel}"><display-name>{channel}</display-name></channel>'
                    f'<programme channel="{channel}" start="20261124100000 +0000" '
                    f'stop="20261124110000 +0000"><title>{title}</title></programme></tv>').encode()
        data = {'https://first': guide('a', 'First'),
                'https://second': guide('a', 'Wrong') .replace(b'</tv>',
                  b'<channel id="b"><display-name>b</display-name></channel>'
                  b'<programme channel="b" start="20261124110000 +0000" '
                  b'stop="20261124120000 +0000"><title>Second</title></programme></tv>')}
        config = {'sources': [{'name': url, 'url': url} for url in data]}
        xml, report = build(config, {'a', 'b'}, now, fetch=lambda url, limit: data[url])
        root = ET.fromstring(xml)
        self.assertEqual(report['covered_ids'], 2)
        self.assertEqual([p.findtext('title') for p in root.findall('programme')], ['First', 'Second'])

    def test_no_good_source_does_not_touch_old_file(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'guide.xml.gz'
            atomic(output, gzip.compress(b'old'))
            with self.assertRaises(ValueError):
                build({'sources': [{'name': 'bad', 'url': 'https://bad'}]}, {'a'},
                      fetch=lambda url, limit: b'<bad/>')
            self.assertEqual(gzip.decompress(output.read_bytes()), b'old')

    def test_ids_and_offset(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'playlist.m3u'
            path.write_text('#EXTM3U\n#EXTINF:-1 tvg-id="khl.ru",KHL\nhttp://example\n'
                            '#EXTINF:-1 tvg-id="",Unknown\nhttp://example2\n')
            self.assertEqual(playlist_ids(path), {'khl.ru'})
        self.assertEqual(date('20261124120000 +0300').hour, 9)


if __name__ == '__main__':
    unittest.main()
