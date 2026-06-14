#!/usr/bin/env python3
"""
backfill.py -- one-time historical fill of the archive from Crossref.

Why this exists
---------------
RSS feeds only carry recent papers, so the daily job (aggregate.py) can't reach
back in time. Crossref indexes the full back-catalogue of every journal, so this
script pulls everything from SETTINGS["start_date"] to today, filters it with the
SAME keyword rules as the daily job, and merges the hits into the same per-year
files (data/papers-YYYY.json). Run it once; the daily job takes over from there.

Usage
-----
    python backfill.py --verify-issns     # check every ISSN resolves correctly
    python backfill.py --dry-run          # fetch + filter, report counts, write nothing
    python backfill.py                     # full backfill, writes data files

It is safe to re-run: results merge and de-duplicate against what's already there.
"""

import argparse
import sys
import time

import requests

from feeds import ISSNS, CORE_QUERIES, SETTINGS
# reuse the daily job's helpers so filtering/merging behave identically
from aggregate import clean_text, is_relevant, merge, load_archive, write_archive, _key

CROSSREF = "https://api.crossref.org/works"
JOURNALS = "https://api.crossref.org/journals/{}"

# reverse map: every ISSN -> (journal name, publisher)
_PUB_OF = {}  # name -> publisher, filled from feeds.FEEDS
from feeds import FEEDS as _FEEDS
for _n, _p, _u in _FEEDS:
    _PUB_OF[_n] = _p
ISSN_TO_JOURNAL = {}
for _name, _issns in ISSNS.items():
    for _i in _issns:
        ISSN_TO_JOURNAL[_i] = (_name, _PUB_OF.get(_name, ""))

ALL_ISSNS = sorted({i for v in ISSNS.values() for i in v})


def _headers():
    mail = SETTINGS.get("crossref_mailto", "")
    ua = SETTINGS["user_agent"]
    if mail and "example.com" not in mail:
        ua += f" (mailto:{mail})"
    return {"User-Agent": ua}


def _mailto_param():
    mail = SETTINGS.get("crossref_mailto", "")
    return {"mailto": mail} if mail and "example.com" not in mail else {}


# ---------------------------------------------------------------------------
# Crossref record -> our flat schema
# ---------------------------------------------------------------------------
def _date(item):
    """Best ISO date for a Crossref item.

    Advance/accepted articles often report 'issued'/'published' as a bare YEAR,
    which previously padded to YYYY-01-01. We instead pick the most *granular*
    date available (full Y-M-D beats Y-M beats Y), preferring the online date,
    and fall back to Crossref's 'created' timestamp (always a full date, set at
    DOI registration ~ online publication) so we never invent January 1st.
    """
    order = ("published-online", "published", "issued", "published-print", "created")
    rank = {k: i for i, k in enumerate(order)}
    best = None  # (granularity, -priority, [y,m,d])
    for key in order:
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            cand = (len(parts[0]), -rank[key], parts[0])
            if best is None or cand[:2] > best[:2]:
                best = cand
    if best is None:
        return ""
    p = best[2]
    y = p[0]
    m = p[1] if len(p) > 1 else 1
    d = p[2] if len(p) > 2 else 1
    return f"{y:04d}-{m:02d}-{d:02d}"


def _journal_and_pub(item):
    for issn in item.get("ISSN", []) or []:
        if issn in ISSN_TO_JOURNAL:
            return ISSN_TO_JOURNAL[issn]
    ct = item.get("container-title") or []
    return (ct[0] if ct else "Unknown", "")


def normalise(item):
    titles = item.get("title") or []
    title = clean_text(titles[0]) if titles else ""
    doi = (item.get("DOI") or "").strip()
    if not title or not doi:
        return None

    abstract = clean_text(item.get("abstract", ""))
    cap = SETTINGS.get("abstract_max_chars", 1600)
    if len(abstract) > cap:
        abstract = abstract[:cap].rsplit(" ", 1)[0] + "\u2026"

    keep, hits = is_relevant(title + " \n " + abstract)
    if not keep:
        return None

    journal, publisher = _journal_and_pub(item)
    authors = []
    for a in item.get("author", []) or []:
        name = " ".join(p for p in (a.get("given"), a.get("family")) if p)
        if name:
            authors.append(name)

    return {
        "title": title,
        "link": item.get("URL") or f"https://doi.org/{doi}",
        "journal": journal,
        "publisher": publisher,
        "date": _date(item),
        "abstract": abstract,
        "authors": authors,
        "doi": doi,
        "keywords": hits,
    }


