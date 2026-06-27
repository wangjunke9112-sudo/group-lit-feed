"""
Configuration for the group literature feed.

Everything you are likely to change lives in this file:
  - FEEDS      : the journals to watch (name, publisher, RSS URL)
  - KEYWORDS   : the search terms used to decide if a paper is relevant
  - SETTINGS   : matching behaviour, how long to keep papers, etc.

Adding a journal later is a one-line edit. URL patterns by publisher:
  Nature   : https://www.nature.com/<code>.rss
  ACS      : https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc=<code>
  RSC      : https://feeds.rsc.org/rss/<CODE>
  Wiley    : https://onlinelibrary.wiley.com/feed/<online-ISSN-no-dashes>/most-recent
  Science  : https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=<code>
  CellPress: https://www.cell.com/<journal-slug>/inpress.rss
"""

# ---------------------------------------------------------------------------
# 1. JOURNALS
# Each entry: (display name, publisher label, RSS feed URL)
# The publisher label is only used for grouping/colour in the UI.
# ---------------------------------------------------------------------------
FEEDS = [
    # ---- Nature Portfolio -------------------------------------------------
    ("Nature",                          "Nature",  "https://www.nature.com/nature.rss"),
    ("Nature Energy",                   "Nature",  "https://www.nature.com/nenergy.rss"),
    ("Nature Materials",                "Nature",  "https://www.nature.com/nmat.rss"),
    ("Nature Chemistry",                "Nature",  "https://www.nature.com/nchem.rss"),
    ("Nature Synthesis",                "Nature",  "https://www.nature.com/natsynth.rss"),
    ("Nature Sustainability",           "Nature",  "https://www.nature.com/natsustain.rss"),
    ("Nature Photonics",                "Nature",  "https://www.nature.com/nphoton.rss"),
    ("Nature Nanotechnology",           "Nature",  "https://www.nature.com/nnano.rss"),
    ("Nature Communications",           "Nature",  "https://www.nature.com/ncomms.rss"),
    ("Nature Electronics",              "Nature",  "https://www.nature.com/natelectron.rss"),
    ("Nature Methods",                  "Nature",  "https://www.nature.com/nmeth.rss"),
    ("Nature Reviews Materials",        "Nature",  "https://www.nature.com/natrevmats.rss"),
    ("Nature Reviews Methods Primers",  "Nature",  "https://www.nature.com/nrmp.rss"),
    ("Nature Physics",                  "Nature",  "https://www.nature.com/nphys.rss"),
    ("Nature Reviews Chemistry",        "Nature",  "https://www.nature.com/natrevchem.rss"),

    # ---- ACS --------------------------------------------------------------
    ("Journal of the American Chemical Society", "ACS", "https://pubs.acs.org/action/showFeed?feed=rss&jc=jacsat&type=axatoc"),
    ("Chemical Reviews",                "ACS",     "https://pubs.acs.org/action/showFeed?feed=rss&jc=chreay&type=axatoc"),
    ("Accounts of Chemical Research",   "ACS",     "https://pubs.acs.org/action/showFeed?feed=rss&jc=achre4&type=axatoc"),
    ("ACS Energy Letters",              "ACS",     "https://pubs.acs.org/action/showFeed?feed=rss&jc=aelccp&type=axatoc"),
    ("ACS Applied Materials & Interfaces","ACS",   "https://pubs.acs.org/action/showFeed?feed=rss&jc=aamick&type=axatoc"),

    # ---- RSC --------------------------------------------------------------
    ("Chemical Society Reviews",        "RSC",     "http://feeds.rsc.org/rss/cs"),
    ("Energy & Environmental Science",  "RSC",     "http://feeds.rsc.org/rss/ee"),

    # ---- Wiley ------------------------------------------------------------
    ("Advanced Materials",              "Wiley",   "https://onlinelibrary.wiley.com/feed/15214095/most-recent"),
    ("Advanced Energy Materials",       "Wiley",   "https://onlinelibrary.wiley.com/feed/16146840/most-recent"),
    ("Advanced Functional Materials",   "Wiley",   "https://onlinelibrary.wiley.com/feed/16163028/most-recent"),
    ("Angewandte Chemie Int. Ed.",      "Wiley",   "https://onlinelibrary.wiley.com/feed/15213773/most-recent"),
    ("Small",                           "Wiley",   "https://onlinelibrary.wiley.com/feed/16136829/most-recent"),
    ("Small Methods",                   "Wiley",   "https://onlinelibrary.wiley.com/feed/23669608/most-recent"),

    # ---- Science (AAAS) ---------------------------------------------------
    ("Science",                         "Science", "https://feeds.science.org/rss/science.xml"),
    ("Science Advances",                "Science", "https://feeds.science.org/rss/science-advances.xml"),

    # ---- Cell Press -------------------------------------------------------
    ("Joule",                           "Cell",    "https://www.cell.com/joule/rss"),
    ("Matter",                          "Cell",    "https://www.cell.com/matter/rss"),
]

