#!/usr/bin/env python3
"""Check local setup: dependencies, .env values, cached tokens, connectivity.

    python scripts/doctor.py

Run this first when something stops working — it isolates config problems from
API problems without burning rate limit.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime

import _bootstrap  # noqa: F401

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
MEH = "\033[33m·\033[0m"


def check_dependencies() -> bool:
    print("Dependencies")
    all_ok = True
    for module in ("requests", "dotenv"):
        try:
            importlib.import_module(module)
            print(f"  {OK} {module}")
        except ImportError:
            print(f"  {BAD} {module} — run: pip install -r requirements.txt")
            all_ok = False
    return all_ok


def check_env_file() -> None:
    """Inspect .env itself before blaming the credentials in it."""
    from rpg.config import REPO_ROOT
    from rpg.envcheck import diagnose, format_problems

    print("\n.env file")
    problems = diagnose(("SPOTIFY_CLIENT_ID", "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"))
    if not problems:
        print(f"  {OK} {REPO_ROOT / '.env'} parses cleanly")
        return
    print(f"  {BAD} {len(problems)} problem(s) found:\n")
    print(format_problems(problems))


def check_provider(name: str, loader, store_name: str) -> bool:
    from rpg.config import ConfigError
    from rpg.tokens import TokenStore

    print(f"\n{name}")
    try:
        config = loader()
    except ConfigError as exc:
        print(f"  {BAD} config: {exc}")
        return False

    print(f"  {OK} credentials present")
    print(f"  {MEH} redirect uri: {config.redirect_uri}")

    store = TokenStore(store_name)
    tokens = store.load()
    if tokens is None:
        print(f"  {BAD} no cached token — run: python scripts/auth_{store_name}.py")
        return False

    if tokens.expires_at:
        expires = datetime.fromtimestamp(tokens.expires_at).strftime("%Y-%m-%d %H:%M")
        status = "expired" if tokens.expired() else f"valid until {expires}"
    else:
        status = "no expiry recorded"
    print(f"  {OK} token cached ({status})")

    if tokens.refresh_token:
        print(f"  {OK} refresh token present")
    else:
        print(f"  {BAD} no refresh token — re-authorize with --force")
        return False

    if tokens.scope:
        print(f"  {MEH} granted scopes: {tokens.scope}")
    return True


def main() -> int:
    print("Running Playlist Generator — setup check\n" + "─" * 46)

    deps_ok = check_dependencies()
    if not deps_ok:
        return 1

    check_env_file()

    from rpg.config import spotify_config, strava_config

    spotify_ok = check_provider("Spotify", spotify_config, "spotify")
    strava_ok = check_provider("Strava", strava_config, "strava")

    print("\n" + "─" * 46)
    if spotify_ok and strava_ok:
        print("Both providers are configured and authorized.")
        print("  python scripts/probe_spotify.py")
        print("  python scripts/inspect_strava.py")
        return 0

    print("Setup incomplete — see the items marked ✗ above.")
    print("Setup walkthrough: docs/API_SETUP.md")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
