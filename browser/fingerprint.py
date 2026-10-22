"""BrowserForge fingerprint generation + locks + cookie migration helpers.

Chromium migration path:
  - Fingerprint: BrowserForge generates (with explicit Screen(max_width=1440,
                 max_height=900) so windows fit the 1920x1080 Xvfb desktop),
                 and we serialize the FULL Fingerprint to fingerprint.json
                 (locked across restarts).
  - Injection:   Uses BrowserForge's native prototype-level InjectFunction
                 (Navigator.prototype, Screen.prototype, userAgentData,
                 WebGL UNMASKED_VENDOR/RENDERER, Audio/Video codecs, Battery,
                 Intl) with `overrideStatic()` patched out so
                 `window.SharedArrayBuffer` stays intact for AI Studio Build.
  - Cookies:     Exports from the legacy Camoufox/Firefox profile's
                 cookies.sqlite by copying cookies.sqlite (+ -wal / -shm) to a
                 temp directory first so WAL-committed rolling cookies
                 (e.g. SIDCC / __Secure-1PSIDTS) are never missed while the
                 legacy profile directory remains 100% untouched.

NOTHING in this file writes into the legacy Firefox profile. All writes go into
the new Chromium profile dir.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from browserforge.fingerprints import (
    Fingerprint,
    FingerprintGenerator,
    NavigatorFingerprint,
    Screen,
    ScreenFingerprint,
    VideoCard,
)
from browserforge.injectors.utils import InjectFunction, only_injectable_headers


# ---------------------------------------------------------------------------
# Fingerprint persistence
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = "browserforge-v2"


def fingerprint_path(profile_dir: str) -> str:
    return os.path.join(profile_dir, "fingerprint.json")


def _dict_to_fingerprint(data: Dict[str, Any]) -> Fingerprint:
    """Reconstruct a BrowserForge Fingerprint dataclass from a serialized dict."""
    raw = data.get("fingerprint") if "fingerprint" in data else data
    scr = raw["screen"]
    nav = raw["navigator"]
    vc = raw.get("videoCard")
    return Fingerprint(
        screen=ScreenFingerprint(**scr),
        navigator=NavigatorFingerprint(**nav),
        headers=dict(raw.get("headers") or {}),
        videoCodecs=dict(raw.get("videoCodecs") or {}),
        audioCodecs=dict(raw.get("audioCodecs") or {}),
        pluginsData=dict(raw.get("pluginsData") or {}),
        battery=raw.get("battery"),
        videoCard=VideoCard(**vc) if isinstance(vc, dict) else None,
        multimediaDevices=list(raw.get("multimediaDevices") or []),
        fonts=list(raw.get("fonts") or []),
        mockWebRTC=bool(raw.get("mockWebRTC", False)),
        slim=bool(raw.get("slim", False)),
    )


def _fingerprint_to_dict(fp: Fingerprint) -> Dict[str, Any]:
    """Serialize full BrowserForge Fingerprint into our versioned JSON envelope."""
    raw_fp = json.loads(fp.dumps())
    return {
        "schema": _SCHEMA_VERSION,
        "generated_at": time.time(),
        "fingerprint": raw_fp,
        # Keep top-level navigator/screen aliases for convenient dict access
        "navigator": raw_fp["navigator"],
        "screen": raw_fp["screen"],
        "headers": raw_fp["headers"],
        "videoCard": raw_fp.get("videoCard"),
    }


def load_or_create_fingerprint(profile_dir: str) -> Dict[str, Any]:
    """Load fingerprint.json if present, else generate a fresh one and persist.

    CRITICAL:
    1. Must persist the full fingerprint on first run so that subsequent
       launches of the same profile reuse the *exact same* fingerprint
       (UA, Client Hints, Screen, WebGL vendor/renderer, codecs, etc.).
    2. Must constrain Screen(max_width=1440, max_height=900) so the window fits
       inside the 1920x1080 Xvfb virtual screen.
    3. If a legacy Camoufox fingerprint.json is encountered (has
       'executable_path' or 'env.CAMOU_CONFIG_1'), back it up to
       'fingerprint.camoufox.bak.json' before writing the new one.
    """
    os.makedirs(profile_dir, exist_ok=True)
    path = fingerprint_path(profile_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("schema") == _SCHEMA_VERSION and "fingerprint" in data:
                # Validate round-trip reconstruction
                _dict_to_fingerprint(data)
                return data
            # Detect legacy Camoufox fingerprint.json and preserve a backup
            if "executable_path" in data or "firefox_user_prefs" in data:
                bak_path = os.path.join(profile_dir, "fingerprint.camoufox.bak.json")
                if not os.path.exists(bak_path):
                    shutil.copy2(path, bak_path)
        except Exception:
            pass

    gen = FingerprintGenerator(
        browser="chrome",
        os="windows",
        screen=Screen(min_width=1280, max_width=1440, min_height=720, max_height=900),
    )
    fp = gen.generate()
    data = _fingerprint_to_dict(fp)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return data


# ---------------------------------------------------------------------------
# Injection JS & Context options
# ---------------------------------------------------------------------------

def build_injection_js(fp_data: Dict[str, Any]) -> str:
    """Build prototype-level stealth injection script from locked fingerprint.

    Uses BrowserForge's native `InjectFunction(fp)` (which proxies
    Navigator.prototype, Screen.prototype, navigator.userAgentData,
    WebGLRenderingContext.prototype.getParameter, HTMLMediaElement.canPlayType,
    navigator.getBattery, and Intl) with two surgical adjustments for
    Google AI Studio Build:
      1. Neutralize `overrideStatic()` so `window.SharedArrayBuffer` is NOT
         clobbered to `undefined` (AI Studio preview sandboxes rely on it).
      2. Ensure `window.chrome.runtime` stub exists without clobbering real
         `window.chrome` properties on headed Chromium.
    """
    fp_obj = _dict_to_fingerprint(fp_data)
    raw_js = InjectFunction(fp_obj)
    # Prevent overrideStatic() from deleting window.SharedArrayBuffer.
    # CRITICAL: BrowserForge's InjectFunction(fp) returns `(()=>{ ... })()`
    # WITHOUT a trailing semicolon! If another `(() => { ... })()` is appended
    # directly after it, JavaScript parses the two IIFEs as `(iife1)(iife2)`,
    # which throws `TypeError: (intermediate value)(...) is not a function` and
    # aborts the entire init script before `window.__PROVIDER_NAME__` runs!
    patched_js = raw_js.replace("overrideStatic();", "/* overrideStatic() disabled for SharedArrayBuffer */").rstrip()
    if not patched_js.endswith(";"):
        patched_js += ";"
    extra_js = """
