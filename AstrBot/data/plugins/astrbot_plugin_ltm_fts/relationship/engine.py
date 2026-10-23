"""Pure cross-scene relationship engine (M2).

This module implements the ``engine`` part of
``evaluation/relationship/INTERFACES.md``.  It is deliberately pure:

* no I/O, no database, no model call, no network, no randomness, no clock read;
* every timestamp is passed in explicitly as epoch seconds;
* every day string is an ``Asia/Shanghai`` calendar day supplied by the caller
  (use :func:`day_for` to derive it from a timestamp);
* all exposed state objects are frozen dataclasses and every function returns
  new objects -- inputs are never mutated;
* engineering constants are imported from :mod:`.params` and are never
  re-tuned here (no hidden bonuses, no inactivity decay).

Responsibilities split with the caller (``store``/``service``), which owns:

* persistence and transactions (including "at most one of two concurrent
  claims wins" for the two closest seats);
* the ``active_days`` counter and the generation-scoped idempotency keys;
* storing/expiring :class:`MoodWindow` objects;
* persisting ``seen_event_ids`` and ``seen_fingerprints`` reported through the
  decisions (see :func:`evaluate`).

``evaluate`` never writes ``seen_event_ids`` / ``seen_fingerprints`` back to the
caller; it returns decisions whose audit trail tells the caller what to persist.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

try:  # normal package import
    from . import params, accumulation
except ImportError:  # pragma: no cover - direct file loading fallback
    import params  # type: ignore[no-redef]
    import accumulation  # type: ignore[no-redef]

__all__ = [
    "DAY_FORMAT",
    "STAGES",
    "SEAT_STATUSES",
    "SEAT_ACTIONS",
    "EVIDENCE_KINDS",
    "STRENGTHS",
    "SUBJECTS",
    "LIMIT_REASONS",
    "RelationshipState",
    "DailyCounters",
    "Evidence",
    "Decision",
    "Evaluation",
    "MoodWindow",
    "evaluate",
    "reset_state",
    "apply_correction",
    "new_mood_window",
    "mood_active",
    "extend_topic_window",
    "relationship_affinity",
    "day_for",
]

DAY_FORMAT = "%Y-%m-%d"

STAGES = ("initial", "familiar", "trusted", "closest", "distant_with_history")
SEAT_STATUSES = ("none", "active", "paused")
SEAT_ACTIONS = ("none", "claim", "reject_capacity", "release", "pause")
EVIDENCE_KINDS = (
    "effective_interaction",
    "trust_positive",
    "trust_negative",
    "severe_negative",
    "mood_negative",
    "invalid_farming",
    "uncertain",
    "topic_boundary",
)
STRENGTHS = ("normal", "strong")
SUBJECTS = ("member", "quoted", "other", "unknown")
LIMIT_REASONS = (
    "",
    "daily_cap",
    "interval",
    "duplicate",
    "uncertain",
    "subject_mismatch",
    "unverified",
    "floor",
    "ceiling",
    "farming",
)

MOOD_KIND_ORDINARY = "ordinary"
MOOD_KIND_TOPIC = "topic_disengagement"


# ---------------------------------------------------------------------------
# frozen value objects (contract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationshipState:
    """Long-term relationship state of one member towards one character."""

    familiarity: float = params.F_INITIAL
    trust: float = params.T_INITIAL
    stage: str = "initial"
    generation: int = 0
    meaningful_episodes: int = 0
    active_days: int = 0
    closest_since: float | None = None
    seat_status: str = "none"  # none|active|paused
    last_f_positive_at: float | None = None
    recent_negative_days: tuple[str, ...] = ()


@dataclass(frozen=True)
class DailyCounters:
    """Per-day accounting caps for one member (Asia/Shanghai day)."""

    day: str
    f_positive: float = 0.0
    t_positive: float = 0.0
    t_negative_magnitude: float = 0.0
    last_f_positive_at: float | None = None


@dataclass(frozen=True)
class Evidence:
    """One piece of verifiable evidence proposed by the constrained judge."""

    event_id: str
    kind: str
    strength: str
    subject: str
    source_session: str
    source_message_id: str
    fingerprint: str
    at: float
    verified: bool
    meaningful: bool
    day: str
    credit: accumulation.Credit | None = None


@dataclass(frozen=True)
class Decision:
    """Auditable outcome for one evidence event."""

    accepted: bool
    event_id: str
    delta_f: float
    delta_t: float
    reason: str
    limit_reason: str
    stage_after: str
    seat_action: str  # none|claim|reject_capacity|release|pause
    meaningful_episode: bool
    audit: dict


@dataclass(frozen=True)
class Evaluation:
    """Result of one evaluation transaction (pure; nothing is persisted here)."""

    state: RelationshipState
    counters: DailyCounters
    decisions: tuple[Decision, ...]
    seat_action: str
    rejected_events: tuple[dict, ...]


@dataclass(frozen=True)
class MoodWindow:
    """Temporary mood / topic-disengagement window (stored by the caller)."""

    kind: str  # ordinary|topic_disengagement
    topic_key: str
    started_at: float
    expires_at: float


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


def day_for(at: float) -> str:
    """Return the ``Asia/Shanghai`` calendar day for an epoch-second timestamp.

    The configured ``params.TZ`` is used explicitly; the host local timezone is
    never consulted.  Invalid input falls back to the epoch start (1970-01-01
    UTC+8) instead of guessing a local time.
    """
    stamp = _coerce_float(at, 0.0)
    return datetime.fromtimestamp(stamp, tz=params.TZ).strftime(DAY_FORMAT)


def _coerce_float(value: object, default: float | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    return number


def _coerce_int(value: object, default: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value >= minimum else default


def _optional_float(value: object) -> float | None:
    return _coerce_float(value, None)


def _valid_day(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        datetime.strptime(value, DAY_FORMAT)
    except ValueError:
        return False
    return True


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalise_negative_days(days: object) -> list[str]:
    if isinstance(days, str) or not isinstance(days, (tuple, list)):
        return []
    cleaned = sorted({day for day in days if _valid_day(day)})
    return cleaned[-params.NEGATIVE_DAY_RING :]


def _normalise_seen_ids(seen: object) -> set[str]:
    if seen is None:
        return set()
    if isinstance(seen, (set, frozenset, list, tuple)):
        return {item for item in seen if isinstance(item, str) and item}
    if isinstance(seen, Mapping):
        return {key for key in seen if isinstance(key, str) and key}
    return set()


def _normalise_fingerprints(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    normalised: dict[str, float] = {}
    for key, stamp in value.items():
        if not isinstance(key, str) or not key:
            continue
        number = _coerce_float(stamp, None)
        if number is not None:
            normalised[key] = number
    return normalised


def _coerce_seats(seats_taken: object) -> int:
    """Fail closed: unknown capacity information is treated as "full"."""
    if isinstance(seats_taken, bool) or not isinstance(seats_taken, int) or seats_taken < 0:
        return params.CLOSEST_CAPACITY
    return seats_taken


def _coerce_events(events: object) -> list[Evidence]:
    if events is None:
        return []
    if isinstance(events, (str, bytes)):
        return []
    if not isinstance(events, Iterable):
        return []
    return [item for item in events if isinstance(item, Evidence)]


def _iter_evidence(events: object) -> Iterable[object]:
    if events is None:
        return ()
    if isinstance(events, (str, bytes)):
        return ()
    if not isinstance(events, Iterable):
        return ()
    return events


# ---------------------------------------------------------------------------
# internal mutable accumulator (never exposed)
# ---------------------------------------------------------------------------


@dataclass
class _Accumulator:
    day: str = ""
    f_positive: float = 0.0
    t_positive: float = 0.0
    t_negative_magnitude: float = 0.0
    last_f_positive_at: float | None = None
    familiarity: float = params.F_INITIAL
    trust: float = params.T_INITIAL
    episodes: int = 0
    recent_negative_days: list[str] = field(default_factory=list)
    fingerprints: dict[str, float] = field(default_factory=dict)
    fingerprint_sources: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _Outcome:
    event_id: str
    accepted: bool = False
    delta_f: float = 0.0
    delta_t: float = 0.0
    reason: str = ""
    limit_reason: str = ""
    meaningful_episode: bool = False
    negative_day: str | None = None
    severe: bool = False
    mood_only: bool = False
    topic_only: bool = False
    fingerprint_counted: bool = False
    audit: dict = field(default_factory=dict)


def _reject(
    event_id: str,
    reason: str,
    limit_reason: str,
    *,
    audit: dict | None = None,
) -> _Outcome:
    base: dict = {
        "rule_version": params.RULE_VERSION,
        "reason": reason,
        "limit_reason": limit_reason,
    }
    if audit:
        base.update(audit)
        base["reason"] = reason
        base["limit_reason"] = limit_reason
    return _Outcome(
        event_id=event_id,
        accepted=False,
        reason=reason,
        limit_reason=limit_reason,
        audit=base,
    )


# ---------------------------------------------------------------------------
# evidence processing (shared by evaluate and apply_correction)
# ---------------------------------------------------------------------------


def _process_event(event: object, acc: _Accumulator) -> _Outcome:
    """Validate one evidence item, score it and mutate the internal accumulator.

    ``acc.seen`` / ``acc.fingerprints`` are private copies: the caller's sets
    and dicts are never modified by the public functions.
    """
    if not isinstance(event, Evidence):
        return _reject(
            "",
            "malformed_event",
            "uncertain",
            audit={"event_type": type(event).__name__},
        )

    event_id = event.event_id
    if not isinstance(event_id, str) or not event_id:
        return _reject("", "malformed_event", "uncertain", audit={"field": "event_id"})
    if event_id in acc.seen:
        return _reject(event_id, "idempotent_replay", "duplicate", audit={"at": None})
    acc.seen.add(event_id)

    at = _coerce_float(event.at, None)
    if at is None:
        return _reject(event_id, "invalid_timestamp", "uncertain")
    day = event.day
    if not _valid_day(day):
        return _reject(event_id, "invalid_day", "uncertain", audit={"at": at})
    derived_day = day_for(at)
    if derived_day != day:
        return _reject(
            event_id,
            "day_mismatch",
            "uncertain",
            audit={"at": at, "day": day, "derived_day": derived_day},
        )
    if acc.day and day < acc.day:
        # A delayed event for an already-closed accounting day cannot be
        # applied without guessing that day's cap usage; fail closed.
        return _reject(
            event_id,
            "stale_day",
            "uncertain",
            audit={"at": at, "day": day, "accounting_day": acc.day},
        )

    kind = event.kind
    if kind not in EVIDENCE_KINDS:
        return _reject(event_id, "unknown_kind", "uncertain", audit={"at": at, "day": day})
    subject = event.subject if isinstance(event.subject, str) else ""
    if subject not in SUBJECTS:
        return _reject(event_id, "unknown_subject", "uncertain", audit={"at": at, "day": day})
    verified = event.verified
    if not isinstance(verified, bool):
        return _reject(event_id, "invalid_verified_flag", "uncertain", audit={"at": at, "day": day})
    strength = event.strength if isinstance(event.strength, str) else ""
    meaningful = event.meaningful is True
    fingerprint = event.fingerprint if isinstance(event.fingerprint, str) else ""
    source_session = event.source_session if isinstance(event.source_session, str) else ""
    source_message_id = event.source_message_id if isinstance(event.source_message_id, str) else ""

    # Accounting day rollover: the caps are per Asia/Shanghai day.
    if acc.day != day:
        acc.day = day
        acc.f_positive = 0.0
        acc.t_positive = 0.0
        acc.t_negative_magnitude = 0.0

    audit: dict = {
        "rule_version": params.RULE_VERSION,
        "kind": kind,
        "subject": subject,
        "strength": strength,
        "verified": verified,
        "meaningful": meaningful,
        "source_session": source_session,
        "source_message_id": source_message_id,
        "fingerprint": fingerprint,
        "at": at,
        "day": day,
    }

    # --- evidence that cannot change long-term F / T ----------------------
    if kind == "invalid_farming":
        return _reject(event_id, "farming", "farming", audit=audit)
    if kind == "uncertain":
        return _reject(event_id, "uncertain_evidence", "uncertain", audit=audit)
    if kind == "topic_boundary":
        return _Outcome(
            event_id=event_id,
            accepted=True,
            reason="topic_boundary",
            topic_only=True,
            audit={**audit, "outcome": "topic_boundary"},
        )
    if kind == "mood_negative":
        return _Outcome(
            event_id=event_id,
            accepted=True,
            reason="mood_only",
            mood_only=True,
            audit={**audit, "outcome": "mood_only"},
        )

    # --- long-term changes need a verified, attributable member action ----
    if subject != "member":
        return _reject(event_id, "subject_mismatch", "subject_mismatch", audit=audit)
    if not verified:
        return _reject(event_id, "unverified", "unverified", audit=audit)
    if kind in ("trust_positive", "trust_negative", "severe_negative") and strength not in STRENGTHS:
        return _reject(event_id, "invalid_strength", "uncertain", audit=audit)

    credit = event.credit if kind == "effective_interaction" else None
    if credit is not None:
        if not isinstance(credit, accumulation.Credit) or not credit.valid(at):
            return _reject(event_id, "invalid_familiarity_credit", "uncertain", audit=audit)
        if acc.last_f_positive_at is not None and at < acc.last_f_positive_at:
            return _reject(event_id, "stale_credit", "uncertain", audit=audit)
        if (acc.last_f_positive_at is not None
                and at - acc.last_f_positive_at < accumulation.UNIT_SECONDS
                and credit.unit_start != acc.last_f_positive_at):
            return _reject(event_id, "exchange_anchor_mismatch", "uncertain", audit=audit)
        audit["f_credit"] = credit.audit()
        meaningful = meaningful and credit.episode_eligible

    # Cross-scene content dedupe: one piece of content counts at most once per
    # DUPLICATE_CONTENT_WINDOW_SECONDS per member, regardless of session.
    previous = acc.fingerprints.get(fingerprint) if fingerprint else None
    same_source_f = (kind == "effective_interaction" and bool(source_message_id)
                     and acc.fingerprint_sources.get(fingerprint) in {
                         (source_session, source_message_id, "trust_positive"),
                         (source_session, source_message_id, "trust_negative"),
                         (source_session, source_message_id, "severe_negative")})
    if credit is None and previous is not None and not same_source_f and (at - previous) < params.DUPLICATE_CONTENT_WINDOW_SECONDS:
        return _reject(event_id, "duplicate_content", "duplicate", audit=audit)

    delta_f = 0.0
    delta_t = 0.0
    limit_reason = ""
    episode = False
    new_negative_day = False
    severe = kind == "severe_negative"
    counted_fingerprint = False

    if kind == "effective_interaction":
        last_positive = acc.last_f_positive_at
        if last_positive is not None and (at - last_positive) < params.F_MIN_INCREMENT_INTERVAL_SECONDS and (credit is None or credit.gain <= 0):
            limit_reason = "interval"
        else:
            remaining_daily = params.F_DAILY_POSITIVE_CAP - acc.f_positive
            headroom = params.F_MAX - acc.familiarity
            candidate = credit.gain if credit is not None else params.F_EFFECTIVE_INCREMENT
            if remaining_daily <= 0.0:
                limit_reason = "daily_cap"
            elif headroom <= 0.0:
                limit_reason = "ceiling"
            else:
                delta_f = min(candidate, remaining_daily, headroom)
                if delta_f > 0.0:
                    acc.familiarity += delta_f
                    acc.f_positive += delta_f
                    acc.last_f_positive_at = credit.unit_start if credit is not None else at
                    if delta_f < candidate:
                        limit_reason = "daily_cap" if remaining_daily < candidate else "ceiling"
        if meaningful:
            # A meaningful, verified, non-duplicate interaction is an episode
            # even when the daily F cap or the 20-minute interval limits F.
            acc.episodes += 1
            episode = True
        if fingerprint and (delta_f > 0.0 or episode):
            acc.fingerprints[fingerprint] = at
            acc.fingerprint_sources[fingerprint] = (source_session, source_message_id, kind)
            counted_fingerprint = True

    elif kind == "trust_positive":
        candidate = params.T_STRONG_POSITIVE if strength == "strong" else params.T_NORMAL_POSITIVE
        remaining_daily = params.T_DAILY_POSITIVE_CAP - acc.t_positive
        headroom = params.T_MAX - acc.trust
        if remaining_daily <= 0.0:
            limit_reason = "daily_cap"
        elif headroom <= 0.0:
            limit_reason = "ceiling"
        else:
            delta_t = min(candidate, remaining_daily, headroom)
            if delta_t > 0.0:
                acc.trust += delta_t
                acc.t_positive += delta_t
                if delta_t < candidate:
                    limit_reason = "daily_cap" if remaining_daily < candidate else "ceiling"
        if meaningful:
            acc.episodes += 1
            episode = True
        if fingerprint and (delta_t > 0.0 or episode):
            acc.fingerprints[fingerprint] = at
            acc.fingerprint_sources[fingerprint] = (source_session, source_message_id, kind)
            counted_fingerprint = True

    elif kind in ("trust_negative", "severe_negative"):
        magnitude = (
            abs(params.T_STRONG_NEGATIVE) if strength == "strong" else abs(params.T_NORMAL_NEGATIVE)
        )
        remaining_cap = params.T_DAILY_NEGATIVE_MAGNITUDE_CAP - acc.t_negative_magnitude
        applied = min(magnitude, max(0.0, remaining_cap))
        if applied <= 0.0:
            limit_reason = "daily_cap"
        else:
            if applied < magnitude:
                limit_reason = "daily_cap"
            floor_headroom = acc.trust - params.T_MIN
            if applied > floor_headroom:
                # A single event must never push trust below the floor.
                applied = max(0.0, floor_headroom)
                limit_reason = "floor"
            if applied > 0.0:
                acc.trust -= applied
                acc.t_negative_magnitude += applied
                delta_t = -applied
            else:
                limit_reason = "floor"
        # Valid negative evidence is recorded on its distinct day even when the
        # daily magnitude cap / floor truncated the numeric change.
        if day not in acc.recent_negative_days:
            acc.recent_negative_days.append(day)
            acc.recent_negative_days.sort()
            acc.recent_negative_days = acc.recent_negative_days[-params.NEGATIVE_DAY_RING :]
            new_negative_day = True
        if fingerprint:
            acc.fingerprints[fingerprint] = at
            acc.fingerprint_sources[fingerprint] = (source_session, source_message_id, kind)
            counted_fingerprint = True

    accepted = bool(delta_f or delta_t or episode or new_negative_day)
    reason = "accepted" if accepted else (limit_reason or "no_effect")

    audit.update(
        {
            "delta_f": delta_f,
            "delta_t": delta_t,
            "f_positive": acc.f_positive,
            "t_positive": acc.t_positive,
            "t_negative_magnitude": acc.t_negative_magnitude,
            "familiarity": acc.familiarity,
            "trust": acc.trust,
            "meaningful_episodes": acc.episodes,
            "meaningful_episode": episode,
            "negative_day_recorded": new_negative_day,
            "severe": severe,
            "fingerprint_counted": counted_fingerprint,
            "limit_reason": limit_reason,
            "reason": reason,
        }
    )
    return _Outcome(
        event_id=event_id,
        accepted=accepted,
        delta_f=delta_f,
        delta_t=delta_t,
        reason=reason,
        limit_reason=limit_reason,
        meaningful_episode=episode,
        negative_day=day if new_negative_day else None,
        severe=severe,
        fingerprint_counted=counted_fingerprint,
        audit=audit,
    )


# ---------------------------------------------------------------------------
# stage / seat resolution
# ---------------------------------------------------------------------------


def _stage_middle(
    *,
    familiarity: float,
    trust: float,
    episodes: int,
    current_stage: str,
) -> str:
    """Return the middle stage for a member without an occupied closest seat.

    Promotion needs the full entry conditions.  Demotion uses the retention
    thresholds from ``params`` so ordinary fluctuations do not bounce the stage
    around.  ``distant_with_history`` preserves the fact that the member was
    once familiar while trust is currently below the familiar retention line.
    """
    if (
        familiarity >= params.TRUSTED_F
        and trust >= params.TRUSTED_T
        and episodes >= params.TRUSTED_EPISODES
    ):
        return "trusted"
    if (
        current_stage == "trusted"
        and familiarity >= params.TRUSTED_F
        and trust >= params.TRUSTED_RETAIN_T
    ):
        return "trusted"
    history = familiarity >= params.FAMILIAR_F or current_stage in (
        "familiar",
        "trusted",
        "distant_with_history",
    )
    if history and familiarity >= params.FAMILIAR_F:
        if trust >= params.FAMILIAR_RETAIN_T:
            return "familiar"
        return "distant_with_history"
    return "initial"


def _closest_eligible(*, familiarity: float, trust: float, episodes: int, active_days: int) -> bool:
    return (
        familiarity >= params.CLOSEST_F
        and trust >= params.CLOSEST_T
        and active_days >= params.CLOSEST_ACTIVE_DAYS
        and episodes >= params.CLOSEST_EPISODES
    )


def _leave_condition(*, trust: float, recent_negative_days: list[str]) -> bool:
    return (
        trust < params.CLOSEST_LEAVE_T
        and len(recent_negative_days) >= params.CLOSEST_NEGATIVE_DISTINCT_DAYS
    )


def _resolve_status(
    *,
    prior_stage: str,
    prior_seat: str,
    prior_closest_since: float | None,
    familiarity: float,
    trust: float,
    episodes: int,
    active_days: int,
    recent_negative_days: list[str],
    had_severe_evidence: bool,
    seats_taken: int,
    now: float | None,
) -> tuple[str, str, float | None, str]:
    """Return ``(stage, seat_status, closest_since, seat_action)``."""
    stage = prior_stage if prior_stage in STAGES else "initial"
    seat = prior_seat if prior_seat in SEAT_STATUSES else "none"
    closest_since = _optional_float(prior_closest_since)

    release = _leave_condition(trust=trust, recent_negative_days=recent_negative_days)
    eligible_closest = _closest_eligible(
        familiarity=familiarity,
        trust=trust,
        episodes=episodes,
        active_days=active_days,
    )

    # An occupied closest seat keeps its seat until the documented leave
    # condition fires; newcomers cannot evict it (R-26/R-28).
    if seat in ("active", "paused"):
        if release:
            stage = _stage_middle(
                familiarity=familiarity,
                trust=trust,
                episodes=episodes,
                current_stage="trusted",
            )
            return stage, "none", None, "release"
        if seat == "active" and had_severe_evidence:
            # A single severe event pauses intimate expression but keeps the
            # seat while the evidence is processed (R-29 / CASE-044).
            return "closest", "paused", closest_since, "pause"
        return "closest", seat, closest_since, "none"

    if eligible_closest:
        if seats_taken < params.CLOSEST_CAPACITY and now is not None:
            claimed_since = closest_since if closest_since is not None else now
            return "closest", "active", claimed_since, "claim"
        if seats_taken < params.CLOSEST_CAPACITY and now is None:
            # Cannot record closest_since without a valid timestamp; stay at
            # the trusted level instead of fabricating a time.
            stage = _stage_middle(
                familiarity=familiarity,
                trust=trust,
                episodes=episodes,
                current_stage=prior_stage if prior_stage in STAGES else "initial",
            )
            return stage, "none", None, "none"
        # Capacity is full: stay a trusted friend, do not expose a queue and do
        # not evict seated members (R-27/CASE-042).
        return "trusted", "none", None, "reject_capacity"

    stage = _stage_middle(
        familiarity=familiarity,
        trust=trust,
        episodes=episodes,
        current_stage=stage,
    )
    return stage, "none", None, "none"


# ---------------------------------------------------------------------------
# public API: evaluate
# ---------------------------------------------------------------------------


def evaluate(
    state: RelationshipState,
    counters: DailyCounters,
    evidence_list: Iterable[Evidence],
    *,
    seen_event_ids: set[str],
    seen_fingerprints: dict[str, float],
    seats_taken: int,
    now: float,
) -> Evaluation:
    """Evaluate a batch of evidence in one pure transaction.

    Inputs
    ------
    state, counters
        The persisted state for this member.  They are not modified.
    evidence_list
        Evidence proposed for this transaction, in arrival/commit order.  Order
        matters only for same-day caps and the 20-minute F interval.
    seen_event_ids
        Event ids already committed for the current generation (persisted by
        the caller).  Replays are rejected with ``limit_reason="duplicate"``.
    seen_fingerprints
        ``fingerprint -> last at`` for content that was already counted; a
        fingerprint seen less than ``DUPLICATE_CONTENT_WINDOW_SECONDS`` ago is
        rejected as duplicate content (cross-scene).
    seats_taken
        Effective closest seats currently held by *other* members of the same
        character (paused seats still count).  Unknown/invalid values fail
        closed (treated as full).
    now
        Explicit epoch seconds used only for a new ``closest_since`` stamp.

    Returns
    -------
    Evaluation
        ``decisions`` has one entry per submitted item (malformed items get an
        event id of ``""``).  ``rejected_events`` lists the decisions with
        ``accepted=False``.  The caller must persist every event id (both
        accepted and rejected) and every ``audit["fingerprint"]`` whose
        ``audit["fingerprint_counted"]`` is true.

    Decision classification
    -----------------------
    * ``accepted=True`` means the item was valid evidence and contributed at
      least one effect: an F/T delta, a new meaningful episode, a new distinct
      negative-evidence day, or a valid mood/topic signal that deliberately
      changes no long-term score.
    * ``accepted=False`` means nothing was committed; the ``reason`` says why
      (``idempotent_replay``, ``farming``, ``uncertain_evidence``,
      ``subject_mismatch``, ``unverified``, ``duplicate_content``,
      ``daily_cap``, ``interval``, ``floor``, ``ceiling``, ...).
    * ``limit_reason`` is empty when no parameter limit shaped the result, and
      otherwise one of the contract values plus ``farming`` (rule 2).

    Notes
    -----
    * No inactivity decay and no time-driven score change exist anywhere.
    * ``state.active_days`` is maintained by the caller and passed through
      unchanged; it only gates closest promotion.
    * A member's own seat is excluded from ``seats_taken``.
    * ``evidence.day`` must be the Asia/Shanghai day derived from ``evidence.at``
      (use :func:`day_for`); a mismatch, a malformed timestamp or an event for
      an already-closed accounting day is rejected as ``uncertain`` (fail
      closed, never guessed).
    * Repeated events are rejected by event id; content repeats are rejected by
      fingerprint.  Both containers are read-only inputs here -- the caller
      persists the resulting ids/fingerprints after the transaction commits.
    * Wrong top-level argument types (not a ``RelationshipState`` /
      ``DailyCounters``) raise ``TypeError`` instead of guessing; malformed
      *items inside* ``evidence_list`` are reported as rejected evidence.
    """
    if not isinstance(state, RelationshipState):
        raise TypeError("state must be a RelationshipState")
    if not isinstance(counters, DailyCounters):
        raise TypeError("counters must be a DailyCounters")

    seats = _coerce_seats(seats_taken)
    now_value = _coerce_float(now, None)

    state_last_f = _optional_float(state.last_f_positive_at)
    counter_last_f = _optional_float(counters.last_f_positive_at)
    if state_last_f is None:
        last_f_positive_at = counter_last_f
    elif counter_last_f is None:
        last_f_positive_at = state_last_f
    else:
        last_f_positive_at = max(state_last_f, counter_last_f)

    acc = _Accumulator(
        day=counters.day if _valid_day(counters.day) else "",
        f_positive=_clamp(_coerce_float(counters.f_positive, 0.0) or 0.0, 0.0, params.F_MAX),
        t_positive=_clamp(_coerce_float(counters.t_positive, 0.0) or 0.0, 0.0, params.T_MAX),
        t_negative_magnitude=_clamp(
            _coerce_float(counters.t_negative_magnitude, 0.0) or 0.0,
            0.0,
            params.T_DAILY_NEGATIVE_MAGNITUDE_CAP,
        ),
        last_f_positive_at=last_f_positive_at,
        familiarity=_clamp(_coerce_float(state.familiarity, params.F_INITIAL) or 0.0, params.F_MIN, params.F_MAX),
        trust=_clamp(_coerce_float(state.trust, params.T_INITIAL) or 0.0, params.T_MIN, params.T_MAX),
        episodes=_coerce_int(state.meaningful_episodes, 0),
        recent_negative_days=_normalise_negative_days(state.recent_negative_days),
        fingerprints=_normalise_fingerprints(seen_fingerprints),
        seen=_normalise_seen_ids(seen_event_ids),
    )
    active_days = _coerce_int(state.active_days, 0)

    outcomes: list[_Outcome] = []
    for event in _iter_evidence(evidence_list):
        outcomes.append(_process_event(event, acc))

    had_severe = any(outcome.severe for outcome in outcomes)
    stage, seat_status, closest_since, seat_action = _resolve_status(
        prior_stage=state.stage,
        prior_seat=state.seat_status,
        prior_closest_since=state.closest_since,
        familiarity=acc.familiarity,
        trust=acc.trust,
        episodes=acc.episodes,
        active_days=active_days,
        recent_negative_days=acc.recent_negative_days,
        had_severe_evidence=had_severe,
        seats_taken=seats,
        now=now_value,
    )

    new_state = RelationshipState(
        familiarity=_clamp(acc.familiarity, params.F_MIN, params.F_MAX),
        trust=_clamp(acc.trust, params.T_MIN, params.T_MAX),
        stage=stage,
        generation=_coerce_int(state.generation, 0),
        meaningful_episodes=_coerce_int(acc.episodes, 0),
        active_days=active_days,
        closest_since=closest_since,
        seat_status=seat_status,
        last_f_positive_at=acc.last_f_positive_at,
        recent_negative_days=tuple(acc.recent_negative_days),
    )

    new_counters = DailyCounters(
        day=acc.day if acc.day else (counters.day if isinstance(counters.day, str) else ""),
        f_positive=_clamp(acc.f_positive, 0.0, params.F_DAILY_POSITIVE_CAP),
        t_positive=_clamp(acc.t_positive, 0.0, params.T_DAILY_POSITIVE_CAP),
        t_negative_magnitude=_clamp(
            acc.t_negative_magnitude,
            0.0,
            params.T_DAILY_NEGATIVE_MAGNITUDE_CAP,
        ),
        last_f_positive_at=acc.last_f_positive_at,
    )

    decisions: list[Decision] = []
    for index, outcome in enumerate(outcomes):
        audit = dict(outcome.audit)
        audit.update(
            {
                "stage_after": stage,
                "seat_status_after": seat_status,
                "generation": new_state.generation,
                "counters_day": new_counters.day,
                "event_index": index,
            }
        )
        is_last = index == len(outcomes) - 1
        decisions.append(
            Decision(
                accepted=outcome.accepted,
                event_id=outcome.event_id,
                delta_f=outcome.delta_f,
                delta_t=outcome.delta_t,
                reason=outcome.reason,
                limit_reason=outcome.limit_reason,
                stage_after=stage,
                seat_action=seat_action if is_last else "none",
                meaningful_episode=outcome.meaningful_episode,
                audit=audit,
            )
        )

    rejected = tuple(
        {
            "event_id": decision.event_id,
            "reason": decision.reason,
            "limit_reason": decision.limit_reason,
            "at": decision.audit.get("at"),
            "day": decision.audit.get("day"),
            "kind": decision.audit.get("kind"),
        }
        for decision in decisions
        if not decision.accepted
    )

    return Evaluation(
        state=new_state,
        counters=new_counters,
        decisions=tuple(decisions),
        seat_action=seat_action,
        rejected_events=rejected,
    )


# ---------------------------------------------------------------------------
# public API: reset / correction
# ---------------------------------------------------------------------------


def reset_state(state: RelationshipState, day: str) -> RelationshipState:
    """Member-initiated relationship reset (R-30 / CASE-046).

    Returns a new state with ``generation + 1``, F/T back to the configured
    initial values, stage ``initial``, the closest seat released and all
    accumulated history (episodes, active days, negative-day ring, F interval)
    cleared so old evidence cannot reappear as progress.

    ``day`` is the Asia/Shanghai day of the explicit reset; it is validated so
    callers cannot silently pass a local-timezone timestamp, and the caller
    keeps it in its audit log (this pure state has no audit field).  The caller
    must also invalidate old-generation event ids; the engine only clears the
    scores.
    """
    if not isinstance(state, RelationshipState):
        raise TypeError("state must be a RelationshipState")
    if not _valid_day(day):
        raise ValueError("day must be an Asia/Shanghai 'YYYY-MM-DD' string")
    generation = _coerce_int(state.generation, 0) + 1
    return RelationshipState(
        familiarity=params.F_INITIAL,
        trust=params.T_INITIAL,
        stage="initial",
        generation=generation,
        meaningful_episodes=0,
        active_days=0,
        closest_since=None,
        seat_status="none",
        last_f_positive_at=None,
        recent_negative_days=(),
    )


def apply_correction(
    state: RelationshipState,
    counters: DailyCounters,
    remaining_verified_events: Iterable[Evidence],
) -> RelationshipState:
    """Rebuild long-term state by replaying only the remaining verified events.

    Used when a piece of evidence is revoked/corrected (CASE-034, R-19/R-24).
    F and T are recomputed from the retained verified evidence in chronological
    order (``at``, then ``event_id``), re-applying the daily caps, the 20-minute
    F interval and the duplicate-content window exactly as :func:`evaluate`
    would have on first sight.  No "extra bonus" is ever added to hide the old
    error: the result is exactly the replay of what remains.

    ``generation`` is preserved (a correction is not a new relationship
    generation).  The caller keeps the audit chain by diffing old vs new values.
    ``counters`` is accepted for interface symmetry; the caller owns the daily
    counters and should persist the returned state.  Seats: an occupied closest
    seat is kept when the rebuilt data still qualifies; it is released when the
    leave condition fires or when the rebuilt F/T/episodes no longer qualify.
    A paused seat resumes ``active`` only when the severe evidence behind the
    pause is no longer among the remaining verified events.
    """
    if not isinstance(state, RelationshipState):
        raise TypeError("state must be a RelationshipState")

    events = _coerce_events(remaining_verified_events)
    events = [
        event
        for event in events
        if event.verified is True
        and _coerce_float(event.at, None) is not None
        and _valid_day(event.day)
    ]
    events.sort(key=lambda event: (_coerce_float(event.at, 0.0) or 0.0,
                                   event.kind == "effective_interaction", str(event.event_id)))

    acc = _Accumulator()
    outcomes = [_process_event(event, acc) for event in events]
    had_severe = any(outcome.severe for outcome in outcomes)

    prior_stage = state.stage if state.stage in STAGES else "initial"
    prior_seat = state.seat_status if state.seat_status in SEAT_STATUSES else "none"
    prior_since = _optional_float(state.closest_since)
    active_days = _coerce_int(state.active_days, 0)

    release = _leave_condition(trust=acc.trust, recent_negative_days=acc.recent_negative_days)
    eligible_closest = _closest_eligible(
        familiarity=acc.familiarity,
        trust=acc.trust,
        episodes=acc.episodes,
        active_days=active_days,
    )

    stage = prior_stage
    seat_status = prior_seat
    closest_since = prior_since
    if prior_seat in ("active", "paused"):
        if release:
            stage = _stage_middle(
                familiarity=acc.familiarity,
                trust=acc.trust,
                episodes=acc.episodes,
                current_stage="trusted",
            )
            seat_status = "none"
            closest_since = None
        elif eligible_closest:
            stage = "closest"
            if prior_seat == "paused" and not had_severe:
                seat_status = "active"
        else:
            stage = _stage_middle(
                familiarity=acc.familiarity,
                trust=acc.trust,
                episodes=acc.episodes,
                current_stage="trusted",
            )
            seat_status = "none"
            closest_since = None
    else:
        stage = _stage_middle(
            familiarity=acc.familiarity,
            trust=acc.trust,
            episodes=acc.episodes,
            current_stage=prior_stage,
        )
        seat_status = "none"
        closest_since = None

    return RelationshipState(
        familiarity=_clamp(acc.familiarity, params.F_MIN, params.F_MAX),
        trust=_clamp(acc.trust, params.T_MIN, params.T_MAX),
        stage=stage,
        generation=_coerce_int(state.generation, 0),
        meaningful_episodes=_coerce_int(acc.episodes, 0),
        active_days=active_days,
        closest_since=closest_since,
        seat_status=seat_status,
        last_f_positive_at=acc.last_f_positive_at,
        recent_negative_days=tuple(acc.recent_negative_days),
    )


# ---------------------------------------------------------------------------
# public API: mood windows
# ---------------------------------------------------------------------------


def new_mood_window(kind: str, at: float, *, topic_key: str = "") -> MoodWindow | None:
    """Create a mood window, or ``None`` for unknown/invalid input.

    ``ordinary`` moods are valid for ``MOOD_ORDINARY_VALIDITY_SECONDS`` and have
    an empty ``topic_key``.  ``topic_disengagement`` windows last
    ``TOPIC_DISENGAGEMENT_SECONDS`` and are scoped to a concrete ``topic_key``
    so that changing the topic is not silently blocked by the same member.
    """
    stamp = _coerce_float(at, None)
    if stamp is None:
        return None
    if kind == MOOD_KIND_ORDINARY:
        return MoodWindow(
            kind=kind,
            topic_key="",
            started_at=stamp,
            expires_at=stamp + params.MOOD_ORDINARY_VALIDITY_SECONDS,
        )
    if kind == MOOD_KIND_TOPIC:
        key = topic_key if isinstance(topic_key, str) else ""
        return MoodWindow(
            kind=kind,
            topic_key=key,
            started_at=stamp,
            expires_at=stamp + params.TOPIC_DISENGAGEMENT_SECONDS,
        )
    return None


def mood_active(window: MoodWindow | None, now: float) -> bool:
    """Return ``True`` while the window is still in force (half-open interval).

    Invalid/unknown windows or timestamps conservatively return ``False``.
    Expiry only ends the temporary window; it never sends a message and never
    changes long-term F/T (R-20/R-37).
    """
    if not isinstance(window, MoodWindow):
        return False
    if window.kind not in (MOOD_KIND_ORDINARY, MOOD_KIND_TOPIC):
        return False
    started = _coerce_float(window.started_at, None)
    expires = _coerce_float(window.expires_at, None)
    current = _coerce_float(now, None)
    if started is None or expires is None or current is None:
        return False
    if expires <= started:
        return False
    return started <= current < expires


def extend_topic_window(window: MoodWindow, at: float) -> MoodWindow:
    """Re-trigger a topic-disengagement window without unbounded extension.

    * duplicate/repeated messages *inside* the active window never move the
      expiry (R-37: repeated messages must not extend silence);
    * a re-trigger after expiry moves the expiry to ``at +
      TOPIC_DISENGAGEMENT_SECONDS``;
    * the absolute cap ``started_at + MAX_TOPIC_DISENGAGEMENT_SECONDS`` is never
      exceeded, so the window cannot be extended forever.

    Unknown window kinds, invalid times and backwards time are returned
    unchanged.
    """
    if not isinstance(window, MoodWindow) or window.kind != MOOD_KIND_TOPIC:
        return window
    started = _coerce_float(window.started_at, None)
    expires = _coerce_float(window.expires_at, None)
    stamp = _coerce_float(at, None)
    if started is None or expires is None or stamp is None:
        return window
    if stamp < started:
        return window
    if stamp < expires:
        # Duplicate/repeated message inside the window: no extension.
        return window
    absolute_cap = started + params.MAX_TOPIC_DISENGAGEMENT_SECONDS
    extended = min(absolute_cap, stamp + params.TOPIC_DISENGAGEMENT_SECONDS)
    if extended <= expires:
        return window
    return MoodWindow(
        kind=window.kind,
        topic_key=window.topic_key,
        started_at=started,
        expires_at=extended,
    )


# ---------------------------------------------------------------------------
# public API: participation hint
# ---------------------------------------------------------------------------


def relationship_affinity(state: RelationshipState) -> int:
    """Return the 0..2 participation hint used by the integration layer.

    ``0`` initial; ``1`` familiar/trusted; ``2`` closest (or
    distant_with_history while a real familiar history exists).  This hint only
    raises the chance to continue an existing turn; it must never open a turn by
    itself, and the integration layer keeps all hard conditions (R-38).
    """
    if not isinstance(state, RelationshipState):
        return 0
    stage = state.stage
    if stage == "initial":
        return 0
    if stage in ("familiar", "trusted"):
        return 1
    if stage == "closest":
        history = _has_familiar_history(state)
        return 2 if history else 1
    if stage == "distant_with_history":
        return 2 if _has_familiar_history(state) else 0
    return 0


def _has_familiar_history(state: RelationshipState) -> bool:
    familiarity = _coerce_float(state.familiarity, 0.0) or 0.0
    episodes = _coerce_int(state.meaningful_episodes, 0)
    return familiarity >= params.FAMILIAR_F or episodes > 0
