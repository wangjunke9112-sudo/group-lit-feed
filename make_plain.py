#!/usr/bin/env python3
"""
make_plain.py -- static, JavaScript-free views of the group literature archive.

WHY
    index.html renders papers via JavaScript at runtime. Anything that reads the
    page without executing JS (an AI assistant, a text client, some crawlers)
    sees an empty shell. This script writes the same archive as plain HTML.

DESIGN
    Standalone: reads data/papers-*.json, writes plain*.html only. It never
    touches index.html, aggregate.py, feeds.py or any data file, so the existing
    site is unaffected. Delete the script and plain*.html to revert.

    Slices are QUESTION-driven, not component-driven. A component axis
    ("SAM", "TCO") silently steers reading toward incremental questions -- make
    a better SAM -- and those areas are already crowded. The axes below are
    built around the two open problems that actually need settling:

      A. Wide-bandgap loss: how much is EXTRINSIC (processing -> morphology,
         composition, crystallinity) versus INTRINSIC to the material?
      B. Integrated tandem stability: with many interfaces, WHICH one limits?
         Reachable by single-layer optimisation and by sub-cell selective
         stability testing.

    Slices use AND logic across concept groups, which keeps each page small and
    sharp. An OR-only filter on "stability" returned 11343 papers -- unreadable
    and useless for finding a gap.

    Within a page, papers are ranked by JOURNAL TIER first and date second, so
    truncation drops marginal work rather than landmark work.

USAGE
    python3 make_plain.py               # build all pages
    python3 make_plain.py --selftest    # offline tests
    python3 make_plain.py --max 400     # papers per page
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")

# --- concept vocabularies -------------------------------------------------
C = {
    "wbg": r"wide[- ]?bandgap|wide[- ]?gap|high[- ]?bandgap|"
           r"bromide[- ]rich|Br[- ]rich|mixed[- ]halide",
    "nbg": r"narrow[- ]?bandgap|low[- ]?bandgap|tin[- ]lead|Sn[- ]Pb|Pb[- ]Sn",
    "tandem": r"tandem|multijunction|multi[- ]junction|triple[- ]junction|"
              r"two[- ]terminal|2[- ]terminal|four[- ]terminal|sub[- ]?cell",
    "processing": r"crystalli[sz]ation|crystallinity|nucleation|grain growth|"
                  r"antisolvent|anti[- ]solvent|solvent engineering|annealing|"
                  r"morpholog|film formation|gas quench|blade|slot[- ]die|"
                  r"vacuum flash|precursor|intermediate phase|solvate|"
                  r"processing condition|deposition condition|scalable",
    "intrinsic": r"intrinsic|thermodynamic|miscibility gap|spinodal|"
                 r"phase diagram|formation energy|entrop|lattice strain|"
                 r"octahedral tilt|Goldschmidt|bond dissociation|"
                 r"first[- ]principles|\bDFT\b|ab initio",
    "segregation": r"halide segregation|phase segregation|photo[- ]?induced "
                   r"(?:halide )?(?:de)?mixing|Hoke effect|demixing|"
                   r"halide redistribution",
    "voc_loss": r"open[- ]circuit voltage|Voc (?:deficit|loss)|voltage loss|"
                r"quasi[- ]Fermi|QFLS|non[- ]radiative|radiative limit|"
                r"implied Voc|iVoc|photoluminescence quantum",
    "composition": r"composition(?:al)?\s*(?:gradient|inhomogen|variation|"
                   r"distribution|heterogen)|stoichiometr|halide (?:ratio|"
                   r"content)|A[- ]site|cation (?:ratio|composition)|"
                   r"depth profil",
    "hetero": r"heterogen|inhomogen|spatial variation|local (?:variation|"
              r"heterogen)|nanoscale variation|domain[- ]to[- ]domain|"
              r"grain[- ]to[- ]grain|microscop(?:y|ic) mapping",
    "stability": r"stabilit|degrad|operational (?:stabilit|lifetime)|"
                 r"\bT80\b|\bT90\b|light soaking|photostabilit|thermal stress|"
                 r"damp heat|ISOS|aging|ageing|lifetime",
    "interface": r"interface|interfacial|contact layer|buried interface|"
                 r"charge[- ]transport layer|\bETL\b|\bHTL\b|"
                 r"transport layer|surface recombination",
    "icl": r"interconnect|recombination (?:layer|junction)|tunnel(?:ling)? "
           r"junction|\bICL\b|charge recombination layer",
    "resolved": r"sub[- ]?cell|selective|resolved|attribut|decoupl|disentangl|"
                r"which (?:layer|interface|component)|origin of|dominant|"
                r"limiting|bottleneck|isolat",
    "operando": r"operando|in[- ]?situ|real[- ]?time|time[- ]resolved|"
                r"GIWAXS|GISAXS|synchrotron|tomograph|cross[- ]section|"
                r"depth[- ]resolved|hyperspectral|PL imaging|"
                r"photoluminescence imaging|EL imaging|lock[- ]in|"
                r"drift[- ]diffusion|impedance|transient",
    "ion": r"ion migration|ionic (?:transport|conduct|motion)|"
           r"mobile ion|halide vacanc|iodide migration|hysteresis|"
           r"pre[- ]conditioning|scan rate",
    "reverse": r"reverse bias|shading|partial shad|shunt|breakdown|"
               r"hot spot|reverse[- ]bias|bypass diode",
    "coupling": r"current match|current mismatch|luminescent coupling|"
                r"series[- ]connected|operating point|imbalance",
    # Domain anchor. Deliberately broader than "perovskite": many relevant
    # papers say "absorber", "wide-bandgap top cell" or "recombination junction"
    # without ever using the word. The upstream feed already restricts the
    # archive to this field, so a hard "perovskite" requirement only lost work.
    # --- fundamental end: formation, new matter, new architectures ---
    "formation": r"nucleation|crystal growth|crystalli[sz]ation pathway|"
                 r"formation mechanism|intermediate phase|solvate|colloid|"
                 r"precursor chemistry|sol[- ]gel|Ostwald|ripening|"
                 r"phase transition|polymorph|growth kinetic|self[- ]assembl",
    "newmat": r"lead[- ]free|Pb[- ]free|bismuth|antimony|silver bismuth|"
              r"double perovskite|chalcogenide perovskite|"
              r"low[- ]dimensional|2D perovskite|quasi[- ]2D|Ruddlesden|"
              r"Dion[- ]Jacobson|quantum dot|nanocrystal|"
              r"new (?:material|composition|absorber)|novel (?:material|"
              r"composition|semiconductor)|emerging (?:material|absorber)",
    "architecture": r"device (?:architecture|structure|design|layout)|"
                    r"inverted structure|p[- ]i[- ]n|n[- ]i[- ]p|"
                    r"back[- ]contact|interdigitated|bifacial|"
                    r"textured|light management|photon recycl|"
                    r"optical (?:design|engineering|coupling)|"
                    r"three[- ]terminal|3[- ]terminal|module architecture",
    "metrology": r"protocol|standard(?:i[sz]ed|isation|ization)?|"
                 r"round[- ]robin|inter[- ]laborator|reproducib|"
                 r"measurement (?:protocol|artefact|artifact|uncertaint)|"
                 r"certified (?:efficiency|measurement)|best practice|"
                 r"reporting (?:standard|guideline)|checklist|"
                 r"stabilised (?:power )?output|\bMPPT\b|maximum power point",
    "integration": r"self[- ]powered|\bIoT\b|internet of things|"
                   r"wearable|implantable|sensor node|"
                   r"integrated (?:circuit|chip|system|device)|"
                   r"on[- ]chip|monolithic integration|"
                   r"powering|autonomous (?:sensor|system)|"
                   r"energy[- ]autonomous",

    # --- application axes (for project types other than mechanism-led 面上) ---
    "detector": r"photodetector|photodiode|image sensor|X[- ]ray detect|"
                r"scintillat|radiation detect|photoconduct|responsivity|"
                r"detectivit|dark current",
    "led": r"light[- ]emitting|\bLED\b|\bLEDs\b|electroluminescen|"
           r"emission efficiency|\bEQE\b.{0,40}emit|colour purity|color purity|"
           r"luminescen|phosphor|down[- ]conversion",
    "indoor": r"indoor (?:photovolta|light|PV)|low[- ]light|ambient light|"
              r"artificial light|LED illuminat|dim light|IoT power|"
              r"energy harvest",
    "scaleup": r"module|large[- ]area|scalab|upscal|blade coat|slot[- ]die|"
               r"roll[- ]to[- ]roll|inkjet|spray|manufactur|throughput|"
               r"pilot line|mini[- ]module|cm2|square cent",
    "reliability": r"encapsulat|damp heat|thermal cycl|IEC 61215|IEC61215|"
                   r"outdoor|field test|accelerated (?:aging|ageing|test)|"
                   r"reliabilit|certif|bankabilit|degradation rate",
    "flexible": r"flexible|lightweight|bendab|foldab|substrate[- ]free|"
                r"plastic substrate|PET substrate|space (?:solar|photovolta)|"
                r"specific power|radiation hard",
    "pv": r"perovskite|halide|solar cell|photovolta|absorber|"
          r"top cell|bottom cell|photoactive|light[- ]harvest|"
          r"tandem|multijunction|multi[- ]junction|sub[- ]?cell|"
          r"thin[- ]film|optoelectronic|semiconductor|"
          r"\bPV\b|\bmodule\b|\bmodules\b|\bcell efficiency\b|"
          r"\bPCE\b|power conversion efficiency|\bLED\b|photodetector",
}


def _rx(*keys):
    return re.compile("|".join(C[k] for k in keys), re.I)


# ("slug", "Title", "purpose", [group1, group2, ...])  -- AND across groups
SLICES = [
    ("wbg-processing",
     "A1 · Wide-bandgap: processing-induced losses",
     "Does how we make the film (crystallisation, morphology, composition) set "
     "the loss? Evidence linking processing knobs to WBG performance.",
     [_rx("wbg"), _rx("processing"), _rx("pv")]),

    ("wbg-intrinsic",
     "A2 · Wide-bandgap: intrinsic / thermodynamic limits",
     "Evidence that the loss is inherent to the composition, not the recipe: "
     "miscibility, formation energetics, lattice strain, first-principles work.",
     [_rx("wbg"), _rx("intrinsic", "segregation"), _rx("pv")]),

    ("wbg-voc",
     "A3 · Wide-bandgap: Voc-loss attribution (bulk vs interface)",
     "Papers that try to LOCATE the voltage loss rather than merely reduce it: "
     "QFLS, PLQY, implied Voc, recombination partitioning.",
     [_rx("wbg"), _rx("voc_loss"), _rx("pv")]),

    ("wbg-heterogeneity",
     "A4 · Wide-bandgap: compositional / spatial heterogeneity",
     "Local composition and property variation: the bridge between "
     "'processing' and 'intrinsic', and a likely place a clean answer hides.",
     [_rx("wbg"), _rx("composition", "hetero"), _rx("pv")]),

    ("wbg-segregation",
     "A5 · Halide segregation: mechanism and suppression",
     "The canonical WBG instability: driving force, kinetics, reversibility, "
     "and what actually stops it under operation.",
     [_rx("segregation"), _rx("pv")]),

    ("tandem-interface-limit",
     "B1 · Tandem stability: which layer/interface is limiting?",
     "Work that ATTRIBUTES degradation to a specific interface rather than "
     "reporting an overall lifetime.",
     [_rx("tandem"), _rx("stability"), _rx("resolved", "interface")]),

    ("subcell-selective",
     "B2 · Sub-cell selective / resolved characterisation",
     "Methods that interrogate one junction inside a finished tandem: "
     "selective illumination, sub-cell EQE/PL, depth-resolved probes.",
     [_rx("tandem"), _rx("resolved", "operando")]),

    ("tandem-coupling",
     "B3 · Series coupling: mismatch, operating point, reverse bias",
     "Failure modes that exist ONLY because two cells are wired in series: "
     "current mismatch, load-point shift, shading, reverse bias.",
     [_rx("tandem"), _rx("coupling", "reverse")]),

    ("icl",
     "B4 · Interconnection / recombination layer",
     "The layer unique to monolithic tandems: design, transparency, "
     "sputter damage, and its role in degradation.",
     [_rx("icl"), _rx("pv")]),

    ("nbg-stability",
     "B5 · Narrow-bandgap (Sn-Pb) stability",
     "Sn(II) oxidation and the narrow-gap sub-cell as the usual suspect for "
     "tandem lifetime.",
     [_rx("nbg"), _rx("stability"), _rx("pv")]),

    ("operando",
     "C1 · Operando / in-situ characterisation",
     "Tools that watch films and devices while they form or while they "
     "degrade: the experimental basis for settling both questions.",
     [_rx("operando"), _rx("pv")]),

    ("ion-dynamics",
     "C2 · Ion migration and its device signatures",
     "Mobile ions as the shared mechanism behind hysteresis, metastability "
     "and slow degradation.",
     [_rx("ion"), _rx("pv")]),

    ("stability-mechanism",
     "C3 · Degradation pathways (mechanism-level)",
     "Papers naming a pathway and a rate-limiting step, not just reporting T80.",
     [_rx("stability"), _rx("resolved", "intrinsic"), _rx("pv")]),

    # ---- D: application axes -------------------------------------------
    # These do not serve the mechanism-led 面上 line. They exist because other
    # funding types (校企合作, 重点研发, 市级产学研, 国际合作) reward device
    # applications and manufacturability rather than mechanism.
    ("app-detector",
     "D1 · Photodetectors and X-ray / radiation detection",
     "Tandem and multilayer halide devices used as detectors. Note SKLLM "
     "already has Ce-halide radiation-detection work, so this is the most "
     "natural in-house collaboration axis.",
     [_rx("detector"), _rx("pv")]),

    ("app-led",
     "D2 · Light emission: LEDs and luminescent devices",
     "The reverse-biased twin of a solar cell, and the lab's institutional "
     "strength (OLED / luminescent materials). Strong fit for joint work "
     "inside SKLLM rather than as a solo line.",
     [_rx("led"), _rx("pv")]),

    ("app-indoor",
     "D3 · Indoor / low-light photovoltaics",
     "Wide-bandgap absorbers are natural indoor harvesters, which turns the "
     "A-axis weakness (large gap) into a strength. Short path to an IoT-facing "
     "industry project.",
     [_rx("indoor"), _rx("pv")]),

    ("app-scaleup",
     "D4 · Scale-up: modules, coating, manufacturability",
     "Large-area processing and module integration. This is what an industry "
     "partner will actually ask about.",
     [_rx("scaleup"), _rx("pv")]),

    ("app-reliability",
     "D5 · Reliability, encapsulation and certification",
     "Damp heat, thermal cycling, IEC protocols, outdoor data. The language "
     "of company partners and applied-programme reviewers.",
     [_rx("reliability"), _rx("pv")]),

    ("app-flexible",
     "D6 · Flexible, lightweight and space photovoltaics",
     "Specific-power-driven applications where perovskite tandems beat silicon "
     "on a metric other than efficiency.",
     [_rx("flexible"), _rx("pv")]),

    # ---- E: the fundamental end, and platform-level novelty --------------
    # These support proposals that are NOT loss-mechanism stories: new matter,
    # new architectures, new measurement standards, new device platforms.
    ("fundamental-formation",
     "E1 · Film formation: nucleation, growth, phase evolution",
     "The fundamental chemistry of how the solid forms. Underpins the A-axis "
     "extrinsic/intrinsic question, but stands alone as a basic-science line.",
     [_rx("formation"), _rx("pv")]),

    ("new-materials",
     "E2 · New compositions and material families",
     "Lead-free, double perovskites, chalcogenide perovskites, low-dimensional "
     "and nanocrystal systems. The route to a genuinely new absorber rather "
     "than a better recipe.",
     [_rx("newmat"), _rx("pv")]),

    ("architecture",
     "E3 · Device architectures and optical design",
     "Layouts rather than layers: back-contact, three-terminal, bifacial, "
     "textured light management, photon recycling.",
     [_rx("architecture"), _rx("pv")]),

    ("metrology",
     "E4 · Measurement protocols, standards, reproducibility",
     "How the field measures itself. A credible route to a methods-led project "
     "or a community-facing contribution, and unusually citable.",
     [_rx("metrology"), _rx("pv")]),

    ("integration",
     "E5 · Self-powered systems, IoT and on-chip integration",
     "Where the cell stops being the product and becomes a component. Natural "
     "language for industry partners and applied programmes.",
     [_rx("integration"), _rx("pv")]),
]

TIERS = [
    ["nature", "science"],
    ["nature energy", "nature materials", "joule",
     "energy & environmental science", "energy and environmental science"],
    ["nature photonics", "nature nanotechnology", "nature communications",
     "science advances", "nature chemistry", "nature synthesis",
     "nature reviews", "nature electronics", "nature physics",
     "nature sustainability", "matter"],
    ["journal of the american chemical society", "angewandte",
     "advanced materials", "advanced energy materials", "chemical reviews",
     "chemical society reviews", "accounts of chemical research",
     "acs energy letters"],
]

# Nothing is discarded. Each slice produces TWO pages:
#   plain-<slug>.html      recent years, full abstracts   (the reading page)
#   plain-<slug>-all.html  every match, one compact line   (the complete record)
# Truncating an archive defeats its purpose -- a 2023 foundational paper matters
# as much as last month's. Recency gets priority of PLACEMENT, not of existence.
RECENT_YEARS = 3          # how far back the detailed page reaches
ABSTRACT_CHARS = 1600
COMPACT_CHUNK = 2500      # entries per page in the complete list before paging


def tier(journal):
    j = (journal or "").strip().lower()
    for i, names in enumerate(TIERS):
        if i == 0:
            if j in names:
                return 0
        elif any(n in j for n in names):
            return i
    return len(TIERS)


def is_review(p):
    return (p.get("type") or "").lower() == "review"


def load_papers(data_dir=DATA_DIR):
    papers = []
    if not os.path.isdir(data_dir):
        return papers
    for fn in sorted(os.listdir(data_dir)):
        if not re.fullmatch(r"papers-\d{4}\.json", fn):
            continue
        try:
            with open(os.path.join(data_dir, fn), "r", encoding="utf-8") as fh:
                papers.extend(json.load(fh).get("papers", []))
        except Exception as exc:
            print(f"  ! skipping {fn}: {type(exc).__name__}: {exc}")
    return papers


def hay(p):
    return (p.get("title") or "") + " \n " + (p.get("abstract") or "")


def matches(p, groups):
    """AND across groups: every concept group must appear somewhere."""
    text = hay(p)
    return all(g.search(text) for g in groups)


def _negdate(d):
    try:
        y, m, day = (d or "0000-01-01")[:10].split("-")
        return -(int(y) * 10000 + int(m) * 100 + int(day))
    except Exception:
        return 0


def rank(p):
    """Journal tier first, then newest. Landmark work stays at the top."""
    return (tier(p.get("journal")), _negdate(p.get("date", "")))


def esc(s):
    return html.escape(str(s or ""), quote=True)


CSS = ("body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
       "sans-serif;font-size:15px;line-height:1.6;color:#15181d;background:#fff;"
       "max-width:920px;margin:0 auto;padding:24px}"
       "h1{font-size:21px;color:#003A6A;margin:0 0 4px}"
       "h2{font-size:15px;color:#003A6A;margin:30px 0 6px;"
       "border-bottom:2px solid #e3e6ea;padding-bottom:5px}"
       ".sub{color:#5a626e;font-size:12px;font-family:ui-monospace,Menlo,monospace;"
       "margin-bottom:6px}"
       ".purpose{color:#5a626e;font-size:13.5px;margin:0 0 16px;font-style:italic}"
       ".nav{margin:14px 0 22px;padding:12px 14px;background:#f7f8fa;"
       "border:1px solid #e3e6ea;border-radius:8px;font-size:13.5px}"
       ".nav a{display:inline-block;margin:3px 12px 3px 0;color:#003A6A}"
       "article{border-bottom:1px solid #eef0f3;padding:13px 0}"
       "article h3{font-size:14.5px;font-weight:600;margin:0 0 5px;line-height:1.4}"
       "article h3 a{color:#15181d;text-decoration:none}"
       "article h3 a:hover{color:#003A6A;text-decoration:underline}"
       ".meta{font-family:ui-monospace,Menlo,monospace;font-size:11px;"
       "color:#8b929c;margin-bottom:5px}"
       ".rev{background:#e6f3ec;color:#0a7d54;padding:1px 5px;border-radius:3px;"
       "font-weight:700}"
       ".auth{font-size:12.5px;color:#5a626e;margin-bottom:5px}"
       ".abs{font-size:13.5px;color:#2b313b}"
       ".cmp{padding:4px 0;border-bottom:1px solid #f4f5f7;font-size:13.5px;"
       "line-height:1.5}"
       ".cmp a{color:#15181d;text-decoration:none}"
       ".cmp a:hover{color:#003A6A;text-decoration:underline}"
       ".cmp .meta{display:inline;margin:0 0 0 6px}"
       ".yearnav{font-family:ui-monospace,Menlo,monospace;font-size:12px;"
       "line-height:2;color:#8b929c}"
       ".yearnav a{margin-right:4px}"
       "footer{margin-top:36px;padding-top:14px;border-top:1px solid #e3e6ea;"
       "font-size:12px;color:#8b929c}")


def page(title, purpose, body, generated, back=True):
    nav = ('<div class="nav"><a href="plain.html">&larr; All slices</a> · '
           '<a href="index.html">Interactive feed</a></div>') if back else ""
    return ('<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex">\n'
            f'<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n'
            f'<h1>{esc(title)}</h1>\n'
            f'<div class="sub">Static snapshot · generated {esc(generated)} · '
            'no JavaScript</div>\n'
            + (f'<p class="purpose">{esc(purpose)}</p>\n' if purpose else "")
            + f'{nav}{body}\n'
            '<footer>LENS Laboratory · generated by make_plain.py from '
            'data/papers-*.json. Read-only snapshot; use index.html to search.'
            '</footer>\n</body>\n</html>\n')


def render(p):
    ab = (p.get("abstract") or "").strip()
    if len(ab) > ABSTRACT_CHARS:
        ab = ab[:ABSTRACT_CHARS].rsplit(" ", 1)[0] + "…"
    authors = ", ".join(p.get("authors") or [])[:280]
    bits = [p.get("date", ""), p.get("journal", "")]
    doi = p.get("doi", "")
    if doi and doi.startswith("10."):
        bits.append("DOI " + doi)
    meta = esc(" · ".join(b for b in bits if b))
    if is_review(p):
        meta = '<span class="rev">REVIEW</span> ' + meta
    return ("<article>\n"
            f'<h3><a href="{esc(p.get("link",""))}" rel="noopener">'
            f'{esc(p.get("title",""))}</a></h3>\n'
            f'<div class="meta">{meta}</div>\n'
            + (f'<div class="auth">{esc(authors)}</div>\n' if authors else "")
            + (f'<div class="abs">{esc(ab)}</div>\n' if ab else "")
            + "</article>")


def render_compact(p):
    """One line per paper: enough to identify and fetch it, ~6x smaller than a
    full entry, so the complete archive stays servable."""
    bits = [p.get("date", "")[:7], p.get("journal", "")]
    doi = p.get("doi", "")
    if doi and doi.startswith("10."):
        bits.append(doi)
    tag = '<span class="rev">R</span> ' if is_review(p) else ""
    return (f'<div class="cmp">{tag}'
            f'<a href="{esc(p.get("link",""))}" rel="noopener">'
            f'{esc(p.get("title",""))}</a> '
            f'<span class="meta">{esc(" · ".join(b for b in bits if b))}</span></div>')


def _recent_cutoff(years):
    return (dt.date.today() - dt.timedelta(days=int(years * 365.25))).isoformat()


def build(recent_years=RECENT_YEARS, data_dir=DATA_DIR, out_dir=None, verbose=True):
    out_dir = out_dir or os.path.dirname(os.path.abspath(data_dir))
    papers = load_papers(data_dir)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not papers:
        print("! no data/papers-*.json found -- run from the lit-feed repo root")
        return []
    written, index_rows = [], []

    cutoff = _recent_cutoff(recent_years)
    for slug, title, purpose, groups in SLICES:
        hits = [p for p in papers if matches(p, groups)]
        hits.sort(key=rank)
        revs = [p for p in hits if is_review(p)]
        recent = [p for p in hits if (p.get("date") or "") >= cutoff]

        # (a) reading page: recent work in full
        body = (f'<p>{len(hits)} papers matched in total ({len(revs)} reviews). '
                f'Below: the {len(recent)} from the last {recent_years} years, '
                f'ranked by journal tier then date. '
                f'The complete list, including everything older, is on the '
                f'<a href="plain-{esc(slug)}-all.html">full archive page</a>'
                + (f' · <a href="plain-{esc(slug)}-reviews.html">reviews only</a>'
                   if revs else "") + '.</p>\n'
                + "\n".join(render(p) for p in recent))
        fn = os.path.join(out_dir, f"plain-{slug}.html")
        with open(fn, "w", encoding="utf-8") as fh:
            fh.write(page(title, purpose, body, generated))
        written.append(fn)

        # (b) complete archive, compact, paged if very large -- nothing dropped
        chunks = [hits[i:i + COMPACT_CHUNK]
                  for i in range(0, len(hits), COMPACT_CHUNK)] or [[]]
        for ci, chunk in enumerate(chunks):
            nav = ""
            if len(chunks) > 1:
                links = " · ".join(
                    (f'<b>{k+1}</b>' if k == ci else
                     f'<a href="plain-{esc(slug)}-all'
                     f'{"" if k == 0 else "-" + str(k+1)}.html">{k+1}</a>')
                    for k in range(len(chunks)))
                nav = f'<p>Page {ci+1} of {len(chunks)}: {links}</p>'
            cbody = (f'<p>Complete list for this slice: {len(hits)} papers, '
                     f'titles and identifiers only. Full abstracts for recent '
                     f'work are on the <a href="plain-{esc(slug)}.html">'
                     f'reading page</a>.</p>{nav}\n'
                     + "\n".join(render_compact(p) for p in chunk) + nav)
            suffix = "" if ci == 0 else f"-{ci+1}"
            cfn = os.path.join(out_dir, f"plain-{slug}-all{suffix}.html")
            with open(cfn, "w", encoding="utf-8") as fh:
                fh.write(page(title + " - complete archive", purpose, cbody, generated))
            written.append(cfn)

        # (c) reviews: open questions, stated by the authors themselves
        if revs:
            rbody = (f"<p>{len(revs)} reviews / perspectives in this slice, "
                     f"all years.</p>\n" + "\n".join(render(p) for p in revs))
            rfn = os.path.join(out_dir, f"plain-{slug}-reviews.html")
            with open(rfn, "w", encoding="utf-8") as fh:
                fh.write(page(title + " - reviews only",
                              "Reviews state open questions explicitly; this is "
                              "the fastest route to an unsolved problem.",
                              rbody, generated))
            written.append(rfn)
        index_rows.append((slug, title, purpose, len(hits), len(revs), len(recent)))
        if verbose:
            print(f"  {title:<52} {len(hits):>6} total · {len(recent):>5} recent"
                  f" · {len(revs):>4} rev")

    # ---------------- complete archive: EVERY paper, by year ----------------
    # The slices are lenses, not filters. A paper that matches no slice is still
    # part of the field and must remain reachable, or the archive quietly
    # becomes 25 keyword searches instead of a knowledge base.
    by_year = {}
    for p_ in papers:
        by_year.setdefault((p_.get("date") or "unknown")[:4] or "unknown", []).append(p_)
    year_keys = sorted((y for y in by_year if y.isdigit()), reverse=True)
    if "unknown" in by_year:
        year_keys.append("unknown")

    year_nav = " · ".join(f'<a href="plain-archive-{y}.html">{y}</a>'
                          for y in year_keys)
    for y in year_keys:
        items = sorted(by_year[y], key=rank)
        nrev = sum(1 for x in items if is_review(x))
        # split dense years so no single file becomes too large to read
        parts = [items[i:i + COMPACT_CHUNK]
                 for i in range(0, len(items), COMPACT_CHUNK)] or [[]]
        for pi, part in enumerate(parts):
            pnav = ""
            if len(parts) > 1:
                pnav = "<p>Part " + " · ".join(
                    (f"<b>{k+1}</b>" if k == pi else
                     f'<a href="plain-archive-{y}'
                     f'{"" if k == 0 else "-" + str(k+1)}.html">{k+1}</a>')
                    for k in range(len(parts))) + "</p>"
            cbody = (f'<p>{len(items)} papers dated {esc(y)} ({nrev} reviews), '
                     f'ranked by journal tier then date.</p>{pnav}'
                     f'<p class="yearnav">Years: {year_nav}</p>\n'
                     + "\n".join(render_compact(x) for x in part))
            suffix = "" if pi == 0 else f"-{pi+1}"
            yfn = os.path.join(out_dir, f"plain-archive-{y}{suffix}.html")
            with open(yfn, "w", encoding="utf-8") as fh:
                fh.write(page(f"Complete archive · {y}",
                              "Every paper in the archive for this year, "
                              "independent of any slice.", cbody, generated))
            written.append(yfn)

    # papers matching no slice at all -- the blind spot of the current lenses
    sliced_ids = set()
    for _slug, _t, _pu, _groups in SLICES:
        for x in papers:
            if matches(x, _groups):
                sliced_ids.add(id(x))
    unsliced = [x for x in papers if id(x) not in sliced_ids]
    unsliced.sort(key=rank)
    ubody = (f'<p>{len(unsliced)} of {len(papers)} papers match none of the '
             f'{len(SLICES)} slices. This page exists so they stay visible: a '
             f'cluster here means the slice definitions need widening, and it is '
             f'often where an unexpected direction shows up.</p>'
             f'<p class="yearnav">Years: {year_nav}</p>\n'
             + "\n".join(render_compact(x) for x in unsliced[:COMPACT_CHUNK * 4]))
    ufn = os.path.join(out_dir, "plain-unsliced.html")
    with open(ufn, "w", encoding="utf-8") as fh:
        fh.write(page("Unsliced papers", "Everything the current lenses miss.",
                      ubody, generated))
    written.append(ufn)

    rows = "\n".join(
        f'<article><h3><a href="plain-{esc(sl)}.html">{esc(t)}</a></h3>'
        f'<div class="meta">{rc} recent (last {recent_years} y) · '
        f'<a href="plain-{esc(sl)}-all.html">{n} total</a> · '
        + (f'<a href="plain-{esc(sl)}-reviews.html">{r} reviews</a>'
           if r else "no reviews")
        + f'</div><div class="abs">{esc(pu)}</div></article>'
        for sl, t, pu, n, r, rc in index_rows)
    body = ("<h2>Mechanism axes (A–C)</h2>\n"
            "<p>Built around two open problems rather than around device "
            "components:<br>"
            "<b>A.</b> In wide-bandgap absorbers, how much of the loss is "
            "processing-induced (morphology, composition, crystallinity) versus "
            "intrinsic to the material?<br>"
            "<b>B.</b> In an integrated tandem with many interfaces, which one "
            "limits stability, and how would we know?<br>"
            "<b>C.</b> The methods and mechanisms both questions depend on.</p>\n"
            "<p>Each slice has a <b>reading page</b> (recent years, full "
            "abstracts), a <b>complete archive</b> (every match, compact), and "
            "where available a <b>reviews-only</b> page. Nothing is discarded — "
            "recency governs placement, not inclusion.</p>\n"
            + rows
            + '\n<h2>Complete archive</h2>\n'
              '<p>The slices above are lenses. The archive itself is here in '
              'full, by year, so nothing is reachable only through a keyword '
              'that happened to be chosen well:</p>\n'
              f'<p class="yearnav">{year_nav}</p>\n'
              f'<p><a href="plain-unsliced.html">Papers matching no slice '
              f'({len(unsliced)})</a> — worth scanning when looking for a '
              f'direction the current axes do not cover.</p>\n'
            + f"\n<h2>Notes</h2>\n<p>Archive holds {len(papers)} papers. Each "
              f"slice requires ALL of its concept groups to appear (AND logic), "
              f"which is what keeps a slice readable. Within a page, papers are "
              f"ranked by journal tier then date. Slice definitions live in the "
              f"SLICES table of make_plain.py; adding one costs a single row.</p>")
    fn = os.path.join(out_dir, "plain.html")
    with open(fn, "w", encoding="utf-8") as fh:
        fh.write(page("LENS literature - static index", "", body, generated, back=False))
    written.append(fn)
    if verbose:
        print(f"\nindex -> plain.html ({len(papers)} papers in archive)")
    return written


def selftest():
    print("offline selftest...")
    S = {s: g for s, _, _, g in SLICES}

    wbg_proc = {"title": "Crystallisation control of bromide-rich wide-bandgap perovskite films",
                "abstract": "Antisolvent and annealing tune morphology and grain growth.",
                "journal": "Nature Energy", "date": "2026-01-01", "type": "article"}
    assert matches(wbg_proc, S["wbg-processing"])
    assert not matches(wbg_proc, S["tandem-coupling"])

    wbg_int = {"title": "Thermodynamic miscibility gap in mixed-halide perovskites",
               "abstract": "First-principles formation energy explains demixing.",
               "journal": "Nature Materials", "date": "2025-01-01", "type": "article"}
    assert matches(wbg_int, S["wbg-intrinsic"])

    sub = {"title": "Sub-cell resolved degradation in perovskite tandem solar cells",
           "abstract": "Selective illumination isolates each junction during aging.",
           "journal": "Joule", "date": "2026-02-01", "type": "article"}
    assert matches(sub, S["subcell-selective"])
    assert matches(sub, S["tandem-interface-limit"])

    rev = {"title": "Reverse bias and partial shading in monolithic tandem modules",
           "abstract": "Series connection drives one sub-cell into breakdown.",
           "journal": "Nature Energy", "date": "2026-03-01", "type": "article"}
    assert matches(rev, S["tandem-coupling"])

    off = {"title": "Onion farming yields in temperate climates",
           "abstract": "Nothing relevant here.", "journal": "Agri J",
           "date": "2026-01-01", "type": "article"}
    assert not any(matches(off, g) for g in S.values())

    # regression: a paper that never says "perovskite" must still be caught
    no_pk = {"title": "Sputter damage at the recombination junction of monolithic tandems",
             "abstract": "Interconnection layer transparency and damage trade-offs.",
             "journal": "Advanced Energy Materials", "date": "2026-01-01",
             "type": "article"}
    assert matches(no_pk, S["icl"]), "must not require the literal word perovskite"
    no_pk2 = {"title": "Nanoscale compositional heterogeneity in wide-bandgap absorbers",
              "abstract": "Hyperspectral PL imaging maps local halide ratio variation.",
              "journal": "Nature Communications", "date": "2026-01-01",
              "type": "article"}
    assert matches(no_pk2, S["wbg-heterogeneity"])

    mod = {"title": "Damp heat and thermal cycling reliability of encapsulated modules",
           "abstract": "IEC 61215 accelerated testing and outdoor degradation rate.",
           "journal": "Nature Energy", "date": "2026-01-01", "type": "article"}
    assert matches(mod, S["app-reliability"]), "module-level work must not be lost"

    only_stab = {"title": "Improved operational stability of a solar cell",
                 "abstract": "T80 extended to 1000 h in a perovskite device.",
                 "journal": "X", "date": "2026-01-01", "type": "article"}
    assert not matches(only_stab, S["tandem-interface-limit"])

    assert tier("Nature") == 0 and tier("Science") == 0
    assert tier("Nature Energy") == 1 and tier("Joule") == 1
    assert tier("Nature Communications") == 2
    assert tier("Advanced Materials") == 3
    assert tier("Some Local Journal") == len(TIERS)
    a = {"journal": "Nature Energy", "date": "2024-01-01"}
    b = {"journal": "Small", "date": "2026-06-01"}
    assert rank(a) < rank(b)
    c = {"journal": "Joule", "date": "2026-06-01"}
    d = {"journal": "Joule", "date": "2020-01-01"}
    assert rank(c) < rank(d)

    nasty = {"title": '<script>alert(1)</script> & co', "abstract": "a<b>",
             "link": "http://x?a=1&b=2", "authors": ["A B"], "date": "2026-01-01",
             "journal": "J", "doi": "10.1/x", "type": "review"}
    out = render(nasty)
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "REVIEW" in out

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        dd = os.path.join(td, "data")
        os.makedirs(dd)
        recs = [dict(x, link="http://x", authors=["A"], doi="10.1/a")
                for x in (wbg_proc, wbg_int, sub, rev, off)]
        recs.append(dict(wbg_int, title="Review of halide segregation mechanisms",
                         type="review"))
        with open(os.path.join(dd, "papers-2026.json"), "w", encoding="utf-8") as fh:
            json.dump({"papers": recs}, fh)
        files = build(data_dir=dd, out_dir=td, verbose=False)
        idx = open(os.path.join(td, "plain.html"), encoding="utf-8").read()
        for slug, _, _, _ in SLICES:
            assert f'href="plain-{slug}.html"' in idx, slug
        seg = open(os.path.join(td, "plain-wbg-segregation.html"), encoding="utf-8").read()
        assert 'href="plain.html"' in seg
    print("all selftests passed (AND logic, tiering, escaping, linking, build)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--years", type=int, default=RECENT_YEARS,
                    help="how many years the detailed reading page covers")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        print("building static pages from data/papers-*.json...")
        if not build(recent_years=a.years):
            sys.exit(1)
