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


# ---- RSS-boilerplate markers --------------------------------------------------
# Publisher RSS feeds wrap or pad the abstract with non-abstract text:
#  - Wiley glues a one-sentence "significance" blurb + a literal "ABSTRACT"
#    heading in front of the real abstract.
#  - RSC wraps the abstract in licensing/citation boilerplate.
# We strip what we safely can, and flag the rest so an API abstract replaces it.
_RSC_TRAILER_RE = re.compile(
    r"\b(To cite this article before page numbers|The content of this RSS Feed)\b.*$",
    re.IGNORECASE | re.DOTALL,
)
_WILEY_ABSTRACT_MARKER_RE = re.compile(r"\bABSTRACT\b")  # all-caps heading marker
_BOILERPLATE_MARKERS = [
    "this article is licensed under", "creative commons", "advance article",
    "the content of this rss feed", "to cite this article",
    "published online:", "nature portfolio", "springer nature",
    "read the latest article", "browse articles", "this journal publishes",
    "official journal",
]


def _clean_rss_abstract(text):
    """Strip known RSS boilerplate from a feed abstract.
    Nature date/doi prefix, Wiley significance-blurb + 'ABSTRACT' heading, and
    RSC trailing citation/feed notices. The RSC *leading* licence preamble is
    not surgically removed (too fragile); it is flagged for API replacement."""
    if not text:
        return ""
    # Nature: "<Journal>, Published online: <date>; doi:<doi> <abstract>"
    text = _NATURE_PREFIX_RE.sub("", text).strip()
    # Wiley: keep only the real abstract after the all-caps "ABSTRACT" heading.
    m = _WILEY_ABSTRACT_MARKER_RE.search(text)
    if m:
        after = text[m.end():].strip(" :\u2013-")
        if len(after) > 120:
            text = after
    # RSC: drop the trailing "To cite..."/"The content of this RSS Feed..." notice.
    text = _RSC_TRAILER_RE.sub("", text).strip()
    return text.strip()


def abstract_needs_topping_up(text):
    text = clean_text(text or "")
    if len(text) < 300:
        return True
    low = text.lower()
    if any(fragment in low for fragment in _BOILERPLATE_MARKERS):
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


