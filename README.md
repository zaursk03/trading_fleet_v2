# 🤖 Trading Fleet — Freqtrade Ultimate multi-bot

Fleet Docker di bot Freqtrade (fork Ultimate) con cache OHLCV condivisa, isolamento per-bot
(config + DB separati) e accesso remoto **solo via Tailscale**.

**Repo privata.** Versiona: infra, strategie, template `.example`, tooling.
**Non** versiona: file reali con segreti (`config.json`, `.env`), DB, log, runtime IPC, dati di ricerca, artifact UI.

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

- **Due magazzini dati** — Bundle ricerca = `./shared/data` (**fuori dal clone git**, ~2 GB, `:ro` sui
  live, `rw` su dev). Cache live = ftcache (effimera). Il bundle è uno **snapshot statico**: si refresha
  forward periodicamente (§6.2); il live non lo usa (ftcache fetcha live).
- **Fork light** — Clone del fork **sparse (~130 MB), senza bundle candele**; i dati vivono solo in
  `shared/data`. `.dockerignore` esclude `user_data/`: l'immagine non contiene dati in ogni caso.
- **ftcache** — Socket **unica stringa** `/ftcache/ftcache.sock` in compose (`command` + `healthcheck`
  di `srv-ftcache`), config di ogni bot e config dev. Se un punto diverge → due daemon, budget
  dimezzato (split-brain). I bot `depends_on: srv-ftcache {condition: service_healthy}`: se il daemon
  non parte, i bot restano in attesa.
- **Pannello stato FreqUI** — Il fork interroga il socket di **default** (`/tmp/ftcache-<uid>.sock`),
  non quello configurato: senza accorgimento mostra "offline" anche col daemon sano. I bot hanno un
  **wrapper entrypoint** che symlinka il default al condiviso (§7). Source of truth: healthcheck CLI (§2.5).
- **srv-dev** — Task con `docker compose run --rm srv-dev <cmd>` (avvia ftcache se spento).
  **Mai** `compose run` su un servizio **live**: seconda istanza con stesso DB/wallet.
- **Mount strategie** — `:ro` sui live, `:rw` su dev.

---

## 2. Installazione

> Comandi bash: su **Windows** in **WSL2**, su Linux/macOS nel terminale.

### 2.1 Prerequisiti
- **Windows**: WSL2 + Docker Desktop ("Use the WSL 2 based engine") + Git.
- **Linux**: `sudo apt install git docker.io docker-compose-v2 curl acl` +
  `sudo usermod -aG docker $USER && newgrp docker`.
- **Oracle Always Free**: VM.Standard.A1.Flex, Ubuntu 22.04 ARM, 2 OCPU/12 GB (max 2 VM), boot 200 GB.
  Immagini multi-arch: ARM64 nativo ok.
- **Chiave SSH** Ed25519 con passphrase: `ssh-keygen -t ed25519 -f ~/.ssh/oracle_fleet`;
  `chmod 700 ~/.ssh && chmod 600 ~/.ssh/oracle_fleet`. Backup in password manager + USB criptata.
- **Disco**: seed temporaneo ~2 GB + `shared/data` ~2 GB; verifica `df -h` prima di §2.3.

### 2.2 Clone + build (fork LIGHT)
```bash
git clone <repo-privata> trading-fleet && cd trading-fleet        # auth: SSH key o PAT
git clone https://github.com/iterativv/NostalgiaForInfinity.git shared/NostalgiaForInfinity
# fork LIGHT (~130 MB): codice SENZA bundle candele (procedura ufficiale del fork)
git clone --filter=blob:none --sparse https://github.com/titouannwtt/freqtrade-ultimate.git freqtrade-ultimate
git -C freqtrade-ultimate sparse-checkout set --no-cone '/*' '!user_data/data'
docker build --platform linux/$(dpkg --print-architecture) -t freqtrade-ultimate:latest ./freqtrade-ultimate
```
- `--filter=blob:none` + sparse-checkout escludono `user_data/data`: clone ~130 MB.
- Il build esegue `freqtrade install-ui` → **frequi-ultimate latest** baked nell'immagine (§5.2).
- **Clone esistente full/dirty?** Preserva i dati in `shared/data` (§2.3), poi
  `mv freqtrade-ultimate freqtrade-ultimate.old`, riclona light, rebuild, e dopo verify `rm -rf …old`.

