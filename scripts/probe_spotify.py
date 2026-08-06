#!/usr/bin/env python3
"""Probe every Spotify endpoint this project depends on and report what works.

Spotify has removed or restricted a lot of the Web API (Nov 2024 and Feb 2026),
and what your app can reach depends on when it was created. Rather than trust a
blog post, this asks YOUR app directly and prints the ground truth.

    python scripts/probe_spotify.py            # read-only probes
    python scripts/probe_spotify.py --write    # also test playlist creation
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import _bootstrap  # noqa: F401

from rpg.config import ConfigError
from rpg.oauth import OAuthError
from rpg.spotify import SpotifyClient
from rpg.transport import ApiError

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

# Probes that are expected to fail are informative, not errors.
EXPECTED_FAILURE = "Phase 3 BPM — EXPECTED TO FAIL"


@dataclass
class ProbeResult:
    name: str
    needed_for: str
    ok: bool
    detail: str

    @property
    def label(self) -> str:
        """Padded to a fixed width BEFORE colouring — ANSI escapes have no
        display width but do count toward len(), which breaks column alignment."""
        text = "OK" if self.ok else "FAIL"
        colour = GREEN if self.ok else RED
        return f"{colour}{text:<4}{RESET}"


def probe(name: str, needed_for: str, fn) -> ProbeResult:
    try:
        detail = fn()
        return ProbeResult(name, needed_for, True, detail or "")
    except ApiError as exc:
        note = f"HTTP {exc.status_code}"
        if exc.status_code == 403:
            note += " (forbidden — endpoint restricted or app not allowlisted)"
        elif exc.status_code == 404:
            note += " (removed from the API)"
        return ProbeResult(name, needed_for, False, note)
    except Exception as exc:  # noqa: BLE001 - probe should never abort the run
        return ProbeResult(name, needed_for, False, str(exc)[:120])


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Spotify API capabilities.")
    parser.add_argument(
        "--write",
        action="store_true",
        help="also create a real (private) test playlist to verify write access",
    )
    args = parser.parse_args()

    try:
        client = SpotifyClient()
    except (ConfigError, OAuthError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    results: list[ProbeResult] = []
    state: dict = {}

    def check_profile() -> str:
        me = client.me()
        state["user_id"] = me.get("id")
        return f"user {me.get('id')}"

    def check_search() -> str:
        tracks = client.search_tracks("running", limit=10)
        if tracks:
            state["track_uri"] = tracks[0]["uri"]
            state["track_id"] = tracks[0]["id"]
        return f"{len(tracks)} result(s); max is 10 since Feb 2026"

    def check_saved() -> str:
        payload = client.saved_tracks(limit=1)
        return f"{payload.get('total', '?')} saved track(s) in library"

    def check_top() -> str:
        # Ask for a real page — limit=1 always returns 1 and tells us nothing
        # about how big the candidate pool actually is.
        payload = client.top_tracks(limit=50)
        count = len(payload.get("items", []))
        note = "" if count else " — needs listening history Spotify considers sufficient"
        return f"{count} track(s) available{note}"

    def check_audio_features() -> str:
        track_id = state.get("track_id")
        if not track_id:
            raise RuntimeError("no track id available (search probe failed)")
        payload = client.get(f"/audio-features/{track_id}")
        return f"tempo={payload.get('tempo')} BPM"

    results.append(probe("GET /me", "identify account", check_profile))
    results.append(probe("GET /search (track)", "Phase 3 candidate pool", check_search))
    results.append(probe("GET /me/tracks", "Phase 3 candidate pool", check_saved))
    results.append(probe("GET /me/top/tracks", "Phase 3 candidate pool", check_top))
    results.append(probe("GET /audio-features/{id}", EXPECTED_FAILURE, check_audio_features))

    if args.write:

        def check_create() -> str:
            playlist = client.create_playlist(
                name="[rpg] api probe — safe to delete",
                description="Created by scripts/probe_spotify.py to verify write access.",
                public=False,
            )
            state["playlist_id"] = playlist.get("id")
            return f"created playlist {playlist.get('id')}"

        def check_add_items() -> str:
            playlist_id = state.get("playlist_id")
            uri = state.get("track_uri")
            if not playlist_id or not uri:
                raise RuntimeError("need both a playlist and a track uri")
            client.add_items(playlist_id, [uri])
            return "added 1 track"

        results.append(probe("POST /me/playlists", "Phase 3 playlist creation", check_create))
        results.append(probe("POST /playlists/{id}/items", "Phase 3 add tracks", check_add_items))

    # -- report ----------------------------------------------------------
    width = max(len(r.name) for r in results) + 2
    print("\nSpotify API capability probe")
    print("─" * 78)
    for r in results:
        print(f"  {r.name:<{width}} {r.label}  {r.detail}")
        print(f"  {'':<{width}} {'':4}  ↳ {r.needed_for}")
    print("─" * 78)

    audio = next(r for r in results if "audio-features" in r.name)
    if audio.ok:
        print(
            "\nNOTE: /audio-features responded. Your app predates the Nov 2024 cutoff, so "
            "Spotify BPM is available to you — but it is deprecated and could be withdrawn. "
            "Keep the BPM source behind an interface so it can be swapped."
        )
    else:
        print(
            "\nCONFIRMED: /audio-features is unavailable to this app (deprecated for apps "
            "created after 2024-11-27). Spotify cannot be your BPM source.\n"
            "See docs/PHASE0_FINDINGS.md for the alternatives."
        )

    if state.get("playlist_id"):
        print(
            f"\nA test playlist was created ({state['playlist_id']}). "
            "Delete it from your Spotify library when you're done."
        )

    failed_critical = [r for r in results if not r.ok and r.needed_for != EXPECTED_FAILURE]
    if failed_critical:
        print(f"\n{len(failed_critical)} endpoint(s) this project needs are unavailable.")
        return 1

    print("\n✓ Every endpoint this project needs (other than BPM) is reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
