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
import html
import re
import sys
import time
import os
import urllib.parse

import requests

from feeds import ISSNS, CORE_QUERIES, SETTINGS
# reuse the daily job's helpers so filtering/merging behave identically
from aggregate import (clean_text, is_relevant, merge, load_archive,
                       write_archive, _key, pick_crossref_date, classify_type)

CROSSREF = "https://api.crossref.org/works"
WORK = "https://api.crossref.org/works/{}"   # single-DOI lookup (no `select`, full record)
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
    """Most precise ISO date for a Crossref item (shared logic, see aggregate.py)."""
    return pick_crossref_date(item)


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
        "date": _date(item),
        "abstract": abstract,
        "authors": authors,
        "doi": doi,
        "keywords": hits,
        "type": classify_type(title, journal, hint),
    }


# ---------------------------------------------------------------------------
# Crossref querying (cursor pagination)
# ---------------------------------------------------------------------------
# Crossref `select` only accepts specific field names; `type`/`subtype` are NOT
# selectable and 400 the whole request. Keep to the known-good list. (Paper type
# is still derived from title + journal in classify_type.)
SELECT = "DOI,title,author,issued,published,published-online,published-print,created,container-title,ISSN,URL,abstract"


def _get_with_retry(params, label, tries=5):
    """GET Crossref, backing off on 429 (Too Many Requests) and transient errors."""
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
        except Exception as exc:  # transient network error -> brief backoff
            print(f"      .. {type(exc).__name__} on {label}; retry in {delay}s")
            time.sleep(delay); delay = min(delay * 2, 60)
    return None


def _query_one(term, issn, start_date):
    """Page through Crossref for one seed term within ONE ISSN. Small request."""
    params = {
        "query.bibliographic": term,
        "filter": f"from-pub-date:{start_date},issn:{issn}",
        "rows": 1000,
        "cursor": "*",
        "select": SELECT,
    }
    params.update(_mailto_param())
    kept = []
    while True:
        try:
            r = _get_with_retry(params, f"'{term}'/{issn}")
            if r is None:
                break
            msg = r.json().get("message", {})
        except Exception as exc:  # noqa: BLE001
            print(f"      !! '{term}' / {issn}: {type(exc).__name__}: {exc}")
            break
        items = msg.get("items", [])
        if not items:
            break
        for it in items:
            rec = normalise(it)
            if rec:
                kept.append(rec)
        cursor = msg.get("next-cursor")
        if not cursor or len(items) < params["rows"]:
            break
        params["cursor"] = cursor
        time.sleep(1)  # polite
    time.sleep(0.3)    # small gap between queries to ease rate limits
    return kept


def backfill_all(start_date):
    """Per-journal querying: each request carries a single ISSN, so the Crossref
    filter stays short (the all-ISSNs-in-one-filter approach hit HTTP 400)."""
    by_key, per_journal = {}, {}
    for name, issns in ISSNS.items():
        before = len(by_key)
        for issn in issns:
            for term in CORE_QUERIES:
                for rec in _query_one(term, issn, start_date):
                    by_key[_key(rec)] = rec        # de-dupe across terms/ISSNs/journals
        per_journal[name] = len(by_key) - before
        print(f"  {name:<42} +{per_journal[name]:>5} new  (running total {len(by_key)})")
    return list(by_key.values()), per_journal


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


def _clean_abstract_candidate(text):
    """Clean and reject obvious non-abstract snippets."""
    text = clean_text(html.unescape(text or ""))
    if not text:
        return ""

    # Reject very generic landing-page descriptions.
    low = text.lower()
    bad_bits = [
        "read the latest articles",
        "browse articles",
        "nature portfolio",
        "springer nature",
        "official journal",
        "science family of journals",
        "this journal publishes",
        "learn about",
        "submit your article",
    ]
    if any(b in low for b in bad_bits):
        return ""

    # Very short snippets are usually teasers, not useful abstracts.
    if len(text) < 80:
        return ""

    return text


