#!/usr/bin/env bash
set -euo pipefail
set +x
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

REPOSITORY=https://github.com/digiden96/awg-telegram-manager.git
[[ $EUID -eq 0 ]] || { echo 'Run this installer with sudo.' >&2; exit 1; }
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ ! -f "$project_dir/backend/awg_manager.py" ]]; then
  command -v git >/dev/null || { apt-get update; DEBIAN_FRONTEND=noninteractive apt-get install -y git; }
  checkout=$(mktemp -d)
  trap 'rm -rf -- "$checkout"' EXIT
  git clone --depth 1 "$REPOSITORY" "$checkout/source"
  bash "$checkout/source/install.sh" "$@"
  exit $?
fi

[[ -t 0 ]] || { echo 'Interactive terminal required. Download install.sh, then run sudo bash install.sh.' >&2; exit 1; }
[[ ! -e /var/lib/awg-manager/manager.db && ! -e /etc/awg-manager/config.json ]] || { echo 'Manager already installed. Setup refuses to overwrite existing state; see README for upgrades.' >&2; exit 1; }
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || { echo 'This release supports Ubuntu 24.04 only.' >&2; exit 1; }

echo 'AmneziaWG Telegram Manager setup'
echo '1) Attach to an existing AWG interface'
echo '2) Install tested AmneziaWG 3.1 userspace and create awg0'
read -r -p 'Mode [1]: ' install_mode
install_mode=${install_mode:-1}
[[ $install_mode == 1 || $install_mode == 2 ]] || exit 1

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 nodejs qrencode curl jq sudo iptables iproute2 git ca-certificates xz-utils

