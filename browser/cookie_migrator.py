"""Cookie migrator CLI.

One-shot tool to export cookies from legacy Camoufox/Firefox profiles into
the new Chromium-migration layout. Does NOT modify the legacy profile.

Usage:
    python -m browser.cookie_migrator \\
        --legacy /app/camoufox_profiles \\
        --out /app/chromium_profiles \\
        [--source USER_COOKIE_7] \\
        [--dry-run]

Without --source, migrates every subdirectory of --legacy that contains a
cookies.sqlite file.
"""

import argparse
import os
import sys
from typing import List, Tuple

# Allow being run as script (python -m browser.cookie_migrator) from repo root
if __package__ in (None, ""):
    # Bizarre but idiomatic for a utility module run directly
    CURRENT = os.path.dirname(os.path.abspath(__file__))
    PARENT = os.path.dirname(CURRENT)
    if PARENT not in sys.path:
        sys.path.insert(0, PARENT)

from browser.fingerprint import migrate_profile_dir, export_cookies_from_firefox_db  # noqa: E402


def _iter_installable_dirs(root: str) -> List[str]:
    if not os.path.isdir(root):
        return []
    names = []
    for n in sorted(os.listdir(root)):
        sub = os.path.join(root, n)
        if os.path.isdir(sub):
            if os.path.exists(os.path.join(sub, "cookies.sqlite")):
                names.append(n)
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="browser.cookie_migrator")
    ap.add_argument("--legacy", required=True,
                    help="Path to legacy Camoufox profiles root (contains USER_COOKIE_N/...)")
    ap.add_argument("--out", required=True,
                    help="Path where the new Chromium profiles should be created")
    ap.add_argument("--source", default=None,
                    help="Optional: only migrate this source dir (e.g. USER_COOKIE_7)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be done without writing any files")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.legacy):
        print(f"[migrator] legacy root not found: {args.legacy}", file=sys.stderr)
        return 2

    if args.source:
        sources = [args.source]
    else:
        sources = _iter_installable_dirs(args.legacy)
        if not sources:
            print(f"[migrator] no migratable sources found under {args.legacy}", file=sys.stderr)
            return 2

    print(f"[migrator] migrating {len(sources)} source(s): {', '.join(sources)}")

    results: List[Tuple[str, int, str]] = []
    if not args.dry_run:
        os.makedirs(args.out, exist_ok=True)

    failures = 0
    for src in sources:
        legacy_dir = os.path.join(args.legacy, src)
        new_dir = os.path.join(args.out, src)
        if args.dry_run:
            # preview only
            db_path = os.path.join(legacy_dir, "cookies.sqlite")
            cookies = export_cookies_from_firefox_db(db_path) if os.path.exists(db_path) else []
            results.append((src, len(cookies), f"dry-run: would write {len(cookies)} cookies to {new_dir}/cookies.json"))
            continue
        try:
            n, msg = migrate_profile_dir(legacy_dir, new_dir)
            results.append((src, n, msg))
        except Exception as e:
            failures += 1
            results.append((src, 0, f"ERROR: {type(e).__name__}: {e}"))

    print()
    print("[migrator] summary:")
    for src, n, msg in results:
        marker = "OK " if n > 0 else "-- "
        print(f"  {marker}{src:24s} {n:4d} cookies   {msg}")

    if failures:
        print(f"[migrator] {failures} failure(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
