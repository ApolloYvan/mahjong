import unittest
from unittest.mock import Mock

from mj.notify import notify_sequences


class NotifyTests(unittest.TestCase):
    def test_reads_data_frames(self):
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([b": keepalive\n", b'data: {"seq": 4}\n', b'data: {"closed": true}\n']))
        stream.close = Mock()
        api = Mock()
        api.notify.return_value = stream
        self.assertEqual(list(notify_sequences(api, "g")), [{"seq": 4}, {"closed": True}])
        stream.close.assert_called_once()

        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([b": keepalive\n", b'data: {"closed": true}\n']))
        stream.close = Mock()
        api = Mock()
        api.notify.return_value = stream
        self.assertEqual(list(notify_sequences(api, "g")), [{"closed": True}])
        stream.close.assert_called_once()
