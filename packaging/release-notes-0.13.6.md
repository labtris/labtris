Labtris 0.13.6 — the EVE-NG migration no longer registers nothing, or copies everything.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

Two bugs from a real migration run, both of which wasted the transfer.

## Nothing was registered

`labtris-image` was invoked without the service's environment, so it fell
back to the default database URL in `labtris_api/config.py` and died with:

```
asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "pnl"
```

That happened **after** the first image had finished copying, and would have
happened identically on every image after it. The disks landed and nothing was
registered.

`LABTRIS_DATABASE_URL` now comes from `/etc/labtris/labtris.env`, and
`labtris-image` is probed **before** a single byte moves — if it cannot reach
the database the run stops immediately and says which setting to check.
`LABTRIS_SKIP_PLUGINS=1` also silences the ~150 lines of plugin registration
that were burying the error.

## One lab wanted 113GB

A lab using the `linux` and `paloalto` templates pulled in every installed
version of both — four Palo Alto images totalling 94GB among them. Selecting
one 4-node lab started a 113GB transfer.

Image resolution is now three steps, in order of what can be trusted:

1. the exact directory the `.unl` records on a node — EVE-NG usually stores
   it, and it is unambiguous, so no question is asked;
2. a template matching exactly one installed directory;
3. a template matching several — **it asks**, showing each version and its
   size.

```
  'paloalto' matches 4 installed versions (94.0GB for all)
      a  paloalto-12.1.2-rel                              5.1GB
      b  paloalto-12.2.5-rel                              5.2GB
      c  paloalto-13.0-main                              43.0GB
      d  paloalto-release-cosmos-nebula-11.1.main        40.6GB
```

On the run that prompted this, that turns 113.4GB into 48.3GB — and only you
know which version a lab was built against, so guessing would be worse than
asking.
