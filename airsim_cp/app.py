import argparse
from concurrent.futures import Future
import threading
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import uuid
from datetime import datetime, timezone
import time

from .packet_audio import AudioRouter
from .features import Features, make_client
from .protocol import AirSIMError, Client, ProfileStore
from .ui import View
from .call_ui import CallUI
from .dashboard import Dashboard


class App(Features,CallUI,Dashboard):
    def __init__(self, store=None, client=None, demo=False):
        self.store = store or ProfileStore()
        error = ''
        try:
            profile = self.store.load()
        except AirSIMError as problem:
            profile = None; error = str(problem)
        self.client = client or make_client(profile,self.store)
        self.demo = demo
        self.audio = AudioRouter()
        self.audio_call_id = ''
        self.page = 'home'
        self.return_page = 'home'
        self.fields = []; self.field = 0
        self.selection = self.scroll = 0
        self.messages = []; self.calls = {}; self.status = {}
        if self.client.profile and not demo:
            try:
                self.messages = self.store.load_messages(self.client.profile['agent_id'])
            except AirSIMError as problem:
                error = str(problem)
        self.online = False
        self.running = True
        self.busy = False
        self.future = None
        self.next_poll = 0
        self.dirty = True
        self.confirm_title = self.confirm_body = ''
        self.pending = None
        self.notice_title = self.notice_text = ''
        self.notice_action = None
        self.init_features()
        self.init_call_ui()
        self.init_dashboard()
        if demo:
            self.online = True
            self.messages = [{'sender': 'AirSIM Demo', 'content': '你好，Cardputer！这是一条模拟短信，用于验证中文显示、翻页和按键。没有连接真实手机。', 'delivery_id': 'demo'}]
        if error:
            self.notice('配置提示', error)

    def device_name(self):
        return str(self.status.get('model') or self.status.get('device_name') or
                   (self.client.profile or {}).get('host') or 'Android')

    def active_call(self):
        return self.calls.get('active') or {}

    def selected_message(self):
        return self.messages[min(self.selection, len(self.messages) - 1)] if self.messages else {}

    def notice(self, title, text, return_page=None):
        self.return_page = return_page or ('home' if self.page in ('notice', 'confirm') else self.page)
        self.notice_title, self.notice_text = title, text
        self.notice_action = None
        self.page = 'notice'; self.scroll = 0; self.dirty = True

    def form(self, page, fields):
        self.page, self.fields, self.field = page, fields, 0
        self.dirty = True

    def confirm(self, title, body, action, return_page):
        self.confirm_title, self.confirm_body, self.pending = title, body, action
        self.page, self.return_page = 'confirm', return_page
        self.dirty = True

    def submit(self, function, kind):
        if self.busy:
            return
        if self.demo:
            self.notice('模拟模式', '这是离线界面演示，不会发送短信、拨打电话或配对手机。')
            return
        self.busy = True; self.dirty = True
        future = Future()
        self.future = future
        def run():
            try:
                future.set_result(function())
            except Exception as error:
                future.set_exception(error)
        self.work_kind = kind
        threading.Thread(target=run, name='airsim-request', daemon=True).start()

    def refresh(self):
        if self.client.profile:
            self.submit(self.client.snapshot, 'refresh')
        elif not self.demo:
            self.notice('尚未配对', '按 8 进入连接，再按 5 配对安卓手机。', 'home')

    def merge_messages(self,incoming):
        selected=self.message_key(self.selected_message()) if self.messages else None
        merged={self.message_key(item):item for item in self.messages}
        merged.update({self.message_key(item):item for item in incoming})
        messages=sorted((m for m in merged.values() if self.message_hash(m) not in self.deleted),
                        key=lambda item:str(item.get('timestamp','')),reverse=True)[:512]
        if messages!=self.messages:
            self.messages=messages
            if self.page=='message' and selected:
                self.selection=next((i for i,m in enumerate(messages) if self.message_key(m)==selected),0)
            else:self.selection=min(self.selection,max(0,len(messages)-1))
            self.store.save_messages(self.client.profile['agent_id'],messages)
            self.dirty=True

    def tick(self):
        # BLE events arrive even while a notice or form is open. A normal remote
        # hangup must stop PCM immediately, rather than waiting for the next page
        # poll or misreporting the subsequent media receive timeout as a failure.
        link=getattr(self.client,'link',None)
        if link is not None and link.ready:
            with link.lock:
                live=dict(link.active) if link.active else None
                self.calls['history']=list(getattr(link,'recent_calls',[]))
            if self.calls.get('active')!=live:
                self.calls['active']=live;self.dirty=True
            if self.audio_call_id and (not live or live.get('id')!=self.audio_call_id):
                self.audio.stop_async();self.audio.error='';self.audio_call_id=''
                if self.page=='notice' and self.notice_title=='语音已开启':self.page='calls'
                self.dirty=True
        if self.future and self.future.done():
            future, self.future = self.future, None
            self.busy = False; self.dirty = True
            kind = self.work_kind
            try:
                result = future.result()
                if kind == 'refresh':
                    self.calls, self.status = result['calls'], result['status']
                    if link is not None and link.ready:
                        with link.lock:self.calls={'active':dict(link.active) if link.active else None}
                    self.merge_messages(result['sms'])
                    if hasattr(self.client,'start_sms_watch'):self.client.start_sms_watch()
                    self.online = True
                    self.record_history()
                    if self.audio.active and self.active_call().get('id') != self.audio_call_id:
                        self.audio.stop_async()
                elif kind == 'pair':
                    self.activate(result)
                    self.online = True
                    self.notice('连接成功', '配对与手机身份验证通过。返回后可查看短信和电话。', 'home')
                elif self.feature_result(kind,result):
                    pass
                elif kind == 'audio':
                    if self.active_call().get('id')==self.audio_call_id:
                        self.call_hint='' if self.audio.active else '声音未连接，按 6 重试'
                        self.page='calls'
                elif kind == 'send':
                    self.messages.insert(0, {'sender': result['phone'], 'content': result['message'],
                        'direction': 'outgoing', 'delivery_id': 'local-' + str(uuid.uuid4()),
                        'timestamp': datetime.now(timezone.utc).isoformat()})
                    self.store.save_messages(self.client.profile['agent_id'], self.messages)
                    self.page='messages'
                elif kind == 'dial':
                    self.page='calls';self.call_hint='正在呼叫…'
                elif kind.startswith('call_'):
                    self.call_hint='';self.page='calls'
                else:
                    self.page = 'calls'
                self.next_poll = time.monotonic() + ((1 if self.active_call() else 5) if kind == 'refresh' else 0)
            except Exception as error:
                if kind=='dial':self.outgoing_audio_until=0
                self.online = False if kind in ('refresh', 'pair') else self.online
                message = str(error) if isinstance(error, AirSIMError) else '操作失败，请检查设备、配置和连接'
                # Poll failure is visible but does not continuously interrupt text entry.
                if kind=='audio' and self.active_call().get('id')!=self.audio_call_id:
                    pass
                elif kind=='audio' or kind.startswith('call_'):
                    self.call_hint=message;self.page='calls'
                elif kind=='refresh':
                    self.home_hint='连接暂时中断，正在重连';self.home_hint_until=time.monotonic()+15
                else:
                    self.notice('连接失败' if kind == 'refresh' else '操作未完成', message,
                                'settings' if kind == 'pair' else 'home')
                self.next_poll = time.monotonic() + 15
        if self.audio.error:
            error, self.audio.error = self.audio.error, ''
            self.call_hint='声音中断，按 6 重试';self.dirty=True
        if not self.demo and hasattr(self.client,'pending_sms'):
            incoming=self.client.pending_sms()
            if incoming:self.merge_messages(incoming)
        self.tick_call_ui()
        self.tick_lan()
        self.record_history()
        self.tick_dashboard()
        if (self.client.profile and not self.demo and not self.busy and time.monotonic() >= self.next_poll
                and self.page != 'pair'):
            self.refresh()

    @staticmethod
    def message_key(item):
        return item.get('delivery_id') or '\0'.join(str(item.get(key, '')) for key in ('sender', 'timestamp', 'content'))

    def back(self):
        if self.page=='calls' and self.active_call():self.call_minimized=True
        if self.page == 'help':
            self.page, self.return_page = self.help_previous
        elif self.page in ('confirm', 'notice'):
            self.page = self.return_page
            self.pending = None
        elif self.page=='lan':
            self.stop_lan_scan();self.page='settings'
        elif self.page=='pair':
            self.scan_lan()
        elif self.page == 'message':
            self.page = 'messages'
        elif self.page != 'home':
            self.page = 'home'
        self.dirty = True

    def help(self):
        if self.page == 'help':
            self.back()
        else:
            self.help_previous = (self.page, self.return_page)
            self.page = 'help'
        self.dirty = True

    def key(self, key):
        self.dirty = True
        if key == 'ESC':
            self.back(); return
        if key == 'HELP':
            self.help(); return
        if self.call_key(key):return
        if self.busy and self.work_kind != 'refresh':
            return
        if self.page in ('pair', 'compose', 'dial', 'contact', 'dtmf'):
            if key == 'TAB':
                self.field = (self.field + 1) % len(self.fields)
            elif key == 'BACKSPACE':
                self.fields[self.field] = self.fields[self.field][:-1]
            elif key == 'ENTER':
                if self.field < len(self.fields) - 1:
                    self.field += 1
                else:
                    self.review_form()
            elif len(key) == 1 and key.isprintable():
                limit = 2000 if self.page == 'compose' and self.field == 1 else 40
                if self.page=='dtmf':
                    limit=1
                    if key not in '0123456789*#':return
                if self.page == 'pair':
                    limit = (15, 5, 6)[self.field]
                    if key not in ('0123456789.' if self.field == 0 else '0123456789'):
                        return
                if len(self.fields[self.field]) < limit:
                    self.fields[self.field] += key
            return
        key = {'f': 'UP', 'z': 'LEFT', 'x': 'DOWN', 'c': 'RIGHT'}.get(key, key)
        if self.lan_key(key):return
        if self.menu_key(key):return
        if self.page == 'home':
            if key == '4':
                if self.active_call():self.page='calls';self.call_minimized=False
                else:self.form('dial',[''])
            elif key == '5': self.history_menu()
            elif key == '6': self.page = 'messages'
            elif key == '7': self.mode_menu()
            elif key == '8': self.page = 'settings'
        elif self.page == 'messages':
            if key == '4': self.refresh()
            elif key in ('5', 'UP', 'LEFT'): self.selection = max(0, self.selection - 1)
            elif key in ('6', 'DOWN', 'RIGHT'): self.selection = min(max(0, len(self.messages) - 1), self.selection + 1)
            elif key in ('7', 'ENTER') and self.messages: self.open_message()
            elif key == '8': self.form('compose', ['', ''])
        elif self.page in ('message', 'notice'):
            if self.page=='notice' and key=='8' and self.notice_action:
                self.notice_action[1]();return
            if key in ('4',) or (self.page == 'notice' and key in ('8', 'ENTER')): self.back()
            elif key in ('5', 'UP', 'LEFT'): self.scroll = max(0, self.scroll - 3)
            elif key in ('6', 'DOWN', 'RIGHT'):
                text = self.notice_text if self.page == 'notice' else str(self.selected_message().get('content', ''))
                self.scroll = min(max(0, len(View().lines(text, 292)) - 4), self.scroll + 3)
            elif self.page == 'message' and key == '7': self.form('compose', [str(self.selected_message().get('sender', '')), ''])
            elif self.page == 'message' and key == '8': self.form('compose', ['', ''])
        elif self.page == 'settings':
            if key == '4': self.connection_menu()
            elif key == '5': self.scan_lan()
            elif key == '6': self.scan_ble()
            elif key == '7': self.mode_menu()
            elif key == '8': self.more_menu()
        elif self.page == 'confirm':
            if key == '4': self.back()
            elif key == '8' and self.pending:
                if self.busy:
                    return
                pending, self.pending = self.pending, None
                self.page = self.return_page
                pending()
        elif self.page == 'help' and key in ('4', '8', 'ENTER'):
            self.back()

    def review_form(self):
        values, page = list(self.fields), self.page
        if not all(value.strip() for value in values):
            self.notice('请补全输入', '所有输入项都不能为空。', page); return
        if page == 'pair':
            self.confirm('Wi-Fi 配对','手机将切到本地模式，替换原云端注册。确认使用六位码配对？',
                         lambda:self.submit(lambda:Client().pair(*values,accept_local_mode=True),'pair'),page)
        elif page == 'contact':
            self.contacts.append({'name':values[0],'number':values[1]})
            self.store.save_data('contacts.json',self.contacts);self.contacts_menu()
        elif page == 'dtmf':
            identity=self.active_call().get('id')
            if not identity:raise AirSIMError('当前没有通话')
            self.confirm('发送通话按键',values[0],lambda:self.submit(lambda:self.client.dtmf(values[0],identity),'dtmf'),'calls')
        elif page == 'compose':
            self.confirm('确认发送短信', f'收件人：{values[0]}\n正文：{values[1][:45]}\n将通过手机 SIM 发送。',
                         lambda: self.submit(lambda: {'result': self.client.send_sms(*values), 'phone': values[0], 'message': values[1]}, 'send'), page)
        elif page == 'dial':
            self.dial_from_ui(values[0])

    def start_audio(self, identity, *, early=False):
        self.audio_call_id = identity
        token=self.call_token()
        self.submit(lambda: (self.audio.start(self.client, identity, early=True) if early else self.audio.start(self.client, identity))
                    if self.audio_intent==token and self.call_token()==token else None, 'audio')

    def stop_audio(self):
        self.audio.stop(); self.page = 'calls'

    def close(self):
        self.stop_lan_scan()
        self.audio.close()
        if hasattr(self.client,'close'):self.client.close()
        if self.ble_setup:self.ble_setup.close()


