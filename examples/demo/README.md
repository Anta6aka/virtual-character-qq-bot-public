# 5-minute demo

This demo uses Docker Compose with the official AstrBot and NapCat images. It
does not ship any character persona, artwork or logs.

## Prerequisites

- Docker Desktop (or Docker Engine + Compose v2)
- A QQ account that can be logged in through NapCat WebUI
- An LLM provider key supported by AstrBot

## Steps

1. Copy `.env.example` to `.env` and change `NAPCAT_WEBUI_TOKEN`.
2. Start the stack:

   ```bash
   ./scripts/demo-up.sh
   # Windows: .\scripts\demo-up.ps1
   ```

3. Open <http://localhost:6185> (AstrBot) and configure a provider and a
   persona. The demo uses a neutral "小助手" profile; replace it if you want.
4. Open <http://localhost:6099/webui> (NapCat), log in with QQ, and wait for the
   reverse WebSocket to connect.
5. Replace the placeholder group id in `examples/demo/sticker_groups.demo.json` with
   a real group id, then restart AstrBot:

   ```bash
   docker compose restart astrbot
   ```

6. Send a message in that group. The bot should reply; stickers are optional and
   may be rate-limited by the plugin.

## What is mounted

- `runtime/astrbot`: AstrBot data, configuration and databases.
- `runtime/plugin-data`: the demo relationship config and plugin database.
- `examples/demo/character_profile.json`: neutral demo profile.
- `examples/demo/stickers`: one placeholder sticker.
- `examples/demo/sticker_groups.demo.json`: sticker group scope (placeholder id).

## Troubleshooting

- **NapCat does not connect**: open NapCat WebUI and check the reverse WebSocket
  target is `ws://astrbot:6199/ws`.
- **No reply**: confirm AstrBot has a provider key and a persona, and that the
  plugin appears in AstrBot's plugin list.
- **Port already in use**: change `ASTRBOT_PORT` / `NAPCAT_WEBUI_PORT` in `.env`.
- **TTS**: run GPT-SoVITS on the host and point the AstrBot TTS provider at
  `http://host.docker.internal:9880` (Linux uses the `extra_hosts` entry in
  `compose.yml`).

## Stop

```bash
./scripts/demo-down.sh
# Windows: .\scripts\demo-down.ps1
```
