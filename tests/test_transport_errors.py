import io
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

from airsim_cp.protocol import AirSIMError, Transport


class TransportErrorTests(unittest.TestCase):
    def request_error(self, status, body, path='/pair/v1/claim'):
        stream = io.BytesIO(body)
        transport = Transport()
        transport.opener = Mock()
        transport.opener.open.side_effect = HTTPError(
            'http://127.0.0.1:8580' + path, status, 'error', {}, stream)
        with self.assertRaises(AirSIMError) as caught:
            transport.request('127.0.0.1', 8580, 'POST', path, b'{}')
        self.assertEqual(caught.exception.status, status)
        self.assertTrue(stream.closed)
        transport.opener.open.assert_called_once()
        return str(caught.exception)

    def test_storage_failure_explains_recovery_without_claiming_capacity_full(self):
        message = self.request_error(500, b'{"error":"gateway_key_storage_failed"}')
        self.assertIn('无法保存配对密钥', message)
        self.assertIn('生成新码', message)
        self.assertNotIn('已满', message)

    def test_consumed_or_wrong_pairing_code_does_not_suggest_clock_change(self):
        message = self.request_error(401, b'{"error":"pairing_auth_failed"}')
        self.assertIn('生成新码', message)
        self.assertNotIn('时间', message)

    def test_untrusted_or_invalid_response_is_not_displayed(self):
        for body in (b'{"error":"private-secret"}', b'private-secret',
                     b'[]', b'null', b'x' * 5000, b'\xff'):
            with self.subTest(body=body[:32]):
                message = self.request_error(500, body)
                self.assertIn('HTTP 500', message)
                self.assertNotIn('private-secret', message)

    def test_non_pairing_auth_failure_retains_clock_guidance(self):
        message = self.request_error(401, b'authentication required', '/api/sms/send')
        self.assertIn('时间', message)
