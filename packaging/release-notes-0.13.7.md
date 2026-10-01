Labtris 0.13.7 — the EVE-NG lab import works, and you can name labs as they arrive.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

## The import failed

```
==> Importing lab RegressionTestBed_main
No such command 'import'.
```

There is no `labtris lab import`. The importer is an API endpoint
(`POST /labs/import/topology`) and reaching it needs a logged-in session,
which the migration — running as the service user with the database to hand
and no token — does not have.

It now imports straight into the database, reusing the API's own `parse_unl`
and `realize_plan` rather than keeping a second implementation of either.
Same code path, no second thing to keep correct.

## Name labs as they come across

EVE-NG lab names carry whatever they carried — spaces, a stray timestamp,
somebody's first name:

```
  name for this lab [shreyas_1746382452984]:
```

Press Enter to keep it, or type a new one. That moment is the only one where
you are looking at the list and know which lab is which; afterwards it is a
row in a table to find again. `--keep-names` skips the prompt.

## The log spam is actually gone this time

0.13.6 set `LABTRIS_SKIP_PLUGINS`, which is read by nothing. `plugins.py`
looks at `LABTRIS_PLUGINS_DISABLED`, a comma-separated list of plugin names.
Every invocation kept printing ~150 lines of AWS service registration, which
is what buried the database error in the first place.

`packaging/seed-demo-pods.py` sets the same wrong name and has done since it
was written; it is corrected here too.

## Still outstanding

Four working disks failed to flatten on the reported run
(`flatten fd3d0b69/3` and three more) with no reason given. The flatten step
needs to report *why* `qemu-img convert` failed — out of space on the EVE-NG
box is the likely cause and the message should say so. Not fixed in this
release.
