#!/usr/bin/env python3
"""aggregate.py -- build the group literature feed."""

import argparse
import datetime as dt
import html
import json
import os
import re
import time
import urllib.parse

from feeds import FEEDS, KEYWORDS, SETTINGS, ISSNS

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(value):
    if not value:
        return ""
    text = _TAG_RE.sub(" ", str(value))
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _kw_pattern(keyword):
    kw = re.escape(keyword.lower().strip())
    kw = kw.replace(r"\ ", r"\s+")
    return re.compile(r"(?<![a-z])" + kw + r"(?![a-z])", re.IGNORECASE)


_KW_PATTERNS = [(k, _kw_pattern(k)) for k in KEYWORDS]
_PV_PATTERNS = [_kw_pattern(k) for k in SETTINGS.get("perovskite_terms", [])]


def matched_keywords(text):
    found = {k for k, pat in _KW_PATTERNS if pat.search(text)}
    return sorted(found, key=str.lower)


def is_relevant(text):
    hits = matched_keywords(text)
    if not hits:
        return False, []
    if SETTINGS.get("require_perovskite"):
        if not any(pat.search(text) for pat in _PV_PATTERNS):
            return False, hits
    return True, hits


def pick_crossref_date(item):
    order = ("published-online", "published", "issued", "published-print", "created")
    rank = {k: i for i, k in enumerate(order)}
    best = None
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


def crossref_date_for_doi(doi):
    if not doi or not doi.startswith("10."):
        return ""
    try:
        import requests
        r = requests.get(
            "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""),
            params=_ab_params(), headers=_ab_headers(), timeout=15)
        if r.status_code == 200:
            return pick_crossref_date(r.json().get("message", {}))
    except Exception:
        pass
    return ""


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


def abstract_needs_topping_up(text):
    text = clean_text(text or "")
    if len(text) < 200:
        return True
    low = text.lower()
    bad_fragments = ["published online:", "doi:", "nature portfolio", "springer nature",
                     "read the latest article", "read the latest articles", "browse articles",
                     "this journal publishes", "official journal"]
    if any(fragment in low for fragment in bad_fragments):
        return True
    sentence_count = len(re.findall(r"[.!?]\s+", text))
    if len(text) < 350 and sentence_count <= 1:
        return True
    return False


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
        r = requests.get(
            "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""),
            params=_ab_params(), headers=_ab_headers(), timeout=30)
        if r.status_code == 200:
            return _clean_abstract_candidate(r.json().get("message", {}).get("abstract", ""))
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


def fetch_semanticscholar_abstract(doi):
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


def _abstract_from_html(page_html):
    if not page_html:
        return ""
    meta_names = ["citation_abstract", "dc.description", "dcterms.description",
                  "description", "og:description", "twitter:description"]
    for name in meta_names:
        pat = (r'<meta[^>]+(?:name|property)=["\']' + re.escape(name)
               + r'["\'][^>]+content=["\'](.*?)["\'][^>]*>')
        m = re.search(pat, page_html, flags=re.I | re.S)
        if not m:
            pat = (r'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\']'
                   + re.escape(name) + r'["\'][^>]*>')
            m = re.search(pat, page_html, flags=re.I | re.S)
        if m:
            cand = _clean_abstract_candidate(m.group(1))
            if cand:
                return cand
    m = re.search(r'"description"\s*:\s*"((?:\\.|[^"\\])*)"', page_html, flags=re.I | re.S)
    if m:
        try:
            cand = bytes(m.group(1), "utf-8").decode("unicode_escape")
            cand = _clean_abstract_candidate(cand)
            if cand:
                return cand
        except Exception:
            pass
    stripped = re.sub(r"<script\b.*?</script>", " ", page_html, flags=re.I | re.S)
    stripped = re.sub(r"<style\b.*?</style>", " ", stripped, flags=re.I | re.S)
    m = re.search(r"(?:<h2[^>]*>|<h3[^>]*>|<div[^>]*>|<section[^>]*>)"
                  r"[^<]*abstract[^<]*(.*?)"
                  r"(?:<h2\b|<h3\b|<section\b|</section>|references|acknowledg)",
                  stripped, flags=re.I | re.S)
    if m:
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        cand = _clean_abstract_candidate(text)
        if cand:
            return cand
    return ""


