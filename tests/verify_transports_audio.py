"""Run on Cardputer: real PipeWire with fake GATT peer only."""
import asyncio
import json
from pathlib import Path
import struct
import time
import urllib.request
from airsim_cp import ble_wire as wire
from airsim_cp.ble import BLELink,BLEClient
from airsim_cp.protocol import NoRedirect
from airsim_cp.packet_audio import PacketAudio
from fake_ble import FakeGatt

class AudioGatt(FakeGatt):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.media_task=None;self.audio_received=0;self.audio_parts={};self.write_delay=0
    async def disconnect(self):
        if self.media_task:self.media_task.cancel();await asyncio.gather(self.media_task,return_exceptions=True)
        await super().disconnect()
    async def write_gatt_char(self,characteristic,data,response):
        if characteristic==wire.AUDIO_UP:
            if self.write_delay:await asyncio.sleep(self.write_delay)
            magic,seq,stamp,index,count,size=struct.unpack('>7sIIBBB',data[:18])
            parts=self.audio_parts.setdefault(seq,{})
            parts[index]=data[18:]
            if len(parts)==count:
                sealed=b''.join(parts[i] for i in range(count));self.audio_parts.pop(seq)
                raw=wire.media_crypt(self.media_key,1,magic[6],seq,stamp,sealed,True)
                self.audio_received+=len(wire.mulaw_decode(raw))
            return
        before=len(self.commands);await super().write_gatt_char(characteristic,data,response)
        if len(self.commands)==before:return
        command,call,generation,_=self.commands[-1]
        if command=='start_media':
            self.media_key=wire.session_key(self.key,self.session,call,generation,b'media')
            async def stream():
                seq=0
                while self.is_connected:
                    sealed=wire.media_crypt(self.media_key,2,1,seq,seq*160,bytes([255])*160)
                    parts=[sealed[:160],sealed[160:]]
                    for index,part in enumerate(parts):
                        packet=struct.pack('>7sIIBBB',b'\xa5\x5a\1\1\3\1\1',seq,seq*160,index,len(parts),len(part))+part
                        self.callbacks[wire.AUDIO_DOWN](None,packet)
                    seq+=1;await asyncio.sleep(.02)
            self.media_task=asyncio.create_task(stream())
        elif command=='stop_media' and self.media_task:
            self.media_task.cancel();await asyncio.gather(self.media_task,return_exceptions=True);self.media_task=None

results={}
link=BLELink(client_factory=AudioGatt);audio=PacketAudio()
try:
    link.begin('AA:BB:CC:DD:EE:FF');profile=link.confirm();client=BLEClient(profile,link=link)
    client.dial('10000');call=client.snapshot()['calls']['active']
    audio.start(client,call['id']);time.sleep(2)
    results['ble']={'uplink_bytes':audio.uplink_bytes,'downlink_bytes':audio.downlink_bytes,'peer_decrypted_bytes':link.client.audio_received,'active':audio.active}
    print(json.dumps(results),flush=True)
    assert audio.active and audio.uplink_bytes>8000 and audio.downlink_bytes>8000 and link.client.audio_received>8000
    link.client.write_delay=.06;time.sleep(1)
    results['ble']['slow_link_capture_drops']=audio.capture_drops
    results['ble']['capture_queue_frames']=audio.capture_frames.qsize()
    assert audio.active and audio.capture_drops>0 and audio.capture_frames.qsize()<=3
finally:audio.close();link.close()
results['real_phone_touched']=False
Path('/home/pi/.cache/airsim-cardputer-test/transport-audio-checks.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results))
