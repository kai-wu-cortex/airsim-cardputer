import socket
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from airsim_cp.discovery import PAIR, GATEWAY, LANDiscovery, endpoints, group_devices
from airsim_cp.app import App
from airsim_cp.protocol import ProfileStore
from airsim_cp.ui import View


def info(kind=PAIR, **properties):
    props={b'v':b'1',b'expires':str(int(time.time()*1000+120000)).encode(),b'pcm_port':b'9001'}
    props.update({k.encode():str(v).encode() for k,v in properties.items()})
    return SimpleNamespace(name='AirSIM-test.'+kind,port=9000,
                           addresses=[socket.inet_aton('192.168.1.20')],properties=props)


class DiscoveryTests(unittest.TestCase):
    def test_pair_endpoint_and_expiry(self):
        result=endpoints(info(),PAIR)[0]
        self.assertEqual((result['host'],result['port'],result['kind']),('192.168.1.20',9000,'pair'))
        self.assertEqual(endpoints(info(expires=1),PAIR),[])
    def test_gateway_and_untrusted_input(self):
        result=endpoints(info(GATEWAY,agent_id='a',device_name='Redmi'),GATEWAY)[0]
        self.assertEqual((result['name'],result['agent_id'],result['pcm_port']),('Redmi','a',9001))
        self.assertEqual(endpoints(info(v=2),PAIR),[])
        self.assertEqual(endpoints(info(GATEWAY,pcm_port=80),GATEWAY),[])
        value=info();value.addresses=[socket.inet_aton('8.8.8.8')]
        self.assertEqual(endpoints(value,PAIR),[])
    def test_cancelled_scan_does_not_publish_late_service(self):
        with patch.object(LANDiscovery,'run'):
            scan=LANDiscovery();scan.thread.join();scan.close()
            scan.add_service(SimpleNamespace(get_service_info=lambda *a,**k:info()),PAIR,'old')
            self.assertEqual(scan.snapshot()[0],[])


class DiscoveryUXTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=App(ProfileStore(self.temp.name),demo=True)
    def tearDown(self):self.app.close();self.temp.cleanup()
    def test_default_discovery_manual_and_back(self):
        self.app.key('8');self.app.key('5');self.assertEqual(self.app.page,'lan')
        self.assertEqual(View().render(self.app).size,(320,170))
        self.app.key('8');self.assertEqual(self.app.page,'pair')
        self.app.key('4');self.assertEqual(self.app.fields[0],'4')
        self.app.key('ESC');self.assertEqual(self.app.page,'lan')
        self.app.key('ESC');self.assertEqual(self.app.page,'settings')
    def test_select_fills_endpoint_and_focuses_code_without_pairing(self):
        self.app.scan_lan();self.app.lan_devices=endpoints(info(),PAIR)
        self.app.key('6')
        self.assertEqual(self.app.fields,['192.168.1.20','9000',''])
        self.assertEqual(self.app.field,2)
        for c in '123456':self.app.key(c)
        self.app.key('ENTER');self.assertEqual(self.app.page,'confirm')
        self.assertEqual(self.app.confirm_title,'Wi-Fi 配对')
        self.assertIsNone(self.app.client.profile)
    def test_scan_can_be_cancelled_for_manual_while_running(self):
        self.app.demo=False
        fake=SimpleNamespace(close=lambda:None,snapshot=lambda:([],False,''))
        with patch('airsim_cp.features.LANDiscovery',return_value=fake):
            self.app.scan_lan();self.app.key('8');self.app.tick()
        self.assertEqual(self.app.page,'pair');self.assertIsNone(self.app.lan_scan)
    def test_unpaired_gateway_does_not_use_bluetooth_secret(self):
        self.app.scan_lan();self.app.lan_devices=endpoints(info(GATEWAY,agent_id='a'),GATEWAY)
        self.app.key('ENTER');self.assertEqual(self.app.page,'notice')
        self.assertEqual(self.app.notice_title,'Wi-Fi 配对')
        self.app.key('8');self.assertEqual(self.app.page,'lan')
        self.assertIsNone(self.app.client.profile)
    def test_late_connect_result_cannot_override_manual_form(self):
        self.app.manual_lan()
        self.assertTrue(self.app.feature_result('lan_connected',{}))
        self.assertEqual(self.app.page,'pair');self.assertIsNone(self.app.client.profile)
    def test_saved_gateway_requires_authenticated_health(self):
        from mock_phone import Phone
        phone=Phone().start()
        try:
            profile=phone.profile();self.app.store.save(profile);self.app.demo=False
            self.app.page='lan';self.app.lan_devices=[dict(kind='gateway',host='127.0.0.1',port=phone.port,
                pcm_port=profile['pcm_port'],agent_id=profile['agent_id'])]
            self.app.choose_lan();self.app.future.result(timeout=3)
            self.app.tick()
            self.assertEqual(self.app.client.profile['agent_id'],profile['agent_id'])
            self.assertTrue(self.app.online)
        finally:
            if self.app.future:self.app.future.result(timeout=3)
            phone.close()
    def test_bad_identity_never_activates_discovered_gateway(self):
        from mock_phone import Phone
        phone=Phone().start()
        try:
            profile=phone.profile();profile['agent_id']='wrong-agent'
            self.app.store.save(profile);self.app.demo=False
            self.app.page='lan';self.app.lan_devices=[dict(kind='gateway',host='127.0.0.1',port=phone.port,
                pcm_port=profile['pcm_port'],agent_id='wrong-agent')]
            self.app.choose_lan()
            with self.assertRaises(Exception):self.app.future.result(timeout=3)
            self.app.tick();self.assertIsNone(self.app.client.profile)
        finally:phone.close()

    def test_gateway_and_pairing_are_one_selectable_phone(self):
        rows=endpoints(info(GATEWAY,agent_id='a',device_name='Redmi'),GATEWAY)+endpoints(info(),PAIR)
        rows[1]['port']=45678
        self.app.scan_lan();self.app.lan_devices=group_devices(rows)
        self.assertEqual(len(self.app.lan_devices),1)
        self.assertEqual(self.app.lan_devices[0]['name'],'Redmi')
        self.app.busy=True;self.app.work_kind='refresh'
        self.app.key('6')
        self.assertEqual(self.app.page,'pair')
        self.assertEqual(self.app.fields,['192.168.1.20','45678',''])
        self.assertEqual(self.app.field,2)
        self.assertIsNone(self.app.client.profile)
    def test_pairing_on_other_phone_is_not_joined(self):
        gateway=endpoints(info(GATEWAY,agent_id='a'),GATEWAY)[0]
        pairing=endpoints(info(),PAIR)[0];pairing['host']='192.168.1.99'
        grouped=group_devices([gateway,pairing])
        self.assertEqual(len(grouped),2)
        self.assertNotIn('pairing',next(d for d in grouped if d['kind']=='gateway'))
    def test_ambiguous_pairing_sessions_remain_selectable(self):
        gateway=endpoints(info(GATEWAY,agent_id='a'),GATEWAY)[0]
        pairing=endpoints(info(),PAIR)[0]
        grouped=group_devices([gateway,pairing,dict(pairing,port=45679)])
        self.assertEqual(len(grouped),3)
        self.assertNotIn('pairing',next(d for d in grouped if d['kind']=='gateway'))
    def test_expired_joined_pairing_does_not_open_code(self):
        gateway=endpoints(info(GATEWAY,agent_id='a'),GATEWAY)[0]
        self.app.scan_lan();self.app.lan_devices=[dict(gateway,pairing=dict(endpoints(info(),PAIR)[0],expires=1))]
        self.app.key('6');self.assertEqual(self.app.page,'notice')