### 2.3 Seed storico (~2 GB) in `shared/data`, poi refresh lì
Il bundle del fork va **più indietro del limite API 5000 candele**: seedalo una tantum con un clone
temporaneo, poi eliminalo. Il clone principale resta light. I refresh correnti scrivono in `shared/data`.
```bash
mkdir -p shared/data
git clone --filter=blob:none https://github.com/titouannwtt/freqtrade-ultimate.git /tmp/ft-seed   # ~2 GB, con dati
cp -a /tmp/ft-seed/user_data/data/. shared/data/
rm -rf /tmp/ft-seed
# refresh forward (append candele recenti) — periodico, vedi §6.2
docker compose run --rm srv-dev download-data --config /local/sviluppo/config-dev.json --timeframes 15m 5m 1h
```
`.gitignore` della repo fleet: `shared/data/` (già presente).

### 2.4 Template → file reali, poi riempi i segreti
```bash
cp .env.shared.example .env.shared
cp bot1/.env.example bot1/.env
cp bot1/user_data/config.example.json bot1/user_data/config.json
cp bot2/.env.example bot2/.env
cp bot2/user_data/config.example.json bot2/user_data/config.json
cp dev/.env.example dev/.env
chmod 600 .env.shared bot*/.env dev/.env
```
Riempi i **reali** (mai i `.example`):
- `.env.shared`: `TZ=UTC` (dati/funding/candele sono UTC), `TELEGRAM__CHAT_ID`, `FIAT_DISPLAY_CURRENCY`.
- `botN/.env`: `TELEGRAM__TOKEN` + `API_SERVER__USERNAME/PASSWORD/WS_TOKEN/JWT_SECRET_KEY` **univoci
  per bot** (`openssl rand -hex 32`, `-base64 24`), mai vuoti.
- `dev/.env`: solo API/Telegram se servono; **nessuna chiave live**.
- **Hyperliquid**: `walletAddress`/`privateKey` (camelCase) nel `config.json` reale, **non** in `.env`
  (il loader env lowercase-a le chiavi → 422 al init).
- Copia **offsite criptata** di config+env: unica fonte per ricostruire un bot.

```bash
git status --short        # NON deve elencare .env / config.json reali
git check-ignore -v bot1/.env bot1/user_data/config.json
```

### 2.5 Permessi (§3), poi avvio + verify
Applica **§3 prima del primo avvio** (Docker creerebbe i bind-mount mancanti come root).
```bash
docker compose up -d srv-ftcache
docker compose up -d srv-bot1 srv-bot2
docker compose exec -T srv-bot2 python -m freqtrade.ohlcv_cache.healthcheck --socket /ftcache/ftcache.sock
# atteso: ONLINE; clients = numero bot avviati; positions puts > 0 se un bot è live
```

---

## 3. Permessi & UID

UID **non fissi** (Oracle `opc`=1000 sposta `ubuntu`=1001; `ftuser` varia): rilevali sempre.
Pre-crea le dir con ownership+ACL (Docker crea i bind-mount mancanti come **root**).
`cp -a` (seed) preserva ownership misti: normalizza con `chown -R` (incluso `shared/data`),
altrimenti unlink/write da host danno **EACCES**.

```bash
FTUID=$(docker run --rm --entrypoint id freqtrade-ultimate:latest | grep -oP 'uid=\K\d+')
HOSTUID=$(id -u); HOSTGID=$(id -g)

mkdir -p bot1/user_data bot2/user_data dev/user_data shared/run shared/local shared/data backups
sudo chown -R "$HOSTUID:$HOSTGID" bot1/user_data bot2/user_data dev/user_data shared/run shared/local shared/data backups
sudo chmod -R o-rwx bot1/user_data bot2/user_data dev/user_data shared/run shared/local backups
sudo find shared/local -type d -exec chmod 755 {} \;
sudo find shared/local -type f -exec chmod 644 {} \;
sudo find bot1/user_data bot2/user_data -name '*.json' -exec chmod 640 {} \;
chmod 600 .env.shared bot1/.env bot2/.env dev/.env

# ACL container + host + default. shared/data: niente o-rwx (dati non segreti).
for d in bot1/user_data bot2/user_data dev/user_data shared/run shared/local shared/data; do
  sudo setfacl -R -m  "u:${FTUID}:rwX"   "$d";  sudo setfacl -R -d -m "u:${FTUID}:rwX"   "$d"
  sudo setfacl -R -m  "u:${HOSTUID}:rwX" "$d";  sudo setfacl -R -d -m "u:${HOSTUID}:rwX" "$d"
done

ls -ln bot2/user_data/config.json bot2/.env
getfacl -p bot2/user_data/config.json | head
docker compose run --rm srv-dev sh -c 'touch /freqtrade/user_data/data/.wtest && rm /freqtrade/user_data/data/.wtest && echo write-ok'
```
Regole: **mai** `user:` nel compose; **niente** `chown` sui `.env`; log su **stdout**; dopo nuovi file in
`shared/local`: `sudo setfacl -R -m "u:${FTUID}:rwX" shared/local`; niente `sudo` quotidiano.
EACCES su unlink/write → rilancia chown+ACL sulla dir (ownership padre conta per unlink).

