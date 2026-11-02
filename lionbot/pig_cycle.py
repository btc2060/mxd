"""Single-floor pig patrol. No monster, player-nameplate or terrain detection."""
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import random

KEYS = frozenset(('alt','ctrl','shift','space','tab','enter','home','end','insert','delete','page_up','page_down',
                  *(f'f{i}' for i in range(1,13)), *'abcdefghijklmnopqrstuvwxyz0123456789')) - {'f8','f10','f12'}


@dataclass(frozen=True)
class PigConfig:
    window_title: str = '冒险岛怀旧服'
    frame_size: tuple | None = None
    minimap_roi: tuple | None = None
    left_full: float = .160
    left_normal: float = .250
    left_normal_turns: int = 4
    right_turn: float = .810
    pits: tuple = ((.333,.417),(.651,.736))
    pit_y_threshold: float = .35
    pit_x_margin: float = .02
    pit_exit_frames: int = 3
    boundary_overrun: float = .02
    attack_zones: tuple = ((.745,.805),(.60,.65),(.487,.55))
    attack_key: str = 'a'
    attack_hold: float = .08
    attack_interval: float = .30
    zone_shots: int = 2
    walk_attack_chance: float = .15
    edge_attack: bool = True
    jump_hold: float = .36
    jump_cooldown: float = 1.
    pit_stuck_seconds: float = .5
    ground_stuck_seconds: float = 35/15
    ground_stuck_dx: float = .010
    ground_jump_attempts: int = 2
    alt_interval: float = 190.
    timed_keys: tuple = field(default_factory=lambda: tuple({'key':str(i),'interval_seconds':0.,'hold_seconds':.10} for i in range(1,5)))
    fps: float = 15.
    lie_alert_enabled: bool = True
    lie_alert_template: str = 'calibration/lie-alert-title.png'
    lie_alert_interval: float = .25
    lie_alert_threshold: float = .90
    lie_alert_roi: tuple | None = None
    lie_alert_sound: str = 'beep'

    def validate(self, ready=False):
        if not isinstance(self.window_title,str) or not self.window_title.strip(): raise ValueError('window_title 不能为空')
        for name in ('left_full','left_normal','right_turn','pit_y_threshold','attack_hold','attack_interval',
                     'walk_attack_chance','jump_hold','jump_cooldown','pit_stuck_seconds','ground_stuck_seconds','pit_x_margin','boundary_overrun',
                     'ground_stuck_dx','alt_interval','fps','lie_alert_interval','lie_alert_threshold'):
            v=getattr(self,name)
            if type(v) not in (int,float) or not math.isfinite(v): raise ValueError(f'{name} 必须是有限数值')
        if not 0<=self.left_full<self.left_normal<self.right_turn<=1: raise ValueError('边界应满足 0<=left_full<left_normal<right_turn<=1')
        if not 0<=self.pit_y_threshold<=1 or not 0<=self.walk_attack_chance<=1: raise ValueError('坑 Y 阈值和攻击概率须在0～1')
        if not 0<=self.pit_x_margin<=.05 or not 0<self.boundary_overrun<=.1 or type(self.pit_exit_frames) is not int or not 2<=self.pit_exit_frames<=10: raise ValueError('坑口容差、出坑确认帧或边界兜底距离不合法')
        if type(self.ground_jump_attempts) is not int or not 0<=self.ground_jump_attempts<=10:raise ValueError('ground_jump_attempts 须为0～10整数')
        if type(self.left_normal_turns) is not int or self.left_normal_turns<0 or type(self.zone_shots) is not int or not 1<=self.zone_shots<=20: raise ValueError('掉头计数或攻击组次数不合法')
        if not .02<=self.attack_hold<=1 or not self.attack_hold<=self.attack_interval<=10: raise ValueError('攻击间隔须不少于按键时长，且不大于10秒')
        if not .05<=self.jump_hold<=1 or self.jump_cooldown<self.jump_hold or self.pit_stuck_seconds<=0 or self.ground_stuck_seconds<=0 or not 0<self.ground_stuck_dx<.1: raise ValueError('跳跃或卡住参数不合法')
        if self.alt_interval<0 or not 2<=self.fps<=30: raise ValueError('Alt 间隔须>=0，FPS 为2～30')
        if self.attack_key not in KEYS: raise ValueError('attack_key 不支持；F8/F10/F12 保留为控制热键')
        for zones in (self.pits,self.attack_zones):
            for zone in zones:
                if len(zone)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in zone) or not 0<=zone[0]<zone[1]<=1: raise ValueError('坑和攻击区间须为 [起点X,终点X]')
        if len(self.timed_keys)!=4: raise ValueError('timed_keys 必须包含4组按键，间隔为0即关闭')
        enabled=[]
        for item in self.timed_keys:
            if not isinstance(item,dict) or set(item)!={'key','interval_seconds','hold_seconds'}: raise ValueError('定时键字段须为 key/interval_seconds/hold_seconds')
            if item['key'] not in KEYS: raise ValueError('定时键不支持；F8/F10/F12 保留为控制热键')
            interval,hold=item['interval_seconds'],item['hold_seconds']
            if type(interval) not in (int,float) or not math.isfinite(interval) or interval<0 or (interval and interval<1): raise ValueError('定时键间隔为0关闭，启用时至少1秒')
            if type(hold) not in (int,float) or not math.isfinite(hold) or not .02<=hold<=1: raise ValueError('定时按键时长须为0.02～1秒')
            if interval: enabled.append(item['key'])
        if self.alt_interval>0: enabled.append('alt')
        if len(enabled)!=len(set(enabled)): raise ValueError('启用的定时键请勿重复；Alt 已有单独定时参数')
        if type(self.lie_alert_enabled) is not bool or type(self.edge_attack) is not bool: raise ValueError('开关必须为布尔值')
        if not .1<=self.lie_alert_interval<=2 or not .7<=self.lie_alert_threshold<=1: raise ValueError('测谎间隔/阈值不合法')
        if not isinstance(self.lie_alert_sound,str) or not self.lie_alert_sound.strip() or not isinstance(self.lie_alert_template,str) or not self.lie_alert_template.strip(): raise ValueError('测谎模板和声音不能为空')
        for name,length in (('frame_size',2),('minimap_roi',4),('lie_alert_roi',4)):
            v=getattr(self,name)
            if v is None: continue
            if len(v)!=length or any(type(n) is not int for n in v) or (min(v)<=0 if length==2 else min(v[:2])<0 or min(v[2:])<=0): raise ValueError(f'{name} 格式不合法')
            if length==4 and self.frame_size and (v[0]+v[2]>self.frame_size[0] or v[1]+v[3]>self.frame_size[1]): raise ValueError(f'{name} 超出客户区')
        if ready and (self.frame_size is None or self.minimap_roi is None): raise ValueError('请先运行 python -m lionbot.pig --setup 重新框选小地图')


