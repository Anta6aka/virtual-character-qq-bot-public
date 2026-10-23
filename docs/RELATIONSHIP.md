# Relationship design (public overview)

This is a design-level overview. Exact default values live in
`AstrBot/data/plugins/astrbot_plugin_ltm_fts/relationship/params.py`; they are
example engineering defaults, not a recommended production tuning, and should
be reviewed for your own deployment.

## Goals

- Make long-term familiarity visible without turning chat into a scoreboard.
- Keep memory provenance and visibility separate from relationship state.
- Fail closed: a broken relationship config must never block normal dialogue.

## Dimensions

- **Familiarity**: accumulated, capped daily positive evidence with a minimum
  interval between gains.
- **Trust**: evidence-based, with bounded daily positive/negative movement.
- **Stage**: a coarse label (`initial`, `familiar`, `trusted`, `closest`,
  `distant_with_history`) derived from both dimensions and retention rules.
- **Seats**: a small number of high-intimacy seats; seats do not grant extra
  permissions or a quota of affection.
- **Mood**: a short-lived interaction state, not a relationship score.

## Accumulation

- Messages are classified conservatively (greetings, disagreement, promises,
  boundary respect, hostility, etc.).
- Duplicate content, farming and operational queries are filtered.
- Positive familiarity is capped per day and per minimum interval.
- Pending evidence is confirmed only after a successful send; failed sends do
  not mutate the ledger.
- Trust changes require explicit evidence and are capped per day.

## Visibility and prompt rendering

- Numeric scores, stage names, seat lists and debug JSON are never shown to the
  chat partner.
- Cross-scene memory only reuses items whose source visibility allows it.
- The model receives natural-language hints plus hard privacy rules.

## Privacy boundaries

- The private config file is the durable switch.
- The ledger lives in the local SQLite database.
- Incidents and sensitive self-deprecation material are gated separately and
  are not reused as casual banter.

## Known limitations

- Classification is deterministic and regex-based; it is not a semantic model.
- Pure agreement and operational queries can still be over-counted in some
  phrasings; tests cover the common cases only.
- Sticker frequency is model-gated: an 18% catalog offer does not guarantee an
  attachment.
- No automatic backfill for messages missed while the account was offline.
