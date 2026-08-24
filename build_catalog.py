import os, re, json, sys, time, yaml, threading
from collections import Counter
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


GITHUB_API   = "https://api.github.com"
GITHUB_RAW   = "https://raw.githubusercontent.com/microsoft/winget-pkgs/master"
WINGET_REPO  = "microsoft/winget-pkgs"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
WORKERS      = int(os.environ.get("FOSSY_WORKERS", "16"))
CACHE_FILE   = Path(".fossy-cache.json")
HTTP_CACHE   = Path(".fossy-http-cache.json")
VERBOSE      = os.environ.get("FOSSY_VERBOSE", "") not in ("", "0")
ICONS_DIR    = Path("icons")

# Lower index = preferred when multiple installers are available
INSTALLER_RANK: dict[str, int] = {
    "msi": 0, "wix": 1, "burn": 2,
    "inno": 3,
    "nullsoft": 4, "nsis": 4,
    "exe": 5,
    "msix": 6, "appx": 7,
    "msixbundle": 8, "appxbundle": 8,
    "zip": 9, "7z": 9,
    "portable": 10,
    "squirrel": 11,
    "nupkg": 12,
}

ARCH_RANK: dict[str, int] = {
    "x64": 0, "neutral": 1, "x86": 2, "arm64": 3, "arm": 4,
}

SILENT_SWITCHES: dict[str, str] = {
    "msi":        "/quiet /norestart",
    "wix":        "/quiet /norestart",
    "burn":       "/quiet /norestart",
    "inno":       "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-",
    "nullsoft":   "/S",
    "nsis":       "/S",
    "exe":        "/S",
    "squirrel":   "--silent",
}

# URL path extension → installer type; ordered longest-suffix-first to avoid
# ".zip" matching ".appxbundle" etc.
URL_EXT_MAP: list[tuple[str, str]] = [
    (".msixbundle", "msixbundle"),
    (".appxbundle", "appxbundle"),
    (".msix",       "msix"),
    (".appx",       "appx"),
    (".nupkg",      "nupkg"),
    (".msi",        "msi"),
    (".exe",        "exe"),
    (".7z",         "7z"),
    (".rar",        "zip"),
    (".zip",        "zip"),
]

OSS_LICENSES = {
    "agpl", "agpl-3.0",
    "apache", "apache-2.0",
    "artistic",
    "boost",
    "bsd", "bsd-2-clause", "bsd-3-clause",
    "cc0",
    "cddl",
    "curl",
    "epl", "epl-2.0",
    "eupl", "eupl-1.2",
    "gpl", "gpl-2.0", "gpl-3.0",
    "isc",
    "lgpl", "lgpl-2.0", "lgpl-2.1", "lgpl-3.0",
    "mit",
    "mozilla", "mpl", "mpl-2.0",
    "ms-pl", "ms-rl",
    "psf", "python-2.0",
    "unlicense",
    "wtfpl",
    "zlib",
}

# Common winget spellings collapsed to allowlist families before tokenizing
LICENSE_PHRASES = [
    (r"\bgnu\s+(lesser|library)\s+general\s+public\s+license\b", "lgpl"),
    (r"\bgnu\s+affero\s+general\s+public\s+license\b", "agpl"),
    (r"\b(gnu\s+)?general\s+public\s+license\b", "gpl"),
    (r"\bmozilla\s+public\s+license\b", "mpl"),
    (r"\bapache\s+(software\s+)?license\b", "apache"),
    (r"\bthe\s+unlicense\b", "unlicense"),
]

LICENSE_ALIAS = {
    "gplv1": "gpl", "gplv2": "gpl", "gplv3": "gpl",
    "lgplv2": "lgpl", "lgplv21": "lgpl", "lgplv3": "lgpl",
    "agplv3": "agpl",
    "mpl2": "mpl", "mpl20": "mpl",
    "apache2": "apache",
}

FAMILY_RE = re.compile(
    r"^(?:(a|l)?gpl|m(b?sd|it|pl|ozilla)|apache|boost|zlib|wtfpl|isc|artistic|psf)"
)

