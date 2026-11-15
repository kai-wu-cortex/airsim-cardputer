"""Connection management and client features shared by all supported transports."""
import hashlib
import json
import time
from pathlib import Path
from .protocol import AirSIMError, Client, validate_profile
from .ble import BLEClient, BLELink
from .discovery import LANDiscovery

MODE_NAMES={'lan':'Wi-Fi 直连','ble':'蓝牙 VoBLE'}


def make_client(profile,store):
    mode=(profile or {}).get('transport','lan')
    if mode=='ble':return BLEClient(profile,store)
    if mode!='lan':raise AirSIMError('仅支持 Wi-Fi 和蓝牙连接')
    return Client(profile)


class Features:
    def init_features(self):
        self.menu_title='';self.menu_items=[];self.menu_index=0;self.ble_setup=None
        self.contacts=self.store.load_data('contacts.json',[])
        self.call_history=self.store.load_data('calls.json',[])
        self.deleted=set(self.store.load_data('deleted-messages.json',[]))
        self.last_call=None
        self.lan_scan=None;self.lan_devices=[];self.lan_index=0;self.lan_done=False;self.lan_error=''
    def mode_name(self):return MODE_NAMES.get((self.client.profile or {}).get('transport','lan'),'连接')
    def menu(self,title,items):
        self.menu_title=title;self.menu_items=items;self.menu_index=0;self.page='menu';self.dirty=True
    def menu_key(self,key):
        if self.page!='menu':return False
        if key in ('5','UP','LEFT'):self.menu_index=max(0,self.menu_index-1)
        elif key in ('6','DOWN','RIGHT'):self.menu_index=min(max(0,len(self.menu_items)-1),self.menu_index+1)
        elif key in ('7','ENTER') and self.menu_items:self.menu_items[self.menu_index][1]()
        elif key in ('4','8'):self.page='settings'
        return True
    def activate(self,profile,link=None):
        if self.active_call() or self.audio.active:raise AirSIMError('请先结束当前通话，再切换连接')
        validate_profile(profile)
        old=self.client
        self.store.save(profile)
        self.client=BLEClient(profile,self.store,link) if link else make_client(profile,self.store)
        if old is not self.client and hasattr(old,'close'):old.close()
        self.messages=self.store.load_messages(profile['agent_id']);self.calls={};self.status={};self.online=False
        self.page='home';self.next_poll=0
    def connection_menu(self):
        profiles=self.store.profiles()
        items=[(MODE_NAMES.get(p.get('transport','lan'),'连接')+' · '+str(p.get('label') or p.get('host') or p.get('agent_id')),
                lambda p=p:self.activate(p)) for p in profiles]
        items += [('导入配置文件',self.import_profile)]
        self.menu('已保存连接',items)
    def import_profile(self):
        path=self.store.directory/'import.json'
        if not path.exists():
            self.notice('导入现有凭据','将现有配置写入 ~/.config/airsim-cardputer/import.json 后再选择导入。支持 lan、ble。不会注册或修改手机。','settings');return
        if path.stat().st_size>65536:raise AirSIMError('导入文件过大')
        if path.stat().st_mode & 0o077:raise AirSIMError('凭据文件必须设置为 0600 权限')
        profile=json.loads(path.read_text());validate_profile(profile)
        self.confirm('导入连接','仅保存到 Cardputer，不写入手机或 Worker。',lambda:self.activate(profile),'settings')
    def stop_lan_scan(self):
        if self.lan_scan:self.lan_scan.close();self.lan_scan=None
    def scan_lan(self):
        if self.active_call() or self.audio.active:raise AirSIMError('请先结束通话')
        self.stop_lan_scan()
        self.page='lan';self.lan_devices=[];self.lan_index=0;self.lan_done=False;self.lan_error=''
        if not self.demo:self.lan_scan=LANDiscovery()
        else:self.lan_done=True
        self.dirty=True
    def tick_lan(self):
        if self.page!='lan':
            self.stop_lan_scan();return
        if self.lan_scan:
            rows,done,error=self.lan_scan.snapshot()
            if (rows,done,error)!=(self.lan_devices,self.lan_done,self.lan_error):
                selected=self.lan_devices[self.lan_index] if self.lan_devices else None
                self.lan_devices=rows;self.lan_done=done;self.lan_error=error
                self.lan_index=rows.index(selected) if selected in rows else 0
                self.dirty=True
    def manual_lan(self):
        self.stop_lan_scan()
        self.form('pair',[(self.client.profile or {}).get('host',''),'',''])
    def choose_lan(self):
        if not self.lan_devices:return
        device=self.lan_devices[self.lan_index]
        if device['kind']=='pair':
            if device['expires']<=time.time()*1000:
                self.scan_lan();return
            self.stop_lan_scan()
            self.form('pair',[device['host'],str(device['port']),'']);self.field=2
            return
        saved=next((p for p in reversed(self.store.profiles())
                    if p.get('transport','lan')=='lan' and p['agent_id']==device['agent_id']),None)
        if not saved:
            pairing=device.get('pairing')
            if pairing and pairing['expires']>time.time()*1000:
                self.stop_lan_scan()
                self.form('pair',[pairing['host'],str(pairing['port']),'']);self.field=2
                return
            self.notice('Wi-Fi 配对','在手机 AirSIM 打开「一次性配对」\n然后按 8 重新扫描。','lan')
            self.notice_action=('重新扫描',self.scan_lan);return
        candidate=dict(saved,host=device['host'],control_port=device['port'],pcm_port=device['pcm_port'])
        def verify():
            client=Client(candidate)
            try:client.health();return candidate
            finally:client.close()
        self.submit(verify,'lan_connected')
    def lan_key(self,key):
        if self.page!='lan':return False
        if key=='4':self.scan_lan()
        elif key=='8':self.manual_lan()
        elif key in ('UP','LEFT','5'):self.lan_index=max(0,self.lan_index-1)
        elif key in ('DOWN','RIGHT'):self.lan_index=min(max(0,len(self.lan_devices)-1),self.lan_index+1)
        elif key in ('6','7','ENTER'):self.choose_lan()
        return True
    def scan_ble(self):
        if self.active_call():raise AirSIMError('请先结束通话')
        if self.ble_setup:self.ble_setup.close()
        self.ble_setup=BLELink(on_credentials=self.store.save,credentials_for=self.ble_credentials,credential_store=self.store)
        self.submit(self.ble_setup.scan,'ble_scan')
    def ble_credentials(self,agent):
        return next((p for p in reversed(self.store.profiles()) if p.get('transport')=='ble' and p.get('agent_id')==agent),None)
    def begin_ble(self,device):
        if self.ble_setup is None:self.ble_setup=BLELink(on_credentials=self.store.save,credentials_for=self.ble_credentials,credential_store=self.store)
        self.submit(lambda:self.ble_setup.begin(device['address']),'ble_begin')
    def feature_result(self,kind,result):
        if kind=='lan_connected':
            if self.page!='lan':return True
            self.activate(result);self.online=True
        elif kind=='route_selected':
            if self.active_call() or self.audio.active:
                result.close();raise AirSIMError('当前有通话，连接不切换')
            old=self.client;self.client=result;old.close()
            self.store.save(result.profile);self.messages=self.store.load_messages(result.profile['agent_id'])
            self.calls={};self.status={};self.page='home';self.online=True
        elif kind=='ble_scan':
            if not result:self.notice('未发现 AirSIM','请在安卓打开 AirSIM VoBLE，再扫描。未改变现有手机设置。','settings')
            else:self.menu('选择蓝牙手机',[(str(d['name']),lambda d=d:self.begin_ble(d)) for d in result])
        elif kind=='ble_begin':
            if 'sas' in result:
                self.confirm('核对蓝牙码 '+result['sas'],'请核对手机上显示的六位码完全一致，再按 8 确认。不同则按 4 取消。',lambda:self.submit(self.ble_setup.confirm,'ble_confirm'),'settings')
            else:self.activate(result,self.ble_setup);self.ble_setup=None
        elif kind=='ble_confirm':
            self.activate(result,self.ble_setup);self.ble_setup=None
        elif kind=='dtmf':self.notice('按键已发送','DTMF 已提交给当前通话。','calls')
        else:return False
        return True
    def more_menu(self):
        self.menu('更多功能',[
            ('已保存连接 / 导入',self.connection_menu),('SIM 卡选择',self.sim_menu),
            ('通讯录',self.contacts_menu),('通话记录',self.history_menu),
            ('通话按键 DTMF',lambda:self.form('dtmf',[''])),
            ('切换麦克风静音',self.toggle_mute),
            ('自动选择同一手机连接',self.auto_menu),
            ('删除当前短信',self.delete_message),('连接能力说明',self.capabilities),('帮助',self.help)])
    def toggle_mute(self):
        if not self.audio.active:raise AirSIMError('请先开启通话语音')
        engine=self.audio.engine;engine.muted=not engine.muted
        self.notice('麦克风已静音' if engine.muted else '麦克风已开启','仅改变 Cardputer 上行音频。','calls')

    def auto_menu(self):
        if self.active_call() or self.audio.active:raise AirSIMError('请先结束通话')
        profiles=self.store.profiles();agents=sorted(set(p['agent_id'] for p in profiles))
        self.menu('自动选择连接',[('手机 '+agent,lambda agent=agent:self.submit(lambda:self.probe_routes(agent),'route_selected')) for agent in agents])

    def probe_routes(self,agent):
        priority={'lan':0,'ble':1}
        routes=sorted((p for p in self.store.profiles() if p['agent_id']==agent),key=lambda p:priority[p.get('transport','lan')])
        for profile in routes:
            candidate=make_client(profile,self.store)
            try:
                candidate.health()
                return candidate
            except Exception:candidate.close()
        raise AirSIMError('这台手机的已保存连接均不可用')

    def capabilities(self):
        self.notice('连接能力','Wi-Fi 与蓝牙支持拨号、接听、收发短信和双向通话音频。应用需保持运行才能接收来电。','settings')
    def sim_menu(self):
        slots=self.status.get('sim_slots',[])
        def choose(value):
            if not self.client.profile:raise AirSIMError('请先连接手机')
            self.client.profile['sim_account']=value;self.store.save(self.client.profile)
            self.notice('SIM 已选择','后续拨号和短信发送将使用所选卡槽。','settings')
        self.menu('选择手机 SIM',[('系统默认',lambda:choose(''))]+[(str(slot.get('label') or slot['id']),lambda slot=slot:choose(slot['id'])) for slot in slots if slot.get('id')])
    def contacts_menu(self):
        self.menu('通讯录',[('添加联系人',lambda:self.form('contact',['','']))]+[(c['name']+' '+c['number'],lambda c=c:self.contact_actions(c)) for c in self.contacts])
    def contact_actions(self,contact):
        self.menu(contact['name'],[('拨打 '+contact['number'],lambda:self.form('dial',[contact['number']])),
                                  ('发送短信',lambda:self.form('compose',[contact['number'],''])),
                                  ('删除联系人',lambda:self.confirm('删除联系人',contact['name'],lambda:self.remove_contact(contact),'settings'))])
    def remove_contact(self,contact):
        self.contacts=[c for c in self.contacts if c!=contact];self.store.save_data('contacts.json',self.contacts);self.contacts_menu()
    def history_menu(self):
        self.mark_calls_seen()
        labels={'incoming':'来电','ringing':'来电','active':'已接通','ended':'已结束','disconnected':'已结束','dialing':'拨出','held':'保持'}
        self.menu('通话记录',[(str(c.get('number') or '未知号码')+' · '+('未接' if c.get('missed') else labels.get(c.get('state'),c.get('state',''))),lambda c=c:self.form('dial',[str(c.get('number') or '')])) for c in self.phone_history()])
    def record_history(self):self.update_call_history()
    def message_hash(self,message):
        agent=(self.client.profile or {}).get('agent_id','')
        return hashlib.sha256((agent+'\0'+self.message_key(message)).encode()).hexdigest()
    def delete_message(self):
        if not self.messages:self.notice('没有短信','短信列表为空。','settings');return
        item=self.selected_message()
        def remove():
            self.deleted.add(self.message_hash(item));self.store.save_data('deleted-messages.json',sorted(self.deleted))
            self.messages=[m for m in self.messages if self.message_hash(m) not in self.deleted]
            self.store.save_messages(self.client.profile['agent_id'],self.messages);self.page='messages';self.selection=0
        self.confirm('删除本地短信','仅删除 Cardputer 上选中的短信，并屏蔽再次同步；不删除手机短信。',remove,'messages')
