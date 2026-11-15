import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import struct
import tempfile
import time
import unittest
from unittest.mock import Mock
import uuid
from types import SimpleNamespace

from airsim_cp import ble_wire as wire
from airsim_cp.ble import BLELink,BLEClient
from airsim_cp.protocol import AirSIMError,ProfileStore
from airsim_cp.app import App
from airsim_cp.ui import View
from fake_ble import FakeGatt,derive


class BLEWireTests(unittest.TestCase):
    def test_address_recovery_requires_saved_key_proof(self):
        key=secrets.token_bytes(32)
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        public=X25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        nonce=secrets.token_bytes(32);identity='fixture';challenge='challenge'
        transcript=b''.join(struct.pack('>H',len(x))+x for x in (b'\1',identity.encode(),public,bytes(32),nonce,bytes(32)))
        proof=hmac.new(key,b'AirSIM/VoBLE/v1/address-recovery/'+challenge.encode()+transcript,hashlib.sha256).digest()[:16]
        response={'op':'begin','challenge_id':challenge,'android_identity':identity,'android_public':wire.b64(public),'android_nonce':wire.b64(nonce),'address_recovery_proof':wire.b64(proof)}
        saved={'agent_id':identity,'ble_key':wire.b64(key)}
        self.assertIsNotNone(wire.Pairing(response,saved).reconnect)
        response['address_recovery_proof']=wire.b64(bytes(16))
        with self.assertRaisesRegex(AirSIMError,'密钥已变化'):wire.Pairing(response,saved)
    def test_peer_rejection_is_not_reported_as_bad_signature(self):
        pending=object.__new__(wire.Pairing)
        for reason,message in wire.PAIRING_ERRORS.items():
            with self.subTest(reason=reason),self.assertRaisesRegex(AirSIMError,message.split('。')[0]):
                pending.verify_ready({'ok':False,'error':reason})
        with self.assertRaisesRegex(AirSIMError,'手机拒绝'):
            pending.verify_ready({'ok':False,'error':'untrusted payload'})

    def test_actual_invalid_confirmation_tag_still_rejected(self):
        pending=object.__new__(wire.Pairing);pending.key=bytes(32);pending.challenge='fixture'
        with self.assertRaisesRegex(AirSIMError,'签名无效'):
            pending.verify_ready({'ready':True,'confirmation_tag':wire.b64(bytes(16))})

    def test_control_independent_hmac(self):
        key=bytes(range(32));session=b'session';body={'request_id':'fixture'}
        data=wire.control_encode(key,session,'dial','call',12,body)
        magic,g,ns,nn,nc,ni,np=struct.unpack('>4sQBBBBH',data[:18]);self.assertEqual(magic,b'\xc3\x7e\1\2')
        offset=18;fields=[]
        for n in (ns,nn,nc,ni,np):fields.append(data[offset:offset+n]);offset+=n
        ss,nonce,cmd,call,payload=fields;stamp=json.loads(payload)['timestamp_ms']
        context=struct.pack('>H',len(ss))+ss+struct.pack('>H',len(call))+call+struct.pack('>Q',g)
        control=derive(key,hashlib.sha256(context).digest(),b'AirSIM/VoBLE/v1/control')
        expected=hmac.new(control,struct.pack('>H',len(nonce))+nonce+struct.pack('>Q',stamp)+data[:-16],hashlib.sha256).digest()[:16]
        self.assertEqual(data[-16:],expected)
    def test_fragment_roundtrip_and_bounds(self):
        value=secrets.token_bytes(2000);reader=wire.Reassembler();result=None
        for fragment in reversed(wire.fragments(value,2,7,180)):
            parsed=reader.append(fragment)
            if parsed:result=parsed
        self.assertEqual(result,(2,value))
        with self.assertRaises(AirSIMError):wire.fragments(value,2,1,20)
        with self.assertRaises(AirSIMError):reader.append(b'invalid')
    def test_replay_and_forgery(self):
        key=secrets.token_bytes(32);data=wire.control_encode(key,b'session','response','c',1,{})
        reader=wire.ControlReader();reader.decode(key,data)
        with self.assertRaises(AirSIMError):reader.decode(key,data)
        with self.assertRaises(AirSIMError):wire.ControlReader().decode(key,data[:-1]+bytes([data[-1]^1]))
    def test_mulaw_reference(self):
        self.assertEqual(wire.mulaw_encode(struct.pack('<hhhh',0,0,32124,-32124)),bytes([255,255,128,0]))
        self.assertEqual(struct.unpack('<hhhh',wire.mulaw_decode(bytes([255,127,128,0]))),(0,0,32124,-32124))
    def test_audio_crypto_direction_and_fragmentation(self):
        key=secrets.token_bytes(32);raw=bytes(160)
        sealed=wire.media_crypt(key,2,1,7,1120,raw)
        header=struct.pack('>7sIIBBB',b'\xa5\x5a\1\1\3\1\1',7,1120,0,1,len(sealed))
        reader=wire.AudioReader();frames=reader.append(key,header+sealed)
        self.assertEqual(len(frames[0][1]),320);self.assertEqual(reader.append(key,header+sealed),[])
        with self.assertRaises(Exception):wire.media_crypt(key,1,1,7,1120,sealed,True)
    def test_media_bad_length_rejected(self):
        with self.assertRaises(AirSIMError):wire.AudioReader().append(bytes(32),b'bad')


class BLEClientTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=ProfileStore(self.temp.name)
        self.peer=None
        def factory(address,**kwargs):
            if self.peer is None:self.peer=FakeGatt(address,**kwargs)
            return self.peer
        self.link=BLELink(client_factory=factory)
    def tearDown(self):self.link.close();self.temp.cleanup()
    def pair(self):
        offer=self.link.begin('AA:BB:CC:DD:EE:FF');self.assertEqual(len(offer['sas']),6)
        self.assertFalse(self.link.ready)
        profile=self.link.confirm();return BLEClient(profile,self.store,self.link)
    def test_pair_control_sms_and_events(self):
        client=self.pair();self.assertTrue(client.health()['ok'])
        client.send_sms('10000','蓝牙短信')
        client.dial('10000');snapshot=client.snapshot()
        self.assertEqual(snapshot['calls']['active']['id'],'android-call-1')
        self.link.loop.call_soon_threadsafe(self.peer.control_emit,'sms_event','',0,{'sender':'fixture','content':'received','delivery_id':'sms-1'})
        time.sleep(.05);self.assertEqual(client.snapshot()['sms'][0]['content'],'received')
        client.control('hangup','android-call-1');self.assertIsNone(client.snapshot()['calls']['active'])
    def test_terminal_events_keep_observed_missed_calls(self):
        client=self.pair()
        for identity,generation,states in [('missed',1,['incoming','ended']),('answered',2,['incoming','active','ended'])]:
            for state in states:
                self.link.loop.call_soon_threadsafe(self.peer.control_emit,'call_state',identity,generation,
                    {'state':state,'direction':'incoming','number':'10000'})
                time.sleep(.03)
        records=client.snapshot()['calls']['history']
        self.assertTrue(next(c for c in records if c['id']=='missed')['missed'])
        self.assertFalse(next(c for c in records if c['id']=='answered')['missed'])
        self.assertIsNone(self.link.active)
    def test_reconnect_rotates_and_persists_key(self):
        client=self.pair();old=client.profile['ble_key']
        self.link.disconnect();self.link.begin('AA:BB:CC:DD:EE:FF')
        self.assertNotEqual(old,client.profile['ble_key']);self.assertEqual(self.store.load()['ble_key'],client.profile['ble_key'])
    def test_stale_call_not_operated(self):
        client=self.pair();client.dial('10000')
        before=len(self.peer.commands)
        with self.assertRaises(AirSIMError):client.control('hangup','another-call')
        self.assertEqual(len(self.peer.commands),before)

    def test_active_call_uses_events_without_device_info_queries(self):
        client=self.pair();client.dial('10000');self.link.info_at=0
        before=len(self.peer.commands)
        for _ in range(20):self.assertEqual(client.snapshot()['calls']['active']['id'],'android-call-1')
        self.assertEqual(len(self.peer.commands),before)
        client.control('hangup','android-call-1');client.snapshot()
        self.assertEqual(self.peer.commands[-1][0],'device_info')

    def test_simultaneous_fragmented_controls_remain_valid(self):
        self.pair();self.peer.mtu_size=80
        original=self.peer.write_gatt_char
        async def slow_write(*args,**kwargs):
            await asyncio.sleep(.001)
            return await original(*args,**kwargs)
        self.peer.write_gatt_char=slow_write
        async def concurrent():
            return await asyncio.gather(self.link._command('device_info'),self.link._command('device_info'))
        self.assertEqual(len(self.link.run(concurrent())),2)

    def test_remote_hangup_stops_audio_while_success_notice_open(self):
        client=self.pair();app=App(self.store,client=client)
        app.audio.close();app.audio=Mock(active=True,error='')
        app.audio_call_id='ended-call';app.calls={'active':{'id':'ended-call','state':'active'}}
        app.next_poll=time.monotonic()+60
        app.notice('语音已开启','fixture','calls')
        try:
            app.tick()
            app.audio.stop_async.assert_called_once()
            self.assertEqual(app.audio.error,'');self.assertEqual(app.audio_call_id,'')
            self.assertEqual(app.page,'calls');self.assertIsNone(app.calls['active'])
        finally:app.close()

    def test_remote_hangup_clears_late_media_error_without_losing_text(self):
        client=self.pair();app=App(self.store,client=client)
        app.audio.close();app.audio=Mock(active=False,error='音频中断')
        app.audio_call_id='ended-call';app.next_poll=time.monotonic()+60
        app.form('compose',['10000','unfinished text'])
        try:
            app.tick()
            app.audio.stop_async.assert_called_once();self.assertEqual(app.audio.error,'')
            self.assertEqual(app.page,'compose');self.assertEqual(app.fields[1],'unfinished text')
        finally:app.close()

    def test_existing_phone_pairing_without_key_stops_before_prepare(self):
        class PairedPeer(FakeGatt):
            def pair_emit(peer,value):
                if value.get('op')=='begin':value['address_recovery_proof']=wire.b64(bytes(16))
                else:raise AssertionError('must not prepare or confirm an existing pairing')
                super().pair_emit(value)
        link=BLELink(client_factory=PairedPeer)
        try:
            with self.assertRaisesRegex(AirSIMError,'手机已有蓝牙配对'):link.begin('fixture')
            self.assertFalse(link.ready);self.assertIsNone(link.client)
        finally:link.close()

    def test_scan_reuses_saved_key_by_identity(self):
        client=self.pair();old_key=client.profile['ble_key'];self.link.disconnect()
        saved=client.profile.copy();peer=self.peer
        link=BLELink(client_factory=lambda *a,**k:peer,credentials_for=lambda agent:saved if agent==peer.identity else None)
        try:
            profile=link.begin('rotated-address')
            self.assertEqual(profile['ble_address'],'rotated-address')
            self.assertNotEqual(profile['ble_key'],old_key)
            self.assertTrue(link.ready)
        finally:link.close()

    def test_health_recovers_rotated_address_and_replaces_profile(self):
        client=self.pair();old=client.profile['ble_key'];self.store.save(client.profile)
        self.link.disconnect()
        class Scanner:
            @staticmethod
            async def discover(**kwargs):
                return {'rotated':(SimpleNamespace(address='11:22:33:44:55:66',name='fixture'),SimpleNamespace(local_name='fixture'))}
        self.link.scanner=Scanner
        self.assertTrue(client.health()['ok'])
        self.assertEqual(client.profile['ble_address'],'11:22:33:44:55:66')
        self.assertNotEqual(client.profile['ble_key'],old)
        self.assertEqual(len(self.store.profiles()),1)

    def test_lost_ready_recovers_journal_after_process_restart(self):
        client=self.pair();original=client.profile.copy();self.store.save(original)
        self.link.disconnect()
        emit=self.peer.pair_emit
        def drop_ready(value):
            if value.get('ready'):raise TimeoutError('simulated disconnect after phone saved key')
            emit(value)
        self.peer.pair_emit=drop_ready
        with self.assertRaises(AirSIMError):self.link.begin(original['ble_address'])
        self.assertIsNotNone(self.store.ble_pending(original['agent_id']))
        self.assertEqual(self.store.load()['ble_key'],original['ble_key'])
        self.peer.pair_emit=emit
        recovered=BLELink(original,self.store.save,client_factory=lambda *a,**k:self.peer,credential_store=self.store)
        try:
            profile=recovered.begin(original['ble_address'])
            self.assertTrue(recovered.ready)
            self.assertNotEqual(profile['ble_key'],original['ble_key'])
            self.assertEqual(self.store.load()['ble_key'],profile['ble_key'])
            self.assertIsNone(self.store.ble_pending(original['agent_id']))
        finally:recovered.close()

    def test_cleanup_failure_does_not_mask_pairing_rejection(self):
        class BrokenDisconnect(FakeGatt):
            def pair_emit(peer,value):
                if value.get('op')=='begin':value['address_recovery_proof']=wire.b64(bytes(16))
                super().pair_emit(value)
            async def disconnect(peer):
                peer.is_connected=False
                raise EOFError()
        link=BLELink(client_factory=BrokenDisconnect)
        try:
            with self.assertRaisesRegex(AirSIMError,'手机已有蓝牙配对'):link.begin('fixture')
        finally:link.close()

    def test_timeout_identifies_phase(self):
        self.link.stage='等待手机配对确认'
        with self.assertRaisesRegex(AirSIMError,'等待手机配对确认超时'):
            self.link.run(asyncio.sleep(1),.01)

    def test_connect_reuses_scanned_device_without_second_discovery(self):
        device=SimpleNamespace(address='fixture-address',name='fixture')
        class Scanner:
            @staticmethod
            async def discover(**kwargs):return {'fixture':(device,SimpleNamespace(local_name='fixture'))}
        targets=[]
        def factory(target,**kwargs):
            targets.append(target);return FakeGatt(target.address,**kwargs)
        link=BLELink(scanner=Scanner,client_factory=factory)
        try:
            found=link.scan();link.begin(found[0]['address'])
            self.assertIs(targets[0],device)
        finally:link.close()



if __name__=='__main__':unittest.main()
