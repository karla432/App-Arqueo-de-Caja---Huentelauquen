"""Bloque B de seguridad (Karla, 2026-09-26): respaldos verificados, restauración y conservación.
Un respaldo que no se sabe restaurar no es un respaldo: aquí se restaura de verdad, se altera a propósito y se comprueba
que la alteración se detecta."""
import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from nucleo import respaldos
from nucleo.models import EventoAuditoria
from nucleo.respaldos import RespaldoInvalido, aplicar_conservacion, crear_respaldo, verificar_respaldo



def _crear_base_con_bitacora(ruta, eventos=3):
    """Una base SQLite de verdad (archivo) con la tabla de la bitácora, sus triggers y `eventos` eventos bien sellados."""
    con = sqlite3.connect(ruta)
    con.execute("""CREATE TABLE nucleo_eventoauditoria (
        id INTEGER PRIMARY KEY AUTOINCREMENT, dia_id INTEGER, entidad TEXT, entidad_id TEXT, accion TEXT,
        valor_anterior TEXT, valor_posterior TEXT, usuario_id INTEGER, origen TEXT, hora TEXT,
        sello_previo TEXT, sello TEXT)""")
    con.execute("CREATE TRIGGER bitacora_sin_borrado BEFORE DELETE ON nucleo_eventoauditoria BEGIN SELECT RAISE(ABORT,'no'); END")
    con.execute("CREATE TRIGGER bitacora_sin_edicion BEFORE UPDATE ON nucleo_eventoauditoria BEGIN SELECT RAISE(ABORT,'no'); END")
    anterior = ""
    for i in range(eventos):
        e = EventoAuditoria(dia_id=None, entidad="X", entidad_id=str(i), accion="prueba", usuario_id=1, origen="",
                            hora=datetime(2026, 9, 26, 10, i, 0))
        sello = EventoAuditoria.calcular_sello(anterior, e.contenido_para_sello())
        con.execute(
            "INSERT INTO nucleo_eventoauditoria (dia_id,entidad,entidad_id,accion,usuario_id,origen,hora,sello_previo,sello)"
            " VALUES (NULL,?,?,?,1,'',?,?,?)", ("X", str(i), "prueba", e.hora.isoformat(sep=" "), anterior, sello))
        anterior = sello
    con.commit()
    con.close()


@pytest.fixture
def instalacion(tmp_path):
    """Una «instalación» de mentira: base en archivo con 3 eventos sellados y carpetas de datos con archivos."""
    base = tmp_path / "instalacion"
    (base / "archivos_cargados" / "2026-09-02").mkdir(parents=True)
    (base / "archivos_cargados" / "2026-09-02" / "pos_x.xlsx").write_bytes(b"contenido pos")
    (base / "archivos_cargados" / "comprobantes_deposito").mkdir()
    (base / "archivos_cargados" / "comprobantes_deposito" / "c1.pdf").write_bytes(b"%PDF-1.4 comprobante")
    (base / "exportaciones").mkdir()
    (base / "exportaciones" / "captura.xlsx").write_bytes(b"captura")
    # secretos que NO deben viajar en un respaldo
    (base / ".clave_secreta_local").write_text("no-debe-estar-en-el-respaldo", encoding="utf-8")
    (base / ".env").write_text("DJANGO_SECRET_KEY=no-debe-estar", encoding="utf-8")
    _crear_base_con_bitacora(base / "db.sqlite3")
    return base


def _hacer(tmp_path, base):
    return crear_respaldo(tmp_path / "respaldos", ruta_db=base / "db.sqlite3", base_dir=base, ahora=datetime(2026, 9, 26, 22, 0, 0))


def _reescribir(zip_ruta, cambios: dict, nuevo_nombre):
    """Copia el zip aplicando cambios {parte: bytes}; recalcula la huella en el manifiesto si se pide recalcular."""
    with zipfile.ZipFile(zip_ruta) as z:
        partes = {i.filename: z.read(i.filename) for i in z.infolist()}
    partes.update(cambios)
    salida = zip_ruta.with_name(nuevo_nombre)
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        for n, c in partes.items():
            z.writestr(n, c)
    return salida


# ---------------------------------------------------------------------- crear y verificar

def test_el_respaldo_se_crea_con_todo_y_se_verifica(tmp_path, instalacion):
    zip_ruta = _hacer(tmp_path, instalacion)
    assert zip_ruta.name == "respaldo_20260926_220000.zip"
    with zipfile.ZipFile(zip_ruta) as z:
        nombres = set(z.namelist())
    assert {"db.sqlite3", "MANIFIESTO.json", "archivos/archivos_cargados/2026-09-02/pos_x.xlsx",
            "archivos/archivos_cargados/comprobantes_deposito/c1.pdf", "archivos/exportaciones/captura.xlsx"} <= nombres
    resumen = verificar_respaldo(zip_ruta)
    assert resumen["eventos_bitacora"] == 3 and resumen["archivos"] == 4


