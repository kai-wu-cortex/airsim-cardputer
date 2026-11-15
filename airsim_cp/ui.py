"""320×170 UI shared by the real framebuffer and offline preview/tests."""
from pathlib import Path
import os
import time

from PIL import Image, ImageDraw, ImageFont

BACKGROUND = '#0b1522'
PANEL = '#162539'
WHITE = '#f0f5fa'
MUTED = '#91a6bf'
ACCENT = '#54dfbd'
DANGER = '#ff7c8b'


def font(size):
    candidates = [os.environ.get('AIRSIM_FONT', ''), '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                  '/System/Library/Fonts/PingFang.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    for path in candidates:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size, index=2 if 'NotoSansCJK' in path else 0)
    return ImageFont.load_default(size=size)


class View:
    def __init__(self):
        self.small, self.normal, self.title = font(12), font(14), font(16)
        self.large,self.number=font(23),font(26)
        root=Path(__file__).resolve().parents[1]
        self.brand_icon=None
        for path in (root/'icon.png',root/'assets/icon.png'):
            if path.is_file():
                with Image.open(path) as icon:
                    self.brand_icon=icon.convert('RGBA').resize((22,22),Image.Resampling.LANCZOS)
                break

    def render(self, app):
        self.image = Image.new('RGB', (320, 170), BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)
        self.brand()
        status = '模拟数据' if app.demo or (app.client.profile or {}).get('test_fixture') else ('已连接' if app.online else ('连接中…' if app.busy else '未连接'))
        self.text(182, 6, status, self.small, ACCENT if app.online else MUTED)
        self.text(276, 6, time.strftime('%H:%M'), self.small, MUTED)
        self.draw.line((8, 27, 312, 27), fill=PANEL)
        page = app.page
        if page == 'home':
            # Phone number stays beside the name; reserve its width before truncating the name.
            number=app.phone_number()
            number=self.cut(number,155)
            number_width=self.normal.getlength(number)
            name=app.device_name() if app.client.profile else '未连接手机'
            self.text(12,33,self.cut(name,278-number_width),self.normal)
            self.text(308-number_width,33,number,self.normal,ACCENT if app.online else MUTED)
            unread=app.unread_preview();missed=app.missed_preview()
            self.draw.rounded_rectangle((8,57,157,129),5,fill=PANEL)
            self.draw.rounded_rectangle((163,57,312,129),5,fill=PANEL)
            self.text(15,61,'未读短信 '+str(app.unread_count()),self.small,MUTED)
            self.text(170,61,'未接来电 '+str(app.missed_count()),self.small,MUTED)
            if unread:
                self.text(15,80,self.cut(str(unread.get('sender') or '未知号码'),134),self.small,ACCENT)
                self.wrap(str(unread.get('content') or '空短信'),15,92,134,2)
            else:self.text(15,91,'暂无未读',self.normal,MUTED)
            if missed:
                number=str(missed.get('number') or '未知号码')
                self.text(170,82,self.cut(number,134),self.normal,ACCENT)
                self.text(170,106,'按 5 查看记录',self.small,MUTED)
            else:self.text(170,91,'暂无未接',self.normal,MUTED)
            self.home_nav(['通话' if app.active_call() else '拨号','记录','短信','模式','设备'])
            self.draw.rectangle((0,0,319,26),fill=BACKGROUND)
            self.brand()
            self.draw.ellipse((87,11,91,15),fill=ACCENT if app.online else MUTED)
            self.text(96,6,app.connection_label(),self.small,ACCENT if app.online else MUTED)
            self.text(148,6,'设备电量',self.small,MUTED)
            self.battery(app,201,9)
            self.text(276,6,time.strftime('%H:%M'),self.small,MUTED)
        elif page == 'messages':
            self.text(12, 32, f'短信  {len(app.messages)}', self.title)
            if not app.messages:
                self.wrap('还没有短信\n按 8 写一条新短信', 12, 66, 296, 3)
            else:
                start = (app.selection // 3) * 3
                for row, item in enumerate(app.messages[start:start + 3]):
                    y = 57 + row * 24
                    if start + row == app.selection:
                        self.draw.rounded_rectangle((8, y - 1, 312, y + 22), 4, fill=PANEL)
                    label = str(item.get('sender', '?')) + '  ' + str(item.get('content', ''))
                    self.text(14, y, self.cut(label, 288), self.normal)
            self.nav(['刷新', '上一', '下一', '打开', '写信'])
        elif page == 'message':
            item = app.selected_message()
            self.text(12, 33, self.cut(str(item.get('sender', '短信')), 290), self.title, ACCENT)
            lines = self.lines(str(item.get('content', '')), 292)
            for row, line in enumerate(lines[app.scroll:app.scroll + 4]):
                self.text(12, 58 + row * 18, line, self.normal)
            self.nav(['返回', '上页', '下页', '回复', '写信'])
        elif page == 'calls':
            call = app.active_call()
            if call:
                state=call.get('state','');incoming=state in ('incoming','ringing');connected=state in ('active','connected','answered')
                label='来电' if incoming else ('通话中  '+app.call_timer() if connected else '正在呼叫…')
                self.center(label,34,self.title,ACCENT)
                name=app.caller_name()
                if name:
                    self.center(name,58,self.title);self.center(str(call.get('number') or '未知号码'),80,self.title,MUTED)
                else:self.center(str(call.get('number') or '未知号码'),64,self.number)
                muted=getattr(app.audio.engine,'muted',False)
                hint=app.call_hint or ('接听后直接通话' if incoming else ('麦克风已静音' if muted else '麦克风和扬声器已开启') if app.audio.active else '等待对方接听' if not connected else '按 6 连接声音')
                self.center(hint,108,self.small,MUTED)
                if incoming:self.actions([(4,'接听','primary'),(8,'拒接','danger')])
                elif connected:self.actions([(4,'取消静音' if muted else '静音','normal'),(6,'按键' if app.audio.active else '连接声音','normal'),(8,'挂断','danger')])
                else:self.actions([(8,'取消呼叫','danger')])
            else:
                self.center('正在拨号…' if app.busy else '当前没有通话',60,self.large)
                self.center(app.call_hint,99,self.small,MUTED)
                self.actions([(4,'拨号','primary'),(8,'返回','normal')])
        elif page in ('pair', 'compose', 'dial', 'contact', 'dtmf'):
            title = {'pair': '连接设备', 'compose': '发送短信', 'dial': '拨打电话', 'contact':'添加联系人', 'dtmf':'通话按键'}[page]
            self.text(12, 32, title, self.title)
            labels = {'pair': ['手机 IP', '配对端口', '六位码'], 'compose': ['收件号码', '短信正文'], 'dial': ['电话号码'], 'contact':['姓名','号码'], 'dtmf':['按键']}[page]
            for index, label in enumerate(labels):
                y = 58 + index * 25
                self.text(12, y + 2, label, self.small, MUTED)
                self.draw.rounded_rectangle((80, y, 310, y + 23), 3,
                                            fill=PANEL, outline=ACCENT if app.field == index else PANEL)
                value = app.fields[index]
                if page == 'pair' and index == 2:
                    value = '•' * len(value)
                self.text(85, y + 1, self.tail(value or ('_' if app.field == index else ''), 218), self.normal)
            if page == 'dial':
                self.text(12, 100, '输入号码后按 Enter 拨打', self.small, MUTED)
            if page == 'compose':
                self.text(12, 111, f'正文 {len(app.fields[1])}/2000 字  ·  Tab 切换', self.small, MUTED)
            submit='下一项' if app.field<len(app.fields)-1 else {'dial':'拨打','contact':'保存','compose':'预览','pair':'配对','dtmf':'预览'}[page]
            self.footer([(4,'ESC','返回','back','normal'),
                         *([(6,'Tab','换项','mode','normal')] if len(app.fields)>1 else []),
                         (8,'Enter',submit,'phone' if submit=='拨打' else 'check','primary')])
        elif page == 'lan':
            self.text(12,32,'连接设备 · Wi-Fi',self.title)
            if app.lan_devices:
                start=(app.lan_index//2)*2
                for row,device in enumerate(app.lan_devices[start:start+2]):
                    y=56+row*29
                    if start+row==app.lan_index:self.draw.rounded_rectangle((8,y-1,312,y+28),4,fill=PANEL)
                    self.text(14,y,self.cut(device['name'],285),self.normal)
                    detail=device['host']+' · '+('可配对' if device['kind']=='pair' or device.get('pairing') else 'Wi-Fi 设备')
                    self.text(14,y+15,detail,self.small,MUTED)
                self.text(12,117,'F/X 选择 · '+('扫描完成' if app.lan_done else '正在发现…'),self.small,MUTED)
            else:
                self.text(12,59,'未发现设备' if app.lan_done else '正在发现附近设备…',self.normal)
                self.wrap(app.lan_error or '保持同一 Wi-Fi，在手机 AirSIM\n打开一次性配对，或使用手动连接。',12,84,296,2)
            self.actions([(4,'重新扫描','normal'),(6,'连接','primary'),(8,'手动连接','normal')])
        elif page == 'settings':
            self.text(12, 33, '连接设备', self.title)
            profile = app.client.profile
            self.wrap(('当前：'+app.mode_name()) if profile else '选择一种方式连接设备',12,60,296,2)
            self.text(12,90,'按 4 选择已保存的手机',self.normal,MUTED)
            self.text(12,113,'SIM、联系人和通话记录在更多中',self.small,MUTED)
            self.nav(['已保存', 'Wi-Fi', '蓝牙', '模式', '更多'])
        elif page == 'menu':
            self.text(12,32,self.cut(app.menu_title,292),self.title)
            start=(app.menu_index//3)*3
            for row,(label,_) in enumerate(app.menu_items[start:start+3]):
                y=57+row*24
                if start+row==app.menu_index:self.draw.rounded_rectangle((8,y-1,312,y+22),4,fill=PANEL)
                self.text(14,y,self.cut(label,288),self.normal)
            if not app.menu_items:self.text(12,66,'暂无记录',self.normal,MUTED)
            self.nav(['返回','上一','下一','选择',''])
        elif page == 'confirm':
            self.text(12, 33, app.confirm_title, self.title, ACCENT)
            self.wrap(app.confirm_body, 12, 59, 296, 4)
            self.nav(['取消', '', '', '', '确认'])
        elif page == 'help':
            self.text(12, 33, '操作说明', self.title)
            self.wrap('来电：4 接听，8 拒接。\n接通后自动开启声音。\n通话：4 静音，8 挂断。\nESC 返回，长按退出。', 12, 59, 296, 4)
            self.nav(['返回', '', '', '', ''])
        elif page == 'notice':
            self.text(12, 33, app.notice_title, self.title, ACCENT)
            lines = self.lines(app.notice_text, 296)
            for row, line in enumerate(lines[app.scroll:app.scroll + 4]):
                self.text(12, 59 + row * 18, line, self.normal)
            if app.notice_action:
                self.actions([(4,'返回','normal'),(8,app.notice_action[0],'primary')])
            else:self.nav(['返回', '上页', '下页', '', ''])
        if app.busy and page!='calls' and (app.work_kind != 'refresh' or app.page == 'confirm'):
            self.draw.rounded_rectangle((70, 73, 250, 109), 6, fill='#233b52', outline=ACCENT)
            self.text(91, 82, '正在与手机通信…', self.normal)
        return self.image

    def brand(self):
        if self.brand_icon is not None:
            self.image.paste(self.brand_icon,(8,2),self.brand_icon)
            self.text(34,5,'AirSIM',self.normal,WHITE)
        else:self.text(10,3,'AirSIM',self.title,WHITE)

    def text(self, x, y, value, font=None, color=WHITE):
        self.draw.text((x, y), str(value), font=font or self.normal, fill=color)

    def center(self,value,y,font,color=WHITE):
        value=str(value)
        while value and font.getlength(value)>292:value=value[:-2]+'…'
        self.text((320-font.getlength(value))/2,y,value,font,color)

    def actions(self,items):
        self.footer([(key,str(key),label,self.action_icon(label),style) for key,label,style in items])

    @staticmethod
    def action_icon(label):
        return {'返回':'back','取消':'back','上一':'up','上页':'up','下一':'down','下页':'down',
                '刷新':'refresh','重新扫描':'refresh','连接':'link','手动连接':'edit',
                '选择':'check','打开':'check','确认':'check','已保存':'saved',
                'Wi-Fi':'wifi','蓝牙':'bluetooth','模式':'mode','更多':'more',
                '拨号':'phone','通话':'phone','接听':'phone','记录':'history','短信':'message',
                '设备':'device','写信':'edit','回复':'reply','静音':'mute','取消静音':'mic',
                '按键':'keypad','连接声音':'speaker','拒接':'hangup','挂断':'hangup',
                '取消呼叫':'hangup'}.get(label,'check')

    def footer(self,items):
        """One geometry for every page; the slot always follows the physical key."""
        self.draw.rectangle((0,133,319,169),fill=BACKGROUND)
        self.draw.line((8,133,312,133),fill=PANEL)
        for slot,key,label,icon,style in items:
            x=49+(slot-4)*56
            fill={'primary':'#153d36','danger':'#392330','normal':PANEL}[style]
            color={'primary':ACCENT,'danger':DANGER,'normal':WHITE}[style]
            border=color if style!='normal' else '#30475e'
            self.draw.rounded_rectangle((x-26,135,x+26,168),5,fill=fill,outline=border)
            key_width=self.small.getlength(key)
            left=x-(14+key_width)/2
            self.icon(icon,left,138,color if style!='normal' else MUTED)
            self.text(left+14,135,key,self.small,color)
            # Four CJK characters fit at 12px; never shrink below the device guideline.
            label={'重新扫描':'重扫','手动连接':'手动','取消静音':'开麦','连接声音':'声音','取消呼叫':'取消'}.get(label,label)
            while label and self.small.getlength(label)>48:label=label[:-2]+'…'
            self.text(x-self.small.getlength(label)/2,150,label,self.small,color)

    def cut(self, value, width):
        value = value.replace('\n', ' ')
        if self.normal.getlength(value) <= width:
            return value
        while value and self.normal.getlength(value + '…') > width:
            value = value[:-1]
        return value + '…'

    def tail(self, value, width):
        while value and self.normal.getlength(value) > width:
            value = value[1:]
        return value

    def lines(self, value, width):
        lines, current = [], ''
        for char in value:
            if char == '\n':
                lines.append(current); current = ''
            elif self.normal.getlength(current + char) > width:
                lines.append(current); current = char
            else:
                current += char
        lines.append(current)
        return lines

    def wrap(self, value, x, y, width, limit):
        for row, line in enumerate(self.lines(value, width)[:limit]):
            self.text(x, y + row * 18, line)

    def battery(self,app,x,y):
        value=app.status.get('battery') or {}
        if not isinstance(value,dict):value={}
        percent=value.get('percent')
        valid=isinstance(percent,(int,float)) and not isinstance(percent,bool) and 0<=percent<=100
        old=not app.online or (isinstance(value.get('observed_at_ms'),(int,float)) and time.time()*1000-value['observed_at_ms']>180000)
        color=MUTED if old or not valid else (DANGER if percent<=15 else ACCENT)
        self.draw.rounded_rectangle((x,y,x+18,y+10),2,outline=color)
        self.draw.rectangle((x+19,y+3,x+20,y+7),fill=color)
        if valid and percent>0:self.draw.rectangle((x+2,y+2,x+2+round(14*percent/100),y+8),fill=color)
        if value.get('charging'):self.draw.line([(x+11,y+1),(x+7,y+5),(x+11,y+5),(x+8,y+9)],fill=BACKGROUND,width=1)
        self.text(x+25,y-3,str(round(percent))+'%' if valid else '--',self.small,color)

    def home_nav(self,labels):
        self.nav(labels)

    def icon(self,name,x,y,color):
        if name=='phone':
            self.draw.line([(x,y),(x,y+4),(x+5,y+9),(x+9,y+9)],fill=color,width=2)
            self.draw.line([(x,y),(x+3,y+2),(x+1,y+4)],fill=color,width=2)
            self.draw.line([(x+5,y+8),(x+7,y+6),(x+10,y+9)],fill=color,width=2)
        elif name=='history':
            self.draw.ellipse((x,y,x+10,y+10),outline=color)
            self.draw.line([(x+5,y+2),(x+5,y+5),(x+8,y+5)],fill=color)
        elif name=='message':
            self.draw.rounded_rectangle((x-1,y,x+11,y+8),2,outline=color)
            self.draw.line([(x+2,y+8),(x+2,y+10),(x+5,y+8)],fill=color)
        elif name=='mode':
            self.draw.line([(x,y+2),(x+10,y+2),(x+7,y)],fill=color)
            self.draw.line([(x+10,y+8),(x,y+8),(x+3,y+10)],fill=color)
        elif name in ('back','reply','up','down'):
            points={'back':[(x+5,y+1),(x+1,y+5),(x+5,y+9)],
                    'reply':[(x+5,y+1),(x+1,y+5),(x+5,y+9)],
                    'up':[(x+1,y+7),(x+5,y+3),(x+9,y+7)],
                    'down':[(x+1,y+3),(x+5,y+7),(x+9,y+3)]}[name]
            self.draw.line(points,fill=color,width=1)
            if name in ('back','reply'):self.draw.line([(x+1,y+5),(x+10,y+5)],fill=color)
        elif name=='check':self.draw.line([(x,y+5),(x+4,y+9),(x+10,y+1)],fill=color,width=1)
        elif name=='refresh':
            self.draw.arc((x,y,x+10,y+10),45,320,fill=color)
            self.draw.line([(x+7,y),(x+10,y+1),(x+10,y+4)],fill=color)
        elif name=='wifi':
            self.draw.arc((x-1,y,x+11,y+12),220,320,fill=color)
            self.draw.arc((x+2,y+4,x+8,y+10),220,320,fill=color)
            self.draw.ellipse((x+4,y+9,x+6,y+11),fill=color)
        elif name=='bluetooth':self.draw.line([(x+2,y+2),(x+9,y+8),(x+5,y+11),(x+5,y),(x+9,y+3),(x+2,y+9)],fill=color)
        elif name=='more':
            for offset in (0,4,8):self.draw.ellipse((x+offset,y+4,x+offset+2,y+6),fill=color)
        elif name=='edit':
            self.draw.line([(x,y+10),(x+2,y+6),(x+8,y),(x+10,y+2),(x+4,y+8),(x,y+10)],fill=color)
        elif name=='saved':
            self.draw.rectangle((x+1,y,x+9,y+10),outline=color)
            self.draw.line([(x+3,y),(x+3,y+4),(x+7,y+4),(x+7,y)],fill=color)
        elif name=='link':
            self.draw.rounded_rectangle((x,y+3,x+7,y+8),2,outline=color)
            self.draw.rounded_rectangle((x+4,y,x+11,y+5),2,outline=color)
        elif name in ('mic','mute'):
            self.draw.rounded_rectangle((x+3,y,x+7,y+7),2,outline=color)
            self.draw.arc((x,y+2,x+10,y+10),0,180,fill=color)
            self.draw.line([(x+5,y+10),(x+5,y+12)],fill=color)
            if name=='mute':self.draw.line([(x,y),(x+10,y+11)],fill=color)
        elif name=='speaker':
            self.draw.line([(x,y+4),(x+3,y+4),(x+6,y+1),(x+6,y+10),(x+3,y+7),(x,y+7),(x,y+4)],fill=color)
            self.draw.arc((x+5,y+1,x+11,y+11),270,90,fill=color)
        elif name=='keypad':
            for dx in (0,4,8):
                for dy in (0,4,8):self.draw.rectangle((x+dx,y+dy,x+dx+1,y+dy+1),fill=color)
        elif name=='hangup':
            self.draw.line([(x,y+8),(x,y+4),(x+4,y+2),(x+7,y+2),(x+11,y+4),(x+11,y+8)],fill=color,width=2)
        else:
            self.draw.rounded_rectangle((x+1,y,x+9,y+11),2,outline=color)
            self.draw.point((x+5,y+9),fill=color)

    def nav(self, labels):
        self.actions([(index+4,label,'primary' if label in ('确认','选择','打开') else 'normal')
                      for index,label in enumerate(labels) if label])
