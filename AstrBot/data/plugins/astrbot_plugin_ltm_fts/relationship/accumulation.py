"""Pure, bounded familiarity allocation. No mood, trust or response decisions.

History is transient owner-scoped ledger/source data. The audit only retains
numeric credit and a coarse category, never another copy of private wording.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import unicodedata

VERSION = "accumulation-v1"
UNIT_SECONDS = 1200
HALF_LIFE_SECONDS = 21600
HISTORY_SECONDS = 259200
LOW_INFORMATION_GAIN = 0.25
LOW_INFORMATION_DAILY_CAP = 0.5
HISTORY_LIMIT = 2048
LOW_CATEGORIES = frozenset({"greeting", "courtesy"})


def normalise(text: str) -> str:
    value = unicodedata.normalize("NFKC", (text or "")[:1200]).casefold()
    return "".join(c for c in value if not c.isspace()
                   and unicodedata.category(c)[0] not in {"P", "Z", "C", "S"})


_GREETING = re.compile(
    r"(?:早|早安|早上好|早晨好|午安|晚上好|晚安|睡个好觉|好梦|你好|在吗|在不在|"
    r"今天(?:过得)?怎么样|今天过得如何|吃了吗|吃了没|hello|hi|goodmorning|goodnight|"
    r"こんにちは|おはよう(?:ございます)?|こんばんは|おやすみ(?:なさい)?)"
    r"(?:呀|啊|啦|哦|喔|呢|吗|哈)*")
_COURTESY = re.compile(
    r"(?:你(?:真|好|太|最)?(?:棒|厉害|聪明|可爱)|你好棒|太强了|好厉害|最喜欢你|爱你|么么哒|"
    r"我(?:永远|一直|会一直|都会)?支持你|我(?:永远|一直)站在你这边|谢谢(?:你)?|感谢你|辛苦了|"
    r"ありがとう(?:ございます)?|お疲れ(?:さま|様)(?:です)?)"
    r"(?:呀|啊|啦|哦|呢|了)*")


def ordinary_signature(text: str) -> str:
    # Preserve operators, case and internal punctuation in code/math/real facts.
    # Only superficial whitespace, zero-width format marks and sentence-ending
    # punctuation are ignored. Unknown paraphrases are deliberately not guessed.
    clean=unicodedata.normalize("NFKC",(text or "")[:1200])
    clean="".join(c for c in clean if not c.isspace() and unicodedata.category(c)!="Cf")
    return clean.rstrip("。！？!?，,~～")


def category(text: str) -> str:
    clean = normalise(text)
    clean = re.sub(r"^(?:角色A|角色B)[呀啊]?", "", clean)
    if _GREETING.fullmatch(clean):
        return "greeting"
    if _COURTESY.fullmatch(clean):
        return "courtesy"
    return "conversation"


def social_ritual(text: str) -> bool:
    return category(text) in LOW_CATEGORIES


@dataclass(frozen=True)
class Credit:
    gain: float
    unit_start: float
    category: str
    target: float
    load: float
    episode_eligible: bool

    def valid(self, at: float) -> bool:
        numbers=(self.gain,self.unit_start,self.target,self.load)
        return (all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in numbers)
                and 0 <= self.gain <= 1 and 0 <= self.target <= 1 and self.load >= 0
                and 0 <= at-self.unit_start < UNIT_SECONDS
                and self.category in {"greeting","courtesy","conversation"}
                and isinstance(self.episode_eligible,bool))

    def audit(self) -> dict:
        return dict(version=VERSION,gain=self.gain,unit_start=self.unit_start,
                    category=self.category,target=self.target,load=self.load,
                    episode_eligible=self.episode_eligible)


def restore_credit(audit: dict, *, awarded: float | None = None) -> Credit | None:
    data=audit.get("f_credit")
    if data is None:
        return None  # genuinely legacy fixed-unit evidence
    if not isinstance(data,dict) or data.get("version") != VERSION:
        raise ValueError("Unknown familiarity credit format")
    try:
        credit=Credit(**{key:data[key] for key in ("gain","unit_start","category","target","load","episode_eligible")})
        # Replaying a source correction must not top up a previously capped award.
        if awarded is not None:
            credit=Credit(min(credit.gain,max(0,float(awarded))),credit.unit_start,
                          credit.category,credit.target,credit.load,credit.episode_eligible)
        return credit
    except (KeyError,TypeError,ValueError) as error:
        raise ValueError("Invalid familiarity credit metadata") from error


@dataclass(frozen=True)
class Record:
    at: float
    gained: float
    text: str = ""
    fingerprint: str = ""
    credit: Credit | None = None
    meaningful: bool = False
    day: str = ""


def plan(text: str, at: float, day: str, history: list[Record]) -> Credit:
    """Offer only the unearned part of this member's current exchange unit.

    A familiar greeting can recur after recovery; F content dedupe/interval are
    replaced by this allocation, not multiplied on top. Daily F cap stays in
    the engine. T retains its existing source/dedup/cap rules.
    """
    if not isinstance(at,(int,float)) or isinstance(at,bool) or not math.isfinite(at):
        raise ValueError("Invalid accumulation timestamp")
    recent=[r for r in history if 0 <= at-r.at < HISTORY_SECONDS+UNIT_SECONDS]
    kind=category(text)
    signature=ordinary_signature(text)
    # Any accepted unit, including a meaningful exchange at the daily F cap,
    # anchors the fixed window. Midnight and changing scenes do not restart it.
    anchors=[r.credit.unit_start if r.credit else r.at for r in recent]
    active=[start for start in anchors if 0 <= at-start < UNIT_SECONDS]
    start=max(active) if active else float(at)
    in_unit=[r for r in recent if (r.credit.unit_start if r.credit else r.at)==start]
    earned=sum(r.gained for r in in_unit)
    prior_episode=any(r.meaningful for r in in_unit)
    # Coarse social rituals share a family; ordinary interests do not. Only
    # exact normalized ordinary wording repeats, not every mention of a topic.
    repeats={}
    for record in recent:
        unit=record.credit.unit_start if record.credit else record.at
        if unit==start or record.gained<=0 or not 0 <= start-record.at < HISTORY_SECONDS:
            continue
        previous_kind=record.credit.category if record.credit else category(record.text)
        matches=(previous_kind==kind and
                 (kind in LOW_CATEGORIES or (bool(signature) and signature==ordinary_signature(record.text))))
        if matches:
            repeats[unit]=max(repeats.get(unit,0),record.at)
    load=sum(2**(-(start-stamp)/HALF_LIFE_SECONDS) for stamp in repeats.values())
    base=LOW_INFORMATION_GAIN if kind in LOW_CATEGORIES else 1.0
    target=base/(1+load)
    gain=max(0.0,round(target,8)-earned)
    if kind in LOW_CATEGORIES:
        used=sum(r.gained for r in recent if r.day==day
                 and ((r.credit.category if r.credit else category(r.text)) in LOW_CATEGORIES))
        gain=min(gain,max(0.0,LOW_INFORMATION_DAILY_CAP-used))
    return Credit(round(gain,8),float(start),kind,round(target,8),round(load,8),
                  kind=="conversation" and not prior_episode and load==0)
