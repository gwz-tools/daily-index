import io
import unittest
from datetime import datetime, timezone

from epg_probe import LimitedReader, analyse, tvtime


class EpgProbeTests(unittest.TestCase):
    def test_coverage_and_seven_days(self):
        xml = ('<tv><channel id="KHL"><display-name>КХЛ HD</display-name><icon src="https://x/logo"/></channel>'
               '<channel id="football"><display-name>Футбол</display-name></channel>'
               '<programme channel="KHL" start="20261125100000 +0300" stop="20261125110000 +0300"><title>Игра</title></programme>'
               '<programme channel="football" start="20261201070000 +0000" stop="20261201080000 +0000"><title>Матч</title></programme></tv>').encode()
        entries = [{'name': 'КХЛ HD', 'id': 'KHL', 'group': 'Хоккей', 'logo': ''},
                   {'name': 'Футбол', 'id': '', 'group': 'Футбол', 'logo': ''}]
        report = analyse(LimitedReader(io.BytesIO(xml), 1024), entries,
                         datetime(2026, 11, 25, tzinfo=timezone.utc))
        self.assertEqual(report['direct_epg_entries'], 1)
        self.assertEqual(report['exact_name_with_programmes'], 1)
        self.assertEqual(report['future_days_direct_ids'][0], 1)
        self.assertEqual(report['future_days_direct_ids'][6], 0)

    def test_expansion_limit_and_timezone(self):
        with self.assertRaises(ValueError):
            LimitedReader(io.BytesIO(b'12345'), 4).read()
        self.assertEqual(tvtime('20261125100000 +0300').hour, 7)


if __name__ == '__main__':
    unittest.main()
