#!/usr/bin/env bash
# NexEncoder one-command setup for Ubuntu 24.04 LTS. Idempotent: safe to re-run.
#
# Packages come from APT only: no pip, no venv, no Node, no containers.
# Installs Python, GStreamer, the local Prometheus stack, a pinned MediaMTX
# binary, Apache and PHP for the operator pages, the live-data feed, the channel
# units, and the token-authenticated NOC agent.
# FFmpeg 9.0.2 is downloaded and built with DeckLink (--enable-nonfree), OpenSSL
# (--enable-version3), libx264, and libopus. The DeckLink demuxer is patched to forward VANC captions and
# SCTE-104 to the channel supervisor. The nexeffect filter is added for the
# four-corner squeeze and CG stack. TSDuck is checked for, not downloaded.
# Desktop Video and the DeckLink SDK are installed from archives already in
# /home/nexstar (or the home of the user who started this script). They are
# not downloaded. Rebuild FFmpeg with NEXENCODER_FORCE_FFMPEG_BUILD=1.
set -euo pipefail
cd "$(dirname "$0")"

log() { printf '[setup] %s\n' "$*"; }
die() { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
HOST_INSTALL=1
if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
  HOST_INSTALL=0
  log "host install runs only on Ubuntu 24.04 LTS (found ${PRETTY_NAME:-unknown OS})"
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

if [ "$HOST_INSTALL" -eq 0 ]; then
  log "running test suite"
  python3 -m pytest -q
  log "setup complete (test suite only; host install was skipped)"
  exit 0
fi

install_operator_pages() {
  local missing=() pkg phpmod www_tmp
  for pkg in apache2 libapache2-mod-php php-curl; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    log "installing via apt: ${missing[*]}"
    $SUDO apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  fi
  $SUDO a2enmod rewrite headers >/dev/null
  phpmod="$(find /etc/apache2/mods-available -maxdepth 1 -name 'php*.load' -printf '%f\n' 2>/dev/null | head -n 1 || true)"
  phpmod="${phpmod%.load}"
  if [ -z "$phpmod" ]; then
    die "Apache PHP module is not installed"
  fi
  $SUDO a2enmod "$phpmod" >/dev/null
  www_tmp="$(mktemp -d)"
  cp -a web/. "$www_tmp/"
  $SUDO mkdir -p /var/lib/nexencoder
  $SUDO rm -rf /var/lib/nexencoder/www
  $SUDO mv "$www_tmp" /var/lib/nexencoder/www
  $SUDO find /var/lib/nexencoder/www -type d -exec chmod 755 {} +
  $SUDO find /var/lib/nexencoder/www -type f -exec chmod 644 {} +
  $SUDO cp config/apache-nexencoder-listen.conf /etc/apache2/conf-available/nexencoder-listen.conf
  $SUDO cp config/apache-nexencoder.conf /etc/apache2/sites-available/nexencoder.conf
  $SUDO a2disconf nexencoder-listen >/dev/null 2>&1 || true
  $SUDO a2dissite 000-default >/dev/null 2>&1 || true
  $SUDO a2ensite nexencoder >/dev/null
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl enable apache2
    $SUDO systemctl restart apache2 || log "WARNING: could not start Apache"
  fi
  log "operator pages are on port 80 (Apache and PHP). The live feed stays on 127.0.0.1:8788."
}

allow_firewall() {
  if ! command -v ufw >/dev/null 2>&1; then
    log "installing ufw"
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw
  fi
  $SUDO ufw allow OpenSSH >/dev/null
  $SUDO ufw allow 80/tcp >/dev/null
  $SUDO ufw allow 8889/tcp >/dev/null
  $SUDO ufw allow 8889/udp >/dev/null
  $SUDO ufw allow 8790/tcp >/dev/null
  local ports port
  ports="$(python3 -c '
import json
from pathlib import Path
from nexencoder.channels import INPUT_SRT_PORTS, normalize_config
raw = {}
for path in (Path("/var/lib/nexencoder/config.json"), Path("config/encoder.json")):
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        break
cfg = normalize_config(raw if isinstance(raw, dict) else {})
ports = set(INPUT_SRT_PORTS.values())
for spec in cfg["outputs"].values():
    if spec.get("mode") == "srt-listener":
        ports.add(int(spec["port"]))
print(" ".join(str(port) for port in sorted(ports)))
')"
  for port in $ports; do
    $SUDO ufw allow "${port}/udp" >/dev/null
  done
  $SUDO ufw --force enable >/dev/null
  log "ufw allows OpenSSH, 80/tcp, 8889, 8790/tcp, and SRT udp ${ports}"
}

pin_listen() {
  local file="$1" addr="$2" unit="$3"
  local unit_file="" candidate
  [ -f "$file" ] || return 0
  if grep -q 'nexencoder-listen' "$file"; then
    return 0
  fi
  for candidate in "/lib/systemd/system/${unit}" "/usr/lib/systemd/system/${unit}"; do
    if [ -f "$candidate" ]; then
      unit_file="$candidate"
      break
    fi
  done
  if [ -n "$unit_file" ] && grep -q -- "--web.listen-address=" "$unit_file"; then
    log "${unit} already sets --web.listen-address; leaving ${file} unchanged"
    return 0
  fi
  if grep -q '^ARGS=' "$file" && ! grep -q '^ARGS=""$' "$file" && ! grep -q "^ARGS=''$" "$file"; then
    log "WARNING: ${file} already sets ARGS; not pinning ${addr}"
    return 0
  fi
  if grep -q '^ARGS=""$' "$file"; then
    $SUDO sed -i "s/^ARGS=\"\"/ARGS=\"--web.listen-address=${addr}\"/" "$file"
    printf '\n# nexencoder-listen\n' | $SUDO tee -a "$file" >/dev/null
    return 0
  fi
  printf '\n# nexencoder-listen\nARGS="--web.listen-address=%s"\n' "$addr" | $SUDO tee -a "$file" >/dev/null
}

decklink_sdk_dir() {
  local dir
  for dir in /opt/decklink-sdk /usr/include; do
    if [ -f "${dir}/DeckLinkAPI.h" ] && [ -f "${dir}/DeckLinkAPIDispatch.cpp" ] && [ -f "${dir}/DeckLinkAPIVersion.h" ]; then
      printf '%s\n' "$dir"
      return 0
    fi
  done
  return 1
}

install_decklink() {
  local sudo_home="" work zip archive deb
  if [ -n "${SUDO_USER:-}" ]; then
    sudo_home="$(getent passwd "$SUDO_USER" 2>/dev/null | awk -F: '{print $6}' || true)"
  fi
  work="$(mktemp -d)"
  if ! decklink_sdk_dir >/dev/null; then
    zip="$(python3 -c 'import sys; from nexencoder.decklink_install import latest_sdk; found = latest_sdk(sys.argv[1] or None, sys.argv[2] or None); print(found or "")' "${HOME:-}" "$sudo_home")"
    if [ -z "$zip" ]; then
      rm -rf "$work"
      die "DeckLink SDK headers not found. Put Blackmagic_DeckLink_SDK_*.zip in /home/nexstar."
    fi
    log "installing DeckLink SDK headers from ${zip}"
    python3 -c 'import sys; from pathlib import Path; from nexencoder.decklink_install import extract_sdk; extract_sdk(Path(sys.argv[1]), Path(sys.argv[2]))' "$zip" "$work/include"
    $SUDO mkdir -p /opt/decklink-sdk
    $SUDO cp -a "$work/include/." /opt/decklink-sdk/
    if ! decklink_sdk_dir >/dev/null; then
      rm -rf "$work"
      die "DeckLink SDK headers were not installed to /opt/decklink-sdk"
    fi
  else
    log "DeckLink SDK headers are already installed ($(decklink_sdk_dir))"
  fi
  if dpkg -s desktopvideo >/dev/null 2>&1; then
    log "Desktop Video is already installed"
    rm -rf "$work"
    return 0
  fi
  archive="$(python3 -c 'import sys; from nexencoder.decklink_install import latest_driver; found = latest_driver(sys.argv[1] or None, sys.argv[2] or None); print(found or "")' "${HOME:-}" "$sudo_home")"
  if [ -z "$archive" ]; then
    log "WARNING: Desktop Video is not installed. Put Blackmagic_Desktop_Video_Linux_*.tar.gz in /home/nexstar."
    rm -rf "$work"
    return 0
  fi
  log "installing Desktop Video from ${archive}"
  rm -rf "$work"
  work="$(mktemp -d)"
  tar -xzf "$archive" -C "$work"
  deb="$(python3 -c 'import sys; from pathlib import Path; from nexencoder.decklink_install import find_driver_deb; print(find_driver_deb(Path(sys.argv[1])))' "$work")"
  $SUDO apt-get update -qq
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dkms "linux-headers-$(uname -r)"
  if ! $SUDO dpkg -i "$deb"; then
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -f
  fi
  if ! dpkg -s desktopvideo >/dev/null 2>&1; then
    rm -rf "$work"
    die "Desktop Video did not install from ${archive}"
  fi
  $SUDO ldconfig
  rm -rf "$work"
}

install_ffmpeg() {
  local version=9.0.2
  local url="https://ffmpeg.org/releases/ffmpeg-${version}.tar.xz"
  local sha=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
  local prefix=/usr/local
  local bin="${prefix}/bin/ffmpeg"
  local sdk
  if ! sdk="$(decklink_sdk_dir)"; then
    die "DeckLink SDK headers not found. Put Blackmagic_DeckLink_SDK_*.zip in /home/nexstar."
  fi
  if [ "${NEXENCODER_FORCE_FFMPEG_BUILD:-}" != "1" ] && [ -x "$bin" ]; then
    local ver conf
    ver="$("$bin" -version 2>/dev/null | head -n 1 || true)"
    conf="$("$bin" -buildconf 2>/dev/null || true)"
    if [[ "$ver" == *"$version"* && "$conf" == *"--enable-decklink"* && "$conf" == *"--enable-nonfree"* \
      && "$conf" == *"--enable-version3"* && "$conf" == *"--enable-libx264"* && "$conf" == *"--enable-libopus"* ]] \
      && [ -e /usr/local/lib/libavdevice.so ] \
      && grep -a -q 'nexencoder VANC forwarder' /usr/local/lib/libavdevice.so \
      && grep -a -q 'nexencoder effect stack' /usr/local/lib/libavfilter.so; then
      log "FFmpeg ${version} with DeckLink and the VANC forwarder is already installed (${bin})"
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
  log "patching DeckLink capture to forward VANC captions and SCTE-104"
  python3 -c 'import sys; from nexencoder.decklink_patch import patch_file; patch_file(sys.argv[1])' \
    "${src}/libavdevice/decklink_dec.cpp"
  log "installing the nexeffect squeeze and CG filter"
  python3 -c 'import sys; from nexencoder.effect_patch import install_filter; install_filter(sys.argv[1])' \
    "$src"
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
      --enable-version3 \
      --enable-nonfree \
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
  "$bin" -buildconf | grep -q -- '--enable-nonfree'
  "$bin" -buildconf | grep -q -- '--enable-version3'
  grep -a -q 'nexencoder VANC forwarder' /usr/local/lib/libavdevice.so
  grep -a -q 'nexencoder effect stack' /usr/local/lib/libavfilter.so
  log "installed ${bin} ($("$bin" -version | head -n 1))"
}

install_noc_agent() {
  if ! id nexencoder >/dev/null 2>&1; then
    $SUDO useradd --system --home-dir /var/lib/nexencoder --shell /usr/sbin/nologin --comment "NexEncoder NOC agent" nexencoder
    log "created system user nexencoder"
  fi
  for group in systemd-journal video render; do
    if getent group "$group" >/dev/null 2>&1; then
      $SUDO usermod -aG "$group" nexencoder || log "WARNING: could not add nexencoder to ${group}"
    fi
  done
  $SUDO mkdir -p /etc/nexencoder /var/lib/nexencoder/hls /var/lib/nexencoder/state /etc/polkit-1/rules.d /etc/sudoers.d
  $SUDO chmod 0755 /var/lib/nexencoder /var/lib/nexencoder/hls /var/lib/nexencoder/state
  if [ ! -f /etc/nexencoder/noc.json ]; then
    $SUDO cp config/noc.json /etc/nexencoder/noc.json
    $SUDO chmod 0644 /etc/nexencoder/noc.json
    log "installed /etc/nexencoder/noc.json"
  fi
  if [ ! -s /etc/nexencoder/noc.token ]; then
    token="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    printf '%s\n' "$token" | $SUDO tee /etc/nexencoder/noc.token >/dev/null
    unset token
    log "generated /etc/nexencoder/noc.token"
  fi
  $SUDO chown root:nexencoder /etc/nexencoder/noc.token
  $SUDO chmod 0640 /etc/nexencoder/noc.token
  $SUDO cp config/nexencoder-noc.rules /etc/polkit-1/rules.d/50-nexencoder-noc.rules
  $SUDO chmod 0644 /etc/polkit-1/rules.d/50-nexencoder-noc.rules
  sudoers_tmp="$(mktemp)"
  cp config/sudoers-nexencoder-noc "$sudoers_tmp"
  if command -v visudo >/dev/null 2>&1; then
    visudo -cf "$sudoers_tmp"
  fi
  $SUDO cp "$sudoers_tmp" /etc/sudoers.d/nexencoder-noc
  $SUDO chmod 0440 /etc/sudoers.d/nexencoder-noc
  rm -f "$sudoers_tmp"
  $SUDO chmod -R a+rX nexencoder
  unit_tmp="$(mktemp)"
  sed "s|@ROOT@|$(pwd)|g" systemd/nexencoder-noc.service > "$unit_tmp"
  $SUDO cp "$unit_tmp" /etc/systemd/system/nexencoder-noc.service
  rm -f "$unit_tmp"
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable nexencoder-noc
    $SUDO systemctl restart nexencoder-noc || log "WARNING: could not start nexencoder-noc (the nexencoder user must be able to read this tree and the token file)"
  fi
  if ! $SUDO -u nexencoder python3 -c 'import nexencoder' >/dev/null 2>&1; then
    log "WARNING: user nexencoder cannot import nexencoder from $(pwd). Make this directory executable by that user and restart nexencoder-noc."
  fi
  log "NOC agent is nexencoder-noc on port 8790. Token file is /etc/nexencoder/noc.token (mode 640, group nexencoder)."
  log "Set encoder_id, site, location, and allow in /etc/nexencoder/noc.json, then restart nexencoder-noc."
  log "Rotate the token by replacing /etc/nexencoder/noc.token and restarting nexencoder-noc. Do not print the token."
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
  pin_listen /etc/default/prometheus "127.0.0.1:9090" prometheus.service
  pin_listen /etc/default/prometheus-node-exporter "127.0.0.1:9100" prometheus-node-exporter.service
  unit_tmp="$(mktemp)"
  sed "s|@ROOT@|$(pwd)|g" systemd/nexencoder-console.service > "$unit_tmp"
  $SUDO cp "$unit_tmp" /etc/systemd/system/nexencoder-console.service
  rm -f "$unit_tmp"

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
  if apt-cache show gstreamer1.0-wpe >/dev/null 2>&1; then
    if ! dpkg -s gstreamer1.0-wpe >/dev/null 2>&1; then
      log "installing gstreamer1.0-wpe for HTML CG"
      $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gstreamer1.0-wpe \
        || log "WARNING: gstreamer1.0-wpe did not install. HTML layers use Chromium when it is on PATH."
    fi
  else
    log "WARNING: gstreamer1.0-wpe is not in apt. HTML layers use Chromium when it is on PATH."
  fi
  if ! gst-inspect-1.0 ndisrc >/dev/null 2>&1; then
    log "WARNING: GStreamer ndisrc is not installed. NDI CG needs the NDI SDK and that plugin. The layer stays transparent until the receiver is present."
  fi

  if command -v tsp >/dev/null 2>&1; then
    log "tsduck is installed ($(tsp --version 2>/dev/null | head -n 1 || true))"
  else
    log "WARNING: tsp is not on PATH. SCTE-35 insertion needs TSDuck from https://tsduck.io/"
  fi

  install_decklink
  install_ffmpeg
  if dpkg -s desktopvideo >/dev/null 2>&1; then
    desktopvideo_version=$(dpkg-query -W -f='${Version}' desktopvideo 2>/dev/null || true)
    log "Desktop Video is installed (${desktopvideo_version})"
    if [ ! -e /dev/blackmagic ]; then
      log "Reboot so the Desktop Video module can create /dev/blackmagic."
    fi
  fi

  $SUDO mkdir -p /var/lib/nexencoder/hls /var/lib/nexencoder/state /etc/nexencoder
  install_operator_pages
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
    $SUDO systemctl enable prometheus prometheus-node-exporter nexencoder-console \
      nexencoder-channel@A nexencoder-channel@B
    $SUDO systemctl restart prometheus prometheus-node-exporter nexencoder-console \
      || log "WARNING: could not start prometheus or the console"
    if [ -x /usr/local/bin/mediamtx ]; then
      $SUDO systemctl enable mediamtx
      $SUDO systemctl restart mediamtx || log "WARNING: could not start mediamtx"
    fi
    $SUDO systemctl restart nexencoder-channel@A nexencoder-channel@B \
      || log "WARNING: could not start a channel"
  fi
  log "channel units are enabled and restarted"
  install_noc_agent
  allow_firewall
fi

log "running test suite"
python3 -m pytest -q

log "setup complete"
