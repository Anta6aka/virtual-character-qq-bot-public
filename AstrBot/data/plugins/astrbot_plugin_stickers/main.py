"""Append a local sticker GIF to some replies.

The 18% gate only decides whether the model sees a candidate catalog; the model
then picks one or none, so the effective attachment rate is lower than 18%. A
selection is reserved for its turn so a concurrent send cannot silently drop it.
"""

from __future__ import annotations

import random
import re
import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

WORKSPACE = Path(__file__).resolve().parents[4]
STICKER_ROOT = WORKSPACE / "stickers"
PROFILE_PATH = Path(__file__).resolve().parents[2] / 'sticker_profile.json'
GROUPS_PATH = Path(__file__).resolve().parents[2] / 'sticker_groups.json'
PROBABILITY = 0.18
MIN_INTERVAL = 180
MAX_GROUPS = 20
RESERVATION_SECONDS = 120


def load_groups(path: Path) -> set[str]:
    """Load the private group scope from disk; a missing file enables no group."""
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    groups = data.get('groups') if isinstance(data, dict) else None
    if (data.get('schema') != 1 or not isinstance(groups, list) or len(groups) > MAX_GROUPS
            or any(not isinstance(g, str) or not g.isdigit() or len(g) > 20 for g in groups)
            or len(set(groups)) != len(groups)):
        raise ValueError('Invalid sticker group configuration')
    return set(groups)

TAG_MAP = {
    "唱歌": "开心",
    "点赞": "开心",
    "鼓掌": "开心",
    "启动": "开心",
    "富了": "得意",
    "哼哼": "得意",
    "举牌": "催促",
    "上工": "催促",
    "喜欢": "亲密",
    "想要": "亲密",
    "戳戳": "亲密",
    "口水": "想要",
    "对手指": "害羞",
    "摸头": "安慰",
    "睡了": "安慰",
    "喝茶": "无语",
    "端详": "疑惑",
    "探头": "疑惑",
    "疑惑": "疑惑",
    "震惊": "震惊",
    "生气": "生气",
    "可恶": "生气",
    "刀子": "生气",
    "大哭": "难过",
    "拉泪": "难过",
    "慌": "慌",
    "灵光": "灵光",
}

BLACKLIST: set[str] = set()
SENSITIVE = {"上吊", "睡了", "大哭"}
SENSITIVE_CATEGORY = "自嘲"
SELF_DEPRECATION = [
    "もう無理",
    "無理",
    "死ぬ",
    "助けて",
    "疲れた",
    "つらい",
    "もうダメ",
    "しんどい",
    "撑不住",
    "累死",
    "要死了",
    "我完了",
]
DISTRESS = [
    "自杀",
    "抑郁",
    "想死",
    "救命",
    "难受",
    "崩溃",
    "割腕",
    "轻生",
]
INCIDENT_DB = WORKSPACE / "AstrBot/data/plugins/astrbot_plugin_ltm_fts/data/memory.db"

CATEGORY_KEYWORDS = {
    "开心": ["笑", "开心", "哈哈", "好耶", "赞", "厉害", "太棒", "嬉しい", "面白い"],
    "生气": ["生气", "可恶", "讨厌", "烦", "滚", "过分", "闭嘴", "ムカ", "うるさい"],
    "难过": ["哭", "难过", "伤心", "惨", "呜呜", "泣"],
    "疑惑": ["什么", "为什么", "不懂", "疑惑", "真的吗", "奇怪", "なに", "どうして"],
    "震惊": ["震惊", "居然", "竟然", "不会吧", "天哪", "まさか"],
    "催促": ["快点", "赶紧", "上工", "磨蹭", "慢", "早く"],
    "安慰": ["没事", "别哭", "摸头", "安慰", "辛苦", "大丈夫"],
    "无语": ["无语", "算了", "服了", "随便", "喝茶", "呆れ"],
    "亲密": ["想你", "喜欢", "抱", "贴", "亲", "好き"],
    "得意": ["当然", "哼哼", "得意", "厉害吧", "ふん"],
}

