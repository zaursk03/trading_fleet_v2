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

- **Due magazzini dati** — Bundle ricerca = `./shared/data` (**fuori dal clone git**, `:ro` sui live,
  `rw` su dev). Cache live = ftcache (effimera). Non confonderli: il bundle non si resetta come la cache.
- **ftcache** — Socket **unica stringa** `/ftcache/ftcache.sock` in compose (`command` + `healthcheck`
  di `srv-ftcache`), config di ogni bot e config dev. Se un punto diverge → due daemon, budget
  dimezzato (split-brain). I bot hanno `depends_on: srv-ftcache {condition: service_healthy}`:
  se il daemon non parte, i bot restano in attesa.
- **Pannello stato FreqUI** — Il fork interroga il socket di **default** (`/tmp/ftcache-<uid>.sock`),
  non quello configurato: senza accorgimento mostra "offline" anche col daemon sano. I bot hanno un
  **wrapper entrypoint** che symlinka il default al condiviso (§7), così il pannello è veritiero.
  Source of truth operativa: healthcheck CLI (§2.5).
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

### 2.2 Clone + build
```bash
git clone <repo-privata> trading-fleet && cd trading-fleet        # auth: SSH key o PAT
git clone https://github.com/titouannwtt/freqtrade-ultimate.git freqtrade-ultimate
git clone https://github.com/iterativv/NostalgiaForInfinity.git shared/NostalgiaForInfinity
docker build --platform linux/$(dpkg --print-architecture) -t freqtrade-ultimate:latest ./freqtrade-ultimate
```
Il build esegue `freqtrade install-ui`, che scarica **frequi-ultimate latest**: la UI è baked nell'immagine.

### 2.3 Dati di ricerca in `shared/data` (fuori dal clone git)
Il bundle committato nel fork è solo un **seed**: copialo una volta in `shared/data`, poi i download
dev scrivono lì e il clone resta pulito (pull/reset indolori).
```bash
mkdir -p shared/data
cp -a freqtrade-ultimate/user_data/data/. shared/data/
# in alternativa, scarica fresco con srv-dev (download-data) dopo l'avvio
```
Aggiungi al `.gitignore` della repo fleet: `shared/data/`.

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
- Copia **offsite criptata** di config+env: non essendo nel repo, sono l'unica fonte per ricostruire un bot.

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

UID **non fissi** (Oracle `opc`=1000 sposta `ubuntu`=1001; `ftuser` varia per immagine): rilevali sempre.
Pre-crea le directory con ownership+ACL: Docker crea i bind-mount mancanti come **root** → permission denied.
`cp -a` preserva ownership misti: normalizza con `chown -R` (incluso `shared/data`).

```bash
FTUID=$(docker run --rm --entrypoint id freqtrade-ultimate:latest | grep -oP 'uid=\K\d+')
HOSTUID=$(id -u); HOSTGID=$(id -g)

mkdir -p bot1/user_data bot2/user_data dev/user_data shared/run shared/local shared/data backups
sudo chown -R "$HOSTUID:$HOSTGID" bot1/user_data bot2/user_data dev/user_data shared/run shared/local shared/data backups
sudo chmod -R o-rwx bot1/user_data bot2/user_data dev/user_data shared/run shared/local backups
sudo find shared/local -type d -exec chmod 755 {} \;
sudo find shared/local -type f -exec chmod 644 {} \;
sudo find bot1/user_data bot2/user_data -name '*.json' -exec chmod 640 {} \;   # config: owner RW, group R
chmod 600 .env.shared bot1/.env bot2/.env dev/.env                              # env: solo owner

# ACL container + host, anche in default per i file futuri.
# shared/data: niente o-rwx (dati non segreti); la write dev passa dall'ACL, la read dei bot pure.
for d in bot1/user_data bot2/user_data dev/user_data shared/run shared/local shared/data; do
  sudo setfacl -R -m  "u:${FTUID}:rwX"   "$d";  sudo setfacl -R -d -m "u:${FTUID}:rwX"   "$d"
  sudo setfacl -R -m  "u:${HOSTUID}:rwX" "$d";  sudo setfacl -R -d -m "u:${HOSTUID}:rwX" "$d"
done

ls -ln bot2/user_data/config.json bot2/.env
getfacl -p bot2/user_data/config.json | head
# verify write dev su shared/data:
docker compose run --rm srv-dev sh -c 'touch /freqtrade/user_data/data/.wtest && rm /freqtrade/user_data/data/.wtest && echo write-ok'
```
Regole d'oro: **mai** `user:` nel compose; **niente** `chown` sui `.env` (Docker li legge come utente host);
log su **stdout**; dopo nuovi file in `shared/local`: `sudo setfacl -R -m "u:${FTUID}:rwX" shared/local`;
niente `sudo` nell'operatività quotidiana. Permission denied → rilancia il ciclo ACL sulla dir incriminata.