def fetch_openalex_type(doi):
    """Return OpenAlex's work 'type' for a DOI (e.g. 'article', 'review',
    'editorial'), lowercased, or '' if unavailable. This is the authoritative
    signal for the article-vs-review distinction."""
    key = os.environ.get("OPENALEX_KEY", "")
    if not key:
        return ""
    try:
        import requests
        params = {"select": "type", "api_key": key}
        mail = SETTINGS.get("crossref_mailto", "")
        if mail and "example.com" not in mail:
            params["mailto"] = mail
        r = requests.get("https://api.openalex.org/works/doi:" + doi,
                         params=params, headers=_ab_headers(), timeout=30)
        if r.status_code == 200:
            return (r.json().get("type") or "").strip().lower()
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
    Crossref fallback, the pending re-check, and the daily top-up."""
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


_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"]+", re.I)


def normalise_doi(value):
    """Return a clean lowercase DOI, or '' if no DOI is present."""
    if not value:
        return ""
    m = _DOI_RE.search(str(value))
    if not m:
        return ""
    doi = m.group(0)
    doi = doi.rstrip(").,;:]}\"'")
    doi = doi.replace("&nbsp;", "")
    return doi.lower()


def compact_title(value):
    """Compact title for fallback de-duplication when DOI is missing."""
    text = clean_text(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


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
    text = _clean_rss_abstract(text)
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
    r"\b(comment on|reply to|matters arising|correspondence|rejoinder|editorial|"
    r"retraction|corrigendum|erratum|addendum)\b", re.I)
# Strong review signals in a TITLE (high precision; multi-word phrases avoid
# catching research articles that merely use a word like "advances").
_REVIEW_TITLE_RE = re.compile(
    r"\b(review|perspective|roadmap|primer|overview|survey|"
    r"state[- ]of[- ]the[- ]art|"
    r"recent (advances|progress|developments|trends)|"
    r"advances and (challenges|prospects|opportunities)|"
    r"progress and (challenges|prospects|perspectives)|"
    r"challenges and (opportunities|prospects|perspectives)|"
    r"opportunities and challenges)\b", re.I)
# Strong review signals in an ABSTRACT (catches reviews whose title gives no
# hint, e.g. "Choose Your Own Adventure: Fabrication of ...").
_REVIEW_ABSTRACT_RE = re.compile(
    r"\b(in this review|we review|this review (article|summari|provides|covers|focuses)|"
    r"here,? we review|we (comprehensively |critically |briefly )?review|"
    r"we summari[sz]e (recent|the current|the latest)|"
    r"in this perspective|this perspective (article|discusses|provides)|"
    r"we provide (a comprehensive |an )?overview|comprehensive overview of|"
    r"an overview of recent|this article reviews|we survey)\b", re.I)


def classify_type(title, journal, hint="", abstract=""):
    text = (title or "") + " " + (hint or "")
    if _COMMENT_RE.search(text):
        return "comment"
    if journal in REVIEW_JOURNALS:
        return "review"
    if _REVIEW_TITLE_RE.search(text):
        return "review"
    if abstract and _REVIEW_ABSTRACT_RE.search(abstract):
        return "review"
    return "article"


# ---- Authoritative type via OpenAlex, with heuristic fallback -----------------
# OpenAlex publishes a metadata-derived work 'type'. We trust it for the
# article-vs-review distinction and map it to our three buckets. When OpenAlex
# has no usable type for a paper, we fall back to the text heuristics above.
_OA_TYPE_MAP = {
    "review": "review",
    "editorial": "comment", "erratum": "comment", "corrigendum": "comment",
    "paratext": "comment", "letter": "article", "article": "article",
    "preprint": "article", "book-chapter": "article", "report": "article",
}


def map_oa_type(oa_type):
    """Map an OpenAlex type string to our bucket, or None if unknown/blank."""
    return _OA_TYPE_MAP.get((oa_type or "").strip().lower())


def derive_type(paper):
    """Resolve a paper's type. Precedence:
      1. An unambiguous comment/correction in the TITLE always wins.
      2. OpenAlex 'type' (paper['oa_type']) is authoritative for review vs article.
      3. Otherwise fall back to the text heuristics -- but while OpenAlex has not
         yet been consulted, never DOWNGRADE an existing review/comment (so we do
         not briefly hide a known review); OpenAlex may later correct it either way.
    """
    title = paper.get("title", "")
    journal = paper.get("journal", "")
    abstract = paper.get("abstract", "")
    if _COMMENT_RE.search(title or ""):
        return "comment"
    mapped = map_oa_type(paper.get("oa_type"))
    if mapped:
        return mapped  # authoritative
    cur = paper.get("type", "article")
    heuristic = classify_type(title, journal, "", abstract)
    if cur in ("review", "comment") and heuristic == "article":
        return cur     # preserve until OpenAlex confirms or denies
    return heuristic


# ---- Relevance evaluation + pending re-check pool -----------------------------
# A paper that fails the keyword gate is normally dropped. But a BRAND-NEW paper
# whose abstract is not yet in any API can be dropped only because we could not
# read its abstract yet -- and it would never be reconsidered. To avoid losing
# such papers, ones that (a) fail only because the abstract is still missing and
# (b) have a loose field hint in the title are parked in data/pending.json and
# re-checked on later runs once the abstract becomes available.
_LOOSE_HINT_RE = re.compile(
    r"(perovskite|photovolt|solar|tandem|optoelectron|semiconduct|bandgap|band gap|"
    r"halide|light[- ]emitting|\bled\b|photodetector|photodiode|scintillat|"
    r"photocatal|photoelectro|water[- ]splitting|\bco2\b|quantum dot|"
    r"thin[- ]film|charge transport|passivat|ion migration)", re.I)


def _build_record(title, link, journal, publisher, date, abstract, authors, doi, hits, hint=""):
    return {
        "title": title, "link": link, "journal": journal, "publisher": publisher,
        "date": date, "abstract": abstract, "authors": authors, "doi": doi,
        "keywords": hits, "type": classify_type(title, journal, hint, abstract),
    }


def evaluate_entry(entry, journal, publisher):
    """Return ('keep', record) | ('pending', candidate) | ('drop', None)."""
    title = clean_text(entry.get("title", ""))
    link = (entry.get("link") or "").strip()
    if not title or not link:
        return ("drop", None)
    doi = _entry_doi(entry, link)
    abstract = _entry_abstract(entry)
    if abstract_needs_topping_up(abstract):
        better = fetch_abstract_fast(doi)
        if len(better) > len(abstract):
            abstract = better
    keep, hits = is_relevant(title + " \n " + abstract)
    if keep:
        date = _entry_date(entry) or crossref_date_for_doi(doi) or dt.date.today().isoformat()
        hint = " ".join(t.get("term", "") for t in (entry.get("tags") or []) if isinstance(t, dict))
        hint += " " + str(entry.get("dc_type", "") or "")
        return ("keep", _build_record(title, link, journal, publisher, date,
                                       abstract, _entry_authors(entry), doi, hits, hint))
    # Not relevant on current info. Only park it if we could NOT judge fairly
    # (abstract still missing/short) AND it is plausibly in our field AND has a
    # DOI we can re-query later.
    if (abstract_needs_topping_up(abstract) and _LOOSE_HINT_RE.search(title)
            and doi.startswith("10.")):
        return ("pending", {
            "doi": doi, "title": title, "link": link, "journal": journal,
            "publisher": publisher,
            "date": _entry_date(entry) or dt.date.today().isoformat(),
            "authors": _entry_authors(entry), "tries": 0,
        })
    return ("drop", None)


def normalise_entry(entry, journal, publisher):
    """Backward-compatible wrapper: returns a record or None."""
    kind, payload = evaluate_entry(entry, journal, publisher)
    return payload if kind == "keep" else None


PENDING_FILE = os.path.join(DATA_DIR, "pending.json")


def load_pending():
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh).get("pending", [])
    except Exception:
        return []


def save_pending(items):
    os.makedirs(DATA_DIR, exist_ok=True)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(PENDING_FILE, "w", encoding="utf-8") as fh:
        json.dump({"generated": generated, "count": len(items), "pending": items},
                  fh, ensure_ascii=False, indent=1)


def process_pending(pending, limit=200, max_tries=6, max_age_days=21):
    """Re-check parked papers. Returns (promoted_records, surviving_pending).
    A paper is promoted if its abstract is now available and relevant; dropped
    if it now has a real abstract but is irrelevant, or if it has expired."""
    today = dt.date.today()
    promoted, survivors = [], []
    checked = 0
    for item in pending:
        try:
            age = (today - dt.date.fromisoformat((item.get("date", "") or "")[:10])).days
        except Exception:
            age = 0
        if int(item.get("tries", 0)) >= max_tries or age > max_age_days:
            continue  # expired -> drop
        if checked >= limit:
            survivors.append(item)  # defer to a later run
            continue
        checked += 1
        doi = item.get("doi", "")
        abstract = fetch_abstract_fast(doi)
        if abstract and not abstract_needs_topping_up(abstract):
            keep, hits = is_relevant((item.get("title", "") or "") + " \n " + abstract)
            if keep:
                promoted.append(_build_record(
                    item.get("title", ""), item.get("link", ""), item.get("journal", ""),
                    item.get("publisher", ""), item.get("date", "") or today.isoformat(),
                    _cap_abstract(abstract), item.get("authors", []), doi, hits))
            # relevant -> promoted (drop from pool); irrelevant -> drop from pool
            continue
        item["tries"] = int(item.get("tries", 0)) + 1
        survivors.append(item)
    return promoted, survivors


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
    # Wiley now 403-blocks GitHub's servers too -> recover via Crossref by ISSN.
    "Advanced Materials", "Advanced Energy Materials",
    "Advanced Functional Materials", "Angewandte Chemie Int. Ed.",
    "Small", "Small Methods",
}


def _crossref_recent_items(issn, cutoff, rows):
    """Fetch recent Crossref items for one ISSN, using BOTH date filters.

    Publishers stamp dates inconsistently: ACS/Wiley records match
    'from-pub-date', but RSC (EES, Chem Soc Rev) deposits records whose
    publication date falls outside that window, so a pub-date query returns
    nothing even though the journal is publishing normally. 'from-created-date'
    is when the DOI was registered, which catches those. We union both and
    de-duplicate by DOI so no publisher's convention is missed."""
    import requests
    out, seen = [], set()
    for flt in (f"issn:{issn},from-created-date:{cutoff}",
                f"issn:{issn},from-pub-date:{cutoff}"):
        try:
            params = {**_ab_params(), "filter": flt, "sort": "created",
                      "order": "desc", "rows": rows}
            r = requests.get("https://api.crossref.org/works", params=params,
                             headers=_ab_headers(), timeout=30)
            if r.status_code != 200:
                continue
            for item in r.json().get("message", {}).get("items", []):
                d = normalise_doi(item.get("DOI", ""))
                if d and d not in seen:
                    seen.add(d)
                    out.append(item)
        except Exception:
            continue
        time.sleep(0.2)
    return out


