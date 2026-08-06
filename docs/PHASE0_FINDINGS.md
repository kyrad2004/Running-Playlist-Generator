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

## Finding 4 — Half the activity records are duplicates

Measured against a live account over 365 days: **56 run activities, 38 distinct
runs.** Eighteen were the same run recorded twice, by Nike Run Club syncing to
Strava while the Strava app also recorded.

Deduplicating changes the numbers materially:

| | As recorded | Deduplicated |
|---|---|---|
| Runs | 56 | 38 |
| Heart rate | 41% | **58%** |
| Cadence | 18% | **26%** |

Left in, duplicates double weekly volume, break any 80/20 easy-hard split, and
let "best recent effort" pick whichever copy is listed first. This has to happen
in Phase 1 ingestion, before anything reads the data.

`rpg.dedupe` groups same-day activities by distance and duration tolerance
rather than equality — GPS drift and unmatched warmups mean the copies rarely
agree exactly, with observed gaps from 0.0% to 21%. Two details that matter:

- **Merge the fields, keep one record's pace.** Only null fields are filled, so
  pace from one device is never mixed with heart rate from another.
- **A human-typed name beats Strava's auto-generated one.** The richer copy of
  the 2026-02-22 pair was auto-named "Morning Run"; the other was "Malta Half
  Marathon". That label is what tells a VDOT estimate the effort was maximal, so
  losing it would hide the single most valuable data point in the account.

Anything outside a tight tolerance is flagged for review rather than merged
silently, as is a case where both copies recorded heart rate and disagreed
(observed: 168 vs 177 bpm on one run — one sensor is simply wrong).

---

## Finding 5 — Cadence tracks pace (corrected)

**This finding was wrong on first measurement, and the correction is the point.**

The first reading used a 365-day window, which yielded 10 cadence-carrying runs
clustered inside a 1.8 min/mi band. Correlation came out at r = -0.12, and the
conclusion was that cadence is a constant near 160 spm — one parameter, since
the data didn't support two. The caveat recorded at the time was that every
point was an easy-to-moderate effort and it should be revisited if faster
running appeared.

Widening to 730 days produced **37 runs across 3.9 min/mi — 7:15 to 11:09**:

```
   r = +0.61 against speed   (r² 0.37)
   r = -0.54 against pace    (r² 0.29)
   cadence = 131.9 + 0.1744 x speed(m/min),  ±3.9 spm
```

The relationship was always there; the first sample was too narrow to see it.
Fitting against **speed** rather than pace is the better form — cadence scales
with how fast you're moving, and pace is its reciprocal.

Fit quality is moderate, not decisive. Speed explains 37% of cadence variation
and 3.9 spm of scatter remains, so the model shifts the target sensibly across
zones but should never be read as precise.

### What this changes for Phase 3

The target is a function of the prescribed pace, not a constant — which is what
makes the playlist *pace-synced* rather than a fixed-tempo mix:

| Zone | Pace | Target cadence | Half-time BPM band |
|---|---|---|---|
| Easy | 10:36 | 158 spm | 77–82 |
| Marathon | 8:38 | 164 spm | 80–85 |
| Threshold | 8:18 | 166 spm | 80–85 |
| Interval | 7:38 | 169 spm | 82–87 |

`fit_cadence_model()` refuses to return a model when the sample is too narrow or
the correlation too weak (|r| < 0.4), falling back to a constant — so the
original 10-point sample would still, correctly, produce no model.

### The methodological lesson

A correlation measured over a narrow slice of a variable's range says nothing
about the full range. The first conclusion wasn't a miscalculation; it was a
correct calculation on unrepresentative data. What caught it was widening the
window and re-running the same check — which is the argument for having the
tool report `r` rather than eyeballing a table.

---

## Finding 5b — Without ISRC, matching is the hard part, not lookup

The obvious framing of BPM sourcing is "call an API." That part is trivial. The
part that decides whether Phase 3 works is that Spotify's February 2026 revision
removed `external_ids`, and with it **ISRC** — the clean join key to any music
database. Every lookup now goes through artist and title strings:

```
"Blinding Lights - Remastered 2021"    vs   "Blinding Lights"
"Levitating (feat. DaBaby)"            vs   "Levitating"
"Mr. Brightside" by a covers band      vs   The Killers' original
```

