import unittest

from build_sports import sports_entries, render_sports


class SportsPlaylistTests(unittest.TestCase):
    def test_khl_and_football_first_and_header_variant_preserved(self):
        fixed = (
            '#EXTM3U\n'
            '#EXTINF:-1 group-title="🏆 Спорткомплекс",КХЛ\n'
            'https://example.org/khl.m3u8\n'
            '#EXTINF:-1 group-title="🏆 Спорткомплекс",КХЛ\n'
            'https://example.org/khl.m3u8\n'
            '#EXTINF:-1 group-title="Фильмы",Комедия\n'
            'https://example.org/movie.m3u8\n'
        )
        dynamic = (
            '#EXTM3U\n'
            '#EXTINF:-1,Матч! Футбол 1\n'
            '#EXTVLCOPT:http-user-agent=TVPlayer/1.0\n'
            'https://example.org/football.m3u8\n'
            '#EXTINF:-1,Матч! Футбол 1\n'
            'https://example.org/football.m3u8\n'
        )
        rows = sports_entries(fixed, dynamic)
        self.assertEqual(len(rows), 3)
        text = render_sports(rows)
        self.assertEqual(text.count('#EXTINF:'), 3)
        self.assertNotIn('movie.m3u8', text)
        self.assertIn('🏒 Хоккей / КХЛ', text)
        self.assertIn('⚽ Футбол / Матч!', text)
        self.assertIn('резерв без свежей проверки', text)
        self.assertIn('проверено', text)
        self.assertIn('#EXTVLCOPT:http-user-agent=TVPlayer/1.0', text)
        self.assertLess(text.index('КХЛ'), text.index('Матч! Футбол 1'))

    def test_secret_and_private_urls_excluded(self):
        fixed = '#EXTM3U\n#EXTINF:-1,КХЛ\nhttps://example.org/live.m3u8?token=abc\n'
        dynamic = '#EXTM3U\n#EXTINF:-1,КХЛ\nhttp://127.0.0.1/khl.m3u8\n'
        self.assertEqual(sports_entries(fixed, dynamic), [])


if __name__ == '__main__':
    unittest.main()
