import tempfile
import time
import unittest
from pathlib import Path
from airsim_cp.app import App
from airsim_cp.protocol import ProfileStore
from airsim_cp.ui import View


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=App(ProfileStore(self.tmp.name),demo=True)
        self.app.client.profile={'agent_id':'phone-a','transport':'lan'}
        self.app.messages=[]
    def tearDown(self):self.app.close();self.tmp.cleanup()
    def test_phone_number_and_battery_unknown_and_selected_sim(self):
        self.assertEqual(self.app.phone_number(),'未提供');self.assertEqual(self.app.battery_text(),'电量 --')
        self.app.status={'phone_number':'10000','sim_slots':[{'id':'s2','number':'10001'}],
                         'battery':{'percent':67,'charging':True,'observed_at_ms':time.time()*1000}}
        self.app.client.profile['sim_account']='s2'
        self.assertEqual(self.app.phone_number(),'10001');self.assertEqual(self.app.battery_text(),'充电 67%')
        self.app.online=False;self.assertEqual(self.app.battery_text(),'上次 67%')
        self.app.status['battery']['percent']=-1;self.assertEqual(self.app.battery_text(),'电量 --')
    def test_local_unread_is_persistent_scoped_and_excludes_sent(self):
        self.app.messages=[{'delivery_id':'a','content':'hello'},{'delivery_id':'b','direction':'outgoing'}]
        self.assertEqual(self.app.unread_count(),1)
        self.app.open_message();self.assertEqual(self.app.unread_count(),0)
        other=App(ProfileStore(self.tmp.name),demo=True)
        try:
            other.client.profile={'agent_id':'phone-a'};other.messages=list(self.app.messages)
            self.assertEqual(other.unread_count(),0)
            other.client.profile={'agent_id':'phone-b'};self.assertEqual(other.unread_count(),1)
        finally:other.close()
    def test_missed_records_are_per_phone_and_seen_on_open(self):
        self.app.calls={'history':[{'id':'c1','state':'ended','number':'10000','missed':True}]}
        self.app.record_history();self.app.record_history();self.assertEqual(self.app.missed_count(),1)
        self.app.history_menu();self.assertEqual(self.app.missed_count(),0)
        self.assertEqual(len(self.app.phone_history()),1)
        self.app.client.profile={'agent_id':'other'};self.assertEqual(self.app.phone_history(),[])
    def test_home_shortcuts_and_local_modes(self):
        self.app.page='home';self.app.key('7');self.assertEqual(self.app.menu_title,'连接模式')
        self.assertEqual(len(self.app.menu_items),2)
        self.app.page='home';self.assertEqual(View().render(self.app).size,(320,170))
    def test_mode_change_waits_for_refresh_and_cancel_discards(self):
        self.app.mode_menu();self.app.busy=True;self.app.choose_mode('lan')
        self.assertEqual(self.app.page,'menu');self.assertEqual(self.app.requested_mode,'lan')
        self.app.key('ESC');self.app.busy=False;self.app.tick_dashboard()
        self.assertEqual(self.app.page,'home');self.assertIsNone(self.app.requested_mode)
    def test_new_mode_opens_setup_and_active_call_blocks_switch(self):
        self.app.client.profile=None
        self.app.mode_menu();self.app.choose_mode('lan');self.assertEqual(self.app.page,'lan')
        self.app.calls={'active':{'id':'call','state':'active'}}
        with self.assertRaises(Exception):self.app.choose_mode('ble')
