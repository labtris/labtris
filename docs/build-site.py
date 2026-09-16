#!/usr/bin/env python3
"""Render docs/ as a static site for docs.labtris.com.

Stdlib only, and deliberately not a docs framework. The pages are 19 MDX files
that use exactly one JSX component between them, so a framework would be a
dependency tree and a build toolchain bought for nothing.

It reuses build-pdf.py's renderer rather than carrying a second one: the PDF and
the site are two outputs of one Markdown implementation, so a construct that
renders correctly in one cannot silently differ in the other.

Navigation comes from mint.json, which is also what Mintlify reads. Keeping one
source means the sidebar cannot drift from the file that defines it.

    python3 docs/build-site.py            # -> docs/_site/
    python3 docs/build-site.py --check    # parse every page, write nothing
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "_site"

# Import the PDF builder as a module. It lives beside this file and is not a
# package, so it is loaded by path rather than by name.
_spec = importlib.util.spec_from_file_location("build_pdf", HERE / "build-pdf.py")
assert _spec and _spec.loader
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)


def nav() -> list[tuple[str, list[str]]]:
    """(group, [page slugs]) straight out of mint.json."""
    cfg = json.loads((HERE / "mint.json").read_text())
    groups = [(g.get("group", ""), list(g.get("pages") or [])) for g in cfg.get("navigation", [])]
    if not groups:
        raise SystemExit("mint.json has no navigation — nothing to build")
    return groups


def meta(slug: str) -> dict[str, str]:
    """Title and description from a page's frontmatter.

    Falls back to the first H1, then the slug: a page that forgets its
    frontmatter should still get a usable sidebar entry rather than blank.
    """
    src = HERE / f"{slug}.mdx"
    text = src.read_text()
    out: dict[str, str] = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    out[k.strip()] = v.strip().strip('"').strip("'")
    if "title" not in out:
        m = re.search(r"^#\s+(.+)$", text, re.M)
        out["title"] = m.group(1).strip() if m else slug.rsplit("/", 1)[-1]
    return out


def href(slug: str, depth: int) -> str:
    """Relative link from a page nested `depth` directories deep.

    Extensionless, because Cloudflare Pages canonicalises `/x.html` to `/x`
    with a 308. Linking to the `.html` form would cost a redirect on every
    click; worse, an explicit `/x -> /x.html` rewrite in _redirects fights that
    canonicalisation and produces an infinite loop, which is how this was
    found.

    Relative rather than absolute so the built tree still opens from the
    filesystem, where the files really are named `.html`.
    """
    return ("../" * depth) + slug


def sidebar(groups, current: str, depth: int) -> str:
    parts = ['<nav class="side" aria-label="Documentation">']
    for group, pages in groups:
        parts.append(f'<div class="grp">{html.escape(group)}</div><ul>')
        for slug in pages:
            cls = ' class="on"' if slug == current else ""
            title = html.escape(meta(slug)["title"])
            parts.append(f'<li{cls}><a href="{href(slug, depth)}">{title}</a></li>')
        parts.append("</ul>")
    parts.append("</nav>")
    return "".join(parts)


def toc(body_html: str) -> str:
    """On-page contents from the h2s the renderer emitted."""
    heads = re.findall(r"<h2>(.*?)</h2>", body_html)
    if len(heads) < 2:
        return ""
    items = "".join(
        f'<li><a href="#{slugify(h)}">{h}</a></li>' for h in heads
    )
    return f'<aside class="toc"><div class="grp">On this page</div><ul>{items}</ul></aside>'


def slugify(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def anchored(body_html: str) -> str:
    """Give every h2 an id so the on-page contents can reach it."""
    return re.sub(r"<h2>(.*?)</h2>", lambda m: f'<h2 id="{slugify(m.group(1))}">{m.group(1)}</h2>', body_html)


CSS = """
:root{
  --bg:#ffffff; --fg:#16181d; --muted:#5c626e; --rule:#e3e6eb;
  --accent:#1d5c4f; --accent-soft:#eaf2ef; --code-bg:#f6f7f9; --side:#fbfcfc;
}
:root:not([data-theme="light"]){ }
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0f1115; --fg:#e6e8ec; --muted:#9aa2b1; --rule:#252a33;
    --accent:#5bbfa5; --accent-soft:#16241f; --code-bg:#161a21; --side:#12151b;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1115; --fg:#e6e8ec; --muted:#9aa2b1; --rule:#252a33;
  --accent:#5bbfa5; --accent-soft:#16241f; --code-bg:#161a21; --side:#12151b;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;
}
code,pre{font-family:"SF Mono",Menlo,Consolas,"Liberation Mono",monospace}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

