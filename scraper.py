"""Scarica il foglietto della settimana dal sito della parrocchia.

Lo lascia nella cartella che il bot sorveglia; a leggerlo ci pensa lui al giro
dopo del suo timer — o subito, se l'avvio e' partito da
foglietto-bot/avvio-manuale.sh, che incatena i due.

La cartella di destinazione si legge da CARTELLA_FOGLIETTI, la stessa
variabile che usa il bot. Senza, si ripiega su «./foglietto-bot/foglietti»
relativo alla cartella corrente: e' come ha sempre funzionato, ed e' il motivo
per cui foglietto-scraper.service ha «WorkingDirectory=/home/pi».
"""

import os
import subprocess
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

URL = "http://iclesia.com/churches/parrocchia-visitazione-della-beata-vergine-maria"

# Il .env del bot non e' leggibile da qui senza dipendenze in piu': la
# variabile la passa chi ci lancia (il servizio systemd, o avvio-manuale.sh).
FOGLIETTI = Path(os.environ.get("CARTELLA_FOGLIETTI") or "./foglietto-bot/foglietti")

# La pagina scaricata e' un file di lavoro: sta accanto alla destinazione e non
# nella cartella corrente, cosi' non dipende da dove siamo stati lanciati.
PAGINA = FOGLIETTI.parent / "pagina.html"

FOGLIETTI.mkdir(parents=True, exist_ok=True)

# run() aspetta che curl finisca; Popen() no, e si finirebbe per
# leggere il file mentre è ancora vuoto.
subprocess.run(["curl", "-m", "3", "-o", str(PAGINA), URL], check=True)

with PAGINA.open() as fp:
    soup = BeautifulSoup(fp, "html.parser")

links = [link.get("href") for link in soup.find_all("a")]

oggi = date.today()
print(links[13])

# Si scarica accanto alla destinazione e si rinomina a scaricamento finito: il
# rename sullo stesso disco e' atomico, quindi il bot non trova mai un PDF a
# meta'. (Lui aspetta comunque ETA_MINIMA_PDF secondi dall'ultima modifica,
# ma quella e' la cintura: questa e' la bretella.)
destinazione = FOGLIETTI / f"foglietto{oggi}.pdf"
parziale = FOGLIETTI / f".foglietto{oggi}.parziale"
subprocess.run(["curl", "-L", "-o", str(parziale), links[13]], check=True)
os.replace(parziale, destinazione)

print(f"scaricato in {destinazione}")
