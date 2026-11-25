import unittest

from epg_mapping_check import validate


class MappingCheckTests(unittest.TestCase):
    def test_accepts_exact_unique_candidate_only(self):
        config = {'source': 'https://example.com/epg.xml.gz', 'status': 'review_only_not_published',
                  'mappings': [{'group': 'News', 'name': 'Дождь', 'epg_id': 'dozhd'}]}
        report = {'source': config['source'], 'name_review_entries': [
            {'group': 'News', 'name': 'Дождь', 'candidate_ids': ['dozhd'],
             'reason': 'unique_name_candidate', 'has_programme': True}]}
        self.assertEqual(validate(config, report)['verified_review_candidates'], 1)
        report['name_review_entries'][0]['reason'] = 'id_shared_by_candidates'
        with self.assertRaises(ValueError):
            validate(config, report)

    def test_rejects_duplicate_and_changed_id(self):
        config = {'source': 'https://example.com/epg.xml.gz', 'status': 'review_only_not_published',
                  'mappings': [{'group': 'News', 'name': 'Дождь', 'epg_id': 'dozhd'}]}
        report = {'source': config['source'], 'name_review_entries': [
            {'group': 'News', 'name': 'Дождь', 'candidate_ids': ['wrong'],
             'reason': 'unique_name_candidate', 'has_programme': True}]}
        with self.assertRaises(ValueError):
            validate(config, report)
        report['name_review_entries'][0]['candidate_ids'] = ['dozhd']
        config['mappings'].append(config['mappings'][0])
        with self.assertRaises(ValueError):
            validate(config, report)


if __name__ == '__main__':
    unittest.main()
