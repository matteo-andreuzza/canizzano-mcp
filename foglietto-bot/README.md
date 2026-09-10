# foglietto-bot

Redazione automatica del foglietto parrocchiale per **canizzano.it**.

Ogni settimana lo scraper deposita la scansione del foglietto in una cartella;
il bot la legge con Gemini, decide che cosa merita di stare sul sito, crea
eventi e articoli nel CMS via MCP e manda una mail di riepilogo.

```
scraper.py             foglietto-bot (un container)      CMS Django
┌────────────┐  PDF   ┌──────────────────────────┐  MCP  ┌──────────┐
│ sabato 9:00│ ─────▶ │ 1. Gemini legge il PDF   │ ────▶ │ eventi   │
│ da iclesia │ cartel-│ 2. filtra e decide       │ HTTP  │ articoli │
└────────────┘  la    │ 3. scrive nel CMS        │       └──────────┘
                      │ 4. manda il report       │ ──▶ mail HTML
                      └──────────────────────────┘
   scheduler interno     scheduler interno
   (sabato 9:00)         (ogni 30 min)
```

Lo scraper e la scansione girano **nello stesso container**, uno via
`foglietto_bot/servizio.py` (vedi «Come si avvia»): non si parlano fra loro,
si passano la cartella. Lo scraper deposita e basta, il bot trova il PDF al
primo giro utile. Se lo scraper è fermo, il bot non se ne accorge subito ma la
sentinella manda una mail dopo nove giorni.

Il bot **non pubblica il sito** (`./canizzano.sh tutto`, nell'altro
repository, resta un passo manuale in redazione) e **non cancella mai niente**.

## Come si avvia

**Con Docker (il modo consigliato):** un container solo, sempre acceso,
`restart: always`. Dentro c'è uno scheduler che sostituisce i timer di
systemd e un piccolo endpoint HTTP (`/attiva`) che sostituisce il file lasciato
da `avvio-manuale.sh`. Una volta configurato non richiede più aprire un
terminale: parte da solo ogni sabato, e il bottone «Elabora il foglietto»
nella dashboard del sito lo richiama sulla rete Docker condivisa.

```bash
git clone <questo repo> canizzano-mcp && cd canizzano-mcp
cp .env.example .env && nano .env      # chiavi, SMTP, percorsi (vedi sotto)
docker network create canizzano_rete   # una tantum, se non esiste già
docker compose up -d --build
docker compose logs -f bot             # per guardarlo lavorare
```

Il file `compose.yaml` sta nella **radice del repo**, non dentro
`foglietto-bot/`: il contesto di build deve raggiungere sia il pacchetto sia
`scraper.py`, che vive un livello sopra (vedi «Il patto con lo scraper» più
sotto sul perché).

`CARTELLA_LOG_SITO` in `.env` è obbligatoria: è il percorso **assoluto**,
sull'host, della cartella `logs/` del repository del sito (quella con
`attivita.log`). Senza, `docker compose up` si rifiuta di partire con un
errore che lo dice chiaro, invece di montare qualcosa a caso. Vedi «Collegare
i due stack» più sotto per come i due `compose.yaml` si parlano.

Non c'è un comando `verifica` da lanciare a parte: il container lo fa da solo
all'avvio e scrive nei log cosa manca nella configurazione, senza però
spegnersi (con `restart: always` un container che esce subito per un `.env`
incompleto ripartirebbe in loop) — corretto il `.env`, basta
`docker compose restart bot`.

### Collegare i due stack

Il bot raggiunge il CMS come `http://backend:8000/mcp/` (il nome del servizio
Django nel `compose.yaml` del sito) e il sito raggiunge il bot come
`http://bot:8088/attiva` (il nome di questo servizio) — non "localhost": i due
stack Docker sono progetti `compose` separati, e si vedono solo perché
condividono una **rete esterna**:

```bash
docker network create canizzano_rete   # una tantum, prima del primo avvio
```

