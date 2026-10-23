# Setup

## 0. Docker quick start (recommended)

If Docker and Compose v2 are available:

```bash
cp .env.example .env
./scripts/demo-up.sh   # Windows: .\scripts\demo-up.ps1
```

Then open AstrBot at <http://localhost:6185> and NapCat at
<http://localhost:6099/webui>. The script mounts the neutral demo profile and a
placeholder sticker. See [examples/demo/README.md](../examples/demo/README.md).
The manual steps below remain the reference for non-Docker deployments.

## 1. Prerequisites

- Python 3.12+
- AstrBot 4.29.x
- NapCat / OneBot 11 with a reverse WebSocket to AstrBot
- Optional: GPT-SoVITS HTTP endpoint for Japanese TTS
- Optional: local sticker images

## 2. Install the plugins

Copy the directories under `AstrBot/data/plugins/` into your AstrBot
instance's `data/plugins/` directory, then restart AstrBot and confirm the
plugins load without tracebacks.

## 3. Configure the platform

Use AstrBot's `aiocqhttp` platform. The example reverse-WS port is `6199` for
the first bot and `6200` for a second bot. Keep tokens out of the repository.

## 4. Provide a character profile

Copy [character_profile.example.json](../examples/character_profile.example.json)
to `AstrBot/data/plugins/character_profile.json` and edit the name, aliases,
interest pattern and turn guidance. The repository intentionally does not ship
any character persona or copyrighted dialogue.

## 5. Enable the relationship feature (optional)

Copy [relationship.example.json](../examples/relationship.example.json) to
`AstrBot/data/plugins/astrbot_plugin_ltm_fts/data/relationship.json`.
The file is the durable switch because AstrBot strips unknown keys from its
global config. A corrupt file disables the feature without breaking dialogue.

### Optional parameter overrides

Copy [relationship_params.example.json](../examples/relationship_params.example.json)
to `AstrBot/data/plugins/astrbot_plugin_ltm_fts/data/relationship_params.json`.
Only known numeric keys are accepted; unknown keys, wrong types and unsafe
ranges are rejected and the relationship feature fails closed. The defaults in
`params.py` remain the public reference values.

## 6. Enable stickers (optional)

- Put your own assets under `stickers/` at the repository root, or provide a
  `sticker_profile.json` that points to your own manifest.
- Copy [sticker_groups.example.json](../examples/sticker_groups.example.json)
  to `AstrBot/data/sticker_groups.json` and set the allowed group IDs.
- No image assets are distributed with this repository.

## 7. Connection guard (optional)

Copy [connection_guard.example.json](../examples/connection_guard.example.json)
to `connection_guard.json` and edit UINs, ports, runtime paths and the token
file path. The guard is Windows-oriented and expects AstrBot/NapCat to be
reachable on localhost.

## 8. Run checks

```bash
python static_check.py
python tests/run_tests.py
```

## 9. Production hardening

- Run AstrBot and NapCat under a dedicated OS account with minimal rights.
- Keep `cmd_config.json`, `relationship.json`, `connection_guard.json`, token
  files, databases and logs outside Git.
- Back up `memory.db` and the private JSON files before upgrades.
- Review the QQ/NapCat/AstrBot terms of service before automating an account.
