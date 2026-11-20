```markdown
# 🤖 Trading Fleet — Freqtrade Ultimate multi-bot

Fleet Docker di bot Freqtrade (fork Ultimate) con cache OHLCV condivisa, isolamento per-bot e
accesso remoto solo via Tailscale. **Repo privata**: versiona infra, strategie e template `.example`;
i file reali con segreti (`config.json`, `.env`) sono locali e ignorati da git.

> ⚠️ Software educativo. Il trading di criptovalute può portare alla perdita totale del capitale.

---

## 1. Servizi

| Servizio | Ruolo |
|---|---|
| `srv-ftcache` | Daemon OHLCV condiviso: un solo rate-budget per wallet; i bot sono client puri |
| `srv-ftpairlist` | Cache pairlist tra bot (opzionale). "offline" dopo 15 min idle = normale |
| `srv-botN` | Bot live o dry-run (uno per servizio, DB e config isolati) |
| `srv-dev` | Runner usa-e-getta per download/backtest/replay. Mai live, niente porte, limiti CPU/RAM |

- **ftcache**: socket **unica stringa** `/ftcache/ftcache.sock` in compose + tutti i config; se diverge
  hai due daemon e budget dimezzato. I bot `depends_on: service_healthy`: se il daemon non parte, restano in attesa.
- **dev**: task via `docker compose run --rm srv-dev <cmd>`. **Mai** `compose run` su un servizio live
  (seconda istanza con stesso DB/wallet).
- Mount strategie `:ro` sui live, `:rw` su dev.

---

## 2. Installazione

### Prerequisiti
- **Windows**: WSL2 + Docker Desktop (engine WSL2) + Git. **Linux**: `git docker.io docker-compose-v2 curl acl`.
- **Oracle Always Free**: VM.Standard.A1.Flex Ubuntu 22.04 ARM, 2 OCPU/12 GB, boot 200 GB. Immagini multi-arch ok.
- Chiave SSH Ed25519 con passphrase; backup in password manager + USB criptata.

### Clone + build
```bash
git clone <repo-privata> trading-fleet && cd trading-fleet
git clone https://github.com/titouannwtt/freqtrade-ultimate.git freqtrade-ultimate
docker build --platform linux/arm64 -t freqtrade-ultimate:latest ./freqtrade-ultimate
```

### Dai template ai file reali (poi riempi i segreti)
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
Riempi i `.env`: token Telegram, `API_SERVER__USERNAME/PASSWORD/WS_TOKEN/JWT_SECRET_KEY` univoci per bot
(`openssl rand -hex 32` / `-base64 24`), mai vuoti. **Hyperliquid**: `walletAddress`/`privateKey`
(camelCase) vanno nel `config.json` reale, **non** in `.env` (il loader env lowercase-a → 422).
I file reali sono ignorati: non finiranno nei commit. Tieni copia offsite criptata di config+env.

### Permessi (vedi §3) poi avvio + verify
```bash
docker compose up -d srv-ftcache && docker compose up -d srv-bot1 srv-bot2
docker compose exec -T srv-bot2 python -m freqtrade.ohlcv_cache.healthcheck --socket /ftcache/ftcache.sock
# atteso: ONLINE, clients>=N, positions puts>0
```

---

## 3. Permessi & UID (fondamentale)

UID non fissi (Oracle `opc`/`ubuntu`, `ftuser` variabile): rilevali sempre. Docker crea i bind-mount
mancanti come **root**: pre-crea le dir con ownership+ACL.
```bash
FTUID=$(docker run --rm --entrypoint id freqtrade-ultimate:latest | grep -oP 'uid=\K\d+'); HOSTUID=$(id -u)
sudo chown -R "$HOSTUID:$HOSTUID" bot*/user_data dev/user_data shared/run shared/local
sudo chmod -R o-rwx bot*/user_data dev/user_data shared/run shared/local
sudo find shared/local -type d -exec chmod 755 {} \; ; sudo find shared/local -type f -exec chmod 644 {} \;
for d in bot1/user_data bot2/user_data dev/user_data shared/run shared/local; do
  sudo setfacl -R -m "u:${FTUID}:rwX" "$d"; sudo setfacl -R -d -m "u:${FTUID}:rwX" "$d"
  sudo setfacl -R -m "u:${HOSTUID}:rwX" "$d"; sudo setfacl -R -d -m "u:${HOSTUID}:rwX" "$d"
