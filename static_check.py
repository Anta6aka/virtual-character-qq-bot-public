"""Repository static checks that work locally and in CI without AstrBot installed.

Covers: Python AST, JSON parse, Markdown relative links, tracked-file allow-list,
QQ-number and secret regressions. Exit 0 when clean, 1 otherwise.
"""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORBIDDEN_TRACKED_NAMES = {
    "peer_roles.json", "peer_chat.json", "sticker_groups.json", "sticker_profile.json",
    "cmd_config.json", "connection_guard.json", "deployment.json", "operator-access.json",
    "connection_health.json",
}
FORBIDDEN_SUFFIXES = {".db", ".log", ".pyc", ".db-wal", ".db-shm"}
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
QQ_NUMBER = re.compile(r"(?<![0-9])[0-9]{9,12}(?![0-9])")
LINK = re.compile(r"\]\(([^)\s]+)\)")


def tracked_files():
    try:
        raw = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, check=True).stdout
        return [ROOT / item for item in raw.decode("utf-8").split("\0") if item]
    except (OSError, subprocess.SubprocessError):
        result = []
        for path in ROOT.rglob("*"):
            rel = path.relative_to(ROOT)
            if path.is_file() and ".git" not in rel.parts:
                result.append(path)
        return result


def rel(path):
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def check_python(files, problems):
    for path in files:
        if path.suffix != ".py":
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError) as error:
            problems.append(f"python: {rel(path)}: {error}")


def check_json(files, problems):
    for path in files:
        if path.suffix != ".json":
            continue
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as error:
            problems.append(f"json: {rel(path)}: {error}")


def check_markdown_links(files, problems, tracked):
    for path in files:
        if path.suffix != ".md":
            continue
        for target in LINK.findall(path.read_text(encoding="utf-8", errors="replace")):
            if target.startswith(("http://", "https://", "mailto:", "#", "<")):
                continue
            if re.match(r"^[A-Za-z]:[\\/]", target):  # local absolute path, not portable
                continue
            candidate = (path.parent / target.split("#")[0]).resolve()
            if candidate not in tracked and not candidate.exists():
                problems.append(f"markdown: {rel(path)} -> missing {target}")


def check_forbidden(files, problems):
    for path in files:
        name = path.name
        if name in FORBIDDEN_TRACKED_NAMES:
            problems.append(f"privacy: private config tracked: {rel(path)}")
        if path.suffix in FORBIDDEN_SUFFIXES:
            problems.append(f"privacy: private artifact tracked: {rel(path)}")
        if "AstrBot" in path.parts and path.suffix == ".py":
            text = path.read_text(encoding="utf-8", errors="replace")
            match = QQ_NUMBER.search(text)
            if match:
                problems.append(f"privacy: QQ-like number in {rel(path)}: {match.group(0)[:3]}***")
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                problems.append(f"secret: {rel(path)} matches {pattern.pattern}")


def check_yaml(files, problems):
    try:
        import yaml
    except ImportError:
        return
    for path in files:
        if path.suffix not in {".yaml", ".yml"}:
            continue
        try:
            yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        except Exception as error:  # noqa: BLE001 - report any parse failure
            problems.append(f"yaml: {rel(path)}: {type(error).__name__}: {error}")


def main():
    files = tracked_files()
    tracked = {path.resolve() for path in files}
    problems = []
    check_python(files, problems)
    check_json(files, problems)
    check_markdown_links(files, problems, tracked)
    check_forbidden(files, problems)
    check_yaml(files, problems)
    print(f"checked {len(files)} tracked files")
    if problems:
        for problem in problems:
            print("FAIL " + problem)
        print(f"{len(problems)} problem(s)")
        return 1
    print("static checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