def _abstract_from_html(page_html):
    """Extract an abstract from publisher article HTML using common metadata.

    This intentionally avoids BeautifulSoup so no new dependency is needed.
    It first tries high-quality abstract metadata, then JSON-LD description,
    then a conservative Abstract section fallback.
    """
    if not page_html:
        return ""

    # 1. Common publisher metadata fields.
    # Nature/Springer, Wiley, ACS, RSC, Science, Elsevier/Cell often expose one
    # or more of these, though coverage varies by publisher and article type.
    meta_names = [
        "citation_abstract",
        "dc.description",
        "dcterms.description",
        "description",
        "og:description",
        "twitter:description",
    ]

    for name in meta_names:
        pattern = (
            r'<meta[^>]+(?:name|property)=["\']'
            + re.escape(name)
            + r'["\'][^>]+content=["\'](.*?)["\'][^>]*>'
        )
        m = re.search(pattern, page_html, flags=re.I | re.S)
        if not m:
            pattern = (
                r'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\']'
                + re.escape(name)
                + r'["\'][^>]*>'
            )
            m = re.search(pattern, page_html, flags=re.I | re.S)
        if m:
            candidate = _clean_abstract_candidate(m.group(1))
            if candidate:
                return candidate

    # 2. JSON-LD description fallback.
    # Many publishers include {"description": "..."} in article schema.
    m = re.search(
        r'"description"\s*:\s*"((?:\\.|[^"\\])*)"',
        page_html,
        flags=re.I | re.S,
    )
    if m:
        try:
            candidate = m.group(1)
            candidate = bytes(candidate, "utf-8").decode("unicode_escape")
            candidate = _clean_abstract_candidate(candidate)
            if candidate:
                return candidate
        except Exception:
            pass

    # 3. Conservative visible Abstract section fallback.
    # Strip scripts/styles first, then look near the word Abstract.
    stripped = re.sub(r"<script\b.*?</script>", " ", page_html, flags=re.I | re.S)
    stripped = re.sub(r"<style\b.*?</style>", " ", stripped, flags=re.I | re.S)

    m = re.search(
        r'(?:<h2[^>]*>|<h3[^>]*>|<div[^>]*>|<section[^>]*>)[^<]*abstract[^<]*'
        r'(.*?)(?:<h2\b|<h3\b|<section\b|</section>|references|acknowledg)',
        stripped,
        flags=re.I | re.S,
    )
    if m:
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        candidate = _clean_abstract_candidate(text)
        if candidate:
            return candidate

    return ""


def _fetch_publisher_abstract(doi):
    """Resolve DOI to the publisher page and try to extract an abstract."""
    if not doi:
        return ""

    url = "https://doi.org/" + urllib.parse.quote(doi, safe="/")
    headers = _headers()
    headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    delay = 5
    for _ in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=45, allow_redirects=True)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", delay) or delay)
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if r.status_code >= 400:
                return ""
            return _abstract_from_html(r.text)
        except Exception:
            time.sleep(delay)
            delay = min(delay * 2, 60)

    return ""


def _reconstruct_inverted_index(inv):
    """OpenAlex returns abstracts as {word: [positions]}; rebuild the text."""
    if not inv:
        return ""
    pos = []
    for word, idxs in inv.items():
        for i in idxs:
            pos.append((i, word))
    pos.sort()
    return " ".join(w for _, w in pos)


def _fetch_crossref_abstract(doi):
    """Crossref single-work lookup -> abstract ('' if none/blocked)."""
    url = WORK.format(urllib.parse.quote(doi, safe=""))
    delay = 5
    for _ in range(5):
        try:
            r = requests.get(url, params=_mailto_param(), headers=_headers(), timeout=45)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", delay) or delay)
                time.sleep(wait); delay = min(delay * 2, 60); continue
            if r.status_code == 404:
                return ""
            r.raise_for_status()
            return _clean_abstract_candidate(r.json().get("message", {}).get("abstract", ""))
        except requests.HTTPError:
            return ""
        except Exception:
            time.sleep(delay); delay = min(delay * 2, 60)
    return ""


def _fetch_semanticscholar_abstract(doi):
    """Semantic Scholar by DOI. Free; good coverage where Crossref is empty
    (e.g. Wiley/ACS). Optional S2_API_KEY env raises the rate limit."""
    url = ("https://api.semanticscholar.org/graph/v1/paper/DOI:"
           + urllib.parse.quote(doi, safe="/") + "?fields=abstract")
    headers = {}
    key = os.environ.get("S2_API_KEY", "")
    if key:
        headers["x-api-key"] = key
    delay = 5
    for _ in range(5):
        try:
            r = requests.get(url, headers=headers, timeout=45)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", delay) or delay)
                time.sleep(wait); delay = min(delay * 2, 60); continue
            if r.status_code in (400, 404):
                return ""
            r.raise_for_status()
            return _clean_abstract_candidate(r.json().get("abstract", "") or "")
        except requests.HTTPError:
            return ""
        except Exception:
            time.sleep(delay); delay = min(delay * 2, 60)
    return ""


