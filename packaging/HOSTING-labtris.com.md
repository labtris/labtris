# Serving labtris.com

Two things need URLs:

- **`https://labtris.com/install`** returns `get.sh` verbatim, so
  `curl -fsSL https://labtris.com/install | sudo bash` works.
- **`https://docs.labtris.com`** points at the Mintlify site.

Cheapest way to both is Cloudflare Pages — free tier is generous
enough, HTTPS is automatic, and both live behind one nameserver
setting. Everything below is 5–10 minutes of clicks once.

## `labtris.com` on Cloudflare Pages

**1. Register at Cloudflare.** If DNS for `labtris.com` is not
already on Cloudflare, add the domain to your account and change
the registrar's nameservers to what Cloudflare gives you. Wait for
propagation (usually minutes).

**2. Create a Pages project.** Cloudflare dashboard → Workers &
Pages → Create → Pages → Direct upload. This is a one-shot upload
of a tiny site — you can wire it to a repo later if you grow it
into a marketing site.

Locally, make the directory:

    labtris-com/
    ├── index.html          # simple landing page (whatever you want here)
    ├── install             # the get.sh contents, served as text/plain
    └── _redirects          # rewrite rules

`install` is a plain copy of `get.sh` from this repo. `_redirects`
handles the docs subdomain fallback:

    # If someone visits /docs, send them to the docs site.
    /docs           https://docs.labtris.com            302
    /docs/*         https://docs.labtris.com/:splat     302
    # /install is served as a file directly — no rule needed.

Upload the folder. Cloudflare gives you a
`<project>.pages.dev` URL. Test:

    curl -fsSL https://<project>.pages.dev/install | head -3
    # #!/usr/bin/env bash
    # #
    # # Fetch Labtris and hand off to the real installer.

**3. Attach the domain.** Pages project → Custom domains → Add.
Type `labtris.com`. Cloudflare adds the DNS record itself if the
domain is on Cloudflare DNS; otherwise it prints a CNAME to paste
into your DNS provider.

Also add `www.labtris.com` if you want the `www` variant to work.

**4. Content-type for `install`.** Some browsers try to render it
as HTML. Add to `_headers`:

    /install
      Content-Type: text/x-shellscript
      Cache-Control: public, max-age=300

The 5-minute cache limits how long a bad push stays on the CDN
before rolling out.

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

## Updating the install script

`get.sh` is served from the Cloudflare Pages upload, not from
GitHub, so a change to `get.sh` in this repo does not deploy
automatically. Two options:

1. **Manual push after `get.sh` changes.** Re-upload the
   `labtris-com/` folder. Simple, but easy to forget.
2. **Wire Pages to this repo.** Change the project from Direct
   upload to a GitHub connection, point at this repo, set the
   build command to `cp get.sh labtris-com/install`. Then every
   `main` push redeploys.

Option 2 is one more click at setup and zero maintenance after.
Recommended.

## Testing before DNS lands

The `labtris.com/install` URL will not resolve until DNS + Pages is
set up. In the meantime `get.sh` is also reachable at the raw
GitHub URL, which is what the docs used to name:

    curl -fsSL https://raw.githubusercontent.com/rajeshgangam/my-own-pnetlab/main/get.sh | sudo bash

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
