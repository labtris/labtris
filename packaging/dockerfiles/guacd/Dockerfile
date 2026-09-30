# Build guacd from source and emit a .deb. See packaging/build-guacd.sh,
# which is what you actually run — this expects a source tree already
# unpacked beside it.
#
# WHY THIS EXISTS
#
# Ubuntu 26.04 (resolute) ships no guacd, and no libguac-client-vnc0 or
# libguac-client-rdp0 either. Noble's 1.3.0-1.3ubuntu1 was the last build
# Ubuntu made. packaging/packages.txt installs all three, so on resolute the
# native install comes up with working serial consoles and dead VNC and RDP
# tabs. That is one of the two reasons get.sh refuses 26.04.
#
# Resolute also has freerdp3-dev (3.32.0) and no freerdp2-dev, so this has
# to build against FreeRDP 3. Upstream prints, and means, this:
#
#   Support for FreeRDP 3.x is currently experimental. Some features, like
#   RemoteApp, have known issues. If not testing, consider using FreeRDP 2.x.
#
# Labtris does not use RemoteApp — RDP here is a console onto a Windows
# guest, and VNC onto QEMU — so the trade is acceptable, but it is a real
# caveat and not one to discover later. Verified 2026-09-30: 1.6.0
# configures against 3.32.0 with RDP, VNC, SSH, Telnet and Kubernetes all
# enabled.
#
# THE SOURCE IS UNPACKED ON THE HOST, NOT HERE
#
# Extracting the tarball inside a linux/amd64 container on an arm64 machine
# fails: qemu-user does not implement the syscalls GNU tar uses, and every
# member fails with "Cannot open: Function not implemented". The host
# unpacks it natively and this COPYs the tree in, which also keeps the
# checksum check outside the build where its failure is visible.
FROM ubuntu:26.04 AS build

