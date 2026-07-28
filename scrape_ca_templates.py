#!/usr/bin/env python3
"""
scrape_ca_templates.py - Bulk-convert Unraid Community Applications templates
to MOS Hub docker templates, without hitting thousands of individual repos.

Unraid's Community Applications plugin reads one master feed
(ca.unraid.net's applicationFeed.json - this is what the live CA plugin
itself downloads, per its include/paths.php) that already contains every
registered template's full config inline - name, repo, paths, ports,
variables, the works. This script downloads that single feed, filters it,
and converts whatever matches using the same logic as unraid2mos.py.

The feed also contains entries that are NOT docker containers - Unraid
plugins (category "Plugins") and a handful of malformed/incomplete rows
with no Name at all. Neither of those convert into anything useful as a
MOS docker template, so both are skipped by default:

    - Plugin entries: skipped unless --include-plugins is passed, in which
      case they're converted anyway and written to <output-dir>/plugins/
      instead of the main output dir, since the result won't be a real
      docker template.
    - No-Name entries: skipped unless --include-invalid is passed, in which
      case a fallback name is derived from Repository (or a generic
      "unnamed-N") and the result is written to <output-dir>/invalid/.

Requires unraid2mos.py in the same folder (imported as a module - no pip
installs needed either way).

Usage:
    python3 scrape_ca_templates.py --list-only --search plex
    python3 scrape_ca_templates.py --search jellyfin jellyseerr -o out/
    python3 scrape_ca_templates.py --category "MediaServer:" -o out/
    python3 scrape_ca_templates.py --all -o out/          # every app in the feed (~3700+, slow)
    python3 scrape_ca_templates.py --all -o out/ --include-plugins --include-invalid

Run with --help for all options.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import unraid2mos as core  # noqa: E402

DEFAULT_FEED_URL = "https://ca.unraid.net/assets/feed/applicationFeed.json"
FALLBACK_FEED_URL = "https://ca.unraid.net/cdn/feed/applicationFeed.json"

# ca.unraid.net rejects requests with urllib's default "Python-urllib/x.y"
# User-Agent (403 Forbidden), so send something that looks like a normal
# client - same reason the plugin itself doesn't hit this (curl sends a
# sane default UA).
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (unraid2mos-scraper)",
    "Accept": "application/json",
}


def _fetch_url(url: str) -> dict:
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_feed(url: str, fallback_url: str = None) -> dict:
    """Accepts a URL or a local file path (handy for re-using an
    already-downloaded feed without hitting the network every time).

    If url is the default feed URL and it fails, tries fallback_url once -
    mirrors the CA plugin's own assets -> cdn failover behavior."""
    if url.startswith("http://") or url.startswith("https://"):
        try:
            return _fetch_url(url)
        except Exception as e:
            if not fallback_url:
                raise
            print(f"Primary feed URL failed ({e}), trying fallback {fallback_url} ...", file=sys.stderr)
            return _fetch_url(fallback_url)
    return json.loads(Path(url).read_text(encoding="utf-8", errors="replace"))


def matches_search(app: dict, terms: list) -> bool:
    haystack = f"{app.get('Name', '')} {app.get('Repository', '')}".lower()
    return any(t.lower() in haystack for t in terms)


def matches_category(app: dict, term: str) -> bool:
    cats = app.get("CategoryList") or []
    term_low = term.lower()
    return any(term_low in c.lower() for c in cats)


def is_plugin_entry(app: dict) -> bool:
    """Unraid plugin entries (.plg installs, not docker containers) live in
    the same feed as docker templates. They're consistently tagged with a
    'Plugins' category, so that's the signal we key off of."""
    cats = [c.strip().lower() for c in (app.get("CategoryList") or [])]
    return "plugins" in cats


def is_invalid_entry(app: dict) -> bool:
    """Feed entries with no Name can't produce a usable template."""
    return not (app.get("Name") or "").strip()


