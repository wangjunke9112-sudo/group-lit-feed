#!/usr/bin/env python3
"""
aggregate.py -- build the group literature feed.

Run:           python aggregate.py
Self-test:     python aggregate.py --selftest   (no network needed)

What it does
------------
1. Fetches every RSS feed in feeds.FEEDS.
2. Extracts title, link, journal, date, abstract, authors, DOI from each entry.
3. Keeps entries whose title/abstract match feeds.KEYWORDS.
4. Merges with the existing archive (data/papers.json), de-duplicates by DOI/link,
   and drops anything older than SETTINGS["days_to_keep"].
5. Writes data/papers.json, which index.html reads in the browser.

The script is deliberately fault-tolerant: a single broken or rate-limited feed
is logged and skipped, never fatal.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse

from feeds import FEEDS, KEYWORDS, SETTINGS

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(value):
    """Strip HTML, unescape entities, collapse whitespace."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", str(value))
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _kw_pattern(keyword):
    """Whole-word / phrase regex for a keyword.

    Word boundaries are enforced on alphabetic edges only, so 'CO2' and
    'x-ray' behave, and 'ion migration' won't match inside 'champion'.
    """
    kw = re.escape(keyword.lower().strip())
    # internal whitespace in a phrase may be 1+ spaces in the source text
    kw = kw.replace(r"\ ", r"\s+")
    return re.compile(r"(?<![a-z])" + kw + r"(?![a-z])", re.IGNORECASE)


_KW_PATTERNS = [(k, _kw_pattern(k)) for k in KEYWORDS]
_PV_PATTERNS = [_kw_pattern(k) for k in SETTINGS.get("perovskite_terms", [])]


def matched_keywords(text):
    """Return the sorted list of distinct keywords found in text."""
    found = {k for k, pat in _KW_PATTERNS if pat.search(text)}
    return sorted(found, key=str.lower)


def is_relevant(text):
    """Decide if a paper is relevant; return (keep_bool, matched_keywords)."""
    hits = matched_keywords(text)
    if not hits:
        return False, []
    if SETTINGS.get("require_perovskite"):
        if not any(pat.search(text) for pat in _PV_PATTERNS):
            return False, hits
    return True, hits


# ---------------------------------------------------------------------------
# Entry normalisation
# ---------------------------------------------------------------------------
def pick_crossref_date(item):
    """Most precise ISO date from a Crossref work item (shared with backfill.py).

    Prefers the online publication date, then the most granular field available,
    and falls back to Crossref's 'created' timestamp (always a full date) rather
    than padding a bare year to January 1st. This is what keeps just-accepted /
    early-access papers from being dated to YYYY-01-01.
    """
    order = ("published-online", "published", "issued", "published-print", "created")
    rank = {k: i for i, k in enumerate(order)}
    best = None  # (granularity, -priority, parts)
    for key in order:
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            cand = (len(parts[0]), -rank[key], parts[0])
            if best is None or cand[:2] > best[:2]:
                best = cand
    if best is None:
        return ""
    p = best[2]
    y, m, d = p[0], (p[1] if len(p) > 1 else 1), (p[2] if len(p) > 2 else 1)
    return f"{y:04d}-{m:02d}-{d:02d}"


def crossref_date_for_doi(doi):
    """Look up one DOI in Crossref and return its best date, or '' on any failure."""
    if not doi or not doi.startswith("10."):
        return ""
    try:
        import requests
        r = requests.get("https://api.crossref.org/works/" + doi,
                         params=_ab_params(), headers=_ab_headers(), timeout=15)
        if r.status_code == 200:
            return pick_crossref_date(r.json().get("message", {}))
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Multi-source abstract lookup (shared by the daily job and the backfill).
# Order: Crossref -> Semantic Scholar -> OpenAlex (only if OPENALEX_KEY set).
# Crossref lacks abstracts for many Wiley/ACS papers; Semantic Scholar fills a
# lot of those gaps. Server-side only, so there are no CORS concerns.
# ---------------------------------------------------------------------------
def _ab_headers():
    mail = SETTINGS.get("crossref_mailto", "")
    ua = SETTINGS["user_agent"]
    if mail and "example.com" not in mail:
        ua += f" (mailto:{mail})"
    return {"User-Agent": ua}


def _ab_params():
    mail = SETTINGS.get("crossref_mailto", "")
    return {"mailto": mail} if mail and "example.com" not in mail else {}


