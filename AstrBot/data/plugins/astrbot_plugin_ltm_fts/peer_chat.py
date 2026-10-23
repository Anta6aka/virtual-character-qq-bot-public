"""Cross-process turn leases for short character exchanges; never stores dialogue text."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

STOP = re.compile(r'^(?:停|停下|够了|别聊了|不用聊了|结束聊天|到此为止)[。！!\s]*$|(?:你们|你俩|两个机器人).{0,8}(?:别聊|停止|停下|安静)')
RESUME = re.compile(r'继续聊|接着聊|你们聊|你俩聊|两个机器人.{0,8}聊')
ENDING = re.compile(r'また今度|おやすみ|ここまでに|この話は終わり|先聊到这|下次再聊|不聊了|到此为止')
ACK = re.compile(r'^(?:うん|ええ|そうね|そうだね|なるほど|分かったわ?|はい|嗯|好[的吧]?|对[啊呀]?|是啊|哈哈+|确实|谢谢)[。！!、，,.…\s]*$')
CONTROL = re.compile(r'<peer_chat\b[^>\n]*(?:>|$)|</peer_chat\s*>',re.I)


def clean_control(text):
    markers = CONTROL.findall(text or '')
    votes = re.findall(r'''<peer_chat\s+next=["'](continue|stop)["']\s*/>''',text or '',re.I)
    vote = votes[0].lower() if len(markers)==1 and len(votes)==1 else ('stop' if markers else None)
    return CONTROL.sub('',text or '').strip(), vote


def fingerprint(text):
    japanese = clean_control(text)[0].split('【中文】',1)[0]
    value = re.sub(r'[\W_]+','',japanese).lower()[:1200]
    return hashlib.sha256(value.encode()).hexdigest(), {
        hashlib.sha256(value[i:i+2].encode()).hexdigest()[:16] for i in range(max(0,len(value)-1))}


def load_peer_chat(path, roles):
    if not path.exists():
        return None
    config=json.loads(path.read_text(encoding='utf-8-sig'))
    state=Path(config.get('state_path',''))
    if str(state) and not state.is_absolute():
        # A relative state_path is resolved against the private config file, so a restored
        # workspace keeps its shared coordinator inside its own tree.
        state=(path.parent/state).resolve()
    aliases=config.get('aliases',{})
    if (config.get('schema')!=1 or len(roles)<2 or not state.is_absolute() or state.suffix!='.db'
            or not isinstance(aliases,dict) or set(aliases)!=set(roles)
            or any(not isinstance(v,list) or not v or len(v)>20
                   or any(not isinstance(a,str) or not a.strip() or len(a)>40 for a in v)
                   for v in aliases.values())):
        raise ValueError('Invalid private peer_chat configuration')
    engine=PeerChat(state,roles,aliases)
    engine.invalidate_running()
    return engine


class PeerChat:
    # Emergency ceilings, not a desired conversation length.
    MAX_MESSAGES=24
    SESSION_SECONDS=600
    LEASE_SECONDS=180
    COOLDOWN_SECONDS=180

    def __init__(self,path,roles,aliases=None):
        self.path=Path(path)
        self.roles=dict(roles)
        self.aliases=aliases or {k:[v] for k,v in roles.items()}
        self.namespace=hashlib.sha256(json.dumps(roles,sort_keys=True).encode()).hexdigest()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path,timeout=3)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS peer_rounds(channel TEXT PRIMARY KEY,state TEXT NOT NULL,updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS peer_seen(channel TEXT,event TEXT,at REAL,PRIMARY KEY(channel,event))')

    def _key(self,group):
        return hashlib.sha256((self.namespace+':'+group).encode()).hexdigest()

    def invalidate_running(self):
        """A plugin restart has no public window; never resurrect an orphaned exchange."""
        with closing(sqlite3.connect(self.path,timeout=3)) as db,db:
            for channel,raw in db.execute('SELECT channel,state FROM peer_rounds').fetchall():
                state=json.loads(raw)
                state.update(active=False,lease=None)
                db.execute('UPDATE peer_rounds SET state=? WHERE channel=?',(json.dumps(state),channel))

    def _transaction(self,group,now,action):
        channel=self._key(group)
        with closing(sqlite3.connect(self.path,timeout=3,isolation_level=None)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row=db.execute('SELECT state FROM peer_rounds WHERE channel=?',(channel,)).fetchone()
                state=json.loads(row[0]) if row else {}
                if state.get('active') and (now>state['until'] or
                        (state.get('lease') and now-state['lease']['at']>self.LEASE_SECONDS)):
                    self._end(state,now)
                result=action(state,db,channel)
                db.execute('INSERT OR REPLACE INTO peer_rounds VALUES(?,?,?)',
                           (channel,json.dumps(state,sort_keys=True),now))
                db.execute('DELETE FROM peer_seen WHERE at<?',(now-86400,))
                db.execute('DELETE FROM peer_rounds WHERE updated<?',(now-86400,))
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _event(kind,speaker,message):
        return hashlib.sha256((kind+':'+speaker+':'+message).encode()).hexdigest()

    @staticmethod
    def _fresh(db,channel,event,now):
        return db.execute('INSERT OR IGNORE INTO peer_seen VALUES(?,?,?)',(channel,event,now)).rowcount==1

    def _end(self,state,now):
        state.update(active=False,lease=None,cooldown_until=now+self.COOLDOWN_SECONDS)

    def human(self,group,speaker,message,text,targets,now,permit=True):
        key=self._event('human',speaker,message)
        def action(state,db,channel):
            if not message or not self._fresh(db,channel,key,now):
                current=state.get('epoch')==key
                return {'epoch':state.get('epoch'),
                        'owner':state.get('owner') if current and state.get('used')==0 else None,
                        'active':bool(current and state.get('active'))}
            resume=bool(RESUME.search(text))
            invited=bool(set(targets)&self.roles.keys() or
                         any(a in text for names in self.aliases.values() for a in names) or resume)
            previous=state.get('last_speaker')
            cooldown=state.get('cooldown_until',0)
            addressed=set(targets)&self.roles.keys()
            invited_roles=addressed|{k for k,names in self.aliases.items() if any(a in text for a in names)}
            owner=(next(iter(addressed)) if len(addressed)==1 else
                   next((k for k in sorted(self.roles) if k!=previous),sorted(self.roles)[0])) if resume else None
            eligible=permit and invited and not STOP.search(text) and (resume or now>=cooldown)
            state.clear()
            state.update(epoch=key,active=bool(eligible),until=now+self.SESSION_SECONDS,
                         used=0,lease=None,last_speaker=previous,features=[],votes=[],
                         cooldown_until=cooldown,owner=owner if eligible else None,
                         invited_roles=sorted(invited_roles),started=False,night_invitation=resume)
            return {'epoch':key,'owner':state['owner'],'active':state['active']}
        return self._transaction(group,now,action)

    def _reserve(self,state,owner,now):
        if not state.get('active') or state.get('lease') or state['used']>=self.MAX_MESSAGES:
            return None
        token=uuid.uuid4().hex
        state['used']+=1
        state['started']=True
        state['lease']={'token':token,'owner':owner,'at':now,'hash':None,'vote':None}
        return {'token':token,'epoch':state['epoch'],'number':state['used']}

    def resume(self,group,owner,epoch,now):
        def action(state,db,channel):
            if state.get('epoch')==epoch and state.get('owner')==owner and state.get('used')==0:
                return self._reserve(state,owner,now)
        return self._transaction(group,now,action)

    def seed(self,group,epoch,owner,text,vote,now):
        def action(state,db,channel):
            if state.get('active') and state.get('epoch')==epoch:
                state['votes']=(state['votes']+[{'owner':owner,'hash':fingerprint(text)[0],'vote':vote}])[-4:]
        self._transaction(group,now,action)

    def peer(self,group,sender,message,text,self_id,now,permit=True):
        digest,features=fingerprint(text)
        def action(state,db,channel):
            if (sender not in self.roles or self_id not in self.roles or sender==self_id
                    or not message or not self._fresh(db,channel,self._event('peer',sender,message),now)
                    or not state.get('active') or (not permit and not state.get('night_invitation'))):
                return None
            lease=state.get('lease')
            matched=bool(lease and lease['owner']==sender and lease['hash']==digest)
            vote=lease['vote'] if matched else next((v['vote'] for v in reversed(state.get('votes',[]))
                                                      if v['owner']==sender and v['hash']==digest),None)
            if matched:
                state['lease']=None
            else:
                state['used']+=1  # Unreserved human-directed replies count too.
            same=any(features and len(features&set(old))/max(1,len(features|set(old)))>=0.8
                     for old in state.get('features',[]))
            speech=clean_control(text)[0].split('【中文】',1)[0].strip()
            state['last_speaker']=sender
            state['features']=(state.get('features',[])+[sorted(features)])[-4:]
            if (vote=='stop' or (vote is None and ENDING.search(text)) or ACK.fullmatch(speech) or same
                    or state['used']>=self.MAX_MESSAGES):
                self._end(state,now)
                return None
            if (not state.get('started') and not state.get('night_invitation')
                    and self_id not in state.get('invited_roles',[])
                    and not any(alias in text for alias in self.aliases[self_id])):
                return None  # A one-to-one human question does not invite every character to butt in.
            return self._reserve(state,self_id,now)
        return self._transaction(group,now,action)

    def generated(self,group,token,owner,text,vote,now):
        def action(state,db,channel):
            lease=state.get('lease')
            if state.get('active') and lease and lease['token']==token and lease['owner']==owner:
                lease.update(hash=fingerprint(text)[0],vote=vote)
                return True
            return False
        return self._transaction(group,now,action)

    def valid(self,group,token,now):
        return self._transaction(group,now,lambda s,db,c:bool(s.get('active') and s.get('lease') and s['lease']['token']==token))


GUIDANCE=('本群允许角色之间自然搭话，有话则长、无话则短，不以条数为目标。'
          '只接对方具体内容，允许接梗、追问、分歧和新想法；已经说完就收住，'
          '不靠互夸、重复、强行追问或不断另起话题维持聊天。'
          '保持本人人设，不替对方写台词。真人加入后优先回应真人。'
          '照常输出日语和【中文】译文；最后另起一行加内部标记'
          '<peer_chat next="continue"/>表示仍有具体内容可接，或'
          '<peer_chat next="stop"/>表示自然结束。标记会被程序移除，不要解释规则或提到轮次。')