def test_el_respaldo_no_incluye_secretos(tmp_path, instalacion):
    zip_ruta = _hacer(tmp_path, instalacion)
    with zipfile.ZipFile(zip_ruta) as z:
        todo = b"".join(z.read(n) for n in z.namelist() if n != "db.sqlite3")
        assert not any(".clave_secreta" in n or n.endswith(".env") for n in z.namelist())
    assert b"no-debe-estar" not in todo


def test_el_manifiesto_dice_si_la_bitacora_estaba_integra(tmp_path, instalacion):
    manifiesto = respaldos.leer_manifiesto(_hacer(tmp_path, instalacion))
    assert manifiesto["integridad_base"] == "ok"
    assert manifiesto["bitacora"] == {"eventos_verificados": 3, "problemas": []}


def test_no_se_sobrescribe_un_respaldo_existente(tmp_path, instalacion):
    _hacer(tmp_path, instalacion)
    with pytest.raises(RespaldoInvalido, match="Ya existe"):
        _hacer(tmp_path, instalacion)


def test_un_archivo_alterado_dentro_del_respaldo_se_detecta(tmp_path, instalacion):
    zip_ruta = _hacer(tmp_path, instalacion)
    alterado = _reescribir(zip_ruta, {"archivos/exportaciones/captura.xlsx": b"falsificado"}, "respaldo_20260926_230000.zip")
    with pytest.raises(RespaldoInvalido, match="huella"):
        verificar_respaldo(alterado)


def test_una_bitacora_manipulada_dentro_del_respaldo_se_detecta_aunque_se_rehagan_las_huellas(tmp_path, instalacion):
    """El caso difícil: alguien edita un evento en la base del respaldo Y recalcula el manifiesto. Los sellos encadenados lo delatan."""
    zip_ruta = _hacer(tmp_path, instalacion)
    with zipfile.ZipFile(zip_ruta) as z:
        db_bytes = z.read("db.sqlite3")
        manifiesto = json.loads(z.read("MANIFIESTO.json"))
    tmp_db = tmp_path / "manipulada.sqlite3"
    tmp_db.write_bytes(db_bytes)
    con = sqlite3.connect(tmp_db)
    con.execute("DROP TRIGGER IF EXISTS bitacora_sin_edicion")
    con.execute("UPDATE nucleo_eventoauditoria SET accion='falsificada' WHERE id=(SELECT min(id) FROM nucleo_eventoauditoria)")
    con.commit()
    con.close()
    nuevo = tmp_db.read_bytes()
    manifiesto["archivos"]["db.sqlite3"] = {"sha256": hashlib.sha256(nuevo).hexdigest(), "bytes": len(nuevo)}
    manipulado = _reescribir(zip_ruta, {"db.sqlite3": nuevo, "MANIFIESTO.json": json.dumps(manifiesto).encode()}, "respaldo_20260926_231500.zip")
    with pytest.raises(RespaldoInvalido, match="bitácora"):
        verificar_respaldo(manipulado)


def test_una_base_inexistente_se_informa(tmp_path):
    with pytest.raises(RespaldoInvalido, match="No existe la base"):
        crear_respaldo(tmp_path / "r", ruta_db=tmp_path / "no_esta.sqlite3", base_dir=tmp_path)


def test_un_archivo_que_no_es_un_respaldo_se_rechaza(tmp_path):
    falso = tmp_path / "respaldo_falso.zip"
    falso.write_bytes(b"no soy un zip")
    with pytest.raises(RespaldoInvalido):
        verificar_respaldo(falso)


# ---------------------------------------------------------------------- restaurar

def test_ensayo_de_restauracion_en_una_carpeta_aparte(tmp_path, instalacion):
    zip_ruta = _hacer(tmp_path, instalacion)
    destino = tmp_path / "ensayo"
    respaldos.restaurar_en_carpeta(zip_ruta, destino)
    assert (destino / "db.sqlite3").exists()
    assert (destino / "archivos" / "exportaciones" / "captura.xlsx").read_bytes() == b"captura"
    eventos, problemas = respaldos.verificar_cadena_de_bitacora(destino / "db.sqlite3")
    assert eventos == 3 and problemas == []
    with pytest.raises(RespaldoInvalido, match="no está vacía"):
        respaldos.restaurar_en_carpeta(zip_ruta, destino)


