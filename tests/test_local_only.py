"""Upgrade old cloud selections safely; keep authenticated local pairing usable."""
import tempfile
import time
import unittest
from airsim_cp.app import App
from airsim_cp.protocol import AirSIMError, ProfileStore, validate_profile
from mock_phone import Phone


class LocalOnlyTests(unittest.TestCase):
    def test_old_cloud_selection_falls_back_to_saved_wifi_without_losing_records(self):
        phone=Phone()
        with tempfile.TemporaryDirectory() as directory:
            store=ProfileStore(directory);local=phone.profile()
            legacy={'transport':'cloud','agent_id':'old-account'}
            store.save_data('profile.json',legacy)
            store.save_data('profiles.json',[local,legacy])
            store.save_messages(local['agent_id'],[{'content':'fixture retained'}])
            self.assertEqual(store.load(),local)
            self.assertEqual(store.profiles(),[local])
            self.assertEqual(store.load_messages(local['agent_id'])[0]['content'],'fixture retained')
            with self.assertRaises(ValueError):validate_profile(legacy)
            store.save(local)
            self.assertEqual(store.load_data('profiles.json',[]),[local])
    def test_cloud_only_install_starts_unpaired(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ProfileStore(directory)
            store.save_data('profile.json',{'transport':'cloud','agent_id':'old'})
            app=App(store,demo=True)
            try:
                self.assertIsNone(app.client.profile)
                app.mode_menu();self.assertEqual(len(app.menu_items),2)
                with self.assertRaises(AirSIMError):app.choose_mode('cloud')
            finally:app.close()
    def test_local_pair_requires_confirmation_then_completes_without_relay_config(self):
        phone=Phone().start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                app=App(ProfileStore(directory))
                try:
                    app.form('pair',['127.0.0.1',str(phone.port),'123456'])
                    app.field=2;app.key('ENTER')
                    self.assertEqual(app.page,'confirm');self.assertEqual(phone.requests,[])
                    app.key('4');self.assertEqual(phone.requests,[])
                    app.key('ENTER');app.key('8')
                    deadline=time.monotonic()+5
                    while app.busy and time.monotonic()<deadline:app.tick();time.sleep(.02)
                    self.assertTrue(app.online)
                    self.assertTrue(phone.last_registration['local_mode'])
                    self.assertFalse(phone.last_registration['cloud_enabled'])
                    self.assertEqual(app.store.load()['agent_id'],phone.agent)
                    self.assertEqual(len(app.store.profiles()),1)
                finally:app.close()
        finally:phone.close()
