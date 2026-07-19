#!/usr/bin/env python3
"""
make_plain.py -- 从 data/papers-*.json 生成【无 JavaScript 的静态页面】。

为什么需要它
    index.html 用 JavaScript 在运行时读取 data/papers-*.json，任何不执行 JS 的
    读取方（AI 助手、纯文本客户端、部分爬虫）看到的只是空壳。本脚本把同样的
    档案写成普通 HTML，使其可被直接读取。

设计要点
    - 完全独立：只读 data/papers-*.json，只写 plain*.html。
      不修改 index.html / aggregate.py / feeds.py / 任何数据文件，
      现有网站行为完全不变。删掉本脚本与 plain*.html 即可回到原状。
    - 索引页用真实 <a href> 链到每个主题页。这一点很关键：
      只能跟随已发现链接的读取方，无法凭空猜测 URL。

用法
    python3 make_plain.py              # 生成全部页面
    python3 make_plain.py --selftest   # 离线自测
    python3 make_plain.py --months 24  # 调整“最近”窗口
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

# 主题切片：(slug, 显示名, [正则备选]) —— 增删一行即可
TOPICS = [
    ("sam", "SAM · 自组装单分子层 / 空穴选择接触",
     [r"self[- ]assembled monolayer", r"\bSAMs?\b", r"2PACz", r"Me-4PACz",
      r"phosphonic acid", r"carbazole", r"hole[- ]selective contact"]),
    ("tco", "TCO · 透明导电氧化物 / 透明电极",
     [r"transparent conduct", r"\bTCOs?\b", r"\bITO\b", r"\bIZO\b", r"\bAZO\b",
      r"\bFTO\b", r"indium tin oxide", r"sputter", r"transparent electrode"]),
    ("icl", "互连层 / 复合结 / 隧穿结",
     [r"interconnect", r"recombination (?:layer|junction)",
      r"tunnel(?:ling)? junction", r"\bICL\b"]),
    ("wbg", "宽带隙 · 卤素相分离 / 电压损失",
     [r"wide[- ]bandgap", r"halide segregation", r"phase segregation",
      r"open[- ]circuit voltage", r"quasi[- ]Fermi"]),
    ("nbg", "窄带隙 · Sn-Pb 钙钛矿",
     [r"narrow[- ]bandgap", r"tin[- ]lead", r"Sn[- ]Pb", r"tin oxidation"]),
    ("stability", "稳定性 / 衰减机理 / 离子迁移",
     [r"stabilit", r"degrad", r"ion migration", r"operational stability",
      r"\bT80\b", r"light soaking", r"encapsulat"]),
    ("passivation", "钝化 / 界面缺陷",
     [r"passivat", r"defect", r"trap density",
      r"non[- ]radiative recombination", r"interface engineering"]),
    ("tandem", "叠层 / 多结器件",
     [r"tandem", r"multijunction", r"multi[- ]junction", r"current matching"]),
]

PER_PAGE = 150
ABSTRACT_CHARS = 1500


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
            print(f"  ! 跳过 {fn}: {type(exc).__name__}: {exc}")
    papers.sort(key=lambda p: p.get("date", ""), reverse=True)
    return papers


def compile_topics(topics=TOPICS):
    return [(s, n, re.compile("|".join(p), re.I)) for s, n, p in topics]


def match_topic(paper, rx):
    return bool(rx.search((paper.get("title") or "") + " " + (paper.get("abstract") or "")))


def within_months(paper, months, today=None):
    d = (paper.get("date") or "")[:10]
    if not d:
        return False
    try:
        pd = dt.date.fromisoformat(d)
    except ValueError:
        return False
    today = today or dt.date.today()
    return pd >= today - dt.timedelta(days=int(months * 30.44))


def esc(s):
    return html.escape(str(s or ""), quote=True)


CSS = ("body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
       "'PingFang SC','Microsoft YaHei',sans-serif;font-size:15px;line-height:1.6;"
       "color:#15181d;background:#fff;max-width:900px;margin:0 auto;padding:24px}"
       "h1{font-size:21px;color:#003A6A;margin:0 0 4px}"
       "h2{font-size:15px;color:#003A6A;margin:28px 0 8px;border-bottom:2px solid #e3e6ea;"
       "padding-bottom:5px}"
       ".sub{color:#5a626e;font-size:12px;font-family:ui-monospace,Menlo,monospace;"
       "margin-bottom:18px}"
       ".nav{margin:14px 0 22px;padding:12px 14px;background:#f7f8fa;"
       "border:1px solid #e3e6ea;border-radius:8px}"
       ".nav a{display:inline-block;margin:3px 10px 3px 0;color:#003A6A}"
       "article{border-bottom:1px solid #eef0f3;padding:14px 0}"
       "article h3{font-size:15px;font-weight:600;margin:0 0 5px;line-height:1.4}"
       "article h3 a{color:#15181d;text-decoration:none}"
       "article h3 a:hover{color:#003A6A;text-decoration:underline}"
       ".meta{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#8b929c;"
       "margin-bottom:6px}"
       ".auth{font-size:12.5px;color:#5a626e;margin-bottom:6px}"
       ".abs{font-size:13.5px;color:#2b313b}"
       "footer{margin-top:36px;padding-top:14px;border-top:1px solid #e3e6ea;"
       "font-size:12px;color:#8b929c}")


def page(title, body, generated, back=True):
    nav = ('<div class="nav"><a href="plain.html">&larr; 全部主题索引</a> · '
           '<a href="index.html">交互版文献流</a></div>') if back else ""
    return ('<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex">\n'
            f'<title>{esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n'
            f'<h1>{esc(title)}</h1>\n'
            f'<div class="sub">静态快照 · 生成于 {esc(generated)} · 无 JavaScript</div>\n'
            f'{nav}{body}\n'
            '<footer>LENS Laboratory · 由 make_plain.py 从 data/papers-*.json 生成。'
            '只读快照；交互检索请用 index.html。</footer>\n</body>\n</html>\n')


def render_paper(p):
    ab = (p.get("abstract") or "").strip()
    if len(ab) > ABSTRACT_CHARS:
        ab = ab[:ABSTRACT_CHARS].rsplit(" ", 1)[0] + "…"
    authors = ", ".join(p.get("authors") or [])[:300]
    meta = " · ".join(x for x in [p.get("date", ""), p.get("journal", ""),
                                  (p.get("type") or "").title()] if x)
    doi = p.get("doi", "")
    if doi and doi.startswith("10."):
        meta += " · DOI " + doi
    return ("<article>\n"
            f'<h3><a href="{esc(p.get("link",""))}" rel="noopener">'
            f'{esc(p.get("title",""))}</a></h3>\n'
            f'<div class="meta">{esc(meta)}</div>\n'
            + (f'<div class="auth">{esc(authors)}</div>\n' if authors else "")
            + (f'<div class="abs">{esc(ab)}</div>\n' if ab else "")
            + "</article>")


def build_index(slices, total, generated, months):
    rows = "\n".join(
        f'<article><h3><a href="plain-{esc(s)}.html">{esc(n)}</a></h3>'
        f'<div class="meta">{c} 篇</div></article>' for s, n, c in slices)
    body = ("<h2>主题切片</h2>\n<p>每个主题为独立静态页，含标题、作者、摘要、DOI。</p>\n"
            + rows
            + f'\n<h2>时间切片</h2>\n<article><h3>'
              f'<a href="plain-recent.html">最近 {months} 个月的全部论文</a></h3></article>\n'
            + f"<h2>说明</h2>\n<p>库内共 {total} 篇；每页最多 {PER_PAGE} 篇（日期倒序），"
              f"摘要截断至 {ABSTRACT_CHARS} 字符。主题关键词见 make_plain.py 的 TOPICS。</p>")
    return page("LENS 文献库 · 静态索引", body, generated, back=False)


def build(months=18, data_dir=DATA_DIR, out_dir=None, verbose=True):
    out_dir = out_dir or os.path.dirname(os.path.abspath(data_dir))
    papers = load_papers(data_dir)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not papers:
        print("! 未找到 data/papers-*.json —— 请在 lit-feed 仓库根目录运行")
        return []
    written, slices = [], []
    for slug, name, rx in compile_topics():
        hits = [p for p in papers if match_topic(p, rx)]
        body = (f"<p>共匹配 {len(hits)} 篇，以下为最新 {min(len(hits), PER_PAGE)} 篇。</p>\n"
                + "\n".join(render_paper(p) for p in hits[:PER_PAGE]))
        fn = os.path.join(out_dir, f"plain-{slug}.html")
        with open(fn, "w", encoding="utf-8") as fh:
            fh.write(page(f"LENS 文献库 · {name}", body, generated))
        written.append(fn)
        slices.append((slug, name, len(hits)))
        if verbose:
            print(f"  {name:<36} {len(hits):>5} 篇 -> plain-{slug}.html")
    recent = [p for p in papers if within_months(p, months)]
    body = (f"<p>最近 {months} 个月共 {len(recent)} 篇，以下为最新 "
            f"{min(len(recent), PER_PAGE)} 篇。</p>\n"
            + "\n".join(render_paper(p) for p in recent[:PER_PAGE]))
    fn = os.path.join(out_dir, "plain-recent.html")
    with open(fn, "w", encoding="utf-8") as fh:
        fh.write(page(f"LENS 文献库 · 最近 {months} 个月", body, generated))
    written.append(fn)
    fn = os.path.join(out_dir, "plain.html")
    with open(fn, "w", encoding="utf-8") as fh:
        fh.write(build_index(slices, len(papers), generated, months))
    written.append(fn)
    if verbose:
        print(f"\n索引 -> plain.html （库内共 {len(papers)} 篇）")
    return written


def selftest():
    print("离线自测…")
    topics = compile_topics()
    by = {s: rx for s, _, rx in topics}
    sam = {"title": "Self-assembled monolayer of Me-4PACz for inverted perovskite cells",
           "abstract": "A hole-selective contact based on phosphonic acid anchoring."}
    assert match_topic(sam, by["sam"]) and not match_topic(sam, by["tco"])
    tco = {"title": "Sputtered IZO transparent electrode for tandem devices",
           "abstract": "Transparent conducting oxide deposition without damage."}
    assert match_topic(tco, by["tco"]) and match_topic(tco, by["tandem"])
    wbg = {"title": "Suppressing halide segregation in wide-bandgap perovskites",
           "abstract": "Open-circuit voltage deficit reduced."}
    assert match_topic(wbg, by["wbg"])
    off = {"title": "A study of onion farming yields", "abstract": "Nothing relevant."}
    assert not any(match_topic(off, rx) for _, _, rx in topics)
    # 'SAM' 不应匹配 same/sample 之类普通词
    assert not match_topic({"title": "The same sample was measured", "abstract": ""}, by["sam"])
    today = dt.date(2026, 7, 20)
    assert within_months({"date": "2026-06-01"}, 18, today)
    assert not within_months({"date": "2020-01-01"}, 18, today)
    assert not within_months({"date": ""}, 18, today)
    nasty = {"title": '<script>alert("x")</script> & more', "abstract": "a<b>c",
             "link": "http://x/?a=1&b=2", "authors": ["A B"], "date": "2026-01-01",
             "journal": "J", "type": "article", "doi": "10.1/xyz"}
    out = render_paper(nasty)
    assert "<script>" not in out and "&lt;script&gt;" in out
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        dd = os.path.join(td, "data")
        os.makedirs(dd)
        with open(os.path.join(dd, "papers-2026.json"), "w", encoding="utf-8") as fh:
            json.dump({"papers": [
                dict(sam, date="2026-07-01", link="http://a", journal="Nature Energy",
                     authors=["X Y"], doi="10.1/a", type="article"),
                dict(tco, date="2026-06-01", link="http://b", journal="Joule",
                     authors=["Z W"], doi="10.1/b", type="article")]}, fh)
        files = build(months=18, data_dir=dd, out_dir=td, verbose=False)
        assert len(files) == len(TOPICS) + 2, files
        idx = open(os.path.join(td, "plain.html"), encoding="utf-8").read()
        for slug, _, _ in TOPICS:
            assert f'href="plain-{slug}.html"' in idx, slug
        assert 'href="plain-recent.html"' in idx
        s = open(os.path.join(td, "plain-sam.html"), encoding="utf-8").read()
        assert "Me-4PACz" in s and 'href="plain.html"' in s
    print("全部自测通过 ✓ （主题匹配、时间窗、HTML 转义、页面互链、完整构建）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--months", type=int, default=18)
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        print("从 data/papers-*.json 生成静态页…")
        if not build(months=a.months):
            sys.exit(1)
