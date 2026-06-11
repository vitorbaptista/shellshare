#!/usr/bin/env bash
# Release a new version: bump Cargo.toml, commit, tag, push.
# The tag push triggers .github/workflows/release.yml, which runs e2e tests,
# builds all platforms, creates the GitHub release, and publishes to npm.
#
# Usage:
#   scripts/release.sh           # bump patch version (2.0.6 -> 2.0.7)
#   scripts/release.sh 2.1.0     # release an explicit version
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" != "master" ]; then
  echo "error: releases must be cut from master (currently on $branch)" >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree is not clean" >&2
  exit 1
fi

git pull --ff-only origin master

current=$(grep -m1 '^version = ' Cargo.toml | sed 's/version = "\(.*\)"/\1/')

if [ $# -ge 1 ]; then
  version=$1
  if ! [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "error: version must be X.Y.Z, got '$version'" >&2
    exit 1
  fi
else
  IFS=. read -r major minor patch <<<"$current"
  version="$major.$minor.$((patch + 1))"
fi

if git rev-parse "v$version" >/dev/null 2>&1; then
  echo "error: tag v$version already exists" >&2
  exit 1
fi

echo "Releasing $current -> $version"
sed -i "0,/^version = \"$current\"/s//version = \"$version\"/" Cargo.toml
cargo check --quiet  # refresh Cargo.lock

git commit -am "chore: release v$version"
git tag "v$version"
git push --atomic origin master "v$version"

echo
echo "v$version pushed. CI takes it from here:"
echo "  https://github.com/vitorbaptista/shellshare/actions/workflows/release.yml"
