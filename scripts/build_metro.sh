#!/usr/bin/env bash
# Build METRo 4.0.1 into a self-contained directory.
#
# Usage: scripts/build_metro.sh <install-prefix>
#
# METRo needs the *system* Python (apt's python3-libxml2 and python3-numpy only
# exist for it), so this script deliberately ignores any other python3 on PATH
# (e.g. the 3.11 from actions/setup-python). METRo 4.0.1 predates Python 3.12
# and numpy 1.24: it imports `distutils` (removed in 3.12) and uses
# `numpy.float` (removed in numpy 1.24), so we apply a small patch for both
# (scripts/metro_patches/py312-compat.patch).
#
# Needs: gfortran swig python3-dev python3-numpy python3-libxml2 patch cpio
set -euo pipefail

PREFIX=$(realpath -m "${1:?usage: build_metro.sh <install-prefix>}")
PY=${METRO_PYTHON:-/usr/bin/python3}
# Pinned to the metro-4.0.1 release commit (Oct 2021).
METRO_REPO=https://framagit.org/metroprojects/metro.git
METRO_COMMIT=1af5e138634cbf3667f7b6c0adaa316035504d3c
HERE=$(cd "$(dirname "$0")" && pwd)

# Make `python3`/`python3-config` inside METRo's scripts mean the system Python.
shimdir=$(mktemp -d)
ln -s "$PY" "$shimdir/python3"
export PATH="$shimdir:$(dirname "$PY"):$PATH"
export PYTHON_INCLUDE=$("$PY" -c "import sysconfig; print(sysconfig.get_paths()['include'])")

work=$(mktemp -d)
git clone --quiet "$METRO_REPO" "$work/metro"
cd "$work/metro"
git checkout --quiet "$METRO_COMMIT"
patch -p1 < "$HERE/metro_patches/py312-compat.patch"

rm -rf "$PREFIX"
mkdir -p "$PREFIX"
./setup.sh "$PREFIX"

# Self-test: METRo must be able to forecast its own bundled example.
cd "$PREFIX/metro/usr/bin"
"$PY" ./metro --selftest
echo "METRo built and passed its self-test: $PREFIX/metro"
