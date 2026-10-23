"""Persistent relationship ledger and cross-scene source index.

Lives in the plugin's existing ``memory.db`` but only creates ``rel_*`` tables
(additive, idempotent migration).  The module never calls a model and never
touches the network.  Pure decision rules live in :mod:`relationship.engine`;
this module owns transactions, idempotency, daily counters, seats, reset
generations, tombstones, and the audit ledger.

Privacy notes (R-10/R-24):
- ``rel_memory`` keeps the member's own source text in the private local
  database, exactly like the existing ``memories`` table.  Only items that pass
  ``visibility.can_share`` are ever rendered into a model prompt.
- Ledgers store structured metadata plus short evidence snippets; no log line
  or Git artifact may contain real message text.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from . import params
from .identity import MemberIdentity, Scene, identity_for
from . import visibility
from . import engine, accumulation


SCHEMA_VERSION = 3
MAX_PENDING = 256
MAX_TEXT_CHARS = 2000


class StoreError(RuntimeError):
    """Raised only for programmer errors; runtime paths must degrade instead."""


def _now(clock: Callable[[], float] | None = None) -> float:
    return float(clock()) if clock is not None else time.time()


def day_for(at: float) -> str:
    return datetime.fromtimestamp(float(at), params.TZ).strftime("%Y-%m-%d")


def event_key(character_id: str, member_key: str, source_session: str, message_id: str,
              kind: str, fingerprint: str = "") -> str:
    payload = "\x1f".join([character_id, member_key, source_session or "", message_id or "",
                           kind, fingerprint or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def item_key(character_id: str, member_key: str, source_session: str, message_id: str,
             kind: str = "statement") -> str:
    return event_key(character_id, member_key, source_session, message_id, "memory:" + kind)


def fingerprint(text: str) -> str:
    normalized = "".join(ch for ch in (text or "").lower() if ch.isalnum())[:1200]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _bigrams(text: str) -> set[str]:
    normalized = "".join(ch for ch in (text or "").lower() if ch.isalnum())[:1200]
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index:index + 2] for index in range(len(normalized) - 1)}


def relevance_score(query: str, text: str) -> float:
    """Cheap local lexical relevance; ordering hint only, never a fact claim."""
    left, right = _bigrams(query), _bigrams(text)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


class RelationshipStore:
    """SQLite-backed relationship state. One instance per plugin process."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] | None = None):
        self.path = Path(path)
        self._clock = clock
        self._migrated = False

    # ------------------------------------------------------------------ setup
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def migrate(self, *, character_id: str = "") -> dict:
        """Create/upgrade additive schema. Idempotent; never deletes old rows.

        ``character_id`` enables one-time registration of provable legacy
        ``memories`` rows (R-11); without it (pure storage unit tests) the old
        table is untouched.
        """
        if self._migrated:
            return {"schema_version": SCHEMA_VERSION, "migrated": False}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            # Reject newer schemas before writing. Explicit BEGIN makes the
            # schema and version stamp atomic even with executescript.
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rel_meta'").fetchone()
            if exists:
                row = conn.execute("SELECT value FROM rel_meta WHERE key='schema_version'").fetchone()
                if row and int(row[0]) > SCHEMA_VERSION:
                    raise StoreError("relationship schema is newer than this plugin")
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL)
            try:
                row = conn.execute("SELECT value FROM rel_meta WHERE key='schema_version'").fetchone()
                previous = int(row[0]) if row else 0
                if previous > SCHEMA_VERSION:
                    raise StoreError("relationship schema is newer than this plugin")
                conn.execute(
                    "INSERT OR REPLACE INTO rel_meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),))
                conn.execute(
                    "INSERT OR REPLACE INTO rel_meta(key,value) VALUES('params_version',?)",
                    (params.VERSION,))
                conn.execute(
                    "INSERT OR REPLACE INTO rel_meta(key,value) VALUES('rule_version',?)",
                    (params.RULE_VERSION,))
                # v2 already registered its provable legacy sources. A credit-format
                # upgrade must not rescan old dialogue or change source/membership rows.
                registered = self._register_legacy_rows(conn, character_id) if previous < 2 else 0
                if character_id:
                    conn.execute(
                        "INSERT OR REPLACE INTO rel_meta(key,value) VALUES('character_id',?)",
                        (character_id,))
                    conn.execute(
                        "INSERT OR REPLACE INTO rel_meta(key,value) VALUES('legacy_registered_rows',?)",
                        (str(registered),))
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        self._migrated = True
        result = {"schema_version": SCHEMA_VERSION, "migrated": True}
        if character_id:
            result["legacy_registered"] = registered
        return result

    def schema_info(self) -> dict:
        with closing(self.connect()) as conn:
            rows = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM rel_meta")}
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'rel_%' ORDER BY name")]
        return {"meta": rows, "tables": tables}

    def rollback_plan(self) -> str:
        """Documented, non-destructive rollback: disable the feature, keep tables."""
        return ("关闭 provider_ltm_settings.relationship.enable 后旧路径不受影响；"
                "rel_* 表为新增表，可保留供以后恢复；不做 DROP/覆盖旧表。")

    # ------------------------------------------------------- source registry
    def _record_source_conn(self, conn: sqlite3.Connection, identity: MemberIdentity,
                            scene: Scene, *, message_id: str, text: str, at: float,
                            explicit_secret: bool = False, kind: str = "statement") -> dict:
        """Insert one source item inside an existing transaction (idempotent)."""
        clean = (text or "").strip()[:MAX_TEXT_CHARS]
        if not clean or not message_id:
            return {}
        classification = visibility.classify_source(
            text, scene_kind=scene.kind, explicit_secret=explicit_secret)
        key = item_key(identity.character_id, identity.key(), scene.session, message_id, kind)
        share = ""
        if classification.reuse == visibility.REUSE_OWNER_PUBLIC_SAFE:
            share = visibility.minimal_text(clean)
        conn.execute(
            """INSERT OR IGNORE INTO rel_memory(
                item_key, owner_key, character_id, member_id, source_visibility, reuse,
                sensitive, explicit_secret, source_session, source_group_id,
                source_message_id, kind, text, share_text, fingerprint, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, identity.key(), identity.character_id, identity.member_id,
             classification.source_visibility, classification.reuse,
             int(classification.sensitive), int(classification.explicit_secret),
             scene.session, scene.group_id, message_id, kind, clean, share,
             fingerprint(clean), float(at)))
        return {
            "item_id": key,
            "source_visibility": classification.source_visibility,
            "reuse": classification.reuse,
            "sensitive": classification.sensitive,
            "explicit_secret": classification.explicit_secret,
            "share_text": share,
            "reasons": list(classification.reasons),
        }

    def record_source(self, identity: MemberIdentity, scene: Scene, *, message_id: str,
                      text: str, at: float, explicit_secret: bool = False,
                      kind: str = "statement") -> dict:
        """Register a member-authored memory with provenance and visibility."""
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._record_source_conn(
                    conn, identity, scene, message_id=message_id, text=text, at=at,
                    explicit_secret=explicit_secret, kind=kind)
                conn.execute("COMMIT")
                return result
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _register_legacy_rows(self, conn: sqlite3.Connection, character_id: str) -> int:
        """Register provable legacy ``memories`` rows once (R-11, idempotent).

        Only rows that already carry both a speaker account and a source message
        id are eligible; session scope is parsed from the old unified origin.
        Rows without provenance, unparsable sessions or empty text stay untouched
        and are never guessed into a relationship subject.
        """
        if not character_id:
            return 0
        try:
            rows = conn.execute(
                "SELECT umo, text, created_at, speaker, source_id FROM memories "
                "WHERE speaker<>'' AND source_id<>'' AND text<>''").fetchall()
        except sqlite3.Error:
            return 0
        registered = 0
        for row in rows:
            session = str(row["umo"] or "")
            parts = session.split(":", 2)
            if len(parts) != 3:
                continue
            platform, message_type, group = parts[0].strip(), parts[1], parts[2]
            lowered = message_type.lower()
            if "group" in lowered:
                scene_kind = "group_public"
                group_id = group.split(":")[-1] if group else ""
                if not group_id:
                    continue
            elif "friend" in lowered or "private" in lowered:
                scene_kind = "private"
                group_id = ""
            else:
                continue
            identity = identity_for(character_id, platform, str(row["speaker"]))
            if identity is None:
                continue
            scene = Scene(scene_kind, session, group_id, platform)
            before = conn.total_changes
            self._record_source_conn(
                conn, identity, scene, message_id=str(row["source_id"]),
                text=str(row["text"]), at=float(row["created_at"]), kind="legacy")
            if conn.total_changes > before:
                registered += 1
        return registered

    def visible_items(self, identity: MemberIdentity, target: Scene, *, query: str,
                      now: float, limit: int = 5,
                      max_age_seconds: float = 90 * 86400) -> list[dict]:
        """Filter-before-prompt retrieval of the member's own cross-scene memories.

        Exclusions happen in SQL and in :func:`visibility.can_share`:
        deleted/corrected items, paused source sessions, private-only content and
        unknown provenance never reach the caller.  Ordering is deterministic:
        topic relevance first, then recency.  A correction's replacement may be
        retrieved for a query about the *old* wording (CASE-019) because the
        superseded text is kept as an internal relevance bridge.
        """
        limit = max(0, min(int(limit), 10))
        if limit == 0:
            return []
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM rel_memory
                   WHERE owner_key=? AND character_id=? AND deleted=0 AND corrected=0
                     AND created_at>=? AND created_at<=?
                   ORDER BY created_at DESC, item_key ASC LIMIT 200""",
                (identity.key(), identity.character_id, now - max_age_seconds, now)).fetchall()
            paused = self._paused_sessions(conn, identity)
            predecessors: dict[str, list[str]] = {}
            for old in conn.execute(
                    "SELECT superseded_by, text FROM rel_memory WHERE owner_key=? "
                    "AND corrected=1 AND superseded_by<>''", (identity.key(),)):
                predecessors.setdefault(old["superseded_by"], []).append(old["text"])
        items = []
        for row in rows:
            if row["source_session"] in paused:
                continue
            policy = visibility.classify_source(
                row["text"], scene_kind=row["source_visibility"],
                explicit_secret=bool(row["explicit_secret"]))
            item = visibility.MemoryItem(
                item_id=row["item_key"], owner_key=row["owner_key"],
                source_visibility=row["source_visibility"], source_session=row["source_session"],
                source_group_id=row["source_group_id"], reuse=policy.reuse,
                explicit_secret=bool(row["explicit_secret"]), created_at=row["created_at"],
                fingerprint=row["fingerprint"], deleted=bool(row["deleted"]),
                corrected=bool(row["corrected"]),
                public_fragment_verified=bool(visibility.safe_public_fragment(row["text"])))
            if not visibility.can_share(item, target):
                continue
            if row["source_visibility"] == visibility.VIS_PRIVATE:
                if policy.reuse == visibility.REUSE_OWNER_PUBLIC_SAFE:
                    # Private-origin public-safe: only the minimal fragment may
                    # travel into another scene; no channel/time/relationship hints.
                    shareable = visibility.safe_public_fragment(row["text"])
                else:
                    # Own private session only (can_share already enforced the
                    # exact-session rule); full text stays inside that session.
                    shareable = (row["text"] or "").strip()
            else:
                # Only verified owner-safe fragments travel to other scenes;
                # ordinary group matters keep full wording in the source group.
                shareable = (visibility.safe_public_fragment(row["text"])
                             if policy.reuse == visibility.REUSE_OWNER_PUBLIC_SAFE
                             else (row["text"] or "").strip())
            if not shareable:
                continue
            relevance = relevance_score(query, shareable) if query else 0.0
            if query and relevance == 0.0:
                # Correction bridge: a query about the old wording should find
                # the corrected version, not the superseded text (CASE-019).
                for old_text in predecessors.get(row["source_message_id"], []):
                    relevance = max(relevance, relevance_score(query, old_text))
            items.append({
                "item_id": row["item_key"],
                "text": shareable,
                "source_visibility": row["source_visibility"],
                "reuse": policy.reuse,
                "source_group_id": row["source_group_id"],
                "created_day": day_for(row["created_at"]),
                "at": row["created_at"],
                "relevance": relevance,
            })
        if query:
            # "Minimal content for the current question" (R-06): only lexically
            # related items may be injected; no unrelated fallback.
            relevant = [item for item in items if item["relevance"] > 0.0]
            items = relevant
        items.sort(key=lambda item: (-item["relevance"], -item["at"]))
        return items[:limit]

    def _paused_sessions(self, conn: sqlite3.Connection, identity: MemberIdentity) -> set[str]:
        rows = conn.execute(
            "SELECT session FROM rel_source_pause WHERE member_key=? AND paused=1",
            (identity.key(),)).fetchall()
        paused = {row["session"] for row in rows}
        # Reuse the existing per-member/per-session memory control store: a paused
        # source window must never be bypassed from another scene (CASE-017/018).
        try:
            legacy = conn.execute(
                "SELECT umo FROM memory_preferences WHERE speaker=? AND enabled=0",
                (identity.member_id,)).fetchall()
            paused |= {row["umo"] for row in legacy}
        except sqlite3.Error:
            pass
        return paused

    # ---------------------------------------------------- pending familiarity
    def add_pending(self, identity: MemberIdentity, scene: Scene, *, message_id: str,
                    text: str, at: float) -> None:
        if not message_id:
            return
        key = event_key(identity.character_id, identity.key(), scene.session, message_id,
                        "effective_interaction", fingerprint(text))
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = conn.execute("SELECT COUNT(*) FROM rel_pending").fetchone()[0]
                if count >= MAX_PENDING:
                    conn.execute(
                        "DELETE FROM rel_pending WHERE event_id IN "
                        "(SELECT event_id FROM rel_pending ORDER BY created_at ASC LIMIT ?)",
                        (count - MAX_PENDING + 1,))
                conn.execute(
                    """INSERT OR REPLACE INTO rel_pending(
                        event_id, member_key, character_id, source_session, source_group_id,
                        source_message_id, fingerprint, text, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (key, identity.key(), identity.character_id, scene.session, scene.group_id,
                     message_id, fingerprint(text), (text or "").strip()[:MAX_TEXT_CHARS], float(at)))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def expire_pending(self, now: float, ttl: float = params.FAMILIARITY_PENDING_TTL_SECONDS) -> int:
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute("DELETE FROM rel_pending WHERE created_at<?", (now - ttl,))
                conn.execute("COMMIT")
                return cur.rowcount
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def drop_pending(self, identity: MemberIdentity, session: str, message_id: str) -> int:
        """A failed/cancelled send must never be counted as a shared experience."""
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "DELETE FROM rel_pending WHERE member_key=? AND source_session=? "
                    "AND source_message_id=?", (identity.key(), session, message_id))
                conn.execute("COMMIT")
                return cur.rowcount
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------ state evaluation
    def _accumulation_history(self, conn, identity, at):
        """Bounded own-source reads; no private text is added to the audit/model."""
        rows=conn.execute(
            """SELECT e.decided_at,e.delta_f,e.fingerprint,e.audit,
                      (SELECT m.text FROM rel_memory m WHERE m.owner_key=e.member_key
                       AND m.source_session=e.source_session AND m.source_message_id=e.source_message_id
                       AND m.deleted=0 AND m.corrected=0 ORDER BY m.created_at DESC LIMIT 1) AS text
               FROM rel_events e WHERE e.member_key=? AND e.generation=?
                 AND e.kind='effective_interaction' AND e.accepted=1 AND e.invalidated=0
                 AND e.decided_at>=? AND e.decided_at<=?
               ORDER BY e.decided_at DESC,e.rowid DESC LIMIT ?""",
            (identity.key(),self._generation(conn,identity),at-accumulation.HISTORY_SECONDS-accumulation.UNIT_SECONDS,
             at,accumulation.HISTORY_LIMIT)).fetchall()
        records=[]
        for row in rows:
            audit=json.loads(row['audit'])
            records.append(accumulation.Record(
                at=float(row['decided_at']),gained=float(row['delta_f']),text=row['text'] or '',
                fingerprint=row['fingerprint'],credit=accumulation.restore_credit(audit),
                meaningful=bool(audit.get('meaningful_episode')),day=day_for(row['decided_at'])))
        return records

    def evaluate_commit(self, identity: MemberIdentity, scene: Scene, *, message_id: str,
                        at: float, meaningful_hint: bool = False) -> dict:
        """Commit a sent-confirmed reply: familiarity + possible meaningful episode."""
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                pending = conn.execute(
                    "SELECT * FROM rel_pending WHERE member_key=? AND source_session=? "
                    "AND source_message_id=? ORDER BY created_at DESC LIMIT 1",
                    (identity.key(), scene.session, message_id)).fetchone()
                if pending is None:
                    result = {"committed": False, "reason": "no_pending"}
                    conn.execute("COMMIT")
                    return result
                member_paused = conn.execute(
                    "SELECT 1 FROM rel_members WHERE member_key=? AND paused=1",
                    (identity.key(),)).fetchone()
                if member_paused or scene.session in self._paused_sessions(conn, identity):
                    conn.execute("DELETE FROM rel_pending WHERE event_id=?", (pending["event_id"],))
                    conn.execute("COMMIT")
                    return {"committed": False, "reason": "paused"}
                if at - float(pending["created_at"]) > params.FAMILIARITY_PENDING_TTL_SECONDS:
                    # A reply confirmed long after its message can no longer be
                    # tied to a live turn; drop it conservatively (D-004).
                    conn.execute("DELETE FROM rel_pending WHERE event_id=?",
                                 (pending["event_id"],))
                    conn.execute("COMMIT")
                    return {"committed": False, "reason": "pending_expired"}
                evidence = engine.Evidence(
                    event_id=pending["event_id"], kind="effective_interaction", strength="normal",
                    subject="member", source_session=scene.session,
                    source_message_id=message_id or pending["source_message_id"],
                    fingerprint=pending["fingerprint"], at=float(at),
                    verified=True, meaningful=bool(meaningful_hint),
                    day=day_for(at),
                    credit=accumulation.plan(pending['text'],float(at),day_for(at),
                                             self._accumulation_history(conn,identity,at)))
                result = self._apply_evidence(conn, identity, evidence, at,
                                              source_group_id=pending["source_group_id"])
                conn.execute("DELETE FROM rel_pending WHERE event_id=?", (pending["event_id"],))
                conn.execute("COMMIT")
                result["committed"] = bool(result.get("accepted"))
                return result
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def evaluate_trust(self, identity: MemberIdentity, scene: Scene, evidence: engine.Evidence,
                       at: float, *, source_text: str | None = None) -> dict:
        """Evaluate trust; model proposals must match a live captured source."""
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if source_text is not None:
                    source = conn.execute(
                        "SELECT 1 FROM rel_memory WHERE owner_key=? AND source_session=? "
                        "AND source_message_id=? AND text=? AND deleted=0 AND corrected=0",
                        (identity.key(), scene.session, evidence.source_message_id, source_text)).fetchone()
                    paused = conn.execute(
                        "SELECT 1 FROM rel_source_pause WHERE member_key=? AND session=? AND paused=1",
                        (identity.key(), scene.session)).fetchone()
                    member_paused = conn.execute(
                        "SELECT 1 FROM rel_members WHERE member_key=? AND paused=1",
                        (identity.key(),)).fetchone()
                    if not source or paused or member_paused:
                        conn.execute("COMMIT")
                        return {"accepted": False, "limit_reason": "source_unavailable"}
                result = self._apply_evidence(conn, identity, evidence, at, source_group_id=scene.group_id)
                conn.execute("COMMIT")
                return result
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _load_state(self, conn: sqlite3.Connection, identity: MemberIdentity) -> engine.RelationshipState:
        row = conn.execute("SELECT * FROM rel_members WHERE member_key=?",
                           (identity.key(),)).fetchone()
        active_days = conn.execute(
            "SELECT COUNT(*) FROM rel_active_days WHERE member_key=?",
            (identity.key(),)).fetchone()[0]
        negative_rows = conn.execute(
            "SELECT decided_at FROM rel_events WHERE member_key=? AND accepted=1 "
            "AND delta_t<0 AND invalidated=0 AND generation=? ORDER BY decided_at DESC LIMIT 200",
            (identity.key(), self._generation(conn, identity))).fetchall()
        negative_days: list[str] = []
        for item in negative_rows:
            label = day_for(item["decided_at"])
            if label not in negative_days:
                negative_days.append(label)
            if len(negative_days) >= params.NEGATIVE_DAY_RING:
                break
        if row is None:
            return engine.RelationshipState(active_days=int(active_days),
                                            recent_negative_days=tuple(negative_days))
        return engine.RelationshipState(
            familiarity=float(row["familiarity"]), trust=float(row["trust"]),
            stage=row["stage"], generation=int(row["generation"]),
            meaningful_episodes=int(row["meaningful_episodes"]),
            active_days=int(active_days),
            closest_since=row["closest_since"], seat_status=row["seat_status"],
            last_f_positive_at=row["last_f_positive_at"],
            recent_negative_days=tuple(negative_days))

    def _load_counters(self, conn: sqlite3.Connection, identity: MemberIdentity,
                       at: float) -> engine.DailyCounters:
        day = day_for(at)
        row = conn.execute("SELECT * FROM rel_days WHERE member_key=? AND day=?",
                           (identity.key(), day)).fetchone()
        if row is None:
            return engine.DailyCounters(day=day)
        return engine.DailyCounters(
            day=day, f_positive=float(row["f_positive"]),
            t_positive=float(row["t_positive"]),
            t_negative_magnitude=float(row["t_negative"]),
            last_f_positive_at=row["last_f_positive_at"])

    def _seen_events(self, conn: sqlite3.Connection, identity: MemberIdentity) -> set[str]:
        """Physical source events already decided, across generations.

        After an explicit reset (R-30) old event ids must stay non-replayable:
        the generation counter is audit metadata, not a way to resurrect a
        physical message that was already processed.
        """
        return {row["event_id"] for row in conn.execute(
            "SELECT event_id FROM rel_events WHERE member_key=?", (identity.key(),))}

    def _seen_fingerprints(self, conn: sqlite3.Connection, identity: MemberIdentity,
                           at: float, *, exclude_session: str = "",
                           exclude_message: str = "") -> dict[str, float]:
        """Fingerprints already counted for *any* accepted long-term evidence.

        The engine applies the cross-scene duplicate-content window to F and T
        events alike, so loading only ``delta_f>0`` rows would let copied praise
        or a repeated insult count again for trust (CASE-045 non-distinct-day).

        The current physical source event is excluded: one inbound message may
        legitimately produce both a trust candidate (evaluated on receipt) and a
        familiarity commit (after send confirmation), and those are different
        axes (R-15/CASE-025).  A *different* message with the same text still hits
        the duplicate window.
        """
        rows = conn.execute(
            "SELECT fingerprint, MAX(decided_at) AS at FROM rel_events "
            "WHERE member_key=? AND accepted=1 AND invalidated=0 AND fingerprint<>'' "
            "AND decided_at>=? "
            "AND NOT (source_session=? AND source_message_id=? AND ?<>'') "
            "GROUP BY fingerprint",
            (identity.key(), at - params.DUPLICATE_CONTENT_WINDOW_SECONDS,
             exclude_session, exclude_message, exclude_message)).fetchall()
        return {row["fingerprint"]: float(row["at"]) for row in rows}

    def _seats_taken(self, conn: sqlite3.Connection, character_id: str,
                     member_key: str = "") -> int:
        return int(conn.execute(
            "SELECT COUNT(*) FROM rel_seats WHERE character_id=? AND status IN ('active','paused') "
            "AND member_key<>?", (character_id, member_key)).fetchone()[0])

    def _apply_evidence(self, conn: sqlite3.Connection, identity: MemberIdentity,
                        evidence: engine.Evidence, at: float, *, source_group_id: str = "") -> dict:
        state = self._load_state(conn, identity)
        counters = self._load_counters(conn, identity, at)
        if evidence.kind in {"trust_positive", "trust_negative", "severe_negative"} and evidence.source_message_id:
            decided = conn.execute(
                "SELECT 1 FROM rel_events WHERE member_key=? AND source_session=? "
                "AND source_message_id=? AND kind IN ('trust_positive','trust_negative','severe_negative') "
                "AND accepted=1 LIMIT 1",
                (identity.key(), evidence.source_session, evidence.source_message_id)).fetchone()
            if decided:
                return {"accepted": False, "delta_f": 0, "delta_t": 0,
                        "reason": "source_axis_already_decided", "limit_reason": "duplicate"}
        seats = self._seats_taken(conn, identity.character_id, identity.key())
        preference = conn.execute("SELECT intimacy_allowed FROM rel_contact WHERE member_key=?",
                                  (identity.key(),)).fetchone()
        if preference and not preference[0]:
            seats = params.CLOSEST_CAPACITY
        evaluation = engine.evaluate(
            state, counters, [evidence],
            seen_event_ids=self._seen_events(conn, identity),
            seen_fingerprints=self._seen_fingerprints(
                conn, identity, at,
                # Only the familiarity axis may share a physical message with a
                # receive-time trust candidate.  The trust axis must stay
                # idempotent per source event (R-21; deterministic rule + tool
                # proposal for the same message cannot both count).
                exclude_session=evidence.source_session
                if evidence.kind == "effective_interaction" else "",
                exclude_message=evidence.source_message_id
                if evidence.kind == "effective_interaction" else ""),
            seats_taken=seats, now=at)
        self._persist_evaluation(conn, identity, evaluation, at,
                                 source_group_id=source_group_id, evidence_rows=[evidence])
        decision = evaluation.decisions[0] if evaluation.decisions else None
        return {
            "accepted": bool(decision and decision.accepted),
            "delta_f": float(decision.delta_f) if decision else 0.0,
            "delta_t": float(decision.delta_t) if decision else 0.0,
            "reason": decision.reason if decision else "no_decision",
            "limit_reason": decision.limit_reason if decision else "",
            "stage": evaluation.state.stage,
            "stage_changed": evaluation.state.stage != state.stage,
            "seat_action": evaluation.seat_action,
            "meaningful_episode": bool(decision and decision.meaningful_episode),
            "generation": evaluation.state.generation,
        }

    def _persist_evaluation(self, conn: sqlite3.Connection, identity: MemberIdentity,
                            evaluation: engine.Evaluation, at: float, *,
                            source_group_id: str = "",
                            evidence_rows: Iterable[engine.Evidence] = ()) -> None:
        state = evaluation.state
        counters = evaluation.counters
        now = float(at)
        day = counters.day
        conn.execute(
            """INSERT INTO rel_members(
                member_key, character_id, platform, member_id, familiarity, trust, stage,
                generation, meaningful_episodes, closest_since, seat_status,
                last_f_positive_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(member_key) DO UPDATE SET
                familiarity=excluded.familiarity, trust=excluded.trust, stage=excluded.stage,
                generation=excluded.generation,
                meaningful_episodes=excluded.meaningful_episodes,
                closest_since=excluded.closest_since, seat_status=excluded.seat_status,
                last_f_positive_at=excluded.last_f_positive_at, updated_at=excluded.updated_at""",
            (identity.key(), identity.character_id, identity.platform, identity.member_id,
             state.familiarity, state.trust, state.stage, state.generation,
             state.meaningful_episodes, state.closest_since, state.seat_status,
             state.last_f_positive_at, now))
        conn.execute(
            """INSERT INTO rel_days(member_key, day, f_positive, t_positive, t_negative,
                                    last_f_positive_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(member_key, day) DO UPDATE SET
                f_positive=excluded.f_positive, t_positive=excluded.t_positive,
                t_negative=excluded.t_negative,
                last_f_positive_at=excluded.last_f_positive_at""",
            (identity.key(), day, counters.f_positive, counters.t_positive,
             counters.t_negative_magnitude, counters.last_f_positive_at))
        # Active days / meaningful episode registry (distinct evidence only).
        accepted_f = any(d.accepted and d.delta_f > 0 for d in evaluation.decisions)
        if accepted_f:
            conn.execute("INSERT OR IGNORE INTO rel_active_days(member_key, day) VALUES(?,?)",
                         (identity.key(), day))
        by_id = {e.event_id: e for e in evidence_rows}
        visibility_by_event: dict[str, str] = {}
        for evidence in by_id.values():
            if evidence.source_message_id:
                source_row = conn.execute(
                    "SELECT source_visibility FROM rel_memory WHERE owner_key=? "
                    "AND source_session=? AND source_message_id=? LIMIT 1",
                    (identity.key(), evidence.source_session,
                     evidence.source_message_id)).fetchone()
                visibility_by_event[evidence.event_id] = (
                    source_row["source_visibility"] if source_row else "")
        for decision in evaluation.decisions:
            evidence = by_id.get(decision.event_id)
            conn.execute(
                """INSERT OR IGNORE INTO rel_events(
                    event_id, member_key, character_id, generation, kind, strength, subject,
                    source_session, source_message_id, source_visibility, fingerprint,
                    rule_version, delta_f, delta_t, accepted, limit_reason, stage_after,
                    seat_action, audit, decided_at, invalidated)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (decision.event_id, identity.key(), identity.character_id, state.generation,
                 evidence.kind if evidence else "unknown", evidence.strength if evidence else "normal",
                 evidence.subject if evidence else "unknown",
                 (evidence.source_session if evidence else ""),
                 (evidence.source_message_id if evidence else ""),
                 visibility_by_event.get(decision.event_id, ""),
                 (evidence.fingerprint if evidence else ""), params.RULE_VERSION,
                 decision.delta_f, decision.delta_t, int(decision.accepted),
                 decision.limit_reason, decision.stage_after, decision.seat_action,
                 json.dumps(decision.audit, ensure_ascii=False, sort_keys=True), now))
            if decision.meaningful_episode and evidence is not None:
                # Engine already counted the episode in new state; this registry
                # only persists distinct evidence for correction/replay dedupe.
                conn.execute(
                    """INSERT OR IGNORE INTO rel_evidence(
                        member_key, fingerprint, kind, day, source_session, source_message_id,
                        event_id, at, invalidated) VALUES(?,?,?,?,?,?,?,?,0)""",
                    (identity.key(), evidence.fingerprint, evidence.kind, evidence.day,
                     evidence.source_session, evidence.source_message_id, evidence.event_id, now))
        self._apply_seat_action(conn, identity, state, evaluation.seat_action, now)

    def _active_day_count(self, conn: sqlite3.Connection, member_key: str) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM rel_active_days WHERE member_key=?",
                                (member_key,)).fetchone()[0])

    def _apply_seat_action(self, conn: sqlite3.Connection, identity: MemberIdentity,
                           state: engine.RelationshipState, action: str, now: float) -> None:
        member = identity.key()
        current = conn.execute("SELECT status FROM rel_seats WHERE character_id=? AND member_key=?",
                               (identity.character_id, member)).fetchone()
        status = current["status"] if current else None
        if action == "claim":
            if status is None:
                conn.execute(
                    "INSERT INTO rel_seats(character_id, member_key, status, since) "
                    "VALUES(?,?, 'active', ?)", (identity.character_id, member, now))
            elif status != "active":
                conn.execute("UPDATE rel_seats SET status='active', since=? "
                             "WHERE character_id=? AND member_key=?",
                             (now, identity.character_id, member))
            conn.execute("UPDATE rel_members SET seat_status='active', closest_since=COALESCE(closest_since,?) "
                         "WHERE member_key=?", (now, member))
        elif action == "pause":
            if status == "active":
                conn.execute("UPDATE rel_seats SET status='paused' WHERE character_id=? AND member_key=?",
                             (identity.character_id, member))
            conn.execute("UPDATE rel_members SET seat_status='paused' WHERE member_key=?", (member,))
        elif action == "release":
            conn.execute("DELETE FROM rel_seats WHERE character_id=? AND member_key=?",
                         (identity.character_id, member))
            conn.execute("UPDATE rel_members SET seat_status='none', closest_since=NULL WHERE member_key=?",
                         (member,))
        elif action == "reject_capacity":
            conn.execute("UPDATE rel_members SET seat_status='none' WHERE member_key=?", (member,))

    # ------------------------------------------------------------ moods/topics
    def open_topic_window(self, identity: MemberIdentity, *, topic_key: str, at: float,
                          reason: str, trigger_event_id: str = "") -> dict:
        if not topic_key:
            return {}
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM rel_moods WHERE member_key=? AND kind='topic_disengagement' "
                    "AND topic_key=?", (identity.key(), topic_key)).fetchone()
                if existing and float(existing["expires_at"]) > at:
                    # Duplicate message must not extend the window (R-37).
                    conn.execute("COMMIT")
                    return {"opened": False, "expires_at": existing["expires_at"],
                            "reason": "active_window_not_extended"}
                expires = min(float(at) + params.TOPIC_DISENGAGEMENT_SECONDS,
                              float(at) + params.MAX_TOPIC_DISENGAGEMENT_SECONDS)
                conn.execute(
                    """INSERT INTO rel_moods(member_key, character_id, kind, topic_key,
                        trigger_event_id, started_at, expires_at, reason)
                       VALUES(?,?, 'topic_disengagement', ?,?,?,?,?)
                       ON CONFLICT(member_key, kind, topic_key) DO UPDATE SET
                        trigger_event_id=excluded.trigger_event_id,
                        started_at=excluded.started_at,
                        expires_at=excluded.expires_at, reason=excluded.reason""",
                    (identity.key(), identity.character_id, topic_key, trigger_event_id,
                     float(at), expires, reason[:300]))
                conn.execute("COMMIT")
                return {"opened": True, "expires_at": expires, "topic_key": topic_key}
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def active_topic_window(self, identity: MemberIdentity, topic_key: str, at: float) -> dict:
        if not topic_key:
            return {}
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM rel_moods WHERE member_key=? AND kind='topic_disengagement' "
                "AND topic_key=? AND expires_at>?",
                (identity.key(), topic_key, at)).fetchone()
        return dict(row) if row else {}

    def active_topics(self, identity: MemberIdentity, at: float) -> list[dict]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM rel_moods WHERE member_key=? AND kind='topic_disengagement' "
                "AND expires_at>?", (identity.key(), at)).fetchall()
        return [dict(row) for row in rows]

    def open_mood(self, identity: MemberIdentity, *, at: float, duration: float,
                  reason: str, trigger_event_id: str = "") -> dict:
        """Short-lived ordinary mood; expiring never restores trust or sends text."""
        with closing(self.connect()) as conn:
            conn.execute(
                """INSERT INTO rel_moods(member_key, character_id, kind, topic_key,
                    trigger_event_id, started_at, expires_at, reason)
                   VALUES(?,?, 'ordinary', '', ?,?,?,?)
                   ON CONFLICT(member_key, kind, topic_key) DO UPDATE SET
                    trigger_event_id=excluded.trigger_event_id,
                    started_at=excluded.started_at,
                    expires_at=excluded.expires_at, reason=excluded.reason""",
                (identity.key(), identity.character_id, trigger_event_id, float(at),
                 float(at) + float(duration), reason[:300]))
        return {"opened": True, "expires_at": float(at) + float(duration)}

    def active_mood(self, identity: MemberIdentity, at: float) -> dict:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM rel_moods WHERE member_key=? AND kind='ordinary' AND expires_at>?",
                (identity.key(), at)).fetchone()
        return dict(row) if row else {}

    def purge_expired_moods(self, identity: MemberIdentity, at: float) -> int:
        with closing(self.connect()) as conn:
            cur = conn.execute("DELETE FROM rel_moods WHERE member_key=? AND expires_at<=?",
                               (identity.key(), at))
        return cur.rowcount

    # ----------------------------------------------------------- controls
    def set_paused(self, identity: MemberIdentity, paused: bool, at: float) -> None:
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO rel_members(member_key, character_id, platform, member_id,
                        familiarity, trust, stage, generation, meaningful_episodes, seat_status,
                        paused, updated_at)
                       VALUES(?,?,?,?,?,?, 'initial', 0, 0, 'none', ?, ?)
                       ON CONFLICT(member_key) DO UPDATE SET paused=excluded.paused,
                        updated_at=excluded.updated_at""",
                    (identity.key(), identity.character_id, identity.platform, identity.member_id,
                     params.F_INITIAL, params.T_INITIAL, int(paused), float(at)))
                self._audit_control(
                    conn, identity, "member_pause" if paused else "member_resume", at,
                    {"reason": "member_requested_" + ("pause" if paused else "resume")})
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def contact_preferences(self, identity):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT auto_participation,intimacy_allowed FROM rel_contact "
                               "WHERE member_key=?", (identity.key(),)).fetchone()
        return (bool(row[0]), bool(row[1])) if row else (True, True)

    def set_contact_preferences(self, identity, at, *, auto=None, intimacy=None):
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT auto_participation,intimacy_allowed FROM rel_contact "
                                   "WHERE member_key=?", (identity.key(),)).fetchone()
                previous = (bool(row[0]), bool(row[1])) if row else (True, True)
                auto = previous[0] if auto is None else bool(auto)
                intimacy = previous[1] if intimacy is None else bool(intimacy)
                conn.execute("INSERT INTO rel_contact VALUES(?,?,?) ON CONFLICT(member_key) "
                             "DO UPDATE SET auto_participation=excluded.auto_participation,"
                             "intimacy_allowed=excluded.intimacy_allowed",
                             (identity.key(), int(auto), int(intimacy)))
                if not intimacy:
                    conn.execute("DELETE FROM rel_seats WHERE member_key=?", (identity.key(),))
                    conn.execute("UPDATE rel_members SET stage=CASE WHEN stage='closest' THEN "
                                 "'trusted' ELSE stage END,seat_status='none',closest_since=NULL "
                                 "WHERE member_key=?", (identity.key(),))
                self._audit_control(conn, identity, "contact_preference", at,
                                    {"auto": auto, "intimacy": intimacy})
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def is_paused(self, identity: MemberIdentity) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT paused FROM rel_members WHERE member_key=?",
                               (identity.key(),)).fetchone()
        return bool(row["paused"]) if row else False

    def set_session_paused(self, identity: MemberIdentity, session: str, paused: bool,
                           at: float) -> None:
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT INTO rel_source_pause(member_key, session, paused, updated_at)
                       VALUES(?,?,?,?) ON CONFLICT(member_key, session) DO UPDATE SET
                       paused=excluded.paused, updated_at=excluded.updated_at""",
                    (identity.key(), session, int(paused), float(at)))
                self._audit_control(
                    conn, identity, "session_pause" if paused else "session_resume", at,
                    {"session": session,
                     "reason": "member_requested_" + ("pause" if paused else "resume")})
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def session_paused(self, identity: MemberIdentity, session: str) -> bool:
        """Target-window pause wins: a paused session never receives shared recall."""
        if not session:
            return False
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT paused FROM rel_source_pause WHERE member_key=? AND session=?",
                (identity.key(), session)).fetchone()
            if row and row["paused"]:
                return True
            try:
                legacy = conn.execute(
                    "SELECT enabled FROM memory_preferences WHERE umo=? AND speaker=?",
                    (session, identity.member_id)).fetchone()
                if legacy is not None and not legacy["enabled"]:
                    return True
            except sqlite3.Error:
                pass
        return False

    def reset_relationship(self, identity: MemberIdentity, at: float) -> dict:
        """R-30: release own seat, bump generation, clear F/T evidence, keep memory."""
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Single source of truth: the engine owns reset semantics.
                reset = engine.reset_state(self._load_state(conn, identity), day_for(at))
                new_generation = reset.generation
                conn.execute(
                    """INSERT INTO rel_members(member_key, character_id, platform, member_id,
                        familiarity, trust, stage, generation, meaningful_episodes,
                        closest_since, seat_status, last_f_positive_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?, 0, NULL, 'none', NULL, ?)
                       ON CONFLICT(member_key) DO UPDATE SET
                        familiarity=excluded.familiarity, trust=excluded.trust,
                        stage=excluded.stage, generation=excluded.generation,
                        meaningful_episodes=0, closest_since=NULL, seat_status='none',
                        last_f_positive_at=NULL, updated_at=excluded.updated_at""",
                    (identity.key(), identity.character_id, identity.platform, identity.member_id,
                     reset.familiarity, reset.trust, reset.stage, new_generation, float(at)))
                conn.execute("DELETE FROM rel_seats WHERE character_id=? AND member_key=?",
                             (identity.character_id, identity.key()))
                conn.execute("DELETE FROM rel_active_days WHERE member_key=?", (identity.key(),))
                conn.execute("DELETE FROM rel_evidence WHERE member_key=?", (identity.key(),))
                conn.execute("DELETE FROM rel_days WHERE member_key=?", (identity.key(),))
                conn.execute("DELETE FROM rel_pending WHERE member_key=?", (identity.key(),))
                # Reset temporary mood/refusal windows; privacy pauses and contact
                # preferences stay intact. Old negative days are excluded by generation.
                conn.execute("DELETE FROM rel_moods WHERE member_key=?", (identity.key(),))
                conn.execute(
                    """INSERT INTO rel_events(
                        event_id, member_key, character_id, generation, kind, strength, subject,
                        source_session, source_message_id, source_visibility, fingerprint,
                        rule_version, delta_f, delta_t, accepted, limit_reason, stage_after,
                        seat_action, audit, decided_at, invalidated)
                       VALUES(?,?,?,?, 'reset', 'normal', 'member', '', '', '', '',
                        ?, 0, 0, 1, '', 'initial', 'release', ?, ?, 0)""",
                    ("reset:" + identity.key() + ":" + str(new_generation), identity.key(),
                     identity.character_id, new_generation, params.RULE_VERSION,
                     json.dumps({"reason": "member_requested_reset", "generation": new_generation},
                                ensure_ascii=False, sort_keys=True), float(at)))
                conn.execute("COMMIT")
                return {"generation": new_generation, "seat_released": True, "stage": "initial"}
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _audit_control(self, conn: sqlite3.Connection, identity: MemberIdentity,
                       action: str, at: float, detail: dict) -> str:
        """Append a control action to the audit ledger (no score change)."""
        event_id = "control:" + hashlib.sha256(
            (identity.key() + "\x1f" + action + "\x1f" + str(at) + "\x1f"
             + json.dumps(detail, ensure_ascii=False, sort_keys=True)).encode("utf-8")
        ).hexdigest()[:40]
        conn.execute(
            """INSERT OR IGNORE INTO rel_events(
                event_id, member_key, character_id, generation, kind, strength, subject,
                source_session, source_message_id, source_visibility, fingerprint,
                rule_version, delta_f, delta_t, accepted, limit_reason, stage_after,
                seat_action, audit, decided_at, invalidated)
               VALUES(?,?,?,?, 'control', 'normal', 'member', ?, ?, '', '', ?, 0, 0, 1,
                      '', '', 'none', ?, ?, 0)""",
            (event_id, identity.key(), identity.character_id,
             self._generation(conn, identity),
             str(detail.get("session", "")), str(detail.get("message_id", "")),
             params.RULE_VERSION,
             json.dumps({"action": action, "reason": detail.get("reason", ""),
                         "detail": detail}, ensure_ascii=False, sort_keys=True),
             float(at)))
        return event_id

    def forget_source(self, identity: MemberIdentity, *, session: str | None = None,
                      message_id: str | None = None, text: str | None = None,
                      at: float | None = None) -> dict:
        """Tombstone a member's source item(s); invalidate related relationship evidence.

        Invalidation is scoped as tightly as the caller's identifiers allow:
        ``message_id``/``text`` alone can collide across scenes in real
        adapters, so callers also pass ``session`` (``/记忆`` cascade does).
        ``text`` additionally matches the stored content fingerprint, so a
        "forget this fact" cannot leave the same evidence feeding the ledger.
        """
        when = _now(self._clock) if at is None else float(at)
        if session is None and message_id is None and text is None:
            # Caller must identify what to forget; never treat "no selector" as
            # "delete everything" (R-12 conservative deletion).
            return {"error": "no_selector", "deleted_items": 0,
                    "invalidated_events": 0, "invalidated_evidence": 0}
        memory_clauses = ["owner_key=?", "character_id=?"]
        memory_values: list = [identity.key(), identity.character_id]
        if session is not None:
            memory_clauses.append("source_session=?")
            memory_values.append(session)
        if message_id is not None:
            memory_clauses.append("source_message_id=?")
            memory_values.append(message_id)
        if text is not None:
            memory_clauses.append("(text=? OR fingerprint=?)")
            memory_values.extend([text, fingerprint(text)])
        where_memory = " AND ".join(memory_clauses)
        event_conds: list[str] = []
        event_values: list = []
        if session is not None:
            event_conds.append("source_session=?")
            event_values.append(session)
        if message_id is not None:
            event_conds.append("source_message_id=?")
            event_values.append(message_id)
        if text is not None:
            event_conds.append("fingerprint=?")
            event_values.append(fingerprint(text))
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(f"SELECT * FROM rel_memory WHERE {where_memory}",
                                    memory_values).fetchall()
                item_ids = [row["item_key"] for row in rows]
                for item in item_ids:
                    conn.execute("UPDATE rel_memory SET deleted=1 WHERE item_key=?", (item,))
                invalidated = 0
                evidence_invalidated = 0
                if event_conds:
                    where_events = " AND ".join(event_conds)
                    event_ids = [row["event_id"] for row in conn.execute(
                        f"""SELECT event_id FROM rel_events WHERE member_key=? AND accepted=1
                            AND ({where_events})""",
                        [identity.key(), *event_values])]
                    for event_id in event_ids:
                        cur = conn.execute(
                            "UPDATE rel_events SET invalidated=1 WHERE event_id=? AND invalidated=0",
                            (event_id,))
                        invalidated += cur.rowcount
                    evidence_invalidated = conn.execute(
                        f"UPDATE rel_evidence SET invalidated=1 WHERE member_key=? "
                        f"AND ({where_events}) AND invalidated=0",
                        [identity.key(), *event_values]).rowcount
                    # A deleted source must not later commit its pending familiarity
                    # when a reply send is confirmed (R-12 cascade).
                    conn.execute(
                        f"DELETE FROM rel_pending WHERE member_key=? AND ({where_events})",
                        [identity.key(), *event_values])
                self._audit_control(
                    conn, identity, "forget", when,
                    {"session": session or "", "message_id": message_id or "",
                     "fingerprint": fingerprint(text) if text is not None else "",
                     "reason": "member_requested_forget"})
                rebuilt = self._rebuild_state(conn, identity, when)
                conn.execute("COMMIT")
                return {"deleted_items": len(item_ids), "invalidated_events": invalidated,
                        "invalidated_evidence": evidence_invalidated, "rebuilt": rebuilt}
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def correct_source(self, identity: MemberIdentity, *, session: str, message_id: str,
                       replacement: str, replacement_message_id: str,
                       group_id: str = "", at: float) -> dict:
        """Tombstone the old statement, register the replacement and rebuild state.

        All three effects happen in one transaction / one connection: a crash
        cannot leave "old item tombstoned but replacement missing and state not
        rebuilt" (independent review P-03).
        """
        scene = Scene(kind="group_public" if group_id else "private",
                      session=session, group_id=group_id, platform=identity.platform)
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE rel_memory SET corrected=1, superseded_by=? "
                    "WHERE owner_key=? AND source_session=? AND source_message_id=?",
                    (replacement_message_id, identity.key(), session, message_id))
                # Adapter message ids are scene-local: never invalidate another
                # conversation's event that happens to share the same id.
                event_ids = [row["event_id"] for row in conn.execute(
                    "SELECT event_id FROM rel_events WHERE member_key=? AND source_session=? "
                    "AND source_message_id=? AND accepted=1",
                    (identity.key(), session, message_id))]
                for event_id in event_ids:
                    conn.execute("UPDATE rel_events SET invalidated=1 WHERE event_id=?", (event_id,))
                conn.execute(
                    "UPDATE rel_evidence SET invalidated=1 WHERE member_key=? "
                    "AND source_session=? AND source_message_id=?",
                    (identity.key(), session, message_id))
                self._record_source_conn(conn, identity, scene,
                                         message_id=replacement_message_id,
                                         text=replacement, at=at)
                self._audit_control(
                    conn, identity, "correct", at,
                    {"session": session, "message_id": message_id,
                     "replacement_id": replacement_message_id,
                     "reason": "member_requested_correction"})
                rebuilt = self._rebuild_state(conn, identity, at)
                conn.execute("COMMIT")
                return {"corrected": True, "rebuilt": rebuilt}
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _rebuild_state(self, conn: sqlite3.Connection, identity: MemberIdentity,
                       at: float) -> dict:
        """Replay still-valid accepted evidence; never compensate with extra points.

        Also rebuilds the per-day counters and active-day registry, otherwise a
        corrected/deleted event would leave stale cap usage behind and block the
        next legitimate interaction (CASE-016/019/034, R-12/R-19).
        """
        rows = conn.execute(
            """SELECT event_id, kind, strength, subject, source_session, source_message_id,
                      fingerprint, decided_at, audit, delta_f
               FROM rel_events WHERE member_key=? AND generation=? AND accepted=1
                 AND invalidated=0 AND kind<>'reset' ORDER BY decided_at ASC, CASE WHEN kind='effective_interaction' THEN 1 ELSE 0 END, rowid ASC""",
            (identity.key(), self._generation(conn, identity))).fetchall()
        meaningful = {row["event_id"] for row in conn.execute(
            "SELECT event_id FROM rel_evidence WHERE member_key=? AND invalidated=0",
            (identity.key(),))}
        evidence = [
            engine.Evidence(
                event_id=row["event_id"], kind=row["kind"], strength=row["strength"],
                subject=row["subject"], source_session=row["source_session"],
                source_message_id=row["source_message_id"], fingerprint=row["fingerprint"],
                at=float(row["decided_at"]), verified=True,
                meaningful=row["event_id"] in meaningful, day=day_for(row["decided_at"]),
                credit=accumulation.restore_credit(json.loads(row['audit']),awarded=row['delta_f'])
                if row['kind']=='effective_interaction' else None)
            for row in rows if row["kind"] in {"effective_interaction", "trust_positive",
                                               "trust_negative", "severe_negative"}]
        # Replay once through the real per-event path to recover exact daily
        # counters and active days; seats are neutralised during this pass.
        replay_state = engine.RelationshipState(generation=self._generation(conn, identity))
        counters = engine.DailyCounters(day="")
        seen_ids: set[str] = set()
        seen_fingerprints: dict[str, float] = {}
        fingerprint_sources: dict[str, tuple[str, str]] = {}
        day_counters: dict[str, engine.DailyCounters] = {}
        active_days: list[str] = []
        for item in evidence:
            fingerprints = dict(seen_fingerprints)
            source = (item.source_session, item.source_message_id)
            if item.kind == "effective_interaction" and fingerprint_sources.get(item.fingerprint) == source:
                fingerprints.pop(item.fingerprint, None)
            evaluation = engine.evaluate(
                replay_state, counters, [item], seen_event_ids=seen_ids,
                seen_fingerprints=fingerprints,
                seats_taken=params.CLOSEST_CAPACITY, now=item.at)
            replay_state = evaluation.state
            counters = evaluation.counters
            seen_ids.add(item.event_id)
            decision = evaluation.decisions[0] if evaluation.decisions else None
            if decision is not None and decision.audit.get("fingerprint_counted"):
                seen_fingerprints[item.fingerprint] = item.at
                fingerprint_sources[item.fingerprint] = source
            if counters.day:
                day_counters[counters.day] = counters
            if decision is not None and item.kind == "effective_interaction" and decision.delta_f > 0:
                if item.day not in active_days:
                    active_days.append(item.day)
        current = replace(self._load_state(conn, identity), active_days=len(active_days))
        rebuilt = engine.apply_correction(
            current, engine.DailyCounters(day=day_for(at)), evidence)
        preference = conn.execute("SELECT intimacy_allowed FROM rel_contact WHERE member_key=?",
                                  (identity.key(),)).fetchone()
        if preference and not preference[0]:
            rebuilt = replace(rebuilt, stage="trusted" if rebuilt.stage == "closest" else rebuilt.stage,
                              seat_status="none", closest_since=None)
        conn.execute("DELETE FROM rel_days WHERE member_key=?", (identity.key(),))
        for day, counter in day_counters.items():
            conn.execute(
                "INSERT INTO rel_days(member_key, day, f_positive, t_positive, t_negative, "
                "last_f_positive_at) VALUES(?,?,?,?,?,?)",
                (identity.key(), day, counter.f_positive, counter.t_positive,
                 counter.t_negative_magnitude, counter.last_f_positive_at))
        conn.execute("DELETE FROM rel_active_days WHERE member_key=?", (identity.key(),))
        for day in active_days:
            conn.execute("INSERT OR IGNORE INTO rel_active_days(member_key, day) VALUES(?,?)",
                         (identity.key(), day))
        conn.execute(
            "UPDATE rel_members SET familiarity=?, trust=?, stage=?, generation=?, "
            "meaningful_episodes=?, closest_since=?, seat_status=?, last_f_positive_at=?, "
            "updated_at=? WHERE member_key=?",
            (rebuilt.familiarity, rebuilt.trust, rebuilt.stage, rebuilt.generation,
             rebuilt.meaningful_episodes, rebuilt.closest_since, rebuilt.seat_status,
             rebuilt.last_f_positive_at, float(at), identity.key()))
        if rebuilt.stage != "closest" or rebuilt.seat_status not in ("active", "paused"):
            conn.execute("DELETE FROM rel_seats WHERE character_id=? AND member_key=?",
                         (identity.character_id, identity.key()))
            conn.execute("UPDATE rel_members SET seat_status='none', closest_since=NULL "
                         "WHERE member_key=?", (identity.key(),))
        return {"familiarity": rebuilt.familiarity, "trust": rebuilt.trust,
                "stage": rebuilt.stage, "active_days": len(active_days)}

    def _generation(self, conn: sqlite3.Connection, identity: MemberIdentity) -> int:
        row = conn.execute("SELECT generation FROM rel_members WHERE member_key=?",
                           (identity.key(),)).fetchone()
        return int(row["generation"]) if row else 0

    # -------------------------------------------------------------- readings
    def snapshot(self, identity: MemberIdentity, at: float) -> dict:
        with closing(self.connect()) as conn:
            state = self._load_state(conn, identity)
            counters = self._load_counters(conn, identity, at)
            seats = conn.execute(
                "SELECT COUNT(*) FROM rel_seats WHERE character_id=? AND status IN ('active','paused')",
                (identity.character_id,)).fetchone()[0]
            row = conn.execute("SELECT paused FROM rel_members WHERE member_key=?",
                               (identity.key(),)).fetchone()
        return {
            "familiarity": state.familiarity, "trust": state.trust, "stage": state.stage,
            "generation": state.generation, "episodes": state.meaningful_episodes,
            "active_days": state.active_days, "seat_status": state.seat_status,
            "characters_closest_seats": int(seats), "paused": bool(row["paused"]) if row else False,
            "day": counters.day, "f_positive_today": counters.f_positive,
            "t_positive_today": counters.t_positive,
            "t_negative_today": counters.t_negative_magnitude,
        }

    def recent_events(self, identity: MemberIdentity, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 50))
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """SELECT event_id, kind, strength, subject, source_session, source_message_id,
                          source_visibility, delta_f, delta_t, accepted, limit_reason,
                          stage_after, seat_action, decided_at, invalidated, audit
                   FROM rel_events WHERE member_key=? ORDER BY decided_at DESC LIMIT ?""",
                (identity.key(), limit)).fetchall()
        return [dict(row) for row in rows]

    def has_event(self, identity: MemberIdentity, event_id: str) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT 1 FROM rel_events WHERE event_id=?", (event_id,)).fetchone()
        return bool(row)

    # ------------------------------------------------------------ maintenance
    def cleanup(self, at: float) -> dict:
        """Bound transient growth; never touches source memories or the ledger.

        ``rel_events`` is intentionally retained: deleting old ledger rows would
        break long-term idempotency (the same physical event could be counted
        again after the retention window) and the R-24 audit chain.  The ledger
        only grows with distinct member interactions and stores no chat text.
        """
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                pending = conn.execute("DELETE FROM rel_pending WHERE created_at<?",
                                       (at - params.FAMILIARITY_PENDING_TTL_SECONDS,)).rowcount
                moods = conn.execute("DELETE FROM rel_moods WHERE expires_at<?", (at,)).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return {"events": 0, "ledger_retained": True, "pending": pending, "moods": moods}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rel_contact(
    member_key TEXT PRIMARY KEY, auto_participation INTEGER NOT NULL DEFAULT 1,
    intimacy_allowed INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS rel_meta(
    key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS rel_members(
    member_key TEXT PRIMARY KEY,
    character_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    member_id TEXT NOT NULL,
    familiarity REAL NOT NULL DEFAULT 0,
    trust REAL NOT NULL DEFAULT 50,
    stage TEXT NOT NULL DEFAULT 'initial',
    generation INTEGER NOT NULL DEFAULT 0,
    meaningful_episodes INTEGER NOT NULL DEFAULT 0,
    closest_since REAL,
    seat_status TEXT NOT NULL DEFAULT 'none',
    last_f_positive_at REAL,
    paused INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rel_members_character ON rel_members(character_id);

CREATE TABLE IF NOT EXISTS rel_days(
    member_key TEXT NOT NULL,
    day TEXT NOT NULL,
    f_positive REAL NOT NULL DEFAULT 0,
    t_positive REAL NOT NULL DEFAULT 0,
    t_negative REAL NOT NULL DEFAULT 0,
    last_f_positive_at REAL,
    PRIMARY KEY(member_key, day));

CREATE TABLE IF NOT EXISTS rel_active_days(
    member_key TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY(member_key, day));

CREATE TABLE IF NOT EXISTS rel_events(
    event_id TEXT PRIMARY KEY,
    member_key TEXT NOT NULL,
    character_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    strength TEXT NOT NULL DEFAULT 'normal',
    subject TEXT NOT NULL DEFAULT 'member',
    source_session TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_visibility TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    rule_version TEXT NOT NULL,
    delta_f REAL NOT NULL DEFAULT 0,
    delta_t REAL NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    limit_reason TEXT NOT NULL DEFAULT '',
    stage_after TEXT NOT NULL DEFAULT '',
    seat_action TEXT NOT NULL DEFAULT 'none',
    audit TEXT NOT NULL DEFAULT '{}',
    decided_at REAL NOT NULL,
    invalidated INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_rel_events_member ON rel_events(member_key, decided_at);
CREATE INDEX IF NOT EXISTS idx_rel_events_source ON rel_events(member_key, source_session, source_message_id);

CREATE TABLE IF NOT EXISTS rel_seats(
    character_id TEXT NOT NULL,
    member_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    since REAL NOT NULL,
    PRIMARY KEY(character_id, member_key));

CREATE TABLE IF NOT EXISTS rel_evidence(
    member_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    source_session TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL,
    at REAL NOT NULL,
    invalidated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(member_key, fingerprint));

CREATE TABLE IF NOT EXISTS rel_memory(
    item_key TEXT PRIMARY KEY,
    owner_key TEXT NOT NULL,
    character_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    source_visibility TEXT NOT NULL,
    reuse TEXT NOT NULL,
    sensitive INTEGER NOT NULL DEFAULT 0,
    explicit_secret INTEGER NOT NULL DEFAULT 0,
    source_session TEXT NOT NULL,
    source_group_id TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'statement',
    text TEXT NOT NULL,
    share_text TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    corrected INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_rel_memory_owner ON rel_memory(owner_key, created_at);
CREATE INDEX IF NOT EXISTS idx_rel_memory_source ON rel_memory(owner_key, source_session, source_message_id);

CREATE TABLE IF NOT EXISTS rel_pending(
    event_id TEXT PRIMARY KEY,
    member_key TEXT NOT NULL,
    character_id TEXT NOT NULL,
    source_session TEXT NOT NULL DEFAULT '',
    source_group_id TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rel_pending_member ON rel_pending(member_key, created_at);

CREATE TABLE IF NOT EXISTS rel_moods(
    member_key TEXT NOT NULL,
    character_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    topic_key TEXT NOT NULL DEFAULT '',
    trigger_event_id TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(member_key, kind, topic_key));

CREATE TABLE IF NOT EXISTS rel_source_pause(
    member_key TEXT NOT NULL,
    session TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY(member_key, session));
"""
