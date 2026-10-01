Labtris 0.13.8 — imported vendor nodes use the images you just copied.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
```

## The copied images were not being used

This is the one that mattered. A migration copied 100GB of Palo Alto and
Nexus disks, registered them, imported the lab — and every vendor node still
arrived as an alpine placeholder:

```
note: P2-PA-5220: paloalto is a vendor appliance image — imported as a placeholder
note: NXOS: nxosv9k is a vendor appliance image — imported as a placeholder
```

`parse_unl` maps a fixed set of EVE-NG templates and turns everything else
into a placeholder. That is right for a bare import and wrong immediately
after a migration, which is the entire reason the disks were copied.

The import now looks at what is registered and points each node at it:

```
  P1-PA-5220: using paloalto-12.1.2-rel (also registered: paloalto-12.2.5-rel,
              paloalto-release-cosmos-nebula-11.1.main — switch it on the canvas if wrong)
  NXOS: using nxosv9k-9.2.4
```

Where several versions are registered, one is picked and **the others are
named**. Sorting by name is not version order — among `12.1.2-rel`,
`12.2.5-rel`, `13.0-main` and `release-cosmos-nebula-11.1.main` the last
sorts highest and is the oldest — so it picks deterministically and tells you
what else it could have used rather than pretending to have chosen well. A
template with nothing registered still becomes a placeholder, which is
correct.

## Re-running no longer reports false failures

```
labtris-image: template name 'paloalto-13.0-main' already exists
3 step(s) failed: register linux-ubuntu-osboxes, register nxosv9k-9.2.4, register paloalto-13.0-main
```

Those three images were present and correct. Re-running a migration is
normal — transfers get interrupted, and you come back for more labs — and
rsync already skips what it has, but `labtris-image add` exits non-zero on an
existing template. An already-registered image now counts as done.

Its output is also captured and only shown when it matters, so a run reads as
a list of what happened rather than a wall of registration noise.
