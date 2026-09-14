# Immagine del bot del foglietto: un solo container, long-running, che fa da
# scheduler interno e da endpoint /attiva (vedi foglietto_bot/servizio.py).
#
# Il contesto di build e' la radice del repo (non foglietto-bot/) perche' lo
# scraper vive li' — vedi «Perche' scraper.py non sta dentro foglietto-bot/»
# nel README del bot — e il container deve poterlo eseguire con lo stesso
# interprete Python del pacchetto.
FROM python:3.12-slim

# curl: e' quello che usa scraper.py per scaricare la pagina della parrocchia
# (subprocess.run(["curl", ...])) — lasciato com'e', non riscritto per Docker.
# tzdata: senza, gli orari nel registro condiviso con la dashboard del sito
# sarebbero in UTC invece che in Europe/Rome.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Rome \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY foglietto-bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY foglietto-bot/foglietto_bot ./foglietto_bot
COPY scraper.py .

# Il container finisce per girare con l'uid dell'host (vedi entrypoint.sh),
# non root: la build invece e' sempre root, quindi senza questo /app
# resterebbe di sua proprieta' e un uid qualunque non potrebbe scriverci il
# file di lavoro dello scraper (pagina.html, accanto a CARTELLA_FOGLIETTI —
# l'unica cosa che scraper.py scrive fuori dai bind mount). L'uid vero non si
# conosce in fase di build (e' scelto nel .env), quindi si apre a chiunque
# invece di indovinarlo.
RUN chmod 777 /app

# Vedi entrypoint.sh: gira come root solo per sistemare i permessi delle
# cartelle montate (stato/, foglietti/), poi lascia il posto all'utente non
# privilegiato prima del CMD qui sotto.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Percorsi interni fissi (bind mount definiti in compose.yaml): l'utente non
# li tocca, configura solo dove stanno sull'host.
ENV CARTELLA_FOGLIETTI=/app/foglietti \
    CARTELLA_STATO=/app/stato \
    FILE_ATTIVITA=/app/logs-sito/attivita.log

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"ATTIVA_PORTA\",\"8088\")}/salute', timeout=3).status==200 else 1)"

CMD ["python", "-m", "foglietto_bot.servizio"]
