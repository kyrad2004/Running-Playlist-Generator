# Phase 0 Findings — API Feasibility

> The plan predicted Phase 0 was "the phase most likely to reveal a blocker (an
> API being more restricted than expected)." It did. This is that writeup.

**Status:** Strava is fine and gives us everything Phases 1–2 need. Spotify still
works for search and playlist creation, but **cannot supply BPM**, which changes
how Phase 3 has to be built.

---

## Finding 1 — Spotify can no longer give us BPM. This is the blocker.

On **2024-11-27** Spotify deprecated `/v1/audio-features`, `/v1/audio-analysis`,
`/v1/recommendations`, related-artists, and featured-playlists. Apps that were
already in extended quota mode kept access; **every app created since gets a
`403`**. Extended quota mode is not open to new apps without ~250K monthly
active users, so there is no path to it for a personal project.

`audio-features` was the tempo source the plan assumed. It's gone.

### The `librosa` fallback is also closed

Week 1 framed this as a choice between "self-computed audio analysis (librosa)"
and "third-party BPM lookup." Self-computed analysis needs audio, and the same
November 2024 change made `preview_url` return `null` for new apps — the
30-second clips are no longer served. Spotify streams cannot be analyzed
locally.

So librosa is only viable if **you already have the audio files** (a local MP3
library). It is not a general solution over a Spotify catalogue.

### Verify it yourself

```bash
python scripts/probe_spotify.py
```

This hits `/audio-features` with your own token and reports the real status. If
it unexpectedly succeeds, your app predates the cutoff — good, but keep the BPM
source behind an interface anyway, because deprecated endpoints get withdrawn.

### Consequence: matching gets harder than a straight ID lookup

The February 2026 revision also removed `external_ids` from track objects, which
is where **ISRC** lived. ISRC was the clean join key to any external music
database. Without it, matching a Spotify track to an external BPM record has to
go through **artist + title string matching**, which is fuzzy: remasters, live
versions, features, and remixes all collide. Whatever BPM source you pick needs
a confidence threshold and a "no confident match" path — a wrong BPM is worse
than a missing one, because it silently produces a playlist at the wrong tempo.

### Recommended approach

Define a narrow interface now and pick the implementation second:

```python
class BpmProvider(Protocol):
    def bpm_for(self, artist: str, title: str) -> BpmResult | None: ...
```

`BpmResult` should carry `bpm`, `confidence`, and `source`. Everything downstream
depends on the interface, not the vendor — so when a provider dies (and one
already has), it's a one-file change.

Three implementations worth evaluating, cheapest first:

| Option | Coverage | Cost | Notes |
|---|---|---|---|
| **A third-party BPM lookup API** (GetSongBPM, and several newer audio-feature APIs marketing themselves as `audio_features` replacements) | Broad | Free tiers exist; some require an attribution backlink | Fastest path. Vet the free tier's rate limit and match quality on *your* library before committing — several of these are young and their coverage claims are unverified. |
| **AcousticBrainz data dump** | Millions of tracks, frozen in 2022 | Free | The project stopped collecting data, but the dump is still downloadable and keyed by MusicBrainz ID. No coverage of anything released after 2022, and you'd add a MusicBrainz lookup step. Good as a local cache layer. |
| **Local audio analysis** (`librosa` or `essentia`) | Only what you own | Free (compute) | The most defensible answer in an interview — you computed it — but it only works over local files. |

A pragmatic combination: a lookup API as the primary, a local cache of resolved
BPMs (so you pay per track once), and a documented "unmatched" bucket.

**A cheaper reframing worth considering:** cadence-matching does not strictly
require per-track BPM. Curated running playlists are already organized by BPM
band, and Spotify search over playlist names ("170 BPM running") returns usable
pools. That trades precision for zero BPM infrastructure. It's weaker as a
portfolio story, but it de-risks Phase 3 entirely — worth keeping as the
fallback if BPM sourcing eats more than a week.

---

## Finding 2 — Spotify's February 2026 revision moved the endpoints we need

