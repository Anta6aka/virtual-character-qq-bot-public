"""Dry-run first cleanup for temp artifacts. Backups are never touched."""
import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMP_ROOT = ROOT / "temp"


def _within(path, base):
    return path == base or base in path.parents


def guard_root(root):
    """Only temp/ (or an explicit directory outside the repository) may be cleaned.

    A root of the repository, a parent directory, source trees and backups are
    always refused so a single wrong --root cannot delete code or private data.
    """
    resolved = Path(root).resolve()
    repo = ROOT.resolve()
    temp = TEMP_ROOT.resolve()
    backups = (ROOT / "backups").resolve()
    if _within(resolved, backups):
        raise ValueError("refusing to clean the backups directory")
    if _within(repo, resolved) and resolved != repo:
        raise ValueError("refusing to clean a parent of the repository")
    if _within(resolved, repo) and not _within(resolved, temp):
        raise ValueError("only temp/ may be cleaned inside the repository")
    return resolved


def stale_files(root, days, now=None):
    """Return [(path, size)] for files older than `days`."""
    resolved = guard_root(root)
    current = time.time() if now is None else now
    cutoff = current - days * 86400
    result = []
    if not resolved.exists():
        return result
    for path in sorted(resolved.rglob("*")):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                result.append((path, path.stat().st_size))
        except OSError:
            continue
    return result


def summarize(items):
    return len(items), sum(size for _, size in items)


def remove(items):
    backups = (ROOT / "backups").resolve()
    removed = 0
    for path, _ in items:
        try:
            if _within(path.resolve(), backups):
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=TEMP_ROOT)
    parser.add_argument("--days", type=float, default=14.0)
    parser.add_argument("--apply", action="store_true", help="actually delete; default is dry-run")
    args = parser.parse_args()
    items = stale_files(args.root, args.days)
    count, size = summarize(items)
    print(f"{'DELETE' if args.apply else 'DRY-RUN'}: {count} files, {size / 1048576:.1f} MiB "
          f"older than {args.days:g} days under {args.root}")
    for path, file_size in items[:20]:
        print(f"  {path}  {file_size}")
    if count > 20:
        print(f"  ... {count - 20} more")
    if args.apply:
        print(f"removed {remove(items)} files")


if __name__ == "__main__":
    main()
