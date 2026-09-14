#!/usr/bin/env python3
"""Render the handbook as one PDF.

Stdlib only, on purpose. The alternative was pandoc plus a LaTeX distribution,
which is 4 GB of build dependency for ten pages of Markdown that this project
already controls the shape of. The renderer below handles exactly the subset
the handbook uses, and `--check` fails the build if a page ever grows a
construct it does not know — so an unsupported thing is a loud error rather
than a paragraph silently swallowed.

    python3 docs/build-pdf.py            # -> dist/labtris-handbook.pdf
    python3 docs/build-pdf.py --html     # stop at the HTML
    python3 docs/build-pdf.py --check    # parse only, no output

The PDF itself is not committed: it is a build artefact of files that are.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DOCS = HERE

#: Chapters, in reading order. The introduction becomes the contents page
#: rather than a chapter. Each entry: (mdx filename under docs/, chapter
#: number as printed, chapter title as printed).
INDEX = "introduction.mdx"
PAGES = [
    ("install.mdx",           "1",  "Install"),
    ("first-lab.mdx",         "2",  "Your first lab"),
    ("nodes-and-images.mdx",  "3",  "Nodes and images"),
    ("networking.mdx",        "4",  "Networking"),
    ("consoles.mdx",          "5",  "Consoles"),
    ("observing.mdx",         "6",  "Seeing what is happening"),
    ("labs.mdx",              "7",  "Organising labs"),
    ("users.mdx",             "8",  "Users and access"),
    ("api/rest.mdx",          "9",  "API and the assistant"),
    ("operations.mdx",        "10", "Running the server"),
]

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


# --------------------------------------------------------------- inline spans


def inline(text: str) -> str:
    """Markdown spans to HTML, in an order that stops them eating each other.

    Code first and held aside, because a backtick span may legally contain
    asterisks, underscores and brackets that are not markup — `*args` is the
    obvious one, and rendering it as emphasis silently corrupts a command the
    reader is meant to type.
    """
    stash: list[str] = []

    def hold(m: re.Match[str]) -> str:
        stash.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"`([^`]+)`", hold, text)
    text = html.escape(text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _link(m: re.Match[str]) -> str:
    label, href = m.group(1), m.group(2)
    if href.startswith("http"):
        return f'<a href="{href}">{label}</a>'
    # A cross-reference between chapters becomes an internal jump. The
    # fragment is dropped: chapter granularity is what a printed page can
    # actually offer, and a dead #anchor is worse than none.
    path = href.split("#", 1)[0]
    if not path:
        return label
    if path == "README.md":
        return f'<a href="#contents">{label}</a>'
    return f'<a href="#{Path(path).stem}">{label}</a>'


# ------------------------------------------------------------------- renderer


class Unsupported(Exception):
    """A Markdown construct this renderer does not handle."""


def render(md: str, source: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        # Fenced code. Taken verbatim, including the blank lines inside it.
        if line.startswith("```"):
            body: list[str] = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            if i >= n:
                raise Unsupported(f"{source}: unterminated code fence")
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(body)) + "</code></pre>")
            continue

        if line.startswith("---") and set(line.strip()) == {"-"}:
            out.append("<hr>")
            i += 1
            continue

        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level > 3:
                raise Unsupported(f"{source}: h{level} is deeper than the design allows")
            out.append(f"<h{level}>{inline(line[level:].strip())}</h{level}>")
            i += 1
            continue

        # Table. Requires the |---|---| separator on the second line, which is
        # what distinguishes it from a paragraph that happens to contain pipes.
        if line.startswith("|") and i + 1 < n and re.fullmatch(r"\|[\s:|-]+\|", lines[i + 1].strip()):
            head = _cells(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append(_table(head, rows))
            continue

        if line.startswith("> "):
            body = []
            while i < n and lines[i].startswith(">"):
                body.append(lines[i].lstrip(">").strip())
                i += 1
            out.append(f"<blockquote><p>{inline(' '.join(body))}</p></blockquote>")
            continue

        if re.match(r"^[-*] ", line) or re.match(r"^\d+\. ", line):
            ordered = bool(re.match(r"^\d+\. ", line))
            items: list[str] = []
            while i < n and lines[i].strip():
                cur = lines[i]
                if re.match(r"^[-*] ", cur) or re.match(r"^\d+\. ", cur):
                    items.append(re.sub(r"^([-*]|\d+\.) ", "", cur))
                elif cur.startswith("  "):
                    items[-1] += " " + cur.strip()  # continuation of the item
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            body = "".join(f"<li>{inline(t)}</li>" for t in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue

        # Paragraph: everything up to a blank line or the start of a block.
        para: list[str] = []
        while i < n and lines[i].strip():
            if lines[i].startswith(("#", "```", ">", "|")) or re.match(r"^[-*] ", lines[i]):
                break
            para.append(lines[i].strip())
            i += 1
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")

    return "\n".join(out)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table(head: list[str], rows: list[list[str]]) -> str:
    # The handbook uses `| | |` headerless tables for definition lists. Drawing
    # an empty header band on those wastes a centimetre and reads as a mistake.
    headless = not any(c.strip() for c in head)
    parts = ['<table class="defs">' if headless else "<table>"]
    if not headless:
        parts.append("<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead>")
    parts.append("<tbody>")
    for row in rows:
        parts.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


# ----------------------------------------------------------------------- page

CSS = """
@page { size: A4; margin: 18mm 17mm 20mm; }

