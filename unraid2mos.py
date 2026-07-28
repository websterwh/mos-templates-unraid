#!/usr/bin/env python3
"""
unraid2mos.py - Convert Unraid Community Applications docker templates (XML)
into MOS Hub docker templates (JSON).

Usage:
    python3 unraid2mos.py -i MyApp.xml                  # writes ./MyApp.json
    python3 unraid2mos.py -i ./unraid-templates/ -o out/ # convert a whole folder
    python3 unraid2mos.py -u https://raw.githubusercontent.com/.../MyApp.xml

Run with --help for all options. See README.md for details on what gets
converted and why.
"""

import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURATION - edit these to your own setup so you don't have to pass
# flags every time. Everything here can still be overridden on the command
# line (--pool, --old-pool, --puid, --pgid).
# ---------------------------------------------------------------------------

# The MOS pool name host paths should be rewritten to. "cache" is MOS's own
# documented standard (see docs.mos-official.net/docs/MOS-Hub/Creating-Your-
# Own-MOS-Hub-Repository) - it's what MOS Hub templates are expected to use
# so they behave consistently across different MOS installations, regardless
# of what any given user actually named their pool. Set this to "" (empty
# string) to leave host paths exactly as they were in the source Unraid
# template instead - nothing gets rewritten.
DEFAULT_POOL = "cache"

# Old pool name(s) to rewrite to DEFAULT_POOL, on top of the standard Unraid
# names (user, user0, cache, diskN) which are always recognized. Useful if
# your old box used a custom pool name. Leave empty ([]) if not needed.
DEFAULT_OLD_POOLS = []

# PUID/PGID to normalize container variables to.
DEFAULT_PUID = "500"
DEFAULT_PGID = "500"

# ---------------------------------------------------------------------------

MOS_CATEGORIES = [
    "AI", "Backup", "Crypto", "Downloader", "Driver", "Game Server",
    "Home Automation", "Hosting", "Media", "Monitoring", "Network",
    "Productivity", "Security", "System", "Utilities", "Misc",
]

# Keyword -> MOS category mapping, checked in order (first match wins).
CATEGORY_KEYWORDS = [
    ("Home Automation", ["homeautomation", "home automation", "smarthome", "home assistant", "domoticz", "zigbee", "mqtt"]),
    ("Game Server", ["gameserver", "game server", "steamcmd", "minecraft"]),
    ("Downloader", ["download", "usenet", "torrent", "nzb"]),
    ("Media", ["mediaapp", "media", "plex", "jellyfin", "video", "music", "photo", "movie", "audiobook"]),
    ("Backup", ["backup", "archive"]),
    ("Driver", ["driver", "nvidia", "coral", " gpu"]),
    ("AI", ["machine learning", "artificial intelligence", " llm", "large language model"]),
    ("Crypto", ["crypto", "blockchain", "mining", "bitcoin"]),
    ("Network", ["network", "vpn", "proxy", "dns", "firewall", "tunnel"]),
    ("Security", ["security", "auth", "sso", "identity provider", "intrusion"]),
    ("Monitoring", ["monitor", "statistics", "dashboard", "uptime", "analytics", "log viewer"]),
    ("Productivity", ["productivity", "office", "recipe", "note"]),
    ("Hosting", ["hosting", "webserver", "reverse proxy", "cms", "cloud storage", "file sync"]),
    ("System", ["system:", "utilit", "management", "container management"]),
]

# Categories that commonly pair with a Network primary match (e.g.
# Zigbee2MQTT is tagged both Network and Home Automation on real templates).
SECONDARY_CATEGORY_RULES = [
    ("Network", ["home assistant", "zigbee", "mqtt", "smarthome"], "Home Automation"),
]

ID_VAR_TARGETS = {"PUID", "PGID", "GUID", "GID", "UID", "USER_ID", "GROUP_ID"}

BBCODE_TAG_RE = re.compile(r"\[/?(?:b|u|i|span|br|url|img)[^\]]*\]", re.IGNORECASE)


def guess_category(raw: str) -> tuple[list, list]:
    """Return (mos_categories, warnings). Always double check the result -
    Unraid's Category field is free text and inconsistent across authors."""
    if not raw:
        return ["Misc"], ["no <Category> found, defaulted to Misc"]
    low = f" {raw.lower()} "
    for mos_cat, keywords in CATEGORY_KEYWORDS:
        if any(kw in low for kw in keywords):
            cats = [mos_cat]
            for primary, secondary_keywords, secondary_cat in SECONDARY_CATEGORY_RULES:
                if mos_cat == primary and secondary_cat not in cats and any(kw in low for kw in secondary_keywords):
                    cats.append(secondary_cat)
            return cats, [f"guessed category {cats} from Unraid category '{raw.strip()}' - please verify"]
    return ["Misc"], [f"could not map Unraid category '{raw.strip()}' -> defaulted to Misc, please review"]


