import hashlib
import unittest
from unittest.mock import patch
import publish_yandex as y

DATA = b"#EXTM3U\n#EXTINF:-1,Test\nhttps://example.org/live\n"
URL = "https://disk.yandex.ru/d/test"
META = {"size": len(DATA), "sha256": hashlib.sha256(DATA).hexdigest(), "public_url": URL}


class YandexMirrorTest(unittest.TestCase):
    @patch("publish_yandex.request")
    @patch("publish_yandex.api")
    def test_existing_unchanged(self, api, req):
        api.side_effect = [META, META, {"href": "https://example.org/download"}]
        req.return_value = DATA
        self.assertEqual(y.publish(DATA, "token"), URL)
        self.assertEqual(api.call_count, 3)
        req.assert_called_once_with("GET", "https://example.org/download", max_bytes=len(DATA))

    @patch("publish_yandex.request")
    @patch("publish_yandex.api")
    def test_first_publish(self, api, req):
        api.side_effect = [None, {"href": "https://example.org/upload"}, dict(META, public_url=None),
                           {}, META, {"href": "https://example.org/download"}]
        req.side_effect = [b"", DATA]
        self.assertEqual(y.publish(DATA, "token"), URL)
        self.assertEqual(api.call_count, 6)
        self.assertEqual(req.call_count, 2)

    @patch("publish_yandex.request")
    @patch("publish_yandex.api")
    def test_reject_wrong_download(self, api, req):
        api.side_effect = [META, META, {"href": "https://example.org/download"}]
        req.return_value = b"wrong"
        with self.assertRaises(y.MirrorError):
            y.publish(DATA, "token")

    @patch("publish_yandex.request")
    @patch("publish_yandex.api")
    def test_reject_changed_public_link(self, api, req):
        api.side_effect = [META, dict(META, public_url="https://disk.yandex.ru/d/changed")]
        with self.assertRaises(y.MirrorError):
            y.publish(DATA, "token")
        req.assert_not_called()

    def test_reject_insecure_link(self):
        with self.assertRaises(y.MirrorError):
            y.https("http://example.org")
