"""Respaldos y restauración (Karla, 2026-09-26; Bloque B del plan de seguridad).

Un respaldo es UN archivo `respaldo_AAAAmmdd_HHMMSS.zip` con:
  * `db.sqlite3` — copia consistente de la base hecha con la API de respaldo de SQLite (sirve aunque la app esté en uso);
  * `archivos/...` — los archivos cargados, los comprobantes y las exportaciones;
  * `MANIFIESTO.json` — fecha, huella sha256 de cada archivo, cantidad de eventos de la bitácora y si su cadena
    de sellos estaba íntegra al respaldar.
NO incluye secretos (clave secreta, variables de entorno, `.env`): esos se guardan aparte, en el gestor de claves.

Un respaldo solo sirve si se sabe restaurar: `verificar_respaldo` comprueba las huellas, la integridad de la base y la
cadena de sellos de la bitácora sobre el propio zip, sin tocar nada; `restaurar_en_carpeta` ensaya la restauración en
una carpeta aparte; `restaurar_reemplazando` reemplaza la instalación (dejando copia de lo anterior).
Solo SQLite. Con PostgreSQL el respaldo de la base es `pg_dump` (ver README_DESPLIEGUE.md)."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

CARPETAS_DE_DATOS = ("archivos_cargados", "exportaciones")
PATRON = "respaldo_*.zip"
NOMBRE_BASE = "db.sqlite3"
DIARIOS_A_CONSERVAR = 30
MENSUALES_A_CONSERVAR = 12


class RespaldoInvalido(Exception):
    pass


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


# ------------------------------------------------------------------------------ bitácora sobre un archivo

def verificar_cadena_de_bitacora(ruta_db: Path) -> tuple[int, list[str]]:
    """Recalcula los sellos de la bitácora directamente sobre un archivo de base de datos (sin Django en marcha).
    Devuelve (eventos verificados, problemas)."""
    from nucleo.models import EventoAuditoria
    problemas: list[str] = []
    verificados = 0
    con = sqlite3.connect(f"file:{Path(ruta_db).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        anterior_sello, anterior_id = "", None
        for fila in con.execute("SELECT * FROM nucleo_eventoauditoria ORDER BY id"):
            if anterior_id is not None and fila["id"] != anterior_id + 1:
                problemas.append(f"Faltan eventos entre el id {anterior_id} y el {fila['id']}.")
            anterior_id = fila["id"]
            if not fila["sello"]:
                continue
            e = EventoAuditoria(
                dia_id=fila["dia_id"], entidad=fila["entidad"], entidad_id=fila["entidad_id"], accion=fila["accion"],
                valor_anterior=json.loads(fila["valor_anterior"]) if fila["valor_anterior"] else None,
                valor_posterior=json.loads(fila["valor_posterior"]) if fila["valor_posterior"] else None,
                usuario_id=fila["usuario_id"], origen=fila["origen"], hora=datetime.fromisoformat(fila["hora"]),
            )
            if fila["sello_previo"] != anterior_sello:
                problemas.append(f"Evento {fila['id']}: la cadena no coincide con el evento anterior.")
            if EventoAuditoria.calcular_sello(fila["sello_previo"], e.contenido_para_sello()) != fila["sello"]:
                problemas.append(f"Evento {fila['id']}: su contenido no coincide con su sello.")
            anterior_sello = fila["sello"]
            verificados += 1
    finally:
        con.close()
    return verificados, problemas


def _integridad_sqlite(ruta_db: Path) -> str:
    con = sqlite3.connect(f"file:{Path(ruta_db).as_posix()}?mode=ro", uri=True)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


# ------------------------------------------------------------------------------ crear

MAX_SEGUNDOS_DE_COPIA = 120


def _copiar_base(ruta_db: Path, destino: Path) -> None:
    """Copia consistente con la API de respaldo de SQLite (correcta incluso con la app escribiendo). Si la base
    está bloqueada más de MAX_SEGUNDOS_DE_COPIA, falla con un mensaje en vez de esperar para siempre."""
    import time
    inicio = time.monotonic()

    def _avance(estado, restante, total):
        if time.monotonic() - inicio > MAX_SEGUNDOS_DE_COPIA:
            raise RespaldoInvalido("La base estuvo bloqueada demasiado tiempo para copiarla. Intenta de nuevo.")

    fuente = sqlite3.connect(f"file:{Path(ruta_db).as_posix()}?mode=ro", uri=True, timeout=30)
    copia = sqlite3.connect(str(destino))
    try:
        fuente.backup(copia, progress=_avance)
    except sqlite3.Error as exc:
        raise RespaldoInvalido(f"No se pudo copiar la base: {exc}")
    finally:
        copia.close()
        fuente.close()


def crear_respaldo(destino_carpeta: Path, *, ruta_db: Path, base_dir: Path, ahora: datetime | None = None) -> Path:
    """Crea `respaldo_*.zip` en `destino_carpeta`. Lanza RespaldoInvalido si la copia no queda sana."""
    if not Path(ruta_db).exists():
        raise RespaldoInvalido(f"No existe la base de datos {ruta_db}.")
    ahora = ahora or datetime.now()
    destino_carpeta = Path(destino_carpeta)
    destino_carpeta.mkdir(parents=True, exist_ok=True)
    zip_final = destino_carpeta / f"respaldo_{ahora:%Y%m%d_%H%M%S}.zip"
    if zip_final.exists():
        raise RespaldoInvalido(f"Ya existe {zip_final.name}.")

    with tempfile.TemporaryDirectory(prefix="respaldo_") as tmp:
        tmp = Path(tmp)
        copia_db = tmp / NOMBRE_BASE
        _copiar_base(ruta_db, copia_db)
        estado = _integridad_sqlite(copia_db)
        if estado != "ok":
            raise RespaldoInvalido(f"La copia de la base no pasó la revisión de integridad: {estado}")
        eventos, problemas_bitacora = verificar_cadena_de_bitacora(copia_db)

        partes: dict[str, Path] = {NOMBRE_BASE: copia_db}
        for carpeta in CARPETAS_DE_DATOS:
            origen = Path(base_dir) / carpeta
            if origen.exists():
                for archivo in sorted(p for p in origen.rglob("*") if p.is_file()):
                    partes[f"archivos/{carpeta}/{archivo.relative_to(origen).as_posix()}"] = archivo

        manifiesto = {
            "version": 1, "creado": ahora.isoformat(timespec="seconds"),
            "base": NOMBRE_BASE, "integridad_base": estado,
            "bitacora": {"eventos_verificados": eventos, "problemas": problemas_bitacora},
            "archivos": {nombre: {"sha256": _sha256(ruta), "bytes": ruta.stat().st_size} for nombre, ruta in partes.items()},
        }
        parcial = zip_final.with_suffix(".zip.parcial")
        try:
            with zipfile.ZipFile(parcial, "w", zipfile.ZIP_DEFLATED) as z:
                for nombre, ruta in partes.items():
                    z.write(ruta, nombre)
                z.writestr("MANIFIESTO.json", json.dumps(manifiesto, ensure_ascii=False, indent=2))
            parcial.replace(zip_final)
        finally:
            if parcial.exists():
                parcial.unlink()

    verificar_respaldo(zip_final)  # nunca se da por bueno un respaldo que no se pudo releer
    return zip_final


# ------------------------------------------------------------------------------ verificar

def leer_manifiesto(zip_ruta: Path) -> dict:
    try:
        with zipfile.ZipFile(zip_ruta) as z:
            return json.loads(z.read("MANIFIESTO.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise RespaldoInvalido(f"{Path(zip_ruta).name} no es un respaldo válido ({exc}).")


def verificar_respaldo(zip_ruta: Path) -> dict:
    """Comprueba huellas de cada archivo, integridad de la base y cadena de sellos. No modifica nada."""
    zip_ruta = Path(zip_ruta)
    manifiesto = leer_manifiesto(zip_ruta)
    with zipfile.ZipFile(zip_ruta) as z, tempfile.TemporaryDirectory(prefix="verif_") as tmp:
        if z.testzip() is not None:
            raise RespaldoInvalido("El zip está dañado.")
        for nombre, datos in manifiesto["archivos"].items():
            h = hashlib.sha256()
            with z.open(nombre) as f:
                for bloque in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(bloque)
            if h.hexdigest() != datos["sha256"]:
                raise RespaldoInvalido(f"La huella de «{nombre}» no coincide: el respaldo fue alterado o se dañó.")
        base = Path(tmp) / NOMBRE_BASE
        base.write_bytes(z.read(NOMBRE_BASE))
        if _integridad_sqlite(base) != "ok":
            raise RespaldoInvalido("La base dentro del respaldo no pasa la revisión de integridad.")
        eventos, problemas = verificar_cadena_de_bitacora(base)
        if problemas:
            raise RespaldoInvalido("La bitácora del respaldo no está íntegra: " + "; ".join(problemas[:3]))
    return {"creado": manifiesto["creado"], "archivos": len(manifiesto["archivos"]), "eventos_bitacora": eventos}


# ------------------------------------------------------------------------------ restaurar

def _extraer(zip_ruta: Path, hacia: Path) -> None:
    """Extrae con control de rutas: ninguna parte puede salir de la carpeta destino."""
    hacia = Path(hacia).resolve()
    with zipfile.ZipFile(zip_ruta) as z:
        for info in z.infolist():
            if info.filename == "MANIFIESTO.json":
                continue
            destino = (hacia / info.filename).resolve()
            if hacia != destino and hacia not in destino.parents:
                raise RespaldoInvalido(f"Ruta no permitida dentro del respaldo: {info.filename}")
            if info.is_dir():
                continue
            destino.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as origen, open(destino, "wb") as salida:
                shutil.copyfileobj(origen, salida)


def restaurar_en_carpeta(zip_ruta: Path, hacia: Path) -> dict:
    """Ensayo de restauración: verifica y extrae a una carpeta aparte. No toca la instalación."""
    resumen = verificar_respaldo(zip_ruta)
    hacia = Path(hacia)
    if hacia.exists() and any(hacia.iterdir()):
        raise RespaldoInvalido(f"La carpeta {hacia} no está vacía.")
    hacia.mkdir(parents=True, exist_ok=True)
    _extraer(zip_ruta, hacia)
    return resumen


def restaurar_reemplazando(zip_ruta: Path, *, base_dir: Path, ruta_db: Path, ahora: datetime | None = None) -> dict:
    """Reemplaza la base y las carpetas de datos por las del respaldo. Lo anterior NO se borra: se renombra con
    el sufijo `.antes_de_restaurar_<fecha>` para poder volver atrás. La app debe estar detenida."""
    ahora = ahora or datetime.now()
    sufijo = f".antes_de_restaurar_{ahora:%Y%m%d_%H%M%S}"
    resumen = verificar_respaldo(zip_ruta)
    base_dir, ruta_db = Path(base_dir), Path(ruta_db)
    with tempfile.TemporaryDirectory(prefix="restaurar_") as tmp:
        tmp = Path(tmp)
        _extraer(zip_ruta, tmp)
        try:
            if ruta_db.exists():
                ruta_db.rename(ruta_db.with_name(ruta_db.name + sufijo))
            for auxiliar in (ruta_db.with_name(ruta_db.name + "-wal"), ruta_db.with_name(ruta_db.name + "-shm")):
                if auxiliar.exists():
                    auxiliar.rename(auxiliar.with_name(auxiliar.name + sufijo))
            shutil.copy2(tmp / NOMBRE_BASE, ruta_db)
            for carpeta in CARPETAS_DE_DATOS:
                actual = base_dir / carpeta
                if actual.exists():
                    actual.rename(actual.with_name(actual.name + sufijo))
                origen = tmp / "archivos" / carpeta
                if origen.exists():
                    shutil.copytree(origen, actual)
        except PermissionError as exc:
            raise RespaldoInvalido(
                "No se pudo reemplazar un archivo porque está en uso. Detén la aplicación (servidor web) y vuelve a intentar. "
                f"Detalle: {exc}"
            )
    resumen["copia_de_lo_anterior"] = sufijo
    return resumen


# ------------------------------------------------------------------------------ conservación

def aplicar_conservacion(carpeta: Path, *, diarios: int = DIARIOS_A_CONSERVAR, mensuales: int = MENSUALES_A_CONSERVAR) -> list[Path]:
    """Deja los últimos `diarios` respaldos más el último de cada uno de los últimos `mensuales` meses.
    Solo toca archivos `respaldo_*.zip` de esa carpeta. Devuelve los que borró."""
    def fecha(p: Path) -> datetime | None:
        try:
            return datetime.strptime(p.stem.removeprefix("respaldo_"), "%Y%m%d_%H%M%S")
        except ValueError:
            return None

    archivos = sorted((p for p in Path(carpeta).glob(PATRON) if fecha(p)), key=fecha, reverse=True)
    conservar = set(archivos[:diarios])
    ultimo_de_mes: dict[str, Path] = {}
    for p in archivos:  # de más nuevo a más viejo: el primero de cada mes es el último de ese mes
        ultimo_de_mes.setdefault(fecha(p).strftime("%Y-%m"), p)
    for _, p in sorted(ultimo_de_mes.items(), reverse=True)[:mensuales]:
        conservar.add(p)
    borrados = [p for p in archivos if p not in conservar]
    for p in borrados:
        p.unlink()
    return borrados
