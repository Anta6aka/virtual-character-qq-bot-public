"""Orchestration layer between the ltm plugin hooks and the relationship store.

Design constraints:
- R-03: this auxiliary module must never break plugin loading or normal replies.
  Every public method catches its own failures, records a private degradation
  reason and returns a safe fallback (normal dialogue continues; cross-scene
  recall fails closed).
- R-40: no extra dedicated model call per message.  The only model participation
  is an optional constrained ``relationship_assess`` tool whose proposals are
  validated here before touching the store.
- R-13/R-24: diagnostics stay local/private; nothing here renders numbers.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

from . import context, engine, params, visibility, accumulation
from .identity import MemberIdentity, Scene, SCENE_GROUP, SCENE_PRIVATE
from .store import RelationshipStore, StoreError, day_for, event_key, fingerprint

# --- deterministic candidate vocabulary (conservative, not a semantic model) ---
GREETING_RE = re.compile(
    r"^(?:早安|早上好|午安|晚上好|晚安|早|在吗|在不在|你好|hello|hi|こんにちは|おはよう|こんばんは)"
    r"[\s。.!！~～，,]*$", re.I)
PRAISE_RE = re.compile(r"(?:你好棒|你真棒|你最棒|好厉害|太强了|真聪明|最喜欢你|爱你|么么哒)")
SCORE_COMMAND_RE = re.compile(
    r"(?:加\s*\d+\s*分|加满|满分|最高亲密|直接最高|涨好感|提升关系|刷好感|给我(?:加分|升|最高))")
FARMING_RE = re.compile(r"(?:复制|刷屏|每天|反复)")
PURCHASE_RE = re.compile(r"(?:给你(?:买|充|送)|氪金|打赏|付费|充值)")
BUG_FEEDBACK_RE = re.compile(
    r"(?:bug|BUG|报错|故障|失灵|你搞错|系统错|出错|没反应|无回复|误判|误读|这是系统)|"
    r"你(?:的)?(?:回答|回复|理解|结论|算法|解释).{0,8}(?:错|不对|有误)")
EXPLAIN_RE = re.compile(r"(?:我解释|解释一下|不是那个意思|误会|抱歉|对不起|是我说错|我收回)")
CONTROL_RE = re.compile(r"(?:忘记|删除|删掉|清除|隐私|保密|别再记|不要记|记忆|重置|暂停)")
APOLOGY_RE = re.compile(r"(?:对不起|抱歉|我错了|是我不好|原谅)")
REFUSAL_RE = re.compile(r"(?:不想聊|不想谈|别再说|别再问|到此为止|够了|换个话题|不聊这个|别提了)")
TOPIC_STOP_RE = re.compile(
    r"(?:不想聊|不想谈|别问|别再问|别提|别说这个|不要再提|不聊|别再聊|不要再聊|别聊|换个话题|说点别的|别说这个了)")
# Narrow direct-target hostility: criticism of a game/answer is not an attack.
DIRECT_HOSTILITY_RE = re.compile(
    r"^(?:滚|闭嘴|死一边|废物|垃圾|有病|恶心|小丑|傻|蠢)[。.!！~～\s]*$|"
    r"你(?:这个|这家伙|真是|就是|是个|是|个|太|这么|怎么这么|简直是)?"
    r"(?:废物|垃圾|有病|恶心|小丑|傻|蠢|也配)|(?:给我滚|你给我闭嘴)")
SOCIAL_NEGATIVE_RE = re.compile(
    r"(?:滚|闭嘴|有病|恶心|小丑|傻|蠢|你也配|别装|死一边|废物|垃圾)")
THIRD_PARTY_RE = re.compile(
    r"(?:他|她|室友|同事|同学|朋友|别人|某人|人家|弟弟|妹妹|哥哥|姐姐|家人|妈妈|爸爸|父母)"
    r"[^。！？\n]{0,12}(?:说|提到|觉得|骂|告诉|答应|失约|放鸽子|爽约|没兑现|没做到|忘了)")
QUOTED_VERB_RE = re.compile(r"(?:他说|她说|有人说|原话|转述|引用|复述)")
JOKE_RE = re.compile(r"(?:假如|假设|如果|开玩笑|反杀|反串|狗头|doge|/s|玩梗|故事里|小说里)", re.I)
PROMISE_RE = re.compile(r"(?:约定|说好|答应|承诺|下次一起)")
KEEP_PROMISE_RE = re.compile(r"(?:我做到了|按约定|我去了|我完成了|兑现了|我来赴约)")
BREAK_PROMISE_RE = re.compile(r"(?:又忘了|又没来|又没做到|放鸽子|失约|没兑现)")
RESPECT_BOUNDARY_RE = re.compile(r"(?:我理解|尊重|没关系|你不想聊就|不勉强|停在这里)")
REASONED_DISAGREE_RE = re.compile(r"(?:我不同意|我觉得不对|有理由|因为.{0,60}(?:所以|因此))")

# Topic words that should not become a topic signature by themselves.
TOPIC_STOPWORDS = set(
    "这个 那个 今天 明天 昨天 现在 刚才 最近 之前 以后 已经 还是 真的 觉得 怎么 什么 "
    "为什么 这样 那样 可以 可能 应该 一下 一起 我们 你们 他们 还有 然后 所以 但是 不过 "
    "结果 进展 事情 东西 知道 没有 不是 就是 好的 谢谢 有点 有些 自己 时候 一个 怎么样 "
    "喜欢 讨厌 工作 学校 大家 朋友 开始 完成 通过 有趣 そう それ これ 不想 再聊 别再 "
    "不要 换个 换话 话题 这个 说点 别的 算了 不想聊 别再问 别提 不要提 聊 说 提 "
    "我想 我觉得 你不用 不用".split()
)
BYPASS_RE = re.compile(
    r"^(?:/|／)|(?:隐私|记忆|忘记|删除|删掉|清除|重置|暂停|保密)|"
    + BUG_FEEDBACK_RE.pattern + "|" + EXPLAIN_RE.pattern)
TOPIC_CHARS_RE = re.compile(r"[\w\u3040-\u30ff\u4e00-\u9fa5]+")


def _content_words(text: str) -> list[str]:
    """Deterministic topic tokens.

    Reuses the plugin's existing local tokenizer when importable (it is already
    a hard dependency of the host plugin) and falls back to CJK bigrams so this
    module still works in isolation.  No network/model calls are involved.
    """
    clean = (text or "").lower()
    words: list[str] = []
    try:
        from ..interaction import content_words as tokenize  # local plugin tokenizer
        words = [word for word in tokenize(clean) if word not in TOPIC_STOPWORDS]
    except Exception:  # noqa: BLE001 - standalone fallback
        for chunk in TOPIC_CHARS_RE.findall(clean):
            if chunk in TOPIC_STOPWORDS:
                continue
            if len(chunk) <= 2:
                words.append(chunk)
            else:
                words.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
        words = [word for word in words if word not in TOPIC_STOPWORDS]
    ordered: list[str] = []
    for word in sorted(set(words), key=lambda item: (-len(item), item)):
        if word not in ordered:
            ordered.append(word)
    return ordered


def topic_signature(text: str, limit: int = 3) -> str:
    """Deterministic topic key for bounded refusal windows (not a semantic claim)."""
    unique: list[str] = []
    for word in _content_words(text):
        if word not in unique:
            unique.append(word)
        if len(unique) >= limit:
            break
    return "|".join(unique)


def topic_matches(signature: str, text: str) -> bool:
    if not signature:
        return False
    parts = {part for part in signature.split("|") if part}
    if not parts:
        return False
    return bool(parts & set(_content_words(text)))


@dataclass(frozen=True)
class ServiceConfig:
    enabled: bool = True
    character_id: str = "角色A"
    max_visible: int = 3

    @classmethod
    def from_mapping(cls, mapping: Mapping | None, *, default_character_id: str) -> "ServiceConfig":
        data = mapping if isinstance(mapping, Mapping) else {}
        enabled = data.get("enable", True)
        enabled = enabled if isinstance(enabled, bool) else False
        character_id = data.get("character_id")
        if not isinstance(character_id, str) or not character_id.strip() or len(character_id) > 40:
            character_id = default_character_id or "角色A"
        max_visible = data.get("max_visible", 3)
        if not isinstance(max_visible, int) or not 0 <= max_visible <= 5:
            max_visible = 3
        return cls(enabled=enabled, character_id=character_id.strip(), max_visible=max_visible)


@dataclass
class CaptureResult:
    enabled: bool = False
    identity: dict = field(default_factory=dict)
    affinity: int = 0
    auto_participation: bool = True
    intimacy_allowed: bool = True
    suppress: bool = False
    suppress_reason: str = ""
    topic_label: str = ""
    recent_negative: bool = False
    error: str = ""

    def as_extras(self) -> dict:
        return {
            "relationship_enabled": self.enabled,
            "relationship_affinity": self.affinity,
            "relationship_auto_participation": self.auto_participation,
            "relationship_intimacy_allowed": self.intimacy_allowed,
            "relationship_suppress": self.suppress,
            "relationship_suppress_reason": self.suppress_reason,
            "relationship_topic_label": self.topic_label,
            "relationship_recent_negative": self.recent_negative,
            "relationship_error": self.error,
        }


class RelationshipService:
    """Stateful facade. All public methods return safe fallbacks on failure."""

    DEGRADE_SECONDS = 30.0
    CLEANUP_INTERVAL_SECONDS = 3600.0
    MAX_TEXT = 1200

    def __init__(self, db_path, character_id: str, *, config: ServiceConfig | None = None,
                 store: RelationshipStore | None = None,
                 clock: Callable[[], float] | None = None,
                 logger=None):
        self.config = config or ServiceConfig(character_id=character_id)
        self.character_id = self.config.character_id
        self._clock = clock or time.time
        self._logger = logger
        self.store = store
        self._db_path = db_path
        self._lock = threading.Lock()
        self._ready = False
        self._error = ""
        self._degraded_until = 0.0
        self._warned = False
        self._last_cleanup = 0.0
        self.counters = {"model_calls": 0, "model_tool_rounds": 0, "store_errors": 0,
                         "suppressed": 0, "captures": 0, "commits": 0}
        if config is not None and not config.enabled:
            self._ready = False
            self._error = "disabled"

    # ------------------------------------------------------------- lifecycle
    def _now(self) -> float:
        return float(self._clock())

    def _degrade(self, error: Exception) -> None:
        self.counters["store_errors"] += 1
        self._error = type(error).__name__ + ": " + str(error)[:160]
        self._degraded_until = self._now() + self.DEGRADE_SECONDS
        if not self._warned and self._logger is not None:
            try:
                self._logger.warning("[relationship] unavailable; normal dialogue continues: " + self._error)
            except Exception:  # noqa: BLE001 - logging must never raise into the reply path
                pass
            self._warned = True

    def ensure_ready(self) -> bool:
        if not self.config.enabled:
            return False
        if self._now() < self._degraded_until:
            return False
        if self._ready:
            return True
        with self._lock:
            if self._ready:
                return True
            try:
                if self.store is None:
                    self.store = RelationshipStore(self._db_path, clock=self._clock)
                self.store.migrate(character_id=self.character_id)
                self._ready = True
                self._error = ""
                return True
            except Exception as error:  # noqa: BLE001 - R-03 isolation
                self._degrade(error)
                return False

    @property
    def available(self) -> bool:
        return self._ready and self._now() >= self._degraded_until

    def close(self) -> None:
        self._ready = False

    def _maybe_cleanup(self, now: float) -> None:
        """Periodic bounded maintenance; reachable after readiness (review P-05)."""
        if now - self._last_cleanup < self.CLEANUP_INTERVAL_SECONDS:
            return
        try:
            self.store.expire_pending(now)
            self.store.cleanup(now)
            self._last_cleanup = now
        except Exception as error:  # noqa: BLE001 - maintenance must not break replies
            self._degrade(error)

    # --------------------------------------------------------- deterministic
    @staticmethod
    def is_farming(text: str) -> bool:
        clean = (text or "").strip()
        return bool(GREETING_RE.fullmatch(clean) or SCORE_COMMAND_RE.search(clean)
                    or PURCHASE_RE.search(clean)
                    or (PRAISE_RE.search(clean) and len(clean) <= 20))

    @staticmethod
    def is_bypass(text: str) -> bool:
        return bool(BYPASS_RE.search((text or "").strip()[:200]))

    @staticmethod
    def implies_meaningful(text: str) -> bool:
        clean = (text or "").strip()
        if len(clean) < 8 or JOKE_RE.search(clean) or THIRD_PARTY_RE.search(clean):
            return False
        return bool(PROMISE_RE.search(clean) or re.search(r"我(?:今天|昨天|刚才|最近)", clean)
                    or len(clean) >= 24)

    @staticmethod
    def subject_of(text: str, *, directed: bool = False) -> str:
        """Conservative attribution: member / quoted / other / unknown."""
        clean = (text or "").strip()
        if QUOTED_VERB_RE.search(clean):
            return "quoted"
        if THIRD_PARTY_RE.search(clean) or JOKE_RE.search(clean):
            return "other" if THIRD_PARTY_RE.search(clean) else "unknown"
        if DIRECT_HOSTILITY_RE.search(clean) and directed:
            return "member"
        return "member"

    def classify_message(self, text: str, *, directed: bool = False) -> dict:
        """Deterministic candidate intent; never sets scores directly (R-23)."""
        clean = (text or "").strip()
        result = {
            "subject": self.subject_of(clean, directed=directed),
            "farming": self.is_farming(clean),
            "trust": "",          # ""|positive|negative
            "strength": "normal",
            "mood_negative": False,
            "topic_signature": topic_signature(clean) if TOPIC_STOP_RE.search(clean) else "",
            "refusal_request": bool(TOPIC_STOP_RE.search(clean)),
            "meaningful": self.implies_meaningful(clean),
        }
        if self.is_bypass(clean):
            return result
        if BUG_FEEDBACK_RE.search(clean):
            return result
        subject = result["subject"]
        if subject == "member":
            if BREAK_PROMISE_RE.search(clean) and PROMISE_RE.search(clean):
                result.update(trust="negative", strength="strong")
            elif DIRECT_HOSTILITY_RE.search(clean) and directed:
                result.update(trust="negative", strength="normal", mood_negative=True)
            elif KEEP_PROMISE_RE.search(clean):
                result.update(trust="positive", strength="strong")
            elif RESPECT_BOUNDARY_RE.search(clean) or REASONED_DISAGREE_RE.search(clean):
                result.update(trust="positive", strength="normal")
        if EXPLAIN_RE.search(clean) or APOLOGY_RE.search(clean):
            result["trust"] = ""
        return result

    # ------------------------------------------------------------- identity
    def _identity(self, *, platform: str, member_id: str) -> MemberIdentity | None:
        from .identity import identity_for
        return identity_for(self.character_id, platform, member_id)

    def _scene(self, *, platform: str, session: str, group_id: str) -> Scene:
        scene = Scene(
            kind=SCENE_GROUP if group_id else SCENE_PRIVATE,
            session=session or "", group_id=group_id or "", platform=platform or "")
        return scene

    # --------------------------------------------------------------- writes
    def on_message(self, *, platform: str, session: str, group_id: str, member_id: str,
                   message_id: str, text: str, at: float | None = None,
                   directed: bool = False, is_media: bool = False) -> CaptureResult:
        """Register provenance and evaluate receive-time candidates.

        Familiarity is only *pending* here; it is committed by ``on_reply_sent``
        after the adapter confirms a matching reply was actually sent (R-15).
        """
        result = CaptureResult(enabled=bool(self.config.enabled),
                               auto_participation=not self.config.enabled)
        if not self.config.enabled or not (text or "").strip():
            return result
        if not self.ensure_ready():
            return result
        now = self._now() if at is None else float(at)
        try:
            identity = self._identity(platform=platform, member_id=member_id)
            if identity is None:
                result.error = "no_identity"
                return result
            scene = self._scene(platform=platform, session=session, group_id=group_id)
            result.identity = identity.as_dict()
            result.auto_participation, result.intimacy_allowed = self.store.contact_preferences(identity)
            paused = self.store.is_paused(identity) or self.store.session_paused(identity, scene.session)
            clean = text.strip()[: self.MAX_TEXT]
            control = re.sub(r"[。.!！~～\s]+$", "", clean).strip()
            controls = {
                "别主动找我": {"auto": False}, "不要主动找我": {"auto": False},
                "以后别主动找我了": {"auto": False}, "先别主动找我": {"auto": False},
                "可以主动找我了": {"auto": True}, "可以正常接话了": {"auto": True},
                "我们别这么亲近": {"intimacy": False}, "不要那么亲密": {"intimacy": False},
                "先保持距离": {"intimacy": False}, "可以亲近一点": {"intimacy": True},
                "不用那么生分": {"intimacy": True},
            }
            control_applies = control in controls and (directed or not group_id)
            if control_applies:
                self.store.set_contact_preferences(identity, now, **controls[control])
            result.auto_participation, result.intimacy_allowed = self.store.contact_preferences(identity)
            if control_applies or paused:
                return result
            capture = self.classify_message(clean, directed=directed)
            if group_id and not directed:
                # Ambient group speech is not evidence about this relationship.
                # A confirmed eventual reply may still establish familiarity.
                capture.update(trust="", mood_negative=False, refusal_request=False,
                               subject="unknown")
            self.counters["captures"] += 1
            if not is_media:
                self.store.expire_pending(now)
                self._maybe_cleanup(now)
                self.store.record_source(identity, scene, message_id=message_id, text=clean, at=now,
                                         explicit_secret=len(text.strip()) > self.MAX_TEXT)
                if not capture["farming"] or accumulation.social_ritual(clean):
                    # User-approved accumulation replaces blanket greeting exclusion;
                    # score commands/purchases stay blocked. The sent hook allocates credit.
                    self.store.add_pending(identity, scene, message_id=message_id,
                                           text=clean, at=now)
                result.affinity = self._affinity(identity, now)
            if capture["trust"]:
                evidence = engine.Evidence(
                    event_id=event_key(self.character_id, identity.key(), scene.session, message_id,
                                       "trust_" + capture["trust"],
                                       fingerprint(clean)),
                    kind="trust_positive" if capture["trust"] == "positive" else "trust_negative",
                    strength=capture["strength"], subject=capture["subject"],
                    source_session=scene.session, source_message_id=message_id,
                    fingerprint=fingerprint(clean), at=now, verified=not is_media,
                    meaningful=False, day=day_for(now))
                self.store.evaluate_trust(identity, scene, evidence, now)
            if capture["mood_negative"]:
                self._open_mood(identity, at=now, reason="member_hostility",
                                trigger_event_id=message_id)
            window = {}
            if capture["refusal_request"] and not self.is_bypass(clean):
                window = self.store.open_topic_window(
                    identity, topic_key=capture["topic_signature"], at=now,
                    reason="member_asked_to_stop: " + capture["topic_signature"][:40],
                    trigger_event_id=message_id)
                if window.get("opened"):
                    result.topic_label = capture["topic_signature"]
            else:
                active = self._matching_topic_window(identity, clean, now)
                bypass = self.is_bypass(clean)
                if active and not bypass:
                    result.suppress = True
                    result.suppress_reason = "topic_window"
                    result.topic_label = active.get("topic_key", "")
                    self.counters["suppressed"] += 1
                    if capture["subject"] == "member":
                        # Repeated violation of the closed topic is a repeat
                        # boundary breach, not an automatic trust wipe.
                        evidence = engine.Evidence(
                            event_id=event_key(self.character_id, identity.key(), scene.session,
                                               message_id, "boundary_repeat", fingerprint(clean)),
                            kind="trust_negative", strength="normal",
                            subject="member", source_session=scene.session,
                            source_message_id=message_id, fingerprint=fingerprint(clean),
                            at=now, verified=True, meaningful=False, day=day_for(now))
                        self.store.evaluate_trust(identity, scene, evidence, now)
                # Bypass messages (privacy/memory controls, system error feedback,
                # explanations/corrections) are never silenced by a topic window;
                # they also do not extend it (R-36/R-37/CASE-054).
            result.recent_negative = self._recent_negative(identity, now)
            return result
        except Exception as error:  # noqa: BLE001 - R-03
            self._degrade(error)
            result.error = self._error
            return result

    def _affinity(self, identity: MemberIdentity, now: float) -> int:
        try:
            snapshot = self.store.snapshot(identity, now)
        except Exception:  # noqa: BLE001
            return 0
        state = engine.RelationshipState(
            familiarity=snapshot["familiarity"], trust=snapshot["trust"],
            stage=snapshot["stage"], generation=snapshot["generation"],
            meaningful_episodes=snapshot["episodes"], active_days=snapshot["active_days"],
            seat_status=snapshot["seat_status"])
        return engine.relationship_affinity(state)

    def _open_mood(self, identity: MemberIdentity, *, at: float, reason: str,
                   trigger_event_id: str = "") -> None:
        self.store.open_mood(identity, at=at, duration=params.MOOD_ORDINARY_VALIDITY_SECONDS,
                             reason=reason, trigger_event_id=trigger_event_id)

    def _recent_negative(self, identity: MemberIdentity, now: float) -> bool:
        try:
            if self.store.active_mood(identity, now):
                return True
            events = self.store.recent_events(identity, limit=6)
            negatives = [row for row in events
                         if row.get("kind") in {"trust_negative", "mood_negative"}]
            return bool(negatives) and now - float(negatives[0]["decided_at"]) <= params.MOOD_ORDINARY_VALIDITY_SECONDS
        except Exception:  # noqa: BLE001
            return False

    def _matching_topic_window(self, identity: MemberIdentity, text: str, now: float) -> dict:
        """Topic suppression only when a bounded window exists; never by person (R-37)."""
        try:
            for window in self.store.active_topics(identity, now):
                if topic_matches(window.get("topic_key", ""), text):
                    return dict(window)
        except Exception:  # noqa: BLE001
            return {}
        return {}

    def on_reply_sent(self, *, platform: str, session: str, group_id: str, member_id: str,
                      message_id: str, incoming_text: str, reply_text: str,
                      at: float | None = None, confirmed: bool = True) -> dict:
        """Commit or discard the pending interaction after the send result."""
        outcome = {"committed": False, "reason": ""}
        if not self.config.enabled:
            return outcome
        if not self.ensure_ready():
            return outcome
        now = self._now() if at is None else float(at)
        try:
            identity = self._identity(platform=platform, member_id=member_id)
            if identity is None:
                return outcome
            scene = self._scene(platform=platform, session=session, group_id=group_id)
            if not confirmed or not (reply_text or "").strip():
                self.store.drop_pending(identity, scene.session, message_id)
                outcome["reason"] = "send_not_confirmed"
                return outcome
            result = self.store.evaluate_commit(
                identity, scene, message_id=message_id, at=now,
                meaningful_hint=self.implies_meaningful(incoming_text))
            outcome.update(committed=bool(result.get("committed")),
                           reason=result.get("reason", ""),
                           delta_f=result.get("delta_f", 0.0),
                           delta_t=result.get("delta_t", 0.0),
                           stage=result.get("stage", ""),
                           seat_action=result.get("seat_action", ""))
            self.counters["commits"] += 1
            # A delivered refusal by the character closes only that topic, with
            # an absolute expiry and a private reason (R-35/R-37).
            if REFUSAL_RE.search(reply_text or "") and TOPIC_STOP_RE.search(incoming_text or ""):
                sig = topic_signature(incoming_text)
                if sig:
                    self.store.open_topic_window(
                        identity, topic_key=sig, at=now,
                        reason="character_declined_topic: " + sig[:40],
                        trigger_event_id=message_id)
            return outcome
        except Exception as error:  # noqa: BLE001 - R-03
            self._degrade(error)
            outcome["reason"] = "error"
            return outcome

    # ---------------------------------------------------------------- prompt
    def prompt_parts(self, *, platform: str, session: str, group_id: str, member_id: str,
                     query: str, at: float | None = None,
                     refusal_hint: bool = False) -> dict:
        """Return filtered model context; never raises into the reply path."""
        empty = {"parts": [], "affinity": 0, "error": "", "visible_count": 0}
        if not self.config.enabled:
            return empty
        if not self.ensure_ready():
            return {**empty, "error": self._error}
        now = self._now() if at is None else float(at)
        try:
            identity = self._identity(platform=platform, member_id=member_id)
            if identity is None:
                return {**empty, "error": "no_identity"}
            scene = self._scene(platform=platform, session=session, group_id=group_id)
            paused = self.store.is_paused(identity) or self.store.session_paused(identity, scene.session)
            visible = [] if paused else self.store.visible_items(
                identity, scene, query=query, now=now, limit=self.config.max_visible)
            snapshot = self.store.snapshot(identity, now)
            state = engine.RelationshipState(
                familiarity=snapshot["familiarity"], trust=snapshot["trust"],
                stage=snapshot["stage"], generation=snapshot["generation"],
                meaningful_episodes=snapshot["episodes"], active_days=snapshot["active_days"],
                seat_status=snapshot["seat_status"])
            auto_allowed, intimacy_allowed = self.store.contact_preferences(identity)
            parts = context.render_prompt_parts(
                state, self.character_id, visible_items=visible,
                recent_negative=self._recent_negative(identity, now),
                intimacy_allowed=intimacy_allowed)
            if not intimacy_allowed:
                parts.append("<relationship_boundary>对方明确希望保持普通距离，不主动亲昵或试探；正常交流即可。</relationship_boundary>")
            affinity = engine.relationship_affinity(state) if auto_allowed else 0
            return {"parts": parts, "affinity": affinity, "error": "",
                    "visible_count": len(visible),
                    "reason": "paused" if paused else ""}
        except Exception as error:  # noqa: BLE001 - fail closed for shared recall
            self._degrade(error)
            return {**empty, "error": self._error}

    # --------------------------------------------------------------- controls
    def member_identity(self, *, platform: str, member_id: str) -> MemberIdentity | None:
        if not self.config.enabled or not self.ensure_ready():
            return None
        return self._identity(platform=platform, member_id=member_id)

    def forget_source(self, identity: MemberIdentity, *, session: str | None = None,
                      message_id: str | None = None, text: str | None = None,
                      at: float | None = None) -> dict:
        """Cascade a memory deletion/correction into shared index + relationship evidence."""
        if not self.config.enabled or identity is None or not self.ensure_ready():
            return {}
        try:
            return self.store.forget_source(identity, session=session, message_id=message_id,
                                             text=text, at=self._now() if at is None else at)
        except Exception as error:  # noqa: BLE001
            self._degrade(error)
            return {}

    def correct_source(self, identity: MemberIdentity, *, session: str, message_id: str,
                       replacement: str, replacement_message_id: str,
                       group_id: str = "", at: float | None = None) -> dict:
        if not self.config.enabled or identity is None or not self.ensure_ready():
            return {}
        try:
            return self.store.correct_source(
                identity, session=session, message_id=message_id,
                replacement=replacement, replacement_message_id=replacement_message_id,
                group_id=group_id, at=self._now() if at is None else at)
        except Exception as error:  # noqa: BLE001
            self._degrade(error)
            return {}

    def pause_session(self, identity: MemberIdentity, *, session: str, paused: bool,
                      at: float | None = None) -> None:
        if not self.config.enabled or identity is None or not self.ensure_ready():
            return
        try:
            self.store.set_session_paused(identity, session, paused,
                                          self._now() if at is None else at)
        except Exception as error:  # noqa: BLE001
            self._degrade(error)

    # --------------------------------------------------------------- commands
    def command_action(self, action: str, rest: str, *, platform: str, session: str,
                       group_id: str, member_id: str, at: float | None = None) -> str:
        if not self.config.enabled:
            return "关系功能当前已关闭，原有对话不受影响。"
        if not self.ensure_ready():
            return "关系功能暂时不可用，原有对话不受影响。"
        now = self._now() if at is None else float(at)
        identity = self._identity(platform=platform, member_id=member_id)
        if identity is None:
            return "无法确认当前账号身份，操作未执行。"
        action = (action or "").strip()
        try:
            if action in {"查看", "状态"}:
                return "相处的感受可以直接聊；这里不提供关系状态查询。"
            if action == "重置":
                result = self.store.reset_relationship(identity, now)
                return ("已按你的要求重置我们的关系记录；"
                        "共同记忆仍需另行管理。")
            if action == "暂停":
                self.store.set_paused(identity, True, now)
                self.store.set_session_paused(identity, session, True, now)
                return "已暂停记录我们的新关系互动；原有对话照常，可随时说“开启”。"
            if action == "开启":
                self.store.set_paused(identity, False, now)
                self.store.set_session_paused(identity, session, False, now)
                return "已恢复记录关系互动；之前的记录不会因此自动增加。"
            if action in {"记住", "忘记"}:
                return "关系相关操作请用 /记忆 命令管理具体记忆条目。"
            return ("用法：/关系 重置｜暂停｜开启。只作用于你自己的账号；"
                    "查看不会显示分数或内部阶段。")
        except Exception as error:  # noqa: BLE001
            self._degrade(error)
            return "关系操作暂时不可用，原有对话不受影响。"

    # ------------------------------------------------- model proposal channel
    ALLOWED_PROPOSAL_KINDS = {"effective_interaction", "trust_positive", "trust_negative",
                              "severe_negative", "mood_negative", "uncertain", "invalid_farming"}
    ALLOWED_STRENGTHS = {"normal", "strong"}
    ALLOWED_SUBJECTS = {"member", "quoted", "other", "unknown"}

    def assess_proposal(self, proposal: Mapping, *, platform: str, session: str,
                        group_id: str, member_id: str, message_id: str, text: str,
                        at: float | None = None) -> dict:
        """Validate a constrained model proposal; reject malformed/unauthorised.

        The proposal can only reference the *current* member and current message;
        it cannot set scores, seats, visibility or target another member.
        """
        self.counters["model_tool_rounds"] += 1
        decision = {"accepted": False, "reason": ""}
        if not isinstance(proposal, Mapping):
            decision["reason"] = "malformed"
            return decision
        kind = str(proposal.get("kind", "")).strip()
        strength = str(proposal.get("strength", "normal")).strip()
        subject = str(proposal.get("subject", "unknown")).strip()
        source_id = str(proposal.get("source_message_id", "")).strip()
        if kind not in self.ALLOWED_PROPOSAL_KINDS or strength not in self.ALLOWED_STRENGTHS \
                or subject not in self.ALLOWED_SUBJECTS:
            decision["reason"] = "out_of_range"
            return decision
        if kind == "effective_interaction":
            # Only the adapter's sent-confirmed callback may commit familiarity.
            decision["reason"] = "send_confirmation_required"
            return decision
        if self.is_bypass(text) or self.is_farming(text) or self.subject_of(text) != "member":
            decision["reason"] = "unverified_attribution"
            return decision
        if source_id and source_id != message_id:
            decision["reason"] = "source_mismatch"
            return decision
        if not self.ensure_ready():
            decision["reason"] = "unavailable"
            return decision
        now = self._now() if at is None else float(at)
        try:
            identity = self._identity(platform=platform, member_id=member_id)
            if identity is None:
                decision["reason"] = "no_identity"
                return decision
            scene = self._scene(platform=platform, session=session, group_id=group_id)
            evidence = engine.Evidence(
                event_id=event_key(self.character_id, identity.key(), session, message_id,
                                   "tool_" + kind, fingerprint(text)),
                kind=kind, strength=strength, subject=subject,
                source_session=session, source_message_id=message_id,
                fingerprint=fingerprint(text), at=now,
                verified=subject == "member" and kind != "uncertain",
                meaningful=bool(proposal.get("meaningful")) and kind == "effective_interaction",
                day=day_for(now))
            started = time.perf_counter()
            result = self.store.evaluate_trust(identity, scene, evidence, now, source_text=text)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.counters["tool_rounds_ms_total"] = round(
                self.counters.get("tool_rounds_ms_total", 0.0) + elapsed_ms, 3)
            # R-13/R-24: the model only learns whether its candidate was
            # accepted and a qualitative reason; numeric internals stay private.
            decision.update(accepted=bool(result.get("accepted")),
                            reason="evaluated",
                            limit_reason=result.get("limit_reason", ""))
        except Exception as error:  # noqa: BLE001
            self._degrade(error)
            decision["reason"] = "error"
        return decision

    # ------------------------------------------------------------ diagnostics
    def private_diagnostics(self, *, platform: str, session: str, group_id: str,
                            member_id: str, at: float | None = None) -> dict:
        """Local/private only. Never rendered into a chat reply (R-13/R-24)."""
        if not self.ensure_ready():
            return {"available": False, "error": self._error}
        now = self._now() if at is None else float(at)
        identity = self._identity(platform=platform, member_id=member_id)
        if identity is None:
            return {"available": False, "error": "no_identity"}
        snapshot = self.store.snapshot(identity, now)
        return {"available": True, "snapshot": snapshot,
                "recent_events": self.store.recent_events(identity, limit=10),
                "counters": dict(self.counters)}