Lo stesso nome (variabile `NOME_RETE_CONDIVISA`) deve comparire nel `.env` di
**entrambi** i repository. Il `compose.yaml` del sito la referenzia già come
rete esterna sui servizi `backend` ed `esecutore`; questo repo la referenzia
sull'unico servizio che ha, `bot`. Se la rete non esiste ancora, `docker
compose up` si ferma con un errore che lo dice — non con un timeout muto.

### Senza Docker (venv + systemd, se preferisci)

Resta una via percorribile — utile per debug da terminale, o su una macchina
dove non vuoi Docker — ma non è più quella pensata per l'uso quotidiano: il
bottone della dashboard e la ripartenza automatica dopo un riavvio le dà
Docker, non systemd.

```bash
sudo apt install python3-venv
git clone <questo repo> ~/foglietto-bot-src
cp -r ~/foglietto-bot-src/foglietto-bot ~/foglietto-bot
cd ~/foglietto-bot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env      # chiavi, SMTP, cartelle
mkdir -p foglietti stato
./venv/bin/python -m foglietto_bot verifica
```

Lo scraper sta nella radice del repo, non dentro `foglietto-bot/`, ma usa lo
stesso venv. Va lanciato da `~`, perché scrive in `./foglietto-bot/foglietti`:

```bash
cd ~ && ~/foglietto-bot/venv/bin/python ~/foglietto-bot-src/scraper.py
ls -lt ~/foglietto-bot/foglietti | head -3
```

Il PDF appena scaricato deve comparire in cima. Se `ls` non lo mostra, è
finito da un'altra parte: vedi «Quando lo scraper smette di funzionare».

`verifica` non scrive niente da nessuna parte: dice se la configurazione è
completa, se il CMS risponde, quali attività e luoghi vede, e dove finiranno i
file.

#### I timer

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now foglietto-scraper.timer \
                           foglietto-bot.timer \
                           foglietto-bot-sentinella.timer
systemctl list-timers 'foglietto*'
journalctl -u foglietto-scraper -u foglietto-bot -f
```

`foglietto-scraper.service` gira con `WorkingDirectory=/home/pi` (lo scraper
usa un percorso relativo) e ha un `ExecStart=` che punta al repo
(`~/foglietto-bot-src/scraper.py`), non alla copia. Vale la pena controllarlo,
perché uno scraper non trovato non è un errore — il bot tira dritto ed elabora
quello che c'è già.

Le unità danno per scontato l'utente `pi` e la cartella `/home/pi/foglietto-bot`:
se usi altro, correggi `User=`, `WorkingDirectory=`, `ExecStart=` e
`ReadWritePaths=`.

- **`foglietto-bot.timer`** — ogni mezz'ora guarda se in `CARTELLA_FOGLIETTI`
  c'è un PDF mai elaborato. Costa una lettura di cartella: il ciclo è
  settimanale, non serve un watcher sempre acceso.
- **`foglietto-bot-sentinella.timer`** — il lunedì mattina manda una mail se
  da più di nove giorni non arriva un foglietto. Serve ad accorgersi che lo
  scraper si è rotto, invece di scoprirlo a Natale.

Il bottone «Elabora il foglietto» della dashboard **non funziona con questa
via**: chiama l'endpoint HTTP `/attiva` del container Docker sulla rete
condivisa (vedi sotto), e senza container non c'è niente ad ascoltare. Per
forzare un giro da qui si usa `./avvio-manuale.sh` da terminale, a mano.

## Il bottone «Elabora il foglietto» sulla dashboard del sito

Su `/riservata/` (nel repository del sito) c'è un bottone che fa partire un
giro completo **adesso**, senza aspettare lo scheduler: serve quando il
foglietto esce prima del solito, o quando qualcosa non è partito.

```
dashboard del sito ──▶ coda ──▶ esecutore ──POST /attiva──▶ container «bot»
                                (Alpine,       (rete Docker      prova stato/bot.lock
                                 stdlib only)    condivisa)             │
                                                          ┌─────────────┴─────────────┐
                                                     occupato                    libero
                                                          │                          │
                                     registro: «un'altra elaborazione       202, e in un thread:
                                        è già in corso»                  scraper, poi «scansiona»
                                                                                     │
                                                                      logs/attivita.log ◀── ci scrive
                                                                       il bot, coi suoi nomi di processo
```

L'esecutore fa una richiesta HTTP e basta, con un timeout breve: la riga che
scrive subito nel registro (`avvio_manuale_foglietto`) dice «richiesta
consegnata», non «foglietto elaborato». L'esito vero — quanti eventi creati,
cosa in bozza, cosa scartato — arriva dopo, quando il bot ha finito, sotto
`scraper_foglietto` e `ocr_redazione_foglietto`: righe scritte da questo
container via `foglietto_bot/attivita.py`, distinte a colpo d'occhio da
`avvio_manuale_foglietto` che è solo l'atto di aver premuto il bottone.

