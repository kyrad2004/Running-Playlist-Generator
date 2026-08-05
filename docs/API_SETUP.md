# API Setup

One-time registration for both providers. Budget about 15 minutes.

---

## 1. Local setup

```bash
git clone https://github.com/kyrad2004/Running-Playlist-Generator.git
cd Running-Playlist-Generator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Everything below fills in `.env`. It is gitignored — keep it that way.

---

## 2. Spotify

### Register the app

1. Go to <https://developer.spotify.com/dashboard> and log in.
2. **Create app**.
   - *App name* / *description*: anything.
   - *Redirect URI*: `http://127.0.0.1:8888/callback` — type it exactly.
   - *Which API/SDKs*: check **Web API**.
3. Save, then open **Settings** and copy the **Client ID** into `.env`.

You do **not** need the client secret. This project uses Authorization Code with
PKCE, which is the right flow for an app that runs on your own machine.

### Two things that will bite you

**Use `127.0.0.1`, not `localhost`.** Spotify rejects `localhost` in redirect
URIs and requires a literal loopback IP. `scripts/auth_spotify.py` refuses to
start if you get this wrong, but the dashboard will also reject it at save time.

**Add yourself as a user.** A new app is in Development Mode, which only serves
accounts on an explicit allowlist — including your own. In the dashboard:
**Settings → User Management → Add user**, and enter the name and email on your
Spotify account. Skipping this produces a `403` on every API call *after* a
successful login, which looks like a code bug and isn't.

Development Mode also caps you at **5 users** and **1 app per developer**, and
requires a **Premium** account for apps created after February 2026. For a
personal project all three are fine.

### Authorize

```bash
python scripts/auth_spotify.py
```

A browser opens, you approve, the terminal prints your account name. Tokens are
cached in `.tokens/spotify.json` (mode `0600`) and refresh automatically.

### Verify what your app can actually reach

```bash
python scripts/probe_spotify.py            # read-only
python scripts/probe_spotify.py --write    # also creates a throwaway playlist
```

Spotify has removed a lot of endpoints, and what *your* app can use depends on
when it was created. This asks your app directly instead of trusting docs.
`/audio-features` is expected to fail — see [PHASE0_FINDINGS.md](PHASE0_FINDINGS.md).

---

## 3. Strava

### Register the app

1. Go to <https://www.strava.com/settings/api>.
2. Fill in the form:
   - *Application Name*: anything.
   - *Category*: Training.
   - *Website*: any URL.
   - **Authorization Callback Domain**: `localhost` — just the word, no scheme,
     no port, no path.
3. Copy **Client ID** and **Client Secret** into `.env`.

Unlike Spotify, Strava has no PKCE support, so the client secret is required.

### Two things that will bite you

**The callback domain is a domain, not a URL.** Entering
`http://localhost:8899/exchange_token` there produces a login page that redirects
nowhere. It must be exactly `localhost`.

**Tick every box on the consent screen.** Strava lets you approve a subset of the
requested scopes. Untick "View data about your private activities" and you get a
perfectly valid token that silently omits private runs — which reads as "I have
fewer runs than I thought" rather than as a permissions problem.
`scripts/auth_strava.py` checks the granted scopes and fails loudly if
`activity:read_all` is missing.

### Authorize and pull real data

```bash
python scripts/auth_strava.py
python scripts/inspect_strava.py --days 60 --save
```

`inspect_strava.py` is the Phase 0 Week 1 deliverable: it prints your recent
runs, reports what share have heart rate and cadence, previews the Phase 2
data-sufficiency check, and shows which fields only exist on the detail
endpoint. `--save` writes raw JSON to `data/samples/` (gitignored) for reading
through by hand.

---

## 4. Check everything

```bash
python scripts/doctor.py
```

Reports dependencies, credentials, cached tokens and their expiry. Run this
first when something breaks — it separates config problems from API problems
without spending rate limit.

---

## Rate limits

**Strava** — 100 requests / 15 min, 1,000 / day for reads. The 15-minute window
resets on the quarter hour; the daily counter at midnight UTC. Every response's
usage headers are parsed and printed by the scripts. Pulling 60 days of runs
costs 1–2 requests, so you have plenty of headroom, but a loop that fetches
detail for every activity will burn it fast.

**Spotify** — a rolling ~30-second window, not published as a fixed number.
`429` responses carry `Retry-After`, which `rpg/transport.py` honours.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Spotify `403` on every call after a successful login | Your account isn't in **User Management** on the app |
| Spotify `403` on `/audio-features` only | Expected — endpoint is deprecated for new apps |
| `INVALID_CLIENT: Invalid redirect URI` | The URI in `.env` doesn't byte-match the dashboard, or uses `localhost` |
| Strava `401` after authorizing | Authorization Callback Domain isn't exactly `localhost` |
| Strava returns fewer runs than you have | `activity:read_all` wasn't granted — re-run with `--force` |
| `Could not listen on 127.0.0.1:8888` | Port in use; change it in `.env` **and** on the provider |
| Browser doesn't open | Use `--no-browser` and paste the printed URL |
| Token errors after editing `.env` | `python scripts/auth_spotify.py --force` |
