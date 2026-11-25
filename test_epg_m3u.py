import unittest
from epg_m3u import apply_mappings, load_mappings


class MappingTests(unittest.TestCase):
    def test_exact_only_and_preserve_other_data(self):
        entry = '#EXTINF:-1 tvg-id="" group-title="⚽ Футбол",Матч! Премьер\nhttp://example.org/a\n'
        other = '#EXTINF:-1 tvg-id="" group-title="⚽ Футбол",Матч! Футбол 3\nhttp://example.org/b\n'
        result, report = apply_mappings(entry + other, load_mappings('epg_mapping_live.json'))
        self.assertIn('tvg-id="match-premier" group-title="⚽ Футбол",Матч! Премьер', result)
        self.assertIn(other, result)
        self.assertEqual(report['applied'], [('⚽ Футбол', 'Матч! Премьер')])

    def test_duplicate_and_preassigned_untouched(self):
        entry = '#EXTINF:-1 tvg-id="" group-title="📰 Новостные",Дождь\nhttp://example.org/a\n'
        result, report = apply_mappings(entry * 2, load_mappings('epg_mapping_live.json'))
        self.assertEqual(result, entry * 2)
        self.assertFalse(report['applied'])
        assigned = entry.replace('tvg-id=""', 'tvg-id="already"')
        self.assertEqual(apply_mappings(assigned, load_mappings('epg_mapping_live.json'))[0], assigned)