The last is the dangerous one. A confident match to the wrong recording gives a
wrong BPM, and **a wrong BPM is worse than a missing one** — it puts a track in
the playlist at the wrong tempo, and nothing surfaces the error until you're
running to it. So `rpg.bpm` treats "no confident match" as a first-class result,
never an exception, and scores every match:

- Artist agreement is weighted hardest (0.65 vs 0.35), because a matching title
  under a different artist is a cover, and covers are routinely at another tempo.
- Live, remix, acoustic and sped-up markers are stripped to help matching but
  **lower confidence by 25%**, since those recordings genuinely differ in tempo.
- Misses are cached alongside hits. A track no database knows stays unknown, and
  re-asking wastes rate limit on every run.

### The half/double ambiguity is nearly free here

BPM databases and detectors routinely disagree by a factor of two — a track that
feels like 160 gets listed at 80. Normally that has to be resolved. But a runner
at 160 spm can stride to either, so `rpg.cadence` accepts multipliers of 0.5, 1
and 2, and the ambiguity mostly stops mattering.

Which is fortunate, because **almost no popular music sits at 160 BPM**. The
usable raw bands at a 160 ±5 target are:

| Multiplier | Raw BPM | Use |
|---|---|---|
| ×2 | 77.5–82.5 | half-time — the common case |
| ×1 | 155–165 | rare in pop |
| ×0.5 | 310–330 | effectively never |

That is roughly 35 BPM of usable space. Against a 1,501-track library it should
leave a workable pool, but the number to measure is what fraction of a **real**
library lands there — not what fraction a provider can price.

### Still open

No concrete provider is wired. [GetSongBPM](https://getsongbpm.com/api) is the
leading candidate: free, 3,000 requests/hour, established — but a backlink to
their site is **mandatory**, and accounts are suspended without notice if it's
missing. Local analysis via `librosa` is ruled out: it needs audio files, and
with `preview_url` dead and a streaming-only library there is nothing to analyze.

The measurement that decides Phase 3's design is **match rate against real saved
tracks**: 80%+ means per-track BPM works as planned; around 30% means falling
back to curated BPM-band playlists.

---

## Finding 6 — The fitness anchor is stale, and the engine has to know that

The best VDOT input available is the Malta Half Marathon: 13.27 mi at 9:32/mi.
It is also **165 days old**, and the training history around it is not flat:

```
2025-08    9 runs   38.0 mi   ████████████████████
2025-09    4 runs   12.6 mi   ███████
2025-10    2 runs    6.4 mi   ███
2025-11    1 run     2.6 mi   █
2025-12    2 runs    8.7 mi   █████
2026-01    9 runs   35.0 mi   ██████████████████
2026-02    6 runs   36.5 mi   ███████████████████   ← half marathon
2026-03    1 run     1.7 mi   █
2026-04    1 run     2.8 mi   █
2026-05    1 run     2.3 mi   █
2026-07    1 run     2.4 mi   █
2026-08    1 run     1.8 mi   █
```

Two training blocks, then five months at roughly one short run per month. Last
30 days: 2 runs, 4.2 miles.

A naive "rolling VDOT from best recent effort" would read the half marathon,
conclude half-marathon fitness, and prescribe paces accordingly — to someone who
has run 6.5 miles in three months. That is not a rounding error; it's the kind
of recommendation that causes injuries.

**Phase 2 needs a staleness rule, not just a max over the window.** Options,
roughly in order of effort:

1. Decay the VDOT estimate with the age of the effort it came from.
2. Require a minimum recent volume before trusting a history-derived estimate at
   all, and fall back to onboarding when it isn't met.
3. Weight efforts by recency when picking the "best" one.

A related signal worth surfacing: recent short runs sit around 8:55–9:36/mi,
which is at or faster than the half marathon pace of 9:32/mi. Running short
distances at race pace with no easy volume underneath is exactly the pattern the
80/20 rule in Phase 2 Week 5 exists to catch, so there is a real test case here
for the engine's output — and a real answer for "would you trust what it told
you?"

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
Saved tracks are the better source — the probed account has **1,501** of them,
which is a large enough pool that BPM coverage, not catalogue size, is the
binding constraint.

**Simplified:** Phase 3's cadence mapping. Finding 5 replaces the pace→cadence
model with a single constant, which removes the calibration step entirely.

**Added:** deduplication in Phase 1 (Finding 4) and a staleness rule in Phase 2
(Finding 6). Neither was in the original plan; both are required for the output
to be trustworthy.

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