---

## 4. Sicurezza

### 4.1 Hardening host (comandi inline)

> ⚠️ Tieni aperta la sessione SSH corrente finché non verifichi l'accesso con una **nuova** sessione.

```bash
# --- SSH ---
sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.backup
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?MaxAuthTries.*/MaxAuthTries 3/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?X11Forwarding.*/X11Forwarding no/' /etc/ssh/sshd_config
grep -q '^AllowUsers' /etc/ssh/sshd_config || echo "AllowUsers ubuntu opc" | sudo tee -a /etc/ssh/sshd_config
# Include sshd_config.d/*.conf sta in CIMA e sshd usa il PRIMO valore ottenuto:
# un file 00-* ordina prima di 50-cloud-init.conf e vince davvero (un 99-* perderebbe).
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
> Nota: Docker bypassa ufw sulle porte **pubblicate**; qui i bot sono bindati su `127.0.0.1`,
> quindi non esposti. Non pubblicare mai `0.0.0.0:808x`.

```bash
# --- Servizi inutili, patch automatiche, orologio ---
sudo systemctl stop rpcbind rpcbind.socket 2>/dev/null || true
sudo systemctl disable rpcbind rpcbind.socket 2>/dev/null || true
sudo systemctl mask rpcbind rpcbind.socket
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
sudo systemctl enable --now systemd-timesyncd
timedatectl status | grep -E 'Time zone|synchronized'
```
```bash
# --- Avvio flotta al boot (WorkingDirectory = path reale della repo) ---
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
**Oracle Security List (manuale, console web)**: Compute → Instances → VM → Primary VNIC → **Subnet** →
Default Security List → Add Ingress: solo TCP 22 (source tuo IP/32 se possibile) + UDP 41641.
Rimuovi ogni ingress 80/443/808x non necessario.

```bash
# --- Verify host ---
sudo systemctl is-active docker trading-fleet fail2ban ufw systemd-timesyncd
sudo ufw status verbose
sudo ss -tulpen | grep -E 'LISTEN|UNCONN' | awk '{print $1, $5}' | sort -u
sudo fail2ban-client status
sudo fail2ban-client get recidive logpath      # atteso: /var/log/fail2ban.log
systemctl is-enabled rpcbind rpcbind.socket 2>&1   # atteso: masked
```
Atteso sulle porte: solo `127.0.0.1:808x`, `0.0.0.0:22`/`[::]:22`, UDP `41641`. Qualsiasi altra porta
pubblica = **STOP**, rimuovi la regola.

