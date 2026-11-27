#!/usr/bin/env python3
"""
make_plain.py -- static, JavaScript-free views of the group literature archive.

Built for two readers:
  * a person catching up on the field without time to read everything, and
  * an AI assistant assembling proposal context, which reads the TOP of each
    page first and can only follow links that appear as real <a href> tags.

So every page leads with what matters most (a landmark list, then the latest
papers), uses one compact line per paper with the paper's own key claim, and
links onward to further parts and to full abstracts.

WHAT IT WRITES (repo root, next to index.html)
  plain.html                 index: how to read, bundles, slices, archive
  plain-new.html             catch-up: halide-perovskite papers, last 45 days
  plain-<slice>.html         slice briefing: landmark / latest / reviews
  plain-<slice>-pN.html      the full slice, ranked, one line per paper
  plain-<slice>-absN.html    full abstracts for the top of the slice
  plain-b-<bundle>...        the same three views for a proposal bundle
  plain-archive-YYYY.html    every paper in the archive, by year
  plain-unsliced.html        perovskite PV papers that match no slice

PROPOSAL BUNDLES
  bundles.json (repo root) defines shareable views that combine slices and/or
  plain search phrases. Edit it in the GitHub web UI; no Python needed. A
  malformed file never breaks the daily run: the error is printed in the log
  and shown at the top of plain.html, and the built-in default bundles are
  used instead.

HOW A PAPER IS ASSIGNED TO A SLICE  (the fix for misclassification)
  1. Domain gate: it must be a halide-perovskite paper. "Tandem reaction",
     "NH3 oxidation selectivity" and oxide-perovskite catalysis are excluded.
  2. Device class: PV / LED / detector, read from the title first. A blue
     perovskite LED no longer lands in a photovoltaic voltage-loss slice.
  3. Background vs contribution: an abstract's opening sentences usually
     restate the field ("...promising for tandem applications"). Only the
     title and the sentences from "Here, we..." onward count as evidence of
     what the paper is ABOUT.
  4. Focus: the slice's defining concept must be in the title, or occur at
     least twice in the contribution part.
  Every paper also gets a PRIMARY slice (its best fit), shown with a star.

HOUSEKEEPING
  * Pages carry no build timestamp (only plain.html does), and files are
    rewritten only when their content changes -- so a quiet day commits
    almost nothing instead of ~100 MB of HTML.
  * plain*.html files that this run did not produce are deleted, so renamed
    or retired pages never linger as stale copies.

USAGE
  python3 make_plain.py              build everything (the daily workflow)
  python3 make_plain.py --selftest   offline tests
  python3 make_plain.py --stats      print slice sizes and samples, write nothing
"""

import argparse
import datetime as dt
import glob
import html
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
BUNDLES_FILE = os.path.join(HERE, "bundles.json")
SITE = "https://lens-laboratory.com/group-lit-feed/"

# ---------------------------------------------------------------- settings --
LANDMARK_N = 40          # briefing: top papers by journal tier x topical fit
LATEST_DAYS = 120        # briefing: "latest" window
LATEST_N = 40
REVIEWS_N = 20
PART_SIZE = 200          # one-line entries per database part
ABS_PART_SIZE = 50       # full-abstract entries per page
ABS_TOP_SLICE = 300      # abstracts kept for the top N of each slice
ABS_TOP_BUNDLE = 600     # ... and of each bundle
CATCHUP_DAYS = 45        # plain-new.html window
CLAIM_CHARS = 280        # length of the key-claim excerpt
ABSTRACT_CHARS = 2000
ARCHIVE_CHUNK = 2500     # compact lines per year-archive page
UNSLICED_MAX = 3000