def _fetch_openalex_abstract(doi):
    """OpenAlex by DOI. Since Feb 2026 it needs a key for sustained use, so this
    only runs when OPENALEX_KEY is set; otherwise it's skipped."""
    key = os.environ.get("OPENALEX_KEY", "")
    if not key:
        return ""
    url = "https://api.openalex.org/works/doi:" + doi
    params = {"select": "abstract_inverted_index", "api_key": key}
    mail = SETTINGS.get("crossref_mailto", "")
    if mail and "example.com" not in mail:
        params["mailto"] = mail
    delay = 5
    for _ in range(4):
        try:
            r = requests.get(url, params=params, headers=_headers(), timeout=45)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", delay) or delay)
                time.sleep(wait); delay = min(delay * 2, 60); continue
            if r.status_code >= 400:
                return ""
            inv = r.json().get("abstract_inverted_index")
            return _clean_abstract_candidate(_reconstruct_inverted_index(inv))
        except Exception:
            time.sleep(delay); delay = min(delay * 2, 60)
    return ""


def _fetch_abstract(doi):
    """Best-effort abstract from multiple sources, in order of reliability:
    Crossref -> Semantic Scholar -> OpenAlex (if keyed) -> publisher page.
    Returns the first usable abstract, or '' if none has one."""
    doi = (doi or "").strip()
    if not doi:
        return ""
    for source in (_fetch_crossref_abstract,
                   _fetch_semanticscholar_abstract,
                   _fetch_openalex_abstract,
                   _fetch_publisher_abstract):
        ab = source(doi)
        if ab:
            return ab
    return ""


def repair_abstracts(limit=0, dry_run=False, repair_all=False, min_len=200, count_only=False):
    """Fill in missing/short abstracts from multiple sources (Crossref ->
    Semantic Scholar -> OpenAlex if keyed -> publisher page).

    By default only papers whose stored abstract is shorter than `min_len`
    are re-fetched (that's the damaged set) -- much faster. Use repair_all=True
    to re-check every paper. Checkpoints every 100 and is resumable."""
    papers = load_archive()
    cap = SETTINGS.get("abstract_max_chars", 1600)
    pending = [p for p in papers
               if p.get("doi") and (repair_all or len(p.get("abstract") or "") < min_len)]
    no_doi = sum(1 for p in papers if not p.get("doi"))
    print(f"Total papers: {len(papers)} | still missing/short: {len(pending)} | no DOI: {no_doi}")
    if count_only:
        return
    pending.sort(key=lambda p: len(p.get("abstract") or ""))
    targets = pending[:limit] if limit else pending
    print(f"{'ALL papers' if repair_all else f'papers with abstract < {min_len} chars'}: "
          f"{len(targets)} to check ({'dry run' if dry_run else 'will write'})\n")

    fixed = checked = 0
    for p in targets:
        checked += 1
        ab = _fetch_abstract(p["doi"])
        if len(ab) > len(p.get("abstract") or ""):
            if len(ab) > cap:
                ab = ab[:cap].rsplit(" ", 1)[0] + "\u2026"
            p["abstract"] = ab
            fixed += 1
        if checked % 100 == 0:
            print(f"  {checked}/{len(targets)} checked, {fixed} filled")
            if not dry_run:
                write_archive(papers, report=[])          # checkpoint
        time.sleep(0.25)                                   # polite pacing

    print(f"\nDone: {fixed} abstracts filled out of {checked} checked.")
    if dry_run:
        print("(dry run - nothing written)")
    else:
        manifest = write_archive(papers, report=[])
        print(f"Archive holds {manifest['count']} papers.")


def run(dry_run=False):
    start = SETTINGS["start_date"]
    print(f"Backfilling {len(ISSNS)} journals from {start} via Crossref (per-journal)")
    print(f"Seed queries: {', '.join(CORE_QUERIES)}\n")

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
    # backfill has no live feed report; pass an empty status list
    manifest = write_archive(merged, report=[])
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
                    help="cap how many papers to repair this run (shortest first; resumable)")
    ap.add_argument("--repair-all", action="store_true",
                    help="re-check EVERY paper, not just short/missing ones (slow)")
    ap.add_argument("--repair-min-len", type=int, default=200,
                    help="treat abstracts shorter than this many chars as needing repair")
    ap.add_argument("--count-only", action="store_true",
                    help="just report how many abstracts are still missing, then exit")
    args = ap.parse_args()
    if args.verify_issns:
        verify_issns()
    elif args.repair_abstracts:
        repair_abstracts(limit=args.repair_limit, dry_run=args.dry_run,
                         repair_all=args.repair_all, min_len=args.repair_min_len, count_only=args.count_only)
    else:
        run(dry_run=args.dry_run)
