# Architecture

## Runtime chain

```
QQ client
  ↕ (NapCat / OneBot 11)
AstrBot  ── aiocqhttp reverse WebSocket
  ├─ LTM plugin (memory, group context, relationship)
  ├─ sticker plugin (optional local images)
  ├─ Japanese TTS bridge (optional external GPT-SoVITS)
  ├─ dialogue guard (final output validation)
  └─ web search tool (optional)
```

## Request lifecycle

1. AstrBot receives a group/private message from NapCat.
2. The LTM plugin decides whether this is a direct turn, a continuation, or
   background context.
3. Recent group context and long-term memories are retrieved with source and
   visibility metadata.
4. The optional relationship service captures the inbound event and renders
   natural-language context. It never exposes numeric state to the model's
   chat partner.
5. The model produces a reply. The sticker plugin may offer a local sticker
   catalog; if the model selects one, it is attached to the same reply.
6. The TTS bridge may synthesize the Japanese portion and keep the Chinese
   translation as text.
7. The dialogue guard validates the final message shape before sending.
8. On a confirmed send, the relationship service records the reply and commits
   pending familiarity evidence.

## Storage

- `memory.db`: SQLite FTS memory, episodes, incidents, group context and the
  optional relationship tables. It is created lazily and is never committed.
- AstrBot's own `data_v4.db`: conversations, provider statistics and platform
  history. Never commit it.
- Private JSON files: relationship switch, sticker group scope, connection
  guard credentials and character profile. These are deployment-local.

## Failure boundaries

- Relationship configuration errors fail closed: dialogue keeps working.
- Missing sticker assets disable stickers, not the persona.
- A missing TTS service leaves text output intact.
- The connection guard only observes and recovers; it does not rewrite chat
  history.
- Every optional feature has a fallback path so a plugin failure does not take
  down the core dialogue loop.
