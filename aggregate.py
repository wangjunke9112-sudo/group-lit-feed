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

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "papers.json")


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
def _entry_date(entry):
    """Best-effort ISO date (YYYY-MM-DD) for an entry."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return time.strftime("%Y-%m-%d", value)
            except Exception:
                pass
    return dt.date.today().isoformat()


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

    return {
        "title": title,
        "link": link,
        "journal": journal,
        "publisher": publisher,
        "date": _entry_date(entry),
        "abstract": abstract,
        "authors": _entry_authors(entry),
        "doi": _entry_doi(entry, link),
        "keywords": hits,
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
# ---------------------------------------------------------------------------
def load_archive(path=DATA_PATH):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("papers", [])
    except Exception:
        return []


def _key(rec):
    return (rec.get("doi") or rec.get("link") or rec.get("title", "")).lower()


def merge(existing, fresh, days_to_keep):
    """Merge fresh records into existing, de-dupe, prune old, sort newest first."""
    by_key = {}
    for rec in existing + fresh:          # fresh wins on conflict (better metadata)
        by_key[_key(rec)] = rec

    cutoff = (dt.date.today() - dt.timedelta(days=days_to_keep)).isoformat()
    merged = [r for r in by_key.values() if r.get("date", "") >= cutoff]
    merged.sort(key=lambda r: (r.get("date", ""), r.get("journal", "")), reverse=True)
    return merged


def write_archive(papers, report, path=DATA_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "count": len(papers),
        "feeds": [
            {"journal": r["journal"], "found": r["found"],
             "kept": r["kept"], "error": r["error"]}
            for r in report
        ],
        "papers": papers,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return payload


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def run():
    print(f"Harvesting {len(FEEDS)} feeds ...")
    fresh, report = harvest()
    existing = load_archive()
    papers = merge(existing, fresh, SETTINGS["days_to_keep"])
    payload = write_archive(papers, report)

    new_count = len({_key(r) for r in fresh} - {_key(r) for r in existing})
    ok = sum(1 for r in report if r["error"] is None)
    print("-" * 60)
    print(f"Feeds OK: {ok}/{len(report)} | new this run: {new_count} "
          f"| archive total: {payload['count']}")
    print(f"Wrote {DATA_PATH}")


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
    old = (dt.date.today() - dt.timedelta(days=999)).isoformat()
    existing = [
        {"title": "A", "doi": "10.1/a", "link": "x", "date": old, "journal": "J"},
        {"title": "B", "doi": "10.1/b", "link": "y", "date": today, "journal": "J"},
    ]
    fresh = [
        {"title": "B-updated", "doi": "10.1/b", "link": "y", "date": today, "journal": "J"},
        {"title": "C", "doi": "10.1/c", "link": "z", "date": today, "journal": "J"},
    ]
    merged = merge(existing, fresh, days_to_keep=60)
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