def evaluate_crossref_work(item, journal, publisher):
    """Crossref work -> ('keep', record) | ('pending', candidate) | ('drop', None).

    Mirrors evaluate_entry: Crossref frequently has no abstract (RSC and Wiley
    deposit few), so a paper judged only on its title would be dropped forever.
    Field-plausible papers without an abstract are parked for re-check instead."""
    title = clean_text(" ".join(item.get("title") or []))
    if not title:
        return ("drop", None)
    doi = normalise_doi(item.get("DOI", ""))
    link = item.get("URL") or ("https://doi.org/" + doi if doi else "")
    abstract = _clean_abstract_candidate(item.get("abstract", "") or "")
    authors = []
    for a in item.get("author", []) or []:
        name = " ".join(x for x in [a.get("given", ""), a.get("family", "")] if x)
        if name:
            authors.append(clean_text(name))
    date = pick_crossref_date(item) or dt.date.today().isoformat()
    keep, hits = is_relevant(title + " \n " + abstract)
    if keep:
        hint = item.get("type", "") or ""
        return ("keep", _build_record(title, link, journal, publisher, date,
                                      _cap_abstract(abstract), authors, doi, hits, hint))
    if (abstract_needs_topping_up(abstract) and _LOOSE_HINT_RE.search(title)
            and doi.startswith("10.")):
        return ("pending", {"doi": doi, "title": title, "link": link,
                            "journal": journal, "publisher": publisher,
                            "date": date, "authors": authors, "tries": 0})
    return ("drop", None)


def normalise_crossref_work(item, journal, publisher):
    """Backward-compatible wrapper: returns a record or None."""
    kind, payload = evaluate_crossref_work(item, journal, publisher)
    return payload if kind == "keep" else None


def crossref_recent_for_journal(journal, publisher, days=35, rows=120):
    """Pull recent papers for one journal from Crossref by ISSN.
    Returns (records, pending). Used both as the RSS-failure fallback and as the
    daily sweep, so discovery never depends on a publisher's RSS being healthy
    or on its RSS carrying a full abstract."""
    import requests
    issns = ISSNS.get(journal, [])
    if not issns:
        return [], []
    from_date = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    records, pending = [], []
    for issn in issns:
        for item in _crossref_recent_items(issn, from_date, rows):
            kind, payload = evaluate_crossref_work(item, journal, publisher)
            if kind == "keep":
                records.append(payload)
            elif kind == "pending":
                pending.append(payload)
    by_key = {}
    for rec in records:
        by_key[_key(rec)] = _better_record(by_key.get(_key(rec)), rec)
    merged = list(by_key.values())
    merged.sort(key=lambda r: (r.get("date", ""), r.get("journal", "")), reverse=True)
    return merged, pending


