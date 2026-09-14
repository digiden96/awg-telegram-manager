#!/usr/bin/env bash
set -euo pipefail

GO_COMMIT=b5928efb6ca19f0153958460c3d141f04abc5c2e
TOOLS_COMMIT=ee0f0a9aa34ff0a0da4b3433b9512781cfe02843
build_root=$(mktemp -d)
trap 'rm -rf -- "$build_root"' EXIT

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential git curl jq pkg-config libmnl-dev

architecture=$(dpkg --print-architecture)
case "$architecture" in amd64) go_arch=amd64 ;; arm64) go_arch=arm64 ;; *) echo "Unsupported architecture: $architecture" >&2; exit 1 ;; esac
curl -fsSL https://go.dev/dl/?mode=json -o "$build_root/go.json"
read -r go_file go_sum < <(jq -r --arg arch "$go_arch" '[.[]|select(.stable)|.files[]|select(.os=="linux" and .arch==$arch and .kind=="archive")][0]|[.filename,.sha256]|@tsv' "$build_root/go.json")
curl -fsSL "https://go.dev/dl/$go_file" -o "$build_root/$go_file"
printf '%s  %s\n' "$go_sum" "$build_root/$go_file" | sha256sum -c -
tar -xzf "$build_root/$go_file" -C "$build_root"
export PATH="$build_root/go/bin:$PATH"

git init -q "$build_root/go-src"
git -C "$build_root/go-src" remote add origin https://github.com/amnezia-vpn/amneziawg-go.git
git -C "$build_root/go-src" fetch -q --depth 1 origin "$GO_COMMIT"
git -C "$build_root/go-src" checkout -q FETCH_HEAD
make -C "$build_root/go-src"

git init -q "$build_root/tools-src"
git -C "$build_root/tools-src" remote add origin https://github.com/amnezia-vpn/amneziawg-tools.git
git -C "$build_root/tools-src" fetch -q --depth 1 origin "$TOOLS_COMMIT"
git -C "$build_root/tools-src" checkout -q FETCH_HEAD
make -C "$build_root/tools-src/src"

install -m 0755 "$build_root/go-src/amneziawg-go" /usr/local/bin/amneziawg-go
make -C "$build_root/tools-src/src" PREFIX=/usr/local WITH_WGQUICK=yes WITH_SYSTEMDUNITS=yes install
/usr/local/bin/amneziawg-go --version
/usr/local/bin/awg --version