# ================================================================ concepts ==
# Shared vocabularies. Each slice combines these; each is evaluated once per
# paper and cached, which keeps a 70k-paper archive fast.
# Deliberately absent: generic words that matched nearly everything in the
# previous version ("selective", "resolved", "dominant", "interface",
# "transient", plain "in situ" and plain "homogeneous").
C = {
    "wbg": r"wide[- ]?band[- ]?gaps?|wide[- ]gaps?|high[- ]band[- ]?gaps?|"
           r"large[- ]band[- ]?gaps?|bromide[- ]rich|Br[- ]rich|mixed[- ]halides?|"
           r"1\.[6-9]\d?[- ]?eV|2\.0\d?[- ]?eV|top[- ]?(?:sub[- ]?)?cells?|"
           r"CsPbI\s?2\s?Br|CsPbIBr\s?2|triple[- ]halides?|I/Br|"
           r"(?:bromide|Br) (?:content|ratio|fraction)",
    "nbg": r"narrow[- ]?band[- ]?gaps?|low[- ]band[- ]?gaps?|tin[- /]lead|Sn[- /]?Pb|"
           r"Pb[- /]?Sn|mixed (?:tin|Sn)[- /](?:lead|Pb)|ideal[- ]band[- ]?gap|"
           r"1\.(?:1[5-9]|2\d|3[0-4])\d?[- ]?eV",
    "tandem": r"tandems?|multi[- ]?junctions?|triple[- ]junctions?|"
              r"quadruple[- ]junctions?|(?:two|four|three|2|3|4)[- ]terminal|"
              r"\b[234]T\b|sub[- ]?cells?",
    "processing": r"crystalli[sz](?:ation|ed|ing|es?)\b|crystallinity|nucleation|"
                  r"grain[- ](?:growth|size)|anti[- ]?solvent|annealing|"
                  r"film[- ]formation|gas[- ](?:quench\w*|knife|assisted)|"
                  r"blade[- ]coat\w*|slot[- ]die|"
                  r"vacuum[- ](?:flash|deposit\w*|evaporat\w*|quench\w*)|"
                  r"co[- ]?evaporat\w*|thermal(?:ly)? evaporat\w*|"
                  r"vapou?r[- ](?:deposit\w*|phase|assisted)|"
                  r"precursor (?:engineering|chemistry|solutions?|inks?|aging|ageing)|"
                  r"intermediate phases?|solvent engineering|solvates?|"
                  r"processing (?:windows?|conditions?|routes?)|"
                  r"sequential deposition|two[- ]step",
    "intrinsic": r"intrinsic (?:limit\w*|loss\w*|instabilit\w*|origins?|nature|"
                 r"driving force|phase (?:instabilit\w*|behavio\w*))|"
                 r"thermodynamic\w*|miscibility|spinodal|"
                 r"phase diagrams?|formation energ\w*|entrop\w*|"
                 r"lattice (?:strain|distortion|mismatch|contraction)|"
                 r"octahedral (?:tilt\w*|distortion)|Goldschmidt|tolerance factor|"
                 r"first[- ]principles|density functional|\bDFT\b|ab initio|"
                 r"band ?gap bowing|defect (?:tolerance|physics|formation)",
    "segregation": r"(?:halide|phase|photo[- ]?induced|light[- ]induced) segregation|"
                   r"photo[- ]?segregation|demix\w*|\bHoke\b|halide redistribution|"
                   r"(?:photo|light|halide)[- ]?induced (?:de)?mixing|halide (?:de)?mixing|"
                   r"iodide[- ]rich (?:domains?|phases?|clusters?)",
    "voc": r"open[- ]circuit voltages?|\bV\s?OC\b|\bVoc\b|"
           r"(?:photo)?voltage (?:loss\w*|deficit\w*)|quasi[- ]Fermi|\bQFLS\b|"
           r"implied (?:V\s?OC|Voc|voltage)|\bi-?Voc\b|"
           r"non[- ]?radiative (?:recombination|loss\w*|voltage)|radiative limit|"
           r"\bPLQYs?\b|photoluminescence quantum (?:yield|efficienc\w*)|"
           r"energy loss\w*|\bE\s?loss\b",
    "hetero": r"heterogene\w*|inhomogene\w*|homogeni[sz]\w*|"
              r"homogeneous (?:halides?|halogens?|phases?|composition\w*|distribution\w*)|"
              r"(?:halide|halogen|composition\w*)[- ](?:phase[- ])?(?:uniform\w*|"
              r"homogene\w*|distribution\w*)|"
              r"uniform (?:halide|halogen|composition\w*|phase) distribution\w*|"
              r"spatial(?:ly)?[- ](?:resolved|variation\w*|distribution\w*|"
              r"map\w*|heterogene\w*)|"
              r"nanoscale (?:variation\w*|heterogene\w*|map\w*|imag\w*)|"
              r"local (?:variation\w*|composition\w*|band ?gaps?|strain)|"
              r"composition(?:al)? (?:gradient\w*|variation\w*|distribution\w*|"
              r"inhomogene\w*|heterogene\w*|disorder)|"
              r"halide (?:gradient\w*|distribution\w*)|"
              r"vertical(?:ly)? (?:gradient\w*|distribution\w*|segregat\w*|graded)|"
              r"grain[- ]to[- ]grain|hyperspectral|cathodoluminescence|"
              r"nano[- ]?XRF|X[- ]ray fluorescence|"
              r"(?:PL|photoluminescence|EL) (?:map\w*|imag\w*|microscop\w*)|"
              r"depth[- ](?:profil\w*|resolved)|(?:phase|compositional) impurit\w*",
    "stability": r"stabilit\w*|\bstable\b|degrad\w*|"
                 r"operational (?:stabilit\w*|lifetime)|\bT[89]0\b|lifetimes?|"
                 r"\baging\b|\bageing\b|damp[- ]heat|thermal (?:stress|cycl\w*)|"
                 r"light[- ]soaking|decompos\w*|durab\w*|\bISOS\b",
    "mechanism": r"degradation (?:mechanism\w*|pathway\w*|routes?|kinetic\w*|"
                 r"process\w*|chemistr\w*|modes?|origins?|products?)|"
                 r"decomposition (?:mechanism\w*|pathway\w*|products?|kinetic\w*)|"
                 r"failure (?:modes?|mechanism\w*|analys\w*)|post[- ]mortem|"
                 r"root causes?|origins? of (?:the )?(?:degradation|instabilit\w*)|"
                 r"(?:photo|electro|thermo)?chemical degradation|rate[- ]limiting|"
                 r"Arrhenius|lifetime (?:prediction|model\w*|extrapolat\w*)|"
                 r"accelerated (?:aging|ageing|test\w*)|"
                 r"iodine (?:release|loss|formation|vapou?r)|"
                 r"(?:metal|gold|silver|Au|Ag) (?:diffusion|migration)|"
                 r"deprotonat\w*|volatili[sz]\w*",
    "attrib": r"attribut\w*|disentangl\w*|decoupl\w*|deconvolut\w*|"
              r"quantif\w*|partition\w*|pinpoint\w*|"
              r"which (?:layer|interface|sub[- ]?cell|component)|"
              r"limit(?:s|ing)? (?:the )?(?:stability|lifetime|V\s?OC|voltage)|"
              r"bottlenecks?|dominant (?:loss|degradation|recombination)|"
              r"origins? of",
    "subcell": r"sub[- ]?cell[- ](?:resolved|selective|specific|level|limited|"
               r"characteri[sz]\w*)|"
               r"(?:each|individual|separate|isolated) sub[- ]?cells?|"
               r"selective(?:ly)? (?:illuminat\w*|excit\w*|prob\w*|bias\w*)|"
               r"light[- ]bias(?:ed|ing)?|bias light|luminescent coupling|"
               r"spectrally (?:resolved|selective)|reciprocity|"
               r"current[- ]limiting sub[- ]?cell",
    "coupling": r"current[- ]match\w*|current[- ]mismatch\w*|luminescent coupling|"
                r"reverse[- ]bias\w*|partial(?:ly)? shad\w*|shading|"
                r"(?:reverse|electrical) breakdown|hot[- ]spots?|bypass diodes?|"
                r"operating point|energy yield|"
                r"spectral (?:variation\w*|mismatch|changes?)|outdoor|real[- ]world",
    "icl": r"interconnect\w*|recombination (?:layers?|junctions?|contacts?)|"
           r"tunnel(?:l?ing)? (?:junctions?|recombination)|\bICLs?\b|"
           r"charge[- ]recombination layers?|"
           r"intermediate (?:electrodes?|layers?|contacts?)|"
           r"sputter(?:ed|ing)?[- ]?(?:damage|induced)",
    "operando": r"operando|"
                r"in[- ]?situ (?:X[- ]ray|XRD|GIWAXS|diffraction|scattering|PL\b|"
                r"photoluminescence|absorption|UV|spectroscop\w*|monitor\w*|"
                r"observ\w*|measure\w*|characteri[sz]\w*|imaging|microscop\w*|"
                r"TEM|SEM|AFM|Raman|FTIR|stud\w*|track\w*)|"
                r"real[- ]time (?:monitor\w*|observ\w*|track\w*|imaging|measure\w*)|"
                r"time[- ]resolved (?:X[- ]ray|XRD|GIWAXS|diffraction|scattering|"
                r"photoluminescence|PL\b|spectroscop\w*|imaging)|"
                r"\bGIWAXS\b|\bGISAXS\b|\bWAXS\b|synchrotron|"
                r"under (?:operation|operating conditions|bias and illumination|"
                r"illumination and bias)|"
                r"(?:PL|EL|photoluminescence|electroluminescence) imaging|"
                r"lock[- ]in thermograph\w*|tomograph\w*|cryo[- ]?(?:EM|TEM|electron)",
    "ion": r"ion(?:ic)? migration|ionic (?:transport|conduct\w*|motion|defects?|"
           r"charges?|currents?)|mobile ions?|"
           r"ion (?:density|diffusion|accumulation|redistribution|movement)|"
           r"halide (?:vacanc\w*|migration|diffusion)|"
           r"iodide (?:migration|diffusion|interstitial\w*)|vacancy migration|"
           r"hysteresis (?:analys\w*|mechanism\w*|origins?|behavio\w*|dynamic\w*|"
           r"model\w*|effects?)|origins? of (?:the )?hysteresis|anomalous hysteresis|"
           r"metastab\w*|scan[- ]rate dependen\w*|"
           r"transient (?:ion|current|capacitance|photovoltage|photocurrent)|"
           r"impedance spectroscop\w*|drift[- ]diffusion",
    "indoor": r"indoor|low[- ]light|ambient light|dim[- ]light|artificial light|"
              r"(?:LED|fluorescent) (?:illumination|light(?:ing)?)|\d+\s?lux|\blux\b|"
              r"internet of things|\bIoT\b",
    "scaleup": r"\bmodules?\b|mini[- ]?modules?|large[- ]area|scalab\w*|"
               r"scale[- ]up|upscal\w*|blade[- ]coat\w*|slot[- ]die|"
               r"roll[- ]to[- ]roll|\bR2R\b|inkjet|spray[- ]coat\w*|meniscus|"
               r"manufactur\w*|industrial|mass production|aperture area|"
               r"commerciali[sz]\w*",
    "reliability": r"encapsulat\w*|damp[- ]heat|thermal cycl\w*|\bIEC\s?6\d+|"
                   r"outdoor|field (?:test\w*|data|stud\w*|deployment)|"
                   r"accelerated (?:aging|ageing|test\w*|stress)|reliabilit\w*|"
                   r"certif\w*|bankab\w*|degradation rates?|\bISOS\b|"
                   r"85\s?°?\s?C\b|humidity[- ]freeze|"
                   r"UV (?:stability|exposure|precondition\w*)",
    "flexible": r"flexible|lightweight|bendab\w*|foldab\w*|stretchab\w*|"
                r"ultra[- ]?thin|"
                r"space (?:applications?|power|environments?|photovolta\w*|solar)|"
                r"\bAM0\b|radiation (?:hard\w*|toleran\w*|resistan\w*|stability)|"
                r"(?:proton|electron|gamma)[- ]?(?:ray )?irradiat\w*|specific power|"
                r"power[- ]per[- ]weight|aerospace|satellites?|near[- ]space",
    "formation": r"nucleation|crystal growth|"
                 r"crystalli[sz]ation (?:pathways?|kinetic\w*|mechanism\w*|"
                 r"dynamic\w*|process\w*|behavio\w*)|intermediate phases?|"
                 r"solvates?|colloid\w*|precursor chemistry|Ostwald|"
                 r"phase transitions?|polymorph\w*|growth kinetic\w*|"
                 r"film formation|formation (?:mechanism|pathway|dynamics|kinetics)",
    "newmat": r"lead[- ]free|\bPb[- ]free|tin[- ]based|"
              r"(?:pure )?tin (?:perovskites?|halides?)|"
              r"(?:bismuth|antimony|silver[- ]bismuth|copper)[- ](?:based|halides?|perovskites?)|"
              r"(?:Bi|Sb)[- ]based|(?:Cs|MA|FA)\s?3\s?(?:Bi|Sb)\s?2|perovskite[- ]inspired|"
              r"double perovskites?|chalcogenide perovskites?|"
              r"(?<![/\w])(?:2D|quasi[- ]2D|Ruddlesden[- ]Popper|Dion[- ]Jacobson) perovskite "
              r"(?:solar cells?|absorbers?|photovoltaics?)|"
              r"new (?:A|B|X)[- ]site|novel (?:cations?|compositions?|absorbers?)|"
              r"high[- ]entropy|vacancy[- ]ordered|germanium",
    "architecture": r"back[- ]contact\w*|interdigitated|\bIBC\b|three[- ]terminal|"
                    r"\b3T\b|bifacial|semi[- ]?transparent|textur\w*|"
                    r"light (?:management|trapping)|photon (?:recycling|management)|"
                    r"optical (?:design|simulation\w*|model\w*|loss\w*|coupling)|"
                    r"transfer[- ]matrix|nanophotonic\w*|anti[- ]?reflect\w*|"
                    r"device (?:architectures?|structures?|design|layouts?|configurations?)|"
                    r"electrode[- ]free|carbon electrodes?|transparent electrodes?",
    "metrology": r"(?:testing|test|measurement|stability|characteri[sz]ation|reporting|"
                 r"aging|ageing|ISOS) protocols?|protocols? for|standardi[sz]\w*|"
                 r"round[- ]robin|inter[- ]?laborator\w*|"
                 r"reproducibility (?:of|across|between|study|analys\w*|crisis)|"
                 r"lab[- ]to[- ]lab|consensus statement|"
                 r"measurement (?:artefacts?|artifacts?|errors?|uncertaint\w*|"
                 r"best practices?|protocols?|accuracy)|"
                 r"reporting (?:standards?|guidelines?|practices?)|checklists?|"
                 r"stabili[sz]ed power output (?:measurement|protocol)\w*|"
                 r"maximum power point tracking|spectral mismatch|"
                 r"calibrat(?:ion|ed) (?:of|procedure|reference|cells?)|best practices?",
    "integration": r"self[- ]powered|internet of things|\bIoT\b|wearable\w*|"
                   r"implantable|sensor nodes?|"
                   r"integrated (?:devices?|systems?|circuits?|power)|on[- ]chip|"
                   r"energy[- ]autonomous|autonomous (?:sensors?|devices?|power|"
                   r"electronics|wearables?|systems? powered)|photo[- ]?rechargeable|"
                   r"photo[- ]?charging|power(?:ing)? (?:sensors?|electronics|devices)|"
                   r"electronic skin|smart (?:windows?|glass|textiles?)",
    "passivation": r"passivat\w*|defect (?:healing|suppression|management|engineering)|"
                   r"trap[- ](?:states?|densit\w*|passivat\w*|assisted)|grain[- ]boundar\w*|"
                   r"surface (?:reconstruction|treatment|modification|termination|"
                   r"defects?|chemistry)|buried interfaces?|"
                   r"interfac(?:e|ial) (?:engineering|modification|passivation|"
                   r"defects?|recombination|dipoles?|chemistry)|"
                   r"2D/3D|3D/2D|heterojunctions?|dipoles?",
    "contacts": r"self[- ]assembled monolayers?|\bSAMs?\b|\w*PACz|"
                r"hole[- ](?:transport\w*|selective|extraction|contacts?)|"
                r"electron[- ](?:transport\w*|selective|extraction|contacts?)|"
                r"\bHT[LM]s?\b|\bET[LM]s?\b|"
                r"charge[- ](?:transport|extraction|selective) (?:layers?|materials?|contacts?)|"
                r"spiro[- ]?OMeTAD|\bPTAA\b|\bNiO\s?[xX]?\b|\bSnO\s?2\b|"
                r"\bTiO\s?2\b|\bC60\b|fullerenes?|\bPCBM\b|"
                r"transparent (?:conductive|conducting) (?:oxides?|electrodes?)|"
                r"back electrodes?|metal electrodes?|carbon electrodes?",
    "leadsafe": r"lead (?:leakage|sequestration|toxicity|management|capture|adsorption|"
                r"release|immobili[sz]\w*|contamination)|Pb (?:leakage|sequestration|release)|"
                r"recycl\w*|life[- ]cycle|\bLCA\b|toxicit\w*|"
                r"environmental (?:impact\w*|risk\w*|footprint)|end[- ]of[- ]life|"
                r"circular econom\w*|green solvents?|non[- ]?toxic solvents?|"
                r"carbon footprint|energy payback",
    "fuels": r"water splitting|solar[- ](?:driven|to[- ]hydrogen|fuels?)|\bSTH\b|"
             r"photoelectrochemical|photo[- ]?electrodes?|photo[- ]?cathodes?|"
             r"photo[- ]?anodes?|\bPV[- ]EC\b|PV[- ]electroly\w*|"
             r"electroly(?:sis|zers?|sers?)|unassisted|bias[- ]free|"
             r"hydrogen (?:production|evolution|generation)|"
             r"CO2 (?:reduction|conversion|electroreduction)|"
             r"artificial (?:leaf|photosynthesis)|ammonia (?:synthesis|production)",
}
CX = {k: re.compile(v, re.I) for k, v in C.items()}

