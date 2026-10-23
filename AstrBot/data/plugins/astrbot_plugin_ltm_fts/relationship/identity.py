"""Member identity and scene primitives for the cross-scene relationship module.

Contract: ``evaluation/relationship/INTERFACES.md`` (draft-1, 2026-10-22).

Privacy invariants implemented here (R-04 / R-05):

* a relationship subject is exactly ``(character_id, platform, member_id)``;
* nicknames, display names and group cards never participate in the key;
* one platform account gets the same identity in every group/private session;
* different platforms and different characters never merge;
* missing/invalid fields return ``None`` -- identity data is never fabricated.

The module is pure standard-library logic and must not import AstrBot.  Time is
not needed here: callers pass epoch seconds to the modules that need it.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SCENE_GROUP = "group_public"
SCENE_PRIVATE = "private"
STAGE_VALUES = ("initial", "familiar", "trusted", "closest", "distant_with_history")

#: Unit separator: unambiguous join for hashing identity parts, and it cannot
#: appear in platform/account identifiers we accept.
_KEY_SEPARATOR = "\x1f"
_IDENTITY_FIELDS = ("character_id", "platform", "member_id")

_MESSAGE_TYPE_GROUP = "groupmessage"
_MESSAGE_TYPE_PRIVATE = {"friendmessage", "privatemessage"}


def _clean_field(value: Any) -> str | None:
    """Return a canonical non-empty identifier or ``None``.

    Only strings and integers are accepted.  Integers are rendered canonically
    (``123`` -> ``"123"``) because adapters occasionally expose numeric account
    ids; this is canonicalisation, never invention.  Surrounding whitespace is
    stripped so the same account cannot produce two keys through formatting.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_optional_id(value: Any) -> str | None:
    """Normalise an optional id.

    ``None``/blank means "absent" and returns ``""``; invalid types return
    ``None`` so callers can fail closed instead of guessing a scope.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return None
    return value.strip()


def _read_value(source: Any, name: str) -> Any:
    """Read ``source.name`` or ``source.name()`` without letting bad fakes crash us."""
    if source is None:
        return None
    try:
        value = getattr(source, name)
    except (AttributeError, TypeError):
        return None
    if callable(value):
        try:
            return value()
        except TypeError:
            return None
    return value


def _platform_id(event: Any) -> str | None:
    """Platform identity domain (``platform_meta.id``) for an event."""
    meta = _read_value(event, "platform_meta")
    if isinstance(meta, str):
        return _clean_field(meta)
    if isinstance(meta, Mapping):
        value = meta.get("id")
    elif meta is not None:
        value = _read_value(meta, "id")
    else:
        value = None
    cleaned = _clean_field(value)
    if cleaned is not None:
        return cleaned
    # Real AstrMessageEvent also exposes get_platform_id(); only used as a
    # fallback when platform_meta is absent, never to invent a domain.
    return _clean_field(_read_value(event, "get_platform_id"))


def _message_type_token(event: Any) -> str | None:
    """Normalised message-type token, or ``None`` when the event has no type."""
    value = _read_value(event, "get_message_type")
    if value is None:
        return None
    value = getattr(value, "value", value)
    if not isinstance(value, str):
        return None
    token = value.rsplit(".", 1)[-1].replace("_", "").replace(" ", "").lower()
    return token or None


@dataclass(frozen=True)
class MemberIdentity:
    """Stable relationship subject.  Built via :func:`identity_for`."""

    character_id: str
    platform: str
    member_id: str

    def key(self) -> str:
        """SHA-256 hex digest of the identity triple (never contains a nickname)."""
        joined = _KEY_SEPARATOR.join(
            (self.character_id, self.platform, self.member_id)
        )
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def parts(self) -> tuple[str, str, str]:
        return (self.character_id, self.platform, self.member_id)

    def as_dict(self) -> dict[str, str]:
        """Audit-safe representation: no nickname/display name ever appears."""
        return {
            "character_id": self.character_id,
            "platform": self.platform,
            "member_key": self.key(),
        }


@dataclass(frozen=True)
class Scene:
    """Where a memory/relationship interaction happened.

    ``kind`` is ``SCENE_GROUP`` or ``SCENE_PRIVATE``; private scenes always have
    an empty ``group_id``.  ``session`` is the unified-message-origin style key.
    """

    kind: str
    session: str
    group_id: str
    platform: str


def identity_for(
    character_id: Any, platform: Any, member_id: Any
) -> MemberIdentity | None:
    """Build an identity from stable ids, or ``None`` if any part is missing."""
    character = _clean_field(character_id)
    platform_name = _clean_field(platform)
    member = _clean_field(member_id)
    if character is None or platform_name is None or member is None:
        return None
    return MemberIdentity(character, platform_name, member)


def _is_valid_identity(value: Any) -> bool:
    if not isinstance(value, MemberIdentity):
        return False
    for name in _IDENTITY_FIELDS:
        field = getattr(value, name)
        if not isinstance(field, str) or not field or field != field.strip():
            return False
    return True


def identity_from_event(event: Any, character_id: Any) -> MemberIdentity | None:
    """Derive an identity from an AstrBot-like message event.

    Required event surface: ``platform_meta.id`` and ``get_sender_id()``.
    Missing platform/sender, empty values, and messages sent by this bot itself
    return ``None`` (the bot must never become a relationship subject).
    """
    platform_name = _platform_id(event)
    if platform_name is None:
        return None
    sender = _clean_field(_read_value(event, "get_sender_id"))
    if sender is None:
        return None
    self_id = _clean_field(_read_value(event, "get_self_id"))
    if self_id is not None and sender == self_id:
        return None
    return identity_for(character_id, platform_name, sender)


def identity_matches(a: MemberIdentity | None, b: MemberIdentity | None) -> bool:
    """True only when both sides are valid and describe the same subject."""
    if not _is_valid_identity(a) or not _is_valid_identity(b):
        return False
    return a.key() == b.key()


def scene_for(platform: Any, session: Any, group_id: Any = "") -> Scene | None:
    """Build a scene; group scenes need a group id, private scenes must not have one."""
    platform_name = _clean_field(platform)
    session_key = _clean_field(session)
    if platform_name is None or session_key is None:
        return None
    group = _clean_optional_id(group_id)
    if group is None:
        return None
    if group:
        return Scene(SCENE_GROUP, session_key, group, platform_name)
    return Scene(SCENE_PRIVATE, session_key, "", platform_name)


def scene_from_event(event: Any) -> Scene | None:
    """Derive a scene from an AstrBot-like message event, failing closed.

    ``unified_msg_origin`` (then ``session_id``) supplies the session key and
    ``platform_meta.id`` the platform domain.  A group event without a group id,
    a friend event carrying a group id, or any unknown/other message type is
    rejected with ``None`` instead of being guessed into a scope.
    """
    platform_name = _platform_id(event)
    if platform_name is None:
        return None
    session = _clean_field(_read_value(event, "unified_msg_origin"))
    if session is None:
        session = _clean_field(_read_value(event, "get_session_id"))
    if session is None:
        session = _clean_field(_read_value(event, "session_id"))
    if session is None:
        return None
    group = _clean_optional_id(_read_value(event, "get_group_id"))
    if group is None:
        return None
    message_type = _message_type_token(event)
    if message_type is None:
        # No message type available: the group id is the only reliable signal.
        return scene_for(platform_name, session, group)
    if message_type == _MESSAGE_TYPE_GROUP:
        if not group:
            return None
        return Scene(SCENE_GROUP, session, group, platform_name)
    if message_type in _MESSAGE_TYPE_PRIVATE:
        if group:
            # Inconsistent event: do not silently downgrade group content to private.
            return None
        return Scene(SCENE_PRIVATE, session, "", platform_name)
    return None


def is_private(scene: Scene | None) -> bool:
    return isinstance(scene, Scene) and scene.kind == SCENE_PRIVATE


def is_same_group(a: Scene | None, b: Scene | None) -> bool:
    """True only for two valid group scenes on the same platform and group."""
    if not isinstance(a, Scene) or not isinstance(b, Scene):
        return False
    if a.kind != SCENE_GROUP or b.kind != SCENE_GROUP:
        return False
    if not a.platform or not b.platform or a.platform != b.platform:
        return False
    if not a.group_id or not b.group_id:
        return False
    return a.group_id == b.group_id
