#!/bin/sh
set -eu

: "${COMMIT_SHA:?set COMMIT_SHA to the immutable git revision}"
IMAGE="${IMAGE:-ghcr.io/shoonia69/teambook}"

docker build \
  --build-arg "COMMIT_SHA=$COMMIT_SHA" \
  -t "$IMAGE:$COMMIT_SHA" \
  -t "$IMAGE:latest" \
  .