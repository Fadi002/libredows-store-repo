import os, re, json, time, yaml, threading
from collections import Counter
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


GITHUB_API   = "https://api.github.com"
WINGET_REPO  = "microsoft/winget-pkgs"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
WORKERS      = int(os.environ.get("FOSSY_WORKERS", "16"))
CACHE_FILE   = Path(".fossy-cache.json")

OSS_LICENSES = {
    "agpl", "agpl-3.0",
    "apache", "apache-2.0",
    "artistic",
    "boost",
    "bsd", "bsd-2-clause", "bsd-3-clause",
    "cc0",
    "cddl",
    "epl", "epl-2.0",
    "eupl", "eupl-1.2",
    "gpl", "gpl-2.0", "gpl-3.0",
    "isc",
    "lgpl", "lgpl-2.1", "lgpl-3.0",
    "mit",
    "mozilla", "mpl", "mpl-2.0",
    "ms-pl", "ms-rl",
    "psf", "python-2.0",
    "unlicense",
    "wtfpl",
    "zlib",
}

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

APPS: dict[str, str] = {
    # ── Browsers ──────────────────────────────────────────────────────────────
    "Mozilla.Firefox":              "Browsers",
    "Brave.Brave":                  "Browsers",
    "LibreWolf.LibreWolf":          "Browsers",
    "Ungoogled.UngoogledChromium":  "Browsers",
    "Thorium.Thorium":              "Browsers",
    "Waterfox.Waterfox":            "Browsers",

    # ── Productivity ──────────────────────────────────────────────────────────
    "LibreOffice.LibreOffice":      "Productivity",
    "ONLYOFFICE.DesktopEditors":    "Productivity",
    "Obsidian.Obsidian":            "Productivity",
    "Joplin.Joplin":                "Productivity",
    "CherryTree.CherryTree":        "Productivity",
    "StandardNotes.StandardNotes":  "Productivity",
    "AFFiNE.AFFiNE":                "Productivity",
    "MarkText.MarkText":            "Productivity",
    "AppFlowy.AppFlowy":            "Productivity",

    # ── Media ─────────────────────────────────────────────────────────────────
    "VideoLAN.VLC":                 "Media",
    "OBSProject.OBSStudio":         "Media",
    "Audacity.Audacity":            "Media",
    "HandBrake.HandBrake":          "Media",
    "KDE.Kdenlive":                 "Media",
    "mkvtoolnix.mkvtoolnix":        "Media",
    "Stremio.Stremio":              "Media",
    "clsid2.mpc-hc":                "Media",
    "CodecGuide.K-LiteCodecPack.Standard": "Media",

    # ── Design ────────────────────────────────────────────────────────────────
    "Inkscape.Inkscape":            "Design",
    "GIMP.GIMP":                    "Design",
    "Krita.Krita":                  "Design",
    "Blender.Blender":              "Design",
    "darktable.darktable":          "Design",
    "RawTherapee.RawTherapee":      "Design",
    "FreeCAD.FreeCAD":              "Design",
    "OpenSCAD.OpenSCAD":            "Design",
    "LibreCAD.LibreCAD":            "Design",

    # ── Security & Privacy ────────────────────────────────────────────────────
    "KeePassXCTeam.KeePassXC":      "Security",
    "VeraCrypt.VeraCrypt":          "Security",
    "ProtonVPN.ProtonVPN":          "Security",
    "Bitwarden.Bitwarden":          "Security",
    "GnuPG.GnuPG":                  "Security",
    "Cryptomator.Cryptomator":      "Security",
    "I2P.I2P":                      "Security",
    "TorProject.TorBrowser":        "Security",
    "Kleopatra.Kleopatra":          "Security",

    # ── Utilities ─────────────────────────────────────────────────────────────
    "7zip.7zip":                    "Utilities",
    "Rufus.Rufus":                  "Utilities",
    "balenaEtcher.balenaEtcher":    "Utilities",
    "CrystalDewWorld.CrystalDiskInfo": "Utilities",
    "CrystalDewWorld.CrystalDiskMark": "Utilities",
    "WinSCP.WinSCP":                "Utilities",
    "PuTTY.PuTTY":                  "Utilities",
    "HWiNFO.HWiNFO":                "Utilities",
    "CPUID.CPU-Z":                  "Utilities",
    "TechPowerUp.GPU-Z":            "Utilities",
    "Piriform.Recuva":              "Utilities",
    "BleachBit.BleachBit":          "Utilities",
    "Ventoy.Ventoy":                "Utilities",
    "RealVNC.VNCViewer":            "Utilities",
    "UltraVNC.UltraVNC":            "Utilities",
    "File-New-Project.EarTrumpet":  "Utilities",
    "AntibodySoftware.WizTree":     "Utilities",
    "WinDirStat.WinDirStat":        "Utilities",
    "Gyan.FFmpeg":                  "Utilities",
    "jqlang.jq":                    "Utilities",
    "GNU.Wget":                     "Utilities",
    "cURL.cURL":                    "Utilities",
    "Microsoft.PowerShell":         "Utilities",

    # ── Development ───────────────────────────────────────────────────────────
    "Git.Git":                      "Development",
    "Notepad++.Notepad++":          "Development",
    "VSCodium.VSCodium":            "Development",
    "Python.Python.3.12":           "Development",
    "WiresharkFoundation.Wireshark": "Development",
    "Rustlang.Rustup":              "Development",
    "GoLang.Go":                    "Development",
    "OpenJS.NodeJS.LTS":            "Development",
    "GnuPG.Gpg4win":                "Development",
    "HeidiSQL.HeidiSQL":            "Development",
    "DBeaver.DBeaver.Community":    "Development",
    "Postman.Postman":              "Development",
    "Insomnia.Insomnia":            "Development",
    "GitHub.GitHubDesktop":         "Development",
    "Meld.Meld":                    "Development",
    "astral-sh.uv":                 "Development",
    "yt-dlp.yt-dlp":                "Development",

    # ── Communication ─────────────────────────────────────────────────────────
    "Mozilla.Thunderbird":          "Communication",
    "Element.Element":              "Communication",
    "Signal.Signal":                "Communication",
    "Jitsi.Jitsi":                  "Communication",
    "Mattermost.Mattermost":        "Communication",
    "Rocket.Chat.RocketChat":       "Communication",

    # ── File Sharing ──────────────────────────────────────────────────────────
    "qBittorrent.qBittorrent":      "File Sharing",
    "Syncthing.Syncthing":          "File Sharing",
    "Nextcloud.NextcloudDesktop":   "File Sharing",
    "Rclone.Rclone":                "File Sharing",
    "ShareX.ShareX":                "File Sharing",
    "localsend.LocalSend":          "File Sharing",

    # ── Games ─────────────────────────────────────────────────────────────────
    "Valve.Steam":                  "Games",
    "Heroic.HeroicGamesLauncher":   "Games",
    "itch.io.itch":                 "Games",

    # ── Virtualization ────────────────────────────────────────────────────────
    "Oracle.VirtualBox":            "Virtualization",
    "QEMU.QEMU":                    "Virtualization",
    "Vagrant.Vagrant":              "Virtualization",
}

