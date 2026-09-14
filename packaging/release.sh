#!/usr/bin/env bash
#
# Cut a release: build the ISO, render the handbook, tag, and publish.
#
#   ./packaging/release.sh                 # build, then publish
#   ./packaging/release.sh --dry-run       # build and checksum, publish nothing
#   ./packaging/release.sh --publish-only  # artefacts already built elsewhere
#
# Split into build and publish on purpose. The ISO has to be built on Ubuntu —
# the package closure is resolved with debootstrap against the target's own
# release, and a macOS host cannot produce it at all — but the GitHub token
# usually lives on the laptop. So the normal shape of a release is: build on
# the Linux box, copy dist/ back, publish from here with --publish-only.
#
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
cd "$ROOT"

MODE=full
case "${1:-}" in
  --dry-run)      MODE=dry ;;
  --publish-only) MODE=publish ;;
  "")             ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

VERSION=$(sed -n 's/^version *= *"\(.*\)"/\1/p' pyproject.toml | head -1)
[ -n "$VERSION" ] || { echo "no version in pyproject.toml" >&2; exit 1; }
TAG="v$VERSION"
ARCH=${ARCH:-amd64}
ISO="dist/labtris-${VERSION}-${ARCH}.iso"
SUMS="dist/labtris-${VERSION}-${ARCH}.sha256"
PDF="dist/labtris-handbook.pdf"
NOTES="packaging/release-notes-${VERSION}.md"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# A release built from a dirty tree cannot be reproduced from its own tag,
# which makes the tag a lie. Checked before the 30-minute build, not after.
if [ "$MODE" != "publish" ]; then
  [ -z "$(git status --porcelain)" ] || die "working tree is dirty — commit before releasing"
fi

[ -f "$NOTES" ] || die "$NOTES is missing — write the release notes first"

if [ "$MODE" != "publish" ]; then
  say "Building the ISO   (20-30 min)"
  ./packaging/iso/build.sh

  say "Rendering the handbook"
  python3 docs/build-pdf.py
fi

for f in "$ISO" "$SUMS" "$PDF"; do
  [ -f "$f" ] || die "$f is missing — build first, or copy dist/ from the build host"
done

say "Verifying the checksum against the file that is about to be published"
if command -v sha256sum >/dev/null 2>&1; then
  ( cd dist && sha256sum -c "$(basename "$SUMS")" ) || die "the ISO does not match its checksum"
else
  ( cd dist && shasum -a 256 -c "$(basename "$SUMS")" ) || die "the ISO does not match its checksum"
fi

if [ "$MODE" = "dry" ]; then
  say "Dry run — nothing published"
  ls -lh "$ISO" "$SUMS" "$PDF"
  exit 0
fi

command -v gh >/dev/null 2>&1 || die "gh is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated"

if git rev-parse "$TAG" >/dev/null 2>&1; then
  say "Tag $TAG already exists — reusing it"
else
  say "Tagging $TAG"
  git tag -a "$TAG" -m "Labtris $VERSION"
  git push origin "$TAG"
fi

# GitHub refuses any release asset of 2 GiB or more, and the ISO is larger
# than that by design: the whole point is that every package is on the disc.
# So it goes up in parts. The checksum published alongside is of the *whole*
# image, which is what someone can check after reassembling — a per-part
# checksum would verify the download and tell them nothing about the result.
LIMIT=$((1900 * 1024 * 1024))
ISO_SIZE=$(wc -c < "$ISO" | tr -d ' ')
ASSETS=("$SUMS" "$PDF")

if [ "$ISO_SIZE" -ge "$LIMIT" ]; then
  say "ISO is $((ISO_SIZE / 1024 / 1024)) MB — over GitHub's 2 GiB asset cap, splitting"
  rm -f "$ISO".part-*
  split -b "$LIMIT" -d -a 2 "$ISO" "$ISO.part-"
  PARTS=("$ISO".part-*)
  # Reassemble here and compare, so the thing being published is known to
  # produce the original rather than assumed to.
  say "  verifying the parts rebuild the image"
  cat "${PARTS[@]}" > "$ISO.rebuilt"
  if command -v sha256sum >/dev/null 2>&1; then
    A=$(sha256sum "$ISO" | awk '{print $1}'); B=$(sha256sum "$ISO.rebuilt" | awk '{print $1}')
  else
    A=$(shasum -a 256 "$ISO" | awk '{print $1}'); B=$(shasum -a 256 "$ISO.rebuilt" | awk '{print $1}')
  fi
  rm -f "$ISO.rebuilt"
  [ "$A" = "$B" ] || die "the split parts do not rebuild the ISO — refusing to publish them"
  say "  ${#PARTS[@]} parts, verified"
  ASSETS+=("${PARTS[@]}")
else
  ASSETS+=("$ISO")
fi

say "Publishing the release"
gh release create "$TAG" \
  --title "Labtris $VERSION" \
  --notes-file "$NOTES" \
  "${ASSETS[@]}"

say "Published"
gh release view "$TAG" --json url --jq .url
