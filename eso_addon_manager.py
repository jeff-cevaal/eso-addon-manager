#!/usr/bin/env python3
"""
ESO Addon Manager
Install, scan, match and update Elder Scrolls Online addons from ESOUI.com.

Uses the public MMOUI API that ESOUI is part of:
    https://api.mmoui.com/v3/game/ESO/filelist.json         (every addon)
    https://api.mmoui.com/v3/game/ESO/filedetails/<id>.json (one addon + download URL)

The file list includes "UIDir" (the folders each addon installs), which lets
`scan` match your AddOns folders to ESOUI addons automatically.

Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import difflib
import html as htmllib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import types
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

# ── Configuration ────────────────────────────────────────────────────────────

API_BASE         = "https://api.mmoui.com/v3/game/ESO"
SITE_BASE        = "https://www.esoui.com"
def default_app_dir() -> Path:
    """Where this tool keeps its own small files, following each OS's convention."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "eso_addon_manager"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "eso_addon_manager"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "eso_addon_manager"

# APP_DIR holds filelist.json (a cache) and location.json (a pointer to the game folder).
# The real config.json lives in the Elder Scrolls Online folder, next to live/ and pts/, so a
# backup of that folder keeps it. CONFIG_FILE below is only used until a game folder exists.
APP_DIR          = default_app_dir()
CONFIG_FILE      = APP_DIR / "config.json"
FILELIST_CACHE   = APP_DIR / "filelist.json"
FILELIST_MAX_AGE = 3600          # seconds before the cached list is refreshed
HEADERS          = {"User-Agent": "ESOAddonManager/1.0"}
CONFIG_VERSION   = 3
ENV_NAMES        = ("live", "pts")                # "live" is used for both the NA and EU servers

def default_root() -> Path:
    """Default 'Elder Scrolls Online' folder (the one that contains live/, pts/ …)."""
    if sys.platform in ("win32", "darwin"):
        return Path.home() / "Documents" / "Elder Scrolls Online"
    return (Path.home() / ".steam" / "steam" / "steamapps" / "compatdata" / "306130"
            / "pfx" / "drive_c" / "users" / "steamuser" / "Documents" / "Elder Scrolls Online")

# ── Terminal helpers ─────────────────────────────────────────────────────────

if os.name == "nt":
    os.system("")                # enables ANSI colours in Windows terminals

_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_CODES = {"reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
          "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
          "cyan": "\033[96m", "gold": "\033[38;5;220m"}

