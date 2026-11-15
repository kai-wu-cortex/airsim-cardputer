"""BlueZ GATT transport for the unmodified AirSIM Android VoBLE service."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import json
import logging
import secrets
import threading
import time
import uuid
from .protocol import AirSIMError, b64, unb64
from . import ble_wire as wire


class BLELink:
    def __init__(self, saved=None, on_credentials=None, client_factory=None, scanner=None, credentials_for=None, credential_store=None):
        self.saved=saved; self.on_credentials=on_credentials
        self.credential_store=credential_store
        self.credentials_for=credentials_for;self.stage='蓝牙操作'
        self.client_factory=client_factory; self.scanner=scanner;self.devices={}
        self.loop=asyncio.new_event_loop()
        self.thread=threading.Thread(target=self.loop.run_forever,name='airsim-bluez',daemon=True);self.thread.start()
        self.client=None;self.ready=False;self.attempt=None;self.session=b'';self.key=None
        self.pairing_queue=None;self.pending={};self.reassembly=wire.Reassembler();self.reader=wire.ControlReader()
        self.message_id=0;self.address='';self.active=None;self.generation=0;self.messages=[];self.info={};self.recent_calls=[]
        self.lock=threading.RLock();self.on_audio=None;self.closed=False
        self.write_lock=asyncio.Lock();self.info_at=0

    def run(self, coroutine, timeout=20):
        if self.closed:
            coroutine.close();raise AirSIMError('蓝牙连接已关闭')
        future=asyncio.run_coroutine_threadsafe(coroutine,self.loop)
        try:return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            logging.getLogger(__name__).warning('VoBLE timeout stage=%s',self.stage)
            raise AirSIMError(self.stage+'超时；请先核实手机状态，勿重复提交') from None
        except AirSIMError:raise
        except Exception as error:
            logging.getLogger(__name__).warning('VoBLE failed stage=%s type=%s',self.stage,type(error).__name__)
            raise AirSIMError(self.stage+'失败，请保持手机 AirSIM 蓝牙开启，并检查是否被另一客户端占用') from None

    def scan(self):return self.run(self._scan(),15)
    async def _scan(self):
        self.stage='扫描手机'
        if self.scanner is None:
            from bleak import BleakScanner
            self.scanner=BleakScanner
        devices=await self.scanner.discover(timeout=6,return_adv=True,service_uuids=[wire.SERVICE])
        self.devices={dev.address:dev for dev,adv in devices.values()}
        return [{'address':dev.address,'name':adv.local_name or dev.name or 'AirSIM Android'} for dev,adv in devices.values()]

    def begin(self,address):return self.run(self._begin(address),25)
    async def _begin(self,address):
        await self._disconnect()
        self.address=address;self.ready=False;self.pairing_queue=asyncio.Queue(maxsize=16)
        self.reassembly=wire.Reassembler();self.reader=wire.ControlReader()
        if self.client_factory is None:
            from bleak import BleakClient
            self.client_factory=BleakClient
        # Keep the scanned BlueZ device path instead of making Bleak scan again
        # for the same address, which can expire or rotate between discovery passes.
        self.client=self.client_factory(self.devices.get(address,address),disconnected_callback=self._disconnected)
        try:
            self.stage='连接手机蓝牙'
            await self.client.connect()
            # Debian Bleak 0.22 exposes negotiated BlueZ MTU through AcquireWrite.
            acquire=getattr(getattr(self.client,'_backend',None),'_acquire_mtu',None)
            if acquire:
                self.stage='协商蓝牙 MTU'
                try:await asyncio.wait_for(acquire(),3)
                except Exception:pass
            for characteristic in (wire.PAIRING,wire.CONTROL_TX,wire.AUDIO_DOWN):
                self.stage='订阅手机蓝牙回复'
                await self.client.start_notify(characteristic,lambda sender,data,c=characteristic:self._notification(c,bytes(data)))
            self.stage='读取手机配对状态'
            await self._write(wire.PAIRING,json.dumps({'op':'begin'}).encode(),1)
            challenge=await asyncio.wait_for(self.pairing_queue.get(),10)
            wire.check_pairing_response(challenge)
            if not self.saved and self.credentials_for:
                self.saved=self.credentials_for(challenge.get('android_identity'))
            if self.credential_store:
                pending=self.credential_store.ble_pending(challenge.get('android_identity'))
                if pending and wire.recovery_matches(challenge,pending['ble_key']):
                    self.saved=pending
            if challenge.get('address_recovery_proof') and not self.saved:
                # The phone has one pairing credential. Do not offer a SAS that cannot
                # replace it, or clear another client's existing pairing implicitly.
                raise AirSIMError(wire.PAIRING_ERRORS['peer is already paired; authenticated reconnect required'])
            self.attempt=wire.Pairing(challenge,self.saved)
            if self.attempt.reconnect:
                return await self._confirm()
            self.stage='读取六位配对码'
            await self._write(wire.PAIRING,json.dumps(self.attempt.message()).encode(),1)
            preview=await asyncio.wait_for(self.pairing_queue.get(),10)
            wire.check_pairing_response(preview)
            if preview.get('sas')!=self.attempt.sas or preview.get('op')!='prepare':
                raise AirSIMError('蓝牙校验码不一致')
            return {'sas':self.attempt.sas,'agent_id':self.attempt.identity,'address':address}
        except BaseException:
            try:await self._disconnect()
            except Exception as error:
                logging.getLogger(__name__).warning('VoBLE cleanup failed type=%s',type(error).__name__)
            raise

    def confirm(self):return self.run(self._confirm(),20)
    async def _confirm(self):
        if not self.attempt:raise AirSIMError('请重新扫描并开始配对')
        self.stage='等待手机配对确认'
        message=self.attempt.message(True)
        profile={'transport':'ble','agent_id':self.attempt.identity,'ble_address':self.address,'ble_key':b64(self.attempt.key)}
        # Android commits its rotated key before sending ready. Journal the candidate
        # before transmission; after a lost reply only a fresh HMAC recovery proof
        # may select it. It is not an authenticated connection until ready verifies.
        if self.credential_store:self.credential_store.save_ble_pending(profile)
        await self._write(wire.PAIRING,json.dumps(message).encode(),1)
        response=await asyncio.wait_for(self.pairing_queue.get(),12)
        self.attempt.verify_ready(response)
        self.key=self.attempt.key;self.session=secrets.token_bytes(16);self.ready=True
        self.saved=profile
        # Reconnect rotates the pairing key on Android; commit locally before doing anything else.
        if self.on_credentials:self.on_credentials(profile)
        elif self.credential_store:self.credential_store.save(profile)
        if self.credential_store:self.credential_store.clear_ble_pending(profile['agent_id'])
        self.attempt=None
        response=await self._command('device_info')
        self._device_info(response)
        return profile

    def _device_info(self,response):
        info=json.loads(response.get('detail') or '{}')
        if info.get('agent_id')!=self.saved['agent_id']:raise AirSIMError('蓝牙手机身份不匹配')
        with self.lock:
            self.info=info;self.generation=max(self.generation,int(info.get('call_generation',0)))
            self.info_at=time.monotonic()

    def _notification(self,characteristic,data):
        try:
            if characteristic==wire.AUDIO_DOWN:
                if self.on_audio:self.on_audio(data)
                return
            message=self.reassembly.append(data)
            if message is None:return
            kind,payload=message
            if kind==1:
                value=json.loads(payload)
                if self.pairing_queue.full():raise AirSIMError('蓝牙配对响应过多')
                self.pairing_queue.put_nowait(value);return
            if not self.ready or self.key is None:return
            envelope=self.reader.decode(self.key,payload)
            if envelope['session']!=self.session:raise AirSIMError('旧蓝牙会话消息已拒绝')
            body=envelope['body'];command=envelope['command']
            if command=='response':
                pending=self.pending.get(body.get('request_id'))
                if pending and pending[0]==envelope['generation'] and not pending[1].done():pending[1].set_result(body)
            elif command=='call_state':
                with self.lock:
                    generation=envelope['generation']
                    if generation<self.generation:return
                    self.generation=generation
                    call={'id':envelope['call_id'],'generation':generation,'number':body.get('number',''),
                          'state':body.get('state',''),'direction':body.get('direction','')}
                    previous=next((c for c in self.recent_calls if c['id']==call['id'] and c['generation']==generation),{})
                    for field in ('number','direction'):
                        if not call[field]:call[field]=previous.get(field,'')
                    connected=previous.get('connected',False) or call['state'] in ('active','connected','held')
                    observed_incoming=previous.get('observed_incoming',False) or call['state'] in ('incoming','ringing')
                    record=dict(call,connected=connected,observed_incoming=observed_incoming)
                    if call['state'] in ('ended','disconnected'):
                        record['missed']=bool(observed_incoming and not connected)
                    self.recent_calls=[record]+[c for c in self.recent_calls if (c['id'],c['generation'])!=(call['id'],generation)]
                    self.recent_calls=self.recent_calls[:100]
                    if call['state'] in ('ended','disconnected'):
                        if self.active and self.active['id']==call['id']:self.active=None
                    else:self.active=call
            elif command=='sms_event':
                with self.lock:
                    self.messages.append({k:body.get(k,'') for k in ('sender','content','timestamp','delivery_id','sim_slot')})
                    self.messages=self.messages[-512:]
        except Exception:
            # Never admit unauthenticated notifications or log their payload.
            return

    async def _write(self,characteristic,payload,kind):
        async with self.write_lock:
            await self._write_message(characteristic,payload,kind)

    async def _write_message(self,characteristic,payload,kind):
        if not self.client or not self.client.is_connected:raise AirSIMError('蓝牙已断开')
        self.message_id=(self.message_id+1)&65535
        # Android rejects ATT prepared/long writes: every fragment must fit the negotiated MTU.
        capacity=max(20,min(512,self.client.mtu_size-3))
        for fragment in wire.fragments(payload,kind,self.message_id,capacity):
            await self.client.write_gatt_char(characteristic,fragment,response=True)

    async def _command(self,command,call_id='',generation=0,fields=None,timeout=12):
        if not self.ready:raise AirSIMError('蓝牙尚未认证')
        self.stage='读取手机信息' if command=='device_info' else '等待手机操作回复'
        request_id=str(uuid.uuid4());future=self.loop.create_future()
        self.pending[request_id]=(generation,future)
        try:
            body=dict(fields or {},request_id=request_id)
            payload=wire.control_encode(self.key,self.session,command,call_id,generation,body)
            await self._write(wire.CONTROL_RX,payload,2)
            response=await asyncio.wait_for(future,timeout)
            if response.get('ok') is not True:raise AirSIMError('手机拒绝蓝牙操作：'+str(response.get('code','failed'))[:60])
            return response
        finally:self.pending.pop(request_id,None)

    def command(self,command,call_id='',generation=0,fields=None,timeout=12):
        return self.run(self._command(command,call_id,generation,fields,timeout),timeout+3)

    async def _disconnect(self):
        self.ready=False;self.on_audio=None
        try:
            if self.client:
                client,self.client=self.client,None
                if client.is_connected:await client.disconnect()
        finally:self._disconnected(None)

    def _disconnected(self,_):
        self.ready=False
        for _,future in self.pending.values():
            if not future.done():future.set_exception(AirSIMError('蓝牙已断开，操作结果需核实'))
        self.pending.clear()

    def disconnect(self):
        if not self.closed:self.run(self._disconnect(),5)

    async def _shutdown(self):
        try:await self._disconnect()
        finally:
            current=asyncio.current_task()
            pending=[task for task in asyncio.all_tasks() if task is not current]
            for task in pending:task.cancel()
            if pending:await asyncio.gather(*pending,return_exceptions=True)

    def close(self):
        if self.closed:return
        try:self.run(self._shutdown(),5)
        except Exception:pass
        self.closed=True;self.loop.call_soon_threadsafe(self.loop.stop);self.thread.join(timeout=1)
        if not self.thread.is_alive():self.loop.close()


class BLEClient:
    def __init__(self,profile,store=None,link=None):
        self.profile=profile;self.store=store
        self.link=link or BLELink(profile,self._save,credential_store=store)
        self.link.on_credentials=self._save
        self.link.credential_store=store
    def _save(self,profile):
        profile=dict(self.profile,**profile);self.profile=profile
        if self.store:self.store.save(profile)
    def health(self):
        if not self.link.ready:
            devices=self.link.scan()
            target=next((d for d in devices if d['address']==self.profile['ble_address']),None)
            if target is None:
                if len(devices)==1:target=devices[0]
                elif not devices:raise AirSIMError('未发现已配对手机，请保持手机 AirSIM 蓝牙开启')
                else:raise AirSIMError('手机蓝牙地址已变化，附近有多台 AirSIM；请重新扫描并选择原手机')
            # begin validates agent identity and the saved-key address recovery proof
            # before authenticated reconnect; never trust a Bluetooth name alone.
            self.link.begin(target['address'])
        # Authenticated call/SMS events keep the live state current. Large
        # device_info indications compete with PCM and urgent call controls.
        with self.link.lock:
            if self.link.info and (self.link.active or time.monotonic()-self.link.info_at<30):
                return dict(self.link.info,ok=True)
        response=self.link.command('device_info');self.link._device_info(response)
        return dict(self.link.info,ok=True)
    def snapshot(self):
        self.health()
        with self.link.lock:return {'calls':{'active':self.link.active,'history':list(self.link.recent_calls)},'sms':list(self.link.messages),'status':dict(self.link.info)}
    def dial(self,number):
        self.health()
        with self.link.lock:
            self.link.generation+=1;generation=self.link.generation;identity=str(uuid.uuid4())
        self.link.command('dial',identity,generation,{'number':number,**self.sim_fields()})
        # Events carry the authoritative Android call ID; do not overwrite it with the provisional ID.
        return {'android_id':identity}
    def send_sms(self,phone,message):
        self.health();self.link.command('sms_send',fields={'number':phone,'message':message,**self.sim_fields()},timeout=60)
        return {'sent':True}
    def control(self,action,call_id):
        with self.link.lock:active=self.link.active
        if not active or active['id']!=call_id:raise AirSIMError('通话已变化')
        return self.link.command('end' if action=='hangup' else action,call_id,active['generation'])
    def dtmf(self,digit,call_id):
        active=self.link.active
        if not active or active['id']!=call_id:raise AirSIMError('通话已变化')
        return self.link.command('dtmf',call_id,active['generation'],{'number':digit})
    def sim_fields(self):return {'sim_account':self.profile['sim_account']} if self.profile.get('sim_account') else {}
    def close(self):self.link.close()