def strip_shell_quotes(text: str) -> str:
    """Unraid's <ExtraParams>/<PostArgs> are often shell-quoted, e.g.
    --sysctl="net.ipv4.conf.all.src_valid_mark=1". Unraid runs these through a
    shell that strips the quotes; MOS passes the tokens straight to
    `docker create` with no shell, so literal quote characters end up inside
    the value and Docker rejects it. Safe to strip - these values never
    legitimately contain a literal quote character."""
    if not text:
        return text
    return text.replace('"', "").replace("'", "")


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = BBCODE_TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def text_of(el, tag, default=""):
    child = el.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def bool_of(el, tag, default=False):
    val = text_of(el, tag, "").strip().lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no", ""):
        return default
    return default


def rewrite_host_path(path: str, pool: str, old_pools=None) -> str:
    """Rewrite the pool-name segment of a host path to /mnt/<pool>/... -
    everything after it is left untouched. An empty pool means "don't touch
    paths at all" - the source path is returned as-is."""
    if not path or not pool:
        return path
    names = ["user0?", "cache", r"disk\d+"]
    for p in old_pools or []:
        if p:
            names.append(re.escape(p))
    pattern = r"^/mnt/(" + "|".join(names) + r")/"
    return re.sub(pattern, f"/mnt/{pool}/", path)


def convert_config_entries(root, pool, old_pools, rewrite_paths, normalize_ids, puid, pgid, warnings):
    paths, ports, variables, devices, labels = [], [], [], [], []

    for cfg in root.findall("Config"):
        cfg_type = (cfg.get("Type") or "Variable").split(",")[0].strip().lower()
        name = cfg.get("Name") or cfg.get("Target") or "Unnamed"
        target = cfg.get("Target") or ""
        mode = cfg.get("Mode") or ""
        description = clean_text(cfg.get("Description") or "")
        required = (cfg.get("Required") or "false").strip().lower() == "true"
        mask = (cfg.get("Mask") or "false").strip().lower() == "true"
        default_attr = cfg.get("Default") or ""
        value = (cfg.text or "").strip() or default_attr

        if cfg_type == "path":
            host = value or default_attr
            if rewrite_paths:
                new_host = rewrite_host_path(host, pool, old_pools)
                if new_host != host:
                    warnings.append(f"path '{name}': rewrote host path {host!r} -> {new_host!r}")
                host = new_host
            paths.append({
                "name": name,
                "host": host,
                "container": target,
                "mode": mode or "rw",
                "description": description or None,
                "required": required,
            })

        elif cfg_type == "port":
            protocol = (mode or "tcp").lower()
            ports.append({
                "name": name,
                "host": value or target,
                "container": target,
                "protocol": protocol if protocol in ("tcp", "udp") else "tcp",
                "description": description or None,
                "required": required,
                "mask": mask,
            })

        elif cfg_type == "device":
            devices.append({
                "name": name,
                "host": value,
                "container": target,
                "description": description or None,
                "required": required,
            })

        elif cfg_type == "label":
            labels.append({
                "name": name,
                "value": value,
                "description": description or None,
            })

        else:  # "variable" and anything unrecognized
            key = target or name
            if normalize_ids and key.upper() in ID_VAR_TARGETS:
                new_val = pgid if key.upper() in ("PGID", "GID", "GROUP_ID", "GUID") else puid
                if new_val != value:
                    warnings.append(f"variable '{key}': normalized default {value!r} -> {new_val!r}")
                value = new_val
            variables.append({
                "name": name,
                "key": key,
                "value": value,
                "description": description or None,
                "required": required,
                "mask": mask,
            })

    return paths, ports, variables, devices, labels


