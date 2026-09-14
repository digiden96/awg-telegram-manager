#!/usr/bin/env bash
set -euo pipefail
if command -v node >/dev/null && node -e 'process.exit(Number(process.versions.node.split(".")[0])>=22?0:1)'; then exit 0; fi
case $(dpkg --print-architecture) in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) exit 1 ;; esac
temp_dir=$(mktemp -d)
trap 'rm -rf -- "$temp_dir"' EXIT
curl -fsSL https://nodejs.org/dist/index.json -o "$temp_dir/releases.json"
version=$(jq -r '[.[]|select(.version|startswith("v24."))][0].version' "$temp_dir/releases.json")
[[ $version =~ ^v24\.[0-9]+\.[0-9]+$ ]] || exit 1
archive="node-$version-linux-$arch.tar.xz"
curl -fsSL "https://nodejs.org/dist/$version/SHASUMS256.txt" -o "$temp_dir/sums"
curl -fsSL "https://nodejs.org/dist/$version/$archive" -o "$temp_dir/$archive"
(cd "$temp_dir"; grep " $archive$" sums | sha256sum -c -)
install -d -m 0755 /opt/awg-manager-node
tar -xJf "$temp_dir/$archive" --strip-components=1 -C /opt/awg-manager-node
/opt/awg-manager-node/bin/node --version