def test_restaurar_reemplazando_deja_copia_de_lo_anterior(tmp_path, instalacion):
    zip_ruta = _hacer(tmp_path, instalacion)
    # «instalación dañada»: base y archivos distintos a los del respaldo
    base_dir = tmp_path / "otra_instalacion"
    (base_dir / "exportaciones").mkdir(parents=True)
    (base_dir / "exportaciones" / "captura.xlsx").write_bytes(b"version-mala")
    ruta_db = base_dir / "db.sqlite3"
    ruta_db.write_bytes(b"base danada")
    resumen = respaldos.restaurar_reemplazando(zip_ruta, base_dir=base_dir, ruta_db=ruta_db, ahora=datetime(2026, 9, 27, 8, 0, 0))
    assert (base_dir / "exportaciones" / "captura.xlsx").read_bytes() == b"captura"
    assert (base_dir / "archivos_cargados" / "comprobantes_deposito" / "c1.pdf").read_bytes() == b"%PDF-1.4 comprobante"
    assert respaldos.verificar_cadena_de_bitacora(ruta_db)[0] == 3
    sufijo = resumen["copia_de_lo_anterior"]
    assert (base_dir / f"db.sqlite3{sufijo}").read_bytes() == b"base danada"      # nada se borró
    assert (base_dir / f"exportaciones{sufijo}" / "captura.xlsx").read_bytes() == b"version-mala"


def test_un_respaldo_con_rutas_peligrosas_no_se_extrae(tmp_path, instalacion):
    zip_ruta = _hacer(tmp_path, instalacion)
    with zipfile.ZipFile(zip_ruta) as z:
        manifiesto = json.loads(z.read("MANIFIESTO.json"))
    malo = b"escape"
    manifiesto["archivos"]["../../fuera.txt"] = {"sha256": hashlib.sha256(malo).hexdigest(), "bytes": len(malo)}
    hostil = _reescribir(zip_ruta, {"../../fuera.txt": malo, "MANIFIESTO.json": json.dumps(manifiesto).encode()}, "respaldo_20260926_234500.zip")
    with pytest.raises(RespaldoInvalido, match="Ruta no permitida"):
        respaldos.restaurar_en_carpeta(hostil, tmp_path / "destino_hostil")
    assert not (tmp_path / "fuera.txt").exists()


# ---------------------------------------------------------------------- conservación

def _falsos(carpeta, fechas):
    carpeta.mkdir(exist_ok=True)
    for f in fechas:
        (carpeta / f"respaldo_{f:%Y%m%d_%H%M%S}.zip").write_bytes(b"x")


def test_conservacion_deja_30_diarios_y_el_ultimo_de_cada_mes(tmp_path):
    carpeta = tmp_path / "r"
    hoy = datetime(2026, 9, 26, 22, 0, 0)
    _falsos(carpeta, [hoy - timedelta(days=i) for i in range(0, 400)])
    (carpeta / "notas.txt").write_text("no es un respaldo")
    (carpeta / "respaldo_manual_importante.zip").write_bytes(b"x")     # nombre no estándar: jamás se toca
    borrados = aplicar_conservacion(carpeta)
    quedan = sorted(p.name for p in carpeta.glob("respaldo_2*.zip"))
    assert len(quedan) >= 30 and len(borrados) > 300
    assert (carpeta / "respaldo_20260926_220000.zip").exists()           # el más nuevo siempre
    assert (carpeta / "notas.txt").exists() and (carpeta / "respaldo_manual_importante.zip").exists()
    meses = {n[9:15] for n in quedan}
    assert {"202608", "202607", "202606", "202510"} <= meses            # un representante por mes hasta 12 meses atrás
    assert "202409" not in meses


# ---------------------------------------------------------------------- comandos

def test_comando_respaldar_y_restaurar_de_punta_a_punta(tmp_path, instalacion, settings):
    settings.BASE_DIR = instalacion
    settings.DATABASES = {"default": {**settings.DATABASES["default"], "NAME": instalacion / "db.sqlite3"}}
    destino = tmp_path / "salida"
    salida = StringIO()
    call_command("respaldar", "--destino", str(destino), stdout=salida)
    assert "Respaldo listo y verificado" in salida.getvalue() and "FUERA de este equipo" in salida.getvalue()
    zip_ruta = next(destino.glob("respaldo_*.zip"))
    salida = StringIO()
    call_command("restaurar", str(zip_ruta), stdout=salida)
    assert "Respaldo válido" in salida.getvalue()
    call_command("restaurar", str(zip_ruta), "--ensayo", str(tmp_path / "ensayo"), stdout=StringIO())
    assert (tmp_path / "ensayo" / "db.sqlite3").exists()


def test_restaurar_reemplazando_exige_confirmacion(tmp_path, instalacion, settings):
    settings.BASE_DIR = instalacion
    settings.DATABASES = {"default": {**settings.DATABASES["default"], "NAME": instalacion / "db.sqlite3"}}
    zip_ruta = _hacer(tmp_path, instalacion)
    with pytest.raises(CommandError, match="confirmo"):
        call_command("restaurar", str(zip_ruta), "--reemplazar-esta-instalacion", stdout=StringIO())


def test_el_destino_por_defecto_no_se_versiona():
    from pathlib import Path
    raiz = Path(__file__).resolve().parent.parent
    texto = (raiz / ".gitignore").read_text(encoding="utf-8")
    assert "/respaldos/" in texto
