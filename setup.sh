#!/usr/bin/env bash
# NexEncoder one-command setup for Ubuntu 24.04 LTS. Idempotent: safe to re-run.
#
# Packages come from APT only: no pip, no venv, no Node, no containers.
# Installs Python, GStreamer, the local Prometheus stack, a pinned MediaMTX
# binary, and the console plus channel units. FFmpeg 9.0.2 is downloaded and
# built with DeckLink, libx264, and libopus. TSDuck and Desktop Video are
# checked for, not downloaded (Desktop Video needs a Blackmagic login).
# Rebuild FFmpeg with NEXENCODER_FORCE_FFMPEG_BUILD=1.
set -euo pipefail
cd "$(dirname "$0")"

log() { printf '[setup] %s\n' "$*"; }
die() { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
  log "WARNING: targets Ubuntu 24.04 LTS (found ${PRETTY_NAME:-unknown OS}); continuing"
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "run as root or install sudo"
  SUDO="sudo"
fi

APT_PACKAGES=(python3 python3-pytest)
missing=()
for pkg in "${APT_PACKAGES[@]}"; do
  dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [ "${#missing[@]}" -gt 0 ]; then
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found; install manually: ${missing[*]}"
  log "installing via apt: ${missing[*]}"
  $SUDO apt-get update -qq
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer is required (found $(python3 -V 2>&1))"

pin_listen() {
  local file="$1" addr="$2"
  [ -f "$file" ] || return 0
  if grep -q 'nexencoder-listen' "$file"; then
    return 0
  fi
  printf '\n# nexencoder-listen\nARGS="--web.listen-address=%s"\n' "$addr" | $SUDO tee -a "$file" >/dev/null
}

install_ffmpeg() {
  local version=9.0.2
  local url="https://ffmpeg.org/releases/ffmpeg-${version}.tar.xz"
  local sha=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
  local prefix=/usr/local
  local bin="${prefix}/bin/ffmpeg"
  local sdk="" dir
  for dir in /opt/decklink-sdk /usr/include; do
    if [ -f "${dir}/DeckLinkAPI.h" ] && [ -f "${dir}/DeckLinkAPIDispatch.cpp" ] && [ -f "${dir}/DeckLinkAPIVersion.h" ]; then
      sdk="$dir"
      break
    fi
  done
  if [ -z "$sdk" ]; then
    die "DeckLink SDK headers not found. Copy the Desktop Video Linux include directory to /opt/decklink-sdk (DeckLinkAPI.h, DeckLinkAPIDispatch.cpp, DeckLinkAPIVersion.h)."
  fi
  if [ "${NEXENCODER_FORCE_FFMPEG_BUILD:-}" != "1" ] && [ -x "$bin" ]; then
    local ver conf
    ver="$("$bin" -version 2>/dev/null | head -n 1 || true)"
    conf="$("$bin" -buildconf 2>/dev/null || true)"
    if [[ "$ver" == *"$version"* && "$conf" == *"--enable-decklink"* && "$conf" == *"--enable-libx264"* && "$conf" == *"--enable-libopus"* ]]; then
      log "FFmpeg ${version} with DeckLink is already installed (${bin})"
      return 0
    fi
  fi
  local jobs work src
  jobs="$(nproc 2>/dev/null || echo 2)"
  work="$(mktemp -d)"
  log "downloading FFmpeg ${version}"
  curl -fsSL -o "${work}/ffmpeg.tar.xz" "$url"
  echo "${sha}  ${work}/ffmpeg.tar.xz" | sha256sum -c -
  tar -xJf "${work}/ffmpeg.tar.xz" -C "$work"
  src="${work}/ffmpeg-${version}"
  log "building FFmpeg ${version} with DeckLink headers in ${sdk}"
  (
    cd "$src"
    ./configure \
      --prefix="$prefix" \
      --bindir="${prefix}/bin" \
      --libdir="${prefix}/lib" \
      --enable-shared \
      --disable-static \
      --enable-rpath \
      --disable-doc \
      --disable-ffplay \
      --enable-gpl \
      --enable-libx264 \
      --enable-openssl \
      --enable-libopus \
      --enable-decklink \
      --extra-cflags="-I${sdk}" \
      --extra-cxxflags="-I${sdk}"
    make -j"$jobs"
    $SUDO make install
  )
  echo "${prefix}/lib" | $SUDO tee /etc/ld.so.conf.d/nexencoder-ffmpeg.conf >/dev/null
  $SUDO ldconfig
  rm -rf "$work"
  "$bin" -buildconf | grep -q -- '--enable-decklink'
  log "installed ${bin} ($("$bin" -version | head -n 1))"
}

if [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "24.04" ] && command -v apt-get >/dev/null 2>&1; then
  prom_missing=()
  for pkg in prometheus prometheus-node-exporter; do
    dpkg -s "$pkg" >/dev/null 2>&1 || prom_missing+=("$pkg")
  done
  if [ "${#prom_missing[@]}" -gt 0 ]; then
    log "installing via apt: ${prom_missing[*]}"
    $SUDO apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${prom_missing[@]}"
  fi
  if [ -f /etc/prometheus/prometheus.yml ] && grep -q 'job_name: nexencoder' /etc/prometheus/prometheus.yml; then
    log "prometheus scrape config already includes nexencoder"
  else
    if [ -f /etc/prometheus/prometheus.yml ]; then
      $SUDO cp -a /etc/prometheus/prometheus.yml "/etc/prometheus/prometheus.yml.bak.$(date +%Y%m%d%H%M%S)"
    fi
    $SUDO mkdir -p /etc/prometheus
    $SUDO cp config/prometheus.yml /etc/prometheus/prometheus.yml
    log "installed /etc/prometheus/prometheus.yml"
  fi
  pin_listen /etc/default/prometheus "127.0.0.1:9090"
  pin_listen /etc/default/prometheus-node-exporter "127.0.0.1:9100"
  unit_tmp="$(mktemp)"
  sed "s|@ROOT@|$(pwd)|g" systemd/nexencoder-console.service > "$unit_tmp"
  $SUDO cp "$unit_tmp" /etc/systemd/system/nexencoder-console.service
  rm -f "$unit_tmp"
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable prometheus prometheus-node-exporter nexencoder-console
    $SUDO systemctl restart prometheus prometheus-node-exporter nexencoder-console \
      || log "WARNING: could not start prometheus or the console"
  fi

  media_missing=()
  for pkg in gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-libav curl ca-certificates xz-utils \
    build-essential pkg-config nasm yasm libx264-dev libssl-dev libopus-dev; do
    dpkg -s "$pkg" >/dev/null 2>&1 || media_missing+=("$pkg")
  done
  if [ "${#media_missing[@]}" -gt 0 ]; then
    log "installing via apt: ${media_missing[*]}"
    $SUDO apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${media_missing[@]}"
  fi

  if command -v tsp >/dev/null 2>&1; then
    log "tsduck is installed ($(tsp --version 2>/dev/null | head -n 1 || true))"
  else
    log "WARNING: tsp is not on PATH. SCTE-35 insertion needs TSDuck from https://tsduck.io/"
  fi

  install_ffmpeg
  if [ -e /usr/lib/libDeckLinkAPI.so ] || [ -e /usr/lib/x86_64-linux-gnu/libDeckLinkAPI.so ]; then
    log "Desktop Video library is present"
  else
    log "WARNING: libDeckLinkAPI.so was not found. Desktop Video 16 is a separate install and is what creates /dev/blackmagic."
  fi

  $SUDO mkdir -p /var/lib/nexencoder/hls /var/lib/nexencoder/state /etc/nexencoder
  if [ ! -f /var/lib/nexencoder/config.json ]; then
    $SUDO cp config/encoder.json /var/lib/nexencoder/config.json
    log "installed /var/lib/nexencoder/config.json"
  fi
  $SUDO cp config/mediamtx.yml /etc/nexencoder/mediamtx.yml

  mediamtx_arch=""
  case "$(uname -m)" in
    x86_64|amd64) mediamtx_arch=amd64; mediamtx_sha=653abc672a3e693f8d3b2717752492fdcfb8072291ec108d03d3dd857411b0ee ;;
    aarch64|arm64) mediamtx_arch=arm64; mediamtx_sha=6a3aa635fb60ea9b8d566ec306f0a42ff1b6b52a3942bc2baffbe55880d4c3dd ;;
  esac
  if [ -n "$mediamtx_arch" ] && [ ! -x /usr/local/bin/mediamtx ]; then
    mediamtx_url="https://github.com/bluenviron/mediamtx/releases/download/v1.21.1/mediamtx_v1.21.1_linux_${mediamtx_arch}.tar.gz"
    mediamtx_tmp="$(mktemp -d)"
    log "downloading MediaMTX v1.21.1 (${mediamtx_arch})"
    curl -fsSL -o "$mediamtx_tmp/mediamtx.tar.gz" "$mediamtx_url"
    echo "${mediamtx_sha}  $mediamtx_tmp/mediamtx.tar.gz" | sha256sum -c -
    tar -xzf "$mediamtx_tmp/mediamtx.tar.gz" -C "$mediamtx_tmp" mediamtx
    $SUDO install -m 0755 "$mediamtx_tmp/mediamtx" /usr/local/bin/mediamtx
    rm -rf "$mediamtx_tmp"
  elif [ -z "$mediamtx_arch" ]; then
    log "WARNING: no pinned MediaMTX build for $(uname -m)"
  fi

  unit_tmp="$(mktemp)"
  sed "s|@ROOT@|$(pwd)|g" systemd/nexencoder-channel@.service > "$unit_tmp"
  $SUDO cp "$unit_tmp" /etc/systemd/system/nexencoder-channel@.service
  rm -f "$unit_tmp"
  $SUDO cp systemd/mediamtx.service /etc/systemd/system/mediamtx.service
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl daemon-reload
    if [ -x /usr/local/bin/mediamtx ]; then
      $SUDO systemctl enable mediamtx
      $SUDO systemctl restart mediamtx || log "WARNING: could not start mediamtx"
    fi
  fi
  log "channel units are installed. Start them with: systemctl start nexencoder-channel@A nexencoder-channel@B"
fi

log "running test suite"
python3 -m pytest -q

log "setup complete"