def fallback_name(app: dict, index: int) -> str:
    """Best-effort name for an otherwise-nameless feed entry, used only
    when --include-invalid is passed."""
    repo = (app.get("Repository") or "").strip()
    if repo:
        base = repo.rstrip("/").split("/")[-1]
        base = re.sub(r"\.(plg|xml|json)$", "", base, flags=re.IGNORECASE)
        if base:
            return base
    return f"unnamed-{index}"


def config_value(cfg: dict):
    attrs = cfg.get("@attributes", {}) or {}
    val = cfg.get("value")
    if not val:
        val = attrs.get("Default", "")
    return attrs, (val or "")


def convert_feed_entry(app: dict, pool, old_pools, rewrite_paths, normalize_ids, puid, pgid, compact):
    warnings = []

    name = app.get("Name") or ""
    if not name:
        raise ValueError("feed entry has no Name")

    repo = app.get("Repository") or ""
    if not repo:
        warnings.append("no Repository in feed entry - 'repo' will be empty, fill it in manually")

    registry = app.get("Registry") or None
    network = app.get("Network") or "bridge"
    privileged = str(app.get("Privileged", "false")).strip().lower() == "true"
    shell = app.get("Shell") or "bash"

    extra_raw = app.get("ExtraParams") or None
    extra = core.strip_shell_quotes(extra_raw) if extra_raw else None
    if extra != extra_raw:
        warnings.append(f"extra_parameters: stripped shell-style quotes {extra_raw!r} -> {extra!r}")

    post_raw = app.get("PostArgs") or None
    post = core.strip_shell_quotes(post_raw) if post_raw else None
    if post != post_raw:
        warnings.append(f"post_parameters: stripped shell-style quotes {post_raw!r} -> {post!r}")

    cpu_set = app.get("CPUset") or None
    web_ui = app.get("WebUI") or None
    icon = app.get("Icon") or None
    project = app.get("Project") or None
    overview = core.clean_text(app.get("Overview") or "")

    category_raw = ", ".join(app.get("CategoryList") or [])
    categories, cat_warnings = core.guess_category(category_raw)
    warnings.extend(cat_warnings)

    paths, ports, variables, devices, labels = [], [], [], [], []
    for cfg in app.get("Config") or []:
        attrs, value = config_value(cfg)
        cfg_type = (attrs.get("Type") or "Variable").split(",")[0].strip().lower()
        cname = attrs.get("Name") or attrs.get("Target") or "Unnamed"
        target = attrs.get("Target") or ""
        mode = attrs.get("Mode") or ""
        description = core.clean_text(attrs.get("Description") or "")
        required = str(attrs.get("Required", "false")).strip().lower() == "true"
        mask = str(attrs.get("Mask", "false")).strip().lower() == "true"

        if cfg_type == "path":
            host = value
            if rewrite_paths:
                new_host = core.rewrite_host_path(host, pool, old_pools)
                if new_host != host:
                    warnings.append(f"path '{cname}': rewrote host path {host!r} -> {new_host!r}")
                host = new_host
            paths.append({
                "name": cname, "host": host, "container": target,
                "mode": mode or "rw", "description": description or None, "required": required,
            })
        elif cfg_type == "port":
            protocol = (mode or "tcp").lower()
            ports.append({
                "name": cname, "host": value or target, "container": target,
                "protocol": protocol if protocol in ("tcp", "udp") else "tcp",
                "description": description or None, "required": required, "mask": mask,
            })
        elif cfg_type == "device":
            devices.append({
                "name": cname, "host": value, "container": target,
                "description": description or None, "required": required,
            })
        elif cfg_type == "label":
            labels.append({"name": cname, "value": value, "description": description or None})
        else:
            key = target or cname
            if normalize_ids and key.upper() in core.ID_VAR_TARGETS:
                new_val = pgid if key.upper() in ("PGID", "GID", "GROUP_ID", "GUID") else puid
                if new_val != value:
                    warnings.append(f"variable '{key}': normalized default {value!r} -> {new_val!r}")
                value = new_val
            variables.append({
                "name": cname, "key": key, "value": value,
                "description": description or None, "required": required, "mask": mask,
            })

    if not web_ui and ports:
        web_ui = f"http://[IP]:[PORT:{ports[0]['host']}]"
        warnings.append(f"no WebUI found - built a guess from the first port: {web_ui}")

    template = {
        "name": name,
        "repo": repo,
        "category": categories,
        "registry": registry,
        "network": network,
        "custom_ip": None,
        "default_shell": shell,
        "privileged": privileged,
        "extra_parameters": extra,
        "post_parameters": post,
        "web_ui_url": web_ui,
        "icon": icon,
        "project": project,
        "description": overview or None,
        "paths": paths,
        "ports": ports,
        "variables": variables,
        "devices": devices,
        "labels": labels,
    }
    if cpu_set:
        template["cpu_set"] = cpu_set
    if compact:
        template = core.strip_compact(template)

    return template, warnings