def fetch_publisher_abstract(doi):
    if not doi:
        return ""
    try:
        import requests
        url = "https://doi.org/" + urllib.parse.quote(doi, safe="/")
        headers = _ab_headers()
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        if r.status_code >= 400:
            return ""
        return _abstract_from_html(r.text)
    except Exception:
        return ""


def fetch_abstract_fast(doi):
    """Fast chain: Crossref -> OpenAlex only. No publisher-page scrape, so it
    never hangs 20s on a blocked page. Used inline by the daily job, the
    Crossref fallback, and the daily top-up so they stay quick."""
    doi = (doi or "").strip()
    if not doi or not doi.startswith("10."):
        return ""
    for src in (fetch_crossref_abstract, fetch_openalex_abstract):
        ab = src(doi)
        if ab:
            return _cap_abstract(ab)
    return ""


def fetch_abstract(doi):
    """Full chain incl. publisher-page scrape (slow on blocked pages). Reserved
    for the manual backfill repair, NOT the daily path."""
    doi = (doi or "").strip()
    if not doi or not doi.startswith("10."):
        return ""
    for src in (fetch_crossref_abstract, fetch_openalex_abstract,
                fetch_semanticscholar_abstract, fetch_publisher_abstract):
        ab = src(doi)
        if ab:
            return _cap_abstract(ab)
    return ""


