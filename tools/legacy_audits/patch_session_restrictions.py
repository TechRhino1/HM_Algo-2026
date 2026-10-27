"""One-off migration: gate orchestrator session blackouts on the symbol profile.

``is_asian_blackout`` / ``is_rollover_blackout`` used to fire for every
non-crypto symbol. Metals profiles that set ``session_restriction=False`` (XAU)
were still blacked out between 00:00-06:00 and 21:00-23:00 UTC, which contradicted
the profile. This script rewrites the block to honour the profile flag.

Idempotent and import-safe: re-running it on an already-patched tree prints a
status line and changes nothing. The import sweep imports every tool, so the file
must not read or write anything at import time.

Usage: python -m tools.patch_session_restrictions
"""
import os

ORCH_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "jarvis", "application", "orchestrator.py",
)

TARGET_ORCH = """        is_asian_pair = any(c in str(symbol).upper() for c in ("JPY", "AUD", "NZD"))
        is_asian_blackout = (0 <= now_utc_hour < 6) and not is_crypto(symbol) and not is_asian_pair
        is_rollover_blackout = (21 <= now_utc_hour <= 23) and not is_crypto(symbol)
        is_session_blackout = is_asian_blackout or is_rollover_blackout"""

REPLACE_ORCH = """        is_asian_pair = any(c in str(symbol).upper() for c in ("JPY", "AUD", "NZD"))
        has_session_restriction = getattr(_prof, "session_restriction", True) if _prof else True
        is_asian_blackout = (0 <= now_utc_hour < 6) and not is_crypto(symbol) and not is_asian_pair and has_session_restriction
        is_rollover_blackout = (21 <= now_utc_hour <= 23) and not is_crypto(symbol) and has_session_restriction
        is_session_blackout = is_asian_blackout or is_rollover_blackout"""


def main():
    with open(ORCH_PATH, "r", encoding="utf-8") as fh:
        source = fh.read()

    if REPLACE_ORCH in source:
        print("orchestrator.py already patched; nothing to do")
        return 0

    if TARGET_ORCH not in source:
        print("target_orch not found in orchestrator.py; the block has moved, nothing to do")
        return 0

    with open(ORCH_PATH, "w", encoding="utf-8") as fh:
        fh.write(source.replace(TARGET_ORCH, REPLACE_ORCH))

    print("orchestrator.py blackout logic patched successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