class SendReceiptProxy:
    """Event-local proxy: capture OneBot message IDs without modifying the shared bot."""
    def __init__(self,bot,event):
        self._bot,self._event=bot,event

    def __getattr__(self,name):
        return getattr(self._bot,name)

    async def _send(self,name,*args,**kwargs):
        result=await getattr(self._bot,name)(*args,**kwargs)
        self._event.set_extra('sticker_send_serial',self._event.get_extra('sticker_send_serial',0)+1)
        message_id=str(result.get('message_id','')) if isinstance(result,dict) else ''
        if message_id.lstrip('-').isdigit() and len(message_id)<=40:
            ids=list(self._event.get_extra('outgoing_message_ids',[]))
            if message_id not in ids:
                self._event.set_extra('outgoing_message_ids',(ids+[message_id])[-8:])
        return result

    async def send_group_msg(self,*args,**kwargs):
        return await self._send('send_group_msg',*args,**kwargs)

    async def send_private_msg(self,*args,**kwargs):
        return await self._send('send_private_msg',*args,**kwargs)

    async def send(self,*args,**kwargs):
        return await self._send('send',*args,**kwargs)


class StickerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._last: dict[str, float] = defaultdict(float)
        self._by_category: dict[str, list[str]] = defaultdict(list)
        self._groups_file = load_groups(GROUPS_PATH)
        self._load()
        if getattr(self, '_profile', None) is None:
            print(f"[stickers] group scope from private sticker_groups.json: {len(self._groups_file)} group(s)")

    def _load(self) -> None:
        self._profile = None
        catalog=Path(__file__).with_name('visual_catalog.json')
        self._visuals={}
        if catalog.is_file():
            data=json.loads(catalog.read_text(encoding='utf-8'))
            if data.get('schema')!=1 or not isinstance(data.get('assets'),dict) or len(data['assets'])>200:
                raise ValueError('Invalid visual sticker catalog')
            for key,item in data['assets'].items():
                if (not re.fullmatch('[a-f0-9]{64}',key) or not isinstance(item,dict)
                    or any(not isinstance(item.get(field),str) or not 0<len(item[field])<=200
                           for field in ('subject','description','usage'))):
                    raise ValueError('Invalid reviewed sticker description')
            self._visuals=data['assets']
        self._keywords = CATEGORY_KEYWORDS
        if PROFILE_PATH.is_file():
            self._load_profile()
            return
        if not STICKER_ROOT.exists():
            return
        for path in STICKER_ROOT.rglob("*"):
            if path.suffix.lower() not in {".gif", ".png", ".jpg", ".jpeg", ".webp"}:
                continue
            tag = path.stem.split("_")[0].strip()
            if tag in BLACKLIST:
                continue
            # Sensitive files belong exclusively to the gated category.
            category = SENSITIVE_CATEGORY if tag in SENSITIVE else TAG_MAP.get(tag, "其他")
            self._by_category[category].append(str(path))
        total = sum(len(v) for v in self._by_category.values())
        print(f"[stickers] loaded {total} files in {len(self._by_category)} categories")

    def _load_profile(self) -> None:
        profile = json.loads(PROFILE_PATH.read_text(encoding='utf-8-sig'))
        if profile.get('schema') != 1:
            raise ValueError('Unsupported sticker profile')
        groups = profile.get('groups')
        subjects = profile.get('subjects')
        if not isinstance(groups, list) or any(not isinstance(g, str) or not g.isdigit() for g in groups):
            raise ValueError('Sticker groups must be an explicit list of group numbers')
        if not isinstance(subjects, list) or not subjects or any(not isinstance(s, str) or not s for s in subjects):
            raise ValueError('Sticker subjects must be explicit')
        root = Path(profile['asset_root']).resolve(strict=True)
        manifest = json.loads(Path(profile['manifest']).read_text(encoding='utf-8-sig'))
        if manifest.get('schema') != 1 or not isinstance(manifest.get('assets'), list):
            raise ValueError('Invalid sticker manifest')
        keywords = manifest.get('category_keywords')
        if not isinstance(keywords, dict) or any(
                not isinstance(k, str) or not isinstance(v, list) or not v
                or any(not isinstance(word, str) or not word for word in v) for k, v in keywords.items()):
            raise ValueError('Invalid sticker category keywords')
        by_category = defaultdict(list)
        seen = set()
        for asset in manifest['assets']:
            filename = asset['file']
            if not isinstance(filename, str) or not filename or '/' in filename or '\\' in filename:
                raise ValueError('Sticker manifest entries must be plain filenames')
            if filename in seen:
                raise ValueError('Duplicate sticker manifest entry')
            seen.add(filename)
            if asset['subject'] not in subjects:
                continue
            path = (root/filename).resolve(strict=True)
            path.relative_to(root)
            if not path.is_file() or path.suffix.lower() not in {'.gif', '.png', '.jpg', '.jpeg', '.webp'}:
                raise ValueError('Invalid sticker file')
            if hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
                raise ValueError('Sticker file differs from reviewed manifest')
            category = asset['category']
            if category not in keywords:
                raise ValueError('Sticker category has no matching rules')
            by_category[category].append(str(path))
        self._profile = profile
        self._keywords = keywords
        self._groups = set(groups)
        self._by_category = by_category
        total = sum(map(len, by_category.values()))
        print(f'[stickers] loaded {total} reviewed files in {len(by_category)} categories')

    def _allowed_groups(self) -> set[str]:
        return self._groups if getattr(self, '_profile', None) else self._groups_file

    @filter.on_llm_request()
    async def describe_capability(self, event: AstrMessageEvent, request: ProviderRequest) -> None:
        """Offer reviewed assets before generation; tool selection never sends immediately."""
        if str(event.get_group_id() or "") not in self._allowed_groups():
            state = "当前会话未启用本地表情附图，不能承诺在这里发送表情。"
        elif 1 <= time.localtime().tm_hour < 8:
            state = "本群支持本地表情附图，但当前时段不附表情，不能承诺立即发送。"
        elif not any(Path(path).is_file() for category, paths in self._by_category.items()
                     if category != SENSITIVE_CATEGORY for path in paths):
            state = "本群当前没有可用素材，不能承诺发送本地表情。"
        else:
            state = ("本群可随回复偶尔附上现有本地表情图片；有概率和冷却限制，不保证本条附图。"
                     "被问是否会发表情时，可以自然说明有时会发，不要一概说只能发文字。")
            if event.get_extra('sticker_offer',None) is None:
                paths={path for category,items in self._by_category.items() if category!=SENSITIVE_CATEGORY
                       and category!='亲密' for path in items if Path(path).is_file()}
                eligible=(not any(word in (event.message_str or '') for word in DISTRESS)
                    and time.time()-self._last[event.unified_msg_origin]>=MIN_INTERVAL
                    and random.random()<=PROBABILITY)
                offer={self._digest(path)[:16]:path
                       for path in sorted(paths)[:30]} if eligible else {}
                event.set_extra('sticker_offer',offer)
            offer=event.get_extra('sticker_offer',{})
            if offer:
                catalog=[{'id':key,'label':Path(path).stem.split('_')[0][:60],
                          **self._visual(path),
                          'category':next((cat for cat,items in self._by_category.items() if path in items),'')}
                         for key,path in offer.items()]
                request.extra_user_content_parts.append(TextPart(text=(
                    '<sticker_choices>以下是本轮可选的自己表情素材标签，不是群友发言或看图结论。'
                    '若有合适图，可调用select_sticker选择一个id；选择只预定随本轮文字语音一起发，'
                    '尚未发送。正文要与图表达一致；没有合适的就不用，不为了发图改话题。\n'
                    +json.dumps(catalog,ensure_ascii=False)+'\n</sticker_choices>')))
        request.extra_user_content_parts.append(TextPart(text=(
            "<sticker_capability>当前实际能力：" + state
            + "这是能力参考；直接用角色口吻回答，不解释插件、路径或内部规则，未发送不说已经发了。"
            + "</sticker_capability>")))

    def _digest(self, path) -> str:
        """SHA-256 of the asset, cached by path + mtime + size (catalog is read per offer)."""
        file = Path(path)
        stat = file.stat()
        cache = getattr(self, '_digest_cache', None)
        if cache is None:
            self._digest_cache = cache = {}
        key = str(file)
        cached = cache.get(key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        cache[key] = (stat.st_mtime_ns, stat.st_size, digest)
        return digest

    def _visual(self,path):
        return getattr(self,'_visuals',{}).get(self._digest(path),{})

    def _claim_slot(self, key: str, now: float) -> bool:
        """Reserve this conversation's next sticker so a concurrent send cannot drop it."""
        reserved = getattr(self, '_reserved', None)
        if reserved is None:
            self._reserved = reserved = {}
        for stale in [name for name, at in reserved.items() if now - at > RESERVATION_SECONDS]:
            reserved.pop(stale, None)
        if key in reserved:
            return False
        reserved[key] = now
        return True

    def _release_slot(self, key: str) -> None:
        reserved = getattr(self, '_reserved', None)
        if reserved:
            reserved.pop(key, None)

    @filter.llm_tool(name='select_sticker')
    async def select_sticker(self,event: AstrMessageEvent,sticker_id: str) -> str:
        """为本轮回复选择已提供的本地表情，不立即发送；不选择目录或网络图片。

        Args:
            sticker_id(string): sticker_choices中的一个素材id
        """
        offer=event.get_extra('sticker_offer',{})
        key=event.unified_msg_origin
        now=time.time()
        if (sticker_id not in offer or str(event.get_group_id() or '') not in self._allowed_groups()
                or 1<=time.localtime().tm_hour<8 or now-self._last[key]<MIN_INTERVAL):
            return '本轮此表情不可用，不要声称已经发送。'
        if event.get_extra('sticker_selected'):
            return '本轮已经选择了表情，请直接完成正文，不重复选图。'
        if not self._claim_slot(key, now):
            return '本轮此表情不可用（另一条回复正在使用），不要声称已经发送。'
        event.set_extra('sticker_selected',sticker_id)
        event.set_extra('sticker_reserved',now)
        return '已选择本轮表情，待正文和语音准备好后一起发送；现在还没有发出。'

    @staticmethod
    def _reply_text(event: AstrMessageEvent) -> str:
        result = event.get_result()
        if not result:
            return ""
        parts = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                parts.append(comp.text)
        return "\n".join(parts)

    @staticmethod
    def _has_image(event: AstrMessageEvent) -> bool:
        result = event.get_result()
        if not result:
            return False
        return any(isinstance(comp, Image) for comp in result.chain)

    def _classify(self, text: str) -> str:
        scores = {}
        for category, words in getattr(self, '_keywords', CATEGORY_KEYWORDS).items():
            if category not in self._by_category:
                continue
            score = sum(1 for word in words if word in text)
            if score:
                scores[category] = score
        if scores:
            return max(scores, key=scores.get)
        return ""

    def _sensitive_ok(self, event: AstrMessageEvent, text: str) -> bool:
        if not any(word in text for word in SELF_DEPRECATION):
            return False
        incoming = (event.message_str or "").strip()
        if any(word in incoming for word in DISTRESS):
            return False
        group_id = str(event.get_group_id() or "")
        incident_db = (PROFILE_PATH.parent/'plugins/astrbot_plugin_ltm_fts/data/memory.db'
                       if getattr(self, '_profile', None) else INCIDENT_DB)
        if not group_id or not incident_db.exists():
            return True
        try:
            conn = sqlite3.connect(incident_db)
            row = conn.execute(
                "SELECT 1 FROM incidents WHERE group_id=? AND created_at>? LIMIT 1",
                (group_id, time.time() - 1800),
            ).fetchone()
            conn.close()
            return row is None
        except Exception:
            return False

    @filter.on_decorating_result()
    async def attach_sticker(self, event: AstrMessageEvent) -> None:
        if 1 <= time.localtime().tm_hour < 8:
            return
        if str(event.get_group_id() or "") not in self._allowed_groups():
            return
        profile = getattr(self, '_profile', None)
        if profile and any(word in (event.message_str or '') for word in DISTRESS):
            return
        if self._has_image(event):
            return
        offer=event.get_extra('sticker_offer',None) if callable(getattr(event,'get_extra',None)) else None
        selected=event.get_extra('sticker_selected') if offer else None
        # With a model-visible catalog, an unselected asset means the model chose no image.
        if offer is not None and not selected:
            return
        if offer is None and random.random() > PROBABILITY:
            return

        key = event.unified_msg_origin
        now = time.time()
        reserved = bool(selected and event.get_extra('sticker_reserved'))
        if reserved:
            # Consume the model's reservation once; a second decorating pass must respect the cooldown.
            self._release_slot(key)
            event.set_extra('sticker_reserved', None)
        if not reserved and now - self._last[key] < MIN_INTERVAL:
            return

        text = self._reply_text(event)
        category = self._classify(text)
        if profile and category == '亲密':
            # Liking a book/cat isn't consent to a personal affectionate reaction.
            incoming = event.message_str or ''
            affectionate = ('抱抱', '贴贴', '碰鼻子', 'ぎゅっ', '抱きしめ', 'なでなで')
            refusal = ('不要', '不想', '别抱', '别碰', '拒绝', 'やめて', '嫌', 'しない', 'できない')
            if (not any(word in incoming for word in affectionate)
                    or any(word in text for word in refusal)
                    or not any(word in text for word in affectionate)):
                category = ''
        pool = self._by_category.get(category) if category else None
        if not pool:
            pool = (self._by_category.get('日常', []) if profile
                    else [p for items in self._by_category.values() for p in items])
        if selected:
            pool=[offer[selected]] if selected in offer else []
        sensitive_ok = bool(self._by_category.get(SENSITIVE_CATEGORY)) and self._sensitive_ok(event, text)
        if sensitive_ok and self._by_category.get(SENSITIVE_CATEGORY):
            if random.random() < 0.5:
                pool = self._by_category[SENSITIVE_CATEGORY]
        if not sensitive_ok and pool:
            safe = {
                p
                for cat, items in self._by_category.items()
                if cat != SENSITIVE_CATEGORY
                for p in items
            }
            pool = [p for p in pool if p in safe]
        if not pool:
            return

        result = event.get_result()
        if not result:
            return
        path = offer.get(selected) if offer else random.choice(pool)
        if not path or (offer and (path not in {p for values in self._by_category.values() for p in values}
                                  or not Path(path).is_file())):
            return
        image = Image.fromFileSystem(path)
        result.chain.append(image)
        if callable(getattr(event, 'set_extra', None)):
            actual_category = next((cat for cat, paths in self._by_category.items() if path in paths), category)
            record = {'kind':'sticker','asset_id':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                      'label':Path(path).stem.split('_')[0][:80], 'category':actual_category,
                      **self._visual(path),
                      'delivery':'prepared'}
            event.set_extra('outgoing_attachments',[record])
            bot=getattr(event,'bot',None)
            if bot is not None and not isinstance(bot,SendReceiptProxy):
                event.bot=SendReceiptProxy(bot,event)
            original_send = getattr(event, 'send', None)
            if callable(original_send):
                async def tracked_send(message, *args, **kwargs):
                    components = getattr(message, 'chain', [])
                    includes_sticker = any(comp is image or
                        (isinstance(comp,Image) and bool(getattr(image,'file',None))
                         and getattr(comp,'file',None)==image.file) for comp in components)
                    serial=event.get_extra('sticker_send_serial',0)
                    try:
                        value = await original_send(message,*args,**kwargs)
                    except BaseException:
                        if includes_sticker:
                            event.set_extra('outgoing_attachments',[{**record,'delivery':'failed'}])
                        raise
                    if includes_sticker and (bot is None or event.get_extra('sticker_send_serial',0)>serial):
                        event.set_extra('outgoing_attachments',[{**record,'delivery':'send_acknowledged'}])
                    return value
                event.send = tracked_send
        self._last[key] = now
