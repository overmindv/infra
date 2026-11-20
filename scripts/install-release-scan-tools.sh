#!/usr/bin/env bash
set -euo pipefail

bin="${1:?pass the directory for scanner binaries}"
mkdir -p "$bin"
tmp="$(mktemp -d)"
trap 'rm -f -- "$tmp/trivy.tgz" "$tmp/gitleaks.tgz"; rmdir -- "$tmp"' EXIT

download() {
  local url="$1" sha="$2" name="$3"
  curl --proto '=https' --tlsv1.2 --silent --show-error --fail --location \
    --retry 3 --output "$tmp/$name.tgz" "$url"
  printf '%s  %s\n' "$sha" "$tmp/$name.tgz" | sha256sum --check --status
  tar -xzOf "$tmp/$name.tgz" "$name" > "$bin/$name"
  chmod 0755 "$bin/$name"
}

download https://github.com/aquasecurity/trivy/releases/download/v0.76.2/trivy_0.76.2_Linux-64bit.tar.gz \
  bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf trivy
download https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
  551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb gitleaks
GOBIN="$(cd "$bin" && pwd)" GOTOOLCHAIN=local go install golang.org/x/vuln/cmd/govulncheck@v1.10.0
