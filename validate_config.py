"""Validate deployment-local JSON configs and example configs.

This script never prints secret values and never imports AstrBot. It is safe to
run in CI and against a prepared deployment directory.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PATHS = (
    ("connection_guard.json", "guard"),
    ("AstrBot/data/plugins/astrbot_plugin_ltm_fts/data/relationship.json", "relationship"),
    ("AstrBot/data/plugins/astrbot_plugin_ltm_fts/data/relationship_params.json", "params"),
    ("AstrBot/data/sticker_groups.json", "groups"),
    ("AstrBot/data/sticker_profile.json", "sticker_profile"),
    ("AstrBot/data/plugins/character_profile.json", "profile"),
)


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        return error


def _is_digits(value, maximum=20):
    return isinstance(value, str) and value.isdigit() and 0 < len(value) <= maximum


def validate_guard(data):
    errors = []
    if not isinstance(data, dict):
        return ["must be a JSON object"]
    if data.get("schema") != 1:
        errors.append("schema must be 1")
    if not _is_digits(data.get("recipient")):
        errors.append("recipient must be a digit string")
    roles = data.get("roles")
    if not isinstance(roles, list) or len(roles) != 2:
        errors.append("roles must contain exactly two entries")
        return errors
    if len({role.get("uin") for role in roles if isinstance(role, dict)}) != 2:
        errors.append("role UINs must be distinct")
    for index, role in enumerate(roles):
        prefix = "roles[%d]." % index
        if not isinstance(role, dict):
            errors.append(prefix + "must be an object")
            continue
        if not _is_digits(role.get("uin")):
            errors.append(prefix + "uin must be a digit string")
        if role.get("port") not in (6099, 6100):
            errors.append(prefix + "port must be 6099 or 6100")
        if role.get("onebot_port") not in (6199, 6200):
            errors.append(prefix + "onebot_port must be 6199 or 6200")
        if not isinstance(role.get("name"), str) or not role["name"].strip():
            errors.append(prefix + "name must be a non-empty string")
        if not isinstance(role.get("runtime"), str) or not role["runtime"].strip():
            errors.append(prefix + "runtime must be a non-empty string")
        if not _is_digits(role.get("group_id")):
            errors.append(prefix + "group_id must be a digit string")
        for key in ("dashboard_port", "tts_port"):
            if not isinstance(role.get(key), int) or role[key] <= 0:
                errors.append(prefix + key + " must be a positive integer")
    return errors


def validate_relationship(data):
    errors = []
    if not isinstance(data, dict):
        return ["must be a JSON object"]
    if not isinstance(data.get("enable"), bool):
        errors.append("enable must be true or false")
    character_id = data.get("character_id")
    if not isinstance(character_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", character_id):
        errors.append("character_id must be 1-64 letters, digits, _ or -")
    visible = data.get("max_visible")
    if not isinstance(visible, int) or isinstance(visible, bool) or not 0 <= visible <= 10:
        errors.append("max_visible must be an integer between 0 and 10")
    return errors


def validate_params(data):
    errors = []
    if not isinstance(data, dict):
        return ["must be a JSON object"]
    path = ROOT / "AstrBot/data/plugins/astrbot_plugin_ltm_fts/relationship/params.py"
    try:
        spec = importlib.util.spec_from_file_location("relationship_params_validate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.apply_overrides(data)
    except Exception as error:  # noqa: BLE001 - report, never crash the validator
        errors.append(str(error)[:200])
    return errors


def validate_groups(data):
    errors = []
    if not isinstance(data, dict):
        return ["must be a JSON object"]
    if data.get("schema") != 1:
        errors.append("schema must be 1")
    groups = data.get("groups")
    if not isinstance(groups, list) or len(groups) > 20:
        errors.append("groups must be a list with at most 20 entries")
    elif any(not _is_digits(item) for item in groups):
        errors.append("every group must be a digit string")
    elif len(set(groups)) != len(groups):
        errors.append("groups must be unique")
    return errors


def validate_profile(data):
    errors = []
    if not isinstance(data, dict):
        return ["must be a JSON object"]
    if data.get("schema") != 1:
        errors.append("schema must be 1")
    if not isinstance(data.get("name"), str) or not 1 <= len(data["name"]) <= 80:
        errors.append("name must be 1-80 characters")
    aliases = data.get("aliases")
    if not isinstance(aliases, list) or not 1 <= len(aliases) <= 20:
        errors.append("aliases must be a list with 1-20 entries")
    elif any(not isinstance(item, str) or not item.strip() or len(item) > 40 for item in aliases):
        errors.append("aliases must be non-empty strings up to 40 characters")
    pattern = data.get("interest_pattern")
    if not isinstance(pattern, str) or not pattern:
        errors.append("interest_pattern must be a non-empty string")
    else:
        try:
            re.compile(pattern)
        except re.error:
            errors.append("interest_pattern must be a valid regular expression")
    guidance = data.get("turn_guidance")
    if not isinstance(guidance, str) or not guidance.strip() or len(guidance) > 4000:
        errors.append("turn_guidance must be a non-empty string up to 4000 characters")
    return errors


def validate_sticker_profile(data):
    errors = []
    if not isinstance(data, dict):
        return ["must be a JSON object"]
    if data.get("schema") != 1:
        errors.append("schema must be 1")
    for key in ("asset_root", "manifest"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            errors.append(key + " must be a non-empty string")
    subjects = data.get("subjects")
    if not isinstance(subjects, list) or not subjects or any(
            not isinstance(item, str) or not item.strip() for item in subjects):
        errors.append("subjects must be a non-empty list of strings")
    groups = data.get("groups")
    if not isinstance(groups, list) or any(not _is_digits(item) for item in groups):
        errors.append("groups must be a list of digit strings")
    return errors


CHECKS = {
    "guard": validate_guard,
    "relationship": validate_relationship,
    "params": validate_params,
    "groups": validate_groups,
    "profile": validate_profile,
    "sticker_profile": validate_sticker_profile,
}


def classify(path):
    name = Path(path).name
    if "connection_guard" in name:
        return "guard"
    if "relationship_params" in name:
        return "params"
    if name.startswith("relationship"):
        return "relationship"
    if "character_profile" in name:
        return "profile"
    if "sticker_groups" in name:
        return "groups"
    if "sticker_profile" in name:
        return "sticker_profile"
    return ""


def validate_file(path, kind=None):
    kind = kind or classify(path)
    if not kind:
        return ["unknown config kind"]
    data = _load(path)
    if isinstance(data, Exception):
        return ["invalid JSON: " + str(data)]
    return CHECKS[kind](data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--strict", action="store_true", help="fail when no config files are found")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    targets = []
    for relative, kind in DEFAULT_PATHS:
        path = root / relative
        if path.exists():
            targets.append((path, kind))
    examples = root / "examples"
    if examples.exists():
        for path in sorted(examples.rglob("*.json")):
            kind = classify(path)
            if kind:
                targets.append((path, kind))
    errors = 0
    checked = 0
    for path, kind in targets:
        checked += 1
        problems = validate_file(path, kind)
        if problems:
            errors += len(problems)
            print("FAIL " + str(path.relative_to(root)))
            for problem in problems:
                print("  - " + problem)
        else:
            print("OK   " + str(path.relative_to(root)))
    print("checked %d config file(s)" % checked)
    if errors:
        print("%d problem(s)" % errors)
        return 1
    if checked == 0 and args.strict:
        print("no config files found")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
