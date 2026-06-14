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
        mail = SETTINGS.get("crossref_mailto", "")
        params = {"mailto": mail} if mail and "example.com" not in mail else {}
        r = requests.get("https://api.crossref.org/works/" + doi,
                         params=params, headers={"User-Agent": SETTINGS["user_agent"]},
                         timeout=15)
        if r.status_code == 200:
            return pick_crossref_date(r.json().get("message", {}))
    except Exception:
        pass
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

    abstract = _entry_abstract(entry)
    keep, hits = is_relevant(title + " \n " + abstract)
    if not keep:
        return None

    doi = _entry_doi(entry, link)
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


def merge(existing, fresh, start_date):
    """Merge fresh into existing, de-dupe, drop pre-start_date, sort newest first."""
    by_key = {}
    for rec in existing + fresh:          # fresh wins on conflict (better metadata)
        by_key[_key(rec)] = rec
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
def run():
    print(f"Harvesting {len(FEEDS)} feeds ...")
    fresh, report = harvest()
    existing = load_archive()
    start = SETTINGS["start_date"]
    papers = merge(existing, fresh, start)
    manifest = write_archive(papers, report)

    new_count = len({_key(r) for r in fresh} - {_key(r) for r in existing})
    ok = sum(1 for r in report if r["error"] is None)
    print("-" * 60)
    print(f"Feeds OK: {ok}/{len(report)} | new this run: {new_count} "
          f"| archive total: {manifest['count']} (since {start})")
    print("By year: " + ", ".join(f"{y}:{c}" for y, c in manifest["year_counts"].items()))
    print(f"Wrote {DATA_DIR}/papers-YYYY.json + manifest.json")


def selftest():
    """Offline test of matching + merge logic (no feedparser/requests needed)."""
    print("Running self-test (offline)...")

    # 1. keyword matcher
    keep, hits = is_relevant("Halide segregation in wide-bandgap perovskite solar cells")
    assert keep and "perovskite" in [h.lower() for h in hits], hits
    keep, _ = is_relevant("A study of champion swimmers and onion farming")  # no false hit on 'ion'/'champion'
    assert not keep
    keep, _ = is_relevant("Photocatalytic CO2 reduction over a new catalyst")
    assert keep  # matches 'co2 reduction'

    # 2. require_perovskite gate
    saved = SETTINGS["require_perovskite"]
    SETTINGS["require_perovskite"] = True
    keep, _ = is_relevant("Silicon tandem photovoltaics reach new efficiency")
    assert not keep, "non-perovskite tandem should be filtered when gate is on"
    keep, _ = is_relevant("Perovskite-silicon tandem photovoltaics")
    assert keep
    SETTINGS["require_perovskite"] = saved

    # 3. merge / prune / dedupe
    today = dt.date.today().isoformat()
    old = "2025-12-31"   # before the 2026-01-01 start date -> should be pruned
    existing = [
        {"title": "A", "doi": "10.1/a", "link": "x", "date": old, "journal": "J"},
        {"title": "B", "doi": "10.1/b", "link": "y", "date": today, "journal": "J"},
    ]
    fresh = [
        {"title": "B-updated", "doi": "10.1/b", "link": "y", "date": today, "journal": "J"},
        {"title": "C", "doi": "10.1/c", "link": "z", "date": today, "journal": "J"},
    ]
    merged = merge(existing, fresh, start_date="2026-01-01")
    titles = sorted(r["title"] for r in merged)
    assert titles == ["B-updated", "C"], titles  # A pruned, B replaced, C added

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