STABLE_RE = re.compile(r"(beta|alpha|rc(?![a-z])|nightly|weekly|dev|preview|snapshot)", re.I)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/vnd.github.v3+json"})
    if GITHUB_TOKEN:
        s.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    retry = Retry(
        total=4,
        backoff_factor=2.0,
        status_forcelist={429, 500, 502, 503, 504},
        allowed_methods={"GET"},
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


_local = threading.local()

def sess() -> requests.Session:
    if not hasattr(_local, "s"):
        _local.s = _session()
    return _local.s


_http_cache: dict = {}
_http_lock = threading.Lock()
_http_dirty = False


def load_caches() -> None:
    global _http_cache
    if HTTP_CACHE.exists():
        try:
            data = json.loads(HTTP_CACHE.read_text(encoding="utf-8"))
            if data.get("__fmt") == 2:
                _http_cache = data.get("urls", {})
                print(f"HTTP cache: {len(_http_cache)} urls")
        except Exception:
            pass


def save_caches() -> None:
    global _http_dirty
    if _http_dirty:
        HTTP_CACHE.write_text(
            json.dumps({"__fmt": 2, "urls": _http_cache}, separators=(",", ":")),
            encoding="utf-8",
        )


def gh_api(url: str):
    """GET an api.github.com JSON url with ETag conditional caching."""
    global _http_dirty
    ent = _http_cache.get(url)
    headers = {}
    if ent and ent.get("etag"):
        headers["If-None-Match"] = ent["etag"]
    r = sess().get(url, timeout=25, headers=headers)
    if r.status_code == 304 and ent:
        try:
            return True, json.loads(ent["body"])
        except Exception:
            pass
    if not r.ok:
        return False, r.status_code
    if r.headers.get("ETag"):
        with _http_lock:
            _http_cache[url] = {"etag": r.headers["ETag"], "body": r.text}
            _http_dirty = True
    try:
        return True, r.json()
    except Exception:
        return False, "bad json"


def gh_list(path: str):
    ok, data = gh_api(f"{GITHUB_API}/repos/{WINGET_REPO}/contents/{path}")
    if not ok or isinstance(data, int):
        return None
    return data


def detect_type(manifest_type: str, url: str) -> str:
    if manifest_type:
        return manifest_type.lower()
    path = unquote(urlparse(url).path).lower()
    for ext, typ in URL_EXT_MAP:
        if path.endswith(ext):
            return typ
    return "exe"


def norm_license_text(s: str) -> list[str]:
    s = re.sub(r"[-+](or-later|only|or-compatible|and-later)", "", s.lower())
    for pat, repl in LICENSE_PHRASES:
        s = re.sub(pat, repl, s)
    tokens = re.split(r"[\s,/|()+\[\]]+", s)
    out = []
    for t in tokens:
        t = t.strip("-.")
        if not t:
            continue
        t = LICENSE_ALIAS.get(t, t)
        out.append(t)
    return out


def token_is_oss(t: str) -> bool:
    if t in OSS_LICENSES:
        return True
    if FAMILY_RE.match(t):
        return True
    return False


def is_oss(license_str: str) -> bool:
    if not license_str:
        return False
    return any(token_is_oss(t) for t in norm_license_text(license_str))


def ver_key(v: str):
    nums = tuple(int(n) for n in re.findall(r"\d+", v))
    stable = 0 if STABLE_RE.search(v) else 1
    if not nums:
        return None
    return (stable,) + nums


def pick_newest(names: list[str]) -> str | None:
    keys = [(ver_key(n), n) for n in names]
    keys = [k for k in keys if k[0] is not None]
    if not keys:
        return None
    return max(keys, key=lambda k: k[0])[1]


def resolve_version_dir(app_id: str) -> tuple[str | None, str | None]:
    """Walk down from the app folder into the newest leaf that holds yaml."""
    parts = app_id.split(".")
    folder = f"manifests/{parts[0][0].lower()}/{'/'.join(parts)}"

    def yaml_present(items) -> bool:
        return any(i["type"] == "file" and i["name"].endswith(".yaml") for i in items)

    cur = folder
    for _ in range(6):
        items = gh_list(cur)
        if items is None:
            return None, "listing-failed"
        if yaml_present(items):
            return cur, None
        dirs = [i["name"] for i in items if i["type"] == "dir"]
        nxt = pick_newest(dirs)
        if not nxt:
            return None, "no-version-folder"
        cur = f"{cur}/{nxt}"
    return None, "too-deep"


def resolve_id_fuzzy(app_id: str) -> str | None:
    """Case-insensitive walk of the letter index to catch winget renames."""
    parts = app_id.split(".")
    letter = parts[0][0].lower()
    pubs = gh_list(f"manifests/{letter}")
    if not pubs:
        return None
    want_pub = parts[0].lower().replace("-", "")
    pub = next((p["name"] for p in pubs if p["type"] == "dir"
                and p["name"].lower().replace("-", "") == want_pub), None)
    if not pub:
        return None
    apps = gh_list(f"manifests/{letter}/{pub}")
    if not apps:
        return None
    want_app = ".".join(parts[1:]).lower()
    app = next((a["name"] for a in apps if a["type"] == "dir"
                and a["name"].lower() == want_app), None)
    if not app:
        return None
    resolved = f"{pub}.{app}"
    return None if resolved.lower() != app_id.lower() else resolved


def load_yaml_url(url: str | None) -> dict:
    if not url:
        return {}
    r = sess().get(url, timeout=25)
    if not r.ok:
        return {}
    try:
        return yaml.safe_load(r.text) or {}
    except yaml.YAMLError:
        return {}


def fetch_manifests(folder_path: str, app_id: str) -> tuple[dict, dict, dict]:
    base = f"{GITHUB_RAW}/{folder_path}"
    ident = app_id

    def raw(name: str) -> dict:
        return load_yaml_url(f"{base}/{name}")

    locale = raw(f"{ident}.locale.en-US.yaml")
    inst   = raw(f"{ident}.installer.yaml")
    ver    = raw(f"{ident}.yaml")

    # Fallback: constructed names missed → discover via folder listing
    if not (locale or inst or ver):
        items = gh_list(folder_path)
        if not items:
            return {}, {}, {}
        files = {i["name"]: i["download_url"] for i in items if i["type"] == "file"}

        def by_suffix(suffix: str):
            return next((u for n, u in files.items() if n.endswith(suffix)), None)

        locale = load_yaml_url(by_suffix(".locale.en-US.yaml"))
        inst   = load_yaml_url(by_suffix(".installer.yaml"))
        vname  = next((n for n in files
                       if n.endswith(".yaml")
                       and ".installer." not in n and ".locale." not in n), None)
        ver    = load_yaml_url(files.get(vname))

    return locale, inst, ver


def best_installer(installers: list[dict]) -> dict | None:
    if not installers:
        return None

    def rank(i: dict) -> tuple[int, int]:
        arch = i.get("Architecture", "").lower()
        typ  = detect_type(i.get("InstallerType", ""), i.get("InstallerUrl", ""))
        return (ARCH_RANK.get(arch, 99), INSTALLER_RANK.get(typ, 99))

    return min(installers, key=rank)


def silent_args(chosen: dict, installer_manifest: dict) -> str:
    for src in (chosen.get("InstallerSwitches", {}),
                installer_manifest.get("InstallerSwitches", {})):
        if src:
            sw = src.get("Silent") or src.get("SilentWithProgress")
            if sw:
                return sw
    typ = detect_type(chosen.get("InstallerType", ""), chosen.get("InstallerUrl", ""))
    return SILENT_SWITCHES.get(typ, "")


def github_owner(*urls: str | None) -> str | None:
    for u in urls:
        if not u:
            continue
        p = urlparse(u)
        if p.netloc.removeprefix("www.") in ("github.com", "avatars.githubusercontent.com"):
            seg = [s for s in p.path.split("/") if s]
            if seg:
                return seg[0]
    return None


def slugify(app_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", app_id.lower()).strip("-")


def fetch_favicon(domain: str) -> bytes | None:
    for url in (
        f"https://icons.duckduckgo.com/ip3/{domain}.ico",
        f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
    ):
        try:
            r = sess().get(url, timeout=12)
            if r.ok and r.content and len(r.content) > 800:
                return r.content
        except Exception:
            continue
    return None


ICON_MAGIC = (
    (b"\x89PNG", "png"),
    (b"\x00\x00\x01\x00", "ico"),
    (b"\xff\xd8", "jpg"),
    (b"GIF8", "gif"),
)


def _icon_ext(data: bytes) -> str | None:
    for magic, ext in ICON_MAGIC:
        if data.startswith(magic):
            return ext
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def resolve_icon(app_id: str, meta: dict, chosen: dict) -> dict | None:
    owner = github_owner(meta.get("PackageUrl"),
                         meta.get("PublisherUrl"),
                         chosen.get("InstallerUrl"))
    data = None
    url = None
    if owner:
        url = f"https://avatars.githubusercontent.com/{owner}?size=256"
        try:
            r = sess().get(url, timeout=15)
            data = r.content if r.ok else None
        except Exception:
            data = None
    if not data or not _icon_ext(data):
        dom = urlparse(meta.get("PackageUrl", "")).netloc
        data, url = None, None
        if dom:
            data = fetch_favicon(dom)
            url = f"https://icons.duckduckgo.com/ip3/{dom}.ico"
    ext = _icon_ext(data) if data else None
    if not ext:
        return None
    ICONS_DIR.mkdir(exist_ok=True)
    # clear any stale variant from a previous run
    for old in ICONS_DIR.glob(f"{slugify(app_id)}.*"):
        old.unlink(missing_ok=True)
    fname = f"{slugify(app_id)}.{ext}"
    try:
        (ICONS_DIR / fname).write_bytes(data)
    except Exception:
        return None
    return {"file": f"icons/{fname}", "url": url}


APP_CACHE: dict = {}
_cache_lock = threading.Lock()


def load_entry_cache() -> None:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if data.get("__fmt") == 3:
                APP_CACHE.update(data.get("entries", {}))
                print(f"Entry cache: {len(APP_CACHE)} apps")
        except Exception:
            pass


def save_entry_cache() -> None:
    CACHE_FILE.write_text(
        json.dumps({"__fmt": 3, "entries": APP_CACHE}, separators=(",", ":")),
        encoding="utf-8",
    )


def build_entry_winget(app_id: str, cfg: dict, report) -> dict | None:
    folder, err = resolve_version_dir(app_id)
    if err == "listing-failed":
        alt = resolve_id_fuzzy(app_id)
        if alt:
            print(f"  ↻  {app_id} → {alt}")
            app_id, cfg = alt, {**cfg}
            folder, err = resolve_version_dir(app_id)
    if err:
        report.fail(app_id, "id_resolve", err)
        return None

    cache_key = f"{app_id}:{folder}"
    with _cache_lock:
        if cache_key in APP_CACHE:
            return APP_CACHE[cache_key]

    locale, inst_m, ver_m = fetch_manifests(folder, app_id)
    if not (locale or inst_m or ver_m):
        report.fail(app_id, "manifest_fetch", "no manifests readable")
        return None
    meta = {**ver_m, **locale}

    lic = cfg.get("license_override") or meta.get("License", "")
    if not is_oss(lic):
        report.fail(app_id, "license_filter", f"not OSS: {lic!r}")
        return None

    installers = (inst_m.get("Installers") or ver_m.get("Installers") or [])
    chosen = best_installer(installers)
    if not chosen or not chosen.get("InstallerUrl"):
        report.fail(app_id, "installer_pick", "no usable installer")
        return None

    typ = detect_type(chosen.get("InstallerType", ""), chosen["InstallerUrl"])
    entry = _assemble(app_id, cfg, meta, chosen, typ, inst_m)
    icon = resolve_icon(app_id, meta, chosen)
    if icon:
        entry["icon"] = icon
    elif VERBOSE:
        print(f"  ⚠  {app_id}: no icon")

    with _cache_lock:
        APP_CACHE[cache_key] = entry
    return entry


def _assemble(app_id: str, cfg: dict, meta: dict, chosen: dict,
              typ: str, inst_m: dict) -> dict:
    return {
        "id":          app_id,
        "name":        meta.get("PackageName", app_id.split(".")[-1]),
        "version":     str(meta.get("PackageVersion") or "unknown"),
        "publisher":   meta.get("Publisher", app_id.split(".")[0]),
        "description": (meta.get("ShortDescription") or meta.get("Description") or "").strip(),
        "license":     meta.get("License") or cfg.get("license_override", ""),
        "homepage":    meta.get("PackageUrl", ""),
        "category":    cfg["category"],
        "tags":        meta.get("Tags") or [],
        "installer": {
            "url":          chosen["InstallerUrl"],
            "type":         typ,
            "architecture": chosen.get("Architecture", "x64"),
            "sha256":       chosen.get("InstallerSha256", ""),
            "silentArgs":   silent_args(chosen, inst_m),
        },
    }


ASSET_EXCLUDE = re.compile(r"(pdb|sig($|\.)|blockmap|checksum|sha256|symbols|debug|\.txt$)", re.I)
ARCH_HINTS = (("amd64", "x64", "win64", "86-64", "x86_64"), ("arm64",), ("win32", "x86", "ia32"))


def asset_arch(name: str) -> str:
    n = name.lower()
    for h in ARCH_HINTS[0]:
        if h in n:
            return "x64"
    for h in ARCH_HINTS[1]:
        if h in n:
            return "arm64"
    for h in ARCH_HINTS[2]:
        if h in n:
            return "x86"
    return "neutral"


WIN_EXTS = {".msi", ".exe", ".msix", ".appx", ".msixbundle", ".appxbundle"}


def is_windows_asset(name: str) -> bool:
    n = name.lower()
    if any(n.endswith(e) for e in WIN_EXTS):
        return True
    # Archives are only accepted when the name says Windows
    return "win" in n and any(n.endswith(e) for e, _ in URL_EXT_MAP)


def _windows_assets(rel: dict) -> list[dict]:
    return [a for a in rel.get("assets", [])
            if is_windows_asset(a["name"])
            and not ASSET_EXCLUDE.search(a["name"])]


def pick_release(repo: str):
    """Newest stable release that actually ships Windows assets."""
    ok, rel = gh_api(f"{GITHUB_API}/repos/{repo}/releases/latest")
    if ok and isinstance(rel, dict) and not rel.get("prerelease") \
            and not rel.get("draft"):
        assets = _windows_assets(rel)
        if assets:
            return rel, assets
    ok, rels = gh_api(f"{GITHUB_API}/repos/{repo}/releases?per_page=30")
    if ok and isinstance(rels, list):
        for rel in rels:
            if rel.get("draft") or rel.get("prerelease"):
                continue
            assets = _windows_assets(rel)
            if assets:
                return rel, assets
    return None, []


def tag_version(tag: str) -> str:
    m = re.search(r"(\d[\d\.]*[a-z0-9]*(?:[\.+_-][a-z0-9]+)*)$", tag or "", re.I)
    return m.group(1) if m else (tag or "unknown")


def build_entry_release(app_id: str, cfg: dict, report) -> dict | None:
    repo = cfg["repo"]
    rel, assets = pick_release(repo)
    if not rel:
        report.fail(app_id, "installer_pick", "no stable release with Windows assets")
        return None
    if not assets:
        report.fail(app_id, "installer_pick", "no installer asset in release")
        return None

    def arank(a: dict) -> tuple[int, int]:
        n = a["name"].lower()
        ext = next((INSTALLER_RANK[t] for e, t in URL_EXT_MAP
                    if n.endswith(e)), 99)
        arch_pen = 0 if any(h in n for h in ARCH_HINTS[0]) else (
            2 if any(h in n for h in ARCH_HINTS[2]) else 1)
        avx = 1 if "avx" in n else 0
        return (arch_pen, ext * 2 + avx)

    asset = min(assets, key=arank)
    ok, repo_meta = gh_api(f"{GITHUB_API}/repos/{repo}")
    lic = cfg.get("license_override") or ""
    desc, home = "", f"https://github.com/{repo}"
    if ok and isinstance(repo_meta, dict):
        spdx = ((repo_meta.get("license") or {}).get("spdx_id") or "")
        lic = lic or spdx
        desc = repo_meta.get("description") or ""
        home = repo_meta.get("html_url") or home
    if not is_oss(lic):
        report.fail(app_id, "license_filter", f"not OSS: {lic!r}")
        return None

    typ = detect_type("", asset["browser_download_url"])
    chosen = {"InstallerUrl": asset["browser_download_url"], "Architecture": asset_arch(asset["name"])}
    meta = {
        "PackageName":  cfg.get("name", app_id.split(".")[-1]),
        "PackageVersion": tag_version(rel.get("tag_name", "")),
        "Publisher":    cfg.get("publisher", repo.split("/")[0]),
        "ShortDescription": desc,
        "PackageUrl":   home,
        "License":      lic,
    }
    entry = _assemble(app_id, cfg, meta, chosen, typ, {})
    icon = resolve_icon(app_id, meta, chosen)
    if icon:
        entry["icon"] = icon
    return entry


class Report:
    def __init__(self):
        self._lock = threading.Lock()
        self.failed: list[dict] = []

    def fail(self, app_id: str, stage: str, reason: str) -> None:
        with self._lock:
            self.failed.append({"id": app_id, "stage": stage, "reason": str(reason)[:160]})
        if VERBOSE:
            print(f"  ✗  {app_id} [{stage}] {reason}")


def write_outputs(ok: list[dict], elapsed: float, report: Report) -> None:
    ok_sorted = sorted(ok, key=lambda e: (e["category"], e["name"].lower()))

    Path("fossy-catalog.json").write_text(
        json.dumps({
            "version":   "1.1",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total":     len(ok_sorted),
            "apps":      ok_sorted,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    Path("fossy-index.json").write_text(
        json.dumps([
            {
                "id":       e["id"],
                "name":     e["name"],
                "version":  e["version"],
                "category": e["category"],
                "homepage": e["homepage"],
                "icon":     e.get("icon", {}).get("file", ""),
            }
            for e in ok_sorted
        ], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    cat_dir = Path("catalog")
    cat_dir.mkdir(exist_ok=True)
    by_cat: dict[str, list[dict]] = {}
    for e in ok_sorted:
        by_cat.setdefault(e["category"], []).append(e)
    for cat, entries in by_cat.items():
        (cat_dir / f"{cat.replace(' ', '_')}.json").write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    Path("build-report.json").write_text(
        json.dumps({
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "built":     len(ok_sorted),
            "failed":    sorted(report.failed, key=lambda f: f["id"]),
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    _gha_summary(ok_sorted, elapsed, report)


def _gha_summary(ok: list[dict], elapsed: float, report: Report) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    cats = Counter(e["category"] for e in ok)
    lines = [
        "## 📦 Fossy Catalog Build",
        "",
        "| | |",
        "|---|---|",
        f"| ✅ Built | **{len(ok)}** |",
        f"| ❌ Failed | **{len(report.failed)}** |",
        f"| ⏱ Elapsed | {elapsed:.1f}s |",
        "",
        "### By Category",
        "| Category | Count |",
        "|----------|------:|",
        *[f"| {cat} | {n} |" for cat, n in sorted(cats.items())],
    ]
    if report.failed:
        lines += ["", "### Failed", "",
                  "| App | Stage | Reason |", "|---|---|---|"]
        lines += [f"| `{f['id']}` | {f['stage']} | {f['reason']} |"
                  for f in sorted(report.failed, key=lambda f: f["id"])]

    Path(summary_path).write_text("\n".join(lines), encoding="utf-8")

    gha_out = os.environ.get("GITHUB_OUTPUT")
    if gha_out:
        with open(gha_out, "a") as fh:
            fh.write(f"built={len(ok)}\nfailed={len(report.failed)}\n")


APPS: dict[str, dict] = {
    # ── Browsers ──────────────────────────────────────────────────────────────
    "Mozilla.Firefox":             {"category": "Browsers"},
    "Brave.Brave":                 {"category": "Browsers"},
    "LibreWolf.LibreWolf":         {"category": "Browsers"},
    "UngoogledChromium.UngoogledChromium": {"category": "Browsers", "name": "Ungoogled Chromium", "source": "github-releases", "repo": "ungoogled-software/ungoogled-chromium-windows"},
    "Waterfox.Waterfox":           {"category": "Browsers"},

    # ── Productivity ──────────────────────────────────────────────────────────
    # LibreOffice dropped: purged from winget-pkgs, no Windows assets on GitHub
    "ONLYOFFICE.DesktopEditors":   {"category": "Productivity"},
    "Joplin.Joplin":               {"category": "Productivity"},
    "StandardNotes.StandardNotes": {"category": "Productivity"},
    "ToEverything.AFFiNE":         {"category": "Productivity", "license_override": "mit"},
    "MarkText.MarkText":           {"category": "Productivity"},
    "AppFlowy.AppFlowy":           {"category": "Productivity"},

    # ── Media ─────────────────────────────────────────────────────────────────
    "VideoLAN.VLC":                {"category": "Media"},
    "OBSProject.OBSStudio":        {"category": "Media"},
    "Audacity.Audacity":           {"category": "Media"},
    "HandBrake.HandBrake":         {"category": "Media"},
    "KDE.Kdenlive":                {"category": "Media"},
    "jurplel.qView":               {"category": "Media"},
    "MoritzBunkus.MKVToolNix":     {"category": "Media"},
    "Stremio.Stremio":             {"category": "Media"},
    "clsid2.mpc-hc":               {"category": "Media"},

    # ── Design ────────────────────────────────────────────────────────────────
    "Inkscape.Inkscape":           {"category": "Design"},
    "GIMP.GIMP":                   {"category": "Design"},
    "KDE.Krita":                   {"category": "Design"},
    "BlenderFoundation.Blender":   {"category": "Design"},
    "darktable.darktable":         {"category": "Design"},
    "RawTherapee.RawTherapee":     {"category": "Design"},
    "FreeCAD.FreeCAD":             {"category": "Design"},
    "OpenSCAD.OpenSCAD":           {"category": "Design"},
    "LibreCAD.LibreCAD":           {"category": "Design"},

    # ── Security & Privacy ────────────────────────────────────────────────────
    "KeePassXCTeam.KeePassXC":     {"category": "Security"},
    "IDRIX.VeraCrypt":             {"category": "Security", "source": "github-releases", "repo": "veracrypt/VeraCrypt", "license_override": "apache-2.0"},
    "Proton.ProtonVPN":            {"category": "Security"},
    "Bitwarden.Bitwarden":         {"category": "Security"},
    "GnuPG.GnuPG":                 {"category": "Security"},
    "Cryptomator.Cryptomator":     {"category": "Security"},
    "i2p.I2PEasyInstallBundle":    {"category": "Security", "license_override": "gpl-2.0"},
    "Henry++.simplewall":          {"category": "Security"},
    "TorProject.TorBrowser":       {"category": "Security"},

    # ── Utilities ─────────────────────────────────────────────────────────────
    "7zip.7zip":                   {"category": "Utilities"},
    "Rufus.Rufus":                 {"category": "Utilities"},
    "Balena.Etcher":               {"category": "Utilities"},
    "CrystalDewWorld.CrystalDiskInfo": {"category": "Utilities"},
    "CrystalDewWorld.CrystalDiskMark": {"category": "Utilities"},
    "WinSCP.WinSCP":               {"category": "Utilities"},
    "PuTTY.PuTTY":                 {"category": "Utilities"},
    "BleachBit.BleachBit":         {"category": "Utilities"},
    "Ventoy.Ventoy":               {"category": "Utilities"},
    "uvncbvba.UltraVNC":           {"category": "Utilities"},
    "File-New-Project.EarTrumpet": {"category": "Utilities"},
    "WinDirStat.WinDirStat":       {"category": "Utilities"},
    "Gyan.FFmpeg":                 {"category": "Utilities"},
    "jqlang.jq":                   {"category": "Utilities"},
    "cURL.cURL":                   {"category": "Utilities", "license_override": "curl"},
    "hluk.CopyQ":                  {"category": "Utilities"},
    "Microsoft.PowerShell":        {"category": "Utilities"},

    # ── Development ───────────────────────────────────────────────────────────
    "Git.Git":                     {"category": "Development"},
    "Notepad++.Notepad++":         {"category": "Development"},
    "VSCodium.VSCodium":           {"category": "Development"},
    "Python.Python":               {"category": "Development"},
    "WiresharkFoundation.Wireshark": {"category": "Development"},
    "Rustlang.Rustup":             {"category": "Development"},
    "GoLang.Go":                   {"category": "Development"},
    "OpenJS.NodeJS.LTS":           {"category": "Development"},
    "GnuPG.Gpg4win":               {"category": "Development"},
    "HeidiSQL.HeidiSQL":           {"category": "Development"},
    "DBeaver.DBeaver.Community":   {"category": "Development"},
    "GitHub.GitHubDesktop":        {"category": "Development"},
    "Meld.Meld":                   {"category": "Development"},
    "astral-sh.uv":                {"category": "Development"},
    "yt-dlp.yt-dlp":               {"category": "Development"},

    # ── Communication ─────────────────────────────────────────────────────────
    "Mozilla.Thunderbird":         {"category": "Communication"},
    "Element.Element":             {"category": "Communication"},
    # Signal dropped: purged from winget, GitHub releases carry no Windows assets
    "Jitsi.Meet":                  {"category": "Communication"},
    "Mattermost.MattermostDesktop": {"category": "Communication"},
    "RocketChat.RocketChat":       {"category": "Communication"},

    # ── File Sharing ──────────────────────────────────────────────────────────
    "qBittorrent.qBittorrent":     {"category": "File Sharing"},
    "Syncthing.Syncthing":         {"category": "File Sharing"},
    "Nextcloud.NextcloudDesktop":  {"category": "File Sharing"},
    "Rclone.Rclone":               {"category": "File Sharing"},
    "ShareX.ShareX":               {"category": "File Sharing"},
    "LocalSend.LocalSend":         {"category": "File Sharing"},

    # ── Games ─────────────────────────────────────────────────────────────────
    "HeroicGamesLauncher.HeroicGamesLauncher": {"category": "Games"},
    "ItchIo.Itch":                 {"category": "Games"},

    # ── Virtualization ────────────────────────────────────────────────────────
    # Vagrant dropped: purged from winget, GitHub releases carry no Windows assets
    "Oracle.VirtualBox":           {"category": "Virtualization"},
}


def process(app_id: str, cfg: dict, ok: list, report: Report, lock: threading.Lock) -> None:
    try:
        src = cfg.get("source", "winget")
        builder = build_entry_release if src == "github-releases" else build_entry_winget
        entry = builder(app_id, cfg, report)
        with lock:
            if entry:
                ok.append(entry)
                print(f"  ✓  {entry['name']:<42} {entry['version']:>14}")
            elif not VERBOSE:
                print(f"  ✗  {app_id}")
    except Exception as e:
        report.fail(app_id, "exception", repr(e))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    load_caches()
    load_entry_cache()
    print(f"Fossy  |  {len(APPS)} apps  |  {WORKERS} workers"
          + ("" if GITHUB_TOKEN else "\n⚠  No GITHUB_TOKEN — rate limited to 60 req/hr"))

    rl_ok, rl = gh_api(f"{GITHUB_API}/rate_limit")
    if rl_ok and isinstance(rl, dict):
        core = rl["resources"]["core"]
        print(f"API quota: {core['remaining']}/{core['limit']}")

    ok: list[dict] = []
    report = Report()
    lock = threading.Lock()
    t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(process, aid, cfg, ok, report, lock)
                   for aid, cfg in APPS.items()]
        for _ in as_completed(futures):
            pass

    elapsed = time.monotonic() - t0
    write_outputs(ok, elapsed, report)
    save_entry_cache()
    save_caches()

    print(f"\n{'─' * 56}")
    print(f"Done in {elapsed:.1f}s  |  {len(ok)} built  |  {len(report.failed)} failed")
    if report.failed:
        for f in sorted(report.failed, key=lambda f: f["id"]):
            print(f"  ✗  {f['id']:<44} [{f['stage']}] {f['reason']}")
    print("Outputs: fossy-catalog.json  fossy-index.json  catalog/  icons/  build-report.json")


if __name__ == "__main__":
    main()