# ---- domain and device class --------------------------------------------
PK_RX = re.compile(r"perovskite", re.I)
HALIDE_RX = re.compile(
    r"halides?|iodides?|bromides?|"
    r"lead[- ](?:halide|iodide|bromide|based|free|tin)|tin[- ](?:halide|based|lead)|"
    r"organometal\w*|organic[- ]inorganic|hybrid perovskites?|"
    r"methylammonium|formamidinium|guanidinium|"
    r"perovskite (?:solar|photovolta\w*|light[- ]emitting|LEDs?|nanocrystals?|"
    r"quantum dots?|photodetectors?|detectors?|scintillators?|single crystals?)",
    re.I)
# Case-sensitive on purpose: "SnI" with re.I would match "snippet". OpenAlex
# often writes subscripts with spaces ("CsPbBr 3", "MA 3 Bi 2 Br 9").
FORMULA_RX = re.compile(
    r"(?:Pb|Sn|Ge)\s?\d?\s?(?:I|Br|Cl)|(?:Cs|FA|MA|GA|Rb)\s?\d?\s?(?:Pb|Sn|Bi|Sb|Ge)|"
    r"(?:FA|MA|Cs)\s?\d|PbX|(?:Bi|Sb)\s?2\s?(?:I|Br|Cl)")
OXIDE_RX = re.compile(
    r"perovskite oxides?|oxide perovskites?|perovskite[- ]type oxides?|"
    r"solid oxide|oxygen (?:evolution|reduction|vacanc\w*)|"
    r"ferroelectric oxides?|multiferroic|cathode materials?|"
    r"magnetoresist\w*|superconduct\w*|piezoelectric", re.I)
OXIDE_FORMULA_RX = re.compile(   # case-sensitive: BiFeO3, SrTiO 3, La0.6Sr0.4CoO3
    r"\b(?:La|Sr|Ba|Ca|Bi|Pb|Na|K|Ni|Co|Fe|Mn|Ti|Nd|Pr|Y|Li)[\d.x]*\s?"
    r"(?:[A-Z][a-z]?\s?[\d.x]*\s?){0,4}O\s?[3δ]")
PV_RX = re.compile(
    r"solar cells?|photovoltaic\w*|\bPVs?\b|solar modules?|\bPSCs?\b|"
    r"\bPCEs?\b|power conversion efficienc\w*|solar energy conversion", re.I)
LED_RX = re.compile(
    r"light[- ]emitting|\bLEDs?\b|\bPeLEDs?\b|electroluminescen\w*|"
    r"\blas(?:ing|ers?)\b|\bemitters?\b|\bdisplays?\b", re.I)
DET_RX = re.compile(
    r"photo[- ]?detect\w*|photodiodes?|X[- ]ray (?:detect\w*|imag\w*)|"
    r"\bdetectors?\b|scintillat\w*|radiation detect\w*|gamma[- ]ray|"
    r"photoconduct\w*|image sensors?|\bimagers?\b|photosensor\w*", re.I)

# ---- background vs contribution ----------------------------------------
_SENT_RX = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
_CONTRIB_RX = re.compile(
    r"\b(?:here,?\s+we|herein|in this (?:work|study|paper|article|report|letter)|"
    r"this (?:work|study|paper) (?:reports?|demonstrates?|shows?|presents?|"
    r"introduces?|proposes?|reveals?|develops?)|"
    r"we (?:report|demonstrate|show|develop|propose|present|find|found|reveal|"
    r"introduce|design|achieve|identify|discover|uncover|establish|investigate|"
    r"examine|study|explore|employ|combine|quantify|resolve|track|observe|"
    r"fabricate|apply|elucidate|use|utili[sz]e|systematically))\b", re.I)


_PASSIVE_RX = re.compile(
    r"\b(?:is|are|was|were|has been|have been) (?:\w+ly )?(?:introduced|developed|"
    r"proposed|demonstrated|reported|designed|employed|achieved|investigated|presented|"
    r"fabricated|applied|realized|realised|revealed|found|shown|identified|obtained|"
    r"synthesi[sz]ed|incorporated|utili[sz]ed|explored|studied|examined|quantified|"
    r"elucidated|constructed|engineered)\b|"
    r"\b(?:this|the (?:proposed|resulting|optimi[sz]ed|modified|target))"
    r" (?:strategy|approach|method|additive|treatment|design|device|molecule)\b", re.I)
_OUTLOOK_RX = re.compile(
    r"pave[sd]? (?:the|a) (?:way|path|road)|open(?:s|ing)? (?:up )?(?:a |new )?"
    r"(?:avenues?|routes?|doors?|opportunit\w*|possibilit\w*)|holds? (?:great )?promise|"
    r"promising (?:for|candidates?|routes?|strateg\w*|pathways?|approach\w*|platforms?)|"
    r"great potential|potential (?:for|applications?)|"
    r"(?:provides?|offers?|presents?) (?:a |an |new |valuable |important |useful )*"
    r"(?:insights?|guidelines?|guidance|routes?|strateg\w*|pathways?|platforms?|"
    r"perspectives?|framework|avenues?|foundation|design principles?|understanding)|"
    r"toward[s]? (?:the )?(?:commerciali|practical|future|next|real)|"
    r"future (?:development|applications?|research|design|studies)|"
    r"(?:applicable|extend\w*|generali[sz]\w*) to (?:other|tandem|a broad)|"
    r"next[- ]generation|sheds? (?:new )?light|step (?:forward|toward)|"
    r"(?:highlights?|underlines?|underscores?|demonstrates?) the (?:importance|potential|promise)",
    re.I)


def split_abstract(ab):
    """Return (background, contribution, claim).

    Abstracts usually open by restating the field ('Perovskites are promising
    for tandem applications...'). Only text from the first contribution
    sentence onward is evidence of what THIS paper is about.
    """
    sents = [s.strip() for s in _SENT_RX.split(ab or "") if s.strip()]
    if not sents:
        return "", "", ""
    idx = None
    for i, s in enumerate(sents):
        if _CONTRIB_RX.search(s):
            idx = i
            break
    if idx is None:                  # passive-voice abstracts: "X is introduced..."
        for i, s in enumerate(sents[1:], 1):
            if _PASSIVE_RX.search(s):
                idx = i
                break
    if idx is None:
        idx = 1 if len(sents) >= 3 else 0
    background = " ".join(sents[:idx])
    body = sents[idx:]
    while len(body) > 1 and _OUTLOOK_RX.search(body[-1]):
        body = body[:-1]            # "...paves the way for efficient tandems."
    contribution = " ".join(body)
    claim = sents[idx]
    if len(claim) < 90 and idx + 1 < len(sents):
        claim = claim + " " + sents[idx + 1]
    if len(claim) > CLAIM_CHARS:
        claim = claim[:CLAIM_CHARS].rsplit(" ", 1)[0] + "…"
    return background, contribution, claim


def device_classes(title, contribution, abstract):
    """PV / LED / DET, read from the title first. PV wins in the title, because
    PV papers routinely mention electroluminescence imaging or LED light."""
    if PV_RX.search(title):
        return {"PV"}
    cls = set()
    if LED_RX.search(title):
        cls.add("LED")
    if DET_RX.search(title):
        cls.add("DET")
    if cls:
        return cls
    text = contribution or abstract
    n = {"PV": len(PV_RX.findall(text)), "LED": len(LED_RX.findall(text)),
         "DET": len(DET_RX.findall(text))}
    best = max(n.values())
    return {k for k, v in n.items() if v == best and v > 0}


def is_halide_perovskite(text, cls):
    """Perovskite, AND either halide chemistry or a PV device context.
    Excludes oxide-perovskite catalysis, ferroics and gas sensors."""
    if not PK_RX.search(text):
        return False
    if FORMULA_RX.search(text):
        return True
    if OXIDE_RX.search(text) or OXIDE_FORMULA_RX.search(text):
        return False
    return bool(HALIDE_RX.search(text)) or bool(cls)


# ================================================================== slices ==
# need  : list of concept groups; each group = alternatives; every group must
#         hit the title or the contribution part (background alone never counts)
# focus : the slice's defining concepts -- must hit the title, or the
#         contribution at least `min_c` times
# boost : ranking only (never filters): pushes the most on-point papers up
# cls   : required device class (None = any halide-perovskite paper)
# umbrella slices are broad catch-alls; they are never a paper's primary slice
# Scope words that abstracts drop in passing ("...also relevant to tandems")
# must be in the title or appear at least twice in the contribution.
NEED_MIN = {"wbg": 2, "tandem": 2, "nbg": 2}


class Slice:
    def __init__(self, slug, code, title, purpose, need, focus, boost=(),
                 cls="PV", min_c=2, umbrella=False):
        self.slug, self.code, self.title, self.purpose = slug, code, title, purpose
        self.need = [list(g) for g in need]
        self.focus = list(focus)
        self.boost = list(boost)
        self.cls = cls
        self.min_c = min_c
        self.umbrella = umbrella

    def rule(self):
        parts = []
        if self.cls:
            parts.append({"PV": "photovoltaic", "LED": "light-emitting",
                          "DET": "detector"}[self.cls] + " papers")
        for g in self.need:
            parts.append(" or ".join(g))
        txt = "Halide perovskite · " + " · ".join(parts)
        if self.focus:
            txt += (f" · defining concept ({' / '.join(self.focus)}) in the title "
                    f"or ≥{self.min_c}× in the paper's own contribution")
        return txt


