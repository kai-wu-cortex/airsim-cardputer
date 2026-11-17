"""Foreground call flow: direct controls, incoming priority and automatic audio."""
import time
import threading
from concurrent.futures import Future
from .protocol import AirSIMError
from .early_audio import EARLY_STATES

INCOMING={'incoming','ringing'}
CONNECTED={'active','connected','answered'}


class CallUI:
    def init_call_ui(self):
        self.call_hint='';self.home_hint='';self.home_hint_until=0
        self.queued_call=None;self.call_seen=None;self.call_minimized=False
        self.call_draft=None;self.call_started=0;self.call_second=-1
        self.audio_intent=None;self.audio_attempted=None;self.outgoing_audio_until=0
        self.call_future=None;self.call_action='';self.call_target=None

    def call_token(self,call=None):
        call=self.active_call() if call is None else call
        return (call.get('id'),call.get('generation',0)) if call and call.get('id') else None

    def dial_from_ui(self,number):
        self.outgoing_audio_until=time.monotonic()+180
        self.call_hint='正在拨号…';self.page='calls'
        self.submit(lambda:self.client.dial(number),'dial')

    def request_call_action(self,action):
        call=self.active_call()
        if not call:return
        if self.demo or self.queued_call or self.call_future:return
        self.queued_call=(action,self.call_token(call))
        self.call_hint={'answer':'正在接听…','reject':'正在拒接…','hangup':'正在挂断…'}[action]
        if action=='answer':self.audio_intent=self.call_token(call)
        else:
            self.audio_intent=None;self.outgoing_audio_until=0;self.audio_attempted=self.call_token(call)
            self.audio.stop_async()
        self.page='calls';self.call_minimized=False;self.dirty=True

    def call_key(self,key):
        if self.page!='calls':return False
        call=self.active_call();state=call.get('state','')
        if state in INCOMING:
            if key in ('4','ENTER'):self.request_call_action('answer')
            elif key=='8':self.request_call_action('reject')
        elif call:
            if key=='8':self.request_call_action('hangup')
            elif state in CONNECTED and key=='4' and self.audio.active:
                self.audio.engine.muted=not self.audio.engine.muted
            elif state in CONNECTED and key=='6':
                if self.audio.active:self.form('dtmf',[''])
                elif not self.busy:
                    self.audio_intent=self.call_token(call);self.audio_attempted=None;self.call_hint=''
        elif key in ('4','7','ENTER'):self.form('dial',[''])
        elif key=='8':self.page='home'
        return True

    def tick_call_ui(self):
        call=self.active_call();token=self.call_token(call);state=call.get('state','')
        now=time.monotonic()
        if self.call_future and self.call_future.done():
            future,self.call_future=self.call_future,None
            try:
                future.result()
                if token==self.call_target:self.call_hint=''
            except Exception as error:
                if token==self.call_target:
                    self.call_hint=str(error) if isinstance(error,AirSIMError) else '通话操作失败，请核实手机状态'
            self.dirty=True
        if token!=self.call_seen:
            previous=self.call_seen;self.call_seen=token;self.call_started=0;self.call_second=-1
            self.call_minimized=False;self.call_hint='';self.audio_attempted=None
            if token and state in INCOMING:
                if self.page in ('compose','dial','contact','pair'):
                    self.call_draft=(self.page,list(self.fields),self.field)
                elif self.page=='confirm' and self.return_page in ('compose','dial','contact','pair'):
                    self.call_draft=(self.return_page,list(self.fields),self.field)
                self.pending=None;self.page='calls'
            elif not token and previous:
                self.queued_call=None;self.audio_intent=None;self.outgoing_audio_until=0
                self.home_hint='通话已结束';self.home_hint_until=now+4
                if self.page=='calls':
                    if self.call_draft:self.page,self.fields,self.field=self.call_draft
                    else:self.page='home'
                self.call_draft=None
            self.dirty=True
        if token and state in INCOMING and not self.call_minimized:self.page='calls'
        if (token and state in EARLY_STATES and call.get('direction')=='outgoing'
                and self.outgoing_audio_until>now and self.status.get('agent_kind')=='mu300'):
            self.audio_intent=token
            if (self.audio_attempted!=token and not self.busy and not self.call_future
                    and not self.audio.active and not self.queued_call):
                self.audio_attempted=token
                self.start_audio(call['id'],early=True)
        if token and state in CONNECTED:
            if getattr(self.audio.engine,'early',False):
                self.audio.engine.early=False
                self.audio.stop_async()
                self.audio_attempted=None
            if not self.call_started:self.call_started=now
            second=int(now-self.call_started)
            if second!=self.call_second:self.call_second=second;self.dirty=True
            if self.outgoing_audio_until>now and call.get('direction')=='outgoing':
                self.audio_intent=token;self.outgoing_audio_until=0
        if self.queued_call and not self.call_future:
            action,target=self.queued_call;self.queued_call=None
            if token!=target:
                self.call_hint='通话状态已变化';return
            identity=call['id']
            # Answer/end must not wait behind a slow status, SMS or media request.
            future=Future();self.call_future=future;self.call_target=target;self.call_action=action
            def control():
                try:future.set_result(self.client.control(action,identity))
                except Exception as error:future.set_exception(error)
            threading.Thread(target=control,name='airsim-call-control',daemon=True).start()
            return
        if (token and state in CONNECTED and token==self.audio_intent
                and self.audio_attempted!=token and not self.busy and not self.call_future and not self.audio.active):
            self.audio_attempted=token;self.call_hint='正在接通声音…'
            self.start_audio(call['id'])
        if self.home_hint and now>=self.home_hint_until:self.home_hint='';self.dirty=True

    def call_timer(self):
        seconds=max(0,self.call_second)
        return f'{seconds//60:02d}:{seconds%60:02d}'

    def caller_name(self):
        number=str(self.active_call().get('number') or '')
        return next((c['name'] for c in self.contacts if c.get('number')==number and number),'')
