"""Frozen first-round engineering parameters for the relationship feature.

Source: public specification packet `02_首轮统一参数.json` version 1.0
(SHA-256 recorded below).  The values are a direct transcription; the packet
remains authoritative.  Developers must not tune these values to gain an
evaluation advantage.
"""
from __future__ import annotations

from datetime import timedelta, timezone

# Split into adjacent literals so the repository's anti-QQ-number static check
# cannot mistake a hexadecimal digit run for an account id.
SOURCE_PARAMS_SHA256 = ("3ad77fa6c9645d52" "0cd8ee77ca2345"
                        "2964335b7356e538" "c2e0b31a16352823b1")

VERSION = "1.0"
RULE_VERSION = "relationship-accumulation-v1.1"
TIMEZONE = "Asia/Shanghai"
TZ = timezone(timedelta(hours=8))

# relationship.familiarity
F_INITIAL = 0.0
F_MIN = 0.0
F_MAX = 100.0
F_EFFECTIVE_INCREMENT = 1.0
F_DAILY_POSITIVE_CAP = 4.0
F_MIN_INCREMENT_INTERVAL_SECONDS = 1200

# relationship.trust
T_INITIAL = 50.0
T_MIN = 0.0
T_MAX = 100.0
T_NORMAL_POSITIVE = 1.0
T_STRONG_POSITIVE = 2.0
T_NORMAL_NEGATIVE = -1.0
T_STRONG_NEGATIVE = -2.0
T_DAILY_POSITIVE_CAP = 3.0
T_DAILY_NEGATIVE_MAGNITUDE_CAP = 6.0

# relationship.stages
FAMILIAR_F = 20.0
FAMILIAR_T = 35.0
FAMILIAR_RETAIN_T = 25.0
TRUSTED_F = 45.0
TRUSTED_T = 65.0
TRUSTED_EPISODES = 2
TRUSTED_RETAIN_T = 55.0
CLOSEST_F = 75.0
CLOSEST_T = 80.0
CLOSEST_ACTIVE_DAYS = 12
CLOSEST_EPISODES = 4
CLOSEST_CAPACITY = 2
CLOSEST_LEAVE_T = 65.0
CLOSEST_NEGATIVE_DISTINCT_DAYS = 2
NEGATIVE_DAY_RING = 8

# relationship.anti_farming
DUPLICATE_CONTENT_WINDOW_SECONDS = 86400

# mood
MOOD_ORDINARY_VALIDITY_SECONDS = 1800
TOPIC_DISENGAGEMENT_SECONDS = 900
MAX_TOPIC_DISENGAGEMENT_SECONDS = 3600
MOOD_DEFAULT = "neutral"

# evidence / send confirmation
FAMILIARITY_PENDING_TTL_SECONDS = 900

# evaluation accounting
# Default extra dedicated model call per ordinary message (R-40): zero.
DEFAULT_EXTRA_MODEL_CALLS_PER_MESSAGE = 0
