#!/usr/bin/env bash
# Set up (or update) a hub server from this checkout. Safe to re-run.
#
#   git clone https://github.com/blackdiamondlodge/BlackDiamondHub.git
#   cd BlackDiamondHub && sudo deploy/bootstrap.sh
#
# What it does:
#   - installs Docker Engine + compose plugin from Docker's apt repo
#   - grows the root LVM volume to use the whole disk (Ubuntu installer
#     leaves half of it unallocated)
#   - creates .env from .env.example on first run, then stops so you can fill
#     it in (set HUB_IMAGE_TAG to the site, e.g. mercerisland)
#   - sets BDH_GO2RTC_CANDIDATES in .env to this host's LAN address if it is
#     blank (the WebRTC candidate the cameras go2rtc companion advertises)
#   - pulls the images and starts the stack (--remove-orphans also removes
#     the old standalone go2rtc container)
#
# Existing .env values are never overwritten.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
cd "$(dirname "$0")/.."
OWNER="${SUDO_USER:-root}"

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "== installing Docker"
  apt-get update -q
  apt-get install -yq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck source=/dev/null
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
fi

root_dev="$(findmnt -no SOURCE /)"
if lvs "$root_dev" >/dev/null 2>&1; then
  vg="$(lvs --noheadings -o vg_name "$root_dev" | tr -d ' ')"
  pv="$(pvs --noheadings -o pv_name -S vg_name="$vg" | head -n1 | tr -d ' ')"
  pvresize "$pv" >/dev/null || true
  free="$(vgs --noheadings --units b --nosuffix -o vg_free "$vg" | tr -d ' ')"
  if [ "${free:-0}" -gt $((1024 * 1024 * 1024)) ]; then
    echo "== growing $root_dev into $((free / 1024 / 1024 / 1024))G free"
    lvextend -r -l +100%FREE "$root_dev"
  fi
fi

mkdir -p backups media
chown "$OWNER:" backups media

created_env=0
if [ ! -f .env ]; then
  install -m 600 -o "$OWNER" .env.example .env
  created_env=1
fi

if ! grep -q '^BDH_GO2RTC_CANDIDATES=.\+' .env; then
  ip="$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')"
  echo "== setting BDH_GO2RTC_CANDIDATES=$ip:8555 in .env"
  sed -i '/^BDH_GO2RTC_CANDIDATES=/d' .env
  echo "BDH_GO2RTC_CANDIDATES=$ip:8555" >> .env
fi

if [ "$created_env" -eq 1 ]; then
  echo "== created .env from .env.example: fill it in, then re-run this script"
  exit 0
fi

echo "== starting the stack"
docker compose pull -q
docker compose up -d --remove-orphans
docker compose ps
