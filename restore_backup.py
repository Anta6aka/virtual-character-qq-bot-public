"""Verify and restore a backup into a NEW directory, without touching a live deployment."""

import argparse
import shutil
from pathlib import Path

from backup_db import checksum, publish_directory, verify_backup


def restore(backup: Path, destination: Path) -> Path:
    backup = backup.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("Restore destination must not exist")
    if destination.is_relative_to(backup):
        raise ValueError("Restore destination cannot be inside the backup")
    manifest = verify_backup(backup)
    stage = destination.with_name(destination.name + ".partial")
    stage.mkdir(parents=True, exist_ok=False)
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        workspace = relative.parts[0] == "workspace"
        external_config = (relative.parts[:2] == ("external", "napcat_desktop")
                           or relative.as_posix() in {"external/voice_assets.json", "external/char_c_voice_assets.json", "external/char_b_voice_assets.json"})
        if not workspace and not external_config:
            continue
        source = backup / relative
        target = stage / relative.relative_to("workspace") if workspace else stage / relative
        source.resolve().relative_to(backup / ("workspace" if workspace else "external"))
        target.resolve().relative_to(stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if checksum(target) != entry["sha256"]:
            raise RuntimeError(f"Restored file checksum mismatch: {relative}")
    publish_directory(stage, destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(f"Restored workspace files to {restore(args.backup, args.destination)}")