def _entry_date(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return time.strftime("%Y-%m-%d", value)
            except Exception:
                pass
    return ""


_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#\"<>]+")


def _entry_doi(entry, link):
    for key in ("prism_doi", "dc_identifier", "id"):
        raw = entry.get(key, "")
        m = re.search(r"10\.\d{4,9}/\S+", str(raw))
        if m:
            return m.group(0).rstrip(").,;")
    m = re.search(r"10\.\d{4,9}/[^\s?#]+", link or "")
    if m:
        return m.group(0).rstrip(").,;")
    return (link or "").strip()


_NATURE_PREFIX_RE = re.compile(
    r"^.{0,80}?,\s*Published online:\s*.*?;\s*doi:\s*10\.\d{4,9}/\S+\s*",
    re.IGNORECASE,
)


def _strip_nature_boilerplate(text):
    return _NATURE_PREFIX_RE.sub("", text).strip()


def _entry_abstract(entry):
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
    text = _strip_nature_boilerplate(text)
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
_COMMENT_RE = re.compile(
    r"\b(comment on|reply to|matters arising|correspondence|rejoinder|editorial)\b", re.I)
_REVIEW_RE = re.compile(r"\b(review|perspective|roadmap|primer)\b", re.I)


def classify_type(title, journal, hint=""):
    text = (title or "") + " " + (hint or "")
    if _COMMENT_RE.search(text):
        return "comment"
    if journal in REVIEW_JOURNALS:
        return "review"
    if _REVIEW_RE.search(text):
        return "review"
    return "article"


def normalise_entry(entry, journal, publisher):
    title = clean_text(entry.get("title", ""))
    link = (entry.get("link") or "").strip()
    if not title or not link:
        return None
    doi = _entry_doi(entry, link)
    abstract = _entry_abstract(entry)
    if abstract_needs_topping_up(abstract):
        better = fetch_abstract_fast(doi)
        if len(better) > len(abstract):
            abstract = better
    keep, hits = is_relevant(title + " \n " + abstract)
    if not keep:
        return None
    date = _entry_date(entry) or crossref_date_for_doi(doi) or dt.date.today().isoformat()
    hint = " ".join(t.get("term", "") for t in (entry.get("tags") or []) if isinstance(t, dict))
    hint += " " + str(entry.get("dc_type", "") or "")
    return {
        "title": title, "link": link, "journal": journal, "publisher": publisher,
        "date": date, "abstract": abstract, "authors": _entry_authors(entry),
        "doi": doi, "keywords": hits, "type": classify_type(title, journal, hint),
    }


def fetch_feed(url):
    import requests
    headers = {"User-Agent": SETTINGS["user_agent"],
               "Accept": "application/rss+xml, application/xml, text/xml, */*"}
    resp = requests.get(url, headers=headers, timeout=SETTINGS["request_timeout"])
    resp.raise_for_status()
    return resp.content


IMPORTANT_FALLBACK_JOURNALS = {
    "Journal of the American Chemical Society", "Chemical Reviews",
    "Accounts of Chemical Research", "ACS Energy Letters",
    "ACS Applied Materials & Interfaces", "Joule", "Matter",
}


def normalise_crossref_work(item, journal, publisher):
    title = clean_text(" ".join(item.get("title") or []))
    if not title:
        return None
    doi = clean_text(item.get("DOI", ""))
    link = item.get("URL") or ("https://doi.org/" + doi if doi else "")
    # Use only the abstract Crossref already returned here -- do NOT fetch per
    # paper inline (that was the >30 min slowdown). The bounded daily top-up
    # fills the rest afterwards using the fast chain.
    abstract = _clean_abstract_candidate(item.get("abstract", "") or "")
    keep, hits = is_relevant(title + " \n " + abstract)
    if not keep:
        return None
    authors = []
    for a in item.get("author", []) or []:
        name = " ".join(x for x in [a.get("given", ""), a.get("family", "")] if x)
        if name:
            authors.append(clean_text(name))
    hint = item.get("type", "") or ""
    date = pick_crossref_date(item) or dt.date.today().isoformat()
    return {
        "title": title, "link": link, "journal": journal, "publisher": publisher,
        "date": date, "abstract": _cap_abstract(abstract), "authors": authors,
        "doi": doi, "keywords": hits, "type": classify_type(title, journal, hint),
    }


def crossref_recent_for_journal(journal, publisher, days=35, rows=120):
    """Fallback for important journals whose RSS feeds are blocked. Queries
    Crossref by ISSN for recent papers. Recovers the papers fast; abstracts are
    filled later by the bounded top-up (no slow per-paper scraping here)."""
    import requests
    issns = ISSNS.get(journal, [])
    if not issns:
        return []
    from_date = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    records = []
    for issn in issns:
        try:
            params = {**_ab_params(),
                      "filter": f"issn:{issn},from-pub-date:{from_date}",
                      "sort": "published", "order": "desc", "rows": rows}
            r = requests.get("https://api.crossref.org/works", params=params,
                             headers=_ab_headers(), timeout=30)
            if r.status_code != 200:
                continue
            for item in r.json().get("message", {}).get("items", []):
                rec = normalise_crossref_work(item, journal, publisher)
                if rec:
                    records.append(rec)
        except Exception:
            continue
        time.sleep(0.2)
    by_key = {}
    for rec in records:
        by_key[_key(rec)] = _better_record(by_key.get(_key(rec)), rec)
    merged = list(by_key.values())
    merged.sort(key=lambda r: (r.get("date", ""), r.get("journal", "")), reverse=True)
    return merged


def harvest():
    import feedparser
    records = []
    report = []
    for journal, publisher, url in FEEDS:
        status = {"journal": journal, "url": url, "found": 0, "kept": 0,
                  "error": None, "note": None}
        try:
            raw = fetch_feed(url)
            parsed = feedparser.parse(raw)
            status["found"] = len(parsed.entries)
            for entry in parsed.entries:
                rec = normalise_entry(entry, journal, publisher)
                if rec:
                    records.append(rec)
                    status["kept"] += 1
        except Exception as exc:
            status["error"] = f"{type(exc).__name__}: {exc}"
            if journal in IMPORTANT_FALLBACK_JOURNALS:
                fallback = crossref_recent_for_journal(journal, publisher)
                if fallback:
                    records.extend(fallback)
                    status["found"] = len(fallback)
                    status["kept"] = len(fallback)
                    status["note"] = (f"RSS failed; Crossref fallback recovered "
                                      f"{len(fallback)} relevant paper(s)")
                    status["error"] = None
        line = f"  {journal:<42} found {status['found']:>3}  kept {status['kept']:>3}"
        if status["error"]:
            line += f"  !! {status['error']}"
        elif status["note"]:
            line += f"  -- {status['note']}"
        print(line)
        report.append(status)
        time.sleep(1)
    return records, report


def _year_files(data_dir=DATA_DIR):
    if not os.path.isdir(data_dir):
        return []
    return [os.path.join(data_dir, f) for f in os.listdir(data_dir)
            if re.fullmatch(r"papers-\d{4}\.json", f)]


def load_archive(data_dir=DATA_DIR):
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


def _doi_of(rec):
    """Normalised DOI for a record, pulled from the doi field OR the link.
    This lets a link-keyed copy and a doi-keyed copy of the same paper collapse."""
    d = (rec.get("doi") or "").strip()
    if not d.startswith("10."):
        m = _DOI_RE.search(rec.get("link") or "")
        d = m.group(0) if m else ""
    return d.lower().rstrip("/").rstrip(").,;")


def _better_record(old, new):
    if not old:
        return new
    if not new:
        return old
    merged = dict(old)
    merged.update(new)
    old_abs = old.get("abstract") or ""
    new_abs = new.get("abstract") or ""
    if len(old_abs) > len(new_abs):
        merged["abstract"] = old_abs
    else:
        merged["abstract"] = new_abs
    if len(old.get("authors") or []) > len(new.get("authors") or []):
        merged["authors"] = old.get("authors") or []
    if len(old.get("keywords") or []) > len(new.get("keywords") or []):
        merged["keywords"] = old.get("keywords") or []
    old_doi = old.get("doi") or ""
    new_doi = new.get("doi") or ""
    if old_doi.startswith("10.") and not new_doi.startswith("10."):
        merged["doi"] = old_doi
    tried = old.get("ab_tried", new.get("ab_tried"))
    if tried is not None and abstract_needs_topping_up(merged.get("abstract") or ""):
        merged["ab_tried"] = tried
    else:
        merged.pop("ab_tried", None)
    return merged


def merge(existing, fresh, start_date):
    """Merge fresh into existing, de-dupe, drop pre-start_date, sort newest first.
    De-dupe is two-pass: first by primary key, then by DOI so that a paper stored
    under its link and the same paper recovered under its DOI collapse into one."""
    by_key = {}
    for rec in existing + fresh:
        by_key[_key(rec)] = _better_record(by_key.get(_key(rec)), rec)

    # Second pass: collapse anything sharing a DOI (kills link-vs-DOI duplicates).
    by_doi = {}
    no_doi = []
    for rec in by_key.values():
        d = _doi_of(rec)
        if d:
            by_doi[d] = _better_record(by_doi.get(d), rec)
        else:
            no_doi.append(rec)
    deduped = list(by_doi.values()) + no_doi

    merged = [r for r in deduped if r.get("date", "") >= start_date]
    merged.sort(key=lambda r: (r.get("date", ""), r.get("journal", "")), reverse=True)
    return merged


def write_archive(papers, report, data_dir=DATA_DIR):
    os.makedirs(data_dir, exist_ok=True)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    by_year = {}
    for p in papers:
        year = (p.get("date") or "")[:4] or "unknown"
        by_year.setdefault(year, []).append(p)
    for year, items in by_year.items():
        with open(os.path.join(data_dir, f"papers-{year}.json"), "w", encoding="utf-8") as fh:
            json.dump({"year": year, "count": len(items), "generated": generated,
                       "papers": items}, fh, ensure_ascii=False, indent=1)
    manifest = {
        "generated": generated, "count": len(papers),
        "years": sorted(by_year.keys(), reverse=True),
        "year_counts": {y: len(v) for y, v in sorted(by_year.items(), reverse=True)},
        "feeds": [{"journal": r["journal"], "found": r["found"], "kept": r["kept"],
                   "error": r["error"], "note": r.get("note")} for r in report],
    }
    with open(os.path.join(data_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    return manifest


def topup_abstracts(papers, limit, min_len=200, max_tries=2):
    """Fill a bounded number of missing/boilerplate abstracts in place, using the
    FAST chain (Crossref+OpenAlex) so the daily run stays quick. ab_tried skips
    dead ends after max_tries."""
    targets = [p for p in papers
               if p.get("doi", "").startswith("10.")
               and abstract_needs_topping_up(p.get("abstract") or "")
               and int(p.get("ab_tried", 0)) < max_tries]
    targets.sort(key=lambda p: len(p.get("abstract") or ""))
    filled = 0
    for p in targets[:limit]:
        ab = fetch_abstract_fast(p["doi"])
        if len(ab) > len(p.get("abstract") or ""):
            p["abstract"] = _cap_abstract(ab)
            p.pop("ab_tried", None)
            filled += 1
        else:
            p["ab_tried"] = int(p.get("ab_tried", 0)) + 1
        time.sleep(0.1)
    return filled


def run():
    print(f"Harvesting {len(FEEDS)} feeds ...")
    fresh, report = harvest()
    existing = load_archive()
    start = SETTINGS["start_date"]
    papers = merge(existing, fresh, start)
    daily_cap = SETTINGS.get("daily_abstract_topup", 300)
    filled = topup_abstracts(papers, limit=daily_cap) if daily_cap else 0
    manifest = write_archive(papers, report)
    new_count = len({_key(r) for r in fresh} - {_key(r) for r in existing})
    ok = sum(1 for r in report if r["error"] is None)
    missing = sum(1 for p in papers if abstract_needs_topping_up(p.get("abstract") or ""))
    print("-" * 60)
    print(f"Feeds OK: {ok}/{len(report)} | new this run: {new_count} "
          f"| archive total: {manifest['count']} (since {start})")
    print(f"Abstracts topped up this run: {filled} | still missing/short: {missing}")
    print("By year: " + ", ".join(f"{y}:{c}" for y, c in manifest["year_counts"].items()))
    print(f"Wrote {DATA_DIR}/papers-YYYY.json + manifest.json")


def selftest():
    print("Running self-test (offline)...")
    keep, hits = is_relevant("Halide segregation in wide-bandgap perovskite solar cells")
    assert keep and "perovskite" in [h.lower() for h in hits], hits
    keep, _ = is_relevant("A study of champion swimmers and onion farming")
    assert not keep
    keep, _ = is_relevant("Photocatalytic CO2 reduction over a new catalyst")
    assert keep
    assert abstract_needs_topping_up("")
    assert abstract_needs_topping_up(
        "Nature Energy, Published online: 25 June 2026; doi:10.1038/s41560-026-00000-0")
    saved = SETTINGS["require_perovskite"]
    SETTINGS["require_perovskite"] = True
    keep, _ = is_relevant("Silicon tandem photovoltaics reach new efficiency")
    assert not keep
    keep, _ = is_relevant("Perovskite-silicon tandem photovoltaics")
    assert keep
    SETTINGS["require_perovskite"] = saved

    today = dt.date.today().isoformat()
    old = "2025-12-31"
    existing = [
        {"title": "A", "doi": "10.1/a", "link": "x", "date": old, "journal": "J",
         "abstract": "old pruned abstract"},
        {"title": "B", "doi": "10.1/b", "link": "y", "date": today, "journal": "J",
         "abstract": "this useful old abstract should survive"},
    ]
    fresh = [
        {"title": "B-updated", "doi": "10.1/b", "link": "y", "date": today, "journal": "J",
         "abstract": ""},
        {"title": "C", "doi": "10.1/c", "link": "z", "date": today, "journal": "J",
         "abstract": "new abstract"},
    ]
    merged = merge(existing, fresh, start_date="2026-01-01")
    titles = sorted(r["title"] for r in merged)
    assert titles == ["B-updated", "C"], titles
    b = next(r for r in merged if r["doi"] == "10.1/b")
    assert b["abstract"] == "this useful old abstract should survive", b

    # DOI-robust dedup: link-keyed copy + doi-keyed copy collapse to one.
    dup_existing = [{"title": "Z", "doi": "", "link": "https://pubs.acs.org/doi/10.1021/z.1",
                     "date": today, "journal": "J", "abstract": "short"}]
    dup_fresh = [{"title": "Z", "doi": "10.1021/z.1", "link": "https://doi.org/10.1021/z.1",
                  "date": today, "journal": "J",
                  "abstract": "a much longer recovered abstract from crossref fallback here"}]
    dmerged = merge(dup_existing, dup_fresh, start_date="2026-01-01")
    assert len(dmerged) == 1, f"duplicate not collapsed: {dmerged}"
    assert dmerged[0]["abstract"].startswith("a much longer"), dmerged[0]
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