done
```
Regole: mai `user:` nel compose; niente `chown` sui `.env`; log su stdout; dopo nuovi file in
`shared/local` rilancia `setfacl -R -m u:${FTUID}:rwX shared/local`.

---

## 4. Sicurezza

**Host**: SSH key-only (`PermitRootLogin no`, `PasswordAuthentication no`, `AllowUsers`), ufw solo
22/tcp + 41641/udp, fail2ban (sshd + recidive `backend=pyinotify`), rpcbind masked, unattended-upgrades,
orologio sincronizzato. **Oracle Security List**: solo 22 + 41641, mai 80/443/808x pubbliche.
Systemd `trading-fleet.service` al boot. Verify: `sudo ss -tulpen | grep LISTEN` → solo
`127.0.0.1:808x`, 22, 41641. *(Comandi completi in `hardening.sh`.)*

**Tailscale**: `tailscale up`; `serve --bg --https=443 http://127.0.0.1:8081` (bot1),
`--https=8443 …8082` (bot2), botN = `(N+6)*1000+443`. Niente Funnel. CORS = origin `https://<host>.ts.net[:porta]`.

**Exchange/wallet**: API key senza withdraw + IP whitelist + una per bot + 2FA; Hyperliquid **API agent**
(mai chiave principale) + vault/subaccount per isolare il capitale; credenziali API server univoche,
ruotate periodicamente e dopo esposizioni. Repo privata; segreti locali + backup offsite criptato.

---

## 5. Aggiornamenti

```bash
cd freqtrade-ultimate && git pull --ff-only && docker build --platform linux/arm64 -t freqtrade-ultimate:latest .
cd .. && docker compose up -d --force-recreate srv-bot1 srv-bot2   # senza build, recreate è inutile
# strategie locali: modifica in shared/local → reload/restart del bot
# config: modifica il file REALE (ignorato) → restart. Su LIVE finestra calma: allo stop cadono gli
# ordini resting e lo stoploss locale è giù ~1 min; valuta /stopentry prima, stop manuali se slitta.
# template: se cambi struttura, aggiorna anche i .example committati (senza segreti).
```

---

## 6. Manutenzione

**Backup**: il fork può fare backup DB al graceful shutdown, ma è **locale e solo allo stop**.
Serve comunque snapshot periodico **a caldo** (senza fermare il live) + **rotazione** + **offsite**.
```bash
docker compose exec -T srv-bot2 python -c "import sqlite3;s=sqlite3.connect('file:/freqtrade/user_data/tradesv3.sqlite?mode=ro',uri=True);d=sqlite3.connect('/freqtrade/user_data/tradesv3-backup.sqlite');s.backup(d);d.close();s.close()"
mv bot2/user_data/tradesv3-backup.sqlite backups/bot2-$(date +%Y%m%d).sqlite
find backups -name 'bot2-*.sqlite' -mtime +7 -delete
```
| Cosa | Policy |
|---|---|
| DB live | Mai purgare / mai `rm` (stato posizioni + track record) |
| `backups/` | Ruota 7 giornalieri + 4 settimanali + offsite |
| `dev/user_data/*results/` | Prune periodico (grandi, ignorati) |
| Log | Già rototati (10m×3) |
| Cache ftcache | Effimera, azzerabile a daemon fermo. Non è il bundle dati |
| Disco | Monitora `df -h`: disk-full su live = write SQLite falliti |

**Troubleshooting**

| Sintomo | Fix |
|---|---|
| Due daemon / budget dimezzato | Socket path divergente → allinea `/ftcache/ftcache.sock` |
| `422 deserialize` Hyperliquid | camelCase vuoti o in `.env` → reali in `config.json` |
| pairlist "offline" | Idle 900s: normale |
| Bot in attesa all'avvio | `depends_on` ftcache non healthy → risolvi daemon |
| Permission denied | Rilancia blocco permessi §3 |
| FreqUI non apre | `tailscale serve status` + CORS + porta dedicata |
| `.env`/config finiti in git | `git rm --cached <file>` + negazioni gitignore + ruota i segreti esposti |

---

## 7. Aggiungere un bot (botN)

1. `mkdir -p botN/user_data`; copia i template:
   `cp bot1/.env.example botN/.env`; `cp bot1/user_data/config.example.json botN/user_data/config.json`.
   Riempi segreti univoci; camelCase HL nel config reale; copia offsite.
2. Copia un blocco service nel compose: `container_name botN`, porta `127.0.0.1:808N:8080`, mount
   `botN/user_data` + `shared/local:/local:ro` + `shared/run:/ftcache:rw` + bundle; `depends_on` ftcache. Niente `user:`.
3. Permessi §3 su `botN/user_data`.
4. `tailscale serve --bg --https=<((N+6)*1000+443)> http://127.0.0.1:808N`; CORS.
5. `up -d srv-botN`; verify. **Go-live**: DB vergine, sizing/leva validati, mount `:ro`. Mai `compose run` sul live.

---

## Committato vs locale

| Committato (repo privata) | Locale / ignorato |
|---|---|
| compose, `hardening.sh`, strategie `shared/local`, template `.example`, tooling | `config.json`, `.env`, DB, log, `shared/run`, `dev/user_data`, backups, cache |

## Risorse
[Freqtrade](https://www.freqtrade.io/) · [Ultimate](https://github.com/titouannwtt/freqtrade-ultimate) · [Tailscale](https://tailscale.com/kb/) · [Oracle Free](https://www.oracle.com/cloud/free/)
```
