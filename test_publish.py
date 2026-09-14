from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import publish_dropbox as p
import refresh as r


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {'output': str(self.root/'out.m3u'), 'report': str(self.root/'report.json'), 'dropbox': {'path': '/folder/list.txt', 'expected_file_id': 'id:original'}}
        self.data = r.render({'первый': r.Entry('Первый', 'https://example.org/live.m3u8')}).encode()
        Path(self.config['output']).write_bytes(self.data)
        self.report = {'status': 'ok', 'playlist_sha256': hashlib.sha256(self.data).hexdigest(), 'working_channels': 1, 'checked_at': datetime.now(timezone.utc).isoformat()}
        self.save_report()

    def tearDown(self):
        self.temp.cleanup()

    def save_report(self):
        Path(self.config['report']).write_text(json.dumps(self.report))

    def test_verified_output(self):
        self.assertEqual(p.verified_data(self.config), self.data)

    def test_changed_bytes_are_rejected(self):
        Path(self.config['output']).write_bytes(self.data+b'\n')
        with self.assertRaises(p.PublishError):
            p.verified_data(self.config)

    def test_stale_build_is_rejected(self):
        self.report['checked_at'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.save_report()
        with self.assertRaisesRegex(p.PublishError, 'stale'):
            p.verified_data(self.config)

    def test_missing_secrets_is_failure(self):
        with patch.dict(p.os.environ, {}, clear=True), self.assertRaisesRegex(p.PublishError, 'Missing'):
            p.access_token()

    def test_file_identity_and_revision_are_preserved(self):
        old = {'.tag': 'file', 'id': 'id:original', 'path_lower': '/folder/list.txt', 'rev': 'abc', 'content_hash': 'old'}
        new = dict(old, size=len(self.data), content_hash=p.content_hash(self.data))
        with patch.object(p, 'metadata', side_effect=[old,new]), patch.object(p, 'request_json', return_value=new) as upload:
            p.publish(self.config, 'test-access-token', self.data)
        args = json.loads(upload.call_args.args[2]['Dropbox-API-Arg'])
        self.assertEqual(args['mode'], {'.tag': 'update', 'update': 'abc'})
        self.assertFalse(args['autorename'])

    def test_wrong_destination_never_uploads(self):
        wrong = {'.tag': 'file', 'id': 'id:other'}
        with patch.object(p, 'metadata', return_value=wrong), patch.object(p, 'request_json') as upload:
            with self.assertRaisesRegex(p.PublishError, 'identity'):
                p.publish(self.config, 'test', self.data)
            upload.assert_not_called()

    def test_content_hash_verifies_all_blocks(self):
        data = b'a' * (4*1024*1024) + b'b'
        expected = hashlib.sha256(hashlib.sha256(data[:-1]).digest()+hashlib.sha256(b'b').digest()).hexdigest()
        self.assertEqual(p.content_hash(data), expected)


class SortTests(unittest.TestCase):
    def test_section_order_and_channel_order(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'order.txt'
            path.write_text('[Федеральные]\nПервый\nРоссия 1\n[Новости]\nРоссия 24\n[18+]\nAdult\n', encoding='utf-8')
            entries = {r.norm(name): r.Entry(name, 'https://example.org/'+str(i), group='wrong') for i,name in enumerate(['Adult','Россия 24','Россия 1','Первый'])}
            output = list(r.parse_m3u(r.render(entries, str(path))))
            self.assertEqual([e.name for e in output], ['Первый','Россия 1','Россия 24','Adult'])
            self.assertEqual([e.group for e in output], ['Федеральные','Федеральные','Новости','18+'])

    def test_global_download_budget(self):
        budget = r.Budget(100, 100)
        budget.account(50)
        with self.assertRaisesRegex(r.Reject, 'run_budget'):
            budget.account(51)
        self.assertTrue(budget.exhausted)


if __name__ == '__main__':
    unittest.main()
