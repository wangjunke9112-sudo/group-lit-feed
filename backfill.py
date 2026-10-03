#!/usr/bin/env python3
"""
backfill.py -- one-time historical fill of the archive from Crossref.
"""

import argparse
import datetime as dt
import html
import re
import sys
import time
import os
import urllib.parse

import requests

from feeds import ISSNS, CORE_QUERIES, SETTINGS
from aggregate import (clean_text, is_relevant, merge, load_archive,
                       write_archive, _key, pick_crossref_date, classify_type,
                       fetch_abstract, _better_record)

CROSSREF = "https://api.crossref.org/works"
JOURNALS = "https://api.crossref.org/journals/{}"

_PUB_OF = {}
from feeds import FEEDS as _FEEDS
for _n, _p, _u in _FEEDS:
    _PUB_OF[_n] = _p
ISSN_TO_JOURNAL = {}
for _name, _issns in ISSNS.items():
    for _i in _issns:
        ISSN_TO_JOURNAL[_i] = (_name, _PUB_OF.get(_name, ""))


def _headers():
    mail = SETTINGS.get("crossref_mailto", "")
    ua = SETTINGS["user_agent"]
    if mail and "example.com" not in mail:
        ua += f" (mailto:{mail})"
    return {"User-Agent": ua}


def _mailto_param():
    mail = SETTINGS.get("crossref_mailto", "")
    return {"mailto": mail} if mail and "example.com" not in mail else {}


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
    sub = item.get("subtype")
    hint = (item.get("type", "") or "") + " " + (sub if isinstance(sub, str) else " ".join(sub or []))
    return {
        "title": title,
        "link": item.get("URL") or f"https://doi.org/{doi}",
        "journal": journal,
        "publisher": publisher,
        "date": pick_crossref_date(item),
        "abstract": abstract,
        "authors": authors,
        "doi": doi,
        "keywords": hits,
        "type": classify_type(title, journal, hint),
    }


SELECT = "DOI,title,author,issued,published,published-online,published-print,created,container-title,ISSN,URL,abstract"


def _get_with_retry(params, label, tries=5):
    delay = 5
    for attempt in range(tries):
        try:
            r = requests.get(CROSSREF, params=params, headers=_headers(), timeout=60)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", delay) or delay)
                print(f"      .. 429 on {label}; waiting {wait}s (try {attempt+1}/{tries})")
                time.sleep(wait); delay = min(delay * 2, 60); continue
            r.raise_for_status()
            return r
        except requests.HTTPError:
            raise
        except Exception as exc:
            print(f"      .. {type(exc).__name__} on {label}; retry in {delay}s")
            time.sleep(delay); delay = min(delay * 2, 60)
    return None


def _enumerate_issn(issn, start_date, on_gap, progress=None):
    kept, seen_dois = [], set()
    scanned = 0
    for dfilter in ("from-created-date", "from-pub-date"):
        params = {"filter": f"{dfilter}:{start_date},issn:{issn}",
                  "rows": 1000, "cursor": "*", "select": SELECT}
        params.update(_mailto_param())
        while True:
            try:
                r = _get_with_retry(params, f"{issn}/{dfilter}")
            except Exception as exc:
                on_gap(issn, dfilter, f"{type(exc).__name__}: {exc}")
                break
            if r is None:
                on_gap(issn, dfilter, "request failed after retries")
                break
            try:
                msg = r.json().get("message", {})
            except Exception as exc:
                on_gap(issn, dfilter, f"bad JSON: {type(exc).__name__}")
                break
            items = msg.get("items", [])
            if not items:
                break
            scanned += len(items)
            for it in items:
                doi = (it.get("DOI") or "").strip().lower()
                if doi and doi in seen_dois:
                    continue
                if doi:
                    seen_dois.add(doi)
                rec = normalise(it)
                if rec:
                    kept.append(rec)
            if progress:
                progress(scanned, len(kept))
            cursor = msg.get("next-cursor")
            if not cursor or len(items) < params["rows"]:
                break
            params["cursor"] = cursor
            time.sleep(1)
        time.sleep(0.3)
    return kept, scanned


def backfill_all(start_date):
    by_key, per_journal = {}, {}
    gaps = []

    def on_gap(issn, dfilter, why):
        gaps.append((issn, dfilter, why))
        print(f"      !! GAP {issn} [{dfilter}]: {why}")

    for name, issns in ISSNS.items():
        before = len(by_key)
        scanned_total = 0
        for issn in issns:
            recs, scanned = _enumerate_issn(issn, start_date, on_gap)
            scanned_total += scanned
            for rec in recs:
                key = _key(rec)
                if key in by_key:
                    by_key[key] = _better_record(by_key[key], rec)
                else:
                    by_key[key] = rec
        per_journal[name] = len(by_key) - before
        print(f"  {name:<42} scanned {scanned_total:>6}  +{per_journal[name]:>5} relevant"
              f"  (running total {len(by_key)})")

    if gaps:
        print(f"\n!! {len(gaps)} incomplete request stream(s) -- coverage may have holes:")
        for issn, dfilter, why in gaps:
            print(f"   {issn} [{dfilter}]: {why}")
        print("   Re-run to fill these; results merge and de-duplicate.")
    return list(by_key.values()), per_journal


def verify_issns():
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


