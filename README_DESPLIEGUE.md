# Despliegue y migración de cuentas (seguridad y perfiles)

Este documento no contiene credenciales. Las contraseñas y la clave secreta se ingresan por teclado o por variables
de entorno del servidor, nunca en archivos del repositorio.

## 1. Perfiles

| Perfil | Qué hace | Qué NO hace |
|---|---|---|
| Encargado de Ingresos | Carga archivos, registra ventas/arqueos/depósitos/reintegros, resuelve incidencias operativas, genera capturas | No valida, no visa, no aprueba, no reabre, no configura |
| Jefe de Contabilidad | Valida depósitos/reintegros/documentos/notas de crédito, visa diferencias, aprueba cierres ordinarios, genera archivos finales | No aprueba ni visa lo que él preparó; no opera; no autoriza excepciones; no reabre |
| Gerente de Administración y Finanzas | Todo lo funcional: autoriza excepciones y diferencias sobre el umbral, reabre días (con motivo), configura parámetros y asigna perfiles | Si aprobó o visó algo que ella preparó, debe indicar el motivo (queda como excepción de control) |
| Administrador técnico | Usuarios, grupos e infraestructura en /admin/ | No ve ni opera pantallas financieras; no aprueba nada, aunque sea superusuario |
| Solo Lectura | Consulta días, ventas, arqueos, depósitos, reintegros, estados y reportes; descarga solo archivos ya generados y autorizados | No carga, crea, edita, valida, aprueba, reabre ni genera nada. No descarga archivos originales cargados |
| Sin perfil | Solo la pantalla «Sin perfil asignado» | Nada financiero |

«Solo Visualización» fue retirado: quien lo tenía funciona como Solo Lectura y ya no se puede asignar.

Ser superusuario o staff **no** otorga ningún permiso de la aplicación.

## 2. Variables de entorno de producción

**Por defecto la aplicación arranca en PRODUCCIÓN.** Un equipo de desarrollo se declara con `ARQUEO_ENTORNO=desarrollo` o con un archivo vacío `.entorno_desarrollo` en la carpeta del proyecto (no se versiona; no debe existir en el servidor).

Ver `.env.example`. En producción la aplicación se niega a arrancar sin `DJANGO_SECRET_KEY` y
`DJANGO_ALLOWED_HOSTS`. Activa: `DEBUG=False`, redirección a HTTPS, cookies seguras, HSTS, sesión de 8 horas que expira
al cerrar el navegador.

Verificación previa a poner en marcha (con las variables ya cargadas):

```bash
python manage.py check --deploy
```

## 2b. Bloqueo por intentos fallidos

5 intentos fallidos seguidos bloquean el usuario 15 minutos (se levanta solo; ni la clave correcta entra mientras dure). Aplica también a `/admin/login/`, cuenta usuarios inexistentes igual que reales y deja en la bitácora el bloqueo de cuentas reales (`cuenta_bloqueada_por_intentos`). Para levantarlo antes: `python manage.py recuperar_acceso USUARIO --password-desde-entorno VARIABLE`. No protege contra ataques distribuidos desde muchas cuentas ni sustituye al doble factor.

## 2c. Doble factor, sesiones y contraseñas

- **Doble factor (TOTP)** con Google/Microsoft Authenticator: obligatorio para **Gerente, Jefe de Contabilidad y Administrador técnico** en producción (en desarrollo no se exige; `ARQUEO_DOS_PASOS=1/0` lo fuerza). Al primer ingreso la persona escanea un QR, confirma con un código y recibe **8 códigos de recuperación** de un solo uso (solo se guarda su huella). En cada ingreso nuevo se pide el código; un código no se puede reutilizar y los fallos cuentan para el mismo bloqueo de 5 intentos / 15 minutos.
- **Pérdida del teléfono:** un código de recuperación, o desde la consola del servidor `python manage.py recuperar_acceso USUARIO --reiniciar-dos-pasos` (la persona vuelve a activarlo al ingresar). Queda en la bitácora.
- **Sesión:** en producción vence a los **60 minutos sin actividad** y al cerrar el navegador.
- **Contraseñas:** mínimo **12 caracteres** (además de las reglas estándar de Django: no comunes, no solo números, no parecidas al usuario).
- **Registro de seguridad en archivo:** `logs/seguridad.log` (o `ARQUEO_LOGS`), con rotación de 10 archivos de 10 MB: ingresos, cierres de sesión, fallos y bloqueos.

