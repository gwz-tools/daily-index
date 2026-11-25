import unittest
from epg_audit import audit


class AuditTests(unittest.TestCase):
    def test_exact_unique_only_and_never_edit(self):
        playlist = ('#EXTM3U\n#EXTINF:-1 group-title="🏒 Хоккей" tvg-id="",КХЛ HD\nhttps://stream\n'
                    '#EXTINF:-1 group-title="⚽ Футбол" tvg-id="",Матч Футбол\nhttps://other\n')
        xml = ('<tv><channel id="khl"><display-name>KHL HD</display-name></channel>'
               '<channel id="f1"><display-name>Матч Футбол</display-name></channel>'
               '<channel id="f2"><display-name>Матч Футбол</display-name></channel></tv>').encode()
        result = audit(playlist, xml)
        self.assertEqual(result['channels'], 2)
        self.assertEqual(result['ids'], 0)
        self.assertEqual(result['suggestions'], [])

    def test_exact_match_suggested_only(self):
        playlist = '#EXTINF:-1 group-title="Спорт" tvg-id="", КХЛ HD\nurl\n'
        xml = '<tv><channel id="khl"><display-name>КХЛ HD</display-name><icon src="https://example.org/khl.png"/></channel></tv>'.encode()
        result = audit(playlist, xml)
        self.assertEqual(result['suggestions'][0]['candidate_id'], 'khl')
        self.assertEqual(result['suggestions'][0]['candidate_logo'], 'https://example.org/khl.png')
        self.assertEqual(result['suggestions'][0]['status'], 'review_required')

    def test_internal_exact_id_only_if_unique(self):
        playlist = ('#EXTINF:-1 tvg-id="one",КХЛ HD\nurl\n'
                    '#EXTINF:-1 tvg-id="",КХЛ HD\nurl2\n'
                    '#EXTINF:-1 tvg-id="first",КХЛ\nurl3\n'
                    '#EXTINF:-1 tvg-id="second",КХЛ\nurl4\n'
                    '#EXTINF:-1 tvg-id="",КХЛ\nurl5\n')
        result = audit(playlist)
        self.assertEqual([x['candidate_id'] for x in result['internal_suggestions']], ['one'])


if __name__ == '__main__':
    unittest.main()
