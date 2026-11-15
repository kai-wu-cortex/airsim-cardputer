import tempfile
import threading
import time
import unittest
from airsim_cp.sms_feed import SMSFeed
from airsim_cp.protocol import AirSIMError,ProfileStore
from airsim_cp.app import App
from mock_phone import Phone


class EventPeer:
    def __init__(self):
        self.event=threading.Event();self.waiting=threading.Event();self.rows=[];self.requests=[];self.revision=0
    def health(self):return {'ok':True}
    def signed(self,method,path):
        self.requests.append((method,path))
        if path=='/api/sms':return list(self.rows),''
        self.waiting.set();self.event.wait(.1);self.event.clear()
        return {'revision':self.revision,'sms_pending':len(self.rows)},''


class SMSFeedTests(unittest.TestCase):
    def wait_for(self,predicate):
        end=time.monotonic()+2
        while time.monotonic()<end:
            if predicate():return
            time.sleep(.01)
        self.fail('Event inbox did not update')
    def test_receives_event_without_waiting_for_snapshot_and_never_acknowledges(self):
        peer=EventPeer();feed=SMSFeed(peer)
        try:
            self.assertTrue(peer.waiting.wait(1))
            peer.rows=[{'delivery_id':'reply','sender':'10000','content':'fixture reply'}]
            peer.revision=1;peer.event.set()
            self.wait_for(lambda:bool(feed.pending))
            peer.rows=[]  # Another client subsequently clears the shared queue.
            self.assertEqual(feed.drain()[0]['content'],'fixture reply')
            self.assertTrue(all(method=='GET' for method,_ in peer.requests))
            self.assertFalse(any('/ack' in path for _,path in peer.requests))
        finally:feed.close()
        peer.event.set();feed.thread.join(1);self.assertFalse(feed.thread.is_alive())
    def test_unsupported_events_stop_instead_of_hot_polling(self):
        peer=EventPeer();original=peer.signed
        def signed(method,path):
            if path.startswith('/api/events'):raise AirSIMError('unsupported',status=404)
            return original(method,path)
        peer.signed=signed;feed=SMSFeed(peer);feed.thread.join(1)
        self.assertFalse(feed.thread.is_alive());feed.close()
    def test_identity_failure_never_reads_sms(self):
        peer=EventPeer()
        def bad_health():raise AirSIMError('identity mismatch')
        peer.health=bad_health;feed=SMSFeed(peer)
        try:time.sleep(.05);self.assertEqual(peer.requests,[])
        finally:feed.close()
    def test_busy_composer_receives_persists_and_deduplicates_without_losing_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=App(ProfileStore(tmp),demo=True);app.demo=False
            try:
                app.client.profile=Phone().profile();app.messages=[]
                app.form('compose',['10000','draft']);app.busy=True;app.work_kind='send'
                incoming=[{'delivery_id':'new','sender':'10000','content':'reply','timestamp':'2026-11-15T06:06:51Z'}]
                app.client.pending_sms=lambda:incoming
                app.tick();app.tick()
                self.assertEqual(app.page,'compose');self.assertEqual(app.fields[1],'draft')
                self.assertEqual(app.unread_count(),1)
                self.assertEqual(app.store.load_messages(app.client.profile['agent_id']),incoming)
                self.assertEqual(len(app.messages),1)
            finally:app.close()
