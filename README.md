# Group Literature Feed

A daily literature monitor for the group. It pulls new papers from a fixed list of
journals via their RSS feeds, keeps only those matching our keywords, groups them by
journal, and publishes the result as a simple webpage. Everything runs for free on
GitHub (no server to maintain).

```
feeds.py            ← the journals, keywords, ISSNs (edit this)
aggregate.py        ← daily job: fetches RSS feeds, filters, updates the data files
backfill.py         ← one-time job: pulls 2020→now history from Crossref
index.html          ← the webpage (reads the data files in the browser)
data/manifest.json  ← list of years + feed status (auto-generated)
data/papers-YYYY.json ← one file per year of papers (auto-generated)
requirements.txt    ← Python dependencies
.github/workflows/update.yml    ← daily refresh
.github/workflows/backfill.yml  ← manual one-time history backfill
```

## What it does each day

1. Fetches all 27 journal feeds (Nature portfolio, ACS, RSC, Wiley, Science, Cell Press
   incl. Joule and Matter).
2. Reads each entry's title and abstract and keeps it only if it matches a keyword.
3. Merges new hits into the archive, removes duplicates (by DOI/link), and keeps
   everything published on or after the start date (default 2020-01-01 — nothing is
   auto-deleted).
4. Commits the updated data files; GitHub Pages re-publishes the page.

