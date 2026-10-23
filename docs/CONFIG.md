# Configuration reference

Private JSON files are deployment-local and must never be committed. The
`examples/` directory contains redacted templates.

## Files

| File | Purpose | Key fields |
|---|---|---|
| `connection_guard.json` | Two-role NapCat/AstrBot health checks | `schema`, `recipient`, `token_file`, `roles[]` (`uin`, `port`, `onebot_port`, `runtime`, `group_id`, `dashboard_port`, `tts_port`) |
| `AstrBot/data/plugins/astrbot_plugin_ltm_fts/data/relationship.json` | Durable relationship switch | `enable` (bool), `character_id` (1-64 chars), `max_visible` (0-10) |
| `.../data/relationship_params.json` | Optional numeric overrides | Only known numeric keys from `relationship/params.py`; unknown or unsafe values are rejected |
| `AstrBot/data/sticker_groups.json` | Sticker group scope | `schema: 1`, `groups: ["<group-id>", ...]` (max 20, unique digits) |
| `AstrBot/data/sticker_profile.json` | Optional reviewed sticker manifest | `schema: 1`, `asset_root`, `manifest`, `subjects`, `groups` |
| `AstrBot/data/plugins/character_profile.json` | Character style | `schema: 1`, `name`, `aliases`, `interest_pattern` (regex), `turn_guidance` |

## Plugin settings

Two plugins expose AstrBot `_conf_schema.json` settings:

- `astrbot_plugin_ltm_fts`: `relationship.enable`, `relationship.character_id`,
  `relationship.max_visible`.
- `astrbot_plugin_stickers`: `enabled`, `probability` (0-1),
  `min_interval_seconds`.

Precedence for the relationship switch is plugin config > private
`relationship.json` > defaults. A corrupt private file fails the feature closed
and leaves normal dialogue running.

## Validation

```bash
python validate_config.py          # validate existing private configs and examples
python validate_config.py --root /path/to/deployment
```

The validator never prints secret values. It checks JSON shape, digit-only
identifiers, port ranges, regex validity, duplicate group ids and parameter
ranges. CI runs it against every example file.