def c(color: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"{_CODES[color]}{text}{_CODES['reset']}"

def pad(text: str, width: int) -> str:
    """Pad/truncate PLAIN text to a fixed width (colour it afterwards)."""
    return text[: width - 1] + "…" if len(text) > width else text.ljust(width)

def sep() -> None:
    print(c("dim", "  " + "─" * 100))

def banner() -> str:
    inner = " ESO Addon Manager  · powered by ESOUI "
    width = len(inner)
    return (f"\n{c('gold', '  ╔' + '═' * width + '╗')}\n"
            f"{c('gold', '  ║')}{inner}{c('gold', '║')}\n"
            f"{c('gold', '  ╚' + '═' * width + '╝')}\n")

def fmt_date(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "?"

# ── Config ───────────────────────────────────────────────────────────────────
#
# config.json layout (v3):
#   root, saved_vars ("ask"|"always"|"never"), default_env,
#   envs: { "live": {installed, manual_links}, "pts": {…} }
# Commands receive a per-environment "view" (build_cfg) so each of live / pts
# keeps its own tracked addons.

def location_file() -> Path:
    return APP_DIR / "location.json"

def read_location() -> str | None:
    try:
        return json.loads(location_file().read_text(encoding="utf-8")).get("root") or None
    except (OSError, ValueError, AttributeError):
        return None

def config_path_for(store: dict) -> Path:
    """config.json sits in the game folder once that exists; before that, in the app folder."""
    root = Path(store.get("root") or "")
    return root / "config.json" if store.get("root") and root.is_dir() else CONFIG_FILE

def active_config_path() -> Path:
    loc = read_location()
    if loc and (Path(loc) / "config.json").is_file():
        return Path(loc) / "config.json"
    return CONFIG_FILE

def have_config() -> bool:
    return location_file().is_file() or CONFIG_FILE.is_file()

def write_store(store: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    path = config_path_for(store)
    text = json.dumps(store, indent=2)
    def atomic(target: Path) -> None:                     # never leave a half-written config behind
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    try:
        atomic(path)
    except OSError as e:                                  # e.g. read-only game folder
        print(c("yellow", f"  Couldn't write {path} ({e}); keeping the config in {APP_DIR}."))
        path = CONFIG_FILE
        atomic(path)
    if store.get("root"):
        location_file().write_text(json.dumps({"root": store["root"]}, indent=2), encoding="utf-8")
    if path != CONFIG_FILE and CONFIG_FILE.is_file():     # the starter config has moved into the game folder
        try:
            CONFIG_FILE.replace(CONFIG_FILE.with_name("config.moved.json.bak"))
        except OSError:
            pass

def read_store() -> dict:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    loc = read_location()
    source = None
    for cand in ((Path(loc) / "config.json") if loc else None, CONFIG_FILE):
        if cand is not None and cand.is_file():
            source = cand
            break
    old = {}
    if source:
        try:
            old = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(c("yellow", f"  Config file {source} unreadable — starting fresh."))
            source = None
    if old.get("config_version") == CONFIG_VERSION and "envs" in old:
        store = old
    else:
        if old:
            print(c("yellow", f"  Config file {source} is not in a format this version understands — starting fresh."))
        store, source = {"config_version": CONFIG_VERSION}, None
    if source and source.name == "config.json" and source.parent != CONFIG_FILE.parent:
        store["root"] = str(source.parent)                # the file's own folder wins (restored backups)
    elif loc and not store.get("root"):
        store["root"] = loc
    store.setdefault("root", str(default_root()))
    store.setdefault("saved_vars", "ask")
    store.setdefault("default_env", "live")
    store.setdefault("envs", {})
    if source and source != config_path_for(store):
        write_store(store)                                # starter config -> game folder
    return store

def build_cfg(store: dict, env: str) -> dict:
    """The working config for one environment (live / pts)."""
    root = Path(store["root"])
    data = store["envs"].setdefault(env, {})
    return {"_store": store, "env": env, "root": str(root),
            "addon_dir": str(root / env / "AddOns"),
            "sv_dir": str(root / env / "SavedVariables"),
            "installed": data.setdefault("installed", {}),
            "manual_links": data.setdefault("manual_links", {}),
            "saved_vars": store.get("saved_vars", "ask")}

def load_config(env: str | None = None) -> dict:
    store = read_store()
    env = (env or store.get("default_env") or "live").lower()
    return build_cfg(store, env)

def save_config(cfg: dict) -> None:
    store = cfg.setdefault("_store", {"config_version": CONFIG_VERSION, "envs": {}})
    store["config_version"] = CONFIG_VERSION
    store.setdefault("envs", {})[cfg.get("env", "live")] = {
        "installed": cfg["installed"], "manual_links": cfg["manual_links"]}
    write_store(store)

def available_envs(root: Path) -> list:
    """Environment folders that actually exist under the ESO folder."""
    try:
        return sorted(d.name for d in root.iterdir()
                      if d.is_dir() and ((d / "AddOns").is_dir() or (d / "SavedVariables").is_dir()))
    except OSError:
        return []

def normalize_root(p: Path):
    """Accept the ESO folder, an environment folder, or an AddOns/SavedVariables folder."""
    name = p.name.lower()
    if name in ("addons", "savedvariables"):
        return p.parent.parent, p.parent.name.lower()
    if name in ENV_NAMES and ((p / "AddOns").is_dir() or (p / "SavedVariables").is_dir()):
        return p.parent, name
    return p, None

# ── Network / API ────────────────────────────────────────────────────────────

FAILURES: list = []          # things that went wrong this run; any entry makes the exit code 1

def note_failure(what: str) -> None:
    FAILURES.append(what)

MAX_DOWNLOAD     = 300 * 1024 * 1024      # refuse bigger downloads
MAX_UNPACKED     = 1024 * 1024 * 1024     # refuse zips that unpack to more than this
MAX_ZIP_ENTRIES  = 20000
TRUSTED_HOSTS    = ("esoui.com", "mmoui.com", "tamrieltradecentre.com")

def host_trusted(url: str) -> bool:
    """Only https URLs on the sites this tool is meant to talk to."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS)

class _SafeRedirect(HTTPRedirectHandler):
    """Follow redirects only to trusted https hosts (no http://, file://, or unrelated sites)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_trusted(newurl):
            raise URLError(f"refusing redirect to untrusted address: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_OPENER = build_opener(_SafeRedirect)          # default build_opener verifies TLS certificates

def http_get(url: str, timeout: int = 30) -> bytes:
    if not host_trusted(url):
        raise URLError(f"refusing to fetch untrusted address: {url}")
    with _OPENER.open(Request(url, headers=HEADERS), timeout=timeout) as r:
        data = r.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("download is larger than the safety limit")
    return data

def http_json(url: str):
    try:
        return json.loads(http_get(url).decode("utf-8"))
    except HTTPError as e:
        note_failure(f"HTTP {e.code} for {url}")
        print(c("red", f"  Network error: HTTP {e.code} for {url}"))
    except (URLError, TimeoutError) as e:
        note_failure(f"network error: {e}")
        print(c("red", f"  Network error: {e}"))
    except json.JSONDecodeError:
        print(c("red", f"  Unexpected (non-JSON) response from {url}"))
    return None

def _read_cache():
    try:
        data = json.loads(FILELIST_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) and data else None
    except (OSError, json.JSONDecodeError):
        return None

def get_filelist(force: bool = False):
    """Return the full ESOUI addon list (cached for an hour)."""
    if not force and FILELIST_CACHE.exists():
        if time.time() - FILELIST_CACHE.stat().st_mtime < FILELIST_MAX_AGE:
            cached = _read_cache()
            if cached:
                return cached
    print(c("dim", "  Downloading addon list from ESOUI …"), end="", flush=True)
    data = http_json(f"{API_BASE}/filelist.json")
    if isinstance(data, list) and data:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        FILELIST_CACHE.write_text(json.dumps(data), encoding="utf-8")
        print(c("green", f" {len(data)} addons"))
        return data
    cached = _read_cache()
    if cached:
        print(c("yellow", "  Using the last cached addon list instead."))
        return cached
    note_failure("could not get the addon list")
    print(c("red", "  Could not get the addon list and no cache exists."))
    return None

def fetch_details(uid: str):
    """One addon's full details (includes the download URL)."""
    data = http_json(f"{API_BASE}/filedetails/{uid}.json")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        if "ERROR" in data:
            note_failure(f"ESOUI says: {data['ERROR']} (id {uid})")
            print(c("red", f"  ESOUI says: {data['ERROR']} (id {uid})"))
            return None
        return data
    return None

# ── Manifest parsing ─────────────────────────────────────────────────────────

def strip_codes(text: str) -> str:
    """Remove ESO |c…|r colour / |t texture / |H link markup from titles."""
    return re.sub(
        r"\|c[0-9A-Fa-f]{6}|\|r|\|t[^|]*\|t?|\|H[^|]*\|h[^|]*\|h|\|[a-zA-Z]",
        "", text).strip().rstrip("|").strip()

def split_camel(name: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s).strip()

def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())

def author_match(a: str, b: str) -> bool:
    """True if two author fields share a whole author name (handles 'A, B & C')."""
    def names(x):
        parts = re.split(r",|&|;|/|\band\b|\+", (x or "").lower())
        return {norm(p) for p in parts if norm(p)}
    na, nb = names(a), names(b)
    if na & nb:
        return True
    # tolerate decorations like "Solinur (Dev)" but only for reasonably long names
    return any(len(x) >= 5 and len(y) >= 5 and (x in y or y in x) for x in na for y in nb)

def is_lib(folder: str) -> bool:
    return folder.lower().startswith("lib")

def parse_manifest(path: Path) -> dict:
    meta = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("##") or ":" not in line:
                    continue
                key, _, value = line[2:].partition(":")
                meta[key.strip()] = value.strip()
    except OSError:
        pass
    return meta

def manifest_path(folder: Path):
    """The addon manifest (.txt / .addon) inside a folder, or None."""
    for ext in (".txt", ".addon"):
        if (folder / f"{folder.name}{ext}").exists():
            return folder / f"{folder.name}{ext}"
    return next((p for p in sorted(folder.glob("*.txt"))
                 if "Title" in parse_manifest(p)), None)

def find_manifests(addon_dir: Path) -> dict:
    """{folder_name: (manifest_path, meta)} for every addon folder."""
    found = {}
    for folder in sorted(p for p in addon_dir.iterdir() if p.is_dir()):
        manifest = manifest_path(folder)
        if manifest:
            found[folder.name] = (manifest, parse_manifest(manifest))
    return found

def manifest_sig(paths: list) -> str:
    """Fingerprint of the manifests on disk — changes when files are swapped for another copy."""
    h = hashlib.sha1()
    for p in sorted(paths, key=lambda x: str(x)):
        h.update(p.parent.name.encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
    return h.hexdigest()

def vkey(text: str) -> tuple:
    """'2.0 r43' -> (2, 0, 43) so versions can be ordered."""
    return tuple(int(x) for x in re.findall(r"\d+", text or ""))

def manifest_versions(metas: list) -> dict:
    vs = [m.get("Version", "") for m in metas if m.get("Version")]
    avs = [m.get("AddOnVersion", "") for m in metas if m.get("AddOnVersion")]
    return {"v": max(vs, key=vkey, default=""), "av": max(avs, key=vkey, default="")}

def downgraded(old: dict, new: dict) -> bool:
    """True if the manifest on disk now has a LOWER version than we last recorded."""
    for k in ("av", "v"):
        a, b = vkey(old.get(k, "")), vkey(new.get(k, ""))
        if a and b and b < a:
            return True
    return False

# ── Matching local folders to ESOUI addons ───────────────────────────────────

def auto_match(folders: set, filelist: list) -> dict:
    """
    Match folders to ESOUI addons using each addon's UIDir list.
    Returns {uid: set(folders)}.
    Lib* folders are matched on their own and never merged into a bigger addon.
    """
    idx = {}
    for addon in filelist:
        for d in addon.get("UIDir") or []:
            idx.setdefault(d.lower(), []).append(addon)

    result, claimed = {}, set()

    # Libraries: pick the most specific (fewest folders) addon listing the folder
    for f in sorted(folders):
        if is_lib(f) and idx.get(f.lower()):
            best = min(idx[f.lower()],
                       key=lambda a: (len(a.get("UIDir") or []), int(a["UID"])))
            result.setdefault(best["UID"], set()).add(f)
            claimed.add(f)

    # Everything else: greedily take the addon that explains the most folders
    nonlib = {f for f in folders if not is_lib(f)}
    while True:
        scores = {}
        for f in nonlib - claimed:
            for addon in idx.get(f.lower(), []):
                scores.setdefault(addon["UID"], (addon, set()))[1].add(f)
        if not scores:
            break

        def rank(item):
            addon, fs = item
            dirs = [d for d in addon.get("UIDir") or [] if not is_lib(d)]
            return (len(fs), len(fs) / max(len(dirs), 1), -int(addon["UID"]))

        uid, (addon, fs) = max(scores.items(), key=lambda kv: rank(kv[1]))
        result.setdefault(uid, set()).update(fs)
        claimed |= fs
    return result

def group_leftovers(folders: set) -> list:
    """Group unmatched folders by shared name prefix (never Lib*)."""
    names = sorted(folders, key=lambda n: (len(n), n.lower()))
    seen, groups = set(), []
    for p in names:
        if p in seen:
            continue
        seen.add(p)
        group = [p]
        if not is_lib(p):
            for q in names:
                if (q not in seen and not is_lib(q) and len(q) > len(p)
                        and q.lower().startswith(p.lower())):
                    group.append(q)
                    seen.add(q)
        groups.append(group)
    return groups

def looks_current(addon: dict, folders: list, info: dict) -> bool:
    """Best guess whether the files on disk are the addon's latest release."""
    remote = norm(addon.get("UIVersion"))
    for f in folders:
        if remote and remote in (norm(info[f][1].get("Version")),
                                 norm(info[f][1].get("AddOnVersion"))):
            return True
    newest = max(info[f][0].stat().st_mtime for f in folders)
    return newest * 1000 >= int(addon.get("UIDate") or 0) - 86_400_000

def manifest_match(folder: str, meta: dict, filelist: list):
    """
    Fallback for folders whose name isn't in any addon's UIDir: compare the
    manifest's Title / Author / Version with ESOUI's name / author / version.
    Returns an ESOUI UID or None (None whenever the answer is ambiguous).
    """
    names = {n for n in (norm(strip_codes(meta.get("Title", ""))), norm(folder)) if n}
    author = meta.get("Author", "")
    version = norm(meta.get("Version", ""))
    if not names:
        return None

    found = []                                   # (addon, name_score, author_ok, version_ok)
    for addon in filelist:
        n = norm(addon["UIName"])
        if not n:
            continue
        if n in names:
            score = 1.0
        else:
            score = 0.0
            for x in names:
                sm = difflib.SequenceMatcher(None, n, x, autojunk=False)
                if sm.real_quick_ratio() >= 0.85 and sm.quick_ratio() >= 0.85:
                    score = max(score, sm.ratio())
        if score < 0.85:
            continue
        author_ok = author_match(author, addon.get("UIAuthorName", ""))
        version_ok = bool(version) and norm(addon.get("UIVersion")) == version
        # exact name, or a close name backed up by the same author
        if score == 1.0 or author_ok:
            found.append((addon, score, author_ok, version_ok))
    if not found:
        return None

    found.sort(key=lambda t: (not t[2], not t[3], -t[1], -int(t[0].get("UIDownloadTotal") or 0)))
    best = found[0]
    if not best[2]:                              # no author confirmation -> must be unambiguous
        if len([t for t in found if t[1] == 1.0]) != 1:
            return None
    return best[0]["UID"]

def candidate_matches(name: str, folders: list, filelist: list, limit: int = 5,
                      author: str = ""):
    """Fuzzy-rank ESOUI addons against a local name/folder names."""
    keys = {norm(name)} | {norm(f) for f in folders} | {norm(split_camel(f)) for f in folders}
    keys.discard("")
    matchers = []
    for k in keys:
        sm = difflib.SequenceMatcher(None, autojunk=False)
        sm.set_seq2(k)
        matchers.append((k, sm))
    scored = []
    for addon in filelist:
        names = {norm(addon["UIName"])} | {norm(d) for d in addon.get("UIDir") or []}
        names.discard("")
        best = 0.0
        for k, sm in matchers:
            for n in names:
                if k == n:
                    s = 1.0
                elif k in n or n in k:
                    s = 0.1 + 0.85 * min(len(k), len(n)) / max(len(k), len(n))
                else:
                    sm.set_seq1(n)
                    if sm.real_quick_ratio() < 0.5 or sm.quick_ratio() < 0.5:
                        continue
                    s = sm.ratio()
                best = max(best, s)
        if best >= 0.5 and author_match(author, addon.get("UIAuthorName", "")):
            best = min(1.0, best + 0.15)             # same author: strong hint
        if best >= 0.5:
            scored.append((best, addon))
    scored.sort(key=lambda t: (-t[0], -int(t[1].get("UIDownloadTotal") or 0)))
    return scored[:limit]

# ── Scan ─────────────────────────────────────────────────────────────────────

def do_scan(cfg: dict, filelist: list, quiet: bool = False) -> dict:
    """Rebuild the installed list from what is actually on disk."""
    addon_dir = Path(cfg["addon_dir"])
    info = find_manifests(addon_dir)
    by_uid = {a["UID"]: a for a in filelist}

    assignments, leftover = {}, set(info)

    # 1. links you made by hand
    for f in list(leftover):
        uid = cfg["manual_links"].get(f.lower())
        if uid in by_uid:
            assignments.setdefault(uid, set()).add(f)
            leftover.discard(f)

    how = {uid: "manual" for uid in assignments}

    # 2. automatic match via UIDir
    for uid, fs in auto_match(leftover, filelist).items():
        assignments.setdefault(uid, set()).update(fs)
        how.setdefault(uid, "")
        leftover -= fs

    # 3. fallback: match by manifest Title / Author / Version
    by_manifest = {}
    for f in sorted(leftover):
        uid = manifest_match(f, info[f][1], filelist)
        if uid:
            by_manifest.setdefault(uid, set()).add(f)
    for uid, fs in by_manifest.items():
        # unmatched sub-folders ("CombatMetricsFonts") follow their main folder
        for f in sorted(leftover - fs):
            if not is_lib(f) and any(not is_lib(p) and len(f) > len(p) and f.lower().startswith(p.lower())
                                     for p in fs):
                fs = fs | {f}
        assignments.setdefault(uid, set()).update(fs)
        how.setdefault(uid, "manifest")
        leftover -= fs

    old = cfg["installed"]
    new = {}
    rows = []
    for uid, fs in assignments.items():
        addon, folders = by_uid[uid], sorted(fs)
        remote_date = int(addon.get("UIDate") or 0)
        prev = old.get(uid, {})
        metas = [info[f][1] for f in folders]
        sig = manifest_sig([info[f][0] for f in folders])
        mver = manifest_versions(metas)
        remote = norm(addon.get("UIVersion"))
        on_disk_matches = bool(remote) and any(
            remote in (norm(m.get("Version")), norm(m.get("AddOnVersion"))) for m in metas)

        if on_disk_matches:
            current = True                                   # manifest says it IS the latest
        elif prev.get("sig") == sig:
            # files unchanged since we last looked: current only if that was true then
            current = int(prev.get("installed_date") or 0) >= remote_date > 0
        elif prev.get("sig") and downgraded(prev.get("mver", {}), mver):
            current = False                                  # an older copy replaced newer files
        else:
            current = looks_current(addon, folders, info)    # new, or changed some other way
        first_meta = metas[0]
        new[uid] = {
            "name": addon["UIName"],
            "author": addon.get("UIAuthorName", "?"),
            "version": addon.get("UIVersion") if current else
                       (prev.get("version") if prev.get("sig") == sig and prev.get("version")
                        else (first_meta.get("Version") or first_meta.get("AddOnVersion") or "?")),
            "installed_date": remote_date if current else 0,
            "by_tool": bool(prev.get("by_tool")) and current,
            "sig": sig, "mver": mver,
            "folders": folders,
        }
        locked = bool(prev.get("locked"))
        if locked:
            new[uid]["locked"] = True
        rows.append((addon["UIName"], uid, new[uid]["version"], current or locked, len(folders),
                     how.get(uid, ""), addon.get("UIVersion", "?"), locked, current))

    unmatched = []
    for group in group_leftovers(leftover):
        meta = info[group[0]][1]
        title = strip_codes(meta.get("Title", group[0])) or group[0]
        key = f"local:{group[0]}"
        new[key] = {"name": title, "author": meta.get("Author", "?"),
                    "version": meta.get("Version") or meta.get("AddOnVersion") or "?",
                    "installed_date": 0, "by_tool": False, "folders": sorted(group)}
        unmatched.append((title, key, len(group)))

    cfg["installed"] = new
    save_config(cfg)

    if not quiet:
        sep()
        for name, uid, ver, ok, n, via, remote_ver, locked, current in sorted(rows, key=lambda r: r[0].lower()):
            tag = (c("cyan", "🔒 locked" + ("" if current else f" ({remote_ver} available)")) if locked
                   else c("green", "current") if current else c("yellow", f"update → {remote_ver}"))
            extra = c("dim", f"  (+{n - 1} folders)") if n > 1 else ""
            guess = c("yellow", "  ← matched by manifest") if via == "manifest" else ""
            print(f"  {pad(name, 56)} {c('cyan', pad('[' + uid + ']', 7))} "
                  f"{pad(str(ver), 14)} {tag}{extra}{guess}")
        for title, key, n in sorted(unmatched, key=lambda r: r[0].lower()):
            extra = c("dim", f"  (+{n - 1} folders)") if n > 1 else ""
            print(f"  {pad(title, 56)} {c('yellow', 'no match')}{extra}")
        sep()
    return {"matched": len(rows), "unmatched": len(unmatched),
            "by_manifest": sum(1 for r in rows if r[5] == "manifest"),
            "outdated": sum(1 for r in rows if not r[3]), "folders": len(info)}

def cmd_scan(args, cfg: dict) -> None:
    addon_dir = Path(cfg["addon_dir"])
    print(f"\n  Scanning {c('cyan', str(addon_dir))} …")
    if not addon_dir.is_dir():
        print(c("red", f"  Directory not found: {addon_dir}"))
        print(c("dim", "  Set it with:  config --set-dir <path>"))
        return
    filelist = get_filelist(force=args.refresh)
    if not filelist:
        return
    stats = do_scan(cfg, filelist)
    print(f"\n  {stats['folders']} folders → "
          f"{c('green', str(stats['matched']))} matched "
          f"({stats['by_manifest']} by manifest fallback), "
          f"{c('yellow', str(stats['unmatched']))} unmatched, "
          f"{c('yellow', str(stats['outdated']))} possibly outdated\n")
    if stats["unmatched"]:
        print(c("dim", "  Run 'match' to link the unmatched ones.") + "\n")
    if stats["outdated"]:
        print(c("dim", "  Run 'update' to bring outdated addons up to date.") + "\n")

# ── Install / update ─────────────────────────────────────────────────────────

def extract_zip(data: bytes, addon_dir: Path) -> list:
    """Safely extract a zip into addon_dir; return its top-level folders."""
    root = addon_dir.resolve()
    top = set()
    written = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise ValueError("zip is too large or has too many files (safety limit)")
        for m in infos:
            name = m.filename.replace("\\", "/").lstrip("/")
            parts = [p for p in name.split("/") if p]
            if not parts or parts[0] == "__MACOSX":
                continue
            target = (root / name).resolve()
            if not target.is_relative_to(root) or target == root:
                continue                              # zip-slip guard
            if len(parts) > 1 or m.is_dir():
                top.add(parts[0])
            if m.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(target, "wb") as dst:
                    while True:                          # count real bytes, not the sizes the zip claims
                        block = src.read(1024 * 1024)
                        if not block:
                            break
                        written += len(block)
                        if written > MAX_UNPACKED:
                            raise ValueError("zip unpacks to more than the safety limit")
                        dst.write(block)
    return sorted(top)

# ── Older versions (ESOUI "Other Files" / archived files) ────────────────────
# The API only offers the current download. Older versions are listed on the addon's web page
# as a table (file name, version, size, uploader, date) with a getfile.php link per row.

_ROW_RE  = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_LINK_RE = re.compile(r"""href\s*=\s*["']([^"']*getfile\.php\?[^"']*)["']""", re.I)
_AID_RE  = re.compile(r"[?&;]aid=(\d+)")
_ID_RE   = re.compile(r"[?&;]id=(\d+)")
_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}:\d{2})\s*([AP]M)", re.I)

def norm_version(v) -> str:
    v = str(v or "").strip().lower()
    return v[1:] if v[:1] == "v" and v[1:2].isdigit() else v

def parse_archive_page(page: str, uid: str) -> list:
    """Rows of the archived-files table: [{version, name, date(ms), aid, url}], newest first as listed."""
    found, seen = [], set()
    for row in _ROW_RE.findall(page):
        link = _LINK_RE.search(row)
        if not link:
            continue
        href = htmllib.unescape(link.group(1))
        aid, owner = _AID_RE.search(href), _ID_RE.search(href)
        if not aid or aid.group(1) in seen or (owner and owner.group(1) != str(uid)):
            continue
        cells = [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", part))).strip()
                 for part in re.split(r"</t[dh]>", row, flags=re.I)]
        cells = [x for x in cells if x]
        if len(cells) < 2:
            continue
        date_ms = 0
        d = _DATE_RE.search(" ".join(cells))
        if d:
            day = d.group(1)
            for fmt in ("%m/%d/%y", "%m/%d/%Y"):
                try:
                    date_ms = int(datetime.strptime(f"{day} {d.group(2)} {d.group(3).upper()}",
                                                    fmt + " %I:%M %p").timestamp() * 1000)
                    break
                except ValueError:
                    pass
        url = urljoin(SITE_BASE + "/downloads/", href)
        seen.add(aid.group(1))
        found.append({"name": cells[0], "version": cells[1], "date": date_ms,
                      "aid": aid.group(1), "url": url})
    return found

def archive_versions(uid: str) -> list:
    """Older versions of an addon from its ESOUI page ([] if the page can't be read)."""
    try:
        page = http_get(f"{SITE_BASE}/downloads/fileinfo.php?id={uid}", timeout=60).decode("utf-8", "replace")
    except Exception as e:
        note_failure(f"could not read the ESOUI page for addon {uid}: {e}")
        print(c("red", f"  Could not read the ESOUI page for addon {uid}: {e}"))
        return []
    rows = parse_archive_page(page, uid)
    if not rows:
        saved = APP_DIR / "last_esoui_page.html"
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            saved.write_text(page, encoding="utf-8")
            where = f" The page was saved to {saved} in case the layout changed."
        except OSError:
            where = ""
        print(c("yellow", f"  No older versions found on the ESOUI page for addon {uid}.{where}"))
    return rows

def install_addon(cfg: dict, uid: str, summary: dict | None = None,
                  version: str | None = None, lock: bool = False) -> bool:
    details = fetch_details(uid)
    if not details:
        note_failure(f"could not get details for addon {uid}")
        return False
    name = details.get("UIName") or (summary or {}).get("UIName") or f"Addon {uid}"
    latest = details.get("UIVersion") or (summary or {}).get("UIVersion") or "?"
    shown, date_ms, clean = latest, int(details.get("UIDate") or (summary or {}).get("UIDate")
                                        or time.time() * 1000), False
    url = details.get("UIDownload") or f"{SITE_BASE}/downloads/download{uid}"
    if version and norm_version(version) != norm_version(latest):
        rows = archive_versions(uid)
        hit = next((r for r in rows if norm_version(r["version"]) == norm_version(version)), None)
        if not hit:
            note_failure(f"{name}: version {version} not found")
            print(c("red", f"  {name}: version {version} was not found on ESOUI."))
            if rows:
                print(c("dim", "  Available: " + ", ".join(r["version"] for r in rows[:12])
                        + (" …" if len(rows) > 12 else "") + f"   (current: {latest})"))
            return False
        url, shown, clean = hit["url"], hit["version"], True
        date_ms = hit["date"] or 1
    if not host_trusted(url):
        note_failure(f"refused untrusted download address for addon {uid}: {url}")
        print(c("red", f"\n    Refusing to download from an unexpected address: {url}"))
        return False
    print(f"  {c('bold', name)}  {c('yellow', str(shown))}" + (c("dim", "  (older version)") if clean else ""))

    addon_dir = Path(cfg["addon_dir"])
    addon_dir.mkdir(parents=True, exist_ok=True)
    try:
        print(c("dim", "    Downloading …"), end="", flush=True)
        data = http_get(url, timeout=180)
        print(c("green", f" {len(data) // 1024} KB"))
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise ValueError("download was not a zip file")
        if clean:                          # going back a version: don't leave newer files behind
            root = addon_dir.resolve()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                tops = {n.replace("\\", "/").lstrip("/").split("/")[0] for n in z.namelist()}
            for t in sorted(t for t in tops if t and t not in (".", "..", "__MACOSX")):
                target = (root / t).resolve()
                if target.parent == root and target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
        folders = extract_zip(data, addon_dir)
    except Exception as e:
        note_failure(f"{name}: {e}")
        print(c("red", f"\n    Failed: {e}"))
        print(c("dim", f"    You can download it manually: {SITE_BASE}/downloads/info{uid}.html"))
        return False

    old = cfg["installed"].get(uid, {})
    folders = sorted(set(folders) | set(old.get("folders", [])))
    for key in [k for k, v in cfg["installed"].items()
                if k.startswith("local:") and set(v.get("folders", [])) <= set(folders)]:
        del cfg["installed"][key]
    paths = [p for p in (manifest_path(addon_dir / f) for f in folders) if p]
    cfg["installed"][uid] = {
        "name": name, "author": details.get("UIAuthorName", "?"), "version": shown,
        "installed_date": date_ms,
        "by_tool": True, "folders": folders,
        "sig": manifest_sig(paths),
        "mver": manifest_versions([parse_manifest(p) for p in paths]),
    }
    if lock:
        cfg["installed"][uid]["locked"] = True
    for f in folders:
        cfg["manual_links"][f.lower()] = uid
    save_config(cfg)
    print(c("green", f"    ✔ Installed ({len(folders)} folder{'s' if len(folders) != 1 else ''})")
          + (c("cyan", f"  🔒 locked at {shown}") if lock else ""))
    return True

def cmd_install(args, cfg: dict) -> None:
    print()
    if args.version and len(args.ids) != 1:
        print(c("red", "  --version works with exactly one addon ID.\n"))
        sys.exit(1)
    for uid in args.ids:
        install_addon(cfg, str(uid), version=args.version, lock=args.lock)
    print()

def resolve_tracked(cfg: dict, token: str, multi: bool = False) -> list:
    """
    ESOUI ID, or an addon name -> list of keys in cfg['installed'].
    An ID or a complete name/folder name is taken as is. Anything else is a partial match:
    the person is asked to confirm (even when only one addon matches). Enter = cancel.
    """
    token = str(token).strip()
    low = token.lower()
    items = sorted(cfg["installed"].items(), key=lambda kv: kv[1].get("name", "").lower())
    if token in cfg["installed"]:
        return [token]
    exact = [k for k, v in items if v.get("name", "").lower() == low
             or low in [f.lower() for f in v.get("folders", [])]]
    if len(exact) == 1:
        return exact
    if token.isdigit() or not low:
        return []
    found = exact or [k for k, v in items
                      if any(low in n for n in [v.get("name", "").lower()] + [f.lower() for f in v.get("folders", [])])]
    if not found:
        return []
    try:
        if len(found) == 1:
            v = cfg["installed"][found[0]]
            ans = input(c("cyan", f"  '{token}' is not a full name. Did you mean {v.get('name', found[0])} "
                                  f"[{found[0]}]? [y/N] ")).strip().lower()
            return found if ans in ("y", "yes") else []
        print(c("bold", f"  '{token}' matches {len(found)} addons:"))
        for n, k in enumerate(found, 1):
            print(f"    {c('dim', pad(str(n) + ')', 4))}{c('cyan', pad('[' + k + ']', 7))} "
                  f"{cfg['installed'][k].get('name', k)}")
        hint = "number(s) like 1 3, a = all" if multi else "a number"
        ans = input(c("cyan", f"  Which one? {hint}, Enter = cancel: ")).strip().lower()
    except EOFError:
        return []
    if multi and ans in ("a", "all"):
        return found
    picks = []
    for part in ans.replace(",", " ").split():
        if part.isdigit() and 1 <= int(part) <= len(found) and found[int(part) - 1] not in picks:
            picks.append(found[int(part) - 1])
    return picks if multi else picks[:1]

def cmd_lock(args, cfg: dict) -> None:
    """Hold addon(s) at the installed version: update skips them."""
    print()
    for token in args.addons:
        keys = resolve_tracked(cfg, token, multi=True)
        if not keys:
            print(c("yellow", f"  Nothing locked for '{token}' (not a tracked addon, or cancelled)."))
        for key in keys:
            cfg["installed"][key]["locked"] = True
            save_config(cfg)
            print(f"  {c('cyan', '🔒')} {cfg['installed'][key]['name']} locked at {cfg['installed'][key].get('version', '?')}")
    print()

def cmd_unlock(args, cfg: dict) -> None:
    print()
    for token in args.addons:
        keys = resolve_tracked(cfg, token, multi=True)
        if not keys:
            print(c("yellow", f"  Nothing unlocked for '{token}' (not a tracked addon, or cancelled)."))
        for key in keys:
            if cfg["installed"][key].pop("locked", None):
                save_config(cfg)
                print(f"  {c('green', '✔')} {cfg['installed'][key]['name']} unlocked")
            else:
                print(c("dim", f"  {cfg['installed'][key]['name']} was not locked."))
    print()

def cmd_versions(args, cfg: dict) -> None:
    """List the current and older versions ESOUI has for one addon."""
    if str(args.addon).isdigit():
        key = str(args.addon)                       # any ESOUI ID works, tracked or not
    else:
        keys = resolve_tracked(cfg, args.addon)
        if not keys:
            print(c("yellow", f"\n  '{args.addon}' is not a tracked addon, or it was cancelled.\n"))
            return
        key = keys[0]
    details = fetch_details(key)
    if not details:
        return
    have = norm_version(cfg["installed"].get(key, {}).get("version"))
    print(f"\n  {c('bold', details.get('UIName', key))}  {c('dim', '[' + key + ']')}")
    sep()
    cur = details.get("UIVersion", "?")
    print(f"  {pad(str(cur), 20)} {pad(fmt_date(details.get('UIDate')), 12)} {c('green', 'current')}"
          + (c("cyan", "  ◀ installed") if have == norm_version(cur) else ""))
    rows = archive_versions(key)
    shown = rows if args.all else rows[:15]
    for r in shown:
        print(f"  {pad(r['version'], 20)} {pad(fmt_date(r['date']), 12)}"
              + (c("cyan", "  ◀ installed") if have == norm_version(r["version"]) else ""))
    if len(rows) > len(shown):
        print(c("dim", f"  … {len(rows) - len(shown)} more (use --all)"))
    sep()
    print(c("dim", f"  Install one with:  install {key} --version <version> [--lock]\n"))

def _update_addons(args, cfg: dict) -> bool:
    """Update linked addons. Returns True if the TamrielTradeCentre addon itself was updated."""
    ttc_updated = False
    filelist = get_filelist(force=True)
    if not filelist:
        return False
    if Path(cfg["addon_dir"]).is_dir():
        do_scan(cfg, filelist, quiet=True)        # make sure the config matches what is on disk
    tracked = {k: v for k, v in cfg["installed"].items() if k.isdigit()}
    skipped = [k for k in cfg["installed"] if k.startswith("local:")]
    if not tracked:
        print(c("yellow", "\n  Nothing linked to ESOUI yet. Run 'scan' (and 'match') first.\n"))
        return False
    by_uid = {a["UID"]: a for a in filelist}

    outdated = []
    for uid, info in tracked.items():
        addon = by_uid.get(uid)
        if not addon:
            print(f"  {c('red', '✗')} {info['name']}  {c('red', 'no longer on ESOUI')}")
            continue
        if int(addon.get("UIDate") or 0) > int(info.get("installed_date") or 0):
            outdated.append((uid, info, addon))

    held = [t for t in outdated if t[1].get("locked")]
    outdated = [t for t in outdated if not t[1].get("locked")]

    print()
    if not outdated:
        print(c("green", f"  All {len(tracked)} linked addons are up to date" + (" (apart from locked ones)." if held else ".")))
    else:
        sep()
        for uid, info, addon in sorted(outdated, key=lambda t: t[1]["name"].lower()):
            print(f"  {pad(info['name'], 56)} {pad(str(info.get('version', '?')), 14)} → "
                  f"{c('yellow', str(addon.get('UIVersion', '?')))}  "
                  f"{c('dim', fmt_date(addon.get('UIDate')))}")
        sep()
        print(f"\n  {len(outdated)} update(s) available.")
        chosen = outdated
        if not args.yes:
            try:
                answer = input(c("cyan", "  Update [a]ll, [c]hoose one by one, or [N]one? ")).strip().lower()
            except EOFError:
                answer = ""
            if answer in ("a", "all", "y", "yes"):
                chosen = outdated
            elif answer in ("c", "choose"):
                chosen = []
                for uid, info, addon in sorted(outdated, key=lambda t: t[1]["name"].lower()):
                    cur = info.get("version", "?")
                    try:
                        pick = input(c("cyan", f"  {info['name']}  {cur} → {addon.get('UIVersion', '?')}   "
                                               f"[y]es, [N]o, [l]ock at {cur}, [q]uit: ")).strip().lower()
                    except EOFError:
                        break
                    if pick in ("y", "yes"):
                        chosen.append((uid, info, addon))
                    elif pick in ("l", "lock"):
                        cfg["installed"][uid]["locked"] = True
                        save_config(cfg)
                        print(c("cyan", f"    🔒 locked at {cur}"))
                    elif pick in ("q", "quit"):
                        break
            else:
                chosen = []
        if chosen:
            print()
            ok = sum(install_addon(cfg, uid, addon) for uid, _, addon in chosen)
            print(f"\n  {c('green', str(ok))} of {len(chosen)} updated.")
            ttc_updated = any(TTC_FOLDER in (a.get("UIDir") or []) for _, _, a in chosen)
    for uid, info, addon in held:
        print(c("cyan", f"  🔒 {info['name']} is locked at {info.get('version', '?')} "
                        f"({addon.get('UIVersion', '?')} available)"))
    if skipped:
        print(c("dim", f"  {len(skipped)} addon(s) not linked to ESOUI were skipped (run 'match')."))
    print()
    return ttc_updated

# ── Tamriel Trade Centre price table ─────────────────────────────────────────
# The TTC addon needs a price table that is NOT part of the addon zip. It is downloaded
# separately from tamrieltradecentre.com and unzipped into the addon's folder.
TTC_FOLDER = "TamrielTradeCentre"
TTC_DEFAULTS = {"enabled": True, "region": "us", "interval_hours": 6, "last_download": {}}

def ttc_settings(store: dict) -> dict:
    t = store.setdefault("ttc", {})
    for k, v in TTC_DEFAULTS.items():
        t.setdefault(k, dict(v) if isinstance(v, dict) else v)
    return t

def ttc_url(region: str) -> str:
    return f"https://{'eu' if region == 'eu' else 'us'}.tamrieltradecentre.com/download/PriceTable"

def ttc_installed(cfg: dict) -> bool:
    return (Path(cfg["addon_dir"]) / TTC_FOLDER).is_dir()

def update_price_table(cfg: dict, force: bool = False) -> bool:
    """Download + unzip the TTC price table. Skipped when TTC is not installed, switched off,
    or was downloaded less than `interval_hours` ago (unless force). Returns True if downloaded."""
    store = cfg["_store"]
    t = ttc_settings(store)
    if not ttc_installed(cfg):
        return False
    if not t["enabled"] and not force:
        return False
    last = float(t["last_download"].get(cfg["env"], 0) or 0)
    wait = float(t["interval_hours"]) * 3600
    age = time.time() - last
    if not force and last and age < wait:
        left = (wait - age) / 3600
        print(c("dim", f"  TTC price table: downloaded {age / 3600:.1f} h ago — next check in {left:.1f} h "
                       f"(every {t['interval_hours']:g} h)."))
        return False
    folder = Path(cfg["addon_dir"]) / TTC_FOLDER
    print(f"  {c('bold', 'TTC price table')}  {c('dim', ttc_url(t['region']))}")
    try:
        data = http_get(ttc_url(t["region"]), timeout=180)
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise ValueError("download was not a zip file")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = z.testzip()
            if bad:
                raise ValueError(f"corrupt zip ({bad})")
        extract_zip(data, folder)
    except Exception as e:                       # leave the old table and the timestamp alone
        note_failure(f"TTC price table: {e}")
        print(c("red", f"    Failed: {e}"))
        return False
    t["last_download"][cfg["env"]] = int(time.time())
    write_store(store)
    print(c("green", f"    ✔ Price table updated ({len(data) // 1024} KB)"))
    return True

def cmd_update(args, cfg: dict) -> None:
    ttc_updated = _update_addons(args, cfg)
    ttc_settings(cfg["_store"])
    if ttc_installed(cfg):
        update_price_table(cfg, force=ttc_updated)   # a fresh TTC addon needs its table again
        print()

def cmd_ttc(args, cfg: dict) -> None:
    """Download the price table right now (ignores the interval)."""
    if not ttc_installed(cfg):
        print(c("yellow", f"\n  {TTC_FOLDER} is not installed in {cfg['addon_dir']}\n"))
        return
    print()
    update_price_table(cfg, force=True)
    print()

# ── Search / list / info / remove ────────────────────────────────────────────

def cmd_search(args, cfg: dict) -> None:
    filelist = get_filelist()
    if not filelist:
        return
    terms = [norm(t) for t in args.query if norm(t)]
    hits = [a for a in filelist
            if all(t in norm(a["UIName"]) or any(t in norm(d) for d in a.get("UIDir") or [])
                   for t in terms)]
    hits.sort(key=lambda a: -int(a.get("UIDownloadTotal") or 0))
    print()
    if not hits:
        close = candidate_matches(" ".join(args.query), [], filelist, limit=5)
        print(c("yellow", "  No exact matches."), "Closest:" if close else "")
        hits = [a for _, a in close]
    installed = set(cfg["installed"])
    shown = hits[:20]
    sep()
    for n, a in enumerate(shown, 1):
        mark = c("green", "  ✔ installed") if a["UID"] in installed else ""
        print(f"  {c('dim', pad(str(n) + ')', 4))}{c('cyan', pad('[' + a['UID'] + ']', 7))} "
              f"{pad(a['UIName'], 45)} {pad(str(a.get('UIVersion', '?')), 12)} "
              f"{c('dim', a.get('UIAuthorName', ''))}{mark}")
    sep()
    print()

    available = [a for a in shown if a["UID"] not in installed]
    if not available:
        return
    try:
        raw = input(c("cyan", "  Install? Enter result number(s), e.g. 1 3 (Enter = no): ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not raw:
        print()
        return

    picks, seen = [], set()
    for token in re.split(r"[,\s]+", raw):
        if not token.isdigit() or not 1 <= int(token) <= len(shown):
            print(c("yellow", f"  Ignoring '{token}' (not a result number)."))
            continue
        a = shown[int(token) - 1]
        if a["UID"] in installed:
            print(c("dim", f"  {a['UIName']} is already installed — skipped."))
        elif a["UID"] not in seen:
            seen.add(a["UID"])
            picks.append(a)
    if picks:
        print()
        for a in picks:
            install_addon(cfg, a["UID"], a)
    print()

def cmd_list(args, cfg: dict) -> None:
    print(f"\n  {c('bold', 'Tracked addons')}  {c('dim', cfg['addon_dir'])}")
    sep()
    items = sorted(cfg["installed"].items(), key=lambda kv: kv[1].get("name", "").lower())
    terms = [t.strip().lower() for t in (getattr(args, "addons", None) or []) if t.strip()]
    if terms:
        def matches(key, info):
            names = [info.get("name", "").lower()] + [f.lower() for f in info.get("folders", [])]
            # a number is an ESOUI ID: exact match only (so "1" doesn't match 1752 or "Addon 1.2")
            return any(t == key.lower() or t in names or (not t.isdigit() and any(t in n for n in names))
                       for t in terms)
        items = [kv for kv in items if matches(*kv)]
        if not items:
            print(c("yellow", f"  No tracked addon matches: {' '.join(args.addons)}"))
            print(c("dim", "  (Use an ESOUI ID, a name, or part of a name or folder name.)"))
    elif not items:
        print(c("yellow", "  Nothing tracked yet — run 'scan'."))
    for key, info in items:
        linked = key.isdigit()
        ident = c("cyan", pad(f"[{key}]", 7)) if linked else c("yellow", pad("local", 7))
        n = len(info.get("folders", []))
        extra = c("dim", f"  ({n} folders)") if n > 1 else ""
        lock = c("cyan", "  🔒 locked") if info.get("locked") else ""
        print(f"  {ident} {pad(info.get('name', key), 58)} "
              f"{pad(str(info.get('version', '?')), 14)}{lock}{extra}")
    sep()
    print()

def cmd_info(args, cfg: dict) -> None:
    uid = str(args.id)
    details = fetch_details(uid)
    if not details:
        return
    print()
    fields = [("Name", "UIName"), ("Author", "UIAuthorName"), ("Version", "UIVersion"),
              ("Downloads", "UIDownloadTotal"), ("Favorites", "UIFavoriteTotal"),
              ("Download", "UIDownload"), ("Page", "UIFileInfoURL")]
    for label, key in fields:
        if details.get(key):
            print(f"  {c('cyan', pad(label + ':', 11))} {details[key]}")
    if details.get("UIDate"):
        print(f"  {c('cyan', pad('Updated:', 11))} {fmt_date(details['UIDate'])}")
    if details.get("UIDir"):
        print(f"  {c('cyan', pad('Folders:', 11))} {', '.join(details['UIDir'])}")
    print("  " + (c("green", "✔ tracked") if uid in cfg["installed"]
                  else c("dim", f"install with:  install {uid}")) + "\n")

# Windows and macOS (default volumes) ignore capitalization in folder names; Linux does not.
# An exact-case match is always preferred, so a case-sensitive Mac volume still works safely.
CASE_INSENSITIVE_FS = sys.platform in ("win32", "darwin")

def saved_variable_files(cfg: dict, folders: list) -> list:
    """
    SavedVariables files that belong to the given addon folders.
    ESO names each file after the names an addon declares in its manifest
    ('## SavedVariables: …'); the folder name is checked too. Files whose name is declared
    by an addon that stays installed are never returned. Call BEFORE deleting the folders
    (the manifests are read from them).
    """
    sv_dir = cfg.get("sv_dir")
    if not sv_dir or not Path(sv_dir).is_dir():
        return []
    addon_dir = Path(cfg["addon_dir"])

    def declared(folder: Path) -> list:
        mp = manifest_path(folder)
        raw = parse_manifest(mp).get("SavedVariables", "") if mp else ""
        return [n for n in re.split(r"[,\s]+", raw) if n]

    removing = set(folders)
    wanted = []
    for f in folders:
        wanted += declared(addon_dir / f)
        wanted.append(f)
    keep = set()
    for other in addon_dir.iterdir():
        if other.is_dir() and other.name not in removing:
            keep.update(declared(other))
    files = []
    for name in dict.fromkeys(wanted):
        if name in keep or "/" in name or "\\" in name or name.startswith("."):
            continue
        path = Path(sv_dir) / f"{name}.lua"
        if path.is_file():                       # libraries often have none
            files.append(path)
    return files

def decide_saved_variables(cfg: dict, files: list, yes: int) -> bool:
    """
    Decide whether to delete an addon's saved-variables files.
      -yy (yes >= 2)        delete them, whatever the setting
      saved_vars = always   delete them
      saved_vars = never    keep them
      saved_vars = ask      ask - but -y alone never deletes your data, it keeps the files
    """
    if not files:
        return False
    names = ", ".join(p.name for p in files)
    if yes >= 2:
        return True
    mode = cfg.get("saved_vars", "ask")
    if mode == "always":
        return True
    if mode == "never":
        print(c("dim", f"  Keeping saved variables: {names}"))
        return False
    if yes:
        print(c("dim", f"  Keeping saved variables: {names}  "
                       "(use -yy, or 'config --saved-vars always', to delete them automatically)"))
        return False
    answer = input(c("cyan", f"  Also delete saved variables ({names})? [y/N] "))
    return answer.strip().lower() in ("y", "yes")

def delete_saved_variables(files: list) -> None:
    for f in files:
        try:
            f.unlink()
        except OSError as e:
            print(c("red", f"  Could not delete {f.name}: {e}"))
    if files:
        print(c("green", f"  ✔ Deleted saved variables: {', '.join(f.name for f in files)}"))

def remove_folder(name: str, cfg: dict, addon_dir: Path, assume_yes: int) -> None:
    """Delete a single folder by name, tracked or not (e.g. a deprecated addon)."""
    if (not name or name in (".", "..") or "/" in name or "\\" in name
            or name.lower().startswith("local:")):
        print(c("yellow", f"  '{name}' is not a tracked addon or a folder name."))
        return
    folders = [p for p in addon_dir.iterdir() if p.is_dir()]
    match = next((p for p in folders if p.name == name), None)
    if match is None:
        same_ignoring_case = [p for p in folders if p.name.lower() == name.lower()]
        if same_ignoring_case and CASE_INSENSITIVE_FS:
            match = same_ignoring_case[0]               # Windows / macOS ignore case
        elif same_ignoring_case:
            print(c("yellow", f"  No folder named '{name}' — file names are case-sensitive here."))
            print(c("dim", f"  Did you mean '{same_ignoring_case[0].name}'?"))
            return
        else:
            print(c("yellow", f"  '{name}' is not tracked and there is no folder with that name."))
            return
    owner = next((k for k, v in cfg["installed"].items() if match.name in v.get("folders", [])), None)
    if owner:
        print(f"\n  {c('bold', match.name)} belongs to {cfg['installed'][owner]['name']} "
              f"— only this folder will be deleted.")
    else:
        print(f"\n  {c('bold', match.name)} is not tracked — will delete the folder.")
    if not assume_yes and input(c("cyan", "  Delete it? [y/N] ")).strip().lower() not in ("y", "yes"):
        print(c("dim", "  Skipped."))
        return
    sv_files = saved_variable_files(cfg, [match.name])
    delete_sv = decide_saved_variables(cfg, sv_files, assume_yes)
    shutil.rmtree(match)
    cfg["manual_links"].pop(match.name.lower(), None)
    if owner:
        left = [f for f in cfg["installed"][owner]["folders"] if f != match.name]
        if left:
            cfg["installed"][owner]["folders"] = left
        else:
            del cfg["installed"][owner]
    save_config(cfg)
    print(c("green", "  ✔ Removed."))
    if delete_sv:
        delete_saved_variables(sv_files)

def cmd_remove(args, cfg: dict) -> None:
    addon_dir = Path(cfg["addon_dir"]).resolve()
    for raw in args.ids:
        key = raw if raw.startswith("local:") else str(raw)
        info = cfg["installed"].get(key)
        if not info:
            remove_folder(raw, cfg, addon_dir, args.yes)
            continue
        folders = info.get("folders", [])
        print(f"\n  {c('bold', info['name'])}: will delete {', '.join(folders)}")
        if not args.yes and input(c("cyan", "  Delete these folders? [y/N] ")).strip().lower() not in ("y", "yes"):
            print(c("dim", "  Skipped."))
            continue
        sv_files = saved_variable_files(cfg, folders)
        delete_sv = decide_saved_variables(cfg, sv_files, args.yes)
        for f in folders:
            target = (addon_dir / f).resolve()
            if target.parent == addon_dir and target.is_dir():
                shutil.rmtree(target)
            cfg["manual_links"].pop(f.lower(), None)
        del cfg["installed"][key]
        save_config(cfg)
        print(c("green", "  ✔ Removed."))
        if delete_sv:
            delete_saved_variables(sv_files)
    print()

# ── Finding the game folder ──────────────────────────────────────────────────

_SKIP_DIRS = {".git", "node_modules", "__pycache__", "site-packages", ".cache", ".npm", ".cargo",
              ".rustup", ".gradle", ".m2", "$recycle.bin", "system volume information", "windows",
              "program files", "program files (x86)", "appdata", "lost+found", ".trash", "trash"}
if sys.platform == "darwin":
    _SKIP_DIRS.add("library")

def looks_like_eso_folder(p: Path) -> bool:
    """An 'Elder Scrolls Online' folder that really contains a game environment (live / pts …)."""
    return p.is_dir() and bool(available_envs(p))

def likely_locations() -> list:
    """The usual places, checked before any slow search."""
    home = Path.home()
    steam_tail = Path("steamapps/compatdata/306130/pfx/drive_c/users/steamuser/Documents/Elder Scrolls Online")
    candidates = [default_root(), home / "Documents" / "Elder Scrolls Online",
                  home / "OneDrive" / "Documents" / "Elder Scrolls Online",
                  home / ".steam" / "steam" / steam_tail,
                  home / ".local" / "share" / "Steam" / steam_tail,
                  home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam" / steam_tail]
    seen, result = set(), []
    for cand in candidates:
        try:
            key = cand.resolve()
        except OSError:
            continue
        if key not in seen and looks_like_eso_folder(cand):
            seen.add(key)
            result.append(cand)
    return result

def _skip_dir(parent: Path, name: str) -> bool:
    low = name.lower()
    if low in _SKIP_DIRS:
        return True
    if parent.name.lower() == "steamapps" and low in ("common", "shadercache", "downloading", "workshop", "temp"):
        return True                                     # huge, never contains Documents
    if parent.name.lower() == "compatdata" and name != "306130":
        return True                                     # only ESO's own Steam prefix matters
    if low in ("proc", "sys", "dev") and parent == parent.parent:
        return True                                     # top-level system folders on Linux
    return False

def find_eso_folders(start: Path, max_depth: int = 14, time_limit: float = 90.0, show_progress: bool = False):
    """
    Breadth-first search below `start` for an 'Elder Scrolls Online' folder that contains a game
    environment. Symbolic links are not followed (no loops), noisy folders are skipped.
    Returns (folders, directories_scanned, timed_out).
    """
    found, seen = [], set()
    queue = deque([(start, 0)])
    scanned, timed_out, started, last_shown = 0, False, time.time(), 0.0
    while queue:
        now = time.time()
        if now - started > time_limit:
            timed_out = True
            break
        if show_progress and now - last_shown > 0.25:
            print(f"\r  Searching … {scanned} folders checked", end="", flush=True)
            last_shown = now
        here, depth = queue.popleft()
        scanned += 1
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for e in entries:
            try:
                if e.name.lower() == "elder scrolls online" and e.is_dir():     # also via a symlink
                    path = Path(e.path)
                    if looks_like_eso_folder(path):
                        key = path.resolve()
                        if key not in seen:
                            seen.add(key)
                            found.append(path)
                        continue
                if depth < max_depth and e.is_dir(follow_symlinks=False) and not _skip_dir(here, e.name):
                    queue.append((Path(e.path), depth + 1))
            except OSError:
                continue
    if show_progress:
        print("\r" + " " * 50 + "\r", end="")
    return found, scanned, timed_out

def describe_folder(p: Path) -> str:
    parts = []
    for env in available_envs(p):
        add = p / env / "AddOns"
        n = sum(1 for d in add.iterdir() if d.is_dir()) if add.is_dir() else 0
        parts.append(f"{env}: {n} addon folders")
    return ", ".join(parts)

def locate_game_folder(start: Path | None = None) -> Path | None:
    """Find the Elder Scrolls Online folder and let the user confirm it. Returns it, or None."""
    found, timed_out = [], False
    if start is None:
        print(c("dim", "\n  Checking the usual places …"))
        found = likely_locations()
        start = Path.home()
        if found:
            print(c("dim", "  Found it in a standard location."))
    if not found:
        if not start.is_dir():
            print(c("red", f"\n  '{start}' is not a folder.\n"))
            return None
        print(c("dim", f"\n  Searching {start} (this can take a minute) …"))
        found, scanned, timed_out = find_eso_folders(start, show_progress=sys.stdout.isatty())
        print(c("dim", f"  Looked through {scanned} folders."))
    if not found:
        print(c("yellow", "\n  Couldn't find an 'Elder Scrolls Online' folder"
                          + (" (stopped at the time limit)." if timed_out else ".")))
        print(c("dim", "  It is usually inside your Documents folder and contains 'live' (and maybe 'pts').\n"
                       "  Try a different place:  config --set-dir search <folder to look in>\n"
                       "  or type the path:       config --set-dir \"<full path>\"\n"))
        return None

    print()
    try:
        if len(found) == 1:
            print(f"  Found:  {c('bold', str(found[0]))}")
            print(c("dim", f"          {describe_folder(found[0])}"))
            answer = input(c("cyan", "\n  Is this correct? [Y/n] ")).strip().lower()
            return found[0] if answer in ("", "y", "yes") else None
        print(f"  Found {len(found)} folders:\n")
        for i, f in enumerate(found, 1):
            print(f"   {c('cyan', str(i))}) {c('bold', str(f))}")
            print(c("dim", f"        {describe_folder(f)}"))
        answer = input(c("cyan", f"\n  Which one? [1-{len(found)}, Enter = 1, n = none] ")).strip().lower()
        if answer in ("n", "no", "none", "q"):
            return None
        if answer == "":
            return found[0]
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            return found[int(answer) - 1]
        print(c("yellow", "  Not a valid choice — nothing changed."))
    except (EOFError, KeyboardInterrupt):
        print()
    return None

def set_root(store: dict, path: Path) -> Path:
    """Remember the game folder (accepts the root, an environment folder, or an AddOns path)."""
    root, env_hint = normalize_root(path)
    existing = root / "config.json"
    if existing.is_file() and existing != config_path_for(store):
        try:                                              # a config from a backup (or an earlier install)
            found = json.loads(existing.read_text(encoding="utf-8"))
            if found.get("config_version") == CONFIG_VERSION and "envs" in found:
                n = sum(len(e.get("installed", {})) for e in found["envs"].values())
                store.clear()
                store.update(found)
                print(c("green", f"\n  ✔ Found an existing config.json in that folder — using it ({n} tracked addons)."))
        except (OSError, ValueError, AttributeError):
            pass
    store["root"] = str(root)
    print(c("green", f"\n  ✔ Elder Scrolls Online folder set to {root}"))
    if env_hint:
        store["default_env"] = env_hint
        print(c("dim", f"  Detected environment '{env_hint}' from that path — it is now the default."))
    if not root.is_dir():
        print(c("yellow", "  Note: that folder doesn't exist (yet)."))
    write_store(store)
    return root

def is_interactive() -> bool:
    """True when a person is at the keyboard (so it is safe to ask questions)."""
    return sys.stdin.isatty() and sys.stdout.isatty()

def offer_search(cfg: dict, command: str, env: str | None, interactive: bool | None = None) -> dict:
    """If a command needs the game folder and it's missing, offer to look for it right now."""
    if command not in NEEDS_FOLDER and command != "install":
        return cfg
    if Path(cfg["root"]).is_dir():
        return cfg
    if interactive is None:
        interactive = is_interactive()
    if not interactive:
        return cfg
    print(c("yellow", f"  Elder Scrolls Online folder not found: {cfg['root']}"))
    try:
        answer = input(c("cyan", "  Search for it now? [Y/n] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return cfg
    if answer not in ("", "y", "yes"):
        return cfg
    chosen = locate_game_folder(None)
    if not chosen:
        return cfg
    set_root(cfg["_store"], chosen)
    print()
    return load_config(env)

def interpret_folder_input(text: str, allow_missing: bool = False) -> Path | None:
    """
    Turn what a person typed into the game folder to use (or None).
      - the game folder itself, an environment folder, or an AddOns path  -> used as is
      - any other existing folder (even just '/home/me')                  -> searched inside
      - a short name that doesn't exist here ('Gaming')                   -> also tried under the home folder
    With allow_missing, a path that doesn't exist at all is returned as typed (set up before the game exists).
    """
    typed = Path(text.strip().strip("\"'")).expanduser()
    options = [typed] if typed.is_absolute() else [typed, Path.home() / typed]
    folder = next((o for o in options if o.is_dir()), None)
    if folder is None:
        if allow_missing:
            return typed
        print(c("yellow", f"       '{typed}' doesn't exist. Try again, or type s to search."))
        return None
    root, _ = normalize_root(folder)
    if looks_like_eso_folder(root):
        return folder                                   # already the game folder
    print(c("dim", f"       That isn't the game folder itself — looking inside {folder} …"))
    chosen = locate_game_folder(folder)
    if chosen:
        return chosen
    if folder.name.lower() == "elder scrolls online":    # right folder, game just never run yet
        if _prompt("       Use it anyway? [y/N] ").lower() in ("y", "yes"):
            return folder
    return None

# ── Guided setup ─────────────────────────────────────────────────────────────

def _prompt(text: str = "       > ") -> str:
    return input(c("cyan", text)).strip()

def _env_summary(root: Path, env: str) -> str:
    add = root / env / "AddOns"
    n = sum(1 for d in add.iterdir() if d.is_dir()) if add.is_dir() else 0
    return f"{n} addon folders"

def _setup_folder(store: dict) -> None:
    print(c("bold", "  1/3  Game folder")
          + c("dim", "  (the 'Elder Scrolls Online' folder that contains live/ and pts/)"))
    while True:
        root = Path(store["root"])
        found = looks_like_eso_folder(root)
        if found:
            print(f"       Current: {root}\n                {c('green', '✔')} {describe_folder(root)}")
            print(c("dim", "       Enter = keep it,  s = search again,  or type a path — even just a folder like /home/me"))
        else:
            print(f"       Current: {root}  {c('yellow', '(not found)')}")
            print(c("dim", "       Enter = search for it,  or type a path — even just a folder like /home/me — and I'll look inside,  q = skip"))
        raw = _prompt()
        if raw == "":
            if found:
                return
            raw = "s"
        if raw.lower() in ("q", "skip"):
            return
        word, _, rest = raw.partition(" ")
        if word.lower() in ("s", "search"):                  # "s"  or  "search /some/folder"
            start = Path(rest.strip().strip("\"'")).expanduser() if rest.strip() else None
            chosen = locate_game_folder(start)
            if chosen:
                set_root(store, chosen)
                return
            continue
        path = interpret_folder_input(raw)
        if path:
            set_root(store, path)
            return

def _setup_env(store: dict) -> None:
    print(c("bold", "\n  2/3  Default game version"))
    root, found = Path(store["root"]), available_envs(Path(store["root"]))
    current = store.get("default_env", "live")
    if not found:
        print(c("dim", f"       No game folders found yet — using '{current}'."))
        return
    if current not in found:
        current = "live" if "live" in found else found[0]
    if len(found) == 1:
        store["default_env"] = found[0]
        write_store(store)
        print(f"       Only '{found[0]}' found — using it.")
        return
    print("       Found more than one:")
    for i, e in enumerate(found, 1):
        print(f"         {i}) {pad(e, 8)} {c('dim', _env_summary(root, e))}")
    while True:
        raw = _prompt(f"       Which one should be the default? [{current}] > ").lower()
        if raw == "":
            choice = current
        elif raw.isdigit() and 1 <= int(raw) <= len(found):
            choice = found[int(raw) - 1]
        elif raw in found:
            choice = raw
        else:
            print(c("yellow", "       Please pick one of the numbers or names above."))
            continue
        break
    store["default_env"] = choice
    write_store(store)
    other = next((e for e in found if e != choice), None)
    print(c("dim", f"       Use the other one any time:  list --env {other}   or   {other} list"))

def _setup_saved_vars(store: dict) -> None:
    print(c("bold", "\n  3/3  Saved settings when you remove an addon")
          + c("dim", "  (its SavedVariables file)"))
    current = store.get("saved_vars", "ask")
    options = [("1", "ask", "ask me each time (recommended)"),
               ("2", "always", "always delete them"),
               ("3", "never", "never delete them")]
    for num, mode, text in options:
        print(f"         {num}) {text}{c('green', '   ← current') if mode == current else ''}")
    names = {num: mode for num, mode, _ in options} | {mode: mode for _, mode, _ in options}
    while True:
        raw = _prompt(f"       Choose [{current}] > ").lower()
        if raw == "":
            return
        if raw in names:
            store["saved_vars"] = names[raw]
            write_store(store)
            return
        print(c("yellow", "       Please type 1, 2 or 3."))

def setup_wizard(cfg: dict, env: str | None, offer_scan: bool = True) -> dict:
    """Ask for the game folder, default environment and saved-variables behaviour."""
    store = cfg["_store"]
    print(c("bold", "\n  Let's get set up.") + c("dim", "  Press Enter to keep the value shown.\n"))
    try:
        _setup_folder(store)
        _setup_env(store)
        _setup_saved_vars(store)
    except (EOFError, KeyboardInterrupt):
        write_store(store)
        print(c("dim", "\n\n  Setup stopped — your answers so far are saved. Run 'setup' to finish.\n"))
        return load_config(env)
    store["setup_done"] = True
    write_store(store)
    cfg = load_config(env)
    print(c("green", "\n  ✔ All set."))
    print(f"    Game folder      : {store['root']}")
    print(f"    Default version  : {store.get('default_env', 'live')}")
    print(f"    Saved settings   : {store.get('saved_vars', 'ask')} on remove")
    print(c("dim", "    Change any of this later with:  setup\n"))
    if offer_scan and Path(cfg["addon_dir"]).is_dir():
        try:
            if input(c("cyan", "  Scan your addons now? [Y/n] ")).strip().lower() in ("", "y", "yes"):
                cmd_scan(types.SimpleNamespace(refresh=False), cfg)
        except (EOFError, KeyboardInterrupt):
            print()
    return cfg

def first_run_setup(cfg: dict, env: str | None) -> dict:
    print(c("bold", "\n  Welcome! This looks like your first run."))
    try:
        answer = input(c("cyan", "  Set things up now? It only takes a minute. [Y/n] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("", "y", "yes"):
        return setup_wizard(cfg, env, offer_scan=False)
    cfg["_store"]["setup_done"] = True               # don't ask again
    write_store(cfg["_store"])
    print(c("dim", "  No problem — run 'setup' whenever you want the guided setup.\n"))
    return cfg

def cmd_setup(args, cfg: dict) -> None:
    if not is_interactive():
        print(c("yellow", "  'setup' asks questions, so it needs a terminal.  Without one, use:\n"
                          "    config --set-dir <path>   config --saved-vars ask|always|never   "
                          "config --default-env <env>\n"))
        return
    setup_wizard(cfg, None, offer_scan=True)

def cmd_config(args, cfg: dict) -> None:
    store = cfg["_store"]
    changed = False
    if args.set_dir:
        words = args.set_dir
        if words[0].lower() == "search":                     # config --set-dir search [START]
            start = Path(" ".join(words[1:])).expanduser() if len(words) > 1 else None
            chosen = locate_game_folder(start)
            if chosen:
                set_root(store, chosen)
                print(c("dim", "  Next step:  scan\n"))
        else:                                                # unquoted paths with spaces still work
            path = interpret_folder_input(" ".join(words), allow_missing=True)
            if path:
                set_root(store, path)
    if args.saved_vars:
        store["saved_vars"] = args.saved_vars
        changed = True
        print(c("green", f"\n  ✔ Saved variables on remove: {args.saved_vars}"))
    if args.backup_dir is not None or args.backup_keep is not None or args.backup_with is not None:
        b = backup_settings(store)
        if args.backup_dir is not None:
            if args.backup_dir.lower() == "default":
                b["dir"] = ""
                print(c("green", f"\n  ✔ Backup folder: default ({default_backup_dir()}), created when the first backup runs"))
            else:
                folder = Path(args.backup_dir).expanduser()
                try:
                    folder.mkdir(parents=True, exist_ok=True)           # configured on purpose: create it now
                    b["dir"] = str(folder)
                    print(c("green", f"\n  ✔ Backup folder: {folder}"))
                except OSError as e:
                    print(c("red", f"\n  Could not create {folder}: {e}"))
        if args.backup_keep is not None:
            b["keep"] = min(max(1, args.backup_keep), 365)
            print(c("green", f"\n  ✔ Keeping the newest {b['keep']} backups per environment"))
        if args.backup_with is not None:
            parsed = parse_with(args.backup_with)
            if parsed is None:
                print(c("red", "\n  --backup-with accepts: screenshots, addons (or none)"))
            else:
                b["with"] = parsed
                print(c("green", f"\n  ✔ Optional backup parts: {', '.join(parsed) or 'none'}"))
        changed = True
    if args.ttc or args.ttc_hours is not None or args.ttc_region:
        t = ttc_settings(store)
        if args.ttc:
            t["enabled"] = args.ttc == "on"
            print(c("green", f"\n  ✔ TTC price table with 'update': {args.ttc}"))
        if args.ttc_hours is not None:
            t["interval_hours"] = max(0.0, args.ttc_hours)
            print(c("green", f"\n  ✔ TTC price table re-downloaded at most every {t['interval_hours']:g} h"))
        if args.ttc_region:
            t["region"] = args.ttc_region
            print(c("green", f"\n  ✔ TTC region: {args.ttc_region}"))
        changed = True
    if args.default_env:
        wanted = args.default_env.lower()
        if wanted in ENV_NAMES or wanted in available_envs(Path(store["root"])):
            store["default_env"] = wanted
            changed = True
            print(c("green", f"\n  ✔ Default environment: {wanted}"))
        else:
            print(c("red", f"\n  Unknown environment '{wanted}'."))
    if changed:
        write_store(store)

    root = Path(store["root"])
    print(f"\n  Config file       : {c('cyan', str(config_path_for(store)))}"
          f"\n  App folder        : {c('dim', str(APP_DIR))}  {c('dim', '(addon list cache, game folder pointer)')}"
          f"\n  Game folder       : {c('cyan', str(root))}"
          f"{'' if root.is_dir() else c('red', '   (not found)')}"
          f"\n  Default env       : {store.get('default_env', 'live')}"
          f"\n  Saved variables   : {store.get('saved_vars', 'ask')}  {c('dim', '(on remove: ask / always / never)')}")
    t = ttc_settings(store)
    print(f"  TTC price table   : {'on' if t['enabled'] else 'off'}, every {t['interval_hours']:g} h, "
          f"region {t['region']}  {c('dim', '(runs with update when TamrielTradeCentre is installed)')}")
    b = backup_settings(store)
    print(f"  Backups           : {backup_dir(store)}{'' if b['dir'] else c('dim', '  (default)')}, keep {b['keep']} per environment, "
          f"plus {', '.join(b['with']) or 'no optional parts'}")
    found = available_envs(root)
    print("  Environments      :", ", ".join(found) if found else c("yellow", "none found"))
    for e in found:
        n_dirs = sum(1 for d in (root / e / "AddOns").iterdir() if d.is_dir()) if (root / e / "AddOns").is_dir() else 0
        tracked = len(store.get("envs", {}).get(e, {}).get("installed", {}))
        stamp = t["last_download"].get(e)
        ttc_note = f", TTC table {datetime.fromtimestamp(stamp):%Y-%m-%d %H:%M}" if stamp else ""
        print(c("dim", f"      {pad(e, 8)} {n_dirs} addon folders, {tracked} tracked{ttc_note}"))
    print(c("dim", "\n  Walk through these settings step by step with:  setup"))
    print()

# ── Backups ──────────────────────────────────────────────────────────────────
# One zip per run for one environment (live by default):
#   always : config.json, AddOnSettings.txt, UserSettings.txt, SavedVariables/, GuildHistory/ (if present)
#   options: Screenshots/, AddOns/   (config: backup_with / --with)
# Zips sit in the backup folder as  eso-<env>-YYYY-MM-DD_HHMMSS.zip ; only the newest N are kept.

BACKUP_ALWAYS_FILES = ("AddOnSettings.txt", "UserSettings.txt")
BACKUP_ALWAYS_DIRS  = ("SavedVariables", "GuildHistory")
BACKUP_OPTIONAL     = {"screenshots": "Screenshots", "addons": "AddOns"}
BACKUP_INFO         = "backup-info.json"
_BACKUP_NAME        = re.compile(r"^eso-([A-Za-z0-9_-]+)-(\d{4}-\d{2}-\d{2}_\d{6})\.zip$")
_STORED_TYPES       = (".png", ".jpg", ".jpeg", ".zip")          # already compressed

def backup_settings(store: dict) -> dict:
    b = store.setdefault("backup", {})
    b.setdefault("dir", "")            # empty = the default folder (created only when a backup runs)
    b.setdefault("keep", 7)
    b.setdefault("with", [])
    return b

def default_backup_dir() -> Path:
    return Path.home() / "Documents" / "ESO Addon Manager Backups"

def backup_dir(store: dict) -> Path:
    d = backup_settings(store)["dir"]
    return Path(d).expanduser() if d else default_backup_dir()

def parse_with(text: str) -> list | None:
    """'screenshots,addons' -> ['screenshots','addons'];  'none' -> [];  unknown name -> None."""
    words = [w for w in re.split(r"[,\s]+", (text or "").lower()) if w]
    if words in ([], ["none"]):
        return []
    if any(w not in BACKUP_OPTIONAL for w in words):
        return None
    return sorted(set(words))

def _under(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False

def backup_members(cfg: dict, with_: list, skip: Path | None = None) -> list:
    """[(archive name, file path)] for what a backup of this environment contains."""
    env_dir = Path(cfg["root"]) / cfg["env"]
    members = []
    conf = config_path_for(cfg["_store"])
    if conf.is_file():
        members.append(("config.json", conf))
    for name in BACKUP_ALWAYS_FILES:
        if (env_dir / name).is_file():
            members.append((name, env_dir / name))
    folders = list(BACKUP_ALWAYS_DIRS) + [BACKUP_OPTIONAL[w] for w in with_]
    for folder in folders:
        top = env_dir / folder
        if not top.is_dir() or top.is_symlink():
            continue
        for here, dirs, files in os.walk(top, followlinks=False):
            hp = Path(here)
            if skip and _under(hp, skip):
                dirs[:] = []
                continue
            for f in sorted(files):
                fp = hp / f
                if fp.is_symlink() or not fp.is_file():
                    continue
                members.append((fp.relative_to(env_dir).as_posix(), fp))
    return members

def backup_signature(members: list, with_: list) -> str:
    """Changes when the files change (config.json without its volatile parts: backup and TTC settings/times)."""
    h = hashlib.sha1(("|".join(with_) + "\n").encode())
    for arc, fp in members:
        if arc == "config.json":
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                data.pop("backup", None)                 # settings and download times are not "changes"
                data.pop("ttc", None)
                h.update(b"config.json" + json.dumps(data, sort_keys=True).encode())
            except (OSError, ValueError):
                h.update(b"config.json?")
            continue
        try:
            st = fp.stat()
        except OSError:
            continue
        h.update(f"{arc}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()

def write_zip(path: Path, members: list, info: dict) -> int:
    """Write the zip to a temp name, then rename: a failed run never leaves a half-written backup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    count = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr(BACKUP_INFO, json.dumps(info, indent=2))
            for arc, fp in members:
                try:
                    z.write(fp, arc, compress_type=(zipfile.ZIP_STORED if arc.lower().endswith(_STORED_TYPES)
                                                    else zipfile.ZIP_DEFLATED))
                    count += 1
                except OSError as e:                  # a file the game changed or removed meanwhile
                    print(c("yellow", f"    skipped {arc}: {e}"))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return count

def list_backups(folder: Path, env: str) -> list:
    """Backups of one environment, newest first: [(path, 'YYYY-MM-DD_HHMMSS')]."""
    found = []
    try:
        for p in folder.iterdir():
            m_ = _BACKUP_NAME.match(p.name)
            if m_ and m_.group(1) == env and p.is_file():
                found.append((p, m_.group(2)))
    except OSError:
        pass
    return sorted(found, key=lambda t: t[1], reverse=True)

def read_backup_info(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read(BACKUP_INFO).decode("utf-8"))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return {}

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024

def cmd_backup(args, cfg: dict) -> None:
    store, env = cfg["_store"], cfg["env"]
    b = backup_settings(store)
    env_dir = Path(cfg["root"]) / env
    if not env_dir.is_dir():
        note_failure(f"no '{env}' folder to back up")
        print(c("red", f"\n  No '{env}' folder in {cfg['root']}\n"))
        return
    with_ = parse_with(args.with_) if args.with_ is not None else list(b["with"])
    if with_ is None:
        print(c("red", "\n  --with accepts: screenshots, addons (or none)\n"))
        sys.exit(1)
    folder = backup_dir(store)
    members = backup_members(cfg, with_, skip=folder)
    if not any(a != "config.json" for a, _ in members):
        note_failure("nothing to back up")
        print(c("yellow", f"\n  Nothing to back up yet in {env_dir}\n"))
        return
    sig = backup_signature(members, with_)
    existing = list_backups(folder, env)
    if existing and not args.force:
        last = read_backup_info(existing[0][0])
        if last.get("sig") == sig:
            print(c("dim", f"\n  Nothing changed since {existing[0][0].name} — no new backup. (--force to make one anyway)\n"))
            return
    created_dir = not folder.exists()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = folder / f"eso-{env}-{stamp}.zip"
    parts = sorted({a.split("/")[0] for a, _ in members})
    info = {"tool": "eso_addon_manager", "env": env, "created": datetime.now().isoformat(timespec="seconds"),
            "sig": sig, "with": with_, "parts": parts}
    print(f"\n  Backing up {c('bold', env)}  {c('dim', ', '.join(parts))}")
    try:
        n = write_zip(target, members, info)
    except OSError as e:
        note_failure(f"backup failed: {e}")
        print(c("red", f"  Backup failed: {e}\n"))
        return
    if created_dir:
        print(c("dim", f"  Created the backup folder {folder}"))
    print(c("green", f"  ✔ {target.name}  ({n} files, {human_size(target.stat().st_size)})"))
    old = list_backups(folder, env)[max(1, int(b["keep"])):]
    for p, _ in old:
        try:
            p.unlink()
        except OSError:
            pass
    if old:
        print(c("dim", f"  Removed {len(old)} older backup(s) (keeping {b['keep']})"))
    if _under(folder, Path(cfg["root"])):
        print(c("yellow", "  Note: the backup folder is inside the game folder, so it is lost together with it."))
    print()

def cmd_backups(args, cfg: dict) -> None:
    folder = backup_dir(cfg["_store"])
    items = list_backups(folder, cfg["env"])
    safety = sorted(p for p in folder.glob(f"eso-{cfg['env']}-before-restore-*.zip")) if folder.is_dir() else []
    print(f"\n  {c('bold', 'Backups')} for {cfg['env']}  {c('dim', str(folder))}")
    sep()
    if not items and not safety:
        print(c("yellow", "  No backups yet — run 'backup'."))
    for n, (p, stamp) in enumerate(items, 1):
        info = read_backup_info(p)
        print(f"  {c('dim', pad(str(n) + ')', 4))}{pad(p.name, 36)} {pad(human_size(p.stat().st_size), 10)} "
              f"{c('dim', ', '.join(info.get('parts', [])))}")
    for p in safety:
        print(f"  {c('dim', '    ')}{pad(p.name, 46)} {pad(human_size(p.stat().st_size), 10)} {c('dim', 'safety copy made before a restore')}")
    sep()
    print()

def _pick_backup(cfg: dict, which: str | None):
    items = list_backups(backup_dir(cfg["_store"]), cfg["env"])
    if not items:
        print(c("yellow", "\n  No backups found — run 'backup' first.\n"))
        return None
    if which in (None, ""):
        print()
        for n, (p, _) in enumerate(items[:15], 1):
            print(f"    {c('dim', pad(str(n) + ')', 4))}{p.name}  {c('dim', human_size(p.stat().st_size))}")
        try:
            which = input(c("cyan", "  Restore which one? Number, Enter = cancel: ")).strip()
        except EOFError:
            which = ""
        if not which:
            print(c("dim", "  Cancelled.\n"))
            return None
    if which.lower() == "latest":
        return items[0][0]
    if which.isdigit() and 1 <= int(which) <= len(items):
        return items[int(which) - 1][0]
    for p, _ in items:
        if p.name == which or p.stem == which:
            return p
    print(c("red", f"\n  No backup '{which}' for {cfg['env']}.\n"))
    return None

def cmd_restore(args, cfg: dict) -> None:
    store, env = cfg["_store"], cfg["env"]
    env_dir = (Path(cfg["root"]) / env).resolve()
    zpath = _pick_backup(cfg, args.backup)
    if not zpath:
        return
    allowed_dirs = set(BACKUP_ALWAYS_DIRS) | set(BACKUP_OPTIONAL.values())
    allowed_files = set(BACKUP_ALWAYS_FILES)
    try:
        z = zipfile.ZipFile(zpath)
    except (OSError, zipfile.BadZipFile) as e:
        note_failure(f"cannot open {zpath.name}: {e}")
        print(c("red", f"\n  Cannot open {zpath.name}: {e}\n"))
        return
    with z:
        entries = []
        for zi in z.infolist():
            name = zi.filename.replace("\\", "/").lstrip("/")
            parts = [x for x in name.split("/") if x]
            if not parts or name == BACKUP_INFO or zi.is_dir() or ((zi.external_attr >> 16) & 0o170000) == 0o120000:
                continue
            if name == "config.json" or parts[0] in allowed_files or (len(parts) > 1 and parts[0] in allowed_dirs):
                target = (env_dir / name).resolve()
                if name != "config.json" and (not target.is_relative_to(env_dir) or target == env_dir):
                    continue                                   # zip-slip guard
                entries.append((zi, name, target))
        if len(entries) > 500_000:
            raise SystemExit("backup has too many files")
        tops = sorted({n.split("/")[0] for _, n, _ in entries if n != "config.json"})
        has_config = any(n == "config.json" for _, n, _ in entries)
        if not tops and not has_config:
            print(c("yellow", "\n  That zip has nothing this tool restores.\n"))
            return
        print(f"\n  {c('bold', zpath.name)} will restore into {env_dir}:")
        print(f"    {', '.join(tops) or '(nothing)'}" + ("   + config.json (asks)" if has_config else ""))
        print(c("dim", "    Files with the same name are overwritten; files that aren't in the backup are left alone."))
        print(c("dim", "    A safety copy of what is there now is made first."))
        if not args.yes:
            try:
                if input(c("cyan", "  Continue? [y/N] ")).strip().lower() not in ("y", "yes"):
                    print(c("dim", "  Cancelled.\n"))
                    return
            except EOFError:
                print(c("dim", "  Cancelled (no one to ask; use -y).\n"))
                return
        restore_config = args.config
        if has_config and not restore_config and not args.yes:
            try:
                restore_config = input(c("cyan", "  Also restore config.json (addon links and settings)? [y/N] ")).strip().lower() in ("y", "yes")
            except EOFError:
                restore_config = False
        # safety copy of the files about to be replaced
        current = [(a, fp) for a, fp in backup_members(cfg, [], skip=backup_dir(store))
                   if a != "config.json" and a.split("/")[0] in tops]
        for w, folder_name in BACKUP_OPTIONAL.items():
            if folder_name in tops:
                current += [(a, fp) for a, fp in backup_members(cfg, [w], skip=backup_dir(store)) if a.startswith(folder_name + "/")]
        if current:
            safety = backup_dir(store) / f"eso-{env}-before-restore-{datetime.now():%Y-%m-%d_%H%M%S}.zip"
            try:
                write_zip(safety, current, {"tool": "eso_addon_manager", "env": env, "note": "safety copy before restore"})
                print(c("dim", f"  Safety copy: {safety.name}"))
            except OSError as e:
                note_failure(f"could not make the safety copy: {e}")
                print(c("red", f"  Could not make the safety copy ({e}) — nothing restored.\n"))
                return
        done = 0
        for zi, name, target in entries:
            if name == "config.json":
                if not restore_config:
                    continue
                target = config_path_for(store)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".restoring")
            with z.open(zi) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(tmp, target)
            done += 1
    print(c("green", f"  ✔ Restored {done} files into {env}.") +
          c("dim", "  Run 'scan' if you restored AddOns or config.json."))
    print()


def cmd_copy(args, cfg: dict) -> None:
    """Copy one environment's AddOns (and optionally SavedVariables) to another, e.g. live → pts."""
    store, root = cfg["_store"], Path(cfg["root"])
    names = [n.lower() for n in args.envs]
    if len(names) > 2:
        print(c("red", "\n  Usage: copy [SOURCE] DEST   (e.g.  copy live pts)\n"))
        return
    src, dst = (store.get("default_env", "live"), names[0]) if len(names) == 1 else names
    known = set(ENV_NAMES) | set(available_envs(root))
    for label, e in (("source", src), ("destination", dst)):
        if e not in known:
            print(c("red", f"\n  Unknown {label} environment '{e}'.  Known: {', '.join(sorted(known))}\n"))
            return
    if src == dst:
        print(c("red", "\n  Source and destination are the same.\n"))
        return
    src_add, dst_add = root / src / "AddOns", root / dst / "AddOns"
    if not src_add.is_dir():
        print(c("red", f"\n  Nothing to copy: {src_add} doesn't exist.\n"))
        return

    src_dirs = sorted(p.name for p in src_add.iterdir() if p.is_dir())
    dst_dirs = {p.name for p in dst_add.iterdir() if p.is_dir()} if dst_add.is_dir() else set()
    overwrite = [n for n in src_dirs if n in dst_dirs]
    extra = sorted(dst_dirs - set(src_dirs)) if args.mirror else []
    sv_src, sv_dst = root / src / "SavedVariables", root / dst / "SavedVariables"
    sv_files = (sorted(p.name for p in sv_src.iterdir() if p.is_file())
                if args.saved_vars and sv_src.is_dir() else [])

    print(f"\n  Copy {c('bold', src)} → {c('bold', dst)}")
    print(f"    AddOns        : {len(src_dirs)} folders  "
          f"({len(src_dirs) - len(overwrite)} new, {len(overwrite)} replaced)")
    if args.mirror:
        print(f"    Mirror        : {len(extra)} folder(s) only in {dst} will be deleted"
              + (f": {', '.join(extra[:6])}{' …' if len(extra) > 6 else ''}" if extra else ""))
    if args.saved_vars:
        print(f"    SavedVariables: {len(sv_files)} file(s) copied over any with the same name in {dst}")
    else:
        print(c("dim", f"    SavedVariables: not copied (add --saved-vars to include them)"))
    if not args.yes and input(c("cyan", "\n  Continue? [y/N] ")).strip().lower() not in ("y", "yes"):
        print(c("dim", "  Cancelled.\n"))
        return

    print(c("dim", "\n  Copying …"), end="", flush=True)
    dst_add.mkdir(parents=True, exist_ok=True)
    for name in extra:
        shutil.rmtree(dst_add / name)
    for name in src_dirs:
        target = dst_add / name
        if target.exists():
            shutil.rmtree(target)                       # replace, so no stale files are left behind
        shutil.copytree(src_add / name, target)
    if sv_files:
        sv_dst.mkdir(parents=True, exist_ok=True)
        for name in sv_files:
            shutil.copy2(sv_src / name, sv_dst / name)
    print(c("green", " done"))

    # carry over your manual links, then rebuild the destination's tracking from what's on disk
    dst_cfg, src_cfg = build_cfg(store, dst), build_cfg(store, src)
    dst_cfg["manual_links"].update(src_cfg["manual_links"])
    filelist = get_filelist()
    if filelist:
        stats = do_scan(dst_cfg, filelist, quiet=True)
        print(f"  {dst}: {stats['matched']} matched, {stats['unmatched']} unmatched, "
              f"{stats['outdated']} possibly outdated.")
    else:
        save_config(dst_cfg)
        print(c("dim", f"  Run '{dst} scan' to build its tracking list."))
    print()

# ── Match (interactive) ──────────────────────────────────────────────────────

def cmd_match(args, cfg: dict) -> None:
    filelist = get_filelist()
    if not filelist:
        return
    by_uid = {a["UID"]: a for a in filelist}
    locals_ = {k: v for k, v in cfg["installed"].items() if k.startswith("local:")}
    if not locals_:
        print(c("green", "\n  Nothing to match — every folder is linked.\n"))
        return

    print(f"\n  {len(locals_)} unmatched addon(s).")
    print(c("dim", "  For each: number = pick a suggestion, text = search again,\n"
                   "  'id 123' = use ESOUI ID 123, Enter = skip, q = quit.\n"))
    done, linked = set(), 0

    for n, (key, info) in enumerate(list(locals_.items()), 1):
        if key in done:
            continue
        folders = info.get("folders", [])
        print(c("dim", f"  [{n}/{len(locals_)}]"), c("bold", info["name"]),
              c("dim", "folders: " + ", ".join(folders)))
        cands = candidate_matches(info["name"], folders, filelist, author=info.get("author", ""))
        chosen, quit_now = None, False
        while True:
            for i, (score, a) in enumerate(cands, 1):
                bar = "█" * round(score * 5) + "░" * (5 - round(score * 5))
                print(f"    {c('cyan', str(i))}) {c('cyan', pad('[' + a['UID'] + ']', 7))} "
                      f"{pad(a['UIName'], 34)} {pad(a.get('UIAuthorName', ''), 16)} "
                      f"{c('green' if score >= .8 else 'yellow', bar)}")
            if not cands:
                print(c("yellow", "    No close matches."))
            raw = input(c("cyan", "  > ")).strip()
            if not raw:
                break
            if raw.lower() == "q":
                quit_now = True
                break
            m = re.fullmatch(r"(?:id|#)\s*(\d+)", raw, re.I)
            if m:
                a = by_uid.get(m.group(1))
                if a and input(c("cyan", f"  Link to '{a['UIName']}'? [Y/n] ")).strip().lower() in ("", "y", "yes"):
                    chosen = a
                    break
                if not a:
                    print(c("red", "  No addon with that ID."))
                continue
            if raw.isdigit() and 1 <= int(raw) <= len(cands):
                chosen = cands[int(raw) - 1][1]
                break
            cands = candidate_matches(raw, [], filelist)    # treat as a new search
        if quit_now:
            break
        if not chosen:
            print(c("dim", "    Skipped.\n"))
            continue

        group = list(folders)
        primary = folders[0]
        sibs = [k for k in locals_ if k not in done and k != key and not is_lib(primary)
                and not is_lib(k[6:]) and k[6:].lower().startswith(primary.lower())]
        if sibs:
            names = ", ".join(k[6:] for k in sibs)
            if input(c("cyan", f"  Also include {names}? [Y/n] ")).strip().lower() in ("", "y", "yes"):
                for s in sibs:
                    group += locals_[s].get("folders", [])
                    done.add(s)
        for f in group:
            cfg["manual_links"][f.lower()] = chosen["UID"]
        done.add(key)
        linked += 1
        save_config(cfg)
        print(c("green", f"    ✔ Linked to [{chosen['UID']}] {chosen['UIName']}\n"))

    if linked:
        do_scan(cfg, filelist, quiet=True)
        print(c("dim", f"  {linked} linked. Run 'update' to check them for updates.\n"))

# ── Entry point ──────────────────────────────────────────────────────────────

HEADER_COMMANDS = {"scan", "match", "update", "list", "remove", "install", "ttc", "lock", "unlock", "versions", "backup", "backups", "restore"}
NEEDS_FOLDER    = {"scan", "match", "update", "remove", "ttc"}

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eso-addons", description="Manage Elder Scrolls Online addons from ESOUI.com",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("First run:  setup   (guided),  then  scan, match, update\n"
                "Environments (live is the default):  pts list   |   list --env pts   |   copy live pts"))
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-e", "--env", metavar="ENV",
                        help="environment folder to use: live or pts (default: live)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    s = sub.add_parser("scan", parents=[common], help="Match your AddOns folders to ESOUI automatically")
    s.add_argument("--refresh", action="store_true", help="Re-download the addon list first")
    sub.add_parser("match", parents=[common], help="Interactively link folders scan could not match")
    u = sub.add_parser("update", parents=[common], help="Check for and apply updates")
    u.add_argument("-y", "--yes", action="store_true", help="Don't ask for confirmation")
    bk = sub.add_parser("backup", parents=[common], help="Zip config.json, settings and SavedVariables (live; -e pts for pts)")
    bk.add_argument("--with", dest="with_", metavar="LIST", help="add parts for this run: screenshots, addons (or none)")
    bk.add_argument("--force", action="store_true", help="make a backup even if nothing changed")
    sub.add_parser("backups", parents=[common], help="List the backups of an environment")
    rs = sub.add_parser("restore", parents=[common], help="Restore a backup (asks first; makes a safety copy)")
    rs.add_argument("backup", nargs="?", metavar="NUMBER|FILE|latest")
    rs.add_argument("-y", "--yes", action="store_true", help="Don't ask for confirmation")
    rs.add_argument("--config", action="store_true", help="also restore config.json")
    sub.add_parser("ttc", parents=[common], help="Download the Tamriel Trade Centre price table now")
    ls = sub.add_parser("list", parents=[common], help="Show tracked addons (all, or only those matching an ID or name)")
    ls.add_argument("addons", nargs="*", metavar="ID|NAME", help="show only these (ID, name, or part of a name/folder)")
    q = sub.add_parser("search", parents=[common], help="Search ESOUI by name")
    q.add_argument("query", nargs="+")
    i = sub.add_parser("install", parents=[common], help="Install addon(s) by ESOUI ID")
    i.add_argument("ids", nargs="+", type=int, metavar="ID")
    i.add_argument("-V", "--version", metavar="VERSION",
                   help="install this version instead of the current one (see 'versions ID')")
    i.add_argument("--lock", action="store_true", help="hold the addon at the installed version (update skips it)")
    lk = sub.add_parser("lock", parents=[common], help="Hold addon(s) at the installed version")
    lk.add_argument("addons", nargs="+", metavar="ID|NAME")
    ul = sub.add_parser("unlock", parents=[common], help="Allow updates for locked addon(s) again")
    ul.add_argument("addons", nargs="+", metavar="ID|NAME")
    vs = sub.add_parser("versions", parents=[common], help="List the current and older versions of an addon")
    vs.add_argument("addon", metavar="ID|NAME")
    vs.add_argument("--all", action="store_true", help="show every older version")
    r = sub.add_parser("remove", parents=[common],
                       help="Remove addon(s) by ID, local:Folder, or a folder name")
    r.add_argument("ids", nargs="+", metavar="ID")
    r.add_argument("-y", "--yes", action="count", default=0,
                   help="-y: skip the delete confirmation (saved variables are kept unless the setting is "
                        "'always');  -yy: also delete saved variables, without asking")
    n = sub.add_parser("info", parents=[common], help="Show details for an ESOUI ID")
    n.add_argument("id", type=int)
    cp = sub.add_parser("copy", help="Copy one environment's addons to another (e.g. live → pts)")
    cp.add_argument("envs", nargs="+", metavar="[SOURCE] DEST",
                    help="source and destination; with one name, the default environment is the source")
    cp.add_argument("--saved-vars", action="store_true", help="also copy SavedVariables files")
    cp.add_argument("--mirror", action="store_true",
                    help="also delete addon folders in DEST that are not in SOURCE")
    cp.add_argument("-y", "--yes", action="store_true", help="Don't ask for confirmation")
    sub.add_parser("setup", help="Guided setup: game folder, default version, saved settings")
    cf = sub.add_parser("config", help="Show or change settings")
    cf.add_argument("--set-dir", nargs="+", metavar="PATH",
                    help="The 'Elder Scrolls Online' folder (the one containing live/ and pts/), or "
                         "'search [FOLDER]' to look for it (default: your home folder)")
    cf.add_argument("--saved-vars", choices=("ask", "always", "never"),
                    help="what 'remove' does with an addon's SavedVariables file (default: ask)")
    cf.add_argument("--default-env", metavar="ENV", help="environment used when none is given")
    cf.add_argument("--backup-dir", metavar="PATH",
                    help="where backup zips go ('default' = Documents/ESO Addon Manager Backups)")
    cf.add_argument("--backup-keep", type=int, metavar="N", help="backups to keep per environment (default: 7)")
    cf.add_argument("--backup-with", metavar="LIST",
                    help="extra parts to back up: screenshots, addons (comma separated) or none")
    cf.add_argument("--ttc", choices=("on", "off"),
                    help="download the Tamriel Trade Centre price table during 'update' (default: on)")
    cf.add_argument("--ttc-hours", type=float, metavar="H",
                    help="minimum hours between price table downloads (default: 6)")
    cf.add_argument("--ttc-region", choices=("us", "eu"), help="price table server (default: us)")
    return p

def check_environment(cfg: dict, command: str) -> bool:
    root, env = Path(cfg["root"]), cfg["env"]
    known = set(ENV_NAMES) | set(available_envs(root))
    if env not in known:
        print(c("red", f"  Unknown environment '{env}'.  Known: {', '.join(sorted(known))}\n"))
        return False
    if command in NEEDS_FOLDER and not Path(cfg["addon_dir"]).is_dir():
        if not root.is_dir():
            print(c("red", f"  Elder Scrolls Online folder not found: {root}"))
            print(c("dim", '  Set it with:  config --set-dir "<path>"   or let the tool look:  config --set-dir search\n'))
        else:
            found = available_envs(root)
            print(c("red", f"  No AddOns folder for '{env}': {cfg['addon_dir']}"))
            print(c("dim", f"  Environments found: {', '.join(found) if found else 'none'}\n"))
        return False
    if command == "install" and not root.is_dir():
        print(c("red", f"  Elder Scrolls Online folder not found: {root}"))
        print(c("dim", '  Set it with:  config --set-dir "<path>"   or let the tool look:  config --set-dir search\n'))
        return False
    return True

LOCK_COMMANDS = {"scan", "match", "update", "install", "remove", "copy", "ttc", "setup", "lock", "unlock", "backup", "restore"}

def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return True                                       # can't tell cheaply; the age check decides
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True

def acquire_lock() -> Path | None:
    """One changing command at a time (a timer run and a manual run must not overlap)."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    lock = APP_DIR / "run.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or 0)
                stale = (not _pid_alive(pid)) or time.time() - lock.stat().st_mtime > 6 * 3600
            except (OSError, ValueError):
                stale = True
            if not stale:
                return None
            try:
                lock.unlink()
            except OSError:
                return None
    return None

def main() -> None:
    parser = build_parser()
    argv = sys.argv[1:]
    prefix_env = None
    if len(argv) > 1 and argv[0].lower() in ENV_NAMES:      # "pts list"
        prefix_env, argv = argv[0].lower(), argv[1:]
    args = parser.parse_args(argv)
    handlers = {"scan": cmd_scan, "match": cmd_match, "update": cmd_update,
                "list": cmd_list, "search": cmd_search, "install": cmd_install,
                "remove": cmd_remove, "info": cmd_info, "config": cmd_config,
                "copy": cmd_copy, "setup": cmd_setup, "ttc": cmd_ttc,
                "lock": cmd_lock, "unlock": cmd_unlock, "versions": cmd_versions,
                "backup": cmd_backup, "backups": cmd_backups, "restore": cmd_restore}
    print(banner())
    if args.command not in handlers:
        parser.print_help()
        return
    env = (getattr(args, "env", None) or prefix_env or "").lower() or None
    first_run = not have_config()
    lock = None
    if args.command in LOCK_COMMANDS:
        lock = acquire_lock()
        if lock is None:
            print(c("yellow", "  Another eso-addons command is already running — try again in a minute.\n"))
            sys.exit(1)
    try:
        cfg = load_config(env)
        if first_run and args.command in HEADER_COMMANDS and is_interactive():
            cfg = first_run_setup(cfg, env)
        if args.command not in ("config", "copy", "setup"):
            cfg = offer_search(cfg, args.command, env)
            if not check_environment(cfg, args.command):
                sys.exit(1)
            if args.command in HEADER_COMMANDS:
                print(f"  Environment: {c('bold', cfg['env'])}  {c('dim', cfg['addon_dir'])}")
        handlers[args.command](args, cfg)
    except KeyboardInterrupt:
        print(c("dim", "\n  Cancelled.\n"))
        sys.exit(130)
    finally:
        if lock:
            try:
                lock.unlink()
            except OSError:
                pass

def run() -> None:
    """Entry point: exit code 0 = fine, 1 = something failed (so systemd/cron can tell)."""
    main()
    if FAILURES:
        problems = list(dict.fromkeys(FAILURES))
        print(c("red", f"  ✗ Finished with {len(problems)} problem(s):"))
        for f in problems:
            print(c("red", f"    - {f}"))
        print()
        sys.exit(1)

if __name__ == "__main__":
    run()
