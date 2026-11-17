"""Call UX regression tests. All phone actions and audio are in-memory fakes."""
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from airsim_cp.app import App
from airsim_cp.protocol import ProfileStore,AirSIMError
from airsim_cp.ui import View


class Phone:
    def __init__(self):
        self.profile={'transport':'ble','agent_id':'ux-fixture'}
        self.link=SimpleNamespace(ready=True,lock=threading.RLock(),active=None)
        self.actions=[]
    def incoming(self,identity='incoming-1'):
        self.link.active={'id':identity,'generation':1,'number':'10000','state':'incoming','direction':'incoming'}
    def control(self,action,identity):
        assert self.link.active and self.link.active['id']==identity
        self.actions.append(action)
        if action=='answer':self.link.active=dict(self.link.active,state='active')
        elif action in ('reject','hangup'):self.link.active=None
    def dial(self,number):
        self.actions.append('dial')
        self.link.active={'id':'outgoing-1','generation':1,'number':number,'state':'active','direction':'outgoing'}
        return {'android_id':'outgoing-1'}
    def snapshot(self):return {'calls':{'active':self.link.active},'sms':[],'status':{'device_name':'测试手机'}}
    def close(self):pass


class Audio:
    def __init__(self):
        self.active=False;self.error='';self.starts=[];self.stops=0;self.fail=False
        self.engine=SimpleNamespace(muted=False)
    def start(self,client,identity,*,early=False):
        self.engine.early=early
        self.starts.append(identity)
        if self.fail:raise AirSIMError('测试音频失败')
        self.active=True
    def stop(self):self.active=False;self.stops+=1
    def stop_async(self):self.stop()
    def close(self):self.stop()


class CallUXTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.phone=Phone()
        self.app=App(ProfileStore(self.temp.name),client=self.phone)
        self.app.audio.close();self.app.audio=Audio();self.app.next_poll=float('inf')
    def tearDown(self):self.app.close();self.temp.cleanup()
    def settle(self,predicate):
        end=time.monotonic()+2
        while time.monotonic()<end:
            self.app.tick()
            if predicate():return
            time.sleep(.005)
        self.fail(f'flow did not settle: {self.app.page} {self.phone.actions}')
    def ring(self):self.phone.incoming();self.app.tick()
    def test_home_has_three_primary_entries(self):
        self.app.key('4');self.assertEqual(self.app.page,'dial')
        self.app.key('ESC');self.app.key('6');self.assertEqual(self.app.page,'messages')
        self.app.key('ESC');self.app.key('8');self.assertEqual(self.app.page,'settings')
    def test_incoming_opens_immediately_without_auto_answer(self):
        self.app.page='settings';self.ring()
        self.assertEqual(self.app.page,'calls');self.assertEqual(self.phone.actions,[])
        self.assertEqual(self.app.audio.starts,[])
    def test_one_press_answer_starts_audio_once(self):
        self.ring();self.app.key('4');self.app.key('4')
        self.settle(lambda:self.app.audio.active and not self.app.busy)
        self.assertEqual(self.phone.actions,['answer'])
        self.assertEqual(self.app.audio.starts,['incoming-1']);self.assertEqual(self.app.page,'calls')
        for _ in range(5):self.app.tick()
        self.assertEqual(len(self.app.audio.starts),1)
    def test_answer_not_lost_during_refresh(self):
        self.ring();future=Future();self.app.future=future;self.app.busy=True;self.app.work_kind='refresh'
        self.app.key('ENTER');self.app.key('4');self.app.tick()
        self.settle(lambda:self.phone.actions==['answer'])
        self.assertFalse(future.done())
        future.set_result(self.phone.snapshot())
        self.settle(lambda:self.app.audio.active)
        self.assertEqual(self.phone.actions,['answer'])
    def test_reject_is_one_press_without_audio(self):
        self.ring();self.app.key('8')
        self.settle(lambda:self.phone.actions==['reject'] and self.app.page=='home')
        self.assertEqual(self.app.audio.starts,[]);self.assertEqual(self.app.page,'home')
    def test_hangup_is_one_press_and_releases_audio(self):
        self.ring();self.app.key('4');self.settle(lambda:self.app.audio.active and not self.app.busy)
        self.app.key('8');self.settle(lambda:self.phone.link.active is None and self.app.page=='home')
        self.assertEqual(self.phone.actions,['answer','hangup'])
        self.assertFalse(self.app.audio.active);self.assertEqual(self.app.page,'home')
    def test_sms_draft_survives_incoming_and_remote_end(self):
        self.app.form('compose',['10000','unfinished']);self.app.field=1;self.ring()
        self.app.key('4');self.settle(lambda:self.app.audio.active and not self.app.busy)
        self.phone.link.active=None;self.app.tick()
        self.assertEqual(self.app.page,'compose');self.assertEqual(self.app.fields,['10000','unfinished'])
        self.assertEqual(self.app.field,1);self.assertFalse(self.app.audio.active)
    def test_local_phone_answer_does_not_take_over_audio(self):
        self.ring();self.phone.link.active=dict(self.phone.link.active,state='active');self.app.tick()
        self.assertEqual(self.app.audio.starts,[])
    def test_outgoing_audio_starts_after_dial_connects(self):
        self.app.form('dial',['10000']);self.app.key('ENTER');self.settle(lambda:self.app.audio.active and not self.app.busy)
        self.assertEqual(self.phone.actions,['dial']);self.assertEqual(self.app.audio.starts,['outgoing-1'])
    def test_incoming_preserves_sms_under_review(self):
        self.app.form('compose',['10000','draft under review']);self.app.field=1;self.app.key('ENTER')
        self.assertEqual(self.app.page,'confirm');self.ring();self.app.key('8')
        self.settle(lambda:self.app.page=='compose' and self.phone.link.active is None)
        self.assertEqual(self.app.page,'compose');self.assertEqual(self.app.fields[1],'draft under review')
    def test_failed_audio_stays_inline_and_can_retry(self):
        self.app.audio.fail=True;self.ring();self.app.key('4')
        self.settle(lambda:len(self.app.audio.starts)==1 and not self.app.busy)
        self.assertEqual(self.app.page,'calls');self.assertIn('失败',self.app.call_hint)
        self.app.audio.fail=False;self.app.key('6');self.settle(lambda:self.app.audio.active)
        self.assertEqual(len(self.app.audio.starts),2)
    def test_queued_answer_cannot_target_replacement_call(self):
        self.ring();self.app.busy=True;self.app.work_kind='refresh';self.app.key('4')
        self.phone.incoming('replacement');self.app.busy=False;self.app.tick()
        self.assertEqual(self.phone.actions,[])
    def test_hangup_bypasses_blocked_refresh_and_stale_result(self):
        self.ring();self.app.key('4');self.settle(lambda:self.app.audio.active and not self.app.busy)
        stale=self.phone.snapshot();future=Future()
        self.app.future=future;self.app.busy=True;self.app.work_kind='refresh'
        self.app.key('8');self.assertFalse(self.app.audio.active)
        self.settle(lambda:self.phone.link.active is None)
        self.assertFalse(future.done());future.set_result(stale)
        self.settle(lambda:not self.app.busy)
        self.assertFalse(self.app.active_call());self.assertEqual(self.phone.actions,['answer','hangup'])
    def test_hangup_does_not_restart_pending_audio(self):
        self.ring();self.phone.link.active['state']='active';self.app.tick()
        future=Future();self.app.future=future;self.app.busy=True;self.app.work_kind='audio'
        self.app.audio_intent=self.app.call_token();self.app.key('8')
        self.settle(lambda:self.phone.link.active is None)
        future.set_result(None);self.settle(lambda:not self.app.busy)
        self.assertFalse(self.app.audio.active);self.assertEqual(self.app.audio.starts,[])
    def test_mu300_early_media_has_no_timer_and_transitions_once(self):
        self.app.status={'agent_kind':'mu300'}
        self.phone.incoming('outgoing-early')
        self.phone.link.active.update(state='dialing',direction='outgoing')
        self.app.outgoing_audio_until=time.monotonic()+180
        self.settle(lambda:self.app.audio.active and not self.app.busy)
        self.assertTrue(self.app.audio.engine.early)
        self.assertEqual(self.app.call_started,0)
        self.phone.link.active['state']='active'
        self.settle(lambda:len(self.app.audio.starts)==2 and not self.app.busy)
        self.assertFalse(self.app.audio.engine.early)
        self.assertGreater(self.app.call_started,0)
        for _ in range(5):self.app.tick()
        self.assertEqual(len(self.app.audio.starts),2)

    def test_mu300_early_audio_does_not_take_over_external_dial(self):
        self.app.status={'agent_kind':'mu300'}
        self.phone.incoming('external-outgoing')
        self.phone.link.active.update(state='dialing',direction='outgoing')
        self.app.tick()
        self.assertEqual(self.app.audio.starts,[])

    def test_hangup_during_early_media_does_not_open_duplex(self):
        self.app.status={'agent_kind':'mu300'}
        self.phone.incoming('outgoing-early')
        self.phone.link.active.update(state='dialing',direction='outgoing')
        self.app.outgoing_audio_until=time.monotonic()+180
        self.settle(lambda:self.app.audio.active and not self.app.busy)
        self.app.key('8')
        self.settle(lambda:self.phone.link.active is None and not self.app.busy)
        self.assertEqual(len(self.app.audio.starts),1)
        self.assertFalse(self.app.audio.active)

    def test_all_call_screens_render_at_native_size(self):
        for state in ('incoming','dialing','active'):
            self.phone.incoming();self.phone.link.active['state']=state;self.app.tick();self.app.page='calls'
            self.assertEqual(View().render(self.app).size,(320,170))


if __name__=='__main__':unittest.main()