def strip_compact(value):
    """Recursively drop keys whose value is None (used for --compact)."""
    if isinstance(value, dict):
        return {k: strip_compact(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_compact(v) for v in value]
    return value


def convert_template(xml_text: str, pool: str, old_pools: list, rewrite_paths: bool,
                      normalize_ids: bool, puid: str, pgid: str, compact: bool):
    warnings = []
    root = ET.fromstring(xml_text)
    if root.tag != "Container":
        raise ValueError(f"Not an Unraid container template (root tag is <{root.tag}>, expected <Container>)")

    name = text_of(root, "Name")
    if not name:
        raise ValueError("Template has no <Name> - cannot convert")

    repo = text_of(root, "Repository")
    if not repo:
        warnings.append("no <Repository> found - 'repo' will be empty, fill it in manually")

    registry = text_of(root, "Registry") or None
    network = text_of(root, "Network", "bridge") or "bridge"
    privileged = bool_of(root, "Privileged", False)
    shell = text_of(root, "Shell", "bash") or "bash"

    extra_params_raw = text_of(root, "ExtraParams") or None
    extra_params = strip_shell_quotes(extra_params_raw) if extra_params_raw else None
    if extra_params != extra_params_raw:
        warnings.append(f"extra_parameters: stripped shell-style quotes {extra_params_raw!r} -> {extra_params!r}")

    post_args_raw = text_of(root, "PostArgs") or None
    post_args = strip_shell_quotes(post_args_raw) if post_args_raw else None
    if post_args != post_args_raw:
        warnings.append(f"post_parameters: stripped shell-style quotes {post_args_raw!r} -> {post_args!r}")

    cpu_set = text_of(root, "CPUset") or None  # only included below if actually set
    web_ui = text_of(root, "WebUI") or None
    icon = text_of(root, "Icon") or None
    project = text_of(root, "Project") or None
    overview = clean_text(text_of(root, "Overview")) or clean_text(text_of(root, "Description"))
    category_raw = text_of(root, "Category")

    categories, cat_warnings = guess_category(category_raw)
    warnings.extend(cat_warnings)

    paths, ports, variables, devices, labels = convert_config_entries(
        root, pool, old_pools, rewrite_paths, normalize_ids, puid, pgid, warnings
    )

    if not web_ui and ports:
        first_port = ports[0]
        web_ui = f"http://[IP]:[PORT:{first_port['host']}]"
        warnings.append(f"no <WebUI> found - built a guess from the first port: {web_ui}")

    template = {
        "name": name,
        "repo": repo,
        "category": categories,
        "registry": registry,
        "network": network,
        "custom_ip": None,
        "default_shell": shell,
        "privileged": privileged,
        "extra_parameters": extra_params,
        "post_parameters": post_args,
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
        template = strip_compact(template)

    return template, warnings


def slugify_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", name)
    return cleaned or "Template"


def load_xml_source(path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        with urllib.request.urlopen(path_or_url, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    return Path(path_or_url).read_text(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(
        description="Convert Unraid Community Applications docker templates (XML) to MOS Hub docker templates (JSON).",
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("-i", "--input", help="Path to a single .xml file or a directory of .xml files")
    src.add_argument("-u", "--url", help="URL to a single Unraid template XML file")
    ap.add_argument("-o", "--output-dir", default=".", help="Directory to write MOS *.json templates into (default: current directory)")
    ap.add_argument("--pool", default=DEFAULT_POOL, help=f"Pool name to use when rewriting host paths, or \"\" to leave paths untouched (default: {DEFAULT_POOL})")
    ap.add_argument("--old-pool", action="append", default=None,
                     help=f"Old pool name to rewrite to --pool, repeatable (default: {', '.join(DEFAULT_OLD_POOLS) or 'none'})")
    ap.add_argument("--no-rewrite-paths", action="store_true", help="Leave host paths exactly as they were in the Unraid template")
    ap.add_argument("--no-normalize-ids", action="store_true", help="Don't force PUID/PGID/GUID variable defaults")
    ap.add_argument("--puid", default=DEFAULT_PUID, help=f"Value to normalize PUID/UID variables to (default: {DEFAULT_PUID})")
    ap.add_argument("--pgid", default=DEFAULT_PGID, help=f"Value to normalize PGID/GID variables to (default: {DEFAULT_PGID})")
    ap.add_argument("--compact", action="store_true", help="Omit empty/unset optional fields entirely instead of writing them as null")
    args = ap.parse_args()
    old_pools = args.old_pool if args.old_pool else DEFAULT_OLD_POOLS

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sources = []  # list of (label, xml_text)
    if args.url:
        try:
            sources.append((args.url, load_xml_source(args.url)))
        except Exception as e:
            print(f"[ERROR] could not fetch {args.url}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        in_path = Path(args.input)
        if in_path.is_dir():
            xml_files = sorted(in_path.glob("*.xml"))
            if not xml_files:
                print(f"[ERROR] no .xml files found in {in_path}", file=sys.stderr)
                sys.exit(1)
            for f in xml_files:
                sources.append((str(f), f.read_text(encoding="utf-8", errors="replace")))
        elif in_path.is_file():
            sources.append((str(in_path), in_path.read_text(encoding="utf-8", errors="replace")))
        else:
            print(f"[ERROR] {in_path} not found", file=sys.stderr)
            sys.exit(1)

    ok, failed = 0, 0
    for label, xml_text in sources:
        print(f"\n=== {label} ===")
        try:
            template, warnings = convert_template(
                xml_text,
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

        out_file = out_dir / f"{slugify_filename(template['name'])}.json"
        out_file.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
        print(f"[OK] wrote {out_file}")
        for w in warnings:
            print(f"  ! {w}")
        if template["category"] and template["category"][0] not in MOS_CATEGORIES:
            print(f"  ! category '{template['category'][0]}' is not in MOS's known category list, double check it")
        ok += 1

    print(f"\nDone: {ok} converted, {failed} failed.")


if __name__ == "__main__":
    main()