def crossref_sweep(days=None, rows=120):
    """Daily Crossref sweep across EVERY journal that has an ISSN, whether or not
    its RSS worked. RSS feeds are unreliable in two different ways: they break
    (403/SSL), and several carry only a one-line teaser instead of the abstract,
    so keyword matching on RSS alone silently misses papers. Crossref by ISSN is
    the stable backbone; merge() de-duplicates the overlap with RSS.
    Returns (records, pending, per_journal_counts)."""
    if days is None:
        days = SETTINGS.get("crossref_sweep_days", 21)
    records, pending, counts = [], [], {}
    for journal, publisher, _url in FEEDS:
        if journal not in ISSNS:
            continue
        recs, pend = crossref_recent_for_journal(journal, publisher, days=days, rows=rows)
        records.extend(recs)
        pending.extend(pend)
        counts[journal] = len(recs)
    return records, pending, counts


def harvest():
    """Fetch and parse all feeds. Returns (records, report, new_pending)."""
    import feedparser
    records = []
    report = []
    new_pending = []
    for journal, publisher, url in FEEDS:
        status = {"journal": journal, "url": url, "found": 0, "kept": 0,
                  "error": None, "note": None}
        try:
            raw = fetch_feed(url)
            parsed = feedparser.parse(raw)
            status["found"] = len(parsed.entries)
            for entry in parsed.entries:
                kind, payload = evaluate_entry(entry, journal, publisher)
                if kind == "keep":
                    records.append(payload)
                    status["kept"] += 1
                elif kind == "pending":
                    new_pending.append(payload)
        except Exception as exc:
            status["error"] = f"{type(exc).__name__}: {exc}"
            if journal in IMPORTANT_FALLBACK_JOURNALS:
                fallback, fb_pending = crossref_recent_for_journal(journal, publisher)
                new_pending.extend(fb_pending)
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
    return records, report, new_pending


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


def _repair_record_identifier(rec):
    """Repair DOI for old archive records where DOI was previously missed."""
    if not rec:
        return rec
    doi = normalise_doi(rec.get("doi", ""))
    if not doi:
        for field in ("link", "abstract", "title"):
            doi = normalise_doi(rec.get(field, ""))
            if doi:
                break
    if doi:
        rec["doi"] = doi
    return rec


def _key(rec):
    """Stable de-duplication key. DOI preferred; else journal + compact title."""
    rec = _repair_record_identifier(rec)
    doi = normalise_doi(rec.get("doi", ""))
    if doi:
        return "doi:" + doi
    title_key = compact_title(rec.get("title", ""))
    journal_key = compact_title(rec.get("journal", ""))
    if title_key and journal_key:
        return "title:" + journal_key + ":" + title_key
    return "link:" + (rec.get("link") or rec.get("title", "")).lower()


