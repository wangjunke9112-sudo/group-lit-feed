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

    # ---- ACS --------------------------------------------------------------
    ("Journal of the American Chemical Society", "ACS", "https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc=jacsat"),
    ("Chemical Reviews",                "ACS",     "https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc=chreay"),
    ("Accounts of Chemical Research",   "ACS",     "https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc=achre4"),
    ("ACS Energy Letters",              "ACS",     "https://pubs.acs.org/action/showFeed?type=etoc&feed=rss&jc=aelccp"),

    # ---- RSC --------------------------------------------------------------
    ("Chemical Society Reviews",        "RSC",     "https://feeds.rsc.org/rss/CS"),
    ("Energy & Environmental Science",  "RSC",     "https://feeds.rsc.org/rss/EE"),

    # ---- Wiley ------------------------------------------------------------
    ("Advanced Materials",              "Wiley",   "https://onlinelibrary.wiley.com/feed/15214095/most-recent"),
    ("Advanced Energy Materials",       "Wiley",   "https://onlinelibrary.wiley.com/feed/16146840/most-recent"),
    ("Advanced Functional Materials",   "Wiley",   "https://onlinelibrary.wiley.com/feed/16163028/most-recent"),
    ("Angewandte Chemie Int. Ed.",      "Wiley",   "https://onlinelibrary.wiley.com/feed/15213773/most-recent"),

    # ---- Science (AAAS) ---------------------------------------------------
    ("Science",                         "Science", "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science"),
    ("Science Advances",                "Science", "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv"),

    # ---- Cell Press -------------------------------------------------------
    ("Joule",                           "Cell",    "https://www.cell.com/joule/inpress.rss"),
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

    # Drop papers older than this many days from the archive.
    "days_to_keep": 60,

    # Max characters of abstract to store per paper (keeps papers.json small).
    "abstract_max_chars": 1600,

    # Network politeness.
    "request_timeout": 30,       # seconds per feed
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "GroupLitFeed/1.0 (research group RSS aggregator)"
    ),
}