The daily job only sees *recent* papers (that's all RSS carries). To populate the
**history back to 2020**, run the one-time backfill described below — it and the daily
job write to the same files.

## The archive: size, retention, and search

The feed keeps **every** matching paper from the start date onward, split into one file
per year (`data/papers-YYYY.json`). This matters for three reasons:

- **Size is not a concern.** A matching paper is roughly 1 KB of JSON. Filling 2020→now
  in this field gives an estimated low tens of thousands of papers — on the order of
  20–50 MB total, split across the per-year files. GitHub Pages allows a published site up
  to 1 GB and 100 GB of traffic per month, so you are far inside the limits with many years
  of headroom.
- **Splitting by year keeps the repository healthy.** Because the daily job only rewrites
  the *current* year's file, past years are frozen and don't pile up in the repo's history.
- **The page stays fast at any size.** `index.html` loads the years newest-first and shows
  results as each year arrives (a small "loading older years…" note disappears when done),
  renders in pages of 60 ("Load more"), and in *Group by journal* mode only draws a
  journal's papers when you expand it. So a 90-paper archive and a 40,000-paper archive both
  feel responsive.

**Searching the archive.** The page has a search box (matches title, abstract, authors and
keywords across the *entire* archive), a journal dropdown, publisher filter chips, a date
range with quick 7/30/90-day/All buttons, a newest/oldest sort, and the *Group by journal*
toggle. Every filter works over all years at once.

### Filling in the history (2020 → today) — the one-time backfill

RSS only carries recent papers, so the history comes from **Crossref** (a free index of
nearly all journal articles). `backfill.py` pulls every paper from these journals since
`start_date`, filters it with the *same* keyword rules as the daily job, and merges the
hits into the same per-year files. Run it once.

Two ways to run it:

**A. On GitHub (no install).** First do a quick safety check, then the real run:
1. *Actions → Historical backfill (one-time) → Run workflow*, tick **Dry run**, Run. This
   fetches and counts but writes nothing — check the log for sensible per-year totals.
2. If the numbers look right, run it again with Dry run **unticked**. It commits the data
   and the page fills with history within a few minutes.

**B. Locally.**
```bash
pip install -r requirements.txt
python backfill.py --verify-issns   # confirm every ISSN maps to the right journal
python backfill.py --dry-run        # fetch + count, write nothing
python backfill.py                  # full run, writes data/papers-YYYY.json
```
Then commit the `data/` folder (or just push). Re-running is safe — results merge and
de-duplicate, so the backfill and the daily job never collide.

Before the big run it's worth doing `--verify-issns` once (locally) or trusting the
defaults: it prints the journal name Crossref returns for each configured ISSN, so a wrong
ISSN is obvious. The seed search terms Crossref uses are in `CORE_QUERIES` in `feeds.py`;
every result is re-checked against your full `KEYWORDS` list, so those seeds only need to
cast a wide net.

**A note on older abstracts.** Crossref has abstracts for many papers but not all —
coverage is good for Wiley, ACS, RSC and Springer Nature, thinner for Elsevier/Cell Press
and Science. Papers without an abstract still appear with full title, authors, journal,
date and matched keywords (and are fully searchable); they just won't have a *Read more*.

---

## One-time setup (about 5 minutes)

1. **Create a repository.** On GitHub, make a new repository (e.g. `group-lit-feed`)
   and upload these files (or `git push` them).

2. **Enable GitHub Pages.** Repo → *Settings* → *Pages* → under *Build and deployment*
   set *Source* = "Deploy from a branch", *Branch* = `main`, folder = `/ (root)`, Save.
   After a minute the page is live at
   `https://<your-username>.github.io/<repo-name>/`.

3. **Enable Actions write access.** Repo → *Settings* → *Actions* → *General* →
   *Workflow permissions* → choose "Read and write permissions", Save. (This lets the
   daily job commit the updated data.)

4. **Run it once now.** Repo → *Actions* → "Refresh literature feed" → *Run workflow*.
   When it finishes, refresh your Pages URL — the first batch of papers appears.

That's it. From then on it refreshes automatically every day at 06:00 UTC. Change the
time in `.github/workflows/update.yml` (the `cron` line) if you prefer.

---

## Editing the journals and keywords

Everything you'll want to change is in **`feeds.py`**.

**Add a journal** — add one line to `FEEDS`. The URL patterns per publisher are documented
at the top of `feeds.py`. For a Nature-family journal you only need its code (the slug in
its nature.com URL); for ACS/Science the `jc=` code; for RSC the 2-letter code; for Wiley
the online ISSN with dashes removed.

**Add or remove keywords** — edit the `KEYWORDS` list. Matching is case-insensitive and
whole-word, so `tandem` won't match inside another word and `ion migration` won't match
inside "champion".

**Control the firehose** — in `SETTINGS`, `require_perovskite` (default `False`) is the
important knob:

- `False` → a paper is kept if it matches **any** keyword. Maximum recall, but broad
  journals (Nature, Science, JACS, Angewandte) will also surface silicon tandems, organic
  PV, biological CO₂ reduction, etc.
- `True` → a paper must match a keyword **and** mention "perovskite". Much cleaner if the
  group only cares about perovskite-related work.

Other `SETTINGS`: `start_date` (archive begins here; default `2026-01-01`),
`abstract_max_chars`, request timeout.

---

## Running locally (optional, to test before pushing)

```bash
pip install -r requirements.txt
python aggregate.py          # fetches feeds, writes data/papers-YYYY.json
python -m http.server        # then open http://localhost:8000
```

Open the page through `http.server` (not by double-clicking `index.html`), because
browsers block the `fetch` of the data files from a `file://` path.

Offline logic check (no network): `python aggregate.py --selftest`.

---

## Notes and caveats

- **A feed occasionally fails.** Publishers sometimes rate-limit automated requests
  (a 403 from Wiley or Science now and then). The run never stops for one bad feed — it
  logs the failure and continues, and a small banner on the page lists any feed that
  didn't update that day. It's retried the next run.
- **RSS shows new/early articles, not the full back-catalogue.** Feeds carry the most
  recent items (advance-online / articles-in-press / current issue), which is what a daily
  monitor wants. The archive accumulates these from your first run onward (see above).
- **RSC is migrating platforms in 2026.** If the RSC feeds (`feeds.rsc.org/rss/...`) ever
  stop returning items, check `https://pubs.rsc.org/en/ealerts/rssfeed` for the updated
  URL and edit the two RSC lines in `feeds.py`.
- **Abstracts come straight from the feed.** Most publishers include the abstract (or its
  first lines) in the feed, which is what "Read more" shows. A few feeds carry only a short
  teaser; in those cases the snippet shown is whatever the publisher provided.

### Optional: AI one-line summaries

The page currently shows the publisher's own abstract. If you later want a generated
one-sentence summary per paper instead, that can be added as a step in `aggregate.py`
(call an LLM API for each new paper and store the result in a `summary` field). It needs
an API key as a GitHub Actions secret and has a small per-paper cost, so it's left out of
the default free setup — ask and it can be wired in.