ARG DEBIAN_FRONTEND=noninteractive
ARG GUAC_VERSION=1.6.0
# Bumped by hand, not derived: it is the packaging revision, and deriving it
# from the date would make every rebuild a new version.
ARG DEB_REVISION=labtris1

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential autoconf automake libtool libtool-bin pkg-config \
      dpkg-dev fakeroot \
      libcairo2-dev libjpeg-turbo8-dev libpng-dev \
      libossp-uuid-dev libvncserver-dev libwebp-dev \
      libssh2-1-dev libssl-dev libpango1.0-dev libswscale-dev \
      libavcodec-dev libavformat-dev libavutil-dev \
      libpulse-dev libwebsockets-dev \
      libtelnet-dev libvorbis-dev \
 && rm -rf /var/lib/apt/lists/*

COPY src /src/server
WORKDIR /src/server

# Fail loudly on a protocol that did not make it in. A guacd built without
# its VNC plugin still installs, still starts, and still fails every console
# connection at runtime with nothing pointing at the build — which is the
# exact failure this whole package exists to prevent.
# RDP IS OFF, AND NOT BY OVERSIGHT
#
# Guacamole 1.6.0's RDP plugin does not compile against FreeRDP 3, which is
# the only FreeRDP resolute has. FreeRDP 3 removed rdp_freerdp.input and the
# plugin still uses it:
#
#   keyboard.c:110: error: 'freerdp' has no member named 'input'
#   input-queue.c:79: error: 'freerdp' has no member named 'input'
#
# configure still reports "RDP ... yes", because pkg-config finds freerdp3 and
# configure never compiles anything against it — which is exactly why the
# check below is not trusted on its own and the real build is the gate.
#
# The alternatives were worse. Patching the plugin is upstream porting work
# carried in our tree forever; building FreeRDP 2 from source alongside it
# means bundling an EOL RDP stack and owning its CVE stream. So VNC, SSH and
# telnet are built and RDP is left out, deliberately and visibly.
#
# Flip WITH_RDP=yes once upstream lands FreeRDP 3 support — the gate then
# requires it and the build fails loudly rather than quietly shipping without.
ARG WITH_RDP=no

# CFLAGS=-Wno-error: 1.6.0 builds with -Werror, and resolute's glibc already
# defines _XOPEN_SOURCE as 800 while this tree defines its own, so every
# translation unit fails on "'_XOPEN_SOURCE' redefined". A benign collision
# between an older release and a newer libc, not a defect in the code being
# built. Warnings still print; they just stop being fatal.
RUN set -eux; \
    if [ "$WITH_RDP" = yes ]; then RDP_FLAG=--with-rdp; REQUIRED="RDP VNC SSH Telnet"; \
    else RDP_FLAG=--without-rdp; REQUIRED="VNC SSH Telnet"; fi; \
    ./configure --prefix=/usr --with-systemd-dir=/lib/systemd/system \
      $RDP_FLAG CFLAGS="-Wno-error -O2" 2>&1 | tee /tmp/configure.log; \
    for proto in $REQUIRED; do \
      grep -qE "^ *$proto \.+ yes" /tmp/configure.log \
        || { echo "FATAL: $proto plugin did not configure — refusing to package"; \
             grep -E "^ *(RDP|VNC|SSH|Telnet|Kubernetes) " /tmp/configure.log; exit 1; }; \
    done; \
    echo "configured with: $REQUIRED"

RUN make -j"$(nproc)" && make install DESTDIR=/pkg

# Hand-rolled rather than debhelper: there is no debian/ dir upstream, and a
# full source package would be a second thing to maintain for one binary.
# dpkg-shlibdeps still computes the real Depends, so the package refuses to
# install without its libraries instead of failing at first connect.
# dpkg-shlibdeps needs a debian/control with BOTH a Source: and a binary
# Package: stanza, and it needs its failures to be fatal. The first version of
# this had `|| true` with stderr to /dev/null, so when it silently produced
# nothing the package shipped with no Depends at all: it installed fine on a
# bare 26.04 and then died on first run with
#
#   guacd: error while loading shared libraries: libcairo.so.2
#
# which is the precise failure this package was written to stop. So: no
# swallowing, and an explicit check that the result is non-empty.
RUN set -eux; \
    mkdir -p /pkg/DEBIAN; \
    cd /pkg; \
    mkdir -p debian; \
    printf 'Source: guacd\n\nPackage: guacd\nArchitecture: any\n' > debian/control; \
    find usr/sbin usr/lib -type f \( -name 'guacd' -o -name '*.so.*' \) > /tmp/binaries; \
    # -l/pkg/usr/lib: the plugins link libguac.so.25 and libguac-terminal.so.2,
    # which are in THIS package and therefore not installed anywhere yet.
    # Without it shlibdeps errors with "cannot find library libguac.so.25" and
    # emits nothing at all — not a partial answer, nothing.
    # --ignore-missing-info: with -l the internal libs are now FOUND, but they
    # belong to no installed package, so shlibdeps aborts with "no dependency
    # information found". This tolerates that for our own libraries. It could
    # in principle hide a real external library too, which is why the libcairo
    # assertion below exists — it proves external resolution actually happened
    # rather than trusting a flag that also suppresses errors.
    dpkg-shlibdeps -O -l/pkg/usr/lib --ignore-missing-info \
      $(cat /tmp/binaries) > /tmp/raw 2>/tmp/err \
      || { echo "dpkg-shlibdeps failed:"; cat /tmp/err; exit 1; }; \
    sed 's/^shlibs:Depends=//' /tmp/raw > /tmp/deps; \
    DEPS="$(cat /tmp/deps)"; \
    rm -rf debian; \
    [ -n "$DEPS" ] || { echo "FATAL: dpkg-shlibdeps produced no dependencies"; exit 1; }; \
    case "$DEPS" in *libcairo*) : ;; \
      *) echo "FATAL: Depends has no libcairo — shlibdeps did not see the real libraries"; \
         echo "got: $DEPS"; exit 1 ;; esac; \
    echo "computed Depends: $DEPS"; \
    ARCH="$(dpkg --print-architecture)"; \
    INSTALLED="$(du -sk /pkg --exclude=DEBIAN | cut -f1)"; \
    printf 'Package: guacd\n' > DEBIAN/control; \
    printf 'Version: %s-%s\n' "${GUAC_VERSION}" "${DEB_REVISION}" >> DEBIAN/control; \
    printf 'Architecture: %s\n' "$ARCH" >> DEBIAN/control; \
    printf 'Maintainer: Labtris <noreply@labtris.com>\n' >> DEBIAN/control; \
    printf 'Installed-Size: %s\n' "$INSTALLED" >> DEBIAN/control; \
    [ -n "$DEPS" ] && printf 'Depends: %s\n' "$DEPS" >> DEBIAN/control; \
    printf 'Provides: libguac-client-vnc0, libguac-client-ssh0, libguac-client-telnet0\n' >> DEBIAN/control; \
    printf 'Section: net\nPriority: optional\n' >> DEBIAN/control; \
    printf 'Homepage: https://guacamole.apache.org/\n' >> DEBIAN/control; \
    printf 'Description: Apache Guacamole proxy daemon (built from source)\n' >> DEBIAN/control; \
    printf ' Ubuntu stopped packaging guacd after noble, so Labtris builds it\n' >> DEBIAN/control; \
    printf ' from the Apache release tarball. Includes libguac and the RDP, VNC,\n' >> DEBIAN/control; \
    printf ' SSH and telnet client plugins in one package rather than the split\n' >> DEBIAN/control; \
    printf ' Ubuntu used, which is why it Provides those names. RDP is NOT included:\n' >> DEBIAN/control; \
    printf ' 1.6.0 does not compile against FreeRDP 3, the only FreeRDP resolute has.\n' >> DEBIAN/control; \
    printf '#!/bin/sh\nset -e\nldconfig\n' > DEBIAN/postinst; \
    printf '#!/bin/sh\nset -e\nldconfig\n' > DEBIAN/postrm; \
    chmod 0755 DEBIAN/postinst DEBIAN/postrm; \
    cat DEBIAN/control

RUN mkdir -p /out \
 && dpkg-deb --root-owner-group --build /pkg \
      "/out/guacd_${GUAC_VERSION}-${DEB_REVISION}_$(dpkg --print-architecture).deb" \
 && dpkg-deb --info /out/*.deb \
 && dpkg-deb --contents /out/*.deb | grep -E 'guacd|libguac' | head -20

# A scratch stage so `docker build --output` can emit just the .deb.
FROM scratch AS artifact
COPY --from=build /out/ /