header.top{
  position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:16px;
  padding:0 20px;height:56px;background:var(--bg);border-bottom:1px solid var(--rule);
}
.brand{font-weight:700;letter-spacing:-.01em;color:var(--fg)}
.brand span{color:var(--accent)}
.top nav{margin-left:auto;display:flex;gap:18px;align-items:center;font-size:14px}
.top nav a{color:var(--muted)}
#theme{background:none;border:1px solid var(--rule);color:var(--muted);
  border-radius:7px;padding:4px 9px;cursor:pointer;font-size:13px}

.wrap{display:grid;grid-template-columns:250px minmax(0,1fr) 190px;gap:36px;
  max-width:1360px;margin:0 auto;padding:28px 20px 80px}
.side{position:sticky;top:76px;align-self:start;max-height:calc(100vh - 100px);
  overflow:auto;background:var(--side);border:1px solid var(--rule);
  border-radius:12px;padding:14px 12px}
.side .grp{font-size:11px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);font-weight:700;margin:14px 8px 6px}
.side .grp:first-child{margin-top:2px}
.side ul{list-style:none;margin:0;padding:0}
.side li a{display:block;padding:5px 9px;border-radius:7px;color:var(--fg);font-size:14px}
.side li a:hover{background:var(--accent-soft);text-decoration:none}
.side li.on a{background:var(--accent-soft);color:var(--accent);font-weight:600}

.toc{position:sticky;top:76px;align-self:start;font-size:13px}
.toc .grp{font-size:11px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);font-weight:700;margin-bottom:8px}
.toc ul{list-style:none;margin:0;padding:0}
.toc li a{display:block;padding:3px 0;color:var(--muted)}

main{min-width:0}
main h1{font-size:33px;line-height:1.15;letter-spacing:-.02em;margin:0 0 6px;text-wrap:balance}
.lede{color:var(--muted);font-size:17px;margin:0 0 26px}
main h2{font-size:21px;margin:38px 0 12px;padding-bottom:7px;
  border-bottom:1px solid var(--rule);scroll-margin-top:76px;text-wrap:balance}
main h3{font-size:16.5px;margin:26px 0 8px;color:var(--accent)}
main p{margin:0 0 14px}
main ul,main ol{margin:0 0 16px;padding-left:22px}
main li{margin-bottom:5px}
main code{background:var(--code-bg);border:1px solid var(--rule);
  border-radius:4px;padding:1px 5px;font-size:.88em}
main pre{background:var(--code-bg);border:1px solid var(--rule);
  border-left:3px solid var(--accent);border-radius:8px;padding:13px 15px;
  overflow-x:auto;margin:0 0 18px;font-size:13.5px;line-height:1.55}
main pre code{background:none;border:0;padding:0;font-size:inherit}
main blockquote{margin:0 0 18px;padding:12px 16px;background:var(--accent-soft);
  border-left:3px solid var(--accent);border-radius:0 8px 8px 0}