# ---------------------------------------------------------------------------
# Crossref querying (cursor pagination)
# ---------------------------------------------------------------------------
SELECT = "DOI,title,author,issued,published,published-online,published-print,created,container-title,ISSN,URL,abstract"


def query_term(term, start_date, dry_run=False):
    """Page through all Crossref results for one seed term across all ISSNs."""
    issn_filter = ",".join(f"issn:{i}" for i in ALL_ISSNS)
    params = {
        "query.bibliographic": term,
        "filter": f"from-pub-date:{start_date},{issn_filter}",
        "rows": 1000,
        "cursor": "*",
        "select": SELECT,
    }
    params.update(_mailto_param())

    kept, scanned, pages = [], 0, 0
    while True:
        try:
            r = requests.get(CROSSREF, params=params, headers=_headers(), timeout=60)
            r.raise_for_status()
            msg = r.json().get("message", {})
        except Exception as exc:  # noqa: BLE001
            print(f"    !! '{term}' page {pages}: {type(exc).__name__}: {exc}")
            break

        items = msg.get("items", [])
        if not items:
            break
        scanned += len(items)
        for it in items:
            rec = normalise(it)
            if rec:
                kept.append(rec)

        pages += 1
        cursor = msg.get("next-cursor")
        if not cursor or len(items) < params["rows"]:
            break
        params["cursor"] = cursor
        time.sleep(1)  # polite

    print(f"  query '{term:<20}' scanned {scanned:>6}  kept {len(kept):>5}  ({pages} pages)")
    return kept


def verify_issns():
    """Print what journal each configured ISSN resolves to in Crossref."""
    print("Checking ISSNs against Crossref ...\n")
    ok = True
    for name, issns in ISSNS.items():
        resolved = None
        for issn in issns:
            try:
                r = requests.get(JOURNALS.format(issn), headers=_headers(),
                                 params=_mailto_param(), timeout=30)
                if r.status_code == 200:
                    resolved = r.json().get("message", {}).get("title")
                    break
            except Exception:
                pass
            time.sleep(0.5)
        mark = "ok " if resolved else "??  "
        if not resolved:
            ok = False
        print(f"  [{mark}] {name:<42} -> {resolved or 'NOT FOUND (check ISSN)'}")
    print("\nAll ISSNs resolved." if ok else "\nSome ISSNs did not resolve - edit ISSNS in feeds.py.")


def run(dry_run=False):
    start = SETTINGS["start_date"]
    print(f"Backfilling {len(ISSNS)} journals from {start} via Crossref")
    print(f"Seed queries: {', '.join(CORE_QUERIES)}\n")

    by_key = {}
    for term in CORE_QUERIES:
        for rec in query_term(term, start, dry_run):
            by_key[_key(rec)] = rec  # de-dupe across seed queries
    fresh = list(by_key.values())
    print(f"\nUnique matching papers from backfill: {len(fresh)}")

    if dry_run:
        years = {}
        for r in fresh:
            years[r["date"][:4]] = years.get(r["date"][:4], 0) + 1
        print("By year: " + ", ".join(f"{y}:{c}" for y, c in sorted(years.items())))
        print("(dry run - nothing written)")
        return

    existing = load_archive()
    merged = merge(existing, fresh, start)
    # backfill has no live feed report; pass an empty status list
    manifest = write_archive(merged, report=[])
    print(f"\nArchive now holds {manifest['count']} papers.")
    print("By year: " + ", ".join(f"{y}:{c}" for y, c in manifest["year_counts"].items()))
    print("Done. The daily job will keep it current from here.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Historical backfill from Crossref.")
    ap.add_argument("--verify-issns", action="store_true", help="check ISSNs resolve, then exit")
    ap.add_argument("--dry-run", action="store_true", help="fetch + filter but write nothing")
    args = ap.parse_args()
    if args.verify_issns:
        verify_issns()
    else:
        run(dry_run=args.dry_run)
