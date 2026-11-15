"""Phone overview and local, per-phone read/unseen state."""
import hashlib
import time
from .protocol import AirSIMError

MODES={'lan':'Wi-Fi','ble':'蓝牙'}


class Dashboard:
    def init_dashboard(self):
        self.read_messages=set(self.store.load_data('read-messages.json',[]))
        self.seen_calls=set(self.store.load_data('seen-calls.json',[]))
        self.requested_mode=None
        self.requested_profile=None
    def agent_id(self):return (self.client.profile or {}).get('agent_id','')
    def unread_count(self):
        return sum(m.get('direction')!='outgoing' and self.message_hash(m) not in self.read_messages for m in self.messages)
    def unread_preview(self):
        return next((m for m in self.messages if m.get('direction')!='outgoing' and self.message_hash(m) not in self.read_messages),None)
    def missed_preview(self):
        return next((c for c in self.phone_history() if c.get('missed') is True and self.call_key_hash(c) not in self.seen_calls),None)
    def open_message(self):
        if not self.messages:return
        self.read_messages.add(self.message_hash(self.selected_message()))
        self.store.save_data('read-messages.json',sorted(self.read_messages))
        self.page='message';self.scroll=0
    def call_key_hash(self,call):
        return hashlib.sha256((str(call.get('source_agent_id',self.agent_id()))+'\0'+str(call.get('id',''))+'\0'+str(call.get('generation',0))).encode()).hexdigest()
    def phone_history(self):return [c for c in self.call_history if c.get('source_agent_id')==self.agent_id()]
    def missed_count(self):return sum(c.get('missed') is True and self.call_key_hash(c) not in self.seen_calls for c in self.phone_history())
    def mark_calls_seen(self):
        self.seen_calls.update(self.call_key_hash(c) for c in self.phone_history())
        self.store.save_data('seen-calls.json',sorted(self.seen_calls))
    def update_call_history(self):
        records=list(self.calls.get('history') or [])
        if self.active_call():records.append(self.active_call())
        history=list(self.call_history)
        for call in records:
            if not isinstance(call,dict) or not call.get('id'):continue
            saved={k:call[k] for k in ('id','generation','number','state','direction','missed') if k in call}
            saved['source_agent_id']=self.agent_id()
            identity=self.call_key_hash(saved)
            prior=next((c for c in history if self.call_key_hash(c)==identity),{})
            saved=dict(prior,**saved)
            if prior!=saved:
                history=[saved]+[c for c in history if self.call_key_hash(c)!=identity]
        history=history[:100]
        if history!=self.call_history:
            self.call_history=history;self.store.save_data('calls.json',history);self.dirty=True
    def phone_number(self):
        slots=self.status.get('sim_slots') or []
        chosen=(self.client.profile or {}).get('sim_account')
        slot=next((s for s in slots if s.get('id')==chosen),None) if chosen else None
        if slot is None:slot=next((s for s in slots if s.get('is_default_voice') or s.get('default')),None)
        number=(slot or {}).get('number') or self.status.get('phone_number') or self.status.get('android_phone_number')
        return str(number) if number else '未提供'
    def battery_text(self):
        battery=self.status.get('battery') or {}
        if not isinstance(battery,dict):return '电量 --'
        value=battery.get('percent');observed=battery.get('observed_at_ms')
        if not isinstance(value,(int,float)) or isinstance(value,bool) or not 0<=value<=100:return '电量 --'
        old=not self.online
        if isinstance(observed,(int,float)):old=old or time.time()*1000-observed>180000
        return ('上次 ' if old else ('充电 ' if battery.get('charging') else '电量 '))+str(round(value))+'%'
    def connection_label(self):return MODES.get((self.client.profile or {}).get('transport','lan'),'Wi-Fi') if self.client.profile else '未连接'
    def dashboard_hint(self):
        if self.active_call():return '通话中 · 按 4 返回通话'
        if not self.client.profile:return '按 7 选择连接模式'
        if not self.online:return '手机离线 · 显示本机记录'
        return self.home_hint or '电话待机 · 计数为本机未读记录'
    def mode_menu(self):
        profiles=self.store.profiles();current=(self.client.profile or {}).get('transport','lan') if self.client.profile else None
        self.menu('连接模式',[(name+' · '+('当前' if mode==current else ('已保存' if any(p.get('transport','lan')==mode for p in profiles) else '添加连接')),
              lambda mode=mode:self.choose_mode(mode)) for mode,name in MODES.items()])
    def choose_mode(self,mode):
        if mode not in MODES:raise AirSIMError('仅支持 Wi-Fi 和蓝牙连接')
        if self.active_call() or self.audio.active:raise AirSIMError('请先结束通话，再切换模式')
        self.requested_mode=mode
        self.tick_dashboard()
    def queue_mode_profile(self,profile):
        if self.active_call() or self.audio.active:raise AirSIMError('请先结束通话，再切换模式')
        self.requested_profile=profile
    def tick_dashboard(self):
        if self.requested_profile:
            if self.page!='menu' or not self.menu_title.startswith('选择'):
                self.requested_profile=None
            elif not self.busy:
                profile,self.requested_profile=self.requested_profile,None
                if not self.active_call() and not self.audio.active:self.activate(profile)
            return
        if not self.requested_mode:return
        if self.page!='menu' or self.menu_title!='连接模式':self.requested_mode=None;return
        if self.busy:return
        mode,self.requested_mode=self.requested_mode,None
        if self.active_call() or self.audio.active:return
        if self.client.profile and self.client.profile.get('transport','lan')==mode:
            self.page='home';return
        profiles=[p for p in self.store.profiles() if p.get('transport','lan')==mode]
        same=[p for p in profiles if p['agent_id']==self.agent_id()]
        if same:profiles=same
        if len(profiles)>1:
            self.menu('选择'+MODES[mode]+'设备',[(str(p.get('label') or p.get('host') or p['agent_id']),lambda p=p:self.queue_mode_profile(p)) for p in profiles]);return
        if profiles:self.activate(profiles[0])
        elif mode=='lan':self.scan_lan()
        elif mode=='ble':self.scan_ble()