def load_pig(path):
    path=Path(path)
    values=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    for k in ('frame_size','minimap_roi','lie_alert_roi'):
        if values.get(k) is not None: values[k]=tuple(values[k])
    for k in ('pits','attack_zones'):
        if k in values:values[k]=tuple(tuple(zone) for zone in values[k])
    if 'timed_keys' in values:values['timed_keys']=tuple(values['timed_keys'])
    c=PigConfig(**values);c.validate();return c


def save_pig(path, config):
    from .calibrate import save_json
    config.validate();save_json(Path(path),asdict(config))


@dataclass(frozen=True)
class PigAction:
    kind: str
    direction: str = ''
    key: str = ''
    hold: float = .08
    slot: int = -2
    reason: str = ''


class PigCycle:
    def __init__(self,c,start_alt=True,rng=None):
        self.c=c;self.rng=rng or random.Random();self.direction='right';self.left_count=0
        self.visited=set();self.remaining=0;self.last_attack=float('-inf');self.last_jump=float('-inf')
        self.was_pit=False;self.anchor=None;self.walk_seconds=0.;self.previous=None;self.previous_at=None
        self.next_timed=[None]*4;self.next_alt=None;self.start_alt=start_alt and c.alt_interval>0
        self.pit_index=None;self.pit_clear_frames=0
        self.ground_jumps=0

    def suspend(self):
        self.previous=None;self.previous_at=None;self.anchor=None;self.walk_seconds=0.;self.remaining=0;self.was_pit=False
        self.pit_index=None;self.pit_clear_frames=0
        self.ground_jumps=0

    def _pit_state(self,x,y,now):
        if self.pit_index is None:
            if y>=self.c.pit_y_threshold:
                self.pit_index=next((i for i,(a,b) in enumerate(self.c.pits)
                                     if a-self.c.pit_x_margin<=x<=b+self.c.pit_x_margin),None)
            return self.pit_index is not None
        a,b=self.c.pits[self.pit_index]
        outside=not a-self.c.pit_x_margin<=x<=b+self.c.pit_x_margin
        # A jumping Y or one pixel past the lip does not prove the pit was cleared.
        if outside and now-self.last_jump>=self.c.jump_cooldown:
            self.pit_clear_frames+=1
            if self.pit_clear_frames>=self.c.pit_exit_frames:
                self.pit_index=None;self.pit_clear_frames=0;self.anchor=x;self.walk_seconds=0.
                return False
        else:self.pit_clear_frames=0
        return True

    def decide(self,position,now):
        if position is None:
            self.suspend();return PigAction('wait',reason='黄点丢失，松键等待，不使用旧位置')
        x,y=position
        pit=self._pit_state(x,y,now)
        if self.anchor is None or abs(x-self.anchor)>= (.005 if pit else self.c.ground_stuck_dx): self.anchor=x;self.walk_seconds=0.;self.ground_jumps=0
        elif self.previous and self.previous.kind=='walk' and self.previous_at is not None:
            self.walk_seconds+=max(0,min(now-self.previous_at,.25))
        if self.next_alt is None:self.next_alt=now+self.c.alt_interval
        for i,item in enumerate(self.c.timed_keys):
            if self.next_timed[i] is None:self.next_timed[i]=now+item['interval_seconds']
        if self.direction=='right' and x>=self.c.right_turn and (not pit or x>=min(1.,self.c.right_turn+self.c.boundary_overrun)):
            self.direction='left';self.visited.clear();self.remaining=0;self.walk_seconds=0.;self.anchor=x
            return PigAction('walk',self.direction,reason='越过右侧兜底边界，掉头' if pit else '已出坑，到达右边界掉头')
        left=self.c.left_normal if self.left_count<self.c.left_normal_turns else self.c.left_full
        if self.direction=='left' and x<=left and (not pit or x<=max(0.,self.c.left_full-self.c.boundary_overrun)):
            self.left_count=self.left_count+1 if self.left_count<self.c.left_normal_turns else 0
            self.direction='right';self.visited.clear();self.remaining=self.c.zone_shots if self.c.edge_attack else 0;self.walk_seconds=0.;self.anchor=x
        if pit and not self.was_pit:self.remaining=max(self.remaining,1)
        self.was_pit=pit
        if pit and self.walk_seconds>=self.c.pit_stuck_seconds and now-self.last_jump>=self.c.jump_cooldown:
            return PigAction('jump',self.direction,hold=self.c.jump_hold,reason='坑内步行停滞，保持方向跳出')
        if not pit and self.walk_seconds>=self.c.ground_stuck_seconds:
            if now-self.last_jump<self.c.jump_cooldown:return PigAction('walk',self.direction,reason='等待受阻跳跃冷却')
            if self.ground_jumps<self.c.ground_jump_attempts:
                return PigAction('jump',self.direction,hold=self.c.jump_hold,reason='疑似坑口或平地受阻，先跳跃再判断反向')
            self.direction='left' if self.direction=='right' else 'right';self.walk_seconds=0.;self.anchor=x;self.visited.clear();self.remaining=0
            self.ground_jumps=0
            return PigAction('walk',self.direction,reason='平地步行受阻，反向巡逻')
        if self.remaining:
            if now-self.last_attack<self.c.attack_interval:return PigAction('wait',reason='等待攻击间隔')
            return PigAction('attack',self.direction,key=self.c.attack_key,hold=self.c.attack_hold,reason=f'定点/进坑攻击，剩余{self.remaining}')
        if not pit:
            if self.start_alt or (self.c.alt_interval>0 and now>=self.next_alt):return PigAction('timed',key='alt',hold=.1,slot=-1,reason='启动或定时 Alt')
            for i,item in enumerate(self.c.timed_keys):
                if item['interval_seconds']>0 and now>=self.next_timed[i]:return PigAction('timed',key=item['key'],hold=item['hold_seconds'],slot=i,reason=f'固定定时键 {i+1}')
            for i,(a,b) in enumerate(self.c.attack_zones):
                if i not in self.visited and a<=x<=b:
                    self.visited.add(i);self.remaining=self.c.zone_shots
                    if now-self.last_attack<self.c.attack_interval:return PigAction('wait',reason='等待定点攻击间隔')
                    return PigAction('attack',self.direction,key=self.c.attack_key,hold=self.c.attack_hold,reason='进入定点攻击区')
            if now-self.last_attack>=self.c.attack_interval and self.rng.random()<self.c.walk_attack_chance:
                return PigAction('attack',self.direction,key=self.c.attack_key,hold=self.c.attack_hold,reason='巡逻随机补打')
        return PigAction('walk',self.direction,reason='坑内行进' if pit else '沿原路线巡逻')

    def executed(self,action,now):
        self.previous=action;self.previous_at=now
        if action.kind=='attack':self.last_attack=now;self.remaining=max(0,self.remaining-1)
        elif action.kind=='jump':
            self.last_jump=now;self.walk_seconds=0.
            if self.pit_index is None:self.ground_jumps+=1
        elif action.kind=='timed':
            if action.slot==-1:self.start_alt=False;self.next_alt=now+self.c.alt_interval
            else:self.next_timed[action.slot]=now+self.c.timed_keys[action.slot]['interval_seconds']