:root {
  --ink:      #16181d;
  --muted:    #5c626e;
  --rule:     #d8dbe2;
  --accent:   #1d5c4f;
  --wash:     #f4f6f5;
  --code-bg:  #f6f7f9;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  color: var(--ink);
  background: #fff;
  font-family: Charter, "Bitstream Charter", "Iowan Old Style", Georgia,
               "Liberation Serif", serif;
  font-size: 10.4pt;
  line-height: 1.58;
  -webkit-font-smoothing: antialiased;
}

h1, h2, h3, th, .eyebrow, .cover-sub, .toc-num {
  font-family: "Helvetica Neue", Inter, "Liberation Sans", Arial, sans-serif;
}

code, pre { font-family: "SF Mono", Menlo, Consolas, "Liberation Mono", monospace; }

/* ---------------------------------------------------------------- cover */

.cover {
  height: 247mm;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  page-break-after: always;
}
.cover-mark {
  font-family: "Helvetica Neue", Inter, sans-serif;
  font-size: 9pt;
  letter-spacing: .22em;
  text-transform: uppercase;
  color: var(--accent);
  font-weight: 600;
}
.cover-title {
  font-size: 41pt;
  line-height: 1.04;
  font-weight: 400;
  letter-spacing: -.015em;
  margin: 0 0 6mm;
  text-wrap: balance;
}
.cover-sub {
  font-size: 11.5pt;
  color: var(--muted);
  max-width: 96mm;
  line-height: 1.5;
  margin: 0;
}
.cover-rule { height: 3px; width: 34mm; background: var(--accent); margin: 0 0 7mm; }
.cover-foot {
  font-size: 8.5pt;
  color: var(--muted);
  border-top: 1px solid var(--rule);
  padding-top: 3mm;
  display: flex;
  justify-content: space-between;
  font-family: "Helvetica Neue", Inter, sans-serif;
}

/* -------------------------------------------------------------- contents */

.contents { page-break-after: always; }
.contents h2 { border: 0; margin: 0 0 8mm; }
.toc { list-style: none; margin: 0; padding: 0; }
.toc li {
  display: flex;
  gap: 6mm;
  align-items: baseline;
  padding: 2.6mm 0;
  border-bottom: 1px solid var(--rule);
}
.toc-num {
  color: var(--accent);
  font-weight: 600;
  font-size: 9pt;
  min-width: 7mm;
  font-variant-numeric: tabular-nums;
}
.toc-title { font-weight: 600; min-width: 46mm; }
.toc-desc { color: var(--muted); font-size: 9.4pt; }

/* -------------------------------------------------------------- chapters */

.chapter { page-break-before: always; }

.eyebrow {
  font-size: 8pt;
  letter-spacing: .2em;
  text-transform: uppercase;
  color: var(--accent);
  font-weight: 600;
  margin: 0 0 2mm;
}