### 4.2 Tailscale + sicurezza dei bot
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up && sudo tailscale set --operator=$USER
tailscale serve --bg --https=443  http://127.0.0.1:8081   # bot1
tailscale serve --bg --https=8443 http://127.0.0.1:8082   # bot2
tailscale serve status
# botN (N>=2):  N=3; PORT=$(( (N+6)*1000+443 )); tailscale serve --bg --https=$PORT http://127.0.0.1:808$N
```
Niente Funnel. Aggiungi a mano gli origin `https://<host>.ts.net[:porta]` ai `CORS_origins` di ogni config.
```bash
# segreti stretti + anti-leak git
chmod 600 .env.shared bot*/.env dev/.env
sudo find bot*/user_data -name '*.json' -exec chmod 640 {} \;
git check-ignore -v bot1/.env bot1/user_data/config.json
# se già tracciati: rimuovi dall'index (file resta su disco), committa, POI ruota i segreti esposti
git rm --cached --ignore-unmatch bot1/.env bot1/user_data/config.json .env.shared
# credenziali univoche + rotazione (incolla nei .env reali, mai in chat/repo)
openssl rand -hex 32; openssl rand -hex 32; openssl rand -base64 24
docker compose up -d --force-recreate srv-bot1 srv-bot2
docker compose config >/dev/null && echo compose-ok
```
**Manuale (console exchange/wallet)**: API key **senza withdraw** + IP whitelist = IP VM + read/trade only +
**una key per bot** + 2FA. Hyperliquid: **API agent** (mai chiave principale) + **vault/subaccount** per
isolare il capitale. Ruota JWT/ws/password periodicamente e dopo ogni esposizione.

---

## 5. Aggiornamenti

### 5.1 Fork + immagine
```bash
git -C freqtrade-ultimate pull --ff-only origin main
# Se upstream ha force-pushato / il clone diverge ("fatal: Not possible to fast-forward"):
git -C freqtrade-ultimate fetch origin
git -C freqtrade-ultimate reset --hard origin/main   # scarta solo locale; i dati stanno in shared/data
docker build --platform linux/$(dpkg --print-architecture) -t freqtrade-ultimate:latest ./freqtrade-ultimate
docker compose up -d --force-recreate srv-ftcache srv-bot1 srv-bot2   # finestra calma (bot2 live)
```
- Senza build, `--force-recreate` ricrea con la stessa immagine: inutile.
- Il rebuild aggiorna **anche la UI** (install-ui fetcha frequi-ultimate latest, §5.2).
- **Bot live**: finestra calma (`/stopentry` prima; allo stop cadono gli ordini resting e lo stoploss
  locale è giù ~1 min; stop manuali se il restart slitta). Ricrea ftcache per primo, poi i bot.
- Builder legacy deprecated: usa `docker buildx build` / `DOCKER_BUILDKIT=1` (installa `docker-buildx-plugin`).

### 5.2 FreqUI Ultimate (install / update)
La UI vive in `freqtrade/rpc/api_server/ui/installed/` (container:
`/freqtrade/freqtrade/rpc/api_server/ui/installed`). **Via primaria = rebuild** (§5.1): il Dockerfile
esegue `install-ui` che scarica frequi-ultimate latest → UI baked, tutti i bot allineati. Hard-refresh
(Ctrl+Shift+R) dopo il recreate; verifica versione nel footer/about.

**Alternativa senza rebuild** (zip manuale + mount condiviso):
```bash
VER=v2.2.1
mkdir -p shared/frequi/installed
curl -L -o /tmp/frequi-ultimate-ui-$VER.zip \
  https://github.com/titouannwtt/frequi-ultimate/releases/download/$VER/frequi-ultimate-ui-$VER.zip
curl -L -o /tmp/SHA256SUMS \
  https://github.com/titouannwtt/frequi-ultimate/releases/download/$VER/SHA256SUMS
(cd /tmp && sha256sum -c SHA256SUMS)          # il nome zip DEVE matchare SHA256SUMS
python3 -m zipfile -e /tmp/frequi-ultimate-ui-$VER.zip shared/frequi/installed/   # o unzip -o … -d
```
Monta `:ro` su ogni bot **solo se la dir è popolata**: un mount vuoto **nasconde** la UI baked → UI rotta.
```yaml
    volumes:
      - ./shared/frequi/installed:/freqtrade/freqtrade/rpc/api_server/ui/installed:ro
```
Oppure copia subito nei container running (effimero, si perde al recreate): `docker cp shared/frequi/installed/. botN:<UI_PATH>/`.
`shared/frequi/` è artifact vendorizzato: **gitignored**. Ogni bot serve la propria copia: aggiorna tutti quelli che apri.

