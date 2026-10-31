import unittest

from verify_fixed_sports import (
    is_sport, separate_verified_sports, split_records, verify_records
)


class FixedSportVerificationTests(unittest.TestCase):
    def test_only_checked_sport_survives_other_groups_are_byte_identical(self):
        header = '#EXTM3U url-tvg="https://example.org/epg.xml"\n'
        before = (
            '#EXTINF:-1 group-title="📺 Общие каналы WINK 🇷🇺",Первый\n'
            'https://example.org/first.m3u8\n'
        )
        sport_live = (
            '#EXTINF:-1 group-title="🏆 Спорткомплекс",КХЛ\n'
            '#EXTVLCOPT:http-user-agent=TVPlayer/1\n'
            'https://example.org/khl.m3u8\n'
        )
        sport_dead = (
            '#EXTINF:-1 group-title="🏆 Спорткомплекс",Матч! Футбол 1\n'
            'https://example.org/football.m3u8\n'
        )
        after = (
            '#EXTINF:-1 group-title="📷 КАМЕРЫ (ONLINE)",Камера\n'
            'https://example.org/camera.m3u8\n'
        )
        seen = []
        def probe(entry):
            seen.append((entry.url, entry.user_agent))
            return 'LIVE' if 'khl' in entry.url else 'http_403'
        filtered, report = verify_records(
            header + before + sport_live + sport_dead + after,
            probe, workers=2
        )
        self.assertEqual(filtered, header + before + sport_live + after)
        self.assertEqual(report['original_sports'], 2)
        self.assertEqual(report['verified_sports'], 1)
        self.assertEqual(report['excluded_sports'], 1)
        self.assertEqual(len(seen), 2)
        self.assertIn(('https://example.org/khl.m3u8', 'TVPlayer/1'), seen)

    def test_duplicates_and_tokenized_links_are_never_published(self):
        live = (
            '#EXTINF:-1 group-title="🏆 Спорткомплекс",КХЛ 1\n'
            'https://example.org/khl.m3u8\n'
        )
        dup = live.replace('КХЛ 1', 'КХЛ 2')
        token = (
            '#EXTINF:-1 group-title="🏆 Спорткомплекс",КХЛ 3\n'
            'https://example.org/khl.m3u8?token=secret\n'
        )
        called = []
        def probe(entry):
            called.append(entry.url)
            return 'LIVE'
        filtered, report = verify_records('#EXTM3U\n' + live + dup + token, probe)
        self.assertEqual(report['original_sports'], 3)
        self.assertEqual(report['probed_unique_sports'], 1)
        self.assertEqual(report['verified_sports'], 1)
        self.assertEqual(len(called), 1)
        self.assertEqual(filtered.count('#EXTINF:'), 1)
        self.assertNotIn('token=', filtered)

    def test_verified_sport_moved_out_of_fixed_prefix(self):
        original = (
            '#EXTM3U url-tvg="https://example.org/epg"\n'
            '#EXTINF:-1 group-title="📺 Общие",Первый\n'
            'https://example.org/first.m3u8\n'
            '#EXTINF:-1 group-title="🏆 Спорткомплекс",КХЛ\n'
            'https://example.org/khl.m3u8\n'
            '#EXTINF:-1 group-title="📷 КАМЕРЫ (ONLINE)",Камера\n'
            'https://example.org/camera.m3u8\n'
        )
        fixed, sport = separate_verified_sports(original)
        self.assertIn('first.m3u8', fixed)
        self.assertIn('camera.m3u8', fixed)
        self.assertNotIn('khl.m3u8', fixed)
        self.assertIn('khl.m3u8', sport)
        self.assertEqual(sport.count('#EXTINF:'), 1)
        self.assertEqual(fixed.count('#EXTINF:'), 2)

    def test_sport_group_detection_is_exact(self):
        header, rows = split_records(
            '#EXTM3U\n'
            '#EXTINF:-1 group-title="Спорт",KHL\nhttps://example.org/a.m3u8\n'
            '#EXTINF:-1 group-title="🏆 Спорткомплекс",КХЛ\nhttps://example.org/b.m3u8\n'
        )
        self.assertEqual(header, '#EXTM3U\n')
        self.assertEqual([is_sport(x) for x in rows], [False, True])

    def test_all_failed_has_no_unverified_sports(self):
        original = (
            '#EXTM3U\n'
            '#EXTINF:-1 group-title="🏆 Спорткомплекс",КХЛ\n'
            'https://example.org/khl.m3u8\n'
        )
        filtered, report = verify_records(original, lambda _: 'timeout')
        self.assertEqual(filtered, '#EXTM3U\n')
        self.assertEqual(report['verified_sports'], 0)


if __name__ == '__main__':
    unittest.main()