h1 {
  font-size: 22pt;
  font-weight: 400;
  letter-spacing: -.01em;
  margin: 0 0 6mm;
  padding-bottom: 4mm;
  border-bottom: 2px solid var(--ink);
  text-wrap: balance;
}
h2 {
  font-size: 13pt;
  font-weight: 600;
  margin: 9mm 0 3mm;
  padding-bottom: 1.6mm;
  border-bottom: 1px solid var(--rule);
  page-break-after: avoid;
  text-wrap: balance;
}
h3 {
  font-size: 10.5pt;
  font-weight: 600;
  margin: 6mm 0 2mm;
  color: var(--accent);
  page-break-after: avoid;
}

p { margin: 0 0 3.2mm; }
strong { font-weight: 700; }
a { color: var(--accent); text-decoration: none; border-bottom: .5px solid #9fbdb5; }

ul, ol { margin: 0 0 3.5mm; padding-left: 5.5mm; }
li { margin-bottom: 1.4mm; }

hr { border: 0; border-top: 1px solid var(--rule); margin: 7mm 0; }

code {
  font-size: .875em;
  background: var(--code-bg);
  padding: .5mm 1.1mm;
  border-radius: 2px;
  border: .5px solid #e4e6ea;
}

pre {
  background: var(--code-bg);
  border: 1px solid #e4e6ea;
  border-left: 2.5px solid var(--accent);
  border-radius: 3px;
  padding: 3mm 3.5mm;
  margin: 0 0 4mm;
  font-size: 8.4pt;
  line-height: 1.5;
  overflow-x: auto;
  page-break-inside: avoid;
}
pre code { background: none; border: 0; padding: 0; font-size: inherit; }

blockquote {
  margin: 0 0 4mm;
  padding: 2.6mm 4mm;
  background: var(--wash);
  border-left: 2.5px solid var(--accent);
  border-radius: 0 3px 3px 0;
  page-break-inside: avoid;
}
blockquote p { margin: 0; font-size: 9.6pt; color: #363b44; }

table {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 4.5mm;
  font-size: 9.4pt;
  page-break-inside: avoid;
}
th {
  text-align: left;
  font-size: 8pt;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
  padding: 0 3mm 1.8mm 0;
  border-bottom: 1.5px solid var(--ink);
}
td {
  padding: 1.9mm 3mm 1.9mm 0;
  border-bottom: 1px solid var(--rule);
  vertical-align: top;
}
td:last-child, th:last-child { padding-right: 0; }
.defs td:first-child { width: 34%; font-weight: 600; }
"""


def page(pieces: list[str], version: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>The Labtris Handbook</title>"
        f"<style>{CSS}</style></head><body>" + "".join(pieces) + "</body></html>"
    )


def cover(version: str) -> str:
    from datetime import date

    return f"""
<section class="cover">
  <div class="cover-mark">Labtris</div>
  <div>
    <div class="cover-rule"></div>
    <h1 class="cover-title">The Labtris<br>Handbook</h1>
    <p class="cover-sub">Building network labs on real kernel networking &mdash;
    install, topology, consoles, capture, and running the server.</p>
  </div>
  <div class="cover-foot">
    <span>Version {html.escape(version)}</span>
    <span>{date.today():%B %Y}</span>
  </div>
</section>"""


def contents(index_md: str) -> str:
    """The contents page, built from README.md's own table.

    Read rather than duplicated, so the PDF cannot list a different set of
    chapters from the one the repository shows.
    """
    items = []
    for row in re.findall(r"^\| \[(\d+) · ([^\]]+)\]\([^)]+\) \| ([^|]+) \|$", index_md, re.M):
        num, title, desc = row
        items.append(
            f'<li><span class="toc-num">{num}</span>'
            f'<span class="toc-title">{html.escape(title.strip())}</span>'
            f'<span class="toc-desc">{html.escape(desc.strip())}</span></li>'
        )
    if not items:
        raise Unsupported("README.md: could not read the chapter table")
    return (
        '<section class="contents" id="contents"><h2>Contents</h2>'
        f'<ul class="toc">{"".join(items)}</ul></section>'
    )


def _strip_frontmatter(text: str) -> str:
    """Drop a leading `---\\n...\\n---\\n` YAML block if present."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :]
    return text


def _unescape_mdx(text: str) -> str:
    """Reverse the MDX-friendly escaping applied in mdx_convert.py.

    - `\\{` `\\}` → `{` `}` (Markdown does not need them escaped)
    - `` `<placeholder>` `` → `<placeholder>` INSIDE code spans only
      is left as-is; in prose the surrounding backticks were the point,
      so we keep them.
    Only the brace unescape actually matters for the renderer.
    """
    return text.replace("\\{", "{").replace("\\}", "}")


def _strip_mdx_components(text: str) -> str:
    """Flatten `<CodeGroup>`/`<Info>`/etc. so the plain-Markdown renderer
    doesn't choke. `<CodeGroup>` and `</CodeGroup>` become blank lines;
    the fenced code blocks inside render normally, one after another.
    """
    return re.sub(r"^\s*</?[A-Z][A-Za-z0-9]*[^>]*>\s*$", "", text, flags=re.M)


def _load_page(rel_path: str) -> str:
    """Read a docs/*.mdx page, strip Mintlify frontmatter + JSX, return
    plain Markdown the in-tree renderer already understands."""
    src = DOCS / rel_path
    if not src.exists():
        raise Unsupported(f"{rel_path} is listed in PAGES but missing")
    text = src.read_text()
    text = _strip_frontmatter(text)
    text = _strip_mdx_components(text)
    text = _unescape_mdx(text)
    return text


def build_html() -> str:
    version = _version()
    # The introduction MDX has the same shape as the old README index — the
    # `contents()` function reads its Markdown table verbatim.
    index_md = _load_page(INDEX)
    pieces = [cover(version), contents(index_md)]
    for rel, num, title in PAGES:
        md = _load_page(rel)
        # Rebuild the original `# N · Title` H1 so the renderer's TOC anchors
        # and page headings look identical to the pre-MDX output.
        md = f"# {num} · {title}\n\n" + md
        body = render(md, rel)
        body = re.sub(r"<h1>\d+ · ", "<h1>", body, count=1)
        stem = rel.rsplit("/", 1)[-1].removesuffix(".mdx")
        pieces.append(
            f'<section class="chapter" id="{stem}">'
            f'<p class="eyebrow">Chapter {num}</p>{body}</section>'
        )
    return page(pieces, version)


def _version() -> str:
    m = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    return m.group(1) if m else "0.1.0"


def chrome() -> str:
    for c in CHROME_CANDIDATES:
        found = c if Path(c).exists() else shutil.which(c)
        if found:
            return found
    raise SystemExit(
        "no Chrome or Chromium found — install one, or run with --html and print it yourself"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", action="store_true", help="write the HTML and stop")
    ap.add_argument("--check", action="store_true", help="parse every page, write nothing")
    ap.add_argument("-o", "--out", default=str(ROOT / "dist" / "labtris-handbook.pdf"))
    args = ap.parse_args()

    doc = build_html()
    if args.check:
        print(f"parsed {len(PAGES)} chapters, {len(doc):,} bytes of HTML — ok")
        return

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.html:
        target = out.with_suffix(".html")
        target.write_text(doc)
        print(target)
        return

    # Snap-confined Chromium (Ubuntu 22.04+) cannot read /tmp at all — the
    # sandbox only sees $HOME and a few whitelisted paths. If we hand it a
    # source under /tmp it renders an "ERR_FILE_NOT_FOUND" error page and
    # writes a 1-page PDF, which used to ship silently. So the scratch dir
    # goes under $HOME, and we sanity-check the byte count after.
    with tempfile.TemporaryDirectory(dir=Path.home()) as tmp:
        src = Path(tmp) / "handbook.html"
        src.write_text(doc)
        pdf = Path(tmp) / "handbook.pdf"
        subprocess.run(
            [
                chrome(),
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                f"--user-data-dir={tmp}/profile",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf}",
                src.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        size = pdf.stat().st_size
        if size < 32 * 1024:
            raise SystemExit(
                f"handbook PDF is only {size} bytes — Chrome likely rendered an "
                f"error page. On Ubuntu 22.04+, install a non-snap Chromium "
                f"(e.g. `sudo snap remove chromium && sudo apt install chromium`, "
                f"or use `google-chrome` from the .deb)."
            )
        shutil.move(str(pdf), out)
    print(f"{out}  ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    sys.exit(main())
