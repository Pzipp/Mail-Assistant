---
layout: default
title: mail-assistant
---

# mail-assistant

IMAP → LLM → Google Tasks pipeline der automatisk klassificerer indkommende mails og opretter opgaver i Google Tasks.

---

## Hvad den gør

- Henter nye mails fra IMAP siden en konfigurerbar startdato
- Sender hver mail til et LLM (Ollama, Anthropic Claude eller Mistral) for klassificering
- Opretter en Google Task med prioritet og forfaldsdato hvis mailen kræver handling
- Flytter mailen til en foreslået IMAP-mappe
- Markerer opgaven som udført automatisk når mailen besvares eller arkiveres
- Sætter IMAP-flag (`\Flagged`) på mails over en konfigurerbar prioritetsgrænse

## Hvad den IKKE gør

- Sletter **aldrig** mails (ingen EXPUNGE/DELETE)
- Markerer **aldrig** mails som læste
- Besvarer eller sender **aldrig** mails
- Sender **aldrig** mailindhold til tredjeparter ved brug af Ollama (al behandling er lokal)

---

## Krav

| Krav | Version |
|---|---|
| Docker + Docker Compose | ≥ 24 |
| Python | 3.12 (via Docker) |
| IMAP-server med SSL | port 993 |
| LLM-provider | Ollama (lokal) **eller** Anthropic API **eller** Mistral API |
| Google Cloud-projekt | med Tasks API aktiveret |

---

## Quick start

### 1. Klon og konfigurér

```bash
git clone https://github.com/pzipp/mail-assistant.git
cd mail-assistant
cp .env.example .env
# Rediger .env med dine IMAP-oplysninger og LLM-valg
```

### 2. Opsæt Google Tasks

1. Gå til [Google Cloud Console](https://console.cloud.google.com)
2. Opret nyt projekt, f.eks. `mail-assistant`
3. Aktivér **Google Tasks API**: APIs & Services → Library → "Google Tasks API" → Enable
4. Opret credentials: APIs & Services → Credentials → Create Credentials → **OAuth client ID** → Desktop app → Download JSON
5. Gem filen som `credentials/google_credentials.json`
6. OAuth consent screen → tilføj din email som testbruger

### 3. Opret datamapper

```bash
mkdir -p credentials data
```

### 4. Byg og kør OAuth-flow (kun én gang)

```bash
docker compose build

docker run --rm -it \
  -v $(pwd)/data:/data \
  -v $(pwd)/credentials:/credentials \
  --env-file .env \
  mail-assistant-mail-assistant \
  python /app/assistant.py --auth
```

Følg linket i terminalen og godkend adgang. Token gemmes i `data/google_token.pickle`.

### 5. Start

```bash
docker compose up -d
docker compose logs -f
```

### 6. Test manuel kørsel

```bash
docker exec mail-assistant python /app/assistant.py
```

---

## Konfiguration

Al konfiguration sker via `.env` (kopiér fra `.env.example`).

Vigtige variable:

| Variabel | Beskrivelse | Standard |
|---|---|---|
| `LLM_PROVIDER` | `ollama` / `anthropic` / `mistral` | `ollama` |
| `IMAP_FOLDER_PREFIX` | Tom (Dovecot) eller `INBOX.` (cPanel) | `` |
| `SCAN_FROM_DATE` | YYYY-MM-DD — lad stå tom for i dag | _(auto)_ |
| `MAX_MAILS` | Maks mails per kørsel | `30` |
| `DEADLINE_FALLBACK_DAYS` | Dage fra modtagelse som fallback-frist | `0` |
| `COMPLETE_ON_ANSWERED` | Marker task udført når mail besvares | `true` |
| `COMPLETE_ON_ARCHIVED` | Marker task udført når mail arkiveres | `true` |
| `FLAG_ON_PRIORITY` | IMAP-flag ved prioritet (f.eks. `high`) | _(aldrig)_ |
| `SCHEDULE_TIMES` | Kommaseparerede kørsels-tider (HH:MM) | _(14 tider)_ |

Se `.env.example` for fuld dokumentation af alle variable.

---

## Web API

En FastAPI REST-service kører sideløbende på port `8080` med endpoints til:

- Filterregler (CRUD + rækkefølge)
- IMAP-mapper (sync fra server + redigering)
- Seneste behandlede mails
- Manuel kørsel af assistant

---

## Vilkår for brug

Dette er open source software udgivet under [MIT-licensen](LICENSE).

Softwaren leveres som den er, uden garanti af nogen art. Brugeren er selv ansvarlig for opsætning, drift og eventuelle konsekvenser af brugen. Udvikleren påtager sig intet ansvar for tab af data, utilsigtet adgang til mailkonti eller andre skader der måtte opstå ved brug af softwaren.

Ved at bruge softwaren accepterer du MIT-licensens betingelser.

---

## Licens

MIT — se [LICENSE](LICENSE)