### 5.3 NostalgiaForInfinity (NFI)
```bash
git -C shared/NostalgiaForInfinity pull --ff-only origin main
docker compose restart srv-bot1        # bot1 dry: restart sicuro
# senza restart: Telegram /reload_config sul bot che usa NFI
```
- Mount `:ro`: pull sull'host, il bot rilegge al restart/reload.
- Pin per riproducibilità: `git -C shared/NostalgiaForInfinity fetch --tags && git -C … checkout <tag>`.
- I config NFI (`add_config_files`) puntano a `/nfi-repo/configs/…`: path invariato con l'update.
- ⚠️ **NFI ha lookahead bias documentato**: metriche di backtest **non** credibili come prova di edge.

### 5.4 Strategie locali, config, template
- Strategie in `shared/local`: modifica → reload/restart del bot che le usa.
- Config reale (ignorato): modifica → restart. **Live**: finestra calma (§5.1).
- Template `.example` committati: se cambi struttura, aggiornali **senza segreti**.

---

## 6. Manutenzione

```bash
# backup a caldo (WAL-safe, senza fermare il live). Replica per ogni bot live; esempio bot2:
docker compose exec -T srv-bot2 python -c "import sqlite3;s=sqlite3.connect('file:/freqtrade/user_data/tradesv3.sqlite?mode=ro',uri=True);d=sqlite3.connect('/freqtrade/user_data/tradesv3-backup.sqlite');s.backup(d);d.close();s.close();print('ok')"
# (sqlite3 CLI è presente nell'immagine: in alternativa  sqlite3 … ".backup …")
mv bot2/user_data/tradesv3-backup.sqlite backups/bot2-$(date +%Y%m%d).sqlite
find backups -name 'bot2-*.sqlite' -mtime +7 -delete
# prune artefatti dev (grandi, ignorati)
find dev/user_data/backtest_results -type f -mtime +30 -delete 2>/dev/null
find dev/user_data/hyperopt_results -type f -mtime +30 -delete 2>/dev/null
# prune finestre 1m vecchie in shared/data (fuori git, prune senza paura)
df -h
```
Il fork può fare backup DB al graceful shutdown, ma è **locale e solo allo stop**: serve comunque snapshot
periodico a caldo + rotazione + offsite (sopra). Cadenza: giornaliero + pre-operazioni rischiose.

| Cosa | Policy |
|---|---|
| DB live | **Mai** purgare / mai `rm`: stato posizioni + track record |
| `backups/` | 7 giornalieri + 4 settimanali + offsite |
| `dev/user_data/*results/` | Prune periodico (comando sopra) |
| `shared/data/` | Prune finestre che non servono; backup offsite se vuoi versionare i dati |
| Log | Già rototati (json-file 10m×3): bounded |
| Cache ftcache / klines | Effimera: azzerabile a daemon fermo (si ricostruisce). **Non** è il bundle dati |
| Disco | Monitora `df -h`: disk-full su live = write SQLite falliti |

| Sintomo | Causa / fix |
|---|---|
| `pull --ff-only` abort / "forced update" | Upstream force-push → `fetch` + `reset --hard origin/main` (dati in shared/data, clone pulito) |
| Feather dirty nel clone fork | Dati ora in `shared/data`; il clone non si sporca più |
| `shared/data` permission denied (dev) | Ownership+ACL §3 su `shared/data` (cp -a preserva ownership misti) |
| Due daemon / budget dimezzato | Socket path divergente → allinea `/ftcache/ftcache.sock` ovunque |
| Pannello FreqUI "ftcache offline" ma CLI ONLINE | Probe di default `/tmp/…`; wrapper symlink nei bot (§7) o patch fork |
| UI vuota/rotta dopo mount | Mount `shared/frequi` vuoto nasconde la UI baked → rimuovi il mount o popola la dir |
| FreqUI mostra versione vecchia | Hard-refresh; ogni bot ha sua copia; rebuild per UI baked |
| `sha256 … FAILED open or read` | Nome zip diverso da SHA256SUMS → rinomina/riscarica col nome originale |
| `unzip: command not found` | `python3 -m zipfile -e …` oppure `apt install unzip` |
| `422 deserialize` Hyperliquid | camelCase vuoti o in `.env` → valori reali in `config.json` |
| ftpairlist "offline" | Idle-shutdown 900s: normale |
| Bot in attesa all'avvio | `depends_on` ftcache non healthy → risolvi il daemon prima |
| Permission denied | ACL/UID: rilancia §3 |
| FreqUI non apre via Tailscale | `tailscale serve status` + CORS + porta dedicata |
| `.env`/config finiti in git | `git rm --cached --ignore-unmatch …` + ruota i segreti esposti |
| WAL enorme / disk-full | Checkpoint in finestra calma; libera spazio; backup offsite |

