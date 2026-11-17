import base64
import hashlib
import io
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch
from airsim_cp.protocol import Client, AirSIMError
from airsim_cp.early_audio import EarlyAudio
from mock_phone import Phone, sign


class Player:
    def __init__(self):self.stdin=io.BytesIO();self.done=False
    def poll(self):return 0 if self.done else None
    def terminate(self):self.done=True
    def kill(self):self.done=True
    def wait(self,timeout=None):return 0


class EarlyAudioTests(unittest.TestCase):
    def setUp(self):
        self.phone=Phone().start();self.phone.early_mode=True
        self.phone.active={'id':'early-call','state':'dialing','direction':'outgoing'}
        self.client=Client(self.phone.profile())
    def tearDown(self):self.phone.close()

    def test_pending_202_is_not_invalid_json(self):
        self.phone.early_pending=1
        with self.assertRaises(AirSIMError) as caught:self.client.early_grant('early-call')
        self.assertEqual(caught.exception.status,202)
        self.assertEqual(self.client.early_grant('early-call')['direction'],'recvonly')

    def test_grant_tampering_is_rejected(self):
        for field,value in [('call_id','other'),('agent_id','other'),('direction','sendrecv'),
                            ('format','other'),('expires_at',0),('session_id','other'),('proof','bad')]:
            with self.subTest(field=field):
                self.phone.early_mutate=lambda g:dict(g,**{field:value})
                with self.assertRaises(AirSIMError):self.client.early_grant('early-call')

    def test_preface_uses_standard_base64_and_binds_every_identity(self):
        grant=self.client.early_grant('early-call')
        magic,stamp,nonce,signature,encoded=EarlyAudio.preface(self.client,grant).decode().strip().split(' ')
        self.assertEqual(magic,'AIRSIMEM1')
        binding=base64.b64decode(encoded,validate=True)
        self.assertEqual(binding.decode().split('\n'),[self.phone.agent,'early-call',grant['session_id'],str(grant['expires_at']),'recvonly','s16le-8000-mono'])
        self.assertEqual(signature,sign(self.phone.secret,'\n'.join(['PCM','/v1/pcm/early-media',hashlib.sha256(binding).hexdigest(),stamp,nonce])))

    def test_early_stream_retries_pending_and_never_captures_or_sends_pcm(self):
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen();server.settimeout(3)
        self.phone.pcm_port=server.getsockname()[1]
        self.client=Client(self.phone.profile());self.phone.early_pending=1
        stop=threading.Event();observed={};player=Player();audio=EarlyAudio()
        def peer():
            try:
                with server.accept()[0] as connection:
                    connection.settimeout(.15);line=bytearray()
                    while not line.endswith(b'\n'):line+=connection.recv(1)
                    observed['magic']=line.decode().split()[0]
                    connection.sendall(b'AIRSIMREADY'+bytes(320)*4)
                    try:observed['uplink']=connection.recv(320)
                    except socket.timeout:observed['uplink']=b''
                    stop.wait(2)
            except Exception as error:observed['error']=type(error).__name__
        thread=threading.Thread(target=peer);thread.start()
        try:
            with patch('airsim_cp.early_audio.subprocess.Popen',return_value=player) as popen:
                audio.start(self.client,'early-call')
                deadline=time.monotonic()+2
                while audio.downlink_bytes<1280 and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual(audio.downlink_bytes,1280)
                self.assertEqual(audio.uplink_bytes,0)
                self.assertEqual(popen.call_count,1)
                self.assertIn('--playback',popen.call_args.args[0])
                self.assertNotIn('--record',popen.call_args.args[0])
                time.sleep(.2)
        finally:
            audio.close();stop.set();thread.join(3);server.close()
        self.assertEqual(observed.get('uplink'),b'')
        self.assertEqual(observed.get('magic'),'AIRSIMEM1')
        self.assertTrue(player.done)

    def test_incoming_or_connected_call_does_not_open_early_stream(self):
        for state,direction in [('incoming','incoming'),('active','outgoing')]:
            self.phone.active.update(state=state,direction=direction)
            audio=EarlyAudio()
            with patch.object(self.client,'health',return_value={'agent_kind':'mu300'}), \
                 patch.object(self.client,'signed',return_value=({'active':self.phone.active},'')), \
                 patch('airsim_cp.early_audio.socket.create_connection') as connect:
                audio.start(self.client,'early-call');audio.close();connect.assert_not_called()

    def test_renewal_cannot_switch_sessions(self):
        audio=EarlyAudio()
        with patch.object(audio.stop_event,'wait',return_value=False), patch.object(audio,'stop') as stop:
            audio.renew(self.client,'early-call','stale-session')
            stop.assert_called_once()
