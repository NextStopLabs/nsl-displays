#!/bin/bash
# push-release: build current HEAD into an OTA bundle and upload it to the portal.
#
# Usage (bash/zsh/sh - or run directly from fish with ./tools/push-release.sh):
#   NSL_USER_TOKEN="<token>" NSL_ORG_ID="<org-id>" ./tools/push-release.sh [--channel stable] [--notes "..."] [--version X]
#
# The portal signs the bundle automatically (NSL_RELEASE_PRIVATE_KEY on the host).
# Then: portal -> Remote control -> select controller -> Update now.
set -euo pipefail

PORTAL="${PORTAL_URL:-https://nsldisplays.com}"
CHANNEL="stable"
NOTES=""
VERSION=""

while [ $# -gt 0 ]; do
  case "$1" in
    --channel) CHANNEL="${2:?}"; shift 2;;
    --notes) NOTES="${2:?}"; shift 2;;
    --version) VERSION="${2:?}"; shift 2;;
    -h|--help) sed -n '1,9p' "$0"; exit 0;;
    *) echo "unknown flag: $1" >&2; exit 1;;
  esac
done

: "${NSL_USER_TOKEN:?set NSL_USER_TOKEN (portal sign-in token)}"
: "${NSL_ORG_ID:?set NSL_ORG_ID (organisation id)}"

cd "$(dirname "$0")/.."
HEAD_SHA="$(git rev-parse --short HEAD)"
if [ -z "$VERSION" ]; then
  VERSION="$(date +%Y.%m.%d)-$HEAD_SHA"
fi
if ! git diff --quiet HEAD -- . ':!tfl-ibus-announcements'; then
  echo "WARNING: working tree has uncommitted changes (release still builds from HEAD)" >&2
fi

BUNDLE="/tmp/nsl-release-${VERSION}.tar.gz"
git archive --format=tar.gz HEAD -o "$BUNDLE"
echo "built $BUNDLE ($(du -h "$BUNDLE" | cut -f1)) from $HEAD_SHA"

curl -s -X POST "$PORTAL/api/admin/releases" \
  -H "Authorization: Bearer $NSL_USER_TOKEN" \
  -F organisation_id="$NSL_ORG_ID" -F version="$VERSION" -F channel="$CHANNEL" \
  -F notes="${NOTES:-$HEAD_SHA}" \
  -F file="@$BUNDLE"
echo
echo "done: portal -> Remote control -> Update now (bus polls every ~10 s when online)"