## 3. Puesta en marcha

1. Respaldar `db.sqlite3` (copia del archivo con la aplicación detenida) y la carpeta de archivos cargados/exportaciones.
2. Actualizar el código y dependencias.
3. `python manage.py migrate` (aplica 0031: grupos de perfiles y sello de la bitácora; 0032: triggers que impiden borrar o editar la bitácora).
4. `python manage.py verificar_bitacora` → debe decir «Bitácora íntegra».
5. HTTPS: terminar TLS en el servidor web o proxy (nginx/IIS/etc.). Si es un proxy, `DJANGO_PROXY_SSL=1`.

## 4. Migración de cuentas SIN interrumpir el trabajo diario

No se borra ningún usuario, dato financiero ni evento. El orden importa:

1. **Informe** (no cambia nada): `python manage.py migrar_perfiles` → muestra, por cuenta, sus permisos efectivos y qué privilegios técnicos tiene hoy.
2. **Crear el Administrador técnico** (sin contraseña todavía):
   `python manage.py crear_admin_tecnico NOMBRE_DE_USUARIO`
3. **Fijar su contraseña** por teclado: `python manage.py changepassword NOMBRE_DE_USUARIO`
4. **Probar** que entra a `/admin/`, ve usuarios y grupos, y que los datos del negocio aparecen de solo consulta.
5. **Probar la recuperación**: `python manage.py recuperar_acceso NOMBRE_DE_USUARIO --reactivar` (y, si hace falta, `--password-desde-entorno VARIABLE`).
6. **Aplicar**: `python manage.py migrar_perfiles --aplicar`. Solo funciona si el paso 2–3 está listo. Retira superusuario/staff a las cuentas que ya tienen un perfil (Gerente, Jefe, etc.). Las cuentas sin perfil quedan sin acceso hasta que la Gerente les asigne uno en Configuración.
7. Cada cambio de grupos o privilegios queda en la bitácora.

Recuperación si se pierde el acceso: `recuperar_acceso` exige acceso a la consola del servidor (no es una pantalla).

## 5. Datos de prueba (solo desarrollo)

`python manage.py reiniciar_datos_de_prueba --confirmo-que-es-un-ambiente-de-pruebas` borra los días cargados y conserva la
configuración. **Se niega a correr en producción.**

## 7. Respaldos

**Hacer un respaldo** (base + archivos cargados + comprobantes + exportaciones, en un solo zip, con verificación):

```bash
python manage.py respaldar --destino D:\Respaldos\Arqueo
```

- Sin `--destino` usa la variable `ARQUEO_RESPALDOS` o, por defecto, la carpeta `respaldos/` del proyecto (no se versiona).
- Al terminar comprueba las huellas de cada archivo, la integridad de la base y la cadena de sellos de la bitácora. Si algo falla, termina con error para que el programador de tareas lo detecte.
- **No incluye secretos** (clave secreta, `.env`, contraseñas). Esos se guardan aparte, en tu gestor de claves.
- Conservación automática: los últimos 30 respaldos + el último de cada uno de los últimos 12 meses. Solo toca archivos `respaldo_AAAAmmdd_HHMMSS.zip` de esa carpeta.
- **Guarda también una copia fuera de este equipo** (otro disco, otra oficina o la nube). Un respaldo en el mismo computador no protege si ese computador falla o se pierde.

**Programarlo cada noche en Windows** (Programador de tareas; ejecutar desde la carpeta del proyecto, ajusta las rutas):

```bash
schtasks /Create /SC DAILY /ST 22:30 /TN "Respaldo Arqueo Diario" /TR "C:\ruta\proyecto\.venv\Scripts\python.exe C:\ruta\proyecto\manage.py respaldar --destino D:\Respaldos\Arqueo"
```

En Linux (cron): `30 22 * * * cd /ruta/proyecto && .venv/bin/python manage.py respaldar --destino /var/respaldos/arqueo`.

**Comprobar un respaldo sin tocar nada:** `python manage.py restaurar RESPALDO.zip`

**Ensayo de restauración** (extrae a una carpeta aparte para revisarla): `python manage.py restaurar RESPALDO.zip --ensayo CARPETA_VACIA`. Se recomienda hacerlo una vez al mes.

