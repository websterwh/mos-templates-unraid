#!/usr/bin/env python3
"""
scripts/update_templates.py - CI entry point for the scheduled GitHub Actions
workflow (.github/workflows/update-templates.yml).

Fetches the live Unraid Community Applications feed and fully regenerates
docker/*.json from scratch every run: existing files are wiped first, so
apps removed/renamed upstream don't linger, and every app already in the
feed gets rewritten to pick up any upstream changes (new version, fixed
description, new config option, etc).

Only real docker container templates are included - Unraid plugin entries
and feed rows with no Name are always skipped, no flags needed. This reuses
scrape_ca_templates.py's feed-fetch and conversion logic directly (imported,
not duplicated) so there's a single source of truth for the conversion
rules; this file is just a thin, flag-free wrapper meant for automation.

Usage (from repo root):
    python3 scripts/update_templates.py

Exits non-zero if any entry fails to convert, so the workflow run is
flagged red instead of silently committing a partial result.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import unraid2mos as core  # noqa: E402
import scrape_ca_templates as scraper  # noqa: E402

DOCKER_DIR = REPO_ROOT / "docker"


class _Args:
    """Stand-in for the argparse.Namespace that scrape_ca_templates.convert_group()
    expects - this script takes no CLI flags, so just hardcode the defaults."""

    pool = core.DEFAULT_POOL
    no_rewrite_paths = False
    no_normalize_ids = False
    puid = core.DEFAULT_PUID
    pgid = core.DEFAULT_PGID
    compact = False


def main():
    print(f"Fetching {scraper.DEFAULT_FEED_URL} ...")
    feed = scraper.fetch_feed(scraper.DEFAULT_FEED_URL, fallback_url=scraper.FALLBACK_FEED_URL)
    applist = feed.get("applist") or []
    print(f"Feed has {len(applist)} apps (last updated: {feed.get('last_updated', '?')})")

    normal = [
        app for app in applist
        if not scraper.is_invalid_entry(app) and not scraper.is_plugin_entry(app)
    ]
    skipped = len(applist) - len(normal)
    print(f"{len(normal)} valid docker templates, {skipped} plugin/invalid entries skipped\n")

    DOCKER_DIR.mkdir(parents=True, exist_ok=True)
    removed = 0
    for old_file in DOCKER_DIR.glob("*.json"):
        old_file.unlink()
        removed += 1
    print(f"Cleared {removed} existing template file(s) from {DOCKER_DIR}/\n")

    ok, failed = scraper.convert_group(
        normal, DOCKER_DIR, seen_filenames=set(), args=_Args(), old_pools=core.DEFAULT_OLD_POOLS
    )

    print(f"\nDone: {ok} converted, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