SLICES = [
    # -- A: wide-bandgap absorber losses --------------------------------------
    Slice("wbg-all", "A0", "A0 · Wide-bandgap perovskite PV — everything",
          "Umbrella for the whole wide-bandgap absorber literature. Use for "
          "catching up; the A1–A5 slices are the analytical cuts.",
          need=[["wbg"]], focus=["wbg"], umbrella=True),
    Slice("wbg-processing", "A1", "A1 · Wide-bandgap: processing-induced losses",
          "Does how the film is made (crystallisation, morphology, deposition "
          "route) set the loss? Evidence linking processing to WBG performance.",
          need=[["wbg"], ["processing"]], focus=["processing"],
          boost=["voc", "hetero", "attrib"]),
    Slice("wbg-intrinsic", "A2", "A2 · Wide-bandgap: intrinsic / thermodynamic limits",
          "Evidence that the loss is inherent to the composition rather than "
          "the recipe: miscibility, formation energetics, lattice strain, theory.",
          need=[["wbg"], ["intrinsic"]], focus=["intrinsic"],
          boost=["segregation", "voc"]),
    Slice("wbg-voc", "A3", "A3 · Wide-bandgap: voltage-loss attribution",
          "Papers that locate or quantify the voltage loss (QFLS, PLQY, implied "
          "Voc, recombination partitioning); attribution studies rank first.",
          need=[["wbg"], ["voc"]], focus=["voc"], boost=["attrib", "hetero"]),
    Slice("wbg-heterogeneity", "A4", "A4 · Wide-bandgap: compositional / spatial heterogeneity",
          "Local composition and property variation — the measurement that can "
          "discriminate processing-induced from intrinsic loss.",
          need=[["wbg"], ["hetero"]], focus=["hetero"],
          boost=["voc", "segregation", "attrib"]),
    Slice("wbg-segregation", "A5", "A5 · Halide segregation: mechanism and suppression",
          "Driving force, kinetics, reversibility, and what actually stops "
          "photo-induced halide segregation under operation.",
          need=[["segregation"]], focus=["segregation"],
          boost=["intrinsic", "hetero", "mechanism"], min_c=1),
    # -- B: integrated tandems --------------------------------------------------
    Slice("tandem-all", "B0", "B0 · Perovskite-based tandems — everything",
          "Umbrella for all perovskite tandem / multijunction PV (all-perovskite, "
          "perovskite/Si, perovskite/organic, perovskite/CIGS).",
          need=[["tandem"]], focus=["tandem"], umbrella=True),
    Slice("tandem-interface-limit", "B1", "B1 · Tandem stability — ranked toward degradation attribution",
          "Tandem papers whose subject is stability. Papers that attribute "
          "degradation to a specific layer or sub-cell rank first; papers that "
          "merely report 'efficient and stable' devices rank below.",
          need=[["tandem"], ["stability", "mechanism"]],
          focus=["stability", "mechanism"], boost=["mechanism", "attrib", "subcell"]),
    Slice("subcell-selective", "B2", "B2 · Sub-cell selective / resolved characterisation",
          "Methods that interrogate one junction inside a finished tandem: "
          "selective illumination, light bias, sub-cell EQE/PL/EL, reciprocity.",
          need=[["tandem"], ["subcell"]], focus=["subcell"],
          boost=["stability", "voc", "attrib"], min_c=1),
    Slice("tandem-coupling", "B3", "B3 · Series coupling: mismatch, reverse bias, energy yield",
          "Failure and loss modes that exist only because cells are wired in "
          "series: current mismatch, shading, reverse bias, spectral variation.",
          need=[["tandem"], ["coupling"]], focus=["coupling"],
          boost=["stability", "reliability"], min_c=1),
    Slice("icl", "B4", "B4 · Interconnection / recombination layer",
          "The layer unique to monolithic tandems: design, transparency, "
          "sputter damage, and its role in loss and degradation.",
          need=[["tandem"], ["icl"]], focus=["icl"],
          boost=["stability", "voc"], min_c=1),
    Slice("nbg-stability", "B5", "B5 · Narrow-bandgap (Sn–Pb) stability",
          "Sn(II) oxidation and the narrow-gap sub-cell as the usual suspect "
          "for tandem lifetime.",
          need=[["nbg"], ["stability", "mechanism"]],
          focus=["stability", "mechanism"], boost=["mechanism", "attrib"]),
    # -- C: methods and mechanisms --------------------------------------------
    Slice("operando", "C1", "C1 · Operando / in-situ characterisation",
          "Watching films and devices while they form or degrade — the "
          "experimental basis for settling both the A and B questions.",
          need=[["operando"]], focus=["operando"], boost=["stability", "formation"],
          min_c=1),
    Slice("ion-dynamics", "C2", "C2 · Ion migration and its device signatures",
          "Mobile ions behind hysteresis, metastability and slow degradation.",
          need=[["ion"]], focus=["ion"], boost=["mechanism", "stability"]),
    Slice("stability-mechanism", "C3", "C3 · Degradation pathways (mechanism-level)",
          "Papers that name a degradation pathway, failure mode or rate-limiting "
          "step — not papers that only report a longer T80.",
          need=[["mechanism"]], focus=["mechanism"], boost=["attrib", "operando"],
          min_c=1),
    # -- D: applications ----------------------------------------------------------
    Slice("app-detector", "D1", "D1 · Detectors: photodetectors, X-ray and radiation",
          "Halide-perovskite detectors. SKLLM already has radiation-detection "
          "work, so this is the natural in-house collaboration axis.",
          need=[], focus=[], cls="DET"),
    Slice("app-led", "D2", "D2 · Light emission: perovskite LEDs",
          "Halide-perovskite light-emitting devices; the lab's institutional "
          "strength.", need=[], focus=[], cls="LED"),
    Slice("app-indoor", "D3", "D3 · Indoor / low-light photovoltaics",
          "Wide-bandgap absorbers as indoor harvesters: the A-axis weakness "
          "(large gap) becomes a strength.",
          need=[["indoor"]], focus=["indoor"], boost=["integration", "wbg"], min_c=1),
    Slice("app-scaleup", "D4", "D4 · Scale-up: modules, coating, manufacturability",
          "Large-area processing and module integration — what an industry "
          "partner asks about first.",
          need=[["scaleup"]], focus=["scaleup"], boost=["reliability"]),
    Slice("app-reliability", "D5", "D5 · Reliability, encapsulation and certification",
          "Damp heat, thermal cycling, IEC protocols, outdoor data — the "
          "language of company partners and applied-programme reviewers.",
          need=[["reliability"]], focus=["reliability"],
          boost=["mechanism", "scaleup"]),
    Slice("app-flexible", "D6", "D6 · Flexible, lightweight and space photovoltaics",
          "Specific-power-driven applications where perovskites beat silicon on "
          "a metric other than efficiency.",
          need=[["flexible"]], focus=["flexible"], boost=["tandem", "stability"]),
    # -- E: fundamental and platform -----------------------------------------
    Slice("fundamental-formation", "E1", "E1 · Film formation: nucleation, growth, phase evolution",
          "The chemistry of how the solid forms; underpins the A-axis "
          "processing-versus-intrinsic question.",
          need=[["formation"]], focus=["formation"],
          boost=["operando", "processing"]),
    Slice("new-materials", "E2", "E2 · New compositions and material families",
          "Lead-free, tin-based, double and chalcogenide perovskites, new site "
          "chemistry — a genuinely new absorber rather than a better recipe.",
          need=[["newmat"]], focus=["newmat"], boost=["stability"], min_c=1),
    Slice("architecture", "E3", "E3 · Device architectures and optical design",
          "Layouts rather than layers: back-contact, three-terminal, bifacial, "
          "semitransparent, light management.",
          need=[["architecture"]], focus=["architecture"], boost=["tandem"]),
    Slice("metrology", "E4", "E4 · Measurement protocols, standards, reproducibility",
          "How the field measures itself; a credible route to a methods-led or "
          "community-facing project.",
          need=[["metrology"]], focus=["metrology"], boost=["stability"]),
    Slice("integration", "E5", "E5 · Self-powered systems, IoT and on-chip integration",
          "Where the cell becomes a component; the language of industry "
          "partners and applied programmes.",
          need=[["integration"]], focus=["integration"], boost=["indoor"], min_c=1),
    Slice("solar-fuels", "E6", "E6 · PV-driven solar fuels",
          "Perovskite PV or photoelectrodes driving water splitting, CO2 "
          "reduction or other fuel synthesis — the long-term multijunction "
          "direction.", need=[["fuels"]], focus=["fuels"], boost=["tandem"],
          cls=None),
    # -- F: device engineering (the everyday literature) -------------------------
    Slice("passivation", "F1", "F1 · Defect passivation and interface engineering",
          "Surface, bulk, grain-boundary and buried-interface passivation, 2D/3D "
          "heterostructures, interfacial dipoles — the bulk of single-junction "
          "efficiency work.",
          need=[["passivation"]], focus=["passivation"], boost=["voc", "stability"]),
    Slice("contacts", "F2", "F2 · Charge-transport layers, contacts and SAMs",
          "Hole/electron transport materials, self-assembled monolayers, "
          "transparent and back electrodes.",
          need=[["contacts"]], focus=["contacts"], boost=["stability", "voc"]),
    Slice("lead-sustainability", "F3", "F3 · Lead management, recycling and sustainability",
          "Lead leakage and sequestration, toxicity, recycling, life-cycle "
          "assessment, green solvents — the questions regulators and companies ask.",
          need=[["leadsafe"]], focus=["leadsafe"], boost=["scaleup", "reliability"],
          min_c=1),
]
SLICE_BY_SLUG = {s.slug: s for s in SLICES}

# Journal tiers (0 = highest). Used for ranking, so truncation drops marginal
# work rather than landmark work.
TIERS = [
    ["nature", "science"],
    ["nature energy", "nature materials", "joule",
     "energy & environmental science", "energy and environmental science",
     "nature reviews materials", "nature photonics"],
    ["nature nanotechnology", "nature communications", "science advances",
     "nature chemistry", "nature synthesis", "nature reviews", "nature electronics",
     "nature physics", "nature sustainability", "matter", "chemical reviews",
     "chemical society reviews", "nature methods"],
    ["journal of the american chemical society", "angewandte", "advanced materials",
     "advanced energy materials", "acs energy letters", "accounts of chemical research",
     "advanced functional materials"],
]
TIER_PTS = {0: 8, 1: 6, 2: 4, 3: 2}


def tier(journal):
    j = (journal or "").strip().lower()
    if j in TIERS[0]:
        return 0
    for i in (1, 2, 3):
        if any(n in j for n in TIERS[i]):
            return i
    return 4


REVIEW_JOURNALS = {
    "Chemical Reviews", "Chemical Society Reviews", "Nature Reviews Materials",
    "Nature Reviews Chemistry", "Nature Reviews Methods Primers",
    "Accounts of Chemical Research", "Nature Reviews Physics",
}
_REVIEW_TITLE_RX = re.compile(
    r"\b(?:review|perspective|roadmap|primer|overview|tutorial|"
    r"state[- ]of[- ]the[- ]art|recent (?:advances|progress|developments|trends)|"
    r"(?:advances|progress|challenges) and (?:challenges|prospects|perspectives|"
    r"opportunities)|opportunities and challenges|current status)\b", re.I)
_REVIEW_ABS_RX = re.compile(
    r"\b(?:in this review|this review\b|we review|here,? we review|"
    r"in this perspective|this perspective\b|comprehensive overview of|"
    r"we summari[sz]e (?:recent|the current|the latest))\b", re.I)