def main():
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger('airsim_cp').setLevel(logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--render', type=Path)
    parser.add_argument('--page', default='home')
    parser.add_argument('--state-file', type=Path, help='Optional non-sensitive UI test state')
    parser.add_argument('--config-dir', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if not args.render and os.geteuid() == 0:
        raise SystemExit('Run AirSIM as the ordinary device user, never root.')
    app = App(ProfileStore(args.config_dir), demo=args.demo)
    app.page = args.page
    view = View()
    if args.render:
        args.render.parent.mkdir(parents=True, exist_ok=True)
        view.render(app).save(args.render); app.close(); return
    from .hardware import Framebuffer, keyboard, character
    import select
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp'))
    lock = open(runtime / f'airsim-cardputer-{os.getuid()}.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('AirSIM is already running')
    fb = Framebuffer()
    device = keyboard()
    def stop(*_): app.running = False
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    escape_since = None; modifiers = set(); last_minute = ''
    try:
        while app.running:
            app.tick()
            if escape_since and time.monotonic() - escape_since >= 0.8:
                app.running = False; break
            minute = time.strftime('%H:%M')
            if app.dirty or minute != last_minute:
                image = view.render(app); fb.show(image)
                if args.state_file:
                    state_temp = args.state_file.with_suffix('.tmp')
                    state_temp.write_text(json.dumps({'page': app.page, 'busy': app.busy, 'online': app.online,
                                                          'paired': bool(app.client.profile), 'pid': os.getpid(),
                                                          'uid': os.getuid(), 'demo': app.demo}))
                    os.replace(state_temp, args.state_file)
                app.dirty = False; last_minute = minute
            if select.select([device], [], [], 0.05)[0]:
                for event in device.read():
                    if event.type != 1: continue
                    if event.code in (42, 54, 29, 97, 56, 100):
                        if event.value: modifiers.add(event.code)
                        else: modifiers.discard(event.code)
                        continue
                    if event.code == 1:
                        if event.value == 1: escape_since = time.monotonic()
                        elif event.value == 0 and escape_since:
                            if time.monotonic() - escape_since < 0.8: app.key('ESC')
                            else: app.running = False
                            escape_since = None
                        continue
                    if event.value not in (1, 2): continue
                    if event.value==2 and app.page in ('home','calls','confirm'):continue
                    if event.code == 99:
                        folder = app.store.directory / 'screenshots'; folder.mkdir(parents=True, exist_ok=True)
                        view.render(app).save(folder / (time.strftime('%Y%m%d-%H%M%S') + '.png'))
                        continue
                    special = {28:'ENTER', 15:'TAB', 14:'BACKSPACE', 103:'UP', 108:'DOWN', 105:'LEFT', 106:'RIGHT', 138:'HELP'}
                    if modifiers & {29, 97, 56, 100}: continue
                    key = special.get(event.code) or character(event.code, bool(modifiers & {42, 54}))
                    if key:
                        try:app.key(key)
                        except (AirSIMError,ValueError,OSError):
                            app.notice('操作未完成','请检查输入、连接状态和配置文件。','settings')
    finally:
        app.close(); device.close(); fb.close(); lock.close()


if __name__ == '__main__':
    main()
