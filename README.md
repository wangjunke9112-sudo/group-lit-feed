# Group Literature Feed

A daily literature monitor for the group. It pulls new papers from a fixed list of
journals via their RSS feeds, keeps only those matching our keywords, groups them by
journal, and publishes the result as a simple webpage. Everything runs for free on
GitHub (no server to maintain).

```
feeds.py            ← the journals and keywords (edit this)
aggregate.py        ← fetches feeds, filters, builds data/papers.json
index.html          ← the webpage (reads data/papers.json in the browser)
data/papers.json    ← the rolling archive (auto-generated daily)
requirements.txt    ← Python dependencies
.github/workflows/update.yml  ← daily refresh on GitHub Actions
```

## What it does each day

1. Fetches all 27 journal feeds (Nature portfolio, ACS, RSC, Wiley, Science, Cell Press).
2. Reads each entry's title and abstract and keeps it only if it matches a keyword.
3. Merges new hits into the archive, removes duplicates (by DOI/link), and drops
   anything older than 60 days.
4. Commits the updated `data/papers.json`; GitHub Pages re-publishes the page.

The webpage groups papers by journal, shows title, journal, date, authors, and matched
keywords, has a **Read more** button that reveals the abstract (already fetched from the
feed, so no extra click-through to read it), and a link straight to the paper. A search
box and a date window let anyone narrow things down.

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

Other `SETTINGS`: `days_to_keep` (archive length), `abstract_max_chars`, request timeout.

---

## Running locally (optional, to test before pushing)

```bash
pip install -r requirements.txt
python aggregate.py          # fetches feeds, writes data/papers.json
python -m http.server        # then open http://localhost:8000
```

Open the page through `http.server` (not by double-clicking `index.html`), because
browsers block the `fetch` of `papers.json` from a `file://` path.

Offline logic check (no network): `python aggregate.py --selftest`.

---

## Notes and caveats

- **A feed occasionally fails.** Publishers sometimes rate-limit automated requests
  (a 403 from Wiley or Science now and then). The run never stops for one bad feed — it
  logs the failure and continues, and a small banner on the page lists any feed that
  didn't update that day. It's retried the next run.
- **RSS shows new/early articles, not the full back-catalogue.** Feeds carry the most
  recent items (advance-online / articles-in-press / current issue), which is what a daily
  monitor wants. The 60-day archive accumulates these over time.
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
