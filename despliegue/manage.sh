#!/bin/sh
# Ejecuta un comando de manage.py en el servidor con las variables de producción cargadas, como el usuario «arqueo».
#   sudo /srv/arqueo/app/despliegue/manage.sh migrate
#   sudo /srv/arqueo/app/despliegue/manage.sh respaldar --destino /srv/arqueo/respaldos
set -e
exec sudo -u arqueo sh -c 'set -a; . /etc/arqueo/arqueo.env; set +a; cd /srv/arqueo/app && exec .venv/bin/python manage.py "$@"' manage.sh "$@"
