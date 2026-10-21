"""BrowserForge fingerprint generation + locks + cookie migration helpers.

Chromium migration path:
  - Fingerprint: BrowserForge generates, we serialize to fingerprint.json (locked).
  - Cookies:     export from the legacy Camoufox/Firefox profile's cookies.sqlite
                 (READONLY, immutable=1 so production is never touched), and
                 convert to Playwright cookie dicts that browser/instance.py adds
                 via ctx.add_cookies() at startup.

NOTHING in this file writes into the Firefox profile. All writes go into the
new Chromium profile dir.
"""

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Fingerprint persistence
# ---------------------------------------------------------------------------

def fingerprint_path(profile_dir: str) -> str:
    return os.path.join(profile_dir, "fingerprint.json")


def _fp_to_dict(fp) -> Dict[str, Any]:
    """BrowserForge Fingerprint object -> JSON-serializable dict."""
    nav = fp.navigator
    scr = fp.screen
    return {
        "schema": "browserforge-v1",
        "generated_at": time.time(),
        "navigator": {
            "userAgent": nav.userAgent,
            "platform": getattr(nav, "platform", "Win32") or "Win32",
            "languages": list(getattr(nav, "languages", ["en-US", "en"]) or ["en-US", "en"]),
            "hardwareConcurrency": int(getattr(nav, "hardwareConcurrency", 8) or 8),
            "doNotTrack": getattr(nav, "doNotTrack", "1") or "1",
            "maxTouchPoints": int(getattr(nav, "maxTouchPoints", 0) or 0),
            "vendor": getattr(nav, "vendor", "Google Inc.") or "Google Inc.",
        },
        "screen": {
            "width": int(scr.width),
            "height": int(scr.height),
            "availWidth": int(getattr(scr, "availWidth", scr.width)),
            "availHeight": int(getattr(scr, "availHeight", scr.height - 40)),
            "colorDepth": int(getattr(scr, "colorDepth", 24)),
            "pixelDepth": int(getattr(scr, "pixelDepth", 24)),
        },
    }


def _fp_from_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Round-trip validation; keeps only known keys."""
    if data.get("schema") != "browserforge-v1":
        raise ValueError("unsupported fingerprint schema")
    return {
        "navigator": data["navigator"],
        "screen": data["screen"],
    }


def load_or_create_fingerprint(profile_dir: str) -> Dict[str, Any]:
    """Load fingerprint.json if present, else generate a fresh one and persist.

    CRITICAL: Must persist the fingerprint on first run so that subsequent
    launches of the same profile reuse the *same* fingerprint.
    Randomization across restarts would defeat the whole point (Google
    would see the account bounce across many fingerprints).
    """
    os.makedirs(profile_dir, exist_ok=True)
    path = fingerprint_path(profile_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _fp_from_dict(data)
        except Exception:
            # fall through and regenerate
            pass

    # generate fresh
    from browserforge.fingerprints import FingerprintGenerator

    gen = FingerprintGenerator(browser="chrome", os="windows")
    fp = gen.generate()
    data = _fp_to_dict(fp)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return _fp_from_dict(data)


# ---------------------------------------------------------------------------
# injection JS
# ---------------------------------------------------------------------------

def build_injection_js(fp: Dict[str, Any]) -> str:
    """Translate the fingerprint dict into an add_init_script JS snippet.

    Uses json.dumps for every string so quotes/backslashes are escaped
    correctly for JS string literals.
    """
    nav = fp["navigator"]
    scr = fp["screen"]

    js = f"""
(() => {{
  const navDefs = {{
    userAgent: {json.dumps(nav["userAgent"])},
    platform: {json.dumps(nav["platform"])},
    hardwareConcurrency: {int(nav["hardwareConcurrency"])},
    languages: {json.dumps(nav["languages"])},
    doNotTrack: {json.dumps(nav["doNotTrack"])},
    maxTouchPoints: {int(nav["maxTouchPoints"])},
    vendor: {json.dumps(nav.get("vendor", "Google Inc."))},
    webdriver: undefined,
  }};
  for (const [k, v] of Object.entries(navDefs)) {{
    try {{ Object.defineProperty(navigator, k, {{ get: () => v, configurable: true }}); }} catch (e) {{}}
  }}

  const scrDefs = {{
    width: {int(scr["width"])}, height: {int(scr["height"])},
    availWidth: {int(scr["availWidth"])}, availHeight: {int(scr["availHeight"])},
    colorDepth: {int(scr["colorDepth"])}, pixelDepth: {int(scr["pixelDepth"])},
  }};
  for (const [k, v] of Object.entries(scrDefs)) {{
    try {{ Object.defineProperty(screen, k, {{ get: () => v, configurable: true }}); }} catch (e) {{}}
  }}

  // Chrome runtime stub (AI Studio Build expects Chrome)
  try {{ if (!window.chrome) {{ window.chrome = {{ runtime: {{}} }}; }} }} catch (e) {{}}
}})();
"""
    return js


# ---------------------------------------------------------------------------
# Cookie migration from Camoufox/Firefox profile
# ---------------------------------------------------------------------------

_GOOGLE_DOMAINS = (
    "google.com",
    "google.cn",
    "youtube.com",
    "googlevideo.com",
    "googleusercontent.com",
    "googlesyndication.com",
    "gstatic.com",
    "run.app",  # AI Studio Build iframe host
    "aistudio.com",
)


def _domain_matches(host: str) -> bool:
    h = host.lower()
    return any(h.endswith(d) for d in _GOOGLE_DOMAINS)


def export_cookies_from_firefox_db(db_path: str) -> List[Dict[str, Any]]:
    """Read moz_cookies READONLY, return list of Playwright cookie dicts.

    IMPORTANT: opens the sqlite DB in read+immutable mode so we never touch
    the WAL or skip over Firefox's locks. Caller should pass the legacy
    Camoufox profile's cookies.sqlite path.
    """
    if not os.path.exists(db_path):
        return []

    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
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
        ss = sameSite_map.get(int(same_site) if same_site is not None else 0, "Lax")
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
        cookies.append({
            "name": name,
            "value": value,
            "domain": host,
            "path": path or "/",
            "expires": exp_val,
            "httpOnly": bool(is_httponly),
            "secure": bool(is_secure),
            "sameSite": ss,
        })
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