**Restaurar de verdad** (con la aplicación DETENIDA): `python manage.py restaurar RESPALDO.zip --reemplazar-esta-instalacion --confirmo`. Lo que había antes no se borra: queda renombrado con el sufijo `.antes_de_restaurar_<fecha>` para poder volver atrás. Después, `python manage.py verificar_bitacora`.

Con PostgreSQL el respaldo de la base es `pg_dump` (el comando `respaldar` es solo para SQLite).

## 5b. Dependencias

`requirements.lock.txt` fija las versiones exactas con las que se probó todo (instalar con `pip install -r requirements.lock.txt`). El 26-09-2026 se auditaron con `pip-audit` (base de vulnerabilidades de PyPI/OSV): **ninguna vulnerabilidad conocida**. Conviene repetirlo cada mes.

## 6. Lo que este trabajo NO resuelve (declarado)

- Doble factor: el secreto TOTP se guarda en la base tal cual (hace falta para calcular el código); por eso los respaldos deben protegerse como la propia base. Un atacante que ya tenga la contraseña de una cuenta que aún NO activó el segundo paso podría activarlo con su propio teléfono: por eso cada cuenta debe activarlo en su primer ingreso, apenas se le entrega la contraseña.
- Protección contra fórmulas maliciosas en archivos Excel cargados y validación profunda de xlsx: no verificada.
- Que el respaldo se ejecute solo cada noche y se copie fuera del equipo: el comando existe y está probado, pero programarlo y elegir el destino externo se hace en el servidor real (ver sección 7).
- La protección de la bitácora por triggers está implementada solo para SQLite; con otro motor hay que replicarla.
- La configuración real de HTTPS del servidor no se puede verificar desde el desarrollo.

## 8. Servidor en opencloud.cl — arqueo.finanzashuentelauquen.cl (paso a paso)

Supone un servidor **Ubuntu 24.04** (Python 3.12; Django 6.1 no funciona con Python 3.10 o anterior) con acceso SSH y
`sudo`. Los archivos de apoyo están en `despliegue/`. Disposición en el servidor:

| Qué | Dónde |
|---|---|
| Código, base `db.sqlite3`, `archivos_cargados/`, `exportaciones/`, `logs/` | `/srv/arqueo/app` (dueño: usuario `arqueo`) |
| Variables secretas | `/etc/arqueo/arqueo.env` (root:arqueo, permisos 640) |
| Respaldos | `/srv/arqueo/respaldos` (+ copia fuera del servidor) |

**Base limpia:** no se sube ninguna base local. En el servidor `migrate` crea una base nueva que ya trae motivos de
resolución, cuentas contables y grupos de perfil. Cajeras, responsables de traslado, parámetros propios y usuarios se
ingresan una vez en Configuración. Ninguna carga de prueba llega a producción.

### 8.1 DNS
`arqueo.finanzashuentelauquen.cl` → registro **A** con la IP pública del servidor. Comprobar desde cualquier equipo:
`nslookup arqueo.finanzashuentelauquen.cl` debe devolver esa IP. Sin esto el certificado HTTPS (8.6) falla.

### 8.2 Paquetes, hora y cortafuegos
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-dev build-essential git nginx certbot python3-certbot-nginx sqlite3
sudo timedatectl set-timezone America/Santiago
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
```
Revisar también en el panel de opencloud.cl que el firewall de la red permita 22, 80 y 443.

### 8.3 Usuario y código
```bash
sudo adduser --system --group --home /srv/arqueo arqueo
sudo mkdir -p /srv/arqueo/respaldos && sudo chown arqueo:arqueo /srv/arqueo/respaldos
sudo -u arqueo git clone https://github.com/karla432/App-Arqueo-de-Caja---Huentelauquen.git /srv/arqueo/app
cd /srv/arqueo/app
sudo -u arqueo python3 -m venv .venv
sudo -u arqueo .venv/bin/pip install -r requirements.lock.txt
```
Si el repositorio es privado, `git clone` pedirá credenciales: usar un *token* de GitHub de solo lectura o una *deploy key*.
**No** crear el archivo `.entorno_desarrollo` en el servidor.

### 8.4 Variables de producción
```bash
sudo mkdir -p /etc/arqueo
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # copiar el resultado como DJANGO_SECRET_KEY
sudo nano /etc/arqueo/arqueo.env
```
Contenido (sin comillas):
```
ARQUEO_ENTORNO=produccion
DJANGO_SECRET_KEY=<la clave generada>
DJANGO_ALLOWED_HOSTS=arqueo.finanzashuentelauquen.cl
DJANGO_CSRF_TRUSTED_ORIGINS=https://arqueo.finanzashuentelauquen.cl
DJANGO_PROXY_SSL=1
ARQUEO_RESPALDOS=/srv/arqueo/respaldos
```
```bash
sudo chown root:arqueo /etc/arqueo/arqueo.env && sudo chmod 640 /etc/arqueo/arqueo.env
sudo chmod +x /srv/arqueo/app/despliegue/manage.sh
```
Guardar la clave también en el gestor de claves: no va en los respaldos.

### 8.5 Base, estáticos y verificación
```bash
sudo /srv/arqueo/app/despliegue/manage.sh migrate
sudo /srv/arqueo/app/despliegue/manage.sh verificar_bitacora      # «Bitácora íntegra»
sudo /srv/arqueo/app/despliegue/manage.sh collectstatic --noinput
sudo /srv/arqueo/app/despliegue/manage.sh check --deploy
```

### 8.6 Servicio, nginx y HTTPS
```bash
sudo cp /srv/arqueo/app/despliegue/arqueo.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now arqueo
sudo systemctl status arqueo                                     # debe decir active (running)

