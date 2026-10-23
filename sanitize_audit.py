"""Audit tracked files for data that must be removed before a public push.

Read-only and masking-first: findings show file/line plus a shortened sample,
never the full account, nickname or path. Use it before QP-26 (public) only;
a private repository may keep these values.

Exit 0 by default (report), 2 with --fail-on-findings.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".ps1", ".txt", ".gitignore", ".gitattributes", ""}
SKIP_SUFFIXES = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".db", ".zip", ".exe", ".dll"}

QQ_LIKE = re.compile(r"(?<![0-9A-Za-z])[0-9]{9,12}(?![0-9A-Za-z])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
WINDOWS_USER = re.compile(r"[A-Za-z]:\\Users\\([^\\\s\"']+)")


def mask(value, keep=2):
    value = str(value)
    return value[:keep] + "***" if len(value) > keep else "***"


def is_epoch(value):
    """Unix timestamps around 2025-2028 are harmless noise, not accounts."""
    try:
        number = int(value)
    except ValueError:
        return False
    return 1_700_000_000 <= number <= 1_850_000_000


def tracked_files():
    try:
        raw = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, check=True).stdout
        return [ROOT / item for item in raw.decode("utf-8").split("\0") if item]
    except (OSError, subprocess.SubprocessError):
        return []


def scan_text(text, terms=()):
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in QQ_LIKE.finditer(line):
            if is_epoch(match.group(0)):
                continue
            findings.append({"kind": "qq_like", "line": number, "sample": mask(match.group(0), 3)})
        for match in EMAIL.finditer(line):
            findings.append({"kind": "email", "line": number, "sample": mask(match.group(0), 3)})
        for match in WINDOWS_USER.finditer(line):
            findings.append({"kind": "windows_user", "line": number, "sample": mask(match.group(1))})
        for term in terms:
            if term and term in line:
                findings.append({"kind": "term", "line": number, "sample": mask(term)})
    return findings


def scan_file(path, terms=()):
    if path.suffix.lower() in SKIP_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [{**item, "file": str(path.relative_to(ROOT))} for item in scan_text(text, terms)]


def commit_emails():
    try:
        raw = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%ae"],
                             capture_output=True, check=True).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.strip() for line in raw.splitlines() if line.strip()})


def collect(terms=()):
    report = {"files": {}, "commit_emails": [mask(email, 3) for email in commit_emails()]}
    counts = {}
    for path in tracked_files():
        findings = scan_file(path, terms)
        if findings:
            report["files"][str(path.relative_to(ROOT))] = findings
            for item in findings:
                counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    report["counts"] = counts
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terms-file", type=Path,
                        help="extra private terms to look for, one per line (not committed)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-findings", action="store_true")
    args = parser.parse_args()
    terms = []
    if args.terms_file and args.terms_file.is_file():
        terms = [line.strip() for line in args.terms_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = collect(terms)
    total = sum(report["counts"].values())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"audited tracked files; findings={total} counts={report['counts']}")
        print(f"commit author emails: {len(report['commit_emails'])} distinct (masked)")
        for name, items in list(report["files"].items())[:40]:
            kinds = {}
            for item in items:
                kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
            print(f"  {name}: {kinds}")
        if len(report["files"]) > 40:
            print(f"  ... {len(report['files']) - 40} more files")
        print("Review before a public push (QP-24/QP-26); a private repository may keep these.")
    return 2 if (args.fail_on_findings and total) else 0


if __name__ == "__main__":
    sys.exit(main())
