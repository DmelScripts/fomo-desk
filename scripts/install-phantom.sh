#!/bin/bash
# Install the pinned Node runtime and Phantom CLI into this service slug.
# No login, no wallet call, no extra Render service.
set -euo pipefail
cd "$(dirname "$0")/.."

NODE_VERSION="22.21.0"
NODE_SHA256="71a04f4b9144870c9407b8019fe912514229e50246bc706862eded3ac8e9025d"
TARBALL="node-v${NODE_VERSION}-linux-x64.tar.xz"
URL="https://nodejs.org/dist/v${NODE_VERSION}/${TARBALL}"
TMP="$(mktemp)"

curl -fsSL "$URL" -o "$TMP"
echo "${NODE_SHA256}  ${TMP}" | sha256sum -c -
rm -rf .tools/node
mkdir -p .tools/node
tar -xJf "$TMP" -C .tools/node --strip-components=1
rm -f "$TMP"
chmod 755 .tools/node/bin/node .tools/node/bin/npm

export PATH="$PWD/.tools/node/bin:$PATH"
npm ci --ignore-scripts
chmod 755 node_modules/@phantom/cli/dist/bin.js
test -x node_modules/.bin/phantom
test -x .tools/node/bin/node
node_modules/.bin/phantom --version >/dev/null