---

## 4. Sicurezza

### 4.1 Hardening host (comandi inline)

> ⚠️ Tieni aperta la sessione SSH corrente finché non verifichi una **nuova** sessione.

```bash
# --- SSH ---
sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.backup
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?MaxAuthTries.*/MaxAuthTries 3/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?X11Forwarding.*/X11Forwarding no/' /etc/ssh/sshd_config
grep -q '^AllowUsers' /etc/ssh/sshd_config || echo "AllowUsers ubuntu opc" | sudo tee -a /etc/ssh/sshd_config
# Include sshd_config.d/*.conf in CIMA, primo valore ottenuto vince: 00-* batte 50-cloud-init.
printf 'PasswordAuthentication no\nPermitRootLogin no\n' | sudo tee /etc/ssh/sshd_config.d/00-hardening.conf
sudo systemctl restart ssh
sudo sshd -T | grep -Ei 'permitrootlogin|passwordauthentication|allowusers|maxauthtries'
```
```bash
# --- Firewall + fail2ban ---
sudo apt install -y ufw fail2ban python3-pyinotify
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
> Docker bypassa ufw sulle porte **pubblicate**; qui i bot sono `127.0.0.1` → non esposti. Mai `0.0.0.0:808x`.

```bash
# --- Servizi inutili, patch, orologio ---
sudo systemctl stop rpcbind rpcbind.socket 2>/dev/null || true
sudo systemctl disable rpcbind rpcbind.socket 2>/dev/null || true
sudo systemctl mask rpcbind rpcbind.socket
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
sudo systemctl enable --now systemd-timesyncd
timedatectl status | grep -E 'Time zone|synchronized'
```
```bash
# --- Avvio flotta al boot ---
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
**Oracle Security List (manuale)**: Subnet → Default Security List → ingress solo TCP 22 (+ tuo IP/32)
e UDP 41641; rimuovi 80/443/808x.

```bash
# --- Verify host ---
sudo systemctl is-active docker trading-fleet fail2ban ufw systemd-timesyncd
sudo ufw status verbose
sudo ss -tulpen | grep -E 'LISTEN|UNCONN' | awk '{print $1, $5}' | sort -u
sudo fail2ban-client status
sudo fail2ban-client get recidive logpath
systemctl is-enabled rpcbind rpcbind.socket 2>&1
```
Atteso: solo `127.0.0.1:808x`, 22, UDP 41641. Altra porta pubblica = **STOP**.

### 4.2 Tailscale + sicurezza dei bot
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up && sudo tailscale set --operator=$USER
tailscale serve --bg --https=443  http://127.0.0.1:8081
tailscale serve --bg --https=8443 http://127.0.0.1:8082
tailscale serve status
# botN: N=3; PORT=$(( (N+6)*1000+443 )); tailscale serve --bg --https=$PORT http://127.0.0.1:808$N
```
Niente Funnel. CORS = origin `https://<host>.ts.net[:porta]` in ogni config.
```bash
chmod 600 .env.shared bot*/.env dev/.env
sudo find bot*/user_data -name '*.json' -exec chmod 640 {} \;
git check-ignore -v bot1/.env bot1/user_data/config.json
git rm --cached --ignore-unmatch bot1/.env bot1/user_data/config.json .env.shared   # solo se tracciati; poi ruota
openssl rand -hex 32; openssl rand -hex 32; openssl rand -base64 24
docker compose up -d --force-recreate srv-bot1 srv-bot2
docker compose config >/dev/null && echo compose-ok
```
**Manuale (exchange/wallet)**: API key senza withdraw + IP whitelist + read/trade + una per bot + 2FA;
Hyperliquid API agent + vault/subaccount; ruota JWT/ws/password periodicamente e dopo esposizioni.

---

## 5. Aggiornamenti