def _fetch_abstract(doi):
    return fetch_abstract((doi or "").strip())


def _days_old(p):
    """Age of a paper in days, or a huge number if its date is unparseable."""
    try:
        return (dt.date.today() - dt.date.fromisoformat((p.get("date") or "")[:10])).days
    except Exception:
        return 10 ** 6


def repair_abstracts(limit=0, dry_run=False, repair_all=False, min_len=200,
                     max_tries=2, count_only=False, recent_grace_days=120):
    """Fill in missing/short abstracts from multiple sources (full chain:
    Crossref -> OpenAlex -> Semantic Scholar if keyed -> publisher page).

    A paper is eligible for a (re)try if its abstract is shorter than min_len AND
    either it has not yet used its `max_tries` attempts, OR it was published within
    the last `recent_grace_days` days. The recency grace matters because a
    just-published paper often has no abstract in any API for a week or two: it
    burns its attempts immediately, then the abstract appears later and the normal
    rule would skip it forever. Older papers keep the hard limit so genuinely empty
    back-catalogue items are not re-fetched endlessly. Repairs newest-first, so the
    recent papers you actually browse are filled first. Checkpoints every 100;
    resumable."""
    papers = load_archive()
    cap = SETTINGS.get("abstract_max_chars", 1600)
    short = [p for p in papers if len(p.get("abstract") or "") < min_len]

    def eligible(p):
        if not p.get("doi"):
            return False
        if int(p.get("ab_tried", 0)) < max_tries:
            return True
        return _days_old(p) <= recent_grace_days      # exhausted but recent -> retry

    untried = [p for p in short if eligible(p)]
    recent_exhausted = sum(1 for p in short if p.get("doi")
                           and int(p.get("ab_tried", 0)) >= max_tries
                           and _days_old(p) <= recent_grace_days)
    no_doi = sum(1 for p in short if not p.get("doi"))
    print(f"Total papers: {len(papers)} | missing/short: {len(short)} "
          f"| to try: {len(untried)} (incl. {recent_exhausted} recent retries) "
          f"| no DOI: {no_doi}")
    if count_only:
        return

    targets = short if repair_all else untried
    targets = sorted(targets, key=lambda p: p.get("date", ""), reverse=True)  # newest first
    if limit:
        targets = targets[:limit]
    print(f"Repairing {len(targets)} this run ({'dry run' if dry_run else 'will write'})\n")

    fixed = checked = 0
    for p in targets:
        checked += 1
        ab = _fetch_abstract(p["doi"])
        if len(ab) > len(p.get("abstract") or ""):
            if len(ab) > cap:
                ab = ab[:cap].rsplit(" ", 1)[0] + "\u2026"
            p["abstract"] = ab
            p.pop("ab_tried", None)
            fixed += 1
        else:
            p["ab_tried"] = int(p.get("ab_tried", 0)) + 1
        if checked % 100 == 0:
            print(f"  {checked}/{len(targets)} checked, {fixed} filled")
            if not dry_run:
                write_archive(papers, report=None)
        time.sleep(0.25)

    print(f"\nDone: {fixed} abstracts filled out of {checked} checked.")
    if dry_run:
        print("(dry run - nothing written)")
    else:
        manifest = write_archive(papers, report=None)
        print(f"Archive holds {manifest['count']} papers.")


def run(dry_run=False):
    start = SETTINGS["start_date"]
    print(f"Backfilling {len(ISSNS)} journals from {start} via Crossref (per-journal)")
    print("Mode: full enumeration by ISSN (no seed queries); relevance decided locally.")
    print("Date filters: from-created-date UNION from-pub-date.\n")

    fresh, per_journal = backfill_all(start)
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
    manifest = write_archive(merged, report=None)
    print(f"\nArchive now holds {manifest['count']} papers.")
    print("By year: " + ", ".join(f"{y}:{c}" for y, c in manifest["year_counts"].items()))
    print("Done. The daily job will keep it current from here.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Historical backfill from Crossref.")
    ap.add_argument("--verify-issns", action="store_true", help="check ISSNs resolve, then exit")
    ap.add_argument("--dry-run", action="store_true", help="fetch + filter but write nothing")
    ap.add_argument("--repair-abstracts", action="store_true",
                    help="fill missing/short abstracts from multiple sources (resumable)")
    ap.add_argument("--repair-limit", type=int, default=0,
                    help="cap how many papers to repair this run (newest first; resumable)")
    ap.add_argument("--repair-all", action="store_true",
                    help="re-check every short paper, ignoring the retry counter (slow)")
    ap.add_argument("--repair-min-len", type=int, default=200,
                    help="treat abstracts shorter than this many chars as needing repair")
    ap.add_argument("--repair-recent-days", type=int, default=120,
                    help="always retry papers newer than this many days, even if their "
                         "retry attempts are used up (default 120)")
    ap.add_argument("--count-only", action="store_true",
                    help="just report how many abstracts are still missing, then exit")
    args = ap.parse_args()
    if args.verify_issns:
        verify_issns()
    elif args.repair_abstracts:
        repair_abstracts(limit=args.repair_limit, dry_run=args.dry_run,
                         repair_all=args.repair_all, min_len=args.repair_min_len,
                         count_only=args.count_only, recent_grace_days=args.repair_recent_days)
    else:
        run(dry_run=args.dry_run)