def is_review(p):
    # A "review" label is reliable; an "article" label is not (OpenAlex often
    # labels reviews as articles), so text signals are always consulted.
    if (p.get("type") or "").lower() == "review":
        return True
    if (p.get("oa_type") or "").lower() == "review":
        return True
    if (p.get("journal") or "") in REVIEW_JOURNALS:
        return True
    return bool(_REVIEW_TITLE_RX.search(p.get("title") or "") or
                _REVIEW_ABS_RX.search(p.get("abstract") or ""))


# ============================================================ preparation ==
def load_papers(data_dir=DATA_DIR):
    papers = []
    for fn in sorted(glob.glob(os.path.join(data_dir, "papers-*.json"))):
        if not re.search(r"papers-\d{4}\.json$", fn):
            continue
        try:
            with open(fn, "r", encoding="utf-8") as fh:
                papers.extend(json.load(fh).get("papers", []))
        except Exception as exc:
            print(f"  ! skipping {os.path.basename(fn)}: {type(exc).__name__}: {exc}")
    return papers


_TAG_RX = re.compile(r"<[^>]{0,200}>")
_WS_RX = re.compile(r"\s+")


_DASH_RX = re.compile("[\u2010\u2011\u2012\u2013\u2212\u00ad]")


def clean(text):
    """Undo HTML entities, strip stray tags, and turn the Unicode hyphens that
    Wiley and others use ("Wide‐Bandgap", "Tin–Lead") into plain hyphens so the
    vocabularies match them. Em dashes are left alone."""
    t = html.unescape(html.unescape(text or ""))
    t = _DASH_RX.sub("-", t)
    return _WS_RX.sub(" ", _TAG_RX.sub("", t)).strip()


# Cover art / frontispiece records repeat a paper's title:
#   "Tandem Solar Cells: Spatially Resolved ..."  or  "... (Adv. Mater. 12/2023)"
_COVER_RX = re.compile(r"\((?:Adv|Small|Sol|Angew|Chem|Energy|Nano|ChemSusChem|"
                       r"Carbon|InfoMat|EcoMat|Laser|Aggregate|SusMat|Exploration|"
                       r"Batteries|Interdiscip)[^()]{0,60}\d{1,2}/\d{4}\)\s*$")


def _key(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())


def mark_duplicates(papers):
    keys = {}
    for p in papers:
        keys.setdefault(_key(p.get("title") or ""), []).append(p)
    for p in papers:
        t = p.get("title") or ""
        dup = bool(_COVER_RX.search(t))
        if not dup and ": " in t:
            rest = _key(t.split(": ", 1)[1])
            dup = len(rest) > 30 and rest in keys and rest != _key(t)
        if dup:
            p["_pk"] = False          # stays in the year archive, not in slices
            p["_dup"] = True
    return papers


def prepare(p):
    """Annotate one paper in place with everything the slices need."""
    p["title"] = clean(p.get("title"))
    p["abstract"] = clean(p.get("abstract"))
    p["journal"] = clean(p.get("journal"))
    title = p["title"]
    ab = p["abstract"]
    bg, contrib, claim = split_abstract(ab)
    p["_claim"] = claim
    p["_cls"] = device_classes(title, contrib, ab)
    p["_pk"] = ((p.get("type") or "") != "comment" and
                is_halide_perovskite(title + " " + ab, p["_cls"]))
    p["_tier"] = tier(p.get("journal"))
    p["_rev"] = is_review(p)
    p["_date"] = (p.get("date") or "")[:10]
    p["_c"] = {}
    if p["_pk"]:
        for k, rx in CX.items():
            th = len(rx.findall(title))
            ch = len(rx.findall(contrib))
            if th or ch:
                p["_c"][k] = (th, ch)
    p["_txt"] = title + " \n " + contrib   # for bundle phrase matching
    return p


def relevance(s, p):
    """Return a relevance score if paper p belongs to slice s, else None."""
    if not p["_pk"]:
        return None
    if s.cls and s.cls not in p["_cls"]:
        return None
    c = p["_c"]
    focus_in_title = any(c.get(n, (0, 0))[0] for n in s.focus)
    for group in s.need:
        if not any(c.get(n, (0, 0))[0] or
                   c.get(n, (0, 0))[1] >= (1 if focus_in_title else NEED_MIN.get(n, 1))
                   for n in group):
            return None
    if not s.focus:
        return 0
    ft = sum(c.get(n, (0, 0))[0] for n in s.focus)
    fc = sum(c.get(n, (0, 0))[1] for n in s.focus)
    if ft == 0 and fc < s.min_c:
        return None
    rel = 3 * min(ft, 2) + min(fc, 3)
    for group in s.need:
        if not set(group) & set(s.focus):
            rel += 1 if any(c.get(n, (0, 0))[0] for n in group) else 0
    bt = sum(c.get(n, (0, 0))[0] for n in s.boost)
    bc = sum(c.get(n, (0, 0))[1] for n in s.boost)
    rel += min(2 * bt + bc, 5)
    return rel


def assign(papers):
    """Compute slice membership, relevance and each paper's primary slice."""
    members = {s.slug: [] for s in SLICES}
    for p in papers:
        p["_rel"] = {}
        if not p["_pk"]:
            continue
        for s in SLICES:
            r = relevance(s, p)
            if r is not None:
                p["_rel"][s.slug] = r
                members[s.slug].append(p)
        best, best_r = None, -1
        for s in SLICES:                      # SLICES order breaks ties
            r = p["_rel"].get(s.slug)
            if r is not None and not s.umbrella and r > best_r:
                best, best_r = s.slug, r
        p["_primary"] = best
    return members


def composite(p, rel, slug=None):
    return (rel + TIER_PTS.get(p["_tier"], 0) +
            (1 if slug and p.get("_primary") == slug else 0))


def ranked(papers, score):
    """Stable, deterministic order: score, then newest, then title."""
    return sorted(papers, key=lambda p: (-score(p), _negdate(p["_date"]),
                                         (p.get("title") or "")))


def _negdate(d):
    try:
        return -int(d.replace("-", "")[:8].ljust(8, "0"))
    except ValueError:
        return 0


# ================================================================= bundles ==
DEFAULT_BUNDLES = [
    {"slug": "nsfc-wbg-loss",
     "title": "NSFC 面上 · Wide-bandgap losses: processing-induced or intrinsic?",
     "purpose": "Context for a 面上 built on the extrinsic-versus-intrinsic question "
                "in wide-bandgap absorbers.",
     "slices": ["wbg-processing", "wbg-intrinsic", "wbg-voc",
                "wbg-heterogeneity", "wbg-segregation"], "since": 2018},
    {"slug": "nsfc-tandem-stability",
     "title": "NSFC 面上 · Which interface limits an integrated tandem?",
     "purpose": "Context for a 面上 built on attributing tandem degradation to a "
                "specific layer or sub-cell.",
     "slices": ["tandem-interface-limit", "subcell-selective", "icl",
                "nbg-stability", "tandem-coupling"], "since": 2018},
]

_SLUG_RX = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def phrase_rx(phrase):
    """Plain phrase -> forgiving regex: case-insensitive, spaces and hyphens
    interchangeable, optional plural."""
    words = [re.escape(w) for w in re.split(r"[\s\-]+", phrase.strip()) if w]
    if not words:
        return None
    body = r"[\s\-]?".join(words)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?:e?s)?(?![A-Za-z0-9])", re.I)


def load_bundles(path=BUNDLES_FILE):
    """Return (bundles, errors). Never raises: a broken bundles.json must not
    stop the daily build."""
    errors = []
    raw = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError as exc:
            errors.append(f"bundles.json is not valid JSON ({exc}); "
                          "built-in default bundles used instead.")
        except Exception as exc:
            errors.append(f"bundles.json could not be read ({exc}); "
                          "built-in default bundles used instead.")
    items = (raw or {}).get("bundles") if isinstance(raw, dict) else None
    if items is None:
        if raw is not None and not errors:
            errors.append("bundles.json has no 'bundles' list; defaults used.")
        items = DEFAULT_BUNDLES
    out, seen = [], set()
    for i, b in enumerate(items):
        if not isinstance(b, dict):
            errors.append(f"bundle #{i+1} is not an object; skipped.")
            continue
        raw_slug = str(b.get("slug", "")).strip()
        slug = raw_slug.lower()
        if not _SLUG_RX.match(slug):
            errors.append(f"bundle #{i+1}: slug '{raw_slug}' must be lowercase letters, "
                          "digits and hyphens; skipped.")
            continue
        if slug in seen:
            errors.append(f"bundle '{slug}' is defined twice; second copy skipped.")
            continue
        seen.add(slug)
        sl = [str(x) for x in (b.get("slices") or [])]
        bad = [x for x in sl if x not in SLICE_BY_SLUG]
        if bad:
            errors.append(f"bundle '{slug}': unknown slice(s) {', '.join(bad)} ignored.")
        sl = [x for x in sl if x in SLICE_BY_SLUG]
        any_t = [str(x) for x in (b.get("any") or []) if str(x).strip()]
        all_t = [str(x) for x in (b.get("all") or []) if str(x).strip()]
        exc_t = [str(x) for x in (b.get("exclude") or []) if str(x).strip()]
        if not sl and not any_t and not all_t:
            errors.append(f"bundle '{slug}': needs 'slices' or 'any'/'all' terms; skipped.")
            continue
        try:
            since = int(b.get("since") or 0)
        except (TypeError, ValueError):
            errors.append(f"bundle '{slug}': 'since' must be a year; ignored.")
            since = 0
        cls = b.get("class", "PV")
        if cls not in ("PV", "LED", "DET", "any"):
            errors.append(f"bundle '{slug}': class must be PV, LED, DET or any; PV used.")
            cls = "PV"
        out.append({
            "slug": slug, "title": str(b.get("title") or slug),
            "purpose": str(b.get("purpose") or ""), "slices": sl,
            "any": any_t, "all": all_t, "exclude": exc_t, "since": since,
            "cls": cls,
            "_any": [r for r in map(phrase_rx, any_t) if r],
            "_all": [r for r in map(phrase_rx, all_t) if r],
            "_exc": [r for r in map(phrase_rx, exc_t) if r],
        })
    return out, errors


def bundle_members(b, papers):
    """Papers in bundle b, with a bundle-level relevance score in p['_brel']."""
    out = []
    for p in papers:
        if not p["_pk"]:
            continue
        if b["cls"] != "any" and b["cls"] not in p["_cls"]:
            continue
        if b["since"] and (p["_date"][:4] or "0") < str(b["since"]):
            continue
        rel = None
        if b["slices"]:
            rs = [p["_rel"][s] for s in b["slices"] if s in p["_rel"]]
            if not rs:
                continue
            rel = max(rs)
        txt, title = p["_txt"], p.get("title") or ""
        if b["_any"]:
            if not any(r.search(txt) for r in b["_any"]):
                continue
        if b["_all"]:
            if not all(r.search(txt) for r in b["_all"]):
                continue
        if any(r.search(title) for r in b["_exc"]):
            continue
        terms = b["_any"] + b["_all"]
        tt = sum(1 for r in terms if r.search(title))
        rel = (rel or 0) + min(3 * tt + sum(1 for r in terms if r.search(txt)), 6)
        p["_brel"] = rel
        out.append(p)
    return out


