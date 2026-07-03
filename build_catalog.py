import os
import re
import json
import time
import yaml
import hashlib
import requests
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

GITHUB_API   = "https://api.github.com"
WINGET_REPO  = "microsoft/winget-pkgs"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
WORKERS      = 8 
RETRY_TOTAL  = 3
RETRY_BACKOFF = 1.5

OSS_LICENSES = {
    "gpl", "gpl-2.0", "gpl-3.0", "lgpl", "lgpl-2.1", "lgpl-3.0",
    "mit", "apache", "apache-2.0", "bsd", "bsd-2-clause", "bsd-3-clause",
    "mpl", "mpl-2.0", "isc", "cddl", "epl", "epl-2.0",
    "agpl", "agpl-3.0", "cc0", "unlicense", "wtfpl", "zlib",
    "psf", "python-2.0", "eupl", "eupl-1.2", "artistic",
}

INSTALLER_RANK = ["msi", "exe", "msix", "appx", "zip"]

APPS = {
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
    "Cryptpad.Cryptpad":            "Productivity",
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
    "Nicehash.NicehashQuickMiner":  "Media",  # will fail license check — fine

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
    "PortableApps.ClamWinPortable": "Security",

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
    "eM Client.eM Client":          "Communication",

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

def session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers.update({"Accept": "application/vnd.github.v3+json"})
        if GITHUB_TOKEN:
            s.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        retry = Retry(
            total=RETRY_TOTAL,
            backoff_factor=RETRY_BACKOFF,
            status_forcelist={429, 500, 502, 503, 504},
            allowed_methods={"GET"},
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
        _local.session = s
    return _local.session


def gh_get(path: str) -> requests.Response:
    url = f"{GITHUB_API}/repos/{WINGET_REPO}/contents/{path}"
    return session().get(url, timeout=15)

def latest_version_path(app_id: str) -> str | None:
    parts   = app_id.split(".")
    pub     = parts[0]
    folder  = f"manifests/{pub[0].lower()}/{'/'.join(parts)}"

    r = gh_get(folder)
    if not r.ok:
        return None

    versions = [x["name"] for x in r.json() if x["type"] == "dir"]
    if not versions:
        return None

    def ver_key(v: str) -> list[int]:
        return [int(n) for n in re.split(r"[.\-]", v) if n.isdigit()]

    return f"{folder}/{max(versions, key=ver_key)}"


def fetch_yaml(folder_path: str, suffix: str) -> dict | None:
    r = gh_get(folder_path)
    if not r.ok:
        return None

    target = next(
        (f for f in r.json() if f["type"] == "file" and f["name"].endswith(suffix)),
        None,
    )
    if not target:
        return None

    raw = session().get(target["download_url"], timeout=15)
    if not raw.ok:
        return None
    try:
        return yaml.safe_load(raw.text) or {}
    except yaml.YAMLError:
        return None


def fetch_all_yamls(folder_path: str) -> tuple[dict, dict, dict]:
    """Fetch locale, installer, and version manifests in parallel."""
    results = {}
    suffixes = {
        "locale":    ".locale.en-US.yaml",
        "installer": ".installer.yaml",
        "version":   ".yaml",
    }
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fetch_yaml, folder_path, s): k for k, s in suffixes.items()}
        for f in as_completed(futs):
            results[futs[f]] = f.result() or {}
    return results["locale"], results["installer"], results["version"]

def is_oss(license_str: str) -> bool:
    if not license_str:
        return False
    s = license_str.lower()
    return any(lic in s for lic in OSS_LICENSES)


def best_installer(installers: list[dict]) -> dict | None:
    if not installers:
        return None

    def rank(i: dict) -> tuple[int, int]:
        arch = i.get("Architecture", "").lower()
        typ  = i.get("InstallerType", "").lower()
        arch_score = 0 if arch == "x64" else 1 if arch == "neutral" else 2
        try:
            type_score = INSTALLER_RANK.index(typ)
        except ValueError:
            type_score = 99
        return (arch_score, type_score)

    valid = [
        i for i in installers
        if i.get("Architecture", "").lower() in ("x64", "x86", "neutral")
        and i.get("InstallerType", "").lower() in INSTALLER_RANK
    ] or installers

    return min(valid, key=rank)


def silent_switches(chosen: dict, installer_manifest: dict) -> str:
    for source in (
        chosen.get("InstallerSwitches", {}),
        installer_manifest.get("InstallerSwitches", {}),
    ):
        if source:
            sw = source.get("Silent") or source.get("SilentWithProgress")
            if sw:
                return sw
    typ = chosen.get("InstallerType", "exe").lower()
    return "/quiet /norestart" if typ == "msi" else "/S"

def build_entry(app_id: str, category: str) -> dict | None:
    folder = latest_version_path(app_id)
    if not folder:
        return None

    locale, installer_manifest, version_manifest = fetch_all_yamls(folder)
    meta = {**version_manifest, **locale}

    license_str = meta.get("License", "")
    if not is_oss(license_str):
        return None

    installers = (
        installer_manifest.get("Installers")
        or version_manifest.get("Installers")
        or []
    )
    chosen = best_installer(installers)
    if not chosen or not chosen.get("InstallerUrl"):
        return None

    version = str(
        meta.get("PackageVersion")
        or version_manifest.get("PackageVersion")
        or "unknown"
    )

    return {
        "id":          app_id,
        "name":        meta.get("PackageName", app_id.split(".")[-1]),
        "version":     version,
        "publisher":   meta.get("Publisher", app_id.split(".")[0]),
        "description": (meta.get("ShortDescription") or meta.get("Description") or "").strip(),
        "license":     license_str,
        "homepage":    meta.get("PackageUrl", ""),
        "category":    category,
        "tags":        meta.get("Tags", []),
        "installer": {
            "url":         chosen["InstallerUrl"],
            "type":        chosen.get("InstallerType", "exe").lower(),
            "architecture": chosen.get("Architecture", "x64"),
            "sha256":      chosen.get("InstallerSha256", ""),
            "silentArgs":  silent_switches(chosen, installer_manifest),
        },
    }

def main() -> None:
    print(f"Fossy Catalog Builder  |  {len(APPS)} apps  |  {WORKERS} workers")
    if not GITHUB_TOKEN:
        print("WARNING: no GITHUB_TOKEN — rate limited to 60 req/hr\n")

    ok, fail = [], []
    lock = threading.Lock()

    def process(app_id: str, category: str) -> None:
        try:
            entry = build_entry(app_id, category)
            with lock:
                if entry:
                    ok.append(entry)
                    print(f"  ✓  {entry['name']:<35} {entry['version']}")
                else:
                    fail.append(app_id)
                    print(f"  ✗  {app_id}")
        except Exception as e:
            with lock:
                fail.append(app_id)
                print(f"  ✗  {app_id}  ({e})")

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(process, aid, cat) for aid, cat in APPS.items()]
        for f in as_completed(futs):
            pass

    elapsed = time.monotonic() - t0

    ok.sort(key=lambda e: (e["category"], e["name"].lower()))

    catalog = {
        "version":   "1.0",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "apps":      ok,
    }

    out = Path("fossy-catalog.json")
    out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'─' * 50}")
    print(f"Done in {elapsed:.1f}s  |  {len(ok)} built  |  {len(fail)} failed")
    if fail:
        print("Failed:", ", ".join(fail))
    print(f"Output: {out.resolve()}")


if __name__ == "__main__":
    main()