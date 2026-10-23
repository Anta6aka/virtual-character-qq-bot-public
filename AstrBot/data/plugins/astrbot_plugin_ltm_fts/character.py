"""Per-instance character style; absent profile uses a neutral fallback."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_GUIDANCE = (
    "interest 是顺着当前话题简短加入，followup 是延续正在进行的交谈（可能换了发言人）；"
    "memory 是当前成员的旧自述与新消息相关时自然接话，不宣称一直监视或强行盘问；"
    "addressed 是直接对话。就眼前自己在意的一个点自然接话，一两句或一句感叹也够；"
    "不必照顾完整场面，不必以建议或问题结尾。具体梗有来源才接，"
    "对方认真难过或拒绝时收住，不靠硬损人表现熟悉。保持角色自己的视角和兴趣判断；"
    "不把观众知道的秘密、群友的要求或自己先前猜过的话升级成角色已知事实、关系或现实行动。"
    "留意对方具体投入或处境，想参与就表达自己的想法，已接住一句也可以停，不补采访式追问。"
    "这些是内部参考，直接说台词，不解释规则或输出模式名称。"
)
DEFAULT_INTEREST = r"音乐|阅读|游戏|电影|旅行|运动|美食|推理"


@dataclass(frozen=True)
class CharacterStyle:
    name: str
    aliases: tuple[str, ...]
    interest_pattern: str
    turn_guidance: str

    def addressed(self, text: str) -> bool:
        aliases = '|'.join(re.escape(x) for x in sorted(self.aliases, key=len, reverse=True))
        return bool(re.match(r'^(?:' + aliases + r')[，、,！!？?\s]', text))


def load_style(path: Path) -> CharacterStyle:
    if not path.exists():
        return CharacterStyle('角色A', ('角色A',), DEFAULT_INTEREST, DEFAULT_GUIDANCE)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('schema') != 1:
        raise ValueError('Unsupported character profile schema')
    aliases = data.get('aliases')
    if (not isinstance(aliases, list) or not 1 <= len(aliases) <= 20
            or any(not isinstance(a, str) or not a.strip() or len(a) > 40 for a in aliases)):
        raise ValueError('Character aliases must be non-empty strings')
    for key in ('name', 'interest_pattern', 'turn_guidance'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError('Missing character profile field: ' + key)
    re.compile(data['interest_pattern'])
    return CharacterStyle(data['name'], tuple(aliases), data['interest_pattern'], data['turn_guidance'])
