# foglietto-bot

Redazione automatica del foglietto parrocchiale per **canizzano.it**.

Ogni settimana lo scraper deposita la scansione del foglietto in una cartella;
il bot la legge con Gemini, decide che cosa merita di stare sul sito, crea
eventi e articoli nel CMS via MCP e manda una mail di riepilogo.

```
scraper (tuo)          foglietto-bot                    CMS Django
┌────────────┐  PDF   ┌──────────────────────────┐  MCP  ┌──────────┐
│  sabato    │ ─────▶ │ 1. Gemini legge il PDF   │ ────▶ │ eventi   │
│  scarica   │        │ 2. filtra e decide       │ HTTP  │ articoli │
└────────────┘        │ 3. scrive nel CMS        │       └──────────┘
                      │ 4. manda il report       │ ──▶ mail HTML
                      └──────────────────────────┘
```

Il bot **non pubblica il sito** (`./canizzano.sh tutto` resta un passo manuale)
e **non cancella mai niente**.

## Perché non sta dentro Docker

Parla col CMS solo via HTTP su `localhost:8000`, quindi non gli serve stare
nella rete Docker del sito. È un consumatore esterno con un ciclo di vita
suo, e va toccato spesso: un venv gestito da systemd si itera in un secondo,
un container va ricostruito ogni volta.

## Installazione sul Raspberry Pi

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

`verifica` non scrive niente da nessuna parte: dice se la configurazione è
completa, se il CMS risponde, quali attività e luoghi vede, e dove finiranno i
file.

### I timer

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now foglietto-bot.timer foglietto-bot-sentinella.timer
systemctl list-timers 'foglietto-bot*'
journalctl -u foglietto-bot -f
```

Le unità danno per scontato l'utente `pi` e la cartella `/home/pi/foglietto-bot`:
se usi altro, correggi `User=`, `WorkingDirectory=`, `ExecStart=` e
`ReadWritePaths=`.

- **`foglietto-bot.timer`** — ogni mezz'ora guarda se in `CARTELLA_FOGLIETTI`
  c'è un PDF mai elaborato. Costa una lettura di cartella: il ciclo è
  settimanale, non serve un watcher sempre acceso.
- **`foglietto-bot-sentinella.timer`** — il lunedì mattina manda una mail se
  da più di nove giorni non arriva un foglietto. Serve ad accorgersi che lo
  scraper si è rotto, invece di scoprirlo a Natale.

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

Tutto sta in `.env`, niente è scritto nel codice. Vedi `.env.example` per
l'elenco commentato. Le due variabili che si toccano più spesso:

| variabile | a cosa serve |
|---|---|
| `EMAIL_DESTINATARIO` | chi riceve il report (ora `matteoandreuzza@outlook.it`) |
| `SOGLIA_PUBBLICAZIONE` | alzala se vuoi che il bot pubblichi meno e lasci più bozze |

## La chiave MCP del bot

Creane una **dedicata** in admin → Assistente AI, e **togli il permesso di
eliminazione**. Il bot non chiama mai `elimina_contenuto` (il tool è in una
lista di rifiuto nel client e alza un'eccezione se qualcuno prova), ma il
permesso lato server è la serratura che conta: quella nel codice è solo la
seconda. `verifica` avvisa se la chiave che stai usando vede ancora quel tool.

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
  `stato/report/`. `journalctl -u foglietto-bot -n 50` dice se è saltata la
  posta o la pipeline.
- **Un foglietto è andato in errore.** `stato` lo mostra col motivo. Viene
  ritentato al giro dopo fino a `MAX_TENTATIVI` volte, poi lasciato stare;
  `elabora <file> --forza` lo rimette in gioco.
- **Il modello ha sbagliato una scelta.** L'analisi grezza di ogni foglietto è
  in `stato/analisi/<nome>.json`: si vede esattamente che cosa aveva capito.
  Le istruzioni stanno tutte in `foglietto_bot/prompt.py`.
- **Due esecuzioni insieme.** Non può succedere: c'è un lucchetto su
  `stato/bot.lock`, la seconda esce subito senza fare niente.

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
