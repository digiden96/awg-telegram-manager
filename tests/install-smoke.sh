#!/usr/bin/env bash
# Disposable-container check. Never execute this script on a real host.
set -euo pipefail
[[ -f /.dockerenv ]] || exit 1
install -d /etc/amnezia/amneziawg
cat > /etc/amnezia/amneziawg/awg0.conf <<'EOF'
[Interface]
PrivateKey = test-only-key
Address = 10.66.66.1/24
ListenPort = 443
[Peer]
PublicKey = existing-test-peer
AllowedIPs = 10.66.66.2/32
EOF
cat > /usr/local/bin/awg <<'EOF'
#!/bin/bash
case "$1:$3" in
show:peers) echo existing-test-peer ;;
show:dump) printf 'interface\n' ;;
genkey:*) echo test-generated-key ;;
pubkey:*) cat >/dev/null; echo test-generated-public ;;
genpsk:*) echo test-generated-psk ;;
syncconf:*) cat >/dev/null ;;
*) exit 1 ;;
esac
EOF
cat > /usr/local/bin/awg-quick <<'EOF'
#!/bin/bash
cat "$2"
EOF
cat > /usr/local/bin/systemctl <<'EOF'
#!/bin/bash
exit 0
EOF
cat > /usr/local/bin/curl <<'EOF'
#!/bin/bash
if [[ " $* " == *' --config '* ]]; then
  cat >/dev/null
  printf '{"ok":true,"result":{"username":"test_bot"}}'
else
  exec /usr/bin/curl "$@"
fi
EOF
chmod 755 /usr/local/bin/{awg,awg-quick,systemctl,curl}
printf '1\n\n\nexample.com\n\n123456\n123456:TEST_TOKEN\n\nstandalone\nyes\n' | script -qec 'bash /work/install.sh' /dev/null
runuser -u awg-bot -- sudo -n /usr/local/sbin/awg-manager list | jq -e 'length==1 and .[0].exportable==false'
runuser -u awg-bot -- sudo -n /usr/local/sbin/awg-manager add new-device | jq -e '.name=="new-device" and .exportable==true'
runuser -u awg-bot -- test -r /opt/awg-telegram-manager/openclaw-plugin/index.mjs
if runuser -u awg-bot -- test -r /var/lib/awg-manager/manager.db; then exit 1; fi
if bash /work/install.sh; then echo 'Reinstall unexpectedly accepted'; exit 1; fi
echo 'Installer import, helper permissions and overwrite refusal passed (mock AWG/Telegram/systemd).'