### 5.1 Fork + immagine
```bash
git -C freqtrade-ultimate pull --ff-only origin main
# force-push / clone diverge ("fatal: Not possible to fast-forward"):
git -C freqtrade-ultimate fetch origin
git -C freqtrade-ultimate reset --hard origin/main   # dati in shared/data, clone light preservato
docker build --platform linux/$(dpkg --print-architecture) -t freqtrade-ultimate:latest ./freqtrade-ultimate
docker compose up -d --force-recreate srv-ftcache srv-bot1 srv-bot2   # finestra calma (bot2 live)
```
- Sparse-checkout **persiste** across pull/reset: clone resta light, niente feather dirty.
- Rebuild aggiorna anche la UI (§5.2). Senza build, recreate è inutile.
- **Live**: finestra calma (`/stopentry`; stoploss locale giù ~1 min; stop manuali se slitta). ftcache primo.
- Builder legacy deprecated: `docker buildx build` / `DOCKER_BUILDKIT=1`.

### 5.2 FreqUI Ultimate
**Via primaria = rebuild** (§5.1): install-ui fetcha frequi-ultimate latest → baked. Hard-refresh
(Ctrl+Shift+R); verifica footer/about.
**Alternativa senza rebuild** (zip + mount, solo se dir popolata; mount vuoto nasconde la baked):
```bash
VER=v2.2.1
mkdir -p shared/frequi/installed
curl -L -o /tmp/frequi-ultimate-ui-$VER.zip https://github.com/titouannwtt/frequi-ultimate/releases/download/$VER/frequi-ultimate-ui-$VER.zip
curl -L -o /tmp/SHA256SUMS https://github.com/titouannwtt/frequi-ultimate/releases/download/$VER/SHA256SUMS
(cd /tmp && sha256sum -c SHA256SUMS)
python3 -m zipfile -e /tmp/frequi-ultimate-ui-$VER.zip shared/frequi/installed/
```
```yaml
    volumes:
      - ./shared/frequi/installed:/freqtrade/freqtrade/rpc/api_server/ui/installed:ro
```
Oppure `docker cp … botN:<UI_PATH>/` (effimero). `shared/frequi/` gitignored. Ogni bot sua copia.

### 5.3 NostalgiaForInfinity
```bash
git -C shared/NostalgiaForInfinity pull --ff-only origin main
docker compose restart srv-bot1     # dry: sicuro; live: finestra calma
# o Telegram /reload_config
```
Mount `:ro`; pin tag opzionale; config `/nfi-repo/configs/…` invariati.
⚠️ NFI lookahead bias: metriche backtest non credibili come prova di edge.

### 5.4 Strategie locali, config, template
- `shared/local`: modifica → reload/restart.
- Config reale: modifica → restart (live: finestra calma).
- `.example`: aggiorna senza segreti se cambi struttura.

---

## 6. Manutenzione

### 6.1 Backup DB
```bash
docker compose exec -T srv-bot2 python -c "import sqlite3;s=sqlite3.connect('file:/freqtrade/user_data/tradesv3.sqlite?mode=ro',uri=True);d=sqlite3.connect('/freqtrade/user_data/tradesv3-backup.sqlite');s.backup(d);d.close();s.close();print('ok')"
# (sqlite3 CLI presente: alternativa  sqlite3 … ".backup …")
mv bot2/user_data/tradesv3-backup.sqlite backups/bot2-$(date +%Y%m%d).sqlite
find backups -name 'bot2-*.sqlite' -mtime +7 -delete
```
Backup auto-al-stop del fork = locale e solo allo stop: serve snapshot a caldo + rotazione + offsite.
Cadenza: giornaliero + pre-operazioni rischiose.

### 6.2 Bundle dati: refresh forward e re-seed
Il bundle in `shared/data` è **statico**: non si aggiorna da solo.
- **Backtest/replay su finestre storiche fisse**: nessun update needed.
- **Backtest/replay che includono il recente/oggi**: **refresh forward** periodico (append):
  ```bash
  docker compose run --rm srv-dev download-data --config /local/sviluppo/config-dev.json --timeframes 15m 5m 1h
  # aggiungi 1m e funding se usi detail/replay:
  docker compose run --rm srv-dev download-data --config /local/sviluppo/config-dev.json --timeframes 1m
  ```
  Cadenza: settimanale, o prima di backtest che includono oggi. Il **live non usa il bundle** (ftcache).
