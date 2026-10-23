# Changelog

## 0.2.1 — configuration validation and plugin settings

- Added `validate_config.py` with tests for private configs and examples.
- Added `docs/CONFIG.md` and wired validation into CI.
- Added `_conf_schema.json` for the LTM relationship section and sticker
  probability/interval settings, with backwards-compatible defaults.

## 0.2.0 — Docker demo

- Added a Docker Compose stack based on the official NapCat `astrbot` mode.
- Added `.env.example`, demo up/down scripts (sh + PowerShell).
- Added a neutral demo character, placeholder sticker and demo instructions.
- Added Compose validation to CI.

## 0.1.1 — configurable relationship parameters

- Added optional `relationship_params.json` overrides with validation and
  fail-closed behaviour.
- Added a focused test for override validation.
- Documented the tuning path in the public setup and relationship docs.

## 0.1.0 — initial public release

- Sanitized public snapshot of the AstrBot character bot plugin set.
- LTM FTS memory, group context, optional relationship state.
- Local sticker selection, Japanese TTS bridge, dialogue guard, web search.
- Connection guard, static checks and a small isolated test suite.
- No character personas, sticker assets, private configuration or databases.