Il lucchetto è quello di sempre (`stato/bot.lock`, preso da
`pipeline.lucchetto()` dentro a `servizio.py`): l'endpoint lo *guarda* prima
di rispondere, per dare un esito subito in dashboard invece di un `202`
ottimista, ma l'esclusione vera resta lì — se due esecuzioni si accavallano lo
stesso, è il giro arrivato secondo a fermarsi.

Se il bot è spento, o la rete condivisa non esiste ancora, l'esecutore lo
mostra in dashboard come un errore chiaro (connessione rifiutata / rete
irraggiungibile), non come un timeout muto — vedi come lo gestisce
`avvia_foglietto()` nel repository del sito.

### Farsi vedere dalla dashboard

Il CMS tiene un registro in JSON Lines che la dashboard mostra sotto «Ultime
attività». Scrivendoci, quello che fa il bot compare in redazione accanto ai
comandi premuti a mano. Serve una riga nel `.env` (già impostata di default
nel `.env.example` alla radice, per l'uso con Docker):

```
FILE_ATTIVITA=/app/logs-sito/attivita.log
```

Quel percorso è interno al container: punta dove `compose.yaml` monta
`CARTELLA_LOG_SITO` (vedi «Collegare i due stack»). Facoltativa solo nell'uso
senza Docker: senza, il bot lavora lo stesso, solo che da lì non si vede.

## Il patto con lo scraper

Lo scraper deve fare una cosa sola: **mettere il PDF in `CARTELLA_FOGLIETTI`**.

- Il nome del file è libero. L'identità di un foglietto è l'hash del suo
  contenuto, quindi rinominarlo o riscaricarlo non lo fa rielaborare, e due
  foglietti diversi con lo stesso nome restano due foglietti diversi.
- **Scrivi il file altrove e poi spostalo dentro** (`mv` sullo stesso disco è
  atomico). Se invece lo scarichi direttamente lì, il bot potrebbe leggerlo a
  metà: come rete di sicurezza aspetta comunque `ETA_MINIMA_PDF` secondi (60)
  dall'ultima modifica prima di toccarlo.
- I PDF restano nella cartella dopo l'elaborazione: fanno da archivio e non
  vengono riletti.

### Come `scraper.py` prende il file

Scarica la pagina della parrocchia su iclesia.com, raccoglie tutti i link e
prende **`links[13]`**, cioe' il PDF piu' recente per posizione: gli allegati
sono in ordine cronologico e il primo e' l'ultimo pubblicato.

La destinazione si legge da **`CARTELLA_FOGLIETTI`**, la stessa variabile che
usa il bot; senza, si ripiega sul vecchio percorso relativo
`./foglietto-bot/foglietti` — ed e' per quello che
`foglietto-scraper.service` gira con `WorkingDirectory=/home/pi`.

Il file si scarica accanto alla destinazione con un nome provvisorio e si
rinomina a scaricamento finito: il rename sullo stesso disco e' atomico,
quindi il bot non trova mai un PDF a meta'.

**Il limite noto:** l'indice fisso vale finche' il primo allegato della pagina
e' un foglietto. Se la parrocchia pubblica un altro PDF sopra all'ultimo
foglietto — e' gia' successo, in archivio c'e' `Festival_biblico.pdf` — lo
scraper scarica quello senza segnalare niente, e il bot lo elabora come se
fosse il foglietto della settimana. Il rimedio non e' automatico: si guarda il
report che arriva per mail, e se il contenuto non torna si rimette a posto a
mano con `elabora <file giusto> --forza`.

## Comandi

```bash
python -m foglietto_bot verifica                 # controlla tutto, non scrive
python -m foglietto_bot scansiona                # elabora i foglietti nuovi
python -m foglietto_bot scansiona --dry-run      # come sopra ma senza scrivere nel CMS
python -m foglietto_bot elabora foglietti/x.pdf  # un file preciso
python -m foglietto_bot elabora x.pdf --forza    # rifallo anche se già fatto
python -m foglietto_bot stato                    # il registro dei foglietti
python -m foglietto_bot prova-report --solo-file # genera un report finto in stato/report
python -m foglietto_bot prova-report             # e lo spedisce, per provare l'SMTP

./avvio-manuale.sh                               # scraper + scansione, adesso
```

Prove:

```bash
./venv/bin/python -m unittest discover -s tests -t .   # senza rete
./venv/bin/python tests/prova_integrazione.py          # col CMS vero, in sola lettura
```

## Come decide

### Le pagine

Il foglietto è un A4 piegato in due: quattro facciate. **L'ordine nella
scansione non è garantito**, quindi le pagine vengono riconosciute dal
contenuto: copertina (vangelo, mai usata), riflessione/evento (usata solo se è
un evento), intenzioni delle messe (usata solo per l'eventuale avviso nello
spazio sotto la tabella), avvisi parrocchiali (la fonte principale).

Le locandine vengono lette come immagini, e ne escono solo i fatti — cosa,
quando, dove, come si partecipa — senza gli elementi decorativi.

### Il filtro

1. Non ha un'azione o una data (vangelo, citazioni, orari fissi delle messe) →
   scartato.
2. Non riguarda Canizzano e non si tiene qui → scartato. Il giudizio è del
   modello, guidato da esempi: non c'è una lista fissa di parole.
3. Già trovato altrove nello stesso foglietto → una voce sola, arricchita.
4. Materiale lungo o delicato (un tema economico, un appello che tocca
   persone) → **non viene elaborato affatto**, finisce solo nel report sotto
   «Richiede attenzione umana».
5. Superati i controlli: si sceglie l'attività fra quelle che esistono davvero
   nel CMS e si crea l'evento; l'articolo si aggiunge solo se c'è davvero altro
   da dire oltre a data e luogo.
6. In caso di incertezza il contenuto **si crea lo stesso, ma con
   `pubblicato: false`**, e il report dice perché.

Un contenuto resta in bozza quando la confidenza dichiarata dal modello è sotto
`SOGLIA_PUBBLICAZIONE` (0.80) **oppure** quando il bot stesso ha dovuto
rattoppare qualcosa: attività non decisa o inesistente (ripiega su
`ATTIVITA_PREDEFINITA`), data assente (mette un segnaposto).

### La deduplica fra settimane

Un appuntamento importante viene annunciato per più settimane di fila. Prima di
creare un evento il bot cerca nel CMS con `cerca_contenuti`, in una finestra di
`GIORNI_DEDUP` giorni attorno alla data, e confronta i titoli normalizzati:
sopra `SOGLIA_DUPLICATO` (0.82) è lo stesso evento e non lo ricrea. Se il
giorno e l'attività coincidono la soglia si abbassa, perché basta molto meno per
essere sicuri. Il modello riceve anche l'elenco degli eventi già in calendario,
così può segnalarli da solo.

## Configurazione

Tutto sta in `.env`, niente è scritto nel codice. Due file diversi a seconda
di come lo fai girare: **`.env`** alla radice del repo per Docker (`.env.example`
alla radice), **`foglietto-bot/.env`** per l'uso da venv/systemd
(`foglietto-bot/.env.example`) — stesse variabili, stesso significato, cambia
solo dove Docker o systemd vanno a cercarle. Le due variabili che si toccano
più spesso:

| variabile | a cosa serve |
|---|---|
| `EMAIL_DESTINATARIO` | chi riceve il report (ora `matteoandreuzza@outlook.it`) |
| `SOGLIA_PUBBLICAZIONE` | alzala se vuoi che il bot pubblichi meno e lasci più bozze |
| `FILE_ATTIVITA` | il registro del CMS: senza, la dashboard della redazione non vede il bot |

## La chiave MCP del bot

Creane una **dedicata** in admin → Assistente AI (o con `./canizzano.sh mcp
--nuova --nome "foglietto-bot"` nel repository del sito): così puoi revocarla
senza spegnere le chiavi di altri assistenti.

L'unica serratura contro `elimina_contenuto` oggi è **lato client**: il tool è
in una lista di rifiuto nel client MCP del bot (`mcp_client.py`) e alza
un'eccezione se qualcosa prova a chiamarlo. Il CMS non ha (ancora) un
permesso granulare «scrive ma non elimina» — la chiave ha solo `attiva` e
`sola_lettura`, e `sola_lettura` bloccherebbe anche le scritture che al bot
servono. Se un giorno il CMS guadagnerà quel permesso, questa chiave andrà
aggiornata di conseguenza; fino ad allora la protezione reale è questa, non
un'impostazione da spuntare in admin. `verifica` avvisa comunque se la chiave
vede ancora quel tool nello schema, come promemoria che la barriera è nel
codice, non nei permessi.

## Che cosa il bot non fa mai

- Non pubblica il sito. Quello che crea diventa una pagina solo dopo
  `./canizzano.sh tutto`, ed è scritto in fondo a ogni report.
- Non cancella e non depubblica contenuti esistenti.
- Non crea attività, luoghi o edizioni nuove: usa solo quelle che ci sono.
- Non accende `in_evidenza` né `risalto`: decidere che cosa va in home o in
  risalto nella sagra è un giudizio editoriale, e resta a una persona.
- Non elabora due volte lo stesso PDF.

## Se qualcosa va storto

- **Il report non arriva.** Una copia HTML di ogni esecuzione resta in
  `stato/report/`. Con Docker, `docker compose logs -n 100 bot` dice se è
  saltata la posta o la pipeline; senza Docker, `journalctl -u foglietto-bot -n 50`.
- **Un foglietto è andato in errore.** `stato` lo mostra col motivo. Viene
  ritentato al giro dopo fino a `MAX_TENTATIVI` volte, poi lasciato stare;
  `elabora <file> --forza` lo rimette in gioco.
- **Il modello ha sbagliato una scelta.** L'analisi grezza di ogni foglietto è
  in `stato/analisi/<nome>.json`: si vede esattamente che cosa aveva capito.
  Le istruzioni stanno tutte in `foglietto_bot/prompt.py`.
- **Due esecuzioni insieme.** Non può succedere: c'è un lucchetto su
  `stato/bot.lock`, la seconda esce subito senza fare niente.

### Quando lo scraper smette di funzionare

Dipende dalla forma di una pagina che non controlliamo e da come un umano
nomina i file: prima o poi qualcosa cambiera'.

- **`IndexError: list index out of range`** — la pagina ha meno link di prima,
  quindi `links[13]` non esiste. E' cambiato il layout del sito. Si guarda
  com'e' fatta adesso e si corregge l'indice:
  `curl -s <URL> | grep -o 'href="[^"]*"' | head -20`
- **Scarica un PDF che non e' il foglietto.** Il primo allegato in pagina non
  era un foglietto. Si prende il link giusto a mano e si passa al bot con
  `elabora <file> --forza`.
- **`curl: (22)`** — la pagina risponde con un errore HTTP. Il timer riprova
  sabato prossimo.
- **Scarica sempre lo stesso foglietto.** Non e' un errore: la parrocchia non
  ha pubblicato niente di nuovo. Il bot se ne accorge da solo (stesso hash, non
  lo rielabora) e la sentinella manda la mail dopo nove giorni di silenzio.
- **Lo scraper va ma il bot non parte.** Il PDF e' finito nella cartella
  sbagliata: quasi sempre perche' il service girava con la `WorkingDirectory`
  sbagliata. `journalctl -u foglietto-scraper -n 20` mostra dove ha scritto.

Per provarlo senza aspettare sabato: con Docker, premi «Elabora il foglietto»
dalla dashboard del sito (o `curl -X POST -H "Authorization: Bearer $ATTIVA_TOKEN" http://localhost:8088/attiva`
da dentro alla rete condivisa) e guarda `docker compose logs -f bot`; senza
Docker:

```bash
sudo systemctl start foglietto-scraper.service
journalctl -u foglietto-scraper -n 20 --no-pager
```

## Da verificare alla prima esecuzione vera

Due cose non è stato possibile provare senza le chiavi definitive:

1. **La risposta di `crea_evento`/`crea_articolo`.** Non è mai stata chiamata
   sul serio, quindi non si conosce il formato esatto con cui il CMS conferma
   la creazione. Lo slug viene estratto con tre strategie in cascata (riga
   `- **slug**:`, percorso `/eventi/<slug>`, primo codice fra apici inversi); se
   fallissero tutte, l'evento viene creato lo stesso, il report lo segnala come
   dubbio e l'articolo collegato non viene creato. Alla prima esecuzione vera
   vale la pena guardare che lo slug nel report ci sia.
2. **Il modello Gemini.** `GEMINI_MODEL` è impostato a `gemini-3.5-flash` perché
   la lettura di una scansione è un lavoro di visione: se il nome del modello
   nel frattempo è cambiato, si corregge in `.env` senza toccare il codice.
   I `gemini-2.5-*` non sono più raggiungibili dalle chiavi nuove (404), e
   `gemini-3.1-pro-preview` ha quota zero sul piano gratuito: per usarlo
   serve attivare la fatturazione sul progetto Google.