_local = threading.local()

def _session() -> requests.Session:
    if not hasattr(_local, "s"):
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
        _local.s = s
    return _local.s

def gh_get(path: str) -> requests.Response:
    return _session().get(
        f"{GITHUB_API}/repos/{WINGET_REPO}/contents/{path}",
        timeout=20,
    )


_cache: dict = {}
_cache_lock  = threading.Lock()
_cache_dirty = False


def load_cache() -> None:
    global _cache
    if CACHE_FILE.exists():
        try:
            _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            print(f"Cache: {len(_cache)} entries")
        except Exception:
            _cache = {}


def save_cache() -> None:
    if _cache_dirty:
        CACHE_FILE.write_text(
            json.dumps(_cache, separators=(",", ":")),
            encoding="utf-8",
        )


def detect_type(manifest_type: str, url: str) -> str:
    if manifest_type:
        return manifest_type.lower()
    # strip query/fragment
    path = unquote(urlparse(url).path).lower()
    for ext, typ in URL_EXT_MAP:
        if path.endswith(ext):
            return typ
    return "exe"


def is_oss(license_str: str) -> bool:
    if not license_str:
        return False
    normed = re.sub(r"[-+](or-later|only|or-compatible|and-later)", "",
                    license_str.lower())
    tokens = re.split(r"[\s,/|+()\[\]]+", normed)
    return any(t in OSS_LICENSES for t in tokens if t)


def latest_version_folder(app_id: str) -> str | None:
    parts  = app_id.split(".")
    folder = f"manifests/{parts[0][0].lower()}/{'/'.join(parts)}"
    r      = gh_get(folder)
    if not r.ok:
        return None
    versions = [x["name"] for x in r.json() if x["type"] == "dir"]
    if not versions:
        return None

    def ver_key(v: str) -> tuple[int, ...]:
        return tuple(int(n) for n in re.split(r"[.\-]", v) if n.isdigit())

    return f"{folder}/{max(versions, key=ver_key)}"


