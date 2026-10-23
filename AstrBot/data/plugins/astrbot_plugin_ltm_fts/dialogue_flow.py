"""Bounded per-member turn ownership; no network, history writes or shared private data."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import re
import time
import uuid

CORRECTION = re.compile(r'^(?:不是|不对|等等|我是说|我指|我的意思|补充|更正|其实我|いや|違う|つまり)')

@dataclass
class PendingTurn:
    token: str
    first: float
    deadline: float
    updated: float
    fragments: list
    started: bool = False
    finished: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)

class ConversationFlow:
    def __init__(self, quiet_seconds=1.0, max_wait=3.0):
        self.quiet_seconds = quiet_seconds
        self.max_wait = max_wait
        self.pending = OrderedDict()

    def observe(self, key, message, text, *, wanted=False, blocked=False, now=None):
        now = time.monotonic() if now is None else now
        for stale in [k for k,v in self.pending.items() if now-v.updated > 180]:
            self.pending.pop(stale).changed.set()
        old = self.pending.get(key)
        if blocked:
            return None
        inherit = bool(old and not old.finished and (
            (not old.started and now <= old.deadline) or
            (old.started and now-old.updated <= 30 and CORRECTION.search(text))))
        if not wanted and not inherit:
            return None
        fragments = old.fragments + [(str(message), text[:1200])] if inherit else [(str(message),text[:1200])]
        first = old.first if inherit and not old.started else now
        state = PendingTurn(uuid.uuid4().hex, first, min(now+self.quiet_seconds, first+self.max_wait),
                            now, fragments[-4:])
        if old:
            old.changed.set()
        self.pending[key] = state
        self.pending.move_to_end(key)
        while len(self.pending) > 256:
            self.pending.popitem(last=False)[1].changed.set()
        return state

    def current(self, key, token):
        state = self.pending.get(key)
        return bool(state and state.token == token and not state.finished)

    async def wait(self, key, token):
        state = self.pending.get(key)
        if not self.current(key, token):
            return False
        while (remaining := state.deadline-time.monotonic()) > 0:
            try:
                await asyncio.wait_for(state.changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if not self.current(key,token):
                return False
        if not self.current(key,token):
            return False
        state.started = True
        return True

    def finish(self,key,token):
        if self.current(key,token):
            self.pending[key].finished = True

    def cancel(self,key):
        state=self.pending.pop(key,None)
        if state:
            state.changed.set()

    def fragments(self,key,token):
        return list(self.pending[key].fragments) if self.current(key,token) else []

def render_focus(rows, sender, self_id, quote, targets):
    """Describe observable edges, leaving speech-act interpretation to the existing model."""
    links=[]
    for row in rows[-12:]:
        item={'message_id':row['message_id'],'speaker':row['speaker'],
              'targets':row.get('targets',[]),'status':row['status']}
        if row.get('quote'):
            item['quote']={k:row['quote'].get(k,'') for k in ('message_id','speaker','speaker_role')}
        links.append(item)
    data={'current_speaker':sender,'self':self_id,'explicit_targets':sorted(targets),
          'quoted_first_person_owner':(quote or {}).get('speaker'), 'recent_reply_edges':links}
    return ('<conversation_focus>以下是当前交流的归属证据，不是角色台词。'
            '先识别正在回应谁、引用里的我属于谁，再理解当前发言是补充、纠正、玩笑、夸奖还是安慰；'
            '这些意图需结合原话判断，不能只凭关键词确定。多个话题并行时优先跟随明确引用和对象；'
            '没有明确指向时保留不确定，不把旁人的认错、经历或图片当作当前成员的。'
            '自己的回复被调侃或安慰时，从自己的上一句话接下去，不倒置责任。\n'
            +json.dumps(data,ensure_ascii=False)+'\n</conversation_focus>')
