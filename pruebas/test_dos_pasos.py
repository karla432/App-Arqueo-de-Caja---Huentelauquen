"""Verificación en dos pasos (Karla, 2026-09-26): obligatoria para Gerente, Jefe y Administrador técnico en producción."""
from io import StringIO

import pyotp
import pytest
from django.core.management import call_command
from django.urls import reverse

from nucleo import bloqueo_acceso, dos_pasos
from nucleo.models import CodigoDeRecuperacion, DispositivoTotp, EventoAuditoria
from pruebas.perfiles_de_prueba import (
    GRUPO_ADMIN_TECNICO, GRUPO_ENCARGADO, GRUPO_GERENTE, GRUPO_JEFE, GRUPO_SOLO_LECTURA, crear_usuario,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def obligatorio(settings):
    settings.DOS_PASOS_OBLIGATORIO = True


@pytest.fixture
def cuentas(django_user_model):
    return {
        "gerente": crear_usuario(django_user_model, "g", GRUPO_GERENTE),
        "jefe": crear_usuario(django_user_model, "j", GRUPO_JEFE),
        "tecnico": crear_usuario(django_user_model, "t", GRUPO_ADMIN_TECNICO, is_staff=True),
        "encargada": crear_usuario(django_user_model, "e", GRUPO_ENCARGADO),
        "lectura": crear_usuario(django_user_model, "l", GRUPO_SOLO_LECTURA),
    }


def _entrar(client, usuario):
    client.logout()
    client.force_login(usuario)


def _activar(client, usuario):
    """Recorre la activación completa y devuelve (secreto, códigos de recuperación)."""
    _entrar(client, usuario)
    pagina = client.get(reverse("activar_dos_pasos"))
    secreto = DispositivoTotp.objects.get(usuario=usuario).secreto
    resp = client.post(reverse("activar_dos_pasos"), {"codigo": pyotp.TOTP(secreto).now()})
    assert resp.status_code == 200 and pagina.status_code == 200
    return secreto, list(resp.context["codigos"])


# --------------------------------------------------------------- cuándo se exige

def test_en_desarrollo_no_se_exige(client, cuentas, settings):
    settings.DOS_PASOS_OBLIGATORIO = False
    _entrar(client, cuentas["gerente"])
    assert client.get(reverse("ventas_dashboard")).status_code == 200


def test_se_exige_a_gerente_jefe_y_administrador_tecnico_pero_no_a_los_demas(client, cuentas, obligatorio):
    for clave in ("gerente", "jefe"):
        _entrar(client, cuentas[clave])
        resp = client.get(reverse("ventas_dashboard"))
        assert resp.status_code == 302 and resp["Location"] == reverse("verificacion_dos_pasos"), clave
    _entrar(client, cuentas["tecnico"])
    resp = client.get("/admin/")
    assert resp.status_code == 302 and resp["Location"] == reverse("verificacion_dos_pasos")
    for clave in ("encargada", "lectura"):
        _entrar(client, cuentas[clave])
        assert client.get(reverse("ventas_dashboard")).status_code in (200, 302)
        assert reverse("verificacion_dos_pasos") not in client.get(reverse("ventas_dashboard")).get("Location", "")


def test_sin_verificar_no_se_puede_escribir_nada(client, cuentas, obligatorio):
    _entrar(client, cuentas["gerente"])
    assert client.post(reverse("aprobar_dia", args=["2026-09-02"])).status_code == 403
    assert client.post(reverse("guardar_umbrales"), {"umbral_diferencia_caja": "1"}).status_code == 403


# --------------------------------------------------------------- activar

def test_primer_ingreso_lleva_a_activar_y_muestra_qr_y_clave(client, cuentas, obligatorio):
    _entrar(client, cuentas["gerente"])
    resp = client.get(reverse("verificacion_dos_pasos"))
    assert resp.status_code == 302 and resp["Location"] == reverse("activar_dos_pasos")
    html = client.get(reverse("activar_dos_pasos")).content.decode()
    secreto = DispositivoTotp.objects.get(usuario=cuentas["gerente"]).secreto
    assert "<svg" in html and secreto in html and "Authenticator" in html


def test_activar_con_codigo_malo_falla_y_con_el_bueno_entrega_8_codigos(client, cuentas, obligatorio):
    g = cuentas["gerente"]
    _entrar(client, g)
    client.get(reverse("activar_dos_pasos"))
    malo = client.post(reverse("activar_dos_pasos"), {"codigo": "000000"})
    assert "no coincide" in malo.content.decode() and not dos_pasos.tiene_activo(g)
    secreto, codigos = _activar(client, g)
    assert dos_pasos.tiene_activo(g) and len(codigos) == 8
    assert client.get(reverse("ventas_dashboard")).status_code == 200      # ya pasó el segundo paso
    assert EventoAuditoria.objects.filter(accion="dos_pasos_activado", usuario=g).exists()


def test_los_codigos_de_recuperacion_se_guardan_solo_como_huella(client, cuentas, obligatorio):
    g = cuentas["gerente"]
    _, codigos = _activar(client, g)
    huellas = set(CodigoDeRecuperacion.objects.filter(usuario=g).values_list("huella", flat=True))
    assert len(huellas) == 8 and not any(c in huellas or c.replace("-", "") in huellas for c in codigos)


def test_no_se_puede_activar_de_nuevo_ni_cambiar_el_dispositivo(client, cuentas, obligatorio):
    g = cuentas["gerente"]
    secreto, _ = _activar(client, g)
    with pytest.raises(dos_pasos.DosPasosInvalido):
        dos_pasos.iniciar_activacion(g)
    assert DispositivoTotp.objects.get(usuario=g).secreto == secreto


# --------------------------------------------------------------- verificar en cada ingreso

def test_cada_ingreso_pide_el_codigo(client, cuentas, obligatorio):
    g = cuentas["gerente"]
    secreto, _ = _activar(client, g)
    _entrar(client, g)                                                      # ingreso nuevo
    resp = client.get(reverse("ventas_dashboard"))
    assert resp.status_code == 302 and resp["Location"] == reverse("verificacion_dos_pasos")
    assert client.get(reverse("verificacion_dos_pasos")).status_code == 200
    malo = client.post(reverse("verificacion_dos_pasos"), {"codigo": "123456"})
    assert "no es correcto" in malo.content.decode()
    # un código que ya se usó al activar no sirve dos veces; se espera el siguiente paso de tiempo
    totp = pyotp.TOTP(secreto)
    import time
    paso_siguiente = totp.at(time.time() + 30)
    bueno = client.post(reverse("verificacion_dos_pasos"), {"codigo": paso_siguiente})
    assert bueno.status_code == 302
    assert client.get(reverse("ventas_dashboard")).status_code == 200


def test_un_codigo_no_se_puede_reutilizar(client, cuentas, obligatorio):
    import time
    g = cuentas["gerente"]
    secreto, _ = _activar(client, g)
    codigo = pyotp.TOTP(secreto).at(time.time() + 30)
    _entrar(client, g)
    assert client.post(reverse("verificacion_dos_pasos"), {"codigo": codigo}).status_code == 302
    _entrar(client, g)
    repetido = client.post(reverse("verificacion_dos_pasos"), {"codigo": codigo})
    assert repetido.status_code == 200 and "no es correcto" in repetido.content.decode()


def test_un_codigo_de_recuperacion_sirve_una_sola_vez(client, cuentas, obligatorio):
    g = cuentas["gerente"]
    _, codigos = _activar(client, g)
    _entrar(client, g)
    assert client.post(reverse("verificacion_dos_pasos"), {"codigo": codigos[0]}).status_code == 302
    assert EventoAuditoria.objects.filter(accion="dos_pasos_codigo_de_recuperacion_usado", usuario=g).exists()
    _entrar(client, g)
    de_nuevo = client.post(reverse("verificacion_dos_pasos"), {"codigo": codigos[0]})
    assert de_nuevo.status_code == 200
    assert client.post(reverse("verificacion_dos_pasos"), {"codigo": codigos[1].upper()}).status_code == 302  # otro código sirve


def test_cinco_codigos_malos_bloquean_el_segundo_paso(client, cuentas, obligatorio):
    import time
    g = cuentas["gerente"]
    secreto, _ = _activar(client, g)
    _entrar(client, g)
    for _ in range(bloqueo_acceso.MAX_INTENTOS):
        client.post(reverse("verificacion_dos_pasos"), {"codigo": "111111"})
    valido = pyotp.TOTP(secreto).at(time.time() + 30)
    resp = client.post(reverse("verificacion_dos_pasos"), {"codigo": valido})
    assert resp.status_code == 200 and "Demasiados intentos" in resp.content.decode()
    assert EventoAuditoria.objects.filter(accion="dos_pasos_verificacion_fallida", usuario=g).count() == bloqueo_acceso.MAX_INTENTOS


def test_no_hay_redireccion_abierta_tras_verificar(client, cuentas, obligatorio):
    import time
    g = cuentas["gerente"]
    secreto, _ = _activar(client, g)
    _entrar(client, g)
    sesion = client.session
    sesion["despues_de_verificar"] = "https://sitio-malo.example/robar"
    sesion.save()
    resp = client.post(reverse("verificacion_dos_pasos"), {"codigo": pyotp.TOTP(secreto).at(time.time() + 30)})
    assert resp.status_code == 302 and resp["Location"] == "/"


def test_tras_verificar_vuelve_a_la_pagina_que_pidio(client, cuentas, obligatorio):
    import time
    g = cuentas["gerente"]
    secreto, _ = _activar(client, g)
    _entrar(client, g)
    client.get(reverse("historico"))
    resp = client.post(reverse("verificacion_dos_pasos"), {"codigo": pyotp.TOTP(secreto).at(time.time() + 30)})
    assert resp.status_code == 302 and resp["Location"] == reverse("historico")


def test_el_administrador_tecnico_tambien_verifica_para_entrar_a_admin(client, cuentas, obligatorio):
    import time
    t = cuentas["tecnico"]
    secreto, _ = _activar(client, t)
    assert client.get("/admin/").status_code == 200
    _entrar(client, t)
    assert client.get("/admin/").status_code == 302
    client.post(reverse("verificacion_dos_pasos"), {"codigo": pyotp.TOTP(secreto).at(time.time() + 30)})
    assert client.get("/admin/").status_code == 200


# --------------------------------------------------------------- pérdida del teléfono

def test_reiniciar_desde_consola_quita_el_dispositivo_y_pide_activar_de_nuevo(client, cuentas, obligatorio):
    g = cuentas["gerente"]
    _activar(client, g)
    call_command("recuperar_acceso", g.username, "--reiniciar-dos-pasos", stdout=StringIO())
    assert not DispositivoTotp.objects.filter(usuario=g).exists() and not CodigoDeRecuperacion.objects.filter(usuario=g).exists()
    assert EventoAuditoria.objects.filter(accion="dos_pasos_reiniciado", usuario=g).exists()
    _entrar(client, g)
    resp = client.get(reverse("verificacion_dos_pasos"))
    assert resp["Location"] == reverse("activar_dos_pasos")


def test_la_configuracion_por_defecto_sigue_a_produccion():
    import os
    import subprocess
    import sys
    from pathlib import Path
    raiz = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "ARQUEO_"))}
    env.update({"ARQUEO_ENTORNO": "produccion", "DJANGO_SECRET_KEY": "k" * 60, "DJANGO_ALLOWED_HOSTS": "arqueo.ejemplo.cl"})
    r = subprocess.run(
        [sys.executable, "-c", "import os,django;os.environ['DJANGO_SETTINGS_MODULE']='config.settings';django.setup();"
         "from django.conf import settings as s;print(s.DOS_PASOS_OBLIGATORIO,s.SESSION_COOKIE_AGE,s.SESSION_SAVE_EVERY_REQUEST)"],
        cwd=raiz, env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.stdout.strip() == "True 3600 True", r.stdout + r.stderr


def test_la_contrasena_minima_es_de_12_caracteres(django_user_model):
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError
    with pytest.raises(ValidationError):
        validate_password("corta-1234")           # 10 caracteres
    validate_password("una-clave-larga-y-rara-2026")