def convert_group(apps, out_dir, seen_filenames, args, old_pools, noname_ok=False):
    """Convert a list of feed entries into out_dir. Returns (ok, failed) counts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, failed = 0, 0
    for idx, app in enumerate(apps, start=1):
        if noname_ok and is_invalid_entry(app):
            app = dict(app)
            app["Name"] = fallback_name(app, idx)
            extra_warning = "feed entry had no Name - used a fallback name derived from Repository/index"
        else:
            extra_warning = None

        label = app.get("Name") or app.get("Repository") or "?"
        print(f"=== {label} ===")
        try:
            template, warnings = convert_feed_entry(
                app,
                pool=args.pool,
                old_pools=old_pools,
                rewrite_paths=not args.no_rewrite_paths,
                normalize_ids=not args.no_normalize_ids,
                puid=args.puid,
                pgid=args.pgid,
                compact=args.compact,
            )
        except Exception as e:
            print(f"[FAIL] {e}")
            failed += 1
            continue

        if extra_warning:
            warnings.insert(0, extra_warning)

        filename = core.slugify_filename(template["name"])
        if filename in seen_filenames:
            suffix = 2
            while f"{filename}-{suffix}" in seen_filenames:
                suffix += 1
            print(f"  ! filename collision, writing as {filename}-{suffix}.json instead")
            filename = f"{filename}-{suffix}"
        seen_filenames.add(filename)

        out_file = out_dir / f"{filename}.json"
        out_file.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
        print(f"[OK] wrote {out_file}")
        for w in warnings:
            print(f"  ! {w}")
        ok += 1

    return ok, failed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--search", nargs="+", metavar="TERM", help="Only convert apps whose name/repository contains any of these terms (case-insensitive)")
    ap.add_argument("--category", metavar="TERM", help='Only convert apps in a CA category, e.g. "MediaServer:" or "Downloaders:" - substring match')
    ap.add_argument("--all", action="store_true", help="Convert every app in the feed - there are 3000+, this is slow and produces a lot of files")
    ap.add_argument("--list-only", action="store_true", help="Just print matches (name, repo, maintainer) - don't convert or write anything")
    ap.add_argument("--limit", type=int, default=None, help="Stop after converting this many matches")
    ap.add_argument("-o", "--output-dir", default=".", help="Directory to write MOS *.json templates into (default: current directory)")
    ap.add_argument("--feed-url", default=DEFAULT_FEED_URL, help=f"URL of the CA application feed JSON (default: {DEFAULT_FEED_URL}, falls back to {FALLBACK_FEED_URL} automatically if that fails)")
    ap.add_argument("--pool", default=core.DEFAULT_POOL, help=f'Pool name to rewrite host paths to, or "" to leave paths untouched (default: {core.DEFAULT_POOL})')
    ap.add_argument("--old-pool", action="append", default=None, help=f"Old pool name to rewrite to --pool, repeatable (default: {', '.join(core.DEFAULT_OLD_POOLS) or 'none'})")
    ap.add_argument("--no-rewrite-paths", action="store_true", help="Leave host paths exactly as they are in the feed")
    ap.add_argument("--no-normalize-ids", action="store_true", help="Don't force PUID/PGID/GUID variable defaults")
    ap.add_argument("--puid", default=core.DEFAULT_PUID, help=f"Value to normalize PUID/UID variables to (default: {core.DEFAULT_PUID})")
    ap.add_argument("--pgid", default=core.DEFAULT_PGID, help=f"Value to normalize PGID/GID variables to (default: {core.DEFAULT_PGID})")
    ap.add_argument("--compact", action="store_true", help="Omit empty/unset optional fields entirely instead of writing them as null")
    ap.add_argument("--include-plugins", action="store_true", help="Also convert Unraid plugin entries (category 'Plugins') - these aren't docker containers, so the result is incomplete/nonsensical; written to <output-dir>/plugins/ instead of the main output dir")
    ap.add_argument("--include-invalid", action="store_true", help="Also attempt to convert feed entries with no Name, using a fallback name derived from Repository/index; written to <output-dir>/invalid/ instead of the main output dir")
    args = ap.parse_args()

    if not args.search and not args.category and not args.all:
        ap.error("nothing to convert - pass --search TERM, --category TERM, or --all")

    old_pools = args.old_pool if args.old_pool else core.DEFAULT_OLD_POOLS

    print(f"Fetching {args.feed_url} ...", file=sys.stderr)
    feed = fetch_feed(args.feed_url, fallback_url=FALLBACK_FEED_URL if args.feed_url == DEFAULT_FEED_URL else None)
    applist = feed.get("applist") or []
    print(f"Feed has {len(applist)} apps (last updated: {feed.get('last_updated', '?')})", file=sys.stderr)

    matched = []
    for app in applist:
        if args.search and not matches_search(app, args.search):
            continue
        if args.category and not matches_category(app, args.category):
            continue
        matched.append(app)
    if args.limit:
        matched = matched[: args.limit]

    normal, plugins, invalid = [], [], []
    for app in matched:
        if is_invalid_entry(app):
            invalid.append(app)
        elif is_plugin_entry(app):
            plugins.append(app)
        else:
            normal.append(app)

    print(
        f"{len(matched)} apps matched: {len(normal)} docker templates, "
        f"{len(plugins)} plugins (skipped by default, use --include-plugins), "
        f"{len(invalid)} entries with no Name (skipped by default, use --include-invalid)\n",
        file=sys.stderr,
    )

    if args.list_only:
        for app in normal:
            print(f"{app.get('Name', '?'):30}  {app.get('Repository', '?'):45}")
        for app in plugins:
            print(f"{app.get('Name', '?'):30}  {app.get('Repository', '?'):45}  [plugin]")
        for app in invalid:
            print(f"{'(no name)':30}  {app.get('Repository', '?'):45}  [invalid]")
        return

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    seen_filenames = set()

    ok, failed = convert_group(normal, out_dir, seen_filenames, args, old_pools)

    plugin_ok = plugin_failed = 0
    if args.include_plugins:
        plugin_ok, plugin_failed = convert_group(plugins, out_dir / "plugins", seen_filenames, args, old_pools)
    else:
        print(f"\nSkipped {len(plugins)} plugin entries (pass --include-plugins to convert them into {out_dir / 'plugins'})")

    invalid_ok = invalid_failed = 0
    if args.include_invalid:
        invalid_ok, invalid_failed = convert_group(invalid, out_dir / "invalid", seen_filenames, args, old_pools, noname_ok=True)
    else:
        print(f"Skipped {len(invalid)} entries with no Name (pass --include-invalid to convert them into {out_dir / 'invalid'})")

    total_ok = ok + plugin_ok + invalid_ok
    total_failed = failed + plugin_failed + invalid_failed
    print(f"\nDone: {total_ok} converted, {total_failed} failed.")


if __name__ == "__main__":
    main()
