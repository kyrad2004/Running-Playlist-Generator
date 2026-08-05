#!/usr/bin/env python3
"""Run the Spotify OAuth flow once and cache the tokens.

    python scripts/auth_spotify.py           # authorize (or reuse a valid token)
    python scripts/auth_spotify.py --force   # re-authorize from scratch
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401

from rpg.config import ConfigError
from rpg.oauth import OAuthError
from rpg.spotify import SpotifyAuth, SpotifyClient
from rpg.transport import ApiError


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize this app with Spotify.")
    parser.add_argument("--force", action="store_true", help="ignore cached tokens")
    parser.add_argument("--no-browser", action="store_true", help="print the URL, don't open it")
    args = parser.parse_args()

    try:
        auth = SpotifyAuth()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    print("Spotify")
    print(f"  client id     {auth.config.client_id[:6]}…")
    print(f"  redirect uri  {auth.config.redirect_uri}")
    print(f"  scopes        {auth.config.scope_string}")

    try:
        if args.force:
            auth.store.clear()

        existing = auth.store.load()
        if existing and not existing.expired():
            print("\nUsing cached token (still valid). Pass --force to re-authorize.")
        else:
            if existing:
                print("\nCached token expired — refreshing.")
                auth.refresh(existing)
            else:
                auth.authorize_interactive(open_browser=not args.no_browser)
                print("Token saved.")

        profile = SpotifyClient(auth).me()
    except OAuthError as exc:
        print(f"\nOAuth failed: {exc}", file=sys.stderr)
        return 1
    except ApiError as exc:
        print(f"\nAPI call failed: {exc}", file=sys.stderr)
        if exc.status_code == 403:
            print(
                "\n403 right after authorizing usually means your Spotify account is not "
                "on the app's allowlist. In the dashboard: Settings → User Management → "
                "add your own Spotify account (email + display name).",
                file=sys.stderr,
            )
        return 1

    display = profile.get("display_name") or profile.get("id", "unknown")
    print(f"\n✓ Authorized as {display} (id: {profile.get('id')})")
    print(f"  tokens cached at {auth.store.path}")
    print("\nNext: python scripts/probe_spotify.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
