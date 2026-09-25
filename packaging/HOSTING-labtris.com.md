# Serving labtris.com

Two things need URLs:

- **`https://labtris.com/install`** returns `get.sh` verbatim, so
  `curl -fsSL https://labtris.com/install | sudo bash` works.
- **`https://docs.labtris.com`** points at the Mintlify site.

Both are on Cloudflare Pages today, and both are live.

## `labtris.com` is a separate repo

**The site is not in this repo.** It is a Next.js 16 app that lives
in `github.com/rajeshgangam/labtris.com` and builds to a static
export (`output: "export"` in `next.config.ts`) that Cloudflare
Pages serves. Pages, content, styling — all of it is over there.

Nothing in this repo is deployed to labtris.com. If you change a
page, you change it in that repo.

Two files in that repo's `public/` do the work this section used to
describe by hand:

    public/get.sh        # copy of this repo's get.sh (see below)
    public/_redirects    # /install    /get.sh    200
    public/_headers      # Content-Type: text/x-shellscript on both paths

The `200` makes `/install` a rewrite rather than a bounce, so
`curl -fsSL https://labtris.com/install` gets the script body
directly. Verify any time with:

    curl -fsSL https://labtris.com/install | head -3
    # #!/usr/bin/env bash
    # #
    # # Fetch Labtris and hand off to the real installer.

### What is not wired up

`/docs`, `/handbook` and `/handbook.pdf` return **404** on the live
site — the rewrite rules for them were never deployed. Nothing
links to those paths today (the site's "Read the handbook" buttons
point straight at `https://docs.labtris.com`), so this is a missing
convenience, not a broken link. To add them, put this in the site
repo's `public/_redirects`:

    /docs           https://docs.labtris.com            302
    /docs/*         https://docs.labtris.com/:splat     302
    /handbook       https://github.com/labtris/labtris/releases/latest/download/labtris-handbook.pdf  302
    /handbook.pdf   https://github.com/labtris/labtris/releases/latest/download/labtris-handbook.pdf  302

## `docs.labtris.com` on Mintlify

**1. Sign up at mintlify.com** (free for OSS). Install the Mintlify
GitHub App and point it at this repo's `docs/` folder. Push. It
starts deploying to `<project>.mintlify.app` immediately.

**2. Custom domain.** Mintlify dashboard → Settings → Custom
domain → `docs.labtris.com`. Mintlify prints a CNAME record.

**3. Add the CNAME.** Cloudflare DNS → Add record:

    Type:   CNAME
    Name:   docs
    Target: cname.mintlify.app.        # (whatever Mintlify printed)
    Proxy:  DNS only                    # NOT proxied — Mintlify handles TLS

**4. Wait a minute, then visit `https://docs.labtris.com`.**

## The handbook PDF

The PDF is a release artefact — `packaging/release.sh` builds it
with `python3 docs/build-pdf.py` and uploads it alongside the ISO
to every GitHub release. Nothing to host on Cloudflare Pages
itself; the `_redirects` rule above proxies `labtris.com/handbook`
straight to GitHub's stable
`https://github.com/labtris/labtris/releases/latest/download/labtris-handbook.pdf`
URL, which always points at the current release.

Two consequences worth naming:

- **A fresh release automatically updates the link.** No manual push
  to Cloudflare after cutting a release.
- **The URL for a specific version is
  `https://github.com/labtris/labtris/releases/download/v<VERSION>/labtris-handbook.pdf`.**
  Deep-link that from the docs when you want a pinned handbook — e.g.
  a course syllabus that cites a stable version.

If bandwidth to GitHub becomes a concern (unlikely — the file is
~1 MB and served with the release CDN), the alternative is a nightly
copy into the site repo's `public/handbook.pdf` served directly by
Cloudflare Pages. The `_redirects` line above would then swap to a
same-origin rewrite.

## `get.sh` lives in two places

This is the one piece of real coupling between the two repos, and
it is worth knowing about because nothing enforces it.

`get.sh` in this repo is the source of truth. The copy that
`curl https://labtris.com/install` actually returns is
`public/get.sh` in the **site** repo. They are byte-identical right
now, but a fix here does not reach the installer URL until someone
copies it across:

    cp get.sh ../labtris.com/public/get.sh   # then commit + deploy the site

Check for drift without cloning anything:

    curl -fsSL https://labtris.com/install | diff - get.sh && echo in-sync

Worth running after any `get.sh` change, because the failure is
silent: the repo looks right, and users keep getting the old
installer.

## Testing before DNS lands

The `labtris.com/install` URL will not resolve until DNS + Pages is
set up. In the meantime `get.sh` is also reachable at the raw
GitHub URL, which is what the docs used to name:

    curl -fsSL https://raw.githubusercontent.com/labtris/labtris/main/get.sh | sudo bash

The docs no longer advertise this, but it works.

## GitHub org rename (later)

When the repo moves to `github.com/labtris/labtris`, `get.sh`'s
`LABTRIS_REPO` default needs to change alongside. GitHub's redirect
handles the URL bounce for browsers but git clones follow it only
after the first fetch — a fresh install cloning from the redirected
URL is fine, it just takes an extra round trip. So the rename is
low-risk to defer.

Three files carry the current repo URL and want updating on move:

- `get.sh` — `LABTRIS_REPO` default
- `README.md` — the "Grab it from the latest release" link
- `docs/install.mdx` — same link

Nothing else needs a touch. `labtris.com/install` stays the entry
point either way.
