# 🤖 Trading Fleet — Freqtrade Ultimate multi-bot

Fleet Docker di bot Freqtrade (fork Ultimate) con cache OHLCV condivisa, isolamento per-bot
(config + DB separati) e accesso remoto **solo via Tailscale**.

**Repo privata.** Versiona: infra, strategie, template `.example`, tooling.
**Non** versiona: file reali con segreti (`config.json`, `.env`), DB, log, runtime IPC.

> ⚠️ **Disclaimer**: software educativo. Il trading di criptovalute può portare alla perdita totale
> del capitale. Usa a tuo rischio.

**Indice**: [1 Servizi](#1-servizi) · [2 Installazione](#2-installazione) · [3 Permessi & UID](#3-permessi--uid) ·
[4 Sicurezza](#4-sicurezza) · [5 Aggiornamenti](#5-aggiornamenti) · [6 Manutenzione](#6-manutenzione) ·
[7 Aggiungere un bot](#7-aggiungere-un-bot-botn) · [8 Committato vs locale](#8-committato-vs-locale)

---

## 1. Servizi

| Servizio | Ruolo | Note |
|---|---|---|
| `srv-ftcache` | Daemon OHLCV condiviso | Un solo rate-budget per wallet; i bot sono client puri |
| `srv-ftpairlist` | Cache pairlist tra bot (opzionale) | "offline" dopo 15 min idle = normale |
| `srv-botN` | Bot live o dry-run | Uno per servizio; config e DB isolati |
| `srv-dev` | Runner usa-e-getta (download/backtest/replay) | Mai live; niente porte; limiti CPU/RAM |

- **ftcache**: socket **unica stringa** `/ftcache/ftcache.sock` in compose (`command`+`healthcheck` di
  `srv-ftcache`), config di ogni bot e config dev. Se un punto diverge → due daemon, budget dimezzato.
  I bot `depends_on: service_healthy`: se il daemon non parte, restano in attesa.
- **srv-dev**: task con `docker compose run --rm srv-dev <cmd>`. **Mai** `compose run` su un live.
- Mount strategie `:ro` sui live, `:rw` su dev.

---

## 2. Installazione

> Comandi bash: su **Windows** in **WSL2**, su Linux/macOS nel terminale.

### 2.1 Prerequisiti
- **Windows**: WSL2 + Docker Desktop (engine WSL2) + Git.
- **Linux**: `sudo apt install git docker.io docker-compose-v2 curl acl` +
  `sudo usermod -aG docker $USER && newgrp docker`.
- **Oracle Always Free**: VM.Standard.A1.Flex, Ubuntu 22.04 ARM, 2 OCPU/12 GB, boot 200 GB. Multi-arch ok.
- Chiave SSH Ed25519 con passphrase; backup in password manager + USB criptata.

### 2.2 Clone + build
```bash
git clone <repo-privata> trading-fleet && cd trading-fleet
git clone https://github.com/titouannwtt/freqtrade-ultimate.git freqtrade-ultimate
docker build --platform linux/arm64 -t freqtrade-ultimate:latest ./freqtrade-ultimate
```

### 2.3 Template → file reali (poi riempi i segreti)
```bash
mkdir -p bot1/user_data bot2/user_data dev/user_data shared/run backups
cp .env.shared.example .env.shared
cp bot1/.env.example bot1/.env
cp bot1/user_data/config.example.json bot1/user_data/config.json
cp bot2/.env.example bot2/.env
cp bot2/user_data/config.example.json bot2/user_data/config.json
cp dev/.env.example dev/.env
chmod 600 .env.shared bot*/.env dev/.env
```
Riempi i **reali** (mai i `.example`): `.env.shared` con `TZ=UTC`+chat_id+fiat; `botN/.env` con token
Telegram e `API_SERVER__USERNAME/PASSWORD/WS_TOKEN/JWT_SECRET_KEY` univoci (`openssl rand -hex 32`,
`-base64 24`), mai vuoti. **Hyperliquid**: `walletAddress`/`privateKey` (camelCase) nel `config.json`
reale, **non** in `.env` (il loader env lowercase-a → 422). Copia offsite criptata di config+env.

```bash
git status --short        # NON deve elencare .env / config.json reali
git check-ignore -v bot1/.env bot1/user_data/config.json
```

### 2.4 Permessi (§3), poi avvio + verify
```bash
docker compose up -d srv-ftcache
docker compose up -d srv-bot1 srv-bot2
docker compose exec -T srv-bot2 python -m freqtrade.ohlcv_cache.healthcheck --socket /ftcache/ftcache.sock
# atteso: ONLINE; clients = bot avviati; positions puts > 0 se un bot è live
```

---

## 3. Permessi & UID

UID non fissi (Oracle `opc`/`ubuntu`, `ftuser` variabile): rilevali. Docker crea i bind-mount mancanti
come **root**: pre-crea le dir con ownership+ACL.

```bash
FTUID=$(docker run --rm --entrypoint id freqtrade-ultimate:latest | grep -oP 'uid=\K\d+')
HOSTUID=$(id -u); HOSTGID=$(id -g)
mkdir -p bot1/user_data bot2/user_data dev/user_data shared/run shared/local backups
sudo chown -R "$HOSTUID:$HOSTGID" bot1/user_data bot2/user_data dev/user_data shared/run shared/local backups
sudo chmod -R o-rwx bot1/user_data bot2/user_data dev/user_data shared/run shared/local backups
sudo find shared/local -type d -exec chmod 755 {} \;
sudo find shared/local -type f -exec chmod 644 {} \;
sudo find bot1/user_data bot2/user_data -name '*.json' -exec chmod 640 {} \;
chmod 600 .env.shared bot1/.env bot2/.env dev/.env
for d in bot1/user_data bot2/user_data dev/user_data shared/run shared/local backups; do
  sudo setfacl -R -m  "u:${FTUID}:rwX"   "$d";  sudo setfacl -R -d -m "u:${FTUID}:rwX"   "$d"
  sudo setfacl -R -m  "u:${HOSTUID}:rwX" "$d";  sudo setfacl -R -d -m "u:${HOSTUID}:rwX" "$d"
done
ls -ln bot2/user_data/config.json bot2/.env
getfacl -p bot2/user_data/config.json | head
```
Regole: mai `user:` nel compose; niente `chown` sui `.env`; log su stdout; dopo nuovi file in
`shared/local`: `sudo setfacl -R -m "u:${FTUID}:rwX" shared/local`. Permission denied → rilancia il ciclo ACL.

---

## 4. Sicurezza

### 4.1 Hardening host (comandi inline)

> Tieni aperta la sessione SSH corrente finché non verifichi una **nuova** sessione.

```bash
# SSH
sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.backup
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?MaxAuthTries.*/MaxAuthTries 3/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?X11Forwarding.*/X11Forwarding no/' /etc/ssh/sshd_config
grep -q '^AllowUsers' /etc/ssh/sshd_config || echo "AllowUsers ubuntu opc" | sudo tee -a /etc/ssh/sshd_config
printf 'PasswordAuthentication no\nPermitRootLogin no\n' | sudo tee /etc/ssh/sshd_config.d/99-hardening.conf
sudo systemctl restart ssh
sudo sshd -T | grep -Ei 'permitrootlogin|passwordauthentication|allowusers|maxauthtries'
```
```bash
# Firewall + fail2ban
sudo apt install -y ufw fail2ban
sudo ufw allow 22/tcp
sudo ufw allow 41641/udp
sudo ufw --force enable
sudo tee /etc/fail2ban/jail.local >/dev/null <<'EOF'
[sshd]
enabled = true
port = 22
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 3600
findtime = 600
EOF
sudo tee /etc/fail2ban/jail.d/recidive.local >/dev/null <<'EOF'
[recidive]
enabled = true
backend = pyinotify
filter = recidive
logpath = /var/log/fail2ban.log
action = iptables-allports[name=recidive]
bantime = -1
findtime = 86400
maxretry = 2
EOF
sudo systemctl restart fail2ban
```
```bash
# Servizi inutili, patch, orologio
sudo systemctl stop rpcbind rpcbind.socket 2>/dev/null || true
sudo systemctl disable rpcbind rpcbind.socket 2>/dev/null || true
sudo systemctl mask rpcbind rpcbind.socket
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
sudo systemctl enable --now systemd-timesyncd
timedatectl status | grep -E 'Time zone|synchronized'
```
```bash
# Avvio flotta al boot
sudo tee /etc/systemd/system/trading-fleet.service >/dev/null <<EOF
[Unit]
Description=Trading Fleet
Requires=docker.service
After=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$HOME/trading-fleet
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=300
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now trading-fleet
```
**Oracle Security List (manuale, console)**:_subnet → Default Security List → ingress solo TCP 22 +
UDP 41641; rimuovi 80/443/808x.

```bash
# Verify host
sudo systemctl is-active docker trading-fleet fail2ban ufw systemd-timesyncd
sudo ufw status verbose
sudo ss -tulpen | grep -E 'LISTEN|UNCONN' | awk '{print $1, $5}' | sort -u
sudo fail2ban-client status
sudo fail2ban-client get recidive logpath
systemctl is-enabled rpcbind rpcbind.socket 2>&1
```
Atteso porte: solo `127.0.0.1:808x`, 22, UDP 41641. Altre porte pubbliche = STOP.

### 4.2 Tailscale + sicurezza bot
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up && sudo tailscale set --operator=$USER
tailscale serve --bg --https=443  http://127.0.0.1:8081
tailscale serve --bg --https=8443 http://127.0.0.1:8082
tailscale serve status
N=3; PORT=$(( (N+6)*1000+443 )); tailscale serve --bg --https=$PORT http://127.0.0.1:808$N
```
Niente Funnel. Aggiungi a mano gli origin `https://<host>.ts.net[:porta]` ai `CORS_origins` di ogni config.
```bash
# segreti stretti + anti-leak
chmod 600 .env.shared bot*/.env dev/.env
sudo find bot*/user_data -name '*.json' -exec chmod 640 {} \;
git check-ignore -v bot1/.env bot1/user_data/config.json
git rm --cached bot1/.env bot1/user_data/config.json .env.shared   # solo se già tracciati; poi ruota
# credenziali univoche + rotazione (incolla nei .env reali)
openssl rand -hex 32; openssl rand -hex 32; openssl rand -base64 24
docker compose up -d --force-recreate srv-bot1 srv-bot2
docker compose config >/dev/null && echo compose-ok
```
**Manuale (console exchange/wallet)**: API key senza withdraw + IP whitelist + 2FA + una per bot;
Hyperliquid API agent + vault/subaccount. Mai segreti in chat/log/commit.

---

## 5. Aggiornamenti
```bash
cd freqtrade-ultimate && git pull --ff-only origin main
docker build --platform linux/arm64 -t freqtrade-ultimate:latest .
cd .. && docker compose up -d --force-recreate srv-bot1 srv-bot2   # adatta ai bot attivi
# strategie locali: modifica in shared/local → reload/restart
# config reale: modifica → restart. LIVE: finestra calma (/stopentry prima; stop manuali se slitta)
# template: aggiorna i .example committati (senza segreti) se cambi struttura
```

---

## 6. Manutenzione
```bash
# backup a caldo (per ogni bot live; esempio bot2) + rotazione
docker compose exec -T srv-bot2 python -c "import sqlite3;s=sqlite3.connect('file:/freqtrade/user_data/tradesv3.sqlite?mode=ro',uri=True);d=sqlite3.connect('/freqtrade/user_data/tradesv3-backup.sqlite');s.backup(d);d.close();s.close();print('ok')"
mv bot2/user_data/tradesv3-backup.sqlite backups/bot2-$(date +%Y%m%d).sqlite
find backups -name 'bot2-*.sqlite' -mtime +7 -delete
df -h
```
| Cosa | Policy |
|---|---|
| DB live | Mai purgare / mai `rm` |
| `backups/` | 7 giornalieri + 4 settimanali + offsite |
| `dev/user_data/*results/` | Prune periodico |
| Log | Già rototati (10m×3) |
| Cache ftcache | Effimera, azzerabile a daemon fermo. Non è il bundle dati |

| Sintomo | Fix |
|---|---|
| Due daemon | Socket divergente → allinea `/ftcache/ftcache.sock` |
| `422 deserialize` HL | camelCase vuoti o in `.env` → reali in `config.json` |
| pairlist offline | Idle 900s: normale |
| Bot in attesa | `depends_on` ftcache non healthy → risolvi daemon |
| Permission denied | Rilancia §3 |
| FreqUI non apre | `tailscale serve status` + CORS + porta |
| segreti in git | `git rm --cached` + ruota |

---

## 7. Aggiungere un bot (botN)
```bash
N=3
mkdir -p bot$N/user_data
cp bot1/.env.example bot$N/.env
cp bot1/user_data/config.example.json bot$N/user_data/config.json
chmod 600 bot$N/.env
nano bot$N/.env
nano bot$N/user_data/config.json
```
Compose — aggiungi (adatta nome/porte/strategia; **niente** `user:`):
```yaml
  srv-bot3:
    <<: *defaults
    image: freqtrade-ultimate:latest
    container_name: bot3
    env_file: [.env.shared, ./bot3/.env]
    depends_on: { srv-ftcache: { condition: service_healthy } }
    volumes:
      - ./bot3/user_data:/freqtrade/user_data
      - ./shared/local:/local:ro
      - ./shared/run:/ftcache:rw
      - ./freqtrade-ultimate/user_data/data:/freqtrade/user_data/data
    ports: ["127.0.0.1:8083:8080"]
    command: >
      trade --config /freqtrade/user_data/config.json --strategy <Strategia>
```
```bash
FTUID=$(docker run --rm --entrypoint id freqtrade-ultimate:latest | grep -oP 'uid=\K\d+'); HOSTUID=$(id -u)
sudo chown -R "$HOSTUID:$HOSTUID" bot$N/user_data
sudo chmod -R o-rwx bot$N/user_data
sudo find bot$N/user_data -name '*.json' -exec chmod 640 {} \;
sudo setfacl -R -m "u:${FTUID}:rwX" bot$N/user_data; sudo setfacl -R -d -m "u:${FTUID}:rwX" bot$N/user_data
sudo setfacl -R -m "u:${HOSTUID}:rwX" bot$N/user_data; sudo setfacl -R -d -m "u:${HOSTUID}:rwX" bot$N/user_data
PORT=$(( (N+6)*1000+443 )); tailscale serve --bg --https=$PORT http://127.0.0.1:808$N
docker compose up -d srv-bot$N
docker logs --tail 50 bot$N
docker compose exec -T srv-bot$N python -m freqtrade.ohlcv_cache.healthcheck --socket /ftcache/ftcache.sock
```
Go-live: DB vergine (`tradesv3-live.sqlite`, mai quello dry), sizing/leva validati, mount `:ro`,
poi `up -d --force-recreate`. **Mai** `compose run` sul live.

---

## 8. Committato vs locale
| Committato (repo privata) | Locale / ignorato |
|---|---|
| compose, strategie `shared/local/`, template `.example`, tooling | `config.json`, `.env`, DB, log, `shared/run/`, `dev/user_data/`, `backups/`, cache |

## Risorse
[Freqtrade](https://www.freqtrade.io/) · [Ultimate](https://github.com/titouannwtt/freqtrade-ultimate) ·
[Tailscale](https://tailscale.com/kb/) · [Oracle Free](https://www.oracle.com/cloud/free/)
## Risorse

- [Freqtrade](https://www.freqtrade.io/) · [Freqtrade Ultimate](https://github.com/titouannwtt/freqtrade-ultimate)
- [Tailscale](https://tailscale.com/kb/) · [Oracle Cloud Free](https://www.oracle.com/cloud/free/)