---

## 7. Aggiungere un bot (botN)

Esempio `N=3`; sostituisci numero e strategia.
```bash
N=3
mkdir -p bot$N/user_data
cp bot1/.env.example bot$N/.env
cp bot1/user_data/config.example.json bot$N/user_data/config.json
chmod 600 bot$N/.env
nano bot$N/.env                    # segreti UNIVOCI
nano bot$N/user_data/config.json   # camelCase HL QUI; adatta exchange/strategia/sizing
```
**Compose** — aggiungi il blocco (adatta nome/porte/strategia; **niente** `user:`; mount `:ro` se live).
Il wrapper entrypoint crea il symlink del socket di default → condiviso (pannello FreqUI veritiero)
e poi `exec`a freqtrade (PID 1, stop graceful):
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
(`$$` escaping compose; `$(id -u)` valutato nel container → uid-agnostic.)
```bash
# permessi sul nuovo bot
FTUID=$(docker run --rm --entrypoint id freqtrade-ultimate:latest | grep -oP 'uid=\K\d+'); HOSTUID=$(id -u)
sudo chown -R "$HOSTUID:$HOSTUID" bot$N/user_data
sudo chmod -R o-rwx bot$N/user_data
sudo find bot$N/user_data -name '*.json' -exec chmod 640 {} \;
sudo setfacl -R -m "u:${FTUID}:rwX" bot$N/user_data; sudo setfacl -R -d -m "u:${FTUID}:rwX" bot$N/user_data
sudo setfacl -R -m "u:${HOSTUID}:rwX" bot$N/user_data; sudo setfacl -R -d -m "u:${HOSTUID}:rwX" bot$N/user_data
# Tailscale + CORS
PORT=$(( (N+6)*1000+443 )); tailscale serve --bg --https=$PORT http://127.0.0.1:808$N
#   aggiungi a mano l'origin https://<host>.ts.net:$PORT ai CORS_origins del config del bot
# avvio + verify
docker compose up -d srv-bot$N
docker logs --tail 50 bot$N
docker compose exec -T srv-bot$N python -m freqtrade.ohlcv_cache.healthcheck --socket /ftcache/ftcache.sock
```
**Go-live**: DB **vergine** (`"db_url": "sqlite:////freqtrade/user_data/tradesv3-live.sqlite"`, mai quello
dry); sizing/leva validati; mount `:ro`; segreti reali presenti. Poi
`docker compose up -d --force-recreate srv-bot$N`. **Mai** `compose run` sul servizio live.

---

## 8. Committato vs locale

| Committato (repo privata) | Locale / ignorato |
|---|---|
| `docker-compose.yml`, strategie `shared/local/`, template `.example`, tooling | `config.json`, `.env`, `.env.shared`, DB, log, `shared/run/`, `dev/user_data/`, `backups/`, cache, `shared/data/`, `shared/frequi/`, cloni esterni (`freqtrade-ultimate/`, `shared/NostalgiaForInfinity/`) |

---

## Risorse
- [Freqtrade](https://www.freqtrade.io/) · [Freqtrade Ultimate](https://github.com/titouannwtt/freqtrade-ultimate)
- [FreqUI Ultimate releases](https://github.com/titouannwtt/frequi-ultimate/releases) ·
  [NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity)
- [Tailscale](https://tailscale.com/kb/) · [Oracle Cloud Free](https://www.oracle.com/cloud/free/)