def _cap_abstract(text):
    cap = SETTINGS.get("abstract_max_chars", 1600)
    if len(text) > cap:
        text = text[:cap].rsplit(" ", 1)[0] + "\u2026"
    return text


def _clean_abstract_candidate(text):
    text = clean_text(html.unescape(text or ""))
    if not text:
        return ""
    low = text.lower()
    bad = ["read the latest articles", "browse articles", "nature portfolio",
           "springer nature", "official journal", "science family of journals",
           "this journal publishes", "submit your article"]
    if any(b in low for b in bad):
        return ""
    if len(text) < 80:
        return ""
    return text


def _reconstruct_inverted_index(inv):
    if not inv:
        return ""
    pos = []
    for word, idxs in inv.items():
        for i in idxs:
            pos.append((i, word))
    pos.sort()
    return " ".join(w for _, w in pos)


def fetch_crossref_abstract(doi):
    try:
        import requests
        r = requests.get("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""),
                         params=_ab_params(), headers=_ab_headers(), timeout=30)
        if r.status_code == 200:
            return _clean_abstract_candidate(r.json().get("message", {}).get("abstract", ""))
    except Exception:
        pass
    return ""


def fetch_semanticscholar_abstract(doi):
    """Semantic Scholar by DOI. Only used when S2_API_KEY is set -- keyless calls
    are heavily throttled and slow, so without a key this source is skipped."""
    key = os.environ.get("S2_API_KEY", "")
    if not key:
        return ""
    try:
        import requests
        url = ("https://api.semanticscholar.org/graph/v1/paper/DOI:"
               + urllib.parse.quote(doi, safe="/") + "?fields=abstract")
        headers = {"x-api-key": key}
        delay = 5
        for _ in range(4):
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", delay) or delay))
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 200:
                return _clean_abstract_candidate(r.json().get("abstract", "") or "")
            return ""
    except Exception:
        pass
    return ""


def fetch_openalex_abstract(doi):
    key = os.environ.get("OPENALEX_KEY", "")
    if not key:
        return ""
    try:
        import requests
        params = {"select": "abstract_inverted_index", "api_key": key}
        mail = SETTINGS.get("crossref_mailto", "")
        if mail and "example.com" not in mail:
            params["mailto"] = mail
        r = requests.get("https://api.openalex.org/works/doi:" + doi,
                         params=params, headers=_ab_headers(), timeout=30)
        if r.status_code == 200:
            return _clean_abstract_candidate(
                _reconstruct_inverted_index(r.json().get("abstract_inverted_index")))
    except Exception:
        pass
    return ""


def fetch_abstract(doi):
    """First usable abstract across sources, or '' if none has one.

    Order is chosen for speed: Crossref (free) -> OpenAlex (keyed, fast) ->
    Semantic Scholar (only if S2_API_KEY is set). OpenAlex is the main filler
    for the Wiley/ACS papers Crossref lacks."""
    doi = (doi or "").strip()
    if not doi or not doi.startswith("10."):
        return ""
    for src in (fetch_crossref_abstract, fetch_openalex_abstract,
                fetch_semanticscholar_abstract):
        ab = src(doi)
        if ab:
            return _cap_abstract(ab)
    return ""


