# AI Highlight Reel Generator (100% Free)

Paste in a long video link, and this automatically finds the most
exciting moments and cuts them into one highlight reel — with captions —
posted straight to your Facebook Page as a Reel.

## How "the AI" actually picks moments (no magic, just clever scoring)

Every sentence-length chunk of the video gets a score:

```
score = loudness_spike + (excitement_keyword_count × 0.5)
```

- **Loudness spike**: moments where people talk louder/react (cheering,
  laughing, shouting) tend to be genuinely exciting — measured by
  comparing that moment's audio volume to the video's average.
- **Excitement keywords**: the transcript is scanned for words people
  say during exciting moments — "no way," "insane," "let's go," "haha,"
  etc.

The top N highest-scoring moments get cut out, stitched back together in
their original order (so it still tells a coherent mini-story), captioned,
and posted.

This is a real, legitimate technique — simplified from the same
loudness+keyword heuristics that actual "auto-highlight" tools use.

## Setup

Reuses your existing `FB_PAGE_ID` / `FB_PAGE_ACCESS_TOKEN` secrets and
posts using the **Reels API** (better reach than a plain video post).

Add these files to your repo:
```
.github/workflows/highlight_reel.yml
scripts/highlight_reel.py
requirements.txt   (use this folder's version)
```

## Usage

1. Actions tab → **AI Highlight Reel** → **Run workflow**.
2. Paste in `video_url` (works best with videos that have some length to
   them — a few minutes or more, so there's real material to pick from).
3. Optionally change `max_highlights` (default 6 — roughly a 30-60
   second reel depending on how long each moment is).
4. Run. This is the most processing-heavy project yet (downloads +
   transcribes twice + cuts + re-encodes multiple clips), so expect it
   to take a good few minutes.
5. Check your Facebook Page, or grab the artifact backup from the run
   page to preview first.

## Notes & limits

- **Works best on videos with real energy/reactions** — a calm
  instructional video won't have much for the loudness-spike detector to
  find. Podcasts, gaming clips, vlogs, reaction videos, sports
  commentary — anything with audible excitement — works best.
- **The scoring is a heuristic, not true "understanding"** — it's
  measuring loudness and keyword patterns, not actually comprehending
  what's funny or important. It'll sometimes surprise you with a great
  pick, and sometimes pick something is loud but not actually
  interesting. That's an honest limitation, not a bug.
- **Processing time**: this runs transcription twice (once to find
  highlights, once to caption the final reel) — a deliberate tradeoff for
  reliability over speed.
- **Cost**: $0, same free stack as your other projects.
