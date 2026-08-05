# Pace-Synced Running Playlist

Pulls Strava run history, works out a target training zone, matches music tempo
to that zone, and generates a Spotify playlist with an AI-written rationale.

**Current phase:** Phase 0 — API setup and feasibility. Auth for both providers
is built and tested; the plan logic engine is not written yet.

---

## Quick start

```bash
git clone https://github.com/kyrad2004/Running-Playlist-Generator.git
cd Running-Playlist-Generator

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in — see docs/API_SETUP.md

python scripts/auth_spotify.py     # one-time browser authorization
python scripts/auth_strava.py

python scripts/doctor.py           # check config and tokens
python scripts/probe_spotify.py    # what your Spotify app can actually reach
python scripts/inspect_strava.py   # your real runs and their field coverage
```

Full walkthrough, including the two mistakes each provider's dashboard invites:
**[docs/API_SETUP.md](docs/API_SETUP.md)**.

---

## Read this before Phase 3

Spotify deprecated `/v1/audio-features` in November 2024. Apps created after that
date get a `403`, and `preview_url` returns `null`, so neither Spotify's tempo
data nor local analysis of Spotify audio is available. **BPM has to come from
somewhere else**, and that's a bigger piece of work than the original plan
assumed.

Strava is unaffected and supplies everything Phases 1–2 need.

The full writeup — including which endpoints moved in the February 2026 API
revision, three BPM sourcing options, and the Strava field gotchas — is in
**[docs/PHASE0_FINDINGS.md](docs/PHASE0_FINDINGS.md)**.

---

## Layout

```
src/rpg/
  config.py      credentials from .env, scope definitions
  tokens.py      token cache (0600, atomic writes, expiry with leeway)
  oauth.py       PKCE helpers + one-shot loopback callback server
  transport.py   shared HTTP: 429/5xx retry, Strava rate-limit headers
  spotify.py     Authorization Code + PKCE, Web API client
  strava.py      Authorization Code, API v3 client
  activity.py    Strava payloads -> pace, cadence, HR coverage

scripts/
  auth_spotify.py     one-time Spotify authorization
  auth_strava.py      one-time Strava authorization
  doctor.py           check deps, credentials, cached tokens
  probe_spotify.py    probe every endpoint the project needs
  inspect_strava.py   pull real runs, report field coverage

docs/
  API_SETUP.md        registration walkthrough + troubleshooting
  PHASE0_FINDINGS.md  what's blocked, what moved, what it means
```

Auth details worth knowing: Spotify uses **PKCE**, so no client secret is stored
anywhere. Strava rotates its refresh token on every refresh, so each one is
persisted immediately. Tokens live in `.tokens/` — gitignored, mode `0600`.

---

## Tests

```bash
python -m pytest tests/ -q
```

54 tests, no network and no credentials required. They cover the things that
only surface against the live API: PKCE challenge derivation (pinned to the
RFC 7636 test vector), Spotify preserving a refresh token it doesn't re-send,
Strava's refresh-token rotation, partial-scope grants, 429 retry with
`Retry-After`, and the cadence doubling.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | API auth + feasibility | auth done; BPM source still to choose |
| 1 | Strava ingestion → SQLite | not started |
| 2 | VDOT, zones, HR/no-HR branch | not started |
| 3 | BPM matching → playlist | blocked on BPM source |
| 4 | AI rationale | not started |
