import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from airsim_cp.protocol import AirSIMError, Client, ProfileStore, Transport, valid_host, valid_port
from airsim_cp.audio import AudioSession
from airsim_cp.app import App
from airsim_cp.ui import View
from mock_phone import Phone


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.phone = Phone().start(); self.client = Client(self.phone.profile())
    def tearDown(self): self.phone.close()
    def test_encrypted_pairing_and_clock_offset(self):
        result = Client().pair('127.0.0.1', self.phone.port, '123456', accept_local_mode=True)
        self.assertAlmostEqual(result['clock_offset'], 120, delta=1)
        self.assertEqual(Client(result).health()['agent_id'], self.phone.agent)
        snapshot = Client(result).snapshot()
        self.assertIn('你好', snapshot['sms'][0]['content'])
        self.assertFalse(any('/ack' in path for _, path, _ in self.phone.requests))
    def test_direct_android_gateway_uses_health_metadata(self):
        self.phone.direct_gateway=True
        result=self.client.snapshot()
        self.assertEqual(result['status']['battery']['percent'],38)
        self.assertEqual(result['status']['phone_number'],'10000')
        self.assertFalse(any(path=='/api/android/status' for _,path,_ in self.phone.requests))
    def test_pair_requires_explicit_mode_consent(self):
        with self.assertRaises(AirSIMError): Client().pair('127.0.0.1', self.phone.port, '123456')
        self.assertEqual(self.phone.requests, [])
    def test_wrong_pairing_code(self):
        with self.assertRaises(AirSIMError): Client().pair('127.0.0.1', self.phone.port, '654321', accept_local_mode=True)
    def test_expired_pairing_window(self):
        self.phone.expired = True
        with self.assertRaises(AirSIMError): Client().pair('127.0.0.1', self.phone.port, '123456', accept_local_mode=True)
        self.assertEqual(len(self.phone.requests), 1)
    def test_tampered_route_proof(self):
        self.phone.tamper_route = True
        with self.assertRaises(AirSIMError): Client().pair('127.0.0.1', self.phone.port, '123456', accept_local_mode=True)
    def test_health_identity_proof_blocks_operations(self):
        self.phone.tamper_health = True
        with self.assertRaises(AirSIMError): self.client.send_sms('10000', 'test fixture only')
        self.assertFalse(any(method == 'POST' for method, _, _ in self.phone.requests))
    def test_phone_identity_mismatch(self):
        self.client.profile['agent_id'] = 'another-device'
        with self.assertRaises(AirSIMError): self.client.health()
    def test_send_sms_payload(self):
        result = self.client.send_sms('10000', '模拟测试')
        self.assertTrue(result['sent'])
        self.assertEqual(self.phone.requests[-1][2], {'phone': '10000', 'message': '模拟测试'})
    def test_verified_dial_and_call_id_controls(self):
        result = self.client.dial('10000'); self.assertEqual(result['android_id'], 'fixture-call')
        with self.assertRaises(AirSIMError): self.client.control('hangup', 'stale-id')
        self.assertIsNotNone(self.phone.active)
        self.client.control('hangup', 'fixture-call'); self.assertIsNone(self.phone.active)
    def test_dial_rejects_bad_receipt_without_retry(self):
        self.phone.tamper_dial = True
        with self.assertRaises(AirSIMError): self.client.dial('10000')
        self.assertEqual(sum(path == '/api/calls/dial' for _, path, _ in self.phone.requests), 1)
    def test_post_failure_not_retried(self):
        self.phone.fail_post = True
        with self.assertRaises(AirSIMError): self.client.send_sms('10000', 'test')
        self.assertEqual(sum(path == '/api/sms/send' for _, path, _ in self.phone.requests), 1)
    def test_nonce_unique(self):
        self.client.health(); self.client.health()
        self.assertEqual(len(self.phone.seen_nonces), 2)
    def test_private_network_validation(self):
        for host in ('8.8.8.8', 'example.com', '192.168.1.2/evil', '0.0.0.0', '169.254.1.1'):
            with self.assertRaises(AirSIMError): valid_host(host)
        self.assertEqual(valid_host('100.64.1.2'), '100.64.1.2')
        with self.assertRaises(AirSIMError): valid_port('80')
    def test_profile_permissions_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(Path(temp) / 'settings'); store.save(self.client.profile)
            self.assertEqual(store.load(), self.client.profile)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.directory.stat().st_mode & 0o777, 0o700)


class UITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.app = App(ProfileStore(self.temp.name), demo=True)
    def tearDown(self): self.app.close(); self.temp.cleanup()
    def test_navigation_and_chinese_frame(self):
        self.app.key('6'); self.app.key('ENTER')
        self.assertEqual(self.app.page, 'message')
        self.assertEqual(View().render(self.app).size, (320, 170))
        self.app.key('ESC'); self.assertEqual(self.app.page, 'messages')
    def test_text_keys_are_not_shortcuts(self):
        self.app.key('6'); self.app.key('8')
        for key in '45678fzxc': self.app.key(key)
        self.assertEqual(self.app.fields[0], '45678fzxc')
        self.assertEqual(self.app.page, 'compose')
    def test_send_requires_confirmation(self):
        self.app.form('compose', ['10000', 'test']); self.app.field = 1; self.app.key('ENTER')
        self.assertEqual(self.app.page, 'confirm'); self.assertFalse(self.app.busy)
        self.app.key('ENTER'); self.assertEqual(self.app.page, 'confirm')
        self.app.key('4'); self.assertEqual(self.app.page, 'compose')
    def test_help_preserves_confirmation(self):
        self.app.form('compose', ['10000','test']); self.app.field=1;self.app.key('ENTER'); self.app.key('HELP'); self.app.key('ESC')
        self.assertEqual(self.app.page, 'confirm'); self.app.key('4')
        self.assertEqual(self.app.page, 'compose')
    def test_background_refresh_does_not_swallow_text(self):
        self.app.form('compose', ['', ''])
        self.app.busy = True; self.app.work_kind = 'refresh'
        for key in '45678fzxc': self.app.key(key)
        self.assertEqual(self.app.fields[0], '45678fzxc')
        self.app.busy = False

    def test_message_history_is_private_and_scoped(self):
        self.app.store.save_messages('phone-a', [{'content': 'private'}])
        self.assertEqual(self.app.store.load_messages('phone-a'), [{'content': 'private'}])
        self.assertEqual(self.app.store.load_messages('phone-b'), [])
        self.assertEqual(self.app.store.history_path('phone-a').stat().st_mode & 0o777, 0o600)

    def test_pair_warning_precedes_request(self):
        self.app.form('pair', ['192.168.1.2', '12345', '123456']); self.app.field = 2; self.app.key('ENTER')
        self.assertEqual(self.app.page, 'confirm'); self.assertIn('本地模式', self.app.confirm_body)
        self.assertFalse(self.app.busy)
    def test_audio_requires_connected_call(self):
        class FakeClient:
            def health(self): pass
            def signed(self, *_): return {'active': {'id': 'new-call', 'state': 'active'}}, ''
        with self.assertRaises(AirSIMError): self.app.audio.start(FakeClient(), 'old-call')
    def test_fragmented_pcm_handshake(self):
        class Fragmented:
            data = iter([b'AIR', b'SIM', b'RE', b'ADY'])
            def recv(self, _): return next(self.data, b'')
        self.assertEqual(AudioSession.exact(Fragmented(), 11), b'AIRSIMREADY')
    def test_close_during_network_is_prompt(self):
        self.app.demo = False
        self.app.submit(lambda: time.sleep(0.5), 'refresh')
        started = time.monotonic(); self.app.close()
        self.assertLess(time.monotonic() - started, 0.1)


if __name__ == '__main__': unittest.main()
