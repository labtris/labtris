# The labtris.com landing page (not deployed)

> **This page is not on the internet and never has been.**
> labtris.com is served from a separate Next.js repo,
> `github.com/rajeshgangam/labtris.com` — see
> [`../HOSTING-labtris.com.md`](../HOSTING-labtris.com.md). What
> follows describes a Direct-upload flow that is not the one in
> use. Keep this directory only for the copywriting; uploading it
> would *replace* the live site rather than update it.

Starter files for a Cloudflare Pages upload.

The point of this directory is **not** to be a place the site is served
from — the site lives in Cloudflare Pages. This is a template you copy
into your local `labtris-com/` folder (or wire Pages to build from a
sibling checkout of this repo). Drop the five files below into your
upload alongside `install` (a copy of the repo root's `get.sh`) and
re-deploy.

```
labtris-com/
├── index.html          # copied from this directory
├── install             # cp ../../get.sh install
├── _redirects          # copied from this directory
├── _headers            # copied from this directory
├── logo.svg            # copied from this directory
└── screenshot.png      # copied from this directory — see below
```

Deliberate design choices in `index.html`:

- **No feature grid.** Feature grids read as AI-generated and rot in
  place. One paragraph up front, then three specific stories with real
  numbers.
- **The install command is above the fold, in a black terminal panel,
  copyable.** Nothing else competes with it.
- **What's not built yet is on the page.** Same shape as the README —
  saying what's missing is what makes the rest sound trusted.
- **No sign-up. No email capture. No "Get started" that leads to a
  form.** The whole thing is `curl | bash`, and the site says so.

`screenshot.png` is a stand-in: an empty 1600×900 panel in the page's
own background colour, so the hero holds its shape and shows nothing
rather than a broken image. Replace that one file with a real capture —
a lab with several nodes and a link running a capture is the shot the
copy promises — and leave `index.html` alone. 16:9 fills the frame
edge to edge; other ratios letterbox against the panel.

`_redirects` includes the `/handbook` proxy to the current GitHub
release asset — no per-release update needed.
