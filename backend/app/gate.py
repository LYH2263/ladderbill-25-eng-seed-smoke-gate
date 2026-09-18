"""Seed & health gate (read-only, repeatable).

Asserts the fixtures the rest of the project depends on:

* at least one ``dirty`` account (seed name carries the marker ``种子``)
* and at least one ``clean`` account (any account without that marker);
* the default three-tier progressive price ladder is present and its
  ``up_to`` bounds are strictly increasing (the final tier may be the
  open-ended ``NULL`` bound);
* the ``/api/health`` endpoint answers ``ok == true`` and identifies the
  project as ``ladderbill``.

Every failed assertion is reported under a stable item name (see the
``MISSING_*`` constants) so failures name what is absent instead of a vague
"check failed". The gate only reads data; it never rewrites seed rows or
billing numbers to make itself green.

Run after ``docker compose up``::

    docker compose exec backend python -m app.gate

Exit code is 0 when nothing is missing, 1 otherwise. Override the target
with ``BASE_URL`` (default ``http://localhost:9100``).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

from app import db

PROJECT_NAME = "ladderbill"
HEALTH_PATH = "/api/health"
DEFAULT_BASE_URL = "http://localhost:9100"
DEFAULT_TIMEOUT = 3.0

DIRTY_MARKER = "种子"

# Stable missing-item names. Printed verbatim on stderr and in the JSON
# report; treat them as a public contract of the gate and do not rename or
# drop them to force a green run.
MISSING_ACCOUNT_DIRTY = "account.dirty"
MISSING_ACCOUNT_CLEAN = "account.clean"
MISSING_TIERS_COUNT = "tiers.three_bands"
MISSING_TIERS_MONOTONIC = "tiers.up_to_monotonic"
MISSING_HEALTH_REACHABLE = "health.reachable"
MISSING_HEALTH_OK = "health.ok"
MISSING_HEALTH_PROJECT = "health.project"

ACCOUNT_ITEMS = (MISSING_ACCOUNT_DIRTY, MISSING_ACCOUNT_CLEAN)
TIER_ITEMS = (MISSING_TIERS_COUNT, MISSING_TIERS_MONOTONIC)


def _is_dirty(name: str | None) -> bool:
    """Mirror the dashboard's dirty/clean split in billing_service.py."""
    return DIRTY_MARKER in (name or "")


def check_accounts(conn: sqlite3.Connection) -> list[str]:
    names = [r["name"] for r in conn.execute("SELECT name FROM accounts").fetchall()]
    missing: list[str] = []
    if not any(_is_dirty(n) for n in names):
        missing.append(MISSING_ACCOUNT_DIRTY)
    if not any(not _is_dirty(n) for n in names):
        missing.append(MISSING_ACCOUNT_CLEAN)
    return missing


def check_tiers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT up_to, price FROM tiers ORDER BY sort_order, id"
    ).fetchall()
    missing: list[str] = []

    if len(rows) != 3 or any(r["price"] is None for r in rows):
        missing.append(MISSING_TIERS_COUNT)

    # Non-NULL bounds must increase strictly; NULL means the open-ended top
    # band and is only allowed on the final tier.
    monotonic = True
    previous: float | None = None
    for index, row in enumerate(rows):
        bound = row["up_to"]
        if bound is None:
            if index != len(rows) - 1:
                monotonic = False
            break
        try:
            value = float(bound)
        except (TypeError, ValueError):
            monotonic = False
            break
        if previous is not None and value <= previous:
            monotonic = False
            break
        previous = value
    if not monotonic:
        missing.append(MISSING_TIERS_MONOTONIC)

    return missing


def check_health(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    url = base_url.rstrip("/") + HEALTH_PATH
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return [MISSING_HEALTH_REACHABLE]

    missing: list[str] = []
    if payload.get("ok") is not True:
        missing.append(MISSING_HEALTH_OK)
    if payload.get("project") != PROJECT_NAME:
        missing.append(MISSING_HEALTH_PROJECT)
    return missing


def run_checks(base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Return the sorted list of missing item names (empty == all green)."""
    base_url = base_url or os.environ.get("BASE_URL", DEFAULT_BASE_URL)
    missing: list[str] = []

    try:
        conn = db.connect()
    except sqlite3.Error:
        missing.extend(ACCOUNT_ITEMS)
        missing.extend(TIER_ITEMS)
    else:
        try:
            try:
                missing.extend(check_accounts(conn))
            except sqlite3.Error:
                missing.extend(ACCOUNT_ITEMS)
            try:
                missing.extend(check_tiers(conn))
            except sqlite3.Error:
                missing.extend(TIER_ITEMS)
        finally:
            conn.close()

    missing.extend(check_health(base_url, timeout))
    return sorted(set(missing))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ladderbill seed & health gate")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BASE_URL", DEFAULT_BASE_URL),
        help="API base URL (default: %(default)s or $BASE_URL)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    missing = run_checks(args.base_url, args.timeout)
    print(json.dumps({"ok": not missing, "missing": missing}, ensure_ascii=False))
    if missing:
        for name in missing:
            print(f"missing: {name}", file=sys.stderr)
        print(
            "gate failed, missing items: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    print("gate ok, missing=[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