def _doi_of(rec):
    """Normalised DOI for the second-pass dedup, from the doi field or the link."""
    rec = _repair_record_identifier(rec)
    return normalise_doi(rec.get("doi", ""))


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
    Two-pass de-dupe: by primary key, then by DOI (collapses link-vs-DOI dupes)."""
    by_key = {}
    for rec in existing + fresh:
        by_key[_key(rec)] = _better_record(by_key.get(_key(rec)), rec)
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


def write_archive(papers, report=None, data_dir=DATA_DIR):
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
    if report is None:
        # Types-only pass: keep the last run's feed statuses instead of blanking them.
        try:
            with open(os.path.join(data_dir, "manifest.json"), "r", encoding="utf-8") as fh:
                feeds = json.load(fh).get("feeds", [])
        except Exception:
            feeds = []
    else:
        feeds = [{"journal": r["journal"], "found": r["found"], "kept": r["kept"],
                  "error": r["error"], "note": r.get("note")} for r in report]
    manifest = {
        "generated": generated, "count": len(papers),
        "years": sorted(by_year.keys(), reverse=True),
        "year_counts": {y: len(v) for y, v in sorted(by_year.items(), reverse=True)},
        "feeds": feeds,
    }
    with open(os.path.join(data_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
    return manifest


def reclassify_types(papers):
    """Recompute every paper's 'type' via derive_type (OpenAlex-authoritative,
    heuristic fallback). Offline and fast once OpenAlex types are cached on the
    records. Type-only and reversible -- it touches nothing but the 'type' field
    and is recomputed each run, so it self-corrects as OpenAlex data fills in.
    Returns the count changed this pass."""
    changed = 0
    for p in papers:
        new = derive_type(p)
        if new != p.get("type"):
            p["type"] = new
            changed += 1
    return changed


def fill_openalex_types(papers, limit, max_tries=2):
    """Cache OpenAlex's 'type' onto records that lack it, NEWEST papers first
    (so the recent feed your students browse becomes authoritative quickly).
    Bounded per run; an 'oa_type_tried' counter skips dead ends after max_tries.
    Returns the count filled."""
    targets = [p for p in papers
               if p.get("doi", "").startswith("10.")
               and not p.get("oa_type")
               and int(p.get("oa_type_tried", 0)) < max_tries]
    targets.sort(key=lambda p: p.get("date", ""), reverse=True)  # newest first
    filled = 0
    for p in targets[:limit]:
        t = fetch_openalex_type(p["doi"])
        if t:
            p["oa_type"] = t
            p.pop("oa_type_tried", None)
            filled += 1
        else:
            p["oa_type_tried"] = int(p.get("oa_type_tried", 0)) + 1
        time.sleep(0.1)
    return filled


def topup_abstracts(papers, limit, min_len=300, max_tries=2):
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


def _dedupe_pending(items, known_dois):
    """Keep one entry per DOI; drop any whose DOI is already in the archive."""
    out, seen = [], set()
    for it in items:
        d = normalise_doi(it.get("doi", ""))
        if not d or d in known_dois or d in seen:
            continue
        seen.add(d)
        out.append(it)
    return out


def run():
    print(f"Harvesting {len(FEEDS)} feeds ...")
    fresh, report, new_pending = harvest()

    # Crossref sweep across all journals with an ISSN. RSS is a bonus, not the
    # source of truth: some feeds break, and several (RSC especially) carry only
    # a teaser sentence, so relevance judged on RSS alone misses real papers.
    sweep_days = SETTINGS.get("crossref_sweep_days", 21)
    swept, swept_pending, sweep_counts = ([], [], {})
    if sweep_days:
        print(f"Crossref sweep ({sweep_days}d, all journals with an ISSN) ...")
        swept, swept_pending, sweep_counts = crossref_sweep(days=sweep_days)
        top = sorted(sweep_counts.items(), key=lambda kv: kv[1], reverse=True)[:6]
        print("  recovered: " + ", ".join(f"{j}:{n}" for j, n in top if n) or "  recovered: none")
        fresh = fresh + swept
        new_pending = new_pending + swept_pending

    existing = load_archive()
    start = SETTINGS["start_date"]

    # Re-check the parked pool: promote now-relevant papers, drop the rest.
    pending = load_pending()
    promoted, pending_left = process_pending(pending)

    papers = merge(existing, fresh + promoted, start)

    # Park newly-seen borderline papers, minus anything now in the archive.
    archive_dois = {normalise_doi(p.get("doi", "")) for p in papers}
    archive_dois.discard("")
    pending_final = _dedupe_pending(pending_left + new_pending, archive_dois)
    save_pending(pending_final)

    daily_cap = SETTINGS.get("daily_abstract_topup", 300)
    filled = topup_abstracts(papers, limit=daily_cap) if daily_cap else 0

    # Cache OpenAlex's authoritative 'type' onto records (newest first, bounded),
    # then re-derive every paper's type from it (heuristic fallback where OpenAlex
    # has nothing yet). Type-only and reversible.
    oa_cap = SETTINGS.get("daily_oa_type_fill", 800)
    oa_filled = fill_openalex_types(papers, limit=oa_cap) if oa_cap else 0
    reclassified = reclassify_types(papers)

    manifest = write_archive(papers, report)

    new_count = len({_key(r) for r in fresh} - {_key(r) for r in existing})
    ok = sum(1 for r in report if r["error"] is None)
    missing = sum(1 for p in papers if abstract_needs_topping_up(p.get("abstract") or ""))
    typed = sum(1 for p in papers if p.get("oa_type"))
    print("-" * 60)
    print(f"Feeds OK: {ok}/{len(report)} | new this run: {new_count} "
          f"| archive total: {manifest['count']} (since {start})")
    print(f"Abstracts topped up: {filled} | still missing/short: {missing} "
          f"| promoted from pending: {len(promoted)} | pending pool: {len(pending_final)}")
    print(f"OpenAlex types filled: {oa_filled} (cached total {typed}/{manifest['count']}) "
          f"| types reclassified this run: {reclassified}")
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

    # --- abstract cleaning ---
    wiley = ("A barrier-free cathode contact is established by increasing the carrier "
             "density of SnO x with a record efficiency of 22.79% ABSTRACT Compared to "
             "vacuum-deposited electrodes of metal or metal oxides, printable carbon "
             "electrodes offer a more sustainable approach toward commercialization of "
             "perovskite solar cells, and this work demonstrates a clean route to them.")
    cw = _clean_rss_abstract(wiley)
    assert cw.startswith("Compared to vacuum-deposited"), cw
    assert "ABSTRACT" not in cw and "22.79%" not in cw, cw
    rsc = ("Energy Environ. Sci. , 2026, Advance Article DOI : 10.1039/D6EE00100A, "
           "Communication Open Access This article is licensed under a Creative Commons "
           "Attribution 3.0 Unported Licence. A new hydrogen-free exsolution route works. "
           "To cite this article before page numbers are assigned, use the DOI form of "
           "citation above. The content of this RSS Feed (c) The Royal Society of Chemistry")
    cr = _clean_rss_abstract(rsc)
    assert "To cite this article" not in cr and "content of this RSS Feed" not in cr, cr
    assert abstract_needs_topping_up(rsc), "RSC boilerplate should be flagged for API replacement"

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
        {"title": "A", "doi": "10.1000/a", "link": "x", "date": old, "journal": "J",
         "abstract": "old pruned abstract"},
        {"title": "B", "doi": "10.1000/b", "link": "y", "date": today, "journal": "J",
         "abstract": "this useful old abstract should survive"},
    ]
    fresh = [
        {"title": "B-updated", "doi": "10.1000/b", "link": "y", "date": today, "journal": "J",
         "abstract": ""},
        {"title": "C", "doi": "10.1000/c", "link": "z", "date": today, "journal": "J",
         "abstract": "new abstract"},
    ]
    merged = merge(existing, fresh, start_date="2026-01-01")
    titles = sorted(r["title"] for r in merged)
    assert titles == ["B-updated", "C"], titles
    b = next(r for r in merged if r["doi"] == "10.1000/b")
    assert b["abstract"] == "this useful old abstract should survive", b

    dup_existing = [{"title": "Z", "doi": "", "link": "https://pubs.acs.org/doi/10.1021/z.1",
                     "date": today, "journal": "J", "abstract": "short"}]
    dup_fresh = [{"title": "Z", "doi": "10.1021/z.1", "link": "https://doi.org/10.1021/z.1",
                  "date": today, "journal": "J",
                  "abstract": "a much longer recovered abstract from crossref fallback here"}]
    dmerged = merge(dup_existing, dup_fresh, start_date="2026-01-01")
    assert len(dmerged) == 1, f"duplicate not collapsed: {dmerged}"
    assert dmerged[0]["abstract"].startswith("a much longer"), dmerged[0]

    # --- pending pool logic (with a stubbed fast fetch) ---
    # Patch via globals() so it works whether this runs as __main__ or aggregate.
    g = globals()
    real = g["fetch_abstract_fast"]
    long_relevant = (
        "This work reports a wide-bandgap perovskite solar cell in which halide "
        "segregation is suppressed, reducing non-radiative recombination at the "
        "absorber/transport-layer interface and raising the open-circuit voltage. "
        "Quasi-Fermi-level splitting measurements indicate the loss is interfacial, "
        "and the improved sub-cell is integrated into an all-perovskite tandem "
        "photovoltaic device, demonstrating a clear advance for multijunction "
        "solar cells with a stable, reproducible fabrication route overall.")
    # promote case
    g["fetch_abstract_fast"] = lambda doi: long_relevant if doi == "10.x/rel" else ""
    pend = [{"doi": "10.x/rel", "title": "A clever ligand for efficient devices",
             "link": "l", "journal": "J", "publisher": "P", "date": today,
             "authors": ["X"], "tries": 0}]
    promoted, survivors = process_pending(pend)
    assert len(promoted) == 1 and not survivors, (promoted, survivors)
    assert promoted[0]["keywords"], "promoted paper should carry matched keywords"
    # still-missing case -> stays, tries incremented
    g["fetch_abstract_fast"] = lambda doi: ""
    pend2 = [{"doi": "10.x/none", "title": "A clever ligand for efficient devices",
              "link": "l", "journal": "J", "publisher": "P", "date": today,
              "authors": [], "tries": 0}]
    promoted2, survivors2 = process_pending(pend2)
    assert not promoted2 and survivors2 and survivors2[0]["tries"] == 1, (promoted2, survivors2)
    # expired -> dropped
    oldd = (dt.date.today() - dt.timedelta(days=40)).isoformat()
    pend3 = [{"doi": "10.x/old", "title": "x", "date": oldd, "tries": 0}]
    promoted3, survivors3 = process_pending(pend3)
    assert not promoted3 and not survivors3, (promoted3, survivors3)
    g["fetch_abstract_fast"] = real

    assert clean_text("<p>Hello&nbsp;<b>world</b></p>") == "Hello world"

    # --- paper-type classification (title + abstract) ---
    assert classify_type("Perovskite Solar Cells for Space Applications: Progress and Challenges", "Advanced Materials") == "review"
    assert classify_type("Recent Advances in Wide-Bandgap Perovskite Solar Cells", "Advanced Energy Materials") == "review"
    assert classify_type("Halide Perovskites for Photovoltaics: A Review", "Small") == "review"
    assert classify_type("State-of-the-Art Passivation of Perovskite Interfaces", "Advanced Materials") == "review"
    assert classify_type("Choose Your Own Adventure: Fabrication of Tandem Photovoltaics",
                         "Advanced Materials", "",
                         "Here, we review the fabrication of monolithic all-perovskite "
                         "tandem photovoltaics and summarise recent progress in the field.") == "review"
    assert classify_type("Monolithic All-Perovskite Tandem Solar Cells with Minimized Optical and Energetic Losses", "Advanced Materials") == "article"
    assert classify_type("Highly Efficient Perovskite-Perovskite Tandem Solar Cells Reaching 80% of the Theoretical Limit in Photovoltage", "Advanced Materials") == "article"
    assert classify_type("Barrier-Free Cathode Contacts for High-Efficiency Inverted Carbon-Perovskite Solar Cells", "Advanced Energy Materials") == "article"
    assert classify_type("Anything at all", "Chemical Society Reviews") == "review"
    pp = [{"title": "A plain research paper on solar cells", "journal": "Advanced Materials",
           "abstract": "We fabricate a device.", "type": "review"},
          {"title": "Perovskites: Progress and Challenges", "journal": "Advanced Materials",
           "abstract": "", "type": "article"}]
    n = reclassify_types(pp)
    assert n == 1 and pp[0]["type"] == "review" and pp[1]["type"] == "review", pp

    # --- OpenAlex authoritative type (article vs review) ---
    assert map_oa_type("review") == "review"
    assert map_oa_type("article") == "article"
    assert map_oa_type("editorial") == "comment"
    assert map_oa_type("") is None and map_oa_type("weird-unknown") is None
    # OpenAlex authoritatively marks a review the heuristics would miss:
    p_oa_rev = {"title": "Choose Your Own Adventure: Fabrication of Tandems",
                "journal": "Advanced Materials", "abstract": "We fabricate devices.",
                "type": "article", "oa_type": "review"}
    assert derive_type(p_oa_rev) == "review"
    # OpenAlex authoritatively marks an ARTICLE even if the title looks review-ish,
    # so a genuine research paper is not hidden from the article view:
    p_oa_art = {"title": "Recent Advances Enable a Record-Efficiency Tandem Cell",
                "journal": "Advanced Materials", "abstract": "",
                "type": "review", "oa_type": "article"}
    assert derive_type(p_oa_art) == "article", derive_type(p_oa_art)
    # No OpenAlex yet -> heuristic fallback, but never downgrade a known review:
    p_nooa = {"title": "A plain device paper", "journal": "Advanced Materials",
              "abstract": "We fabricate.", "type": "review"}
    assert derive_type(p_nooa) == "review"
    # reclassify applies OpenAlex authoritatively (can now downgrade a mislabel):
    pp2 = [{"title": "Looks like a review: Progress and Challenges",
            "journal": "Advanced Materials", "abstract": "", "type": "review",
            "oa_type": "article"}]
    reclassify_types(pp2)
    assert pp2[0]["type"] == "article", pp2

    # --- Crossref path: keep / pending / drop ---
    it_keep = {"title": ["Wide-bandgap perovskite solar cells with low voltage loss"],
               "DOI": "10.1039/d6ee00001a", "abstract": ""}
    assert evaluate_crossref_work(it_keep, "Energy & Environmental Science", "RSC")[0] == "keep"
    # No abstract yet + field-plausible title that misses the keyword gate -> PENDING
    it_pend = {"title": ["Interfacial engineering of halide thin-film optoelectronic devices"],
               "DOI": "10.1039/d6ee00002a", "abstract": ""}
    kind, cand = evaluate_crossref_work(it_pend, "Energy & Environmental Science", "RSC")
    assert kind == "pending", (kind, cand)
    assert cand["doi"] == "10.1039/d6ee00002a"
    # Clearly out of scope -> drop
    it_drop = {"title": ["A study of onion farming in temperate climates"],
               "DOI": "10.1039/d6ee00003a", "abstract": ""}
    assert evaluate_crossref_work(it_drop, "Energy & Environmental Science", "RSC")[0] == "drop"
    # wrapper still returns a record or None
    assert normalise_crossref_work(it_keep, "Energy & Environmental Science", "RSC") is not None
    assert normalise_crossref_work(it_drop, "Energy & Environmental Science", "RSC") is None

    print("All self-tests passed.")


def why_missing(journal, days=30, rows=200):
    """Read-only diagnosis for ONE journal: list what Crossref published in the
    window and say, for each paper, whether we stored it -- and if not, exactly
    which stage dropped it. Also flags papers we DID store that the website's
    "Perovskite only" checkbox would hide."""
    import requests
    issns = ISSNS.get(journal, [])
    if not issns:
        print(f"No ISSN configured for {journal!r}."); return
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()

    archive = load_archive()
    stored = {}
    for p in archive:
        d = normalise_doi(p.get("doi", ""))
        if d:
            stored[d] = p
    pending_dois = {normalise_doi(x.get("doi", "")) for x in load_pending()}

    # Probe each ISSN three ways so a wrong ISSN, a date-filter problem and a
    # blocked/failed request cannot be confused with "nothing published".
    items = []
    print(f"ISSN probe for {journal} (cutoff {cutoff}):")
    for issn in issns:
        for label, flt in (("all-time        ", f"issn:{issn}"),
                           ("pub-date window ", f"issn:{issn},from-pub-date:{cutoff}"),
                           ("created window  ", f"issn:{issn},from-created-date:{cutoff}")):
            try:
                r = requests.get("https://api.crossref.org/works",
                                 params={**_ab_params(), "filter": flt, "rows": 0},
                                 headers=_ab_headers(), timeout=30)
                if r.status_code == 200:
                    tot = r.json().get("message", {}).get("total-results", 0)
                    print(f"  {issn}  {label} HTTP 200  total-results = {tot}")
                else:
                    print(f"  {issn}  {label} HTTP {r.status_code}  <- request refused")
            except Exception as exc:
                print(f"  {issn}  {label} FAILED: {type(exc).__name__}: {exc}")
            time.sleep(0.2)
        got = _crossref_recent_items(issn, cutoff, rows)
        print(f"  {issn}  fetched {len(got)} item(s) (both filters, de-duplicated)")
        items.extend(got)

    # If the ISSN route is empty, try resolving the journal by name instead --
    # this distinguishes "our ISSN is wrong" from "nothing was published".
    if not items:
        try:
            r = requests.get("https://api.crossref.org/journals",
                             params={**_ab_params(), "query": journal, "rows": 5},
                             headers=_ab_headers(), timeout=30)
            if r.status_code == 200:
                print("  Crossref's own ISSNs for this title:")
                for j in r.json().get("message", {}).get("items", []):
                    print(f"    {j.get('title','?')[:60]:<60} ISSNs: {j.get('ISSN')}")
        except Exception as exc:
            print(f"  journal lookup failed: {type(exc).__name__}: {exc}")

    seen, tally = set(), {}
    print(f"\n{journal} -- Crossref items in the last {days} days: {len(items)}")
    print("=" * 78)
    for item in items:
        doi = normalise_doi(item.get("DOI", ""))
        if not doi or doi in seen:
            continue
        seen.add(doi)
        title = clean_text(" ".join(item.get("title") or []))[:88]
        cr_abs = _clean_abstract_candidate(item.get("abstract", "") or "")
        rec = stored.get(doi)
        if rec:
            text = (rec.get("title", "") + " " + (rec.get("abstract") or "") + " "
                    + " ".join(rec.get("keywords") or [])).lower()
            if "perovskite" in text:
                status = "STORED"
            else:
                status = "STORED-BUT-HIDDEN (pvonly)"
        elif doi in pending_dois:
            status = "PENDING (awaiting abstract)"
        else:
            keep, _hits = is_relevant(title + " \n " + cr_abs)
            if keep:
                status = "MISSING (relevant but not stored!)"
            elif _LOOSE_HINT_RE.search(title):
                status = ("DROPPED-no-abstract" if abstract_needs_topping_up(cr_abs)
                          else "DROPPED-not-relevant")
            else:
                status = "DROPPED-off-topic"
        tally[status] = tally.get(status, 0) + 1
        if not status.startswith("DROPPED-off-topic"):
            print(f"  [{status}] {title}")
    print("-" * 78)
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>4}  {k}")
    print("\nReading it: STORED-BUT-HIDDEN means the paper IS in your archive but the")
    print("website's 'Perovskite only' box hides it (no 'perovskite' in its stored text,")
    print("usually because the abstract is still empty). MISSING means a real gap.")


def audit_coverage(days=365):
    """Compare what we store per journal against what Crossref says exists, so
    coverage gaps are measured rather than guessed. Read-only: writes nothing."""
    import requests
    papers = load_archive()
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    mine = {}
    for p in papers:
        if (p.get("date") or "") >= cutoff:
            mine[p.get("journal", "")] = mine.get(p.get("journal", ""), 0) + 1
    print(f"Coverage audit, last {days} days (stored vs Crossref total for the journal)")
    print(f"{'journal':<42}{'stored':>8}{'crossref':>10}")
    print("-" * 60)
    for journal, _publisher, _url in FEEDS:
        issns = ISSNS.get(journal, [])
        total = 0
        for issn in issns:
            # Query BOTH date filters: RSC deposits records whose pub-date falls
            # outside the window, so a pub-date-only count reports a false zero.
            for flt in (f"issn:{issn},from-created-date:{cutoff}",
                        f"issn:{issn},from-pub-date:{cutoff}"):
                try:
                    r = requests.get("https://api.crossref.org/works",
                                     params={**_ab_params(), "filter": flt, "rows": 0},
                                     headers=_ab_headers(), timeout=30)
                    if r.status_code == 200:
                        total = max(total, r.json().get("message", {}).get("total-results", 0))
                except Exception:
                    pass
                time.sleep(0.2)
        print(f"{journal:<42}{mine.get(journal, 0):>8}{total:>10}")
    print("\nNote: 'crossref' is ALL papers in that journal, not just relevant ones,")
    print("so stored should be much smaller. Look for journals storing near zero.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the group literature feed.")
    ap.add_argument("--selftest", action="store_true", help="run offline logic tests")
    ap.add_argument("--fill-oa-types", nargs="?", type=int, const=100000, default=None,
                    metavar="N",
                    help="one-time: cache OpenAlex 'type' for up to N archive papers "
                         "(newest first), re-derive types, and rewrite. Resumable; "
                         "run again to continue. Does not fetch feeds.")
    ap.add_argument("--audit", nargs="?", type=int, const=365, default=None,
                    metavar="DAYS",
                    help="read-only: compare stored papers per journal against "
                         "Crossref totals for the last DAYS (default 365)")
    ap.add_argument("--why-missing", nargs=2, metavar=("JOURNAL", "DAYS"), default=None,
                    help='read-only: for one journal, show which stage drops each '
                         'recent paper, e.g. --why-missing "Energy & Environmental Science" 30')
    args = ap.parse_args()
    if args.why_missing is not None:
        why_missing(args.why_missing[0], int(args.why_missing[1]))
    elif args.audit is not None:
        audit_coverage(args.audit)
    elif args.selftest:
        selftest()
    elif args.fill_oa_types is not None:
        print(f"Filling OpenAlex types for up to {args.fill_oa_types} paper(s)...")
        papers = load_archive()
        oa_filled = fill_openalex_types(papers, limit=args.fill_oa_types)
        reclassified = reclassify_types(papers)
        typed = sum(1 for p in papers if p.get("oa_type"))
        manifest = write_archive(papers)  # report=None -> preserve feed statuses
        remaining = sum(1 for p in papers
                        if p.get("doi", "").startswith("10.") and not p.get("oa_type")
                        and int(p.get("oa_type_tried", 0)) < 2)
        print(f"OpenAlex types filled: {oa_filled} "
              f"(cached total {typed}/{manifest['count']}) | "
              f"types changed: {reclassified} | still to try: {remaining}")
        print("Run again to continue if 'still to try' is > 0.")
    else:
        run()