- **Re-seed occasionale** (fork estende bundle: nuove coppie/storia più profonda): temp clone → copia →
  rm → **ri-refresh forward** (la copia sovrascrive la coda refreshata):
  ```bash
  git clone --filter=blob:none https://github.com/titouannwtt/freqtrade-ultimate.git /tmp/ft-seed
  cp -a /tmp/ft-seed/user_data/data/. shared/data/
  rm -rf /tmp/ft-seed
  docker compose run --rm srv-dev download-data --config /local/sviluppo/config-dev.json --timeframes 15m 5m 1h
  ```
- Verifica profondità:
  ```bash
  docker compose run --rm --entrypoint python srv-dev -c "import pandas as pd; df=pd.read_feather('/freqtrade/user_data/data/hyperliquid/futures/BTC_USDC_USDC-15m-futures.feather'); print(df['date'].min(), df['date'].max(), len(df))"
  ```

### 6.3 Prune e disco
```bash
find dev/user_data/backtest_results -type f -mtime +30 -delete 2>/dev/null
find dev/user_data/hyperopt_results -type f -mtime +30 -delete 2>/dev/null
df -h
```

| Cosa | Policy |
|---|---|
| DB live | **Mai** purgare / mai `rm` |
| `backups/` | 7 giornalieri + 4 settimanali + offsite |
| `dev/user_data/*results/` | Prune periodico |
| `shared/data/` | Refresh forward periodico (§6.2); re-seed raro; prune finestre inutili; offsite se vuoi versionare |
| Log | Rototati (10m×3) |
| Cache ftcache | Effimera, azzerabile a daemon fermo. **Non** è il bundle |
| Disco | `df -h`: disk-full su live = write SQLite falliti |

| Sintomo | Causa / fix |
|---|---|
| Backtest/replay recente mostra dati vecchi | Bundle statico → refresh forward (§6.2) |
| Clone pesante / feather dirty | Clone light sparse (§2.2); dati in shared/data |
| `pull --ff-only` abort / forced update | fetch + reset --hard (sparse preservato) |
| `shared/data` EACCES (unlink/write) | Ownership padre dopo cp -a/sudo → chown+ACL §3 |
| Seed leftover | `rm -rf /tmp/ft-seed` |
| Due daemon / budget dimezzato | Socket divergente → allinea `/ftcache/ftcache.sock` |
| Pannello ftcache offline ma CLI ONLINE | Probe default /tmp; wrapper symlink (§7) |
| UI vuota dopo mount | Mount shared/frequi vuoto nasconde baked → rimuovi/popola |
| FreqUI versione vecchia | Hard-refresh; per-bot; rebuild |
| sha256 FAILED / unzip missing | Nome zip match SHA256SUMS; python3 -m zipfile |
| 422 Hyperliquid | camelCase reali in config.json, non .env |
| ftpairlist offline | Idle 900s normale |
| Bot in attesa | depends_on ftcache non healthy |
| FreqUI non apre | serve status + CORS + porta |
| segreti in git | rm --cached + ruota |
| WAL enorme / disk-full | Checkpoint calma; libera; offsite |

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
      - ./shared/data:/freqtrade/user_data/data:ro
    ports: ["127.0.0.1:8083:8080"]
    entrypoint: ["/bin/sh", "-c"]
    command:
      - >-
        ln -sf /ftcache/ftcache.sock /tmp/ftcache-$$(id -u).sock &&
        exec freqtrade trade
        --config /freqtrade/user_data/config.json
        --strategy <Strategia>
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
**Go-live**: DB vergine (`tradesv3-live.sqlite`), sizing/leva validati, mount `:ro`, segreti reali.
Poi `up -d --force-recreate`. **Mai** `compose run` sul live.

---

## 8. Committato vs locale

| Committato (repo privata) | Locale / ignorato |
|---|---|
| compose, strategie `shared/local/`, template `.example`, tooling | config.json, .env, .env.shared, DB, log, shared/run/, dev/user_data/, backups/, cache, **shared/data/**, shared/frequi/, cloni esterni (fork light, NFI) |

---

## Risorse
- [Freqtrade](https://www.freqtrade.io/) · [Freqtrade Ultimate](https://github.com/titouannwtt/freqtrade-ultimate)
- [FreqUI Ultimate](https://github.com/titouannwtt/frequi-ultimate/releases) · [NFI](https://github.com/iterativv/NostalgiaForInfinity)
- [Tailscale](https://tailscale.com/kb/) · [Oracle Free](https://www.oracle.com/cloud/free/)