# ================================================================ rendering ==
def esc(s):
    return html.escape(str(s or ""), quote=True)


CSS = ("body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
       "font-size:15px;line-height:1.55;color:#15181d;background:#fff;max-width:960px;"
       "margin:0 auto;padding:22px}h1{font-size:20px;color:#003A6A;margin:0 0 6px}"
       "h2{font-size:15px;color:#003A6A;margin:26px 0 6px;border-bottom:2px solid #e3e6ea;"
       "padding-bottom:4px}.meta{font-size:12.5px;color:#5a626e}.rule{font-size:12px;"
       "color:#8b929c}.nav{font-size:13px;background:#f7f8fa;border:1px solid #e3e6ea;"
       "border-radius:8px;padding:9px 12px;margin:12px 0}ol,ul{padding-left:22px}"
       "li{margin:5px 0;font-size:13.5px}li small{color:#8b929c}.warn{background:#fbeee7;"
       "color:#b4451d;border-radius:8px;padding:9px 12px}.abs{margin:12px 0 18px}"
       ".abs h3{font-size:14.5px;margin:0 0 3px}.abs p{margin:4px 0;font-size:13.5px}"
       "footer{margin-top:30px;font-size:12px;color:#8b929c}")


def page(title, body, back=True):
    nav = ('<p class="nav"><a href="plain.html">← Index</a> · '
           '<a href="plain-new.html">Latest papers</a> · '
           '<a href="index.html">Interactive feed</a></p>') if back else ""
    return ('<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex">\n'
            f'<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n'
            f'<h1>{esc(title)}</h1>\n{nav}{body}\n'
            '<footer>LENS Laboratory · built by make_plain.py from data/papers-*.json · '
            'static and JavaScript-free.</footer>\n</body>\n</html>\n')


def tags(p):
    out = []
    for s in SLICES:
        if s.umbrella or s.slug not in p.get("_rel", {}):
            continue
        out.append(s.code + ("*" if p.get("_primary") == s.slug else ""))
    return " ".join(out)


def digest(p, show_tags=True):
    """One line: date · journal · [REVIEW] title — key claim [slice tags]."""
    claim = p.get("_claim") or ""
    t = tags(p) if show_tags else ""
    return (f'<li><b>{esc(p["_date"][:7])}</b> · <i>{esc(p.get("journal"))}</i> · '
            + ("<b>REVIEW</b> · " if p["_rev"] else "")
            + f'<a href="{esc(p.get("link"))}">{esc(p.get("title"))}</a>'
            + (f' — {esc(claim)}' if claim else "")
            + (f' <small>[{esc(t)}]</small>' if t else "") + "</li>")


def compact(p):
    bits = [p["_date"][:7] if p.get("_date") else "", p.get("journal") or ""]
    doi = p.get("doi") or ""
    if doi.startswith("10."):
        bits.append(doi)
    return (f'<li>{"<b>R</b> " if p.get("_rev") else ""}'
            f'<a href="{esc(p.get("link"))}">{esc(p.get("title"))}</a> '
            f'<small>{esc(" · ".join(b for b in bits if b))}</small></li>')


def abstract_block(p):
    ab = (p.get("abstract") or "").strip()
    if len(ab) > ABSTRACT_CHARS:
        ab = ab[:ABSTRACT_CHARS].rsplit(" ", 1)[0] + "…"
    authors = ", ".join(p.get("authors") or [])
    if len(authors) > 240:
        authors = authors[:240].rsplit(",", 1)[0] + ", et al."
    doi = p.get("doi") or ""
    meta = " · ".join(x for x in [p["_date"], p.get("journal"),
                                  "REVIEW" if p["_rev"] else "",
                                  ("DOI " + doi) if doi.startswith("10.") else "",
                                  ("[" + tags(p) + "]") if tags(p) else ""] if x)
    return ('<div class="abs">'
            f'<h3><a href="{esc(p.get("link"))}">{esc(p.get("title"))}</a></h3>'
            f'<p class="meta">{esc(meta)}</p>'
            + (f'<p class="meta">{esc(authors)}</p>' if authors else "")
            + (f'<p>{esc(ab)}</p>' if ab else '<p class="rule">(no abstract)</p>')
            + "</div>")


def year_line(items):
    counts = {}
    for p in items:
        y = p["_date"][:4] or "?"
        counts[y] = counts.get(y, 0) + 1
    ys = sorted(counts, reverse=True)
    return " · ".join(f"{y}: {counts[y]}" for y in ys[:10])


