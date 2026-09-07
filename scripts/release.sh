#!/usr/bin/env bash
# Build and tag the images with the current commit, so a deploy can be undone.
#
# Rolling back means starting the previous image, which only works if that
# image still exists under a name. `docker compose build` alone overwrites a
# single implicit tag, leaving nothing to go back to — that is what this fixes.
#
#   scripts/release.sh              # tag = current short SHA
#   scripts/release.sh v0.3.0       # explicit tag
#
# Then deploy, and roll back if it misbehaves:
#   APP_TAG=<tag> MODELS_TAG=<tag> docker compose up -d
set -euo pipefail

cd "$(dirname "$0")/.."

tag="${1:-$(git rev-parse --short HEAD)}"

# A dirty tree makes the tag a lie: the SHA would not describe what is inside
# the image, and the rollback target would be unreproducible.
if [ -z "${ALLOW_DIRTY:-}" ] && ! git diff --quiet HEAD 2>/dev/null; then
    echo "working tree is dirty — commit first, or set ALLOW_DIRTY=1" >&2
    exit 1
fi

echo "building $tag"
APP_TAG="$tag" MODELS_TAG="$tag" docker compose build app models

# Also move :dev so a plain `docker compose up` runs what was just built.
docker tag "docs-rag-app:$tag" docs-rag-app:dev
docker tag "docs-rag-models:$tag" docs-rag-models:dev

echo
echo "배포:   APP_TAG=$tag MODELS_TAG=$tag docker compose up -d"
echo "롤백:   APP_TAG=<이전태그> MODELS_TAG=<이전태그> docker compose up -d"
echo
echo "보관 중인 태그:"
docker images docs-rag-app --format "  docs-rag-app:{{.Tag}}  ({{.CreatedSince}})"