def fetch_manifests(folder_path: str) -> tuple[dict, dict, dict]:
    r = gh_get(folder_path)
    if not r.ok:
        return {}, {}, {}

    files: dict[str, str] = {
        f["name"]: f["download_url"]
        for f in r.json()
        if f["type"] == "file"
    }

    def pick(suffix: str) -> str | None:
        return next((u for n, u in files.items() if n.endswith(suffix)), None)

    # Version manifest: a .yaml that is not the installer or a locale file
    def pick_version() -> str | None:
        return next(
            (u for n, u in files.items()
             if n.endswith(".yaml") and ".installer." not in n and ".locale." not in n),
            None,
        )

    def load(url: str | None) -> dict:
        if not url:
            return {}
        raw = _session().get(url, timeout=20)
        if not raw.ok:
            return {}
        try:
            return yaml.safe_load(raw.text) or {}
        except yaml.YAMLError:
            return {}

    with ThreadPoolExecutor(max_workers=3) as ex:
        fl = ex.submit(load, pick(".locale.en-US.yaml"))
        fi = ex.submit(load, pick(".installer.yaml"))
        fv = ex.submit(load, pick_version())
        return fl.result(), fi.result(), fv.result()


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


def build_entry(app_id: str, category: str) -> dict | None:
    global _cache_dirty

    folder = latest_version_folder(app_id)
    if not folder:
        return None

    key = f"{app_id}:{folder}"
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    locale, installer_manifest, version_manifest = fetch_manifests(folder)
    meta = {**version_manifest, **locale}

    if not is_oss(meta.get("License", "")):
        return None

    installers: list[dict] = (
        installer_manifest.get("Installers")
        or version_manifest.get("Installers")
        or []
    )
    chosen = best_installer(installers)
    if not chosen or not chosen.get("InstallerUrl"):
        return None

    typ = detect_type(chosen.get("InstallerType", ""), chosen["InstallerUrl"])

    entry: dict = {
        "id":          app_id,
        "name":        meta.get("PackageName", app_id.split(".")[-1]),
        "version":     str(
            meta.get("PackageVersion")
            or version_manifest.get("PackageVersion")
            or "unknown"
        ),
        "publisher":   meta.get("Publisher", app_id.split(".")[0]),
        "description": (meta.get("ShortDescription") or meta.get("Description") or "").strip(),
        "license":     meta.get("License", ""),
        "homepage":    meta.get("PackageUrl", ""),
        "category":    category,
        "tags":        meta.get("Tags") or [],
        "installer": {
            "url":          chosen["InstallerUrl"],
            "type":         typ,
            "architecture": chosen.get("Architecture", "x64"),
            "sha256":       chosen.get("InstallerSha256", ""),
            "silentArgs":   silent_args(chosen, installer_manifest),
        },
    }

    with _cache_lock:
        _cache[key] = entry
        _cache_dirty = True

    return entry


def write_outputs(ok: list[dict], elapsed: float, failed: list[str]) -> None:
    ok_sorted = sorted(ok, key=lambda e: (e["category"], e["name"].lower()))

    Path("fossy-catalog.json").write_text(
        json.dumps({
            "version":   "1.0",
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

    _gha_summary(ok_sorted, elapsed, failed)


def _gha_summary(ok: list[dict], elapsed: float, failed: list[str]) -> None:
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
        f"| ❌ Failed | **{len(failed)}** |",
        f"| ⏱ Elapsed | {elapsed:.1f}s |",
        "",
        "### By Category",
        "| Category | Count |",
        "|----------|------:|",
        *[f"| {cat} | {n} |" for cat, n in sorted(cats.items())],
    ]
    if failed:
        lines += ["", "### Failed", *[f"- `{f}`" for f in sorted(failed)]]

    Path(summary_path).write_text("\n".join(lines), encoding="utf-8")

    gha_out = os.environ.get("GITHUB_OUTPUT")
    if gha_out:
        with open(gha_out, "a") as f:
            f.write(f"built={len(ok)}\nfailed={len(failed)}\n")

def main() -> None:
    load_cache()
    print(f"Fossy  |  {len(APPS)} apps  |  {WORKERS} workers")
    if not GITHUB_TOKEN:
        print("⚠  No GITHUB_TOKEN — rate limited to 60 req/hr")

    ok:     list[dict] = []
    failed: list[str]  = []
    lock = threading.Lock()

    def process(app_id: str, category: str) -> None:
        try:
            entry = build_entry(app_id, category)
            with lock:
                if entry:
                    ok.append(entry)
                    print(f"  ✓  {entry['name']:<40} {entry['version']}")
                else:
                    failed.append(app_id)
                    print(f"  ✗  {app_id}")
        except Exception as e:
            with lock:
                failed.append(app_id)
                print(f"  ✗  {app_id}  ({e})")

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in as_completed(
            ex.submit(process, aid, cat) for aid, cat in APPS.items()
        ):
            pass

    elapsed = time.monotonic() - t0
    write_outputs(ok, elapsed, failed)
    save_cache()

    print(f"\n{'─' * 52}")
    print(f"Done in {elapsed:.1f}s  |  {len(ok)} built  |  {len(failed)} failed")
    if failed:
        print("Failed:", ", ".join(failed))
    print("Outputs: fossy-catalog.json  fossy-index.json  catalog/")


if __name__ == "__main__":
    main()