# ---------------------------------------------------------------------------
# 2. KEYWORDS
# A paper is kept if its title or abstract contains any of these terms
# (case-insensitive, whole-word / phrase match). Edit freely.
#
# Grouped only for readability -- matching treats them as one flat list.
# ---------------------------------------------------------------------------
KEYWORDS = [
    # --- core material ---
    "perovskite", "perovskites",
    "halide perovskite", "metal halide perovskite",
    # --- device / architecture ---
    "perovskite solar cell", "perovskite solar cells",
    "solar cell", "solar cells", "photovoltaic", "photovoltaics",
    "tandem", "multijunction", "multi-junction", "triple-junction", "triple junction",
    "wide-bandgap", "wide bandgap", "narrow-bandgap", "narrow bandgap",
    "single-junction", "single junction",
    # --- loss / stability physics ---
    "ion migration", "halide segregation", "phase segregation",
    "open-circuit voltage", "non-radiative recombination",
    # --- applications ---
    "water-splitting", "water splitting", "photoelectrochemical",
    "indoor pv", "indoor photovoltaic",
    "co2 reduction", "co2 electroreduction",
    "x-ray detection", "x-ray detector", "scintillator", "radiation detector",
    "photodetector",
    "light-emitting", "perovskite led",
]

# ---------------------------------------------------------------------------
# 3. SETTINGS
# ---------------------------------------------------------------------------
SETTINGS = {
    # If True, a paper must ALSO mention a perovskite-family term to be kept.
    # This sharply cuts noise from broad journals (Nature, Science, JACS,
    # Angewandte) where "tandem", "photovoltaic" or "CO2 reduction" alone
    # would pull in silicon, organic, or biology papers.
    # If False, ANY keyword above is enough (maximum recall).
    "require_perovskite": False,

    # Terms that count as "perovskite-family" for the option above.
    "perovskite_terms": ["perovskite", "perovskites"],

    # Keep every paper published on/after this date. Nothing is auto-deleted.
    # This is also the date the historical backfill (backfill.py) reaches back to.
    "start_date": "2009-01-01",

    # Crossref is used for the one-time historical backfill. Putting a real email
    # here joins Crossref's faster "polite pool" and is good etiquette. Optional.
    "crossref_mailto": "wangjunke9112@gmail.com",

    # Max characters of abstract to store per paper (keeps the data files small).
    "abstract_max_chars": 1600,

    # Network politeness.
    "request_timeout": 30,       # seconds per feed
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "GroupLitFeed/1.0 (research group RSS aggregator)"
    ),
}


# ---------------------------------------------------------------------------
# 4. ISSNs  (used ONLY by the historical backfill, backfill.py)
# Each journal maps to its ISSN(s). Crossref matches any of them. Both print
# and electronic ISSNs are listed where known, for maximum coverage.
# Run `python backfill.py --verify-issns` to confirm each one resolves to the
# right journal before doing the full backfill.
# ---------------------------------------------------------------------------
ISSNS = {
    "Nature":                                   ["0028-0836", "1476-4687"],
    "Nature Energy":                            ["2058-7546"],
    "Nature Materials":                         ["1476-1122", "1476-4660"],
    "Nature Chemistry":                         ["1755-4330", "1755-4349"],
    "Nature Synthesis":                         ["2731-0582"],
    "Nature Sustainability":                    ["2398-9629"],
    "Nature Photonics":                         ["1749-4885", "1749-4893"],
    "Nature Nanotechnology":                    ["1748-3387", "1748-3395"],
    "Nature Communications":                    ["2041-1723"],
    "Nature Electronics":                       ["2520-1131"],
    "Nature Methods":                           ["1548-7091", "1548-7105"],
    "Nature Reviews Materials":                 ["2058-8437"],
    "Nature Reviews Methods Primers":           ["2662-8449"],
    "Nature Physics":                           ["1745-2473", "1745-2481"],
    "Nature Reviews Chemistry":                 ["2397-3358"],
    "Journal of the American Chemical Society": ["0002-7863", "1520-5126"],
    "Chemical Reviews":                         ["0009-2665", "1520-6890"],
    "Accounts of Chemical Research":            ["0001-4842", "1520-4898"],
    "ACS Energy Letters":                       ["2380-8195"],
    "ACS Applied Materials & Interfaces":       ["1944-8244", "1944-8252"],
    "Chemical Society Reviews":                 ["0306-0012", "1460-4744"],
    "Energy & Environmental Science":           ["1754-5692", "1754-5706"],
    "Advanced Materials":                       ["0935-9648", "1521-4095"],
    "Advanced Energy Materials":                ["1614-6832", "1614-6840"],
    "Advanced Functional Materials":            ["1616-301X", "1616-3028"],
    "Angewandte Chemie Int. Ed.":               ["1433-7851", "1521-3773"],
    "Small":                                    ["1613-6810", "1613-6829"],
    "Small Methods":                            ["2366-9608"],
    "Science":                                  ["0036-8075", "1095-9203"],
    "Science Advances":                         ["2375-2548"],
    "Joule":                                    ["2542-4351", "2542-4785"],
    "Matter":                                   ["2590-2385", "2590-2393"],
}

# Seed terms the backfill sends to Crossref to surface candidate papers. Each
# result is then re-checked against the full KEYWORDS list above, so these only
# need to cast a wide net over the field (not match every keyword variant).
CORE_QUERIES = [
    "perovskite", "photovoltaic", "solar cell", "tandem",
    "photodetector", "water splitting", "CO2 reduction",
    "ion migration", "scintillator", "indoor photovoltaic",
]