;(() => {
  try {
    if (!window.chrome) {
      Object.defineProperty(window, 'chrome', {
        value: { runtime: {}, app: { isInstalled: false } },
        writable: true, enumerable: true, configurable: false
      });
    } else if (!window.chrome.runtime) {
      window.chrome.runtime = {};
    }
  } catch (e) {}
})();
"""
    return patched_js + "\n" + extra_js


def build_context_headers(fp_data: Dict[str, Any]) -> Dict[str, str]:
    """Return browser-wide HTTP headers (Sec-CH-UA*, Accept-Language) matching fp."""
    fp_obj = _dict_to_fingerprint(fp_data)
    headers = only_injectable_headers(fp_obj.headers, "chromium")
    # User-Agent is already set via launch_persistent_context(user_agent=...)
    headers.pop("User-Agent", None)
    headers.pop("user-agent", None)
    return headers


# ---------------------------------------------------------------------------
# Cookie migration from Camoufox/Firefox profile
# ---------------------------------------------------------------------------

_GOOGLE_DOMAINS = (
    "google.com",
    "google.cn",
    "ai.studio",
    "ai.google.dev",
    "googleapis.com",
    "youtube.com",
    "googlevideo.com",
    "googleusercontent.com",
    "googlesyndication.com",
    "gstatic.com",
    "run.app",  # AI Studio Build iframe host
    "aistudio.com",
)


def _domain_matches(host: str) -> bool:
    h = host.lower().lstrip(".")
    return any(h == d or h.endswith("." + d) for d in _GOOGLE_DOMAINS)


def export_cookies_from_firefox_db(db_path: str) -> List[Dict[str, Any]]:
    """Read moz_cookies safely (including WAL) and return Playwright cookie dicts.

    IMPORTANT: Copies cookies.sqlite (+ -wal and -shm if present) into a
    temporary directory before querying. Opening in-place with `immutable=1`
    causes SQLite to ignore the `-wal` file, which drops freshly rotated
    Google session cookies (e.g. SIDCC, __Secure-1PSIDCC). Copying to a temp
    dir gives us 100% of WAL-committed rows while never touching the legacy
    Camoufox profile directory.
    """
    if not os.path.exists(db_path):
        return []

    with tempfile.TemporaryDirectory(prefix="ff_cookie_export_") as tmp_dir:
        tmp_db = os.path.join(tmp_dir, "cookies.sqlite")
        shutil.copy2(db_path, tmp_db)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path + suffix
            if os.path.exists(sidecar):
                try:
                    shutil.copy2(sidecar, tmp_db + suffix)
                except Exception:
                    pass

        conn = sqlite3.connect(tmp_db)
        try:
            cur = conn.cursor()
            cur.execute("""
              SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite
              FROM moz_cookies
            """)
            rows = cur.fetchall()
        finally:
            conn.close()

    sameSite_map = {0: "None", 1: "Lax", 2: "Strict", 3: "None"}
    cookies: List[Dict[str, Any]] = []
    for name, value, host, path, expiry, is_secure, is_httponly, same_site in rows:
        if not _domain_matches(host or ""):
            continue
        raw_ss = (int(same_site) & 0xFF) if same_site is not None else 0
        ss = sameSite_map.get(raw_ss, "Lax")
        if ss == "None" and not is_secure:
            # Chromium refuses SameSite=None without Secure
            ss = "Lax"
        # Firefox (modern) stores expiry in MILLISECONDS. Playwright wants SECONDS (float).
        exp_int = int(expiry) if expiry else 0
        if exp_int > 0:
            if exp_int > 999_999_999_999:
                exp_int = exp_int // 1000
            exp_val = float(exp_int)
        else:
            exp_val = -1.0  # session cookie

        # Chromium strictness: __Host- cookies must NOT have a `domain` attribute
        # in Playwright's add_cookies (must use `url` instead, or omit leading dot
        # with path="/"). In Playwright, passing domain without leading dot for
        # __Host- cookies or converting __Host- to `url` avoids rejection.
        cookie_dict: Dict[str, Any] = {
            "name": name,
            "value": value,
            "path": path or "/",
            "expires": exp_val,
            "httpOnly": bool(is_httponly),
            "secure": bool(is_secure),
            "sameSite": ss,
        }
        if name.startswith("__Host-"):
            clean_host = (host or "").lstrip(".")
            cookie_dict["url"] = f"https://{clean_host}{path or '/'}"
        else:
            cookie_dict["domain"] = host
        cookies.append(cookie_dict)
    return cookies


def migrate_profile_dir(legacy_camoufox_dir: str, new_chromium_dir: str) -> Tuple[int, str]:
    """Export cookies from one legacy Camoufox profile into the new Chromium dir.

    Returns (num_cookies, status_message).
    """
    os.makedirs(new_chromium_dir, exist_ok=True)
    src_db = os.path.join(legacy_camoufox_dir, "cookies.sqlite")
    cookies = export_cookies_from_firefox_db(src_db)
    if not cookies:
        return 0, f"no cookies found at {src_db}"
    out_path = os.path.join(new_chromium_dir, "cookies.json")
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2)
    os.replace(tmp, out_path)
    return len(cookies), f"wrote {len(cookies)} cookies to {out_path}"


def ensure_profile_migrated(
    profile_dir: str,
    diagnostic_tag: str,
    legacy_base_dir: Optional[str] = None,
) -> Optional[str]:
    """Automatically migrate cookies from legacy Camoufox profile on first run.

    If `<profile_dir>/cookies.json` does not exist yet and a matching legacy
    profile `<legacy_base_dir>/<diagnostic_tag>/cookies.sqlite` exists, exports
    its cookies into `<profile_dir>/cookies.json`.
    """
    cookies_json = os.path.join(profile_dir, "cookies.json")
    if os.path.exists(cookies_json):
        return None

    legacy_root = legacy_base_dir or os.getenv("FF_LEGACY_ROOT") or "/app/camoufox_profiles"
    if os.path.abspath(legacy_root) == os.path.abspath(os.path.dirname(profile_dir)):
        # Same root directory — check if cookies.sqlite exists directly in profile_dir
        legacy_dir = profile_dir
    else:
        legacy_dir = os.path.join(legacy_root, diagnostic_tag)

    src_db = os.path.join(legacy_dir, "cookies.sqlite")
    if not os.path.exists(src_db):
        return None

    n, msg = migrate_profile_dir(legacy_dir, profile_dir)
    return msg if n > 0 else None


def load_migrated_cookies(new_chromium_dir: str) -> List[Dict[str, Any]]:
    """Load cookies.json from a migrated Chromium profile dir, or []."""
    path = os.path.join(new_chromium_dir, "cookies.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def has_chromium_cookie_db(profile_dir: str) -> bool:
    """Return True if Chromium has already created and populated Default/Cookies."""
    db_path = os.path.join(profile_dir, "Default", "Cookies")
    return os.path.exists(db_path) and os.path.getsize(db_path) > 0
