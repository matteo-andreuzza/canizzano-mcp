#!/bin/sh
# Entrypoint del container: gira come root solo per il tempo di preparare le
# cartelle montate dall'host (stato/, foglietti/), poi lascia il posto
# all'utente non privilegiato prima di eseguire il comando vero (il CMD del
# Dockerfile).
#
# Perche' serve: Docker crea le cartelle dei bind mount mancanti come root
# (succede anche solo lanciando `docker compose up` una prima volta, o con
# `sudo`), e senza questo passaggio il processo applicativo — che non deve
# girare come root — non potrebbe piu' scriverci (PermissionError, in loop
# per via di `restart: always`). Qui si sistema da soli invece di chiedere
# un `chown` manuale sull'host: `docker compose up --build` basta e avanza,
# chiunque lo lanci e qualunque fosse il proprietario prima.
set -eu

UTENTE="${UID_HOST:-1000}"
GRUPPO="${GID_HOST:-1000}"

mkdir -p /app/stato/report /app/stato/analisi /app/stato/richieste /app/foglietti
chown -R "${UTENTE}:${GRUPPO}" /app/stato /app/foglietti

# `chroot --userspec` accetta uid:gid numerici anche senza una voce in
# /etc/passwd (a differenza di `su`), quindi funziona con qualunque
# UID_HOST/GID_HOST, non solo con utenti gia' noti all'immagine. Non serve
# installare gosu: chroot fa parte di coreutils, gia' nell'immagine base.
# `chroot NEWROOT` fa anche un chdir("/") implicito (coreutils), quindi la
# working directory /app va ripristinata dentro alla shell che esegue il
# comando vero, altrimenti «python -m foglietto_bot.servizio» non trova piu'
# il pacchetto.
exec chroot --userspec="${UTENTE}:${GRUPPO}" / sh -c 'cd /app && exec "$@"' sh "$@"
