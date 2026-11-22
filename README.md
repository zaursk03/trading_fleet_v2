# 🤖 Trading Fleet — Freqtrade Ultimate multi-bot

Fleet Docker di bot Freqtrade (fork Ultimate) con cache OHLCV condivisa, isolamento per-bot
(config + DB separati) e accesso remoto **solo via Tailscale**.

**Repo privata.** Versiona: infrastruttura, strategie, template `.example`, tooling.
**Non** versiona: i file reali con segreti (`config.json`, `.env`), DB, log, runtime IPC.

> ⚠️ **Disclaimer**: software a scopo educativo. Il trading di criptovalute comporta rischio
> elevato e può portare alla perdita totale del capitale. Usa a tuo rischio.

**Indice**: [1 Servizi](#1-servizi) · [2 Installazione](#2-installazione) · [3 Permessi & UID](#3-permessi--uid) ·
[4 Sicurezza](#4-sicurezza) · [5 Aggiornamenti](#5-aggiornamenti) · [6 Manutenzione](#6-manutenzione) ·
[7 Aggiungere un bot](#7-aggiungere-un-bot-botn) · [8 Committato vs locale](#8-committato-vs-locale)

---

## 1. Servizi

| Servizio | Ruolo | Note |
|---|---|---|
| `srv-ftcache` | Daemon OHLCV condiviso | Un solo rate-budget per wallet; i bot sono client puri |
| `srv-ftpairlist` | Cache pairlist tra bot (opzionale) | "offline" dopo 15 min idle = **normale**, non un guasto |
| `srv-botN` | Bot live o dry-run | Uno per servizio; config e DB isolati |
| `srv-dev` | Runner usa-e-getta (download/backtest/replay) | Mai live; niente porte; limiti CPU/RAM |

Regole dei servizi speciali:

- **ftcache** — Il socket è una **unica stringa** `/ftcache/ftcache.sock` in: compose
  (`command` + `healthcheck` di `srv-ftcache`), config di ogni bot **e** config dev. Se anche un
  solo punto diverge, hai due daemon e budget dimezzato (split-brain). I bot hanno
  `depends_on: srv-ftcache {condition: service_healthy}`: se il daemon non parte, i bot restano in attesa.
- **srv-dev** — I task si lanciano con `docker compose run --rm srv-dev <cmd>`.
  **Mai** `compose run` su un servizio **live**: spawnerebbe una seconda istanza con stesso DB/wallet.
- **Mount strategie** — `:ro` sui live, `:rw` su dev.

---

## 2. Installazione

> I comandi sono bash: su **Windows** eseguili in **WSL2**; su Linux/macOS nel terminale.

### 2.1 Prerequisiti

- **Windows**: WSL2 + Docker Desktop ("Use the WSL 2 based engine") + Git.
- **Linux**: `sudo apt install git docker.io docker-compose-v2 curl acl` poi
  `sudo usermod -aG docker $USER && newgrp docker`.
- **Oracle Always Free**: VM.Standard.A1.Flex, Ubuntu 22.04 ARM, 2 OCPU / 12 GB (max 2 VM), boot 200 GB.
  Le immagini sono multi-arch: ARM64 nativo ok.
- **Chiave SSH** Ed25519 con passphrase: `ssh-keygen -t ed25519 -f ~/.ssh/oracle_fleet`;
  `chmod 700 ~/.ssh && chmod 600 ~/.ssh/oracle_fleet`. Backup in password manager + USB criptata.

### 2.2 Clone + build

```bash
git clone <repo-privata> trading-fleet && cd trading-fleet   # auth: SSH key o PAT (repo privata)
git clone https://github.com/titouannwtt/freqtrade-ultimate.git freqtrade-ultimate
docker build --platform linux/arm64 -t freqtrade-ultimate:latest ./freqtrade-ultimate
```

### 2.3 Dai template ai file reali

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

Riempi i file **reali** (mai i `.example`):

- `.env.shared`: `TZ=UTC` (dati/funding/candele sono UTC), `TELEGRAM__CHAT_ID`, `FIAT_DISPLAY_CURRENCY`.
- `botN/.env`: `TELEGRAM__TOKEN`, `API_SERVER__USERNAME/PASSWORD/WS_TOKEN/JWT_SECRET_KEY`
  **univoci per bot** (`openssl rand -hex 32`, `openssl rand -base64 24`), mai vuoti.
- **Hyperliquid**: `walletAddress` / `privateKey` sono camelCase → vanno nel **`config.json` reale**,
  **non** in `.env` (il loader env lowercase-a le chiavi → 422 al init).
- I reali sono gitignorati: non finiranno nei commit. Tieni **copia offsite criptata** di config + env
  (non essendo nel repo, sono l'unica fonte per ricostruire un bot).

Verifica anti-leak prima di procedere:

```bash
git status --short          # NON deve elencare .env / config.json reali
git check-ignore -v bot1/.env bot1/user_data/config.json   # devono risultare ignorati
```

### 2.4 Permessi, avvio, verify

Applica i permessi (§3), poi:

```bash
docker compose up -d srv-ftcache
docker compose up -d srv-bot1 srv-bot2
docker compose exec -T srv-bot2 python -m freqtrade.ohlcv_cache.healthcheck --socket /ftcache/ftcache.sock
# atteso: ONLINE; clients = numero di bot avviati; positions puts > 0 se un bot è live
```

---

## 3. Permessi & UID

Gli UID **non sono fissi** (Oracle: `opc`=1000 sposta `ubuntu`=1001; `ftuser` varia per immagine).
Rilevali sempre. Docker crea i bind-mount mancanti come **root**: pre-crea le directory con
ownership + ACL, non lasciarle a Docker.

```bash
FTUID=$(docker run --rm --entrypoint id freqtrade-ultimate:latest | grep -oP 'uid=\K\d+')
HOSTUID=$(id -u)

sudo chown -R "$HOSTUID:$HOSTUID" bot*/user_data dev/user_data shared/run shared/local
sudo chmod -R o-rwx bot*/user_data dev/user_data shared/run shared/local
sudo find shared/local -type d -exec chmod 755 {} \;
sudo find shared/local -type f -exec chmod 644 {} \;
sudo find bot*/user_data -name '*.json' -exec chmod 640 {} \;   # config reali: owner RW, group R

for d in bot1/user_data bot2/user_data dev/user_data shared/run shared/local; do
  sudo setfacl -R -m  "u:${FTUID}:rwX"   "$d";  sudo setfacl -R -d -m "u:${FTUID}:rwX"   "$d"
  sudo setfacl -R -m  "u:${HOSTUID}:rwX" "$d";  sudo setfacl -R -d -m "u:${HOSTUID}:rwX" "$d"
done
```

Regole d'oro:

- **Mai** `user:` nel compose (rompe l'accesso ai binari interni dell'immagine).
- **Niente** `chown` sui `.env` (Docker li legge come utente host).
- Log su **stdout** (niente `--logfile`); rotazione già nel compose.
- Dopo nuovi file in `shared/local`: `sudo setfacl -R -m "u:${FTUID}:rwX" shared/local`.
- Non usare `sudo` per l'operatività quotidiana.

---

## 4. Sicurezza

### 4.1 Host (cloud)

- **SSH**: key-only (`PermitRootLogin no`, `PasswordAuthentication no`, `MaxAuthTries 3`,
  `X11Forwarding no`, `AllowUsers ubuntu opc`). Verifica override in `/etc/ssh/sshd_config.d/`.
- **Firewall**: `ufw` solo `22/tcp` + `41641/udp`; fail2ban con jail `sshd` + `recidive`
  (`backend = pyinotify` per leggere `fail2ban.log`).
- `rpcbind` masked; `unattended-upgrades` attivo; orologio sincronizzato
  (`timedatectl status` → "System clock synchronized: yes", altrimenti `systemd-timesyncd`).
- **Oracle Security List**: solo TCP 22 + UDP 41641. **Mai** 80/443/808x pubbliche.
- Systemd `trading-fleet.service` (oneshot, `docker compose up -d`) abilitato al boot.

Verify porte (il controllo più importante):

```bash
sudo ss -tulpen | grep LISTEN
# atteso SOLO: 127.0.0.1:808x (bot), 0.0.0.0:22 / [::]:22 (SSH), UDP 41641 (Tailscale)
```

> Comandi completi di hardening in `hardening.sh` (esegui su VM nuova, poi rilancia la verify).

### 4.2 Tailscale (accesso remoto unico)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up && sudo tailscale set --operator=$USER
tailscale serve --bg --https=443  http://127.0.0.1:8081   # bot1
tailscale serve --bg --https=8443 http://127.0.0.1:8082   # bot2
# botN (N>=2): porta = (N+6)*1000+443
```

Niente Funnel. In ogni config, `CORS_origins` = gli origin `https://<host>.ts.net[:porta]`.

### 4.3 Exchange / wallet

- API key **senza withdraw**, IP whitelist = IP della VM, read+trade only, **una key per bot**, 2FA.
- Hyperliquid: **API agent** (mai la chiave principale); capitale isolato in **vault/subaccount**.
- Credenziali API server univoche per bot; ruota JWT/ws/password periodicamente e dopo ogni esposizione.
- Repo privata; segreti solo locali + backup offsite criptato; mai segreti in chat/log/commit.

---

## 5. Aggiornamenti

```bash
# immagine fork: pull + build, POI recreate (senza build, --force-recreate è inutile)
cd freqtrade-ultimate && git pull --ff-only origin main
docker build --platform linux/arm64 -t freqtrade-ultimate:latest .
cd .. && docker compose up -d --force-recreate srv-bot1 srv-bot2   # adatta ai tuoi bot attivi

# strategie locali: modifica in shared/local → reload/restart del bot che le usa
# config: modifica il file REALE (ignorato) → restart del bot
# template: se cambi struttura, aggiorna anche i .example committati (SENZA segreti)
```

**Su LIVE**: restart in finestra calma. Allo stop cadono gli ordini resting e lo stoploss locale è
giù per ~1 min: valuta `/stopentry` prima e stop manuali sull'exchange se il restart slitta.

---

## 6. Manutenzione

### 6.1 Backup

Il fork può fare backup del DB al graceful shutdown, ma è **locale e solo allo stop**. Serve comunque:
snapshot periodico **a caldo** (senza fermare il live), **rotazione**, **copia offsite**.
Replica per ogni bot live (esempio bot2):

```bash
docker compose exec -T srv-bot2 python -c "import sqlite3;s=sqlite3.connect('file:/freqtrade/user_data/tradesv3.sqlite?mode=ro',uri=True);d=sqlite3.connect('/freqtrade/user_data/tradesv3-backup.sqlite');s.backup(d);d.close();s.close();print('backup ok')"
mv bot2/user_data/tradesv3-backup.sqlite backups/bot2-$(date +%Y%m%d).sqlite
find backups -name 'bot2-*.sqlite' -mtime +7 -delete
```

Cadenza: giornaliero + prima di ogni operazione rischiosa (restart/migrazione/update).

### 6.2 Policy pulizia

| Cosa | Policy |
|---|---|
| DB live | **Mai** purgare / mai `rm`: stato posizioni + track record |
| `backups/` | Ruota (7 giornalieri + 4 settimanali) + offsite |
| `dev/user_data/backtest_results/`, `hyperopt_results/` | Prune periodico (grandi, ignorati) |
| Log | Già rototati (json-file 10m×3): bounded |
| Cache ftcache / klines | Effimera: azzerabile a daemon fermo (si ricostruisce). **Non** è il bundle dati |
| Disco | Monitora `df -h`: disk-full su live = write SQLite falliti |

### 6.3 Troubleshooting

| Sintomo | Causa / fix |
|---|---|
| Due daemon / budget dimezzato | Socket path divergente → allinea `/ftcache/ftcache.sock` ovunque |
| `422 deserialize` su Hyperliquid | camelCase vuoti o messi in `.env` → valori reali in `config.json` |
| ftpairlist "offline" | Idle-shutdown 900s: normale |
| Bot in attesa all'avvio | `depends_on` ftcache non healthy → risolvi il daemon prima |
| Permission denied | ACL/UID: rilancia il blocco §3 |
| FreqUI non apre via Tailscale | `tailscale serve status` + CORS + porta dedicata |
| `.env`/config finiti in git | `git rm --cached <file>` + negazioni gitignore + **ruota i segreti esposti** |
| WAL enorme / disk-full | Checkpoint in finestra calma; libera spazio; backup offsite |

---

## 7. Aggiungere un bot (botN)

1. **Directory + template**:
   ```bash
   mkdir -p botN/user_data
   cp bot1/.env.example botN/.env
   cp bot1/user_data/config.example.json botN/user_data/config.json
   chmod 600 botN/.env
   ```
   Riempi segreti **univoci**; camelCase Hyperliquid nel config reale; adatta exchange/strategia/sizing;
   copia offsite.
2. **Compose**: copia un blocco service → `container_name: botN`, porta `127.0.0.1:808N:8080`, mount:
   `./botN/user_data:/freqtrade/user_data`, `./shared/local:/local:ro`, `./shared/run:/ftcache:rw`,
   `./freqtrade-ultimate/user_data/data:/freqtrade/user_data/data`; `depends_on: srv-ftcache healthy`.
   **Niente** `user:`.
3. **Permessi**: blocco §3 su `botN/user_data`.
4. **Tailscale**: `tailscale serve --bg --https=<((N+6)*1000+443)> http://127.0.0.1:808N`; aggiungi CORS.
5. **Avvio + verify**: `docker compose up -d srv-botN`; log + healthcheck ftcache.
6. **Go-live**: DB **vergine** (mai quello dry), sizing/leva validati, mount `:ro`.
   **Mai** `compose run` sul servizio live.

---

## 8. Committato vs locale

| Committato (repo privata) | Locale / ignorato |
|---|---|
| `docker-compose.yml`, `hardening.sh`, strategie `shared/local/`, template `.example`, tooling | `config.json`, `.env`, `.env.shared`, DB, log, `shared/run/`, `dev/user_data/`, `backups/`, cache |

---

## Risorse

- [Freqtrade](https://www.freqtrade.io/) · [Freqtrade Ultimate](https://github.com/titouannwtt/freqtrade-ultimate)
- [Tailscale](https://tailscale.com/kb/) · [Oracle Cloud Free](https://www.oracle.com/cloud/free/)