sudo cp /srv/arqueo/app/despliegue/nginx-arqueo.conf /etc/nginx/sites-available/arqueo
sudo ln -s /etc/nginx/sites-available/arqueo /etc/nginx/sites-enabled/arqueo
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d arqueo.finanzashuentelauquen.cl --redirect   # pide un correo; se renueva solo
```
Abrir `https://arqueo.finanzashuentelauquen.cl`: debe mostrar el login con candado.

### 8.7 Primeras cuentas
```bash
sudo /srv/arqueo/app/despliegue/manage.sh crear_admin_tecnico NOMBRE_ADMIN
sudo /srv/arqueo/app/despliegue/manage.sh changepassword NOMBRE_ADMIN   # mínimo 12 caracteres
```
1. Entrar a `/admin/` con esa cuenta y activar el doble factor (QR + guardar los 8 códigos).
2. En `/admin/` crear la cuenta de la Gerente y agregarla al grupo «Gerente de Administración y Finanzas». Al primer
   ingreso activa su doble factor.
3. La Gerente completa Configuración (cajeras, responsables, parámetros) y asigna perfiles al resto del equipo.

### 8.8 Respaldo nocturno
```bash
sudo crontab -u arqueo -e
```
Agregar:
```
30 22 * * * set -a; . /etc/arqueo/arqueo.env; set +a; cd /srv/arqueo/app && .venv/bin/python manage.py respaldar >> /srv/arqueo/respaldos/cron.log 2>&1
```
Y llevar una copia fuera del servidor (ej. `rsync`/`rclone` a otro equipo o nube, o el servicio de snapshots de
opencloud.cl). Probar una vez: `sudo /srv/arqueo/app/despliegue/manage.sh respaldar` y luego `restaurar ... --ensayo`.

### 8.9 Actualizar a una versión nueva
```bash
sudo /srv/arqueo/app/despliegue/manage.sh respaldar
cd /srv/arqueo/app && sudo -u arqueo git pull
sudo -u arqueo .venv/bin/pip install -r requirements.lock.txt
sudo /srv/arqueo/app/despliegue/manage.sh migrate
sudo /srv/arqueo/app/despliegue/manage.sh collectstatic --noinput
sudo systemctl restart arqueo
```

### 8.10 Si algo falla
- Logs de la aplicación: `sudo journalctl -u arqueo -n 100` · nginx: `/var/log/nginx/error.log` · seguridad: `/srv/arqueo/app/logs/seguridad.log`.
- **502 Bad Gateway**: el servicio no está arriba → `systemctl status arqueo`.
- **Redirecciones infinitas**: falta `DJANGO_PROXY_SSL=1` o el `proxy_set_header X-Forwarded-Proto`.
- **400 Bad Request**: el nombre no está en `DJANGO_ALLOWED_HOSTS`.
- **403 CSRF al iniciar sesión**: revisar `DJANGO_CSRF_TRUSTED_ORIGINS` (con `https://`).
- **413 al subir un Excel**: subir `client_max_body_size` en nginx.
- `/admin/` sin estilos: faltó `collectstatic`.
