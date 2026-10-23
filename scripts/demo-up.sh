#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
[ -f .env ] || cp .env.example .env
mkdir -p runtime/astrbot runtime/napcat-config runtime/ntqq runtime/plugin-data
cp examples/demo/relationship.json runtime/plugin-data/relationship.json
docker compose up -d
docker compose ps
printf '
AstrBot: http://localhost:6185
NapCat:  http://localhost:6099/webui
'