def _entry_date(entry):
    """ISO date from the RSS entry itself, or '' if the feed gave no usable date."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return time.strftime("%Y-%m-%d", value)
            except Exception:
                pass
    return ""


def _entry_doi(entry, link):
    """Extract a DOI if present, else fall back to the link as identifier."""
    for key in ("prism_doi", "dc_identifier", "id"):
        raw = entry.get(key, "")
        m = re.search(r"10\.\d{4,9}/\S+", str(raw))
        if m:
            return m.group(0).rstrip(").,;")
    m = re.search(r"10\.\d{4,9}/[^\s?#]+", link or "")
    if m:
        return m.group(0).rstrip(").,;")
    return (link or "").strip()


def _entry_abstract(entry):
    """Pull the longest available description / abstract field."""
    candidates = []
    if entry.get("summary"):
        candidates.append(entry["summary"])
    for c in entry.get("content", []) or []:
        if isinstance(c, dict) and c.get("value"):
            candidates.append(c["value"])
    if entry.get("description"):
        candidates.append(entry["description"])
    cleaned = [clean_text(c) for c in candidates]
    cleaned = [c for c in cleaned if c]
    if not cleaned:
        return ""
    text = max(cleaned, key=len)
    cap = SETTINGS.get("abstract_max_chars", 1600)
    if len(text) > cap:
        text = text[:cap].rsplit(" ", 1)[0] + "\u2026"
    return text


def _entry_authors(entry):
    names = []
    for a in entry.get("authors", []) or []:
        name = a.get("name") if isinstance(a, dict) else str(a)
        if name:
            names.append(clean_text(name))
    if not names and entry.get("author"):
        names = [clean_text(entry["author"])]
    return names


REVIEW_JOURNALS = {
    "Chemical Reviews", "Chemical Society Reviews", "Nature Reviews Materials",
    "Nature Reviews Chemistry", "Nature Reviews Methods Primers",
    "Accounts of Chemical Research",
}
_COMMENT_RE = re.compile(r"\b(comment on|reply to|matters arising|correspondence|rejoinder|editorial)\b", re.I)
_REVIEW_RE = re.compile(r"\b(review|perspective|roadmap|primer)\b", re.I)


def classify_type(title, journal, hint=""):
    """Bucket a paper as 'review', 'comment', or 'article'.

    review  <- review, perspective (and dedicated review journals)
    comment <- comment, correspondence, reply, matters arising, editorial
    article <- everything else (articles, letters, reports, ...)
    `hint` is any extra type string (RSS category / Crossref type) to consider.
    """
    text = (title or "") + " " + (hint or "")
    if _COMMENT_RE.search(text):
        return "comment"
    if journal in REVIEW_JOURNALS:
        return "review"
    if _REVIEW_RE.search(text):
        return "review"
    return "article"


def normalise_entry(entry, journal, publisher):
    """feedparser entry -> our flat record, or None if it should be skipped."""
    title = clean_text(entry.get("title", ""))
    link = (entry.get("link") or "").strip()
    if not title or not link:
        return None

    doi = _entry_doi(entry, link)
    abstract = _entry_abstract(entry)

    # If the feed gave no abstract (or only a short teaser), try the multi-source
    # lookup by DOI. Only the day's handful of papers hit this, so it's cheap.
    if len(abstract) < 200:
        better = fetch_abstract(doi)
        if len(better) > len(abstract):
            abstract = better

    keep, hits = is_relevant(title + " \n " + abstract)
    if not keep:
        return None

    # Prefer the feed's own date (the real online date for early-access papers).
    # If the feed gave none, ask Crossref before falling back to today, so a new
    # accepted/early-access paper still gets an accurate, stable date.
    date = _entry_date(entry) or crossref_date_for_doi(doi) or dt.date.today().isoformat()

    hint = " ".join(t.get("term", "") for t in (entry.get("tags") or []) if isinstance(t, dict))
    hint += " " + str(entry.get("dc_type", "") or "")

    return {
        "title": title,
        "link": link,
        "journal": journal,
        "publisher": publisher,
        "date": date,
        "abstract": abstract,
        "authors": _entry_authors(entry),
        "doi": doi,
        "keywords": hits,
        "type": classify_type(title, journal, hint),
    }


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_feed(url):
    """Return raw feed bytes for a URL, using a browser-like User-Agent."""
    import requests  # imported here so --selftest works without it

    headers = {
        "User-Agent": SETTINGS["user_agent"],
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    resp = requests.get(url, headers=headers, timeout=SETTINGS["request_timeout"])
    resp.raise_for_status()
    return resp.content


def harvest():
    """Fetch and parse all feeds. Returns (records, report)."""
    import feedparser

    records = []
    report = []
    for journal, publisher, url in FEEDS:
        status = {"journal": journal, "url": url, "found": 0, "kept": 0, "error": None}
        try:
            raw = fetch_feed(url)
            parsed = feedparser.parse(raw)
            status["found"] = len(parsed.entries)
            for entry in parsed.entries:
                rec = normalise_entry(entry, journal, publisher)
                if rec:
                    records.append(rec)
                    status["kept"] += 1
        except Exception as exc:  # noqa: BLE001 - never let one feed kill the run
            status["error"] = f"{type(exc).__name__}: {exc}"
        report.append(status)
        print(
            f"  {journal:<42} found {status['found']:>3}  kept {status['kept']:>3}"
            + (f"  !! {status['error']}" if status["error"] else "")
        )
        time.sleep(1)  # be polite between publishers
    return records, report


# ---------------------------------------------------------------------------
# Archive merge / prune
#
# Data is stored as one file per year (data/papers-YYYY.json) plus a small
# data/manifest.json. Splitting by year keeps each file small, keeps the git
# history clean (past years never change again), and lets the page load fast.
# ---------------------------------------------------------------------------
def _year_files(data_dir=DATA_DIR):
    if not os.path.isdir(data_dir):
        return []
    return [os.path.join(data_dir, f) for f in os.listdir(data_dir)
            if re.fullmatch(r"papers-\d{4}\.json", f)]


def load_archive(data_dir=DATA_DIR):
    """Load and concatenate papers from every per-year file."""
    papers = []
    for path in _year_files(data_dir):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                papers.extend(json.load(fh).get("papers", []))
        except Exception:
            pass
    return papers


def _key(rec):
    return (rec.get("doi") or rec.get("link") or rec.get("title", "")).lower()


def _better_record(old, new):
    """Merge two records for the same DOI/link without losing useful metadata.

    Fresh RSS records can have better dates or links, but they can also have
    empty abstracts. Keep the longest available abstract and avoid replacing
    richer stored metadata with sparse fresh metadata.
    """
    if not old:
        return new
    if not new:
        return old

    merged = dict(old)
    merged.update(new)

    old_abs = old.get("abstract") or ""
    new_abs = new.get("abstract") or ""

    # Preserve the richer abstract. This prevents a fresh RSS item with an empty
    # abstract from overwriting an older archive record that already had one.
    if len(old_abs) > len(new_abs):
        merged["abstract"] = old_abs
    else:
        merged["abstract"] = new_abs

    # Preserve richer author and keyword lists when the new record is sparse.
    if len(old.get("authors") or []) > len(new.get("authors") or []):
        merged["authors"] = old.get("authors") or []

    if len(old.get("keywords") or []) > len(new.get("keywords") or []):
        merged["keywords"] = old.get("keywords") or []

    # Prefer a real DOI over a non-DOI fallback identifier.
    old_doi = old.get("doi") or ""
    new_doi = new.get("doi") or ""
    if old_doi.startswith("10.") and not new_doi.startswith("10."):
        merged["doi"] = old_doi

    # Keep the "already tried, found nothing" counter so a re-harvested paper
    # isn't re-checked from scratch. If the kept abstract is now long, drop it.
    tried = old.get("ab_tried", new.get("ab_tried"))
    if tried is not None and len(merged.get("abstract") or "") < 200:
        merged["ab_tried"] = tried
    else:
        merged.pop("ab_tried", None)

    return merged


def merge(existing, fresh, start_date):
    """Merge fresh into existing, de-dupe, drop pre-start_date, sort newest first."""
    by_key = {}
    for rec in existing + fresh:
        by_key[_key(rec)] = _better_record(by_key.get(_key(rec)), rec)

    merged = [r for r in by_key.values() if r.get("date", "") >= start_date]
    merged.sort(key=lambda r: (r.get("date", ""), r.get("journal", "")), reverse=True)
    return merged


def write_archive(papers, report, data_dir=DATA_DIR):
    """Write one file per year + a manifest. Returns the manifest dict."""
    os.makedirs(data_dir, exist_ok=True)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # bucket by calendar year
    by_year = {}
    for p in papers:
        year = (p.get("date") or "")[:4] or "unknown"
        by_year.setdefault(year, []).append(p)

    for year, items in by_year.items():
        with open(os.path.join(data_dir, f"papers-{year}.json"), "w", encoding="utf-8") as fh:
            json.dump({"year": year, "count": len(items), "generated": generated,
                       "papers": items}, fh, ensure_ascii=False, indent=1)

    manifest = {
        "generated": generated,
        "count": len(papers),
        "years": sorted(by_year.keys(), reverse=True),
        "year_counts": {y: len(v) for y, v in sorted(by_year.items(), reverse=True)},
        "feeds": [
            {"journal": r["journal"], "found": r["found"],
             "kept": r["kept"], "error": r["error"]}
            for r in report
        ],
    }
    with open(os.path.join(data_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    return manifest


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def topup_abstracts(papers, limit, min_len=200, max_tries=2):
    """Fill a bounded number of still-missing abstracts in `papers` (in place).

    Tries the emptiest first. Records an attempt counter ('ab_tried') on each
    paper so dead ends (no abstract in any source) are skipped after `max_tries`
    rather than being re-checked forever. Returns the number filled."""
    targets = [p for p in papers
               if p.get("doi", "").startswith("10.")
               and len(p.get("abstract") or "") < min_len
               and int(p.get("ab_tried", 0)) < max_tries]
    targets.sort(key=lambda p: len(p.get("abstract") or ""))
    filled = 0
    for p in targets[:limit]:
        ab = fetch_abstract(p["doi"])
        if len(ab) > len(p.get("abstract") or ""):
            p["abstract"] = _cap_abstract(ab)
            p.pop("ab_tried", None)            # success -> clear the counter
            filled += 1
        else:
            p["ab_tried"] = int(p.get("ab_tried", 0)) + 1
        time.sleep(0.2)
    return filled


def run():
    print(f"Harvesting {len(FEEDS)} feeds ...")
    fresh, report = harvest()
    existing = load_archive()
    start = SETTINGS["start_date"]
    papers = merge(existing, fresh, start)

    # Daily self-healing: top up a small batch of still-missing abstracts so the
    # backlog shrinks on its own over time, without a manual repair run.
    daily_cap = SETTINGS.get("daily_abstract_topup", 300)
    filled = topup_abstracts(papers, limit=daily_cap) if daily_cap else 0

    manifest = write_archive(papers, report)

    new_count = len({_key(r) for r in fresh} - {_key(r) for r in existing})
    ok = sum(1 for r in report if r["error"] is None)
    missing = sum(1 for p in papers if len(p.get("abstract") or "") < 200)
    print("-" * 60)
    print(f"Feeds OK: {ok}/{len(report)} | new this run: {new_count} "
          f"| archive total: {manifest['count']} (since {start})")
    print(f"Abstracts topped up this run: {filled} | still missing/short: {missing}")
    print("By year: " + ", ".join(f"{y}:{c}" for y, c in manifest["year_counts"].items()))
    print(f"Wrote {DATA_DIR}/papers-YYYY.json + manifest.json")


def selftest():
    """Offline test of matching + merge logic (no feedparser/requests needed)."""
    print("Running self-test (offline)...")

    # 1. keyword matcher
    keep, hits = is_relevant("Halide segregation in wide-bandgap perovskite solar cells")
    assert keep and "perovskite" in [h.lower() for h in hits], hits
    keep, _ = is_relevant("A study of champion swimmers and onion farming")
    assert not keep
    keep, _ = is_relevant("Photocatalytic CO2 reduction over a new catalyst")
    assert keep

    # 2. require_perovskite gate
    saved = SETTINGS["require_perovskite"]
    SETTINGS["require_perovskite"] = True
    keep, _ = is_relevant("Silicon tandem photovoltaics reach new efficiency")
    assert not keep, "non-perovskite tandem should be filtered when gate is on"
    keep, _ = is_relevant("Perovskite-silicon tandem photovoltaics")
    assert keep
    SETTINGS["require_perovskite"] = saved

    # 3. merge / prune / dedupe, while preserving useful old abstracts
    today = dt.date.today().isoformat()
    old = "2025-12-31"

    existing = [
        {
            "title": "A",
            "doi": "10.1/a",
            "link": "x",
            "date": old,
            "journal": "J",
            "abstract": "old pruned abstract",
        },
        {
            "title": "B",
            "doi": "10.1/b",
            "link": "y",
            "date": today,
            "journal": "J",
            "abstract": "this useful old abstract should survive",
        },
    ]

    fresh = [
        {
            "title": "B-updated",
            "doi": "10.1/b",
            "link": "y",
            "date": today,
            "journal": "J",
            "abstract": "",
        },
        {
            "title": "C",
            "doi": "10.1/c",
            "link": "z",
            "date": today,
            "journal": "J",
            "abstract": "new abstract",
        },
    ]

    merged = merge(existing, fresh, start_date="2026-01-01")
    titles = sorted(r["title"] for r in merged)
    assert titles == ["B-updated", "C"], titles

    b = next(r for r in merged if r["doi"] == "10.1/b")
    assert b["abstract"] == "this useful old abstract should survive", b

    # 4. abstract HTML cleaning
    assert clean_text("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"

    print("All self-tests passed.")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the group literature feed.")
    ap.add_argument("--selftest", action="store_true", help="run offline logic tests")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    else:
        run()
