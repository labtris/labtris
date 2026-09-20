# The labtris.com landing page

Starter files for the Cloudflare Pages upload described in
[`../HOSTING-labtris.com.md`](../HOSTING-labtris.com.md).

The point of this directory is **not** to be a place the site is served
from — the site lives in Cloudflare Pages. This is a template you copy
into your local `labtris-com/` folder (or wire Pages to build from a
sibling checkout of this repo). Drop the four files below into your
upload alongside `install` (a copy of the repo root's `get.sh`) and
re-deploy.

```
labtris-com/
├── index.html          # copied from this directory
├── install             # cp ../../get.sh install
├── _redirects          # copied from this directory
├── _headers            # copied from this directory
└── logo.svg            # copied from this directory
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

`_redirects` includes the `/handbook` proxy to the current GitHub
release asset — no per-release update needed.