Search and playlist creation still work, but under different paths. The client in
`src/rpg/spotify.py` already uses the current ones:

| Purpose | Old | Current |
|---|---|---|
| Create playlist | `POST /users/{user_id}/playlists` | `POST /me/playlists` |
| Add tracks | `POST /playlists/{id}/tracks` | `POST /playlists/{id}/items` |
| Save / follow | per-entity endpoints | `PUT /me/library` |
| Check saved | per-entity endpoints | `GET /me/library` |

Also changed:

- **Search returns at most 10 results per type** (was 50). Phase 3's candidate
  pool has to come from paging or from multiple queries, not one big call.
- Playlist objects renamed `tracks` → `items`, and playlist entries `track` →
  `item`. Any tutorial older than 2026 will parse the wrong keys.
- Removed fields: `available_markets`, `external_ids` (ISRC — see above),
  `popularity`, `followers`, `label`, `explicit_content`. `popularity` is worth
  noting because it's a natural ranking signal for Phase 3 and it no longer
  exists.
- Development Mode now requires a **Premium** account, and caps you at **1 app**
  and **5 users**. Fine for personal use; worth stating out loud in a portfolio
  writeup, because "why isn't this deployed publicly" has a real answer.

---

## Finding 3 — Strava gives us everything Phases 1–2 need

No blockers. Confirmed field availability on run activities:

| Field | Where | Notes |
|---|---|---|
| `distance`, `moving_time`, `elapsed_time` | summary + detail | metres, seconds |
| `average_speed`, `max_speed` | summary + detail | m/s — pace is derived, never sent directly |
| `has_heartrate` | summary + detail | the Phase 2 branch key |
| `average_heartrate`, `max_heartrate` | summary + detail | absent entirely when `has_heartrate` is false |
| `average_cadence` | summary + detail | **one leg only** — see below |
| `splits_standard`, `splits_metric`, `laps` | **detail only** | not in the list response |
| `start_date_local` | summary + detail | use this, not `start_date`, for "which day did I run" |

### Three gotchas, all encoded in the code

**Cadence is halved.** Strava reports running cadence as RPM for a single leg, so
a 170 spm runner shows up as `85`. Every BPM target derived from raw
`average_cadence` would be exactly half right. `rpg.activity.steps_per_minute()`
doubles it, and there's a test pinning that.

**Splits require a second request.** `GET /athlete/activities` returns summary
objects with no splits or laps. Per-split pace needs `GET /activities/{id}` per
activity — which matters for rate limit: 60 days of runs is ~1 request, but
detail for all of them is one request each.

**Refresh tokens rotate.** Every Strava refresh returns a *new* refresh token and
invalidates the old one. Failing to persist it locks you out until you
re-authorize. `rpg.strava.StravaAuth.refresh()` saves before returning, with a
test covering it.

### Rate limits

Two limits apply simultaneously and are reported in different headers: overall
(`X-RateLimit-*`, 200 per 15 min / 2,000 per day) and read-only
(`X-ReadRateLimit-*`, 100 / 1,000). This project only reads, so the read limit
binds first. `rpg.transport.RateLimitStatus` parses both and labels which one
matters.

---

## Finding 4 — The real data is thinner than the plan assumed

Measured against a live account, 60-day window:

| | Result | Consequence |
|---|---|---|
| Runs | **2** (need 7+) | Cold start isn't an edge case — it's the only case |
| Heart rate | **0%** | The `has_heartrate` branch has no data to validate against |
| Cadence | **0%** | Phase 3 cannot match music to *measured* cadence |
| Pace | 100% | The one signal we can rely on |

This is the profile of phone-recorded runs. Strava's mobile app captures GPS, so
distance and pace are solid, but heart rate needs a strap or watch and running
cadence needs a footpod or watch — neither is inferred from the phone. Unless
the hardware changes, **these columns stay empty**, so they should be treated as
permanently absent rather than as a gap that fills in later.

### What has to change