main blockquote p{margin:0}
main table{width:100%;border-collapse:collapse;margin:0 0 20px;font-size:14.5px;display:block;overflow-x:auto}
main th{text-align:left;font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);padding:0 14px 8px 0;border-bottom:2px solid var(--fg);white-space:nowrap}
main td{padding:9px 14px 9px 0;border-bottom:1px solid var(--rule);vertical-align:top}
main hr{border:0;border-top:1px solid var(--rule);margin:30px 0}
.pager{display:flex;justify-content:space-between;gap:16px;margin-top:48px;
  padding-top:20px;border-top:1px solid var(--rule);font-size:14px}

@media (max-width:1100px){
  .wrap{grid-template-columns:230px minmax(0,1fr)}
  .toc{display:none}
}
@media (max-width:760px){
  .wrap{grid-template-columns:1fr;gap:20px}
  .side{position:static;max-height:none}
}
"""

THEME_JS = """
(function(){
  try{
    var s=localStorage.getItem('labtris-docs-theme');
    if(s){document.documentElement.setAttribute('data-theme',s);}
  }catch(e){}
  document.addEventListener('DOMContentLoaded',function(){
    var b=document.getElementById('theme');
    if(!b)return;
    b.addEventListener('click',function(){
      var cur=document.documentElement.getAttribute('data-theme');
      var dark=cur? cur==='dark'
        : matchMedia('(prefers-color-scheme: dark)').matches;
      var next=dark?'light':'dark';
      document.documentElement.setAttribute('data-theme',next);
      try{localStorage.setItem('labtris-docs-theme',next);}catch(e){}
    });
  });
})();
"""


def shell(title: str, desc: str, body: str, groups, slug: str, depth: int, prev, nxt) -> str:
    up = "../" * depth
    pager = []
    if prev:
        pager.append(f'<a href="{href(prev, depth)}">← {html.escape(meta(prev)["title"])}</a>')
    else:
        pager.append("<span></span>")
    if nxt:
        pager.append(f'<a href="{href(nxt, depth)}">{html.escape(meta(nxt)["title"])} →</a>')
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Labtris docs</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="https://docs.labtris.com/{slug}.html">
<style>{CSS}</style>
<script>{THEME_JS}</script>
</head><body>
<header class="top">
  <a class="brand" href="{up}introduction.html">Lab<span>tris</span> docs</a>
  <nav>
    <a href="https://labtris.com">Home</a>
    <a href="https://github.com/labtris/labtris">GitHub</a>
    <button id="theme" type="button">Theme</button>
  </nav>
</header>
<div class="wrap">
  {sidebar(groups, slug, depth)}
  <main>
    <h1>{html.escape(title)}</h1>
    {f'<p class="lede">{html.escape(desc)}</p>' if desc else ''}
    {body}
    <div class="pager">{''.join(pager)}</div>
  </main>
  {toc(body)}
</div>
</body></html>"""


def build(check_only: bool = False) -> int:
    groups = nav()
    order = [s for _, pages in groups for s in pages]
    if not check_only:
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True)

    for i, slug in enumerate(order):
        md = bp._load_page(f"{slug}.mdx")
        body = anchored(bp.render(md, slug))
        info = meta(slug)
        if check_only:
            continue
        depth = slug.count("/")
        html_doc = shell(
            info["title"], info.get("description", ""), body, groups, slug, depth,
            order[i - 1] if i else None,
            order[i + 1] if i + 1 < len(order) else None,
        )
        dest = OUT / f"{slug}.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html_doc)

    if check_only:
        print(f"parsed {len(order)} pages — ok")
        return 0

    # The docs root should land somewhere useful rather than a 404.
    (OUT / "index.html").write_text(
        '<!doctype html><meta charset="utf-8">'
        '<meta http-equiv="refresh" content="0; url=introduction">'
        '<link rel="canonical" href="https://docs.labtris.com/introduction.html">'
        '<a href="introduction">Labtris documentation</a>'
    )
    # No _redirects file: Cloudflare Pages already serves `/install` from
    # `install.html`. Adding a rewrite for it collides with that behaviour and
    # loops.
    print(f"{OUT}  ({len(order)} pages)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="parse every page, write nothing")
    sys.exit(build(ap.parse_args().check))
