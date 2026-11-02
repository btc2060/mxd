"""Four optional timers and guarded key taps for the single-host garden entry."""
import math

from .brain import Action
from .controls import InputCancelled, InputExecutor

KEYS = frozenset((*'abcdefghijklmnopqrstuvwxyz0123456789','ctrl','shift','alt','space','tab','enter',
                  'home','end','insert','delete','page_up','page_down',*(f'f{i}' for i in range(1,13)))) - {'f8','f10','f12'}


def default_timed_keys():
    return tuple({'key':str(i),'interval_seconds':0.,'hold_seconds':.1} for i in range(1,5))


def normalize_timed_keys(slots):
    if not isinstance(slots,(tuple,list)):return slots
    return tuple({**item,'key':item['key'].strip().lower()} if isinstance(item,dict) and isinstance(item.get('key'),str) else item for item in slots)


def validate_timed_keys(slots):
    slots=normalize_timed_keys(slots)
    if not isinstance(slots,(list,tuple)) or len(slots)!=4:raise ValueError('timed_keys 必须包含4组，间隔为0即关闭')
    enabled=[]
    for item in slots:
        if not isinstance(item,dict) or not {'key','interval_seconds','hold_seconds'}<=set(item) or set(item)-{'key','interval_seconds','hold_seconds','settle_seconds'}:raise ValueError('定时键须含 key、interval_seconds、hold_seconds，可选 settle_seconds')
        if not isinstance(item['key'],str) or item['key'] not in KEYS:raise ValueError('定时键不支持；F8/F10/F12 为保留热键')
        interval,hold=item['interval_seconds'],item['hold_seconds']
        if type(interval) not in (int,float) or not math.isfinite(interval) or interval<0 or (interval and interval<1):raise ValueError('定时键间隔为0关闭，启用时至少1秒')
        if type(hold) not in (int,float) or not math.isfinite(hold) or not .02<=hold<=1:raise ValueError('定时按住时间须为0.02～1秒')
        settle=item.get('settle_seconds',.8)
        if type(settle) not in (int,float) or not math.isfinite(settle) or not 0<=settle<=5:raise ValueError('settle_seconds 须为0～5秒')
        if interval:
            if item['key']=='alt':raise ValueError('Alt 已有独立定时，请用 alt_interval 设置')
            enabled.append(item['key'])
    if len(enabled)!=len(set(enabled)):raise ValueError('启用的定时键请勿重复')


class TimedKeys:
    def __init__(self,slots):
        self.slots=normalize_timed_keys(slots)
        self.next_due=[None]*4

    def action(self,now):
        for i,item in enumerate(self.slots):
            interval=item['interval_seconds']
            if interval<=0:continue
            if self.next_due[i] is None:self.next_due[i]=now+interval
        for i,item in enumerate(self.slots):
            if item['interval_seconds']>0 and now>=self.next_due[i]:
                return Action('timed_key',item['key'],f"固定定时键{i+1}：{item['key']}，间隔{item['interval_seconds']:g}s")

    def executed(self,key,pressed_at):
        item=next((item for item in self.slots if item['key']==key and item['interval_seconds']>0),None)
        if item is None:raise ValueError('定时键未启用')
        index=next(i for i,item_ in enumerate(self.slots) if item_ is item)
        self.next_due[index]=pressed_at+item['interval_seconds']
        return item['hold_seconds']+item.get('settle_seconds',.8)


def bind_timed_special_keys(keyboard,keys,slots):
    for item in normalize_timed_keys(slots):
        key=item['key']
        if item['interval_seconds']>0 and len(key)>1:
            keyboard.special[key]=getattr(keys,key)


class TimedInputExecutor(InputExecutor):
    def __init__(self,keyboard,config,*,timed_keys=(),**kwargs):
        super().__init__(keyboard,config,**kwargs)
        self.timed_holds={item['key']:item['hold_seconds'] for item in normalize_timed_keys(timed_keys) if item['interval_seconds']>0}

    def execute(self,action):
        if action.kind!='timed_key':return super().execute(action)
        self.stop();sent=False
        try:
            self.check()
            if action.direction not in self.timed_holds:raise ValueError('定时键未配置或未启用')
            pressed_at=self.press(action.direction);sent=True
            self.delay(self.timed_holds[action.direction])
            return pressed_at
        except InputCancelled:
            if sent:return pressed_at
            raise
        finally:self.stop()