**Cold start becomes the primary path, not a fallback.** Phase 2 Week 4 lists
the data-sufficiency check and the onboarding fallback as separate items, with
rolling VDOT as the main route. At 2 runs it's the reverse: build the onboarding
estimate first, and treat rolling VDOT as the upgrade that switches on once
history exists.

**The HR branch becomes untestable, not unbuildable.** It can still be written —
it's a good design point and worth showing — but the plan's benchmark ("test
against your own real data") can't be met for that path. Better to say so
explicitly than to pretend it was validated.

**Cadence has to be modeled from pace.** This is the substantive change. Phase 3
assumed measured cadence; with none available, target cadence must be derived.
Step rate rises with speed in a roughly linear way over normal training paces,
so a `cadence ≈ a + b × speed` model with a user-supplied calibration point (count
your steps for 30 seconds on one run, enter it once) is the workable version.
Treat the model as an assumption to test on a real run, which is exactly what
the Phase 3 benchmark already asks.

**Half-time matching stops being optional.** A 170 spm target has no useful pool
of 170 BPM music — most popular music sits between 90 and 140 BPM. Matching 85
BPM at half-time is the normal case, not the edge case the plan implies.

### Two fields worth exploiting

Dumping a detail payload surfaced two things the plan doesn't mention:

- **`best_efforts`** — Strava pre-computes fastest 400m, ½ mile, 1k and mile
  splits per run. That's a ready-made input to VDOT, replacing the "roll your own
  best recent effort" work in Week 4. Caveat: efforts from easy runs aren't
  maximal, so VDOT derived from them **underestimates** fitness. It's a floor,
  not an estimate — which is another argument for asking the user directly during
  onboarding.
- **`perceived_exertion`** — a user-entered 1–10 effort rating. For an athlete
  with no HR data this is the closest available stand-in for intensity, and it
  costs nothing but the habit of filling it in. Worth checking whether it's
  populated before designing around it.

Both live on the detail endpoint only, at one request per activity.

---

## What this means for the plan

**Unchanged:** Phase 1 (Strava ingestion → SQLite) and Phase 2 (VDOT, zones,
HR-vs-no-HR branch). These were always the most defensible part of the project
and nothing about them is blocked.

**Changed:** Phase 3 now contains a real sub-project — sourcing BPM — that the
original plan assumed was a single API call. Two options:

1. Pull "choose and validate a BPM source" forward into Phase 0 Week 2, next to
   the existing "run one test track through your chosen BPM method" item. It's
   the same task, just bigger than it looked.
2. Accept the curated-playlist fallback for Phase 3 and treat true per-track BPM
   as a stretch goal.

Either way this is a good outcome for the portfolio story: "the API I designed
around was deprecated mid-project, here's how I found out and what I changed"
is a stronger interview answer than a project where nothing went wrong.

**Watch out for:** the Phase 3 ranking step. With `popularity` removed and search
capped at 10 results, the candidate pool is narrower than the plan assumed.
Saved tracks (`GET /me/tracks`) and top tracks are likely to be the better
sources — both are probed by `scripts/probe_spotify.py`.

---

## Sources

- [Spotify: Update on Developer Access and Platform Security (Feb 2026)](https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security)
- [Spotify Web API Changelog — February 2026](https://developer.spotify.com/documentation/web-api/references/changes/february-2026)
- [February 2026 Web API Dev Mode Migration Guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)
- [rspotify #550 — community breakdown of the Feb 2026 changes](https://github.com/ramsayleung/rspotify/issues/550)
- [Spotify community: 403 on /v1/audio-features](https://community.spotify.com/t5/Spotify-for-Developers/403-Forbidden-on-v1-audio-features-using-both-user-and-client/td-p/7200198)
- [Spotify community: Preview URLs deprecated](https://community.spotify.com/t5/Spotify-for-Developers/Preview-URLs-Deprecated/td-p/6791368)
- [Strava API rate limits](https://developers.strava.com/docs/rate-limits/)
- [Strava getting started / OAuth](https://developers.strava.com/docs/getting-started/)