def top_journals(items, n=6):
    counts = {}
    for p in items:
        j = p.get("journal") or "?"
        counts[j] = counts.get(j, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return " · ".join(f"{j} ({c})" for j, c in top)


def write_view(prefix, title, intro_html, items, score, writer,
               abs_top, today, show_tags=True):
    """Briefing page + ranked database parts + abstract pages for one view.

    prefix : 'plain-<slug>' or 'plain-b-<slug>'
    """
    ordered = ranked(items, score)
    n_parts = max(1, -(-len(ordered) // PART_SIZE))
    abs_items = ordered[:abs_top]
    n_abs = -(-len(abs_items) // ABS_PART_SIZE) if abs_items else 0

    part_links = " ".join(f'<a href="{prefix}-p{i}.html">part {i}</a>'
                          for i in range(1, n_parts + 1))
    abs_links = " ".join(f'<a href="{prefix}-abs{i}.html">abstracts {i}</a>'
                         for i in range(1, n_abs + 1))

    cutoff = (today - dt.timedelta(days=LATEST_DAYS)).isoformat()
    latest = sorted([p for p in ordered if p["_date"] >= cutoff],
                    key=lambda p: (_negdate(p["_date"]), -score(p),
                                   p.get("title") or ""))[:LATEST_N]
    revs = [p for p in ordered if p["_rev"]][:REVIEWS_N]

    body = (intro_html +
            f'<p class="meta"><b>{len(ordered)}</b> papers · {sum(1 for p in ordered if p["_rev"])} reviews · '
            f'by year: {esc(year_line(ordered))}</p>'
            f'<p class="meta">Top journals: {esc(top_journals(ordered))}</p>'
            f'<p class="nav">Complete ranked list: {part_links or "—"}<br>'
            f'Full abstracts (top {len(abs_items)}): {abs_links or "—"}</p>'
            '<p class="rule">Each line: date · journal · title — the paper\'s own key '
            'claim · [slice codes, * = the paper\'s primary slice]. Ranked by journal '
            'tier × topical fit.</p>'
            f'<h2>1 · Landmark — top {min(LANDMARK_N, len(ordered))} by tier × fit, all years</h2>'
            '<ol>' + "\n".join(digest(p, show_tags) for p in ordered[:LANDMARK_N]) + '</ol>'
            f'<h2>2 · Latest — last {LATEST_DAYS} days ({len(latest)} shown)</h2>'
            + ('<ol>' + "\n".join(digest(p, show_tags) for p in latest) + '</ol>'
               if latest else '<p class="rule">Nothing in this window.</p>')
            + f'<h2>3 · Reviews — top {len(revs)}</h2>'
            + ('<ol>' + "\n".join(digest(p, show_tags) for p in revs) + '</ol>'
               if revs else '<p class="rule">No reviews in this view.</p>')
            + f'<p class="nav">Continue: {part_links}</p>')
    writer(f"{prefix}.html", page(title, body))

    for i in range(1, n_parts + 1):
        chunk = ordered[(i - 1) * PART_SIZE: i * PART_SIZE]
        nav = (f'<p class="nav"><a href="{prefix}.html">↑ briefing</a> · '
               + (f'<a href="{prefix}-p{i-1}.html">← part {i-1}</a> · ' if i > 1 else "")
               + f'part {i} of {n_parts}'
               + (f' · <a href="{prefix}-p{i+1}.html">part {i+1} →</a>' if i < n_parts else "")
               + '</p>')
        start = (i - 1) * PART_SIZE + 1
        body = (nav + f'<p class="meta">Ranks {start}–{start + len(chunk) - 1} of '
                f'{len(ordered)}, by journal tier × topical fit.</p>'
                f'<ol start="{start}">' + "\n".join(digest(p, show_tags) for p in chunk)
                + '</ol>' + nav)
        writer(f"{prefix}-p{i}.html", page(f"{title} — part {i}", body))

    for i in range(1, n_abs + 1):
        chunk = abs_items[(i - 1) * ABS_PART_SIZE: i * ABS_PART_SIZE]
        nav = (f'<p class="nav"><a href="{prefix}.html">↑ briefing</a> · '
               + (f'<a href="{prefix}-abs{i-1}.html">← abstracts {i-1}</a> · ' if i > 1 else "")
               + f'abstracts {i} of {n_abs}'
               + (f' · <a href="{prefix}-abs{i+1}.html">abstracts {i+1} →</a>'
                  if i < n_abs else "") + '</p>')
        start = (i - 1) * ABS_PART_SIZE + 1
        body = (nav + f'<p class="meta">Ranks {start}–{start + len(chunk) - 1}.</p>'
                + "\n".join(abstract_block(p) for p in chunk) + nav)
        writer(f"{prefix}-abs{i}.html", page(f"{title} — abstracts {i}", body))
    return len(ordered)


# =================================================================== build ==
class Writer:
    """Writes only files whose content changed; remembers what it produced."""

    def __init__(self, out_dir):
        self.out_dir, self.written, self.changed = out_dir, set(), 0

    def __call__(self, name, content):
        path = os.path.join(self.out_dir, name)
        self.written.add(name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                if fh.read() == content:
                    return
        except FileNotFoundError:
            pass
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        self.changed += 1

    def remove_orphans(self):
        """Delete plain*.html this run did not produce (renamed/retired pages)."""
        removed = 0
        for path in glob.glob(os.path.join(self.out_dir, "plain*.html")):
            if os.path.basename(path) not in self.written:
                os.remove(path)
                removed += 1
        return removed


def build(data_dir=DATA_DIR, out_dir=None, bundles_path=BUNDLES_FILE,
          today=None, verbose=True):
    out_dir = out_dir or os.path.dirname(os.path.abspath(data_dir))
    today = today or dt.date.today()
    papers = load_papers(data_dir)
    if not papers:
        print("! no data/papers-*.json found -- nothing built, nothing removed")
        return None
    for p in papers:
        prepare(p)
    mark_duplicates(papers)
    members = assign(papers)
    bundles, berrors = load_bundles(bundles_path)
    W = Writer(out_dir)
    stats = {}

    # ---- slices
    for s in SLICES:
        items = members[s.slug]
        intro = (f'<p>{esc(s.purpose)}</p><p class="rule">Rule: {esc(s.rule())}</p>')
        n = write_view(f"plain-{s.slug}", s.title, intro, items,
                       lambda p, slug=s.slug: composite(p, p["_rel"][slug], slug),
                       W, ABS_TOP_SLICE, today)
        stats[s.slug] = n
        if verbose:
            prim = sum(1 for p in items if p.get("_primary") == s.slug)
            print(f"  {s.code:<3} {s.slug:<24} {n:>6} papers  ({prim} primary)")

    # ---- bundles
    bstats = {}
    for b in bundles:
        items = bundle_members(b, papers)
        comp = []
        if b["slices"]:
            comp.append("slices " + ", ".join(
                f'<a href="plain-{x}.html">{SLICE_BY_SLUG[x].code}</a>' for x in b["slices"]))
        if b["any"]:
            comp.append("any of: " + esc(", ".join(b["any"])))
        if b["all"]:
            comp.append("all of: " + esc(", ".join(b["all"])))
        if b["exclude"]:
            comp.append("excluding titles with: " + esc(", ".join(b["exclude"])))
        if b["since"]:
            comp.append(f"since {b['since']}")
        comp.append("device class: " + b["cls"])
        intro = ((f'<p>{esc(b["purpose"])}</p>' if b["purpose"] else "")
                 + f'<p class="rule">Bundle · {" · ".join(comp)} · '
                 f'shareable link: {esc(SITE)}plain-b-{esc(b["slug"])}.html</p>')
        n = write_view(f"plain-b-{b['slug']}", b["title"], intro, items,
                       lambda p: p["_brel"] + TIER_PTS.get(p["_tier"], 0),
                       W, ABS_TOP_BUNDLE, today)
        bstats[b["slug"]] = n
        if verbose:
            print(f"  bundle {b['slug']:<28} {n:>6} papers")

    # ---- latest catch-up page
    cutoff = (today - dt.timedelta(days=CATCHUP_DAYS)).isoformat()
    recent = [p for p in papers if p["_pk"] and p["_date"] >= cutoff]
    groups = {}
    for p in recent:
        if p.get("_primary"):
            key = p["_primary"]
        elif "PV" in p["_cls"]:
            key = "_pv"
        elif "LED" in p["_cls"]:
            key = "app-led"
        elif "DET" in p["_cls"]:
            key = "app-detector"
        else:
            key = "_other"
        groups.setdefault(key, []).append(p)
    order = [s.slug for s in SLICES if not s.umbrella] + ["_pv", "_other"]
    label = {s.slug: s.title for s in SLICES}
    label["_pv"] = "Perovskite PV — no specific slice"
    label["_other"] = "Other halide-perovskite papers"
    body = (f'<p class="meta">{len(recent)} halide-perovskite papers dated within the last '
            f'{CATCHUP_DAYS} days, grouped by each paper\'s primary slice; within a group, '
            'top journals first. This is the page to skim weekly.</p>')
    for key in order:
        g = groups.get(key)
        if not g:
            continue
        g = sorted(g, key=lambda p: (p["_tier"], _negdate(p["_date"]), p.get("title") or ""))
        body += (f'<h2>{esc(label[key])} ({len(g)})</h2><ol>'
                 + "\n".join(digest(p) for p in g) + '</ol>')
    W("plain-new.html", page(f"Latest halide-perovskite papers — last {CATCHUP_DAYS} days", body))

    # ---- complete archive, by year (every paper, not only perovskite)
    by_year = {}
    for p in papers:
        by_year.setdefault(p["_date"][:4] if p["_date"][:4].isdigit() else "unknown",
                           []).append(p)
    years = sorted((y for y in by_year if y.isdigit()), reverse=True)
    if "unknown" in by_year:
        years.append("unknown")
    year_nav = " · ".join(f'<a href="plain-archive-{y}.html">{y}</a>' for y in years)
    for y in years:
        items = sorted(by_year[y], key=lambda p: (p["_tier"], _negdate(p["_date"]),
                                                  p.get("title") or ""))
        parts = [items[i:i + ARCHIVE_CHUNK] for i in range(0, len(items), ARCHIVE_CHUNK)] or [[]]
        for pi, part in enumerate(parts):
            pn = ""
            if len(parts) > 1:
                pn = '<p class="nav">Part ' + " · ".join(
                    (f"<b>{k+1}</b>" if k == pi else
                     f'<a href="plain-archive-{y}{"" if k == 0 else "-" + str(k+1)}.html">{k+1}</a>')
                    for k in range(len(parts))) + "</p>"
            body = (f'<p class="meta">{len(items)} papers dated {esc(y)}, every topic in the '
                    f'archive, top journals first.</p>{pn}<p class="nav">Years: {year_nav}</p>'
                    '<ul>' + "\n".join(compact(p) for p in part) + '</ul>' + pn)
            W(f"plain-archive-{y}{'' if pi == 0 else '-' + str(pi+1)}.html",
              page(f"Complete archive · {y}", body))

    # ---- blind-spot check: perovskite PV papers in no slice
    unsliced = [p for p in papers if p["_pk"] and "PV" in p["_cls"] and not p.get("_primary")]
    unsliced = sorted(unsliced, key=lambda p: (p["_tier"], _negdate(p["_date"]),
                                               p.get("title") or ""))
    body = (f'<p class="meta">{len(unsliced)} halide-perovskite PV papers match no slice '
            f'(top {min(UNSLICED_MAX, len(unsliced))} shown, top journals first). A cluster of '
            'related titles here means a slice is missing or too narrow.</p>'
            '<ol>' + "\n".join(digest(p, show_tags=False) for p in unsliced[:UNSLICED_MAX]) + '</ol>')
    W("plain-unsliced.html", page("Perovskite PV papers matching no slice", body))

    # ---- index
    n_pk = sum(1 for p in papers if p["_pk"])
    n_pv = sum(1 for p in papers if p["_pk"] and "PV" in p["_cls"])
    yr_cut = (today - dt.timedelta(days=365)).isoformat()
    warn = ""
    if berrors:
        warn = ('<div class="warn"><b>bundles.json problems:</b><ul>' +
                "".join(f"<li>{esc(e)}</li>" for e in berrors) + "</ul></div>")
    brows = "".join(
        f'<li><a href="plain-b-{esc(b["slug"])}.html">{esc(b["title"])}</a> '
        f'<small>{bstats.get(b["slug"], 0)} papers</small></li>' for b in bundles)
    axes = [("A", "Wide-bandgap absorber losses"), ("B", "Integrated tandems"),
            ("C", "Methods and mechanisms"), ("D", "Applications"),
            ("E", "Fundamental and platform"), ("F", "Device engineering")]
    srows = ""
    for ax, axname in axes:
        srows += f"<h2>{ax} · {esc(axname)}</h2><ul>"
        for s in SLICES:
            if not s.code.startswith(ax):
                continue
            it = members[s.slug]
            last = sum(1 for p in it if p["_date"] >= yr_cut)
            srows += (f'<li><a href="plain-{s.slug}.html">{esc(s.title)}</a> '
                      f'<small>{len(it)} papers · {last} in the last 12 months · '
                      f'<a href="plain-{s.slug}-p1.html">list</a> · '
                      f'<a href="plain-{s.slug}-abs1.html">abstracts</a></small></li>')
        srows += "</ul>"
    body = (warn +
            f'<p class="meta">Built {dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} · '
            f'{len(papers)} papers in the archive · {n_pk} halide perovskite · {n_pv} perovskite PV.</p>'
            '<h2>How to read this</h2><p>Every slice and bundle opens with a <b>briefing</b>: '
            'the landmark papers (journal tier × topical fit, all years), then the latest '
            f'{LATEST_DAYS} days, then reviews. Each line carries the paper\'s own key claim. '
            'Below each briefing are links to the complete ranked list (one line per paper) '
            'and to full abstracts. Slice codes in brackets show where else a paper belongs; '
            'a star marks its primary slice.</p>'
            '<p>To catch up, start with <a href="plain-new.html">the latest papers</a>. '
            'For a proposal, start with its bundle.</p>'
            '<h2>Proposal bundles</h2><p class="rule">Defined in bundles.json in the repository. '
            'Each bundle has a stable, shareable link and updates daily.</p>'
            f'<ul>{brows or "<li>No bundles defined.</li>"}</ul>'
            + srows +
            '<h2>Complete archive</h2><p>Every paper, by year, every topic:</p>'
            f'<p class="nav">{year_nav}</p>'
            f'<p><a href="plain-unsliced.html">Perovskite PV papers matching no slice '
            f'({len(unsliced)})</a> — the blind-spot check.</p>')
    W("plain.html", page("LENS literature — static index", body, back=False))

    removed = W.remove_orphans()
    if verbose:
        print(f"\n{len(papers)} papers · {n_pk} halide perovskite · {n_pv} perovskite PV")
        print(f"pages: {len(W.written)} produced · {W.changed} changed · "
              f"{removed} stale removed")
        for e in berrors:
            print("  ! " + e)
    return {"slices": stats, "bundles": bstats, "pages": len(W.written),
            "changed": W.changed, "removed": removed, "errors": berrors}


# ================================================================== stats ==
def print_stats(data_dir=DATA_DIR, samples=6, seed=7):
    import random
    papers = mark_duplicates([prepare(p) for p in load_papers(data_dir)])
    members = assign(papers)
    random.seed(seed)
    for s in SLICES:
        it = members[s.slug]
        print(f"\n=== {s.code} {s.slug}: {len(it)} "
              f"({sum(1 for p in it if p.get('_primary') == s.slug)} primary)")
        top = ranked(it, lambda p, slug=s.slug: composite(p, p["_rel"][slug], slug))[:4]
        for p in top:
            print("   top ·", (p.get("title") or "")[:100])
        for p in random.sample(it, min(samples, len(it))):
            print("   rnd ·", (p.get("title") or "")[:100])


# ================================================================ selftest ==
def _mk(title, abstract="", journal="Joule", date="2026-06-01", typ="article"):
    return prepare({"title": title, "abstract": abstract, "journal": journal,
                    "date": date, "type": typ, "link": "https://doi.org/10.1/x",
                    "doi": "10.1/x", "authors": ["A B"]})


def selftest():
    import tempfile
    print("offline selftest...")
    # --- abstract split: background must not count ---
    bg, cb, claim = split_abstract(
        "Wide-bandgap perovskites are promising for tandem applications. "
        "However, losses remain. Here, we report a new hole-transport polymer "
        "for blue LEDs. Devices reach 20% EQE.")
    assert "tandem" in bg and "tandem" not in cb, (bg, cb)
    assert claim.startswith("Here, we report"), claim
    _, _, claim = split_abstract(
        "Tandems are promising. Meanwhile, reverse-bias behaviour remains underexplored. "
        "A graded dielectric layer is introduced to suppress breakdown. It works.")
    assert claim.startswith("A graded dielectric layer is introduced"), claim

    # --- device class ---
    assert device_classes("Efficient perovskite solar cells", "", "") == {"PV"}
    assert device_classes("Pure-blue perovskite light-emitting diodes", "", "") == {"LED"}
    assert device_classes("EL imaging of perovskite solar modules", "", "") == {"PV"}

    # --- domain gate: the real failures sampled from the live archive ---
    for t in ["Interfacial Electronic Modulation in Upcycled Heterostructures for Tandem NO Oxidation",
              "Resolving the Activity-Selectivity Trade-Off in NH3 Oxidation",
              "Enantioselective Three-Component Radical Tandem Reactions",
              "A Monolithically Integrated Multimodal Neural Probe",
              "Standing 1D Chains Enable Efficient Wide-Bandgap Selenium Solar Cells"]:
        p = _mk(t, "We study a catalyst and its selectivity.")
        assert not p["_pk"], t
    ox = _mk("LaNiO3 perovskite oxide electrocatalyst for oxygen evolution",
             "We report an oxide perovskite catalyst with high OER activity.")
    assert not ox["_pk"], "oxide-perovskite catalysis must be excluded"

    # --- slices on realistic papers ---
    led = _mk("Mixed-Halide Perovskite Enables Efficient Blue Light-Emitting Diodes",
              "Mixed-halide perovskites suffer from non-radiative recombination. "
              "Here we raise the PLQY of wide-bandgap CsPbBr3 emitters to 80%.")
    seg = _mk("Suppressing halide segregation in wide-bandgap perovskite solar cells",
              "Mixed-halide perovskites segregate under light. Here we show that "
              "strain relaxation suppresses halide segregation and raises Voc.")
    het = _mk("Homogenized wide-bandgap perovskites for photostable all-perovskite tandem solar cells",
              "Tandems need stable top cells. Here we map compositional heterogeneity "
              "by hyperspectral PL and remove iodide-rich domains, raising Voc.")
    tan = _mk("All-in-one additive enabled efficient narrow-bandgap perovskites for monolithic tandems",
              "Tandem solar cells are promising. Here we use an additive to improve "
              "the Sn-Pb film quality, reaching 28% efficiency.")
    att = _mk("Sub-cell resolved degradation analysis of all-perovskite tandem solar cells",
              "Tandems degrade. Here we use selective illumination to attribute the "
              "degradation to the narrow-bandgap buried interface, identifying the "
              "degradation pathway under operation.")
    cat = _mk("Light-field orchestrated tandem photothermal catalysis for CO2-to-olefin conversion",
              "Perovskite-type catalysts are studied. Here we report selective tandem catalysis.")
    papers = [led, seg, het, tan, att, cat]
    members = assign(papers)
    inn = lambda slug, p: p in members[slug]
    assert not inn("wbg-voc", led), "blue LED must not enter the PV voltage-loss slice"
    assert inn("app-led", led)
    assert inn("wbg-segregation", seg)
    assert not inn("wbg-voc", seg), "one passing Voc mention is not a voltage-loss paper"
    assert inn("wbg-heterogeneity", het)
    assert not inn("tandem-interface-limit", tan), "device paper without stability focus"
    assert inn("tandem-interface-limit", att) and inn("subcell-selective", att)
    assert inn("stability-mechanism", att)
    assert not any(cat in v for v in members.values()), "tandem catalysis must not enter"
    assert att["_primary"] in ("subcell-selective", "tandem-interface-limit",
                               "stability-mechanism"), att["_primary"]
    assert "*" in tags(att)

    # --- fixes found on the live archive ---
    assert not CX["segregation"].search("choke"), "Hoke must be a whole word"
    assert not CX["nbg"].search("1.12 eV silicon"), "silicon is not a narrow-gap perovskite"
    assert not CX["tandem"].search("monolithically integrated photovoltaic transistor")
    assert not CX["newmat"].search("3D/2D perovskite solar cells")
    assert CX["newmat"].search("2D perovskite solar cells")
    assert not CX["metrology"].search("certified efficiency of 26.1% with highly reproducible devices")
    assert not CX["integration"].search("autonomous optimization of perovskite films")
    assert not CX["metrology"].search("with a stabilized efficiency of 14.4%")
    q = _mk("Violet Perovskite Quantum Dots of MA 3 Bi 2 Br 9 Synthesized by LARP",
            "We synthesize quantum dots with violet emission.")
    assert q["_pk"], "spaced formulas must pass the gate"
    o = _mk("BiFeO3 perovskite photovoltaic effect in ferroelectric films",
            "Here we report ferroelectric photovoltaics in BiFeO3 perovskite oxide.")
    assert not o["_pk"], "oxide perovskite PV is out of scope"
    assert clean("&gt;95% retention of <i>perovskite</i>") == ">95% retention of perovskite"
    assert CX["wbg"].search(clean("Wide\u2010Bandgap")) and CX["nbg"].search(clean("Tin\u2013Lead"))
    assert CX["nbg"].search("Sn/Pb perovskite") and CX["wbg"].search("CsPbI 2 Br solar cells")
    assert CX["hetero"].search("homogeneous halogen-phase distribution")
    one = _mk("Efficient inverted perovskite solar cells via surface passivation",
              "Defects limit performance. Here we passivate surfaces with a new molecule "
              "and reach 26% efficiency. This strategy paves the way for efficient "
              "tandem solar cells.")
    assign([one])
    assert "tandem-all" not in one["_rel"], "closing outlook sentence must not count"
    assert "passivation" in one["_rel"]
    cov = [_mk("Wide-bandgap perovskite solar cells for tandems", "Here we show it."),
           _mk("Tandem Solar Cells: Wide-bandgap perovskite solar cells for tandems",
               "Here we show it."),
           _mk("Wide-bandgap perovskite solar cells (Adv. Energy Mater. 12/2025)",
               "Here we show it.")]
    mark_duplicates(cov)
    assert cov[0]["_pk"] and not cov[1]["_pk"] and not cov[2]["_pk"]

    # --- review detection survives a wrong 'article' label ---
    rv = _mk("Recent Advances in Cathode Interfacial Layers for Organic Solar Cells",
             "This review highlights recent progress.")
    assert rv["_rev"]
    assert not _mk("Efficient tandems reaching 30%",
                   "Recent progress in perovskites has been rapid. Here we make cells.")["_rev"]

    # --- ranking: tier and fit, deterministic ---
    assert tier("Nature") == 0 and tier("Nature Energy") == 1 and tier("Small") == 4

    # --- bundle phrases and a broken bundles.json ---
    r = phrase_rx("self-assembled monolayer")
    assert r.search("Self assembled monolayers for contacts")
    assert r.search("self-assembled-monolayer") and not r.search("assembled")
    with tempfile.TemporaryDirectory() as td:
        bad = os.path.join(td, "bundles.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write('{"bundles": [ {"slug": "x", }')         # malformed on purpose
        bs, errs = load_bundles(bad)
        assert errs and bs and bs[0]["slug"] == DEFAULT_BUNDLES[0]["slug"], (errs, bs)
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump({"bundles": [
                {"slug": "Bad Slug", "slices": ["wbg-voc"]},
                {"slug": "ok-one", "slices": ["wbg-voc", "nope"]},
                {"slug": "terms", "any": ["halide segregation"]}]}, fh)
        bs, errs = load_bundles(bad)
        assert [b["slug"] for b in bs] == ["ok-one", "terms"], bs
        assert any("nope" in e for e in errs) and any("Bad Slug" in e for e in errs)
        tb = [b for b in bs if b["slug"] == "terms"][0]
        assert seg in bundle_members(tb, papers)
        assert led not in bundle_members(tb, papers)

    # --- escaping ---
    nasty = _mk('<script>alert(1)</script> perovskite solar cell & co',
                "Here we show a<b> lead iodide perovskite.")
    out = digest(nasty) + abstract_block(nasty)
    assert "<script>" not in out and "alert(1)" in out
    amp = _mk("Bandgap < 1.3 eV & beyond in perovskite solar cells", "Here we show x.")
    out = digest(amp)
    assert "&lt; 1.3 eV &amp; beyond" in out, out

    # --- full build: links, write-if-changed, orphan removal ---
    with tempfile.TemporaryDirectory() as td:
        dd = os.path.join(td, "data")
        os.makedirs(dd)
        recs = [{k: v for k, v in p.items() if not k.startswith("_")} for p in papers + [rv]]
        with open(os.path.join(dd, "papers-2026.json"), "w", encoding="utf-8") as fh:
            json.dump({"papers": recs}, fh)
        bpath = os.path.join(td, "bundles.json")
        with open(bpath, "w", encoding="utf-8") as fh:
            json.dump({"bundles": [{"slug": "test-bundle",
                                    "slices": ["wbg-segregation", "wbg-heterogeneity"]}]}, fh)
        stale = os.path.join(td, "plain-sam-all.html")
        keep = os.path.join(td, "index.html")
        for f in (stale, keep):
            with open(f, "w") as fh:
                fh.write("old")
        day = dt.date(2026, 7, 1)
        r1 = build(dd, td, bpath, today=day, verbose=False)
        assert not os.path.exists(stale), "stale generated page must be removed"
        assert os.path.exists(keep), "non-generated files must never be touched"
        idx = open(os.path.join(td, "plain.html"), encoding="utf-8").read()
        for s in SLICES:
            assert f'href="plain-{s.slug}.html"' in idx, s.slug
            assert os.path.exists(os.path.join(td, f"plain-{s.slug}.html")), s.slug
        assert 'href="plain-b-test-bundle.html"' in idx
        bp = open(os.path.join(td, "plain-b-test-bundle.html"), encoding="utf-8").read()
        assert 'href="plain-b-test-bundle-p1.html"' in bp
        assert 'href="plain-b-test-bundle-abs1.html"' in bp
        assert "Homogenized" in bp and "Suppressing halide segregation" in bp
        r2 = build(dd, td, bpath, today=day, verbose=False)
        assert r2["changed"] <= 1, f"rebuild should only touch plain.html, changed {r2['changed']}"
        assert r2["removed"] == 0
    print("all selftests passed (abstract split, device class, domain gate on real "
          "failures, slice focus, primary slice, reviews, bundles incl. broken JSON, "
          "escaping, links, write-if-changed, orphan removal)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.stats:
        print_stats()
    else:
        print("building static pages from data/papers-*.json ...")
        if build() is None:
            sys.exit(1)
