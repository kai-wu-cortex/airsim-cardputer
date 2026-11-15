"""In-process GATT peer. Mirrors Android's frames; never accesses a Bluetooth radio."""
import hashlib
import hmac
import json
import secrets
import struct
import time
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey,X25519PublicKey
from cryptography.hazmat.primitives import serialization
from airsim_cp import ble_wire as wire
from airsim_cp.protocol import b64,unb64


def derive(source,salt,info):
    return hmac.new(hmac.new(salt,source,hashlib.sha256).digest(),info+b'\x01',hashlib.sha256).digest()


class FakeGatt:
    mtu_size=185
    def __init__(self,address,disconnected_callback=None):
        self.address=address;self.is_connected=False;self.callbacks={};self.disconnected_callback=disconnected_callback
        self.reassembly=wire.Reassembler();self.mid=100;self.commands=[];self.key=None
        self.reader=wire.ControlReader();self.identity='test-android-ble';self.generation=0
    async def connect(self):self.is_connected=True
    async def disconnect(self):
        self.is_connected=False
        if self.disconnected_callback:self.disconnected_callback(self)
    async def start_notify(self,characteristic,callback):self.callbacks[characteristic]=callback
    def emit(self,characteristic,payload,kind):
        self.mid+=1
        for part in wire.fragments(payload,kind,self.mid,180):self.callbacks[characteristic](None,part)
    def pair_emit(self,value):self.emit(wire.PAIRING,json.dumps(value).encode(),1)
    def control_emit(self,command,call,generation,body):
        self.emit(wire.CONTROL_TX,wire.control_encode(self.key,self.session,command,call,generation,body),2)
    async def write_gatt_char(self,characteristic,data,response):
        if characteristic==wire.AUDIO_UP:return
        complete=self.reassembly.append(data)
        if not complete:return
        kind,payload=complete
        if kind==1:
            obj=json.loads(payload)
            if obj['op']=='begin':
                self.private=X25519PrivateKey.generate();self.public=self.private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
                self.nonce=secrets.token_bytes(32);self.challenge='fixture-challenge'
                challenge={'op':'begin','challenge_id':self.challenge,'android_identity':self.identity,'android_public':b64(self.public),'android_nonce':b64(self.nonce)}
                if self.key:
                    transcript=b''.join(struct.pack('>H',len(x))+x for x in (b'\1',self.identity.encode(),self.public,bytes(32),self.nonce,bytes(32)))
                    proof=hmac.new(self.key,b'AirSIM/VoBLE/v1/address-recovery/'+self.challenge.encode()+transcript,hashlib.sha256).digest()[:16]
                    challenge['address_recovery_proof']=b64(proof)
                self.pair_emit(challenge)
                return
            public,nonce=unb64(obj['iphone_public']),unb64(obj['iphone_nonce'])
            transcript=b''.join(struct.pack('>H',len(x))+x for x in (b'\1',self.identity.encode(),self.public,public,self.nonce,nonce))
            material=derive(self.private.exchange(X25519PublicKey.from_public_bytes(public)),hashlib.sha256(transcript).digest(),b'AirSIM/VoBLE/v1/pairing')
            sas=f"{int.from_bytes(hmac.new(material,b'AirSIM/VoBLE/v1/sas'+transcript,hashlib.sha256).digest()[:4],'big')%1000000:06d}"
            if obj['op']=='prepare':self.pair_emit({'op':'prepare','sas':sas})
            else:
                if self.key:
                    assert hmac.compare_digest(unb64(obj['reconnect_tag']),hmac.new(self.key,b'AirSIM/VoBLE/v1/reconnect'+transcript,hashlib.sha256).digest()[:16])
                else:assert obj['sas']==sas
                self.key=material
                self.pair_emit({'ready':True,'confirmation_tag':b64(hmac.new(material,b'AirSIM/VoBLE/v1/ready'+self.challenge.encode(),hashlib.sha256).digest()[:16])})
        else:
            packet=self.reader.decode(self.key,payload);self.session=packet['session'];command=packet['command'];body=packet['body']
            self.commands.append((command,packet['call_id'],packet['generation'],body))
            detail='ok'
            if command=='device_info':detail=json.dumps({'agent_id':self.identity,'device_name':'模拟蓝牙手机','call_generation':self.generation,'sim_slots':[{'id':'sim-1','label':'SIM 1'}]})
            if command=='dial':self.generation=packet['generation']
            if command=='start_media':detail='media_ready'
            self.control_emit('response',packet['call_id'],packet['generation'],{'request_id':body['request_id'],'ok':True,'detail':detail,'code':'ok'})
            if command=='dial':self.control_emit('call_state','android-call-1',self.generation,{'state':'active','number':body['number'],'direction':'outgoing'})
            if command in ('end','reject'):self.control_emit('call_state',packet['call_id'],packet['generation'],{'state':'ended'})
