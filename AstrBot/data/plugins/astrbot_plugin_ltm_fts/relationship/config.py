"""Durable private relationship settings and stable character ids.

AstrBot removes unknown keys from the global config on startup, so the private
JSON file under the plugin data directory is the durable switch:

    <instance>/AstrBot/data/plugins/astrbot_plugin_ltm_fts/data/relationship.json

Field-level precedence: AstrBot config (if present) > private file > defaults.
A corrupt private file fails the feature closed instead of silently enabling it.
"""

import json
from pathlib import Path

CHARACTER_IDS = {
    "角色A": "char_a",
    "角色B": "char_b",
}


def load_private_settings(path):
    """Return (mapping, error). A missing file is normal; an invalid file is an error."""
    path = Path(path)
    if not path.exists():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as error:  # noqa: BLE001 - report, never crash the dialogue path
        return {}, f"invalid private relationship config: {error}"
    if not isinstance(data, dict):
        return {}, "private relationship config must be a JSON object"
    return data, ""


_FIELDS = ("enable", "character_id", "max_visible")


def validate_settings(mapping):
    """Reject wrong field types instead of silently falling back to defaults.

    Any present-but-invalid known field disables the feature (fail-closed);
    unknown keys are ignored so forward-compatible metadata is harmless.
    """
    data = dict(mapping or {})
    errors = []
    if "enable" in data and not isinstance(data["enable"], bool):
        errors.append("enable must be a JSON boolean")
    if "character_id" in data:
        value = data["character_id"]
        if not isinstance(value, str) or not value.strip() or len(value) > 40:
            errors.append("character_id must be a non-empty string (<=40 chars)")
    if "max_visible" in data:
        value = data["max_visible"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5:
            errors.append("max_visible must be an integer 0..5")
    if errors:
        return {"enable": False, "_error": "; ".join(errors)}
    return {key: data[key] for key in _FIELDS if key in data}


def resolve_settings(remote, private, private_error=""):
    """Merge settings per field, then validate; AstrBot config wins over the private file.

    A present-but-invalid private file or field disables the whole feature so a
    broken config can never silently enable or mutate it. The same function is
    used at startup and on every event, so a file changed after startup is
    re-validated instead of trusting the initial service state.
    """
    if private_error:
        return {"enable": False, "_error": private_error}
    merged = dict(private or {})
    if isinstance(remote, dict):
        merged.update(remote)
    elif remote is not None:
        return {"enable": False, "_error": "relationship section in AstrBot config must be an object"}
    return validate_settings(merged)


def stable_character_id(section, persona_name):
    """Explicit id wins; otherwise a fixed persona map; unknown personas disable the feature."""
    explicit = (section or {}).get("character_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return CHARACTER_IDS.get(str(persona_name), "")
