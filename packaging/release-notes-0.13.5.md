Labtris 0.13.5 — labs are numbered, images are lettered.

```
curl -fsSL https://labtris.com/upgrade | sudo bash
sudo -u labtris labtris-migrate-eveng
```

Both lists in the EVE-NG migrator were numbered from 1, so typing `3` could
have meant the third lab or the third image and there was no way to tell from
the input. Labs now take digits and images take letters:

```
Labs (7)
    #  name                             nodes  templates
    1  ccie-rs-lab1                        14  vios, viosl2, veos
    2  junos-basics                         6  vmxvcp, vmxvfp

Images (12, 86.4GB total)
   id  directory                              size
    a  vios-adventerprisek9-m-15.6.2T        1.2GB
    b  viosl2-adventerprisek9-m-15.2         1.1GB
```

So `1 2` is labs and `a c` is images, and a selection can only mean one list.

Ranges work for both — `2-4` over labs, `a-d` over images, and `y-ab` across
the letter rollover. Letters are case-insensitive, and a word that is not a
label is still treated as a name search, so `vmx` picks every image whose
name contains it. Anything that matches neither is named and ignored rather
than silently dropped.

Everything else in 0.13.x is unchanged.
