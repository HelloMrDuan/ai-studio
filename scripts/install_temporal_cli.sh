#!/usr/bin/env bash
set -Eeuo pipefail

VERSION="${TEMPORAL_CLI_VERSION:-1.8.3}"
BIN_DIR="${TEMPORAL_BIN_DIR:-/root/autodl-tmp/bin}"
ARCHIVE="temporal_cli_${VERSION}_linux_amd64.tar.gz"
URL="https://github.com/temporalio/cli/releases/download/v${VERSION}/${ARCHIVE}"

# Pinned checksum for the default 1.8.3 linux_amd64 release. Override only when
# intentionally selecting another version and supplying its published checksum.
if [[ "$VERSION" == "1.8.3" ]]; then
  EXPECTED_SHA="${TEMPORAL_CLI_SHA256:-6f0afac1e9ddea71f480c43a49f5db5167a244c21db923707f069a79bcabdfea}"
else
  EXPECTED_SHA="${TEMPORAL_CLI_SHA256:-}"
fi

mkdir -p "$BIN_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl --noproxy '*' -fL --retry 3 --retry-delay 2 "$URL" -o "$TMP/$ARCHIVE"
if [[ -n "$EXPECTED_SHA" ]]; then
  echo "$EXPECTED_SHA  $TMP/$ARCHIVE" | sha256sum -c -
else
  echo "WARNING: no checksum supplied for Temporal CLI $VERSION" >&2
fi

tar -xzf "$TMP/$ARCHIVE" -C "$TMP"
if [[ ! -f "$TMP/temporal" ]]; then
  echo "Temporal archive did not contain temporal binary" >&2
  exit 1
fi
install -m 0755 "$TMP/temporal" "$BIN_DIR/temporal"
"$BIN_DIR/temporal" --version
