#!/usr/bin/env python3
"""Run the Strava OAuth flow once and cache the tokens.

    python scripts/auth_strava.py           # authorize (or reuse a valid token)
    python scripts/auth_strava.py --force   # re-authorize from scratch
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401

from rpg.config import ConfigError
from rpg.oauth import OAuthError
from rpg.strava import StravaAuth, StravaClient
from rpg.transport import ApiError


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize this app with Strava.")
    parser.add_argument("--force", action="store_true", help="ignore cached tokens")
    parser.add_argument("--no-browser", action="store_true", help="print the URL, don't open it")
    args = parser.parse_args()

    try:
        auth = StravaAuth()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    print("Strava")
    print(f"  client id     {auth.config.client_id}")
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

        client = StravaClient(auth)
        athlete = client.athlete()
    except OAuthError as exc:
        print(f"\nOAuth failed: {exc}", file=sys.stderr)
        return 1
    except ApiError as exc:
        print(f"\nAPI call failed: {exc}", file=sys.stderr)
        if exc.status_code == 401:
            print(
                "\n401 usually means the Authorization Callback Domain on your Strava app "
                "doesn't match the redirect URI. It should be exactly: localhost",
                file=sys.stderr,
            )
        return 1

    name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
    print(f"\n✓ Authorized as {name or athlete.get('username')} (id: {athlete.get('id')})")
    print(f"  tokens cached at {auth.store.path}")
    if client.last_rate_limit:
        print(f"  rate limit: {client.last_rate_limit.describe()}")
    print("\nNext: python scripts/inspect_strava.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