if [[ $install_mode == 2 ]]; then
  config_path=/etc/amnezia/amneziawg/awg0.conf
  [[ ! -e $config_path ]] || { echo "$config_path already exists; refusing to overwrite it." >&2; exit 1; }
  ! command -v awg >/dev/null || { echo 'AWG already installed; use attach mode.' >&2; exit 1; }
  read -r -p 'VPN subnet [10.66.66.0/24]: ' vpn_network
  vpn_network=${vpn_network:-10.66.66.0/24}
  read -r -p 'UDP listen port [443]: ' listen_port
  listen_port=${listen_port:-443}
  [[ $listen_port =~ ^[0-9]+$ ]] && ((listen_port>=1 && listen_port<=65535)) || { echo 'Invalid port' >&2; exit 1; }
  server_address=$(python3 -c 'import ipaddress,sys; n=ipaddress.ip_network(sys.argv[1]); assert n.version==4 and 16<=n.prefixlen<=29; print(next(n.hosts()))' "$vpn_network")
  egress_interface=$(ip -4 route show default | awk 'NR==1 {print $5}')
  [[ $egress_interface =~ ^[A-Za-z0-9_.:-]+$ ]] || { echo 'Cannot detect default network interface' >&2; exit 1; }
  if ss -H -lun | awk '{print $4}' | grep -qE ":${listen_port}$"; then echo 'UDP port is occupied'; exit 1; fi
  provision_new() {
  bash "$project_dir/scripts/install-awg31.sh"
  install -d -m 0700 "$(dirname "$config_path")"
  server_private=$(/usr/local/bin/awg genkey)
  header_key=$(/usr/local/bin/awg genkey)
  random32() { od -An -N4 -tu4 /dev/urandom | tr -d ' '; }
  h1=$(random32); h2=$(random32); h3=$(random32); h4=$(random32)
  install -m 0600 /dev/stdin "$config_path" <<EOF
[Interface]
PrivateKey = $server_private
Address = $server_address/${vpn_network#*/}
ListenPort = $listen_port
MTU = 1280
Jc = 5
Jmin = 64
Jmax = 512
S1 = 32
S2 = 48
S3 = 24
S4 = 32
H1 = $h1
H2 = $h2
H3 = $h3
H4 = $h4
HeaderProtectionKey = $header_key
PostUp = iptables -t nat -A POSTROUTING -s $vpn_network -o $egress_interface -j MASQUERADE
PostDown = iptables -t nat -D POSTROUTING -s $vpn_network -o $egress_interface -j MASQUERADE
PostUp = iptables -I FORWARD -i awg0 -o $egress_interface -s $vpn_network -j ACCEPT
PostUp = iptables -I FORWARD -i $egress_interface -o awg0 -d $vpn_network -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostDown = iptables -D FORWARD -i awg0 -o $egress_interface -s $vpn_network -j ACCEPT
PostDown = iptables -D FORWARD -i $egress_interface -o awg0 -d $vpn_network -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
EOF
  install -m 0644 /dev/stdin /etc/sysctl.d/70-awg-forward.conf <<EOF
net.ipv4.ip_forward=1
EOF
  sysctl -p /etc/sysctl.d/70-awg-forward.conf
  install -d -m 0755 /etc/systemd/system/awg-quick@awg0.service.d
  install -m 0644 /dev/stdin /etc/systemd/system/awg-quick@awg0.service.d/userspace.conf <<EOF
[Service]
Environment=WG_QUICK_USERSPACE_IMPLEMENTATION=/usr/local/bin/amneziawg-go
Environment=PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
EOF
  systemctl daemon-reload
  systemctl enable --now awg-quick@awg0
  if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then ufw allow "$listen_port/udp"; fi
  }
else
  mapfile -t candidates < <(find /etc/amnezia/amneziawg /etc/wireguard /opt/amnezia -maxdepth 3 -type f -name '*.conf' 2>/dev/null || true)
  detected=${candidates[0]:-}
  read -r -p "Existing AWG config [$detected]: " config_path
  config_path=${config_path:-$detected}
  [[ -f $config_path ]] || { echo 'AWG config not found' >&2; exit 1; }
  [[ $config_path != *[[:space:]]* ]] || { echo 'Config paths containing whitespace are not supported' >&2; exit 1; }
  interface_name=$(basename "$config_path" .conf)
  read -r -p "Interface name [$interface_name]: " custom_interface
  interface_name=${custom_interface:-$interface_name}
  [[ $interface_name =~ ^[A-Za-z0-9_-]{1,15}$ ]] || exit 1
  config_path=$(realpath "$config_path")
  [[ $config_path =~ ^/[A-Za-z0-9_./-]+$ ]] || exit 1
  [[ $(basename "$config_path" .conf) == "$interface_name" ]] || { echo 'Interface must match config filename'; exit 1; }
  awg_bin=$(command -v awg || true); quick_bin=$(command -v awg-quick || true)
  [[ -x $awg_bin && -x $quick_bin ]] || { echo 'awg and awg-quick must already be installed' >&2; exit 1; }
  "$awg_bin" show "$interface_name" peers >/dev/null
  # Only host interfaces are supported. Never adopt a Docker config as a host config.
  server_cidr=$(awk -F= '/^[[:space:]]*Address[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "$config_path")
  vpn_network=$(python3 -c 'import ipaddress,sys; n=ipaddress.ip_interface(sys.argv[1]).network; assert n.version==4 and 16<=n.prefixlen<=29; print(n)' "$server_cidr")
  server_address=${server_cidr%/*}
  listen_port=$(awk -F= '/^[[:space:]]*ListenPort[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "$config_path")
fi

interface_name=${interface_name:-awg0}
awg_bin=${awg_bin:-/usr/local/bin/awg}
quick_bin=${quick_bin:-/usr/local/bin/awg-quick}
read -r -p 'Public server hostname or IP: ' public_host
read -r -p "Public UDP port [$listen_port]: " public_port
public_port=${public_port:-$listen_port}
endpoint="$public_host:$public_port"
[[ $endpoint =~ ^[A-Za-z0-9._-]+:[0-9]+$ ]] || { echo 'Invalid endpoint' >&2; exit 1; }
((public_port>=1 && public_port<=65535)) || exit 1

read -r -p 'Telegram numeric owner ID: ' owner_id
[[ $owner_id =~ ^[0-9]+$ ]] || { echo 'Invalid Telegram owner ID' >&2; exit 1; }
read -r -s -p 'Telegram Bot Token: ' bot_token; echo
[[ $bot_token =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || { echo 'Invalid Bot Token format' >&2; exit 1; }
read -r -p 'Telegram topic ID (blank for ordinary private chat): ' topic_id
[[ -z $topic_id || $topic_id =~ ^[0-9]+$ ]] || { echo 'Invalid topic ID' >&2; exit 1; }
read -r -p 'Bot runtime: standalone or openclaw [standalone]: ' bot_runtime
bot_runtime=${bot_runtime:-standalone}
[[ $bot_runtime == standalone || $bot_runtime == openclaw ]] || { echo 'Invalid runtime' >&2; exit 1; }
if [[ $bot_runtime == openclaw ]]; then
  [[ -f /var/lib/openclaw/.openclaw/openclaw.json && -x /var/lib/openclaw/runtime/bin/openclaw ]] || { echo 'Supported OpenClaw installation not found'; exit 1; }
fi

telegram_check=$(curl -fsS --config - <<EOF
url = "https://api.telegram.org/bot$bot_token/getMe"
EOF
)
jq -e '.ok == true' >/dev/null <<<"$telegram_check" || { echo 'Telegram rejected the token' >&2; exit 1; }
echo "Install $bot_runtime manager for $interface_name ($endpoint), owner $owner_id."
read -r -p 'Apply this configuration? [yes/NO]: ' confirmed
[[ $confirmed == yes ]] || exit 0
if [[ $install_mode == 1 ]]; then
  preflight=$(mktemp -d)
  AWG_MANAGER_SETTINGS="$preflight/settings.json" AWG_MANAGER_STATE="$preflight" AWG_MANAGER_NETWORK="$vpn_network" AWG_MANAGER_SERVER_IP="$server_address" python3 "$project_dir/backend/awg_manager.py" check-import "$config_path"
fi
bash "$project_dir/scripts/install-node.sh"
if [[ -x /opt/awg-manager-node/bin/node ]]; then export PATH=/opt/awg-manager-node/bin:$PATH; fi
if [[ $install_mode == 2 ]]; then provision_new; fi

backup_dir=/var/backups/awg-telegram-manager/$(date -u +%Y%m%d-%H%M%S)
install -d -m 0700 "$backup_dir"
cp -a "$config_path" "$backup_dir/server.conf"
install -d -o root -g root -m 0755 /opt/awg-telegram-manager /etc/awg-manager
cp -a "$project_dir/backend" "$project_dir/openclaw-plugin" "$project_dir/telegram-bot" /opt/awg-telegram-manager/
find /opt/awg-telegram-manager -type d -exec chmod 0755 {} +
find /opt/awg-telegram-manager -type f -exec chmod 0644 {} +
chown -R root:root /opt/awg-telegram-manager
install -o root -g root -m 0755 "$project_dir/backend/awg_manager.py" /usr/local/sbin/awg-manager
install -o root -g root -m 0644 "$project_dir/backend/awg-manager.service" /etc/systemd/system/awg-manager.service
install -d -m 0755 /etc/systemd/system/awg-manager.service.d
install -m 0644 /dev/stdin /etc/systemd/system/awg-manager.service.d/config-path.conf <<EOF
[Service]
ReadWritePaths=$(dirname "$config_path")
EOF
install -d -o root -g root -m 0700 /var/lib/awg-manager
install -m 0600 /dev/stdin /etc/awg-manager/manager.env <<EOF
AWG_MANAGER_CONFIG=$config_path
AWG_MANAGER_AWG=$awg_bin
AWG_MANAGER_QUICK=$quick_bin
AWG_MANAGER_INTERFACE=$interface_name
AWG_MANAGER_NETWORK=$vpn_network
AWG_MANAGER_SERVER_IP=$server_address
EOF
jq -n --arg endpoint "$endpoint" --arg config_path "$config_path" --arg awg "$awg_bin" --arg quick "$quick_bin" --arg interface "$interface_name" --arg network "$vpn_network" --arg server_ip "$server_address" '{endpoint:$endpoint,config_path:$config_path,awg:$awg,quick:$quick,interface:$interface,network:$network,server_ip:$server_ip}' > /etc/awg-manager/config.json
chmod 0600 /etc/awg-manager/config.json

getent group awg-manager-bot >/dev/null || groupadd --system awg-manager-bot
id awg-bot >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin -g awg-manager-bot awg-bot
printf '%s\n' "$bot_token" > /etc/awg-manager/telegram-token
chown root:awg-manager-bot /etc/awg-manager/telegram-token
chmod 0640 /etc/awg-manager/telegram-token
jq -n --argjson owner "$owner_id" --arg topic "${topic_id:-}" '{owner_id:$owner,thread_id:(if $topic=="" then null else ($topic|tonumber) end),token_file:"/etc/awg-manager/telegram-token",helper:"/usr/local/sbin/awg-manager"}' > /etc/awg-manager/telegram.json
chown root:awg-manager-bot /etc/awg-manager/telegram.json
chmod 0640 /etc/awg-manager/telegram.json
jq -n --argjson owner "$owner_id" --arg topic "${topic_id:-}" '{chat_id:$owner,thread_id:(if $topic=="" then null else ($topic|tonumber) end),token_file:"/etc/awg-manager/telegram-token"}' > /var/lib/awg-manager/notifications.json
chmod 0600 /var/lib/awg-manager/notifications.json

if id openclaw >/dev/null 2>&1; then usermod -a -G awg-manager-bot openclaw; fi
install -o root -g root -m 0440 "$project_dir/backend/openclaw-awg-manager.sudoers" /etc/sudoers.d/awg-telegram-manager
visudo -cf /etc/sudoers.d/awg-telegram-manager

set -a; source /etc/awg-manager/manager.env; set +a
/usr/local/sbin/awg-manager import-config "$config_path"
systemctl daemon-reload
systemctl enable --now awg-manager

if [[ $bot_runtime == standalone ]]; then
  install -o root -g root -m 0644 "$project_dir/telegram-bot/awg-telegram-bot.service" /etc/systemd/system/awg-telegram-bot.service
  systemctl daemon-reload
  node_bin=$(command -v node)
  install -d -m 0755 /etc/systemd/system/awg-telegram-bot.service.d
  printf '[Service]\nExecStart=\nExecStart=%s /opt/awg-telegram-manager/telegram-bot/bot.mjs\n' "$node_bin" > /etc/systemd/system/awg-telegram-bot.service.d/node.conf
  systemctl daemon-reload
  systemctl enable --now awg-telegram-bot
else
  openclaw_home=/var/lib/openclaw
  id openclaw >/dev/null 2>&1 || { echo 'OpenClaw user not found' >&2; exit 1; }
  openclaw_args=(--owner-id "$owner_id" --endpoint "$endpoint" --token-file /etc/awg-manager/telegram-token --openclaw-home "$openclaw_home")
  if [[ -n $topic_id ]]; then openclaw_args+=(--topic-id "$topic_id"); fi
  python3 "$project_dir/deploy/configure_openclaw.py" "${openclaw_args[@]}"
  runuser -u openclaw -- /var/lib/openclaw/runtime/bin/openclaw config validate
  systemctl restart openclaw
fi

echo "Installed. Backup: $backup_dir"
echo 'Open Telegram and send /vpn to the bot.'
