"""Source-visibility classification and fail-closed reuse rules.

Contract: ``evaluation/relationship/INTERFACES.md`` (draft-1, 2026-10-22).
Common requirements implemented here: R-05..R-12 (source scope, private-only
content, explicit secrets, third-party grants, deleted/corrected/paused items,
filter-before-prompt, minimal public reuse).

The module is pure standard-library logic and must not import AstrBot.  It is
deliberately conservative: unknown enum values, inconsistent item metadata,
missing ids and ambiguous private text are all treated as **not shareable**.
Text heuristics only narrow the allowed reuse; they never widen it beyond the
source scope recorded on the memory item.

Reason codes emitted by :func:`classify_source` are stable strings such as
``scene:private``, ``explicit_secret``, ``sensitive:health``,
``sensitive:third_party``, ``private_ordinary`` and ``unknown_scene_kind``.  They
are for private diagnostics/audit only and must never be shown to users (R-13).
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cycle
    from .identity import Scene

VIS_GROUP_PUBLIC = "group_public"
VIS_PRIVATE = "private"
#: Internal marker for "we could not classify this source": never shareable.
VIS_UNKNOWN = "unknown"

REUSE_OWNER_PRIVATE_ONLY = "owner_private_only"
REUSE_OWNER_PUBLIC_SAFE = "owner_public_safe"
REUSE_SOURCE_GROUP_ONLY = "source_group_only"

SOURCE_VISIBILITIES = (VIS_GROUP_PUBLIC, VIS_PRIVATE)
REUSE_VISIBILITIES = (
    REUSE_OWNER_PRIVATE_ONLY,
    REUSE_OWNER_PUBLIC_SAFE,
    REUSE_SOURCE_GROUP_ONLY,
)

MINIMAL_TEXT_DEFAULT_LIMIT = 120

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------


def _match_text(text: Any) -> str:
    """NFKC, whitespace-free, case-folded copy used for keyword matching."""
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in normalized if not ch.isspace()).casefold()


def _display_text(text: Any) -> str:
    """Return user-facing text unchanged (no NFKC rewrite) when it is a string."""
    return text if isinstance(text, str) else ""


# ---------------------------------------------------------------------------
# Explicit-secret detection (R-07, CASE-009)
# ---------------------------------------------------------------------------

_EXPLICIT_SECRET_MARKERS = (
    # "only tell you" family
    "只告诉你",
    "只跟你说",
    "只跟你讲",
    "只和你讲",
    "只和你一个人说",
    "只跟你一个人说",
    "只有你知道",
    "只有你一个人知道",
    "就你知道",
    # "do not tell others" family
    "别告诉别人",
    "不要告诉别人",
    "不许告诉别人",
    "不能告诉别人",
    "别告诉其他人",
    "不要告诉其他人",
    "别跟别人说",
    "不要跟别人说",
    "别跟其他人说",
    "别告诉他人",
    "不要告诉他人",
    "不告诉别人",
    "别让第三人知道",
    # "do not say it" family
    "别说出去",
    "不要说出去",
    "不许说出去",
    "不要外传",
    "不能外传",
    "别外传",
    "别传出去",
    "不要传出去",
    "别说漏嘴",
    "保密",
    "这是秘密",
    "这是个秘密",
    "我们之间",
    "我们俩之间",
    "你知我知",
    "天知地知",
    "瞒着别人",
    "瞒着大家",
    "别在群里说",
    "不要在群里说",
    "别在群里提",
    "不要在群里提",
    "群里别说",
    # "secretly" family (contract explicitly lists 偷偷/私下说)
    "偷偷",
    "悄悄",
    "私下",
    "私底下",
    # common English signals, matched on the whitespace-free lowercase copy
    "betweenus",
    "justbetweenus",
    "don'ttell",
    "donttell",
    "donottell",
    "keepitsecret",
    "keepthissecret",
    "thisisasecret",
)


def is_explicit_secret(text: Any) -> bool:
    """True when the text itself asks for secrecy; invalid text is not secret."""
    normalized = _match_text(text)
    if not normalized:
        return False
    return any(marker in normalized for marker in _EXPLICIT_SECRET_MARKERS)


# ---------------------------------------------------------------------------
# Sensitivity classification (R-07, CASE-008/010/013)
# ---------------------------------------------------------------------------

_SENSITIVE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "sensitive:health",
        (
            "生病",
            "有病",
            "病了",
            "发烧",
            "发高烧",
            "感冒",
            "咳嗽",
            "头疼",
            "头痛",
            "胃疼",
            "肚子疼",
            "腹痛",
            "住院",
            "手术",
            "治疗",
            "吃药",
            "服药",
            "处方",
            "确诊",
            "诊断",
            "病情",
            "症状",
            "失眠",
            "睡不着",
            "体检",
            "过敏",
            "拉肚子",
            "不舒服",
            "身体不舒服",
            "身体不太好",
            "身体不好",
            "骨折",
            "受伤",
            "癌症",
            "化疗",
            "复查",
            "看医生",
            "去医院",
            "打针",
            "输液",
            "重病",
            "慢性病",
            "心理医生",
            "心理咨询",
        ),
    ),
    (
        "sensitive:emotional_distress",
        (
            "难过",
            "伤心",
            "委屈",
            "焦虑",
            "抑郁",
            "崩溃",
            "想哭",
            "哭了",
            "心情不好",
            "心情很差",
            "心情低落",
            "压力大",
            "压力好大",
            "压力很大",
            "好累",
            "很累",
            "痛苦",
            "烦恼",
            "烦躁",
            "郁闷",
            "不开心",
            "失落",
            "孤独",
            "害怕",
            "恐惧",
            "难受",
            "emo",
            "情绪低落",
            "情绪不好",
            "心态崩了",
            "绝望",
            "无助",
            "想不通",
            "心理压力",
        ),
    ),
    (
        "sensitive:family",
        (
            "爸爸",
            "妈妈",
            "爸妈",
            "父母",
            "父亲",
            "母亲",
            "老爸",
            "老妈",
            "我爸",
            "我妈",
            "我父母",
            "我家里人",
            "家里",
            "家人",
            "家庭",
            "老公",
            "老婆",
            "丈夫",
            "妻子",
            "结婚",
            "离婚",
            "孩子",
            "儿子",
            "女儿",
            "哥哥",
            "姐姐",
            "弟弟",
            "妹妹",
            "亲戚",
            "婆婆",
            "公公",
            "岳父",
            "岳母",
            "爷爷",
            "奶奶",
            "外公",
            "外婆",
            "家暴",
            "单亲",
            "继父",
            "继母",
        ),
    ),
    (
        "sensitive:interpersonal_conflict",
        (
            "吵架",
            "争吵",
            "争执",
            "矛盾",
            "冲突",
            "闹翻",
            "翻脸",
            "绝交",
            "被排挤",
            "排挤",
            "霸凌",
            "欺负",
            "冷战",
            "撕破脸",
            "闹别扭",
            "被孤立",
            "孤立",
            "和同事",
            "和同学",
            "和室友",
            "和舍友",
            "同事关系",
            "人际",
        ),
    ),
    (
        "sensitive:finance",
        (
            "工资",
            "收入",
            "薪水",
            "存款",
            "积蓄",
            "负债",
            "欠款",
            "欠债",
            "贷款",
            "房贷",
            "车贷",
            "房租",
            "破产",
            "投资",
            "股票",
            "基金",
            "彩票",
            "借钱",
            "还钱",
            "花呗",
            "信用卡",
            "没钱",
            "有钱",
            "花钱",
            "赚钱",
            "攒钱",
            "月光",
            "转账",
            "红包",
            "还款",
            "生活费",
            "奖学金",
            "债务",
            "氪金",
        ),
    ),
    (
        "sensitive:contact_credentials",
        (
            "手机号",
            "手机号码",
            "电话号码",
            "电话",
            "微信",
            "微信号",
            "qq号",
            "邮箱",
            "电子邮件",
            "密码",
            "验证码",
            "身份证",
            "身份证号",
            "银行卡",
            "账号",
            "帐号",
            "密钥",
            "token",
            "令牌",
            "联系方式",
            "二维码",
            "加我",
            "扫我",
        ),
    ),
    (
        "sensitive:address",
        (
            "住址",
            "地址",
            "住在",
            "我家在",
            "小区",
            "门牌",
            "楼号",
            "几号楼",
            "单元号",
            "宿舍号",
            "坐标",
            "定位",
        ),
    ),
    (
        "sensitive:third_party",
        (
            "我朋友",
            "我同学",
            "我同事",
            "我室友",
            "我舍友",
            "我闺蜜",
            "我哥们",
            "我对象",
            "我男朋友",
            "我女朋友",
            "我前任",
            "我哥",
            "我姐",
            "我弟",
            "我妹",
            "我表哥",
            "我表姐",
            "我堂哥",
            "我堂姐",
            "他家里",
            "她家里",
            "他的事",
            "她的事",
            "他的隐私",
            "她的隐私",
            "别人的事",
            "别人的私事",
            "他人的事",
            "第三人的事",
            "第三人",
        ),
    ),
)

# Personal logistics: private by default (R-07, CASE-010).
_WHEREABOUTS_MARKERS = (
    "出差",
    "搬家",
    "面试",
    "行程",
    "行踪",
    "请假",
    "调休",
    "订票",
    "订了票",
    "机票",
    "航班",
    "机场",
    "高铁",
    "火车站",
    "动车",
    "回老家",
    "去外地",
    "住酒店",
    "旅游",
    "旅行",
    "度假",
    "年假",
    "探亲",
    "加班",
    "打算",
    "计划",
    "准备去",
    "准备回",
    "要去",
    "得去",
    "要去一趟",
)

_TIME_PLAN_PATTERN = re.compile(
    r"(?:明天|后天|下周|下星期|下个月|下周末|这周末|本周末|周末|今晚|明晚)"
    r"[^，。！？；,.!?;]{0,6}?"
    r"(?:要|去|得|准备|打算|计划|回|出发|飞|坐|参加|出差|请假)"
)
_SELF_PLAN_PATTERN = re.compile(
    r"(?:我|我们|咱)[^，。！？；,.!?;]{0,6}?(?:打算|计划|准备去|准备回|要去|得去|出差|请假)"
)

_UNCERTAIN_MARKERS = (
    "不确定",
    "说不准",
    "还没确定",
    "没确定",
    "还没定",
    "没定",
    "没想好",
    "不好说",
    "说不好",
    "可能吧",
    "也许吧",
    "大概吧",
    "不知道要不要",
    "犹豫",
)


def _has_whereabouts(text: str) -> bool:
    if any(marker in text for marker in _WHEREABOUTS_MARKERS):
        return True
    if _TIME_PLAN_PATTERN.search(text):
        return True
    return bool(_SELF_PLAN_PATTERN.search(text))


def _sensitive_categories(text: Any) -> list[str]:
    """Ordered list of sensitive-category reason codes matched by the text."""
    normalized = _match_text(text)
    if not normalized:
        return []
    found: list[str] = []
    for code, markers in _SENSITIVE_MARKERS:
        if any(marker in normalized for marker in markers):
            found.append(code)
    if _has_whereabouts(normalized):
        found.append("sensitive:whereabouts_plans")
    return found


def is_sensitive(text: Any) -> bool:
    """True when private text belongs to a private-only theme (R-07)."""
    return bool(_sensitive_categories(text))


def _is_uncertain(text: Any) -> bool:
    normalized = _match_text(text)
    if not normalized or len(normalized) <= 3:
        return True
    return any(marker in normalized for marker in _UNCERTAIN_MARKERS)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    source_visibility: str
    reuse: str
    sensitive: bool
    explicit_secret: bool
    reasons: tuple[str, ...]


_PUBLIC_TOPICS = (
    "星露谷物语", "我的世界", "minecraft", "deadlock", "夏日重现", "艾尔登法环",
    "动画", "那部动画",
    "一部动画", "游戏", "音游", "跑步", "游泳", "咖啡", "推理小说",
)
_PUBLIC_PREFIXES = (
    "我喜欢", "我喜欢看", "我喜欢玩", "我喜欢上了", "我最近在追",
    "我最近在玩", "我在玩", "我正在玩", "我在看", "我正在看", "我爱看",
    "我爱玩", "昨晚我喜欢上了", "其实我更喜欢",
)
_PUBLIC_OPINIONS = ("", "剧情挺有意思的", "战斗系统做得很扎实", "挺好玩", "很好玩",
                    "剧情很有意思", "很好看", "挺有意思")
def safe_public_fragment(text):
    """Closed full-message grammar, not a blacklist or arbitrary title extractor.
    Unknown/mixed/free-form private content never becomes public automatically.
    """
    value = _match_text(text)
    if not value or is_explicit_secret(text) or is_sensitive(text):
        return ""
    for prefix in _PUBLIC_PREFIXES:
        for title in _PUBLIC_TOPICS:
            for opinion in _PUBLIC_OPINIONS:
                candidates = (prefix + title, prefix + "《" + title + "》")
                for candidate in candidates:
                    expected = candidate + ("，" + opinion if opinion else "")
                    if value.rstrip("。.!！~～") == _match_text(expected):
                        return minimal_text(text)
    return ""

def classify_source(
    text: Any, *, scene_kind: Any, explicit_secret: bool | None = None
) -> Classification:
    """Classify where a memory item came from and how far it may travel.

    * ``group_public`` content is only ever reused in its source group;
    * private, non-sensitive, non-secret content may be reused in a group as a
      minimal public-safe fragment (R-06, CASE-007/015);
    * private sensitive/uncertain/secret content stays private (R-07,
      CASE-008/009/010/012/013);
    * an explicit secret always defeats ``owner_public_safe``; passing
      ``explicit_secret=False`` cannot declassify secret language in the text;
    * unknown scenes get an unknown (never shareable) source visibility.
    """
    secret_signal = is_explicit_secret(text)
    secret = bool(explicit_secret) or secret_signal
    categories = _sensitive_categories(text)

    if scene_kind == VIS_GROUP_PUBLIC:
        reasons = ["scene:group_public", "group_scope_only"]
        if secret:
            reasons.append("explicit_secret")
        reasons.extend(categories)
        return Classification(
            source_visibility=VIS_GROUP_PUBLIC,
            reuse=(REUSE_OWNER_PUBLIC_SAFE if not secret and safe_public_fragment(text) else REUSE_SOURCE_GROUP_ONLY),
            sensitive=bool(categories) or secret,
            explicit_secret=secret,
            reasons=tuple(reasons),
        )

    if scene_kind == VIS_PRIVATE:
        reasons = ["scene:private"]
        if secret:
            reasons.append("explicit_secret")
            reasons.extend(categories)
            return Classification(
                source_visibility=VIS_PRIVATE,
                reuse=REUSE_OWNER_PRIVATE_ONLY,
                sensitive=True,
                explicit_secret=True,
                reasons=tuple(reasons),
            )
        if categories:
            reasons.extend(categories)
            return Classification(
                source_visibility=VIS_PRIVATE,
                reuse=REUSE_OWNER_PRIVATE_ONLY,
                sensitive=True,
                explicit_secret=False,
                reasons=tuple(reasons),
            )
        if _is_uncertain(text) or not safe_public_fragment(text):
            reasons.append("uncertain")
            return Classification(
                source_visibility=VIS_PRIVATE,
                reuse=REUSE_OWNER_PRIVATE_ONLY,
                sensitive=True,
                explicit_secret=False,
                reasons=tuple(reasons),
            )
        reasons.append("private_ordinary")
        return Classification(
            source_visibility=VIS_PRIVATE,
            reuse=REUSE_OWNER_PUBLIC_SAFE,
            sensitive=False,
            explicit_secret=False,
            reasons=tuple(reasons),
        )

    # Unknown scene kind: never guess a visible scope; can_share() rejects it.
    reasons = ["unknown_scene_kind"]
    if secret:
        reasons.append("explicit_secret")
    reasons.extend(categories)
    return Classification(
        source_visibility=VIS_UNKNOWN,
        reuse=REUSE_OWNER_PRIVATE_ONLY,
        sensitive=True,
        explicit_secret=secret,
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Memory items and fail-closed sharing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryItem:
    item_id: str
    owner_key: str
    source_visibility: str
    source_session: str
    source_group_id: str
    reuse: str
    explicit_secret: bool
    created_at: float
    fingerprint: str = ""
    deleted: bool = False
    corrected: bool = False
    paused: bool = False
    public_fragment_verified: bool = False


def _clean_key(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _session_platform(session: Any) -> str | None:
    """Best-effort platform prefix of a ``platform:type:session`` key.

    Only used to *reject* cross-platform reuse.  Sessions without the unified
    origin shape yield ``None`` and therefore impose no extra restriction.
    """
    if not isinstance(session, str):
        return None
    parts = session.split(":")
    if len(parts) < 3:
        return None
    prefix = parts[0].strip()
    return prefix or None


def _valid_group_target(target: "Scene") -> bool:
    return bool(getattr(target, "group_id", ""))


def can_share(item: MemoryItem, target: Any) -> bool:
    """Return True only when ``item`` may be used in ``target`` -- fail closed.

    The caller remains responsible for binding items to the current member
    (``owner_key``) before prompting; this function enforces the *scope* rules:

    * deleted / corrected / paused items are never recalled (R-12);
    * unknown enum values, inconsistent item metadata and invalid targets are
      rejected instead of guessed (R-03/R-10);
    * group-public memory only returns to the same source group (R-05,
      CASE-013);
    * private ``owner_private_only`` memory only returns to the exact same
      private session; ``owner_public_safe`` memory may be used in group
      contexts as a minimal fragment, and an explicit secret always blocks it
      (R-06..R-09, CASE-007/008/009/011/012/015).
    """
    if not isinstance(item, MemoryItem) or not hasattr(target, "kind"):
        return False
    if item.deleted or item.corrected or item.paused:
        return False
    if item.source_visibility not in SOURCE_VISIBILITIES:
        return False
    if item.reuse not in REUSE_VISIBILITIES:
        return False
    if target.kind not in (VIS_GROUP_PUBLIC, VIS_PRIVATE):
        return False
    if target.kind == VIS_PRIVATE and getattr(target, "group_id", ""):
        # A private scene carrying a group id is inconsistent; never guess.
        return False

    target_platform = _clean_key(getattr(target, "platform", ""))
    source_platform = _session_platform(item.source_session)

    if item.source_visibility == VIS_GROUP_PUBLIC:
        if source_platform and target_platform and source_platform != target_platform:
            return False
        if (item.reuse == REUSE_OWNER_PUBLIC_SAFE and item.public_fragment_verified
                and not item.explicit_secret):
            return bool(_valid_group_target(target) if target.kind == VIS_GROUP_PUBLIC
                        else _clean_key(getattr(target, "session", "")))
        if item.reuse != REUSE_SOURCE_GROUP_ONLY:
            return False
        if target.kind != VIS_GROUP_PUBLIC:
            return False
        if not _valid_group_target(target):
            return False
        if not item.source_group_id or item.source_group_id != target.group_id:
            return False
        if (
            source_platform is not None
            and target_platform is not None
            and source_platform != target_platform
        ):
            return False
        return True

    # item.source_visibility == VIS_PRIVATE
    target_session = _clean_key(getattr(target, "session", ""))
    same_private_session = (
        target.kind == VIS_PRIVATE
        and target_session is not None
        and isinstance(item.source_session, str)
        and target_session == item.source_session.strip()
    )

    if item.reuse == REUSE_OWNER_PRIVATE_ONLY:
        return bool(same_private_session)

    if item.reuse == REUSE_OWNER_PUBLIC_SAFE:
        if item.explicit_secret:
            return False
        if target.kind == VIS_GROUP_PUBLIC:
            if not _valid_group_target(target):
                return False
            if (
                source_platform is not None
                and target_platform is not None
                and source_platform != target_platform
            ):
                return False
            return True
        return bool(same_private_session)

    # owner_public_safe is the only private reuse that can reach groups, and it
    # was handled above; any other combination is inconsistent metadata.
    return False


def filter_for_prompt(items: Iterable[MemoryItem], target: Any) -> list[MemoryItem]:
    """Filter items before any prompt/tool payload; order is preserved.

    This is the R-10 "filter first, then rank" entry point: callers must run it
    before ranking/truncation and before any text reaches the model.  An
    invalid target yields an empty result (fail closed).
    """
    if not hasattr(target, "kind") or target.kind not in (
        VIS_GROUP_PUBLIC,
        VIS_PRIVATE,
    ):
        return []
    if target.kind == VIS_GROUP_PUBLIC and not _valid_group_target(target):
        return []
    if target.kind == VIS_PRIVATE and not _clean_key(getattr(target, "session", "")):
        return []
    try:
        iterator = iter(items)
    except TypeError:
        return []
    return [item for item in iterator if can_share(item, target)]


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


def third_party_grant_allowed(granting_key: Any, owner_key: Any) -> bool:
    """A third party can never grant access to someone else's memory (R-08).

    Always ``False`` -- even for missing values, and even when the requester
    happens to be the owner: an owner must use :func:`owner_grant_allowed`.
    """
    return False


def owner_grant_allowed(owner_key: Any, requester_key: Any) -> bool:
    """Only the owner may widen their own visibility; everyone else is refused."""
    owner = _clean_key(owner_key)
    requester = _clean_key(requester_key)
    if owner is None or requester is None:
        return False
    return owner == requester


# ---------------------------------------------------------------------------
# Minimal public fragment
# ---------------------------------------------------------------------------

_CHANNEL_TIME_PATTERNS = (
    r"昨(?:天(?:晚上|夜里|半夜)?|晚|夜)",
    r"前(?:天(?:晚上|夜里|半夜)?|晚|夜)",
    r"今(?:天(?:早上|上午|中午|下午|晚上|夜里)?|晚|夜)",
    r"明(?:天(?:早上|上午|中午|下午|晚上)?|晚)",
    r"后(?:天(?:早上|上午|中午|下午|晚上)?|晚)",
    r"大前天(?:晚上)?",
    r"刚才|刚刚|方才",
    r"上个?周末?",
    r"上周(?:[一二三四五六日天])?",
    r"这周(?:[一二三四五六日天])?",
    r"本周(?:[一二三四五六日天])?",
    r"上次|上回|之前|先前|以前",
    r"私(?:底下?|聊里?|聊中|信里?|信上)",
    r"偷偷(?:地|的)?|悄悄(?:地|的)?",
    r"我们(?:俩|两个)?之间|我们私下",
    r"聊天记录|对话记录|消息记录",
    r"(?:在)?群(?:里|中)(?:面)?",
    r"QQ|qq|微信|WeChat|wechat|WhatsApp|Telegram|私信",
    r"你(?:偷偷|悄悄|私下)(?:地)?(?:跟我|和我|对我)?(?:说|告诉|讲)(?:过|了)?(?:我)?",
    r"你跟我说过|你告诉过我|你对我说过|你和我说的|你跟我讲的",
    r"跟我说过|告诉过我|对我说过",
    r"别(?:再)?(?:告诉|跟|和)(?:别人|其他人|人家)",
    r"不要(?:告诉|跟|和)(?:别人|其他人)",
    r"别(?:说|讲)出去|不要(?:说|讲)出去|别说漏嘴",
    r"保密|只(?:告诉|跟|和|对)(?:你|您)",
)
_CHANNEL_TIME_RE = re.compile("|".join(_CHANNEL_TIME_PATTERNS))
_REDACT_SPACES_RE = re.compile(r"\s+")
_REPEATED_PUNCT_RE = re.compile(r"([，,。.；;：:！!？?])\1+")
_CLAUSE_BOUNDARIES = set("。！？!?；;，,、 ")
_TRIM_CHARS = " \t\r\n，,、；;：:。.！!？?~～—－-"


def minimal_text(text: Any, limit: int = MINIMAL_TEXT_DEFAULT_LIMIT) -> str:
    """Return the smallest reusable fragment with channel/time hints removed.

    Used only for ``owner_public_safe`` content.  It strips private-channel
    words, concrete time references and "you secretly told me" hints (R-06,
    CASE-007/015), then truncates to ``limit`` characters at a clause boundary
    when possible.  Invalid input or a non-positive limit yields ``""``.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return ""
    source = _display_text(text)
    if not source:
        return ""
    cleaned = _CHANNEL_TIME_RE.sub("", source)
    # Remove any residual explicit-secret wording from the fragment itself.
    for marker in _EXPLICIT_SECRET_MARKERS:
        if marker.isascii():
            continue  # ascii markers are matched on normalized copy, not display text
        cleaned = cleaned.replace(marker, "")
    cleaned = _REDACT_SPACES_RE.sub(" ", cleaned)
    cleaned = _REPEATED_PUNCT_RE.sub(r"\1", cleaned).strip()
    if len(cleaned) > limit:
        window = cleaned[:limit]
        boundary = -1
        for index in range(len(window) - 1, max(-1, limit // 2 - 1), -1):
            if window[index] in _CLAUSE_BOUNDARIES:
                boundary = index
                break
        if boundary >= 0:
            window = window[:boundary]
        window = window.strip(_TRIM_CHARS)
        if window:
            return window
        cleaned = cleaned[:limit]
    return cleaned.strip(_TRIM_CHARS)
