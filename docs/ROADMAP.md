# Public roadmap and gap analysis

This page records the gap review done on 2026-10-23, comparing this repository
with active open-source projects in the same space. It is a planning document,
not a promise of scope.

## Current snapshot

- 58 tracked files, 44 local tests, CI runs static checks only.
- Public docs cover architecture, setup and relationship design.
- No Docker/compose, no `pyproject.toml`, no packaged release artifact.
- Plugin code works, but the onboarding path still assumes a manually prepared
  AstrBot + NapCat environment.
- The project is intentionally a vertical character/relationship plugin set,
  not a general multi-platform bot platform.

## Comparison (GitHub data, 2026-10-23)

| Project | Positioning | Onboarding / packaging | Engineering | Docs / community |
|---|---|---|---|---|
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) (42.9k) | General agent/IM framework | Docker, compose, launcher, desktop, plugin market, guides | pyproject, tests, pre-commit, releases | docs site, six README languages, code of conduct, contributing |
| [LangBot](https://github.com/langbot-app/LangBot) (18.4k) | Production multi-platform agent platform | Docker, packaging, examples | pyproject, coverage, CLA, acceptance Dockerfile | docs site, eight README languages, architecture doc |
| [MaiBot](https://github.com/MaiM-with-u/MaiBot) (6.4k) | QQ character bot with memory/persona | Docker compose, devcontainer, dashboard | pyproject, tests, pre-commit, changelogs | bilingual docs, privacy policy, locale files |
| [Undefined](https://github.com/69gg/Undefined) (262) | OneBot QQ memory/skills bot | uv, pyproject, config example, scripts | tests, lockfile, architecture doc | contributing, roadmap-style docs |
| [zhenxun_bot](https://github.com/zhenxun-org/zhenxun_bot) (3.8k) | NoneBot QQ bot platform | Docker, `.env.example`, dev compose | tests, pyproject, lockfile, Prometheus | code of conduct, contributing, screenshots |
| [angel_memory](https://github.com/kawayiYokami/astrbot_plugin_angel_memory) (190) | AstrBot memory plugin | `requirements.txt`, `_conf_schema.json`, web UI | tests, pytest config, changelog | docs, screenshots, license |
| this repository | Vertical character/relationship plugin set | manual AstrBot + NapCat | 44 tests, static-only CI | architecture/setup/relationship docs, Chinese README |

## What the comparison changes

The first gap review focused on features. The comparison shows the larger gap is
**distribution and verification**, not missing character features:

1. Mature projects make installation a first-class product (Docker, compose,
   config schema, example env, dashboard/launcher). This repository still reads
   like a code snapshot.
2. Mature projects run tests and packaging checks in CI. A 5k-line relationship
   module with no public regression is a credibility gap.
3. AstrBot plugins in the ecosystem ship `_conf_schema.json` and a plugin
   README/logo/changelog; this repository relies on private JSON files instead.
4. Mature projects invest in bilingual docs, screenshots and troubleshooting.
   Character bots are visual products; a text-only README loses users early.
5. The unique asset here is the relationship/accumulation design and the
   source-aware FTS memory. That should be protected and documented, not hidden
   behind a hard-to-install layout.

## Revised priorities

### P0 — make it runnable and verifiable

- **One-command deployment**: Dockerfile + compose (AstrBot + NapCat + optional
  TTS), `.env.example`, and a "5-minute demo" with a neutral character profile
  and a placeholder sticker.
- **CI test matrix**: run `tests/run_tests.py` on Linux and Windows; add pure
  tests for `relationship/engine.py`, `store.py` and `service.py`.
- **AstrBot config schema**: add `_conf_schema.json` for each plugin, plus a
  `validate_config.py` and a configuration reference.
- **Acceptance**: a clean machine can reach a first reply by following README
  only; CI fails on a broken plugin or relationship test.

### P1 — make it trustworthy and discoverable

- `CODE_OF_CONDUCT.md`, issue/PR templates, `NOTICE`, asset/IP policy,
  `PRIVACY.md` and a clear vulnerability-reporting channel.
- English README (or bilingual) with screenshots/GIF and a real troubleshooting
  section (QR login, duplicate instances, WS stalls, plugin load failures).
- Per-plugin changelogs and release artifacts; decide whether exact tuning
  defaults stay public or move to example configuration.
- Add a public export script so the sanitized release cannot drift from the
  private production tree or leak private data.

### P2 — make it durable

- Cross-platform connection guard (or an explicit Windows-only statement),
  metrics/health endpoint and backup/restore guide.
- Refactor `relationship/` into smaller modules with type hints, lint and
  formatting; keep behaviour covered by tests.
- Migration guide for the relationship schema and conversation storage.

## Non-goals

- Replacing AstrBot/LangBot as a general multi-platform platform.
- Building a full web dashboard from scratch.
- Shipping copyrighted character personas, artwork, audio or logs.

## Bottom line

The project has a differentiated core (relationship state + source-aware
memory + dialogue guard). The fastest way to make it credible to outsiders is
to ship an installable demo, prove the core with CI tests, and publish the
same community/security baseline that comparable projects already have.
