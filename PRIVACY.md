# Privatlivspolitik — mail-assistant

## Google OAuth-verifikation

mail-assistant bruger Google OAuth 2.0 til at tilgå Google Tasks på vegne af brugeren.
Applikationen anmoder udelukkende om følgende OAuth-scope:

| Scope | Formål |
|---|---|
| `https://www.googleapis.com/auth/tasks` | Læse og oprette/opdatere opgaver i Google Tasks |

Der anmodes **ikke** om adgang til Gmail, Google Drev, Kalender eller andre Google-tjenester.

---

## Hvilke data tilgås

### Google Tasks
- **Læsning:** Åbne opgaver med titel og forfaldsdato — bruges til at opdage om en opgave allerede er oprettet eller afsluttet.
- **Skrivning:** Oprettelse af nye opgaver (titel, prioritet, forfaldsdato) og markering af opgaver som udført.

### IMAP-mails
- Mailindhold (emne, afsender, brødtekst) hentes fra den konfigurerede IMAP-server og sendes til det valgte LLM for klassificering.
- Ved brug af **Ollama** (lokal model) forlader mailindholdet **aldrig** din server — al behandling sker i din egen infrastruktur.
- Ved brug af **Anthropic** eller **Mistral** API sendes mailindhold til den pågældende udbyders API. Det er brugerens ansvar at sikre, at dette er i overensstemmelse med gældende regler og databehandleraftaler.

---

## Data forlader ikke serveren (Ollama)

Når `LLM_PROVIDER=ollama` er konfigureret:
- Behandles alle mails lokalt på din server
- Ingen maildata sendes til Anthropic, Mistral eller andre tredjeparter
- Google Tasks-API er den eneste udgående forbindelse

---

## Datalagring

| Data | Placering | Indhold |
|---|---|---|
| `data/seen.db` | Lokal SQLite | Message-ID, emne, afsender, behandlingsstatus — **ikke** mailindhold |
| `data/google_token.pickle` | Lokal fil | Google OAuth-token — roteres automatisk |
| `credentials/google_credentials.json` | Lokal fil | Google OAuth client credentials |

Ingen data sendes til applikationsudvikleren. Der er ingen telemetri, ingen analytics og ingen cloud-backup.

---

## Tilbagekaldelse af adgang

Google-adgang kan til enhver tid tilbagekaldes via [Google-kontostyring](https://myaccount.google.com/permissions) under "Tredjepartsapps med kontorettigheder".

Lokal token slettes ved: `rm data/google_token.pickle`

---

## Kontakt

Spørgsmål vedrørende databeskyttelse kan rettes via GitHub Issues i dette repository.
