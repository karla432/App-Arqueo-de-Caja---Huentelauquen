"""Perfiles y permisos de la aplicación (Karla, 2026-09-26; a partir del informe «Revisión de seguridad»).

Principio: el permiso para hacer algo sale del PERFIL FUNCIONAL de la persona (un grupo) y de la operación
concreta — nunca de ser superusuario o staff de Django. Ser Administrador técnico da acceso a la
administración técnica (usuarios y grupos), no a operar ni a aprobar nada financiero.

Perfiles (un grupo cada uno; se excluyen entre sí):

* Encargado de Ingresos ... carga archivos y respaldos, registra ventas/arqueos/depósitos/reintegros y
  resuelve incidencias operativas. No valida ni aprueba su propio trabajo.
* Jefe de Contabilidad ..... revisa y valida, visa diferencias y aprueba cierres ordinarios — siempre que no
  haya preparado la operación que aprueba.
* Gerente de Adm. y Finanzas supervisa todo, autoriza excepciones y diferencias sobre el umbral, reabre días,
  administra parámetros y perfiles. Toda excepción exige motivo y queda registrada.
* Administrador técnico .... mantiene usuarios e infraestructura (/admin/). Sin facultades financieras.
* Solo Lectura (antes también «Solo Visualización», retirado): consulta; nunca escribe.

Una cuenta sin ningún perfil no puede operar ni consultar información financiera.
"""
from __future__ import annotations

from enum import Enum
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


class Perfil(str, Enum):
    ADMIN_TECNICO = "administrador_tecnico"
    GERENTE = "gerente_adm_finanzas"
    JEFE = "jefe_contabilidad"
    ENCARGADO = "encargada_ingresos"
    SOLO_LECTURA = "solo_lectura"
    SIN_PERFIL = "sin_perfil"


GRUPO_GERENTE = "Gerente de Administración y Finanzas"
GRUPO_JEFE = "Jefe de Contabilidad"
GRUPO_ENCARGADO = "Encargada de Ingresos"
GRUPO_ADMIN_TECNICO = "Administrador técnico"
GRUPO_SOLO_VISUALIZACION = "Solo Visualización"  # perfil retirado (2026-09-26): quien lo tenga funciona como Solo Lectura
GRUPO_SOLO_LECTURA = "Solo Lectura"

GRUPO_DE_PERFIL = {
    Perfil.GERENTE: GRUPO_GERENTE,
    Perfil.JEFE: GRUPO_JEFE,
    Perfil.ENCARGADO: GRUPO_ENCARGADO,
    Perfil.ADMIN_TECNICO: GRUPO_ADMIN_TECNICO,
    Perfil.SOLO_LECTURA: GRUPO_SOLO_LECTURA,
}
NOMBRE_DE_PERFIL = {
    Perfil.GERENTE: "Gerente de Administración y Finanzas",
    Perfil.JEFE: "Jefe de Contabilidad",
    Perfil.ENCARGADO: "Encargado de Ingresos",
    Perfil.ADMIN_TECNICO: "Administrador técnico",
    Perfil.SOLO_LECTURA: "Solo Lectura",
    Perfil.SIN_PERFIL: "Sin perfil asignado",
}
# Si por error una cuenta quedara en varios grupos, manda el más restrictivo: primero los de lectura, luego
# el técnico (que no opera), y recién después los funcionales de menor a mayor.
PRECEDENCIA = [
    Perfil.SOLO_LECTURA, Perfil.ADMIN_TECNICO,
    Perfil.ENCARGADO, Perfil.JEFE, Perfil.GERENTE,
]
PERFILES_DE_LECTURA = {Perfil.SOLO_LECTURA}
# Grupos antiguos que se siguen reconociendo (con el perfil al que equivalen), para no dejar sin acceso a nadie.
GRUPOS_HEREDADOS = {GRUPO_SOLO_VISUALIZACION: Perfil.SOLO_LECTURA}


class Permiso(str, Enum):
    CONSULTAR = "consultar"                      # ver días, ventas, arqueos, depósitos, reintegros, estados y reportes
    DESCARGAR_GENERADOS = "descargar_generados"  # bajar archivos que ya existen y están autorizados para consulta
    OPERAR = "operar"                            # cargar, contar, registrar, resolver incidencias operativas
    VALIDAR = "validar"                          # validar depósitos/reintegros, verificar notas, clasificar códigos
    VISAR = "visar"                              # visar diferencias y pérdidas sobre el umbral de visado
    APROBAR_CIERRE = "aprobar_cierre"            # aprobar el cierre ordinario de un día
    AUTORIZAR_EXCEPCION = "autorizar_excepcion"  # diferencias sobre el umbral, segunda firma, autoaprobación con motivo
    REABRIR_DIA = "reabrir_dia"
    REABRIR_EXCEPCION = "reabrir_excepcion"      # volver a abrir una excepción ya resuelta (día no cerrado), con motivo
    GENERAR_EXPORTACION = "generar_exportacion"  # generar a mano los archivos finales de un día
    CONFIGURAR = "configurar"                    # parámetros funcionales
    ASIGNAR_PERFILES = "asignar_perfiles"        # perfiles funcionales de las personas
    ADMIN_TECNICA = "admin_tecnica"              # /admin/: usuarios y grupos


PERMISOS_POR_PERFIL: dict[Perfil, frozenset[Permiso]] = {
    Perfil.ENCARGADO: frozenset({Permiso.CONSULTAR, Permiso.DESCARGAR_GENERADOS, Permiso.OPERAR}),
    Perfil.JEFE: frozenset({
        Permiso.CONSULTAR, Permiso.DESCARGAR_GENERADOS, Permiso.VALIDAR, Permiso.VISAR,
        Permiso.APROBAR_CIERRE, Permiso.GENERAR_EXPORTACION, Permiso.REABRIR_EXCEPCION,
    }),
    Perfil.GERENTE: frozenset({
        Permiso.CONSULTAR, Permiso.DESCARGAR_GENERADOS, Permiso.OPERAR, Permiso.VALIDAR, Permiso.VISAR,
        Permiso.APROBAR_CIERRE, Permiso.AUTORIZAR_EXCEPCION, Permiso.REABRIR_DIA, Permiso.REABRIR_EXCEPCION,
        Permiso.GENERAR_EXPORTACION, Permiso.CONFIGURAR, Permiso.ASIGNAR_PERFILES,
    }),
    Perfil.ADMIN_TECNICO: frozenset({Permiso.ADMIN_TECNICA}),
    Perfil.SOLO_LECTURA: frozenset({Permiso.CONSULTAR, Permiso.DESCARGAR_GENERADOS}),
    Perfil.SIN_PERFIL: frozenset(),
}


class PermisoDenegado(PermissionDenied):
    """403 con un mensaje que se muestra tal cual."""


def perfil_de(usuario) -> Perfil:
    if usuario is None or not getattr(usuario, "is_authenticated", False) or not usuario.is_active:
        return Perfil.SIN_PERFIL
    grupos = set(usuario.groups.values_list("name", flat=True))
    encontrados = {perfil for perfil, nombre in GRUPO_DE_PERFIL.items() if nombre in grupos}
    encontrados |= {perfil for nombre, perfil in GRUPOS_HEREDADOS.items() if nombre in grupos}
    for perfil in PRECEDENCIA:
        if perfil in encontrados:
            return perfil
    return Perfil.SIN_PERFIL


def tiene_permiso(usuario, permiso: Permiso) -> bool:
    """El único lugar donde se decide si alguien puede hacer algo. `is_superuser` NO interviene."""
    return permiso in PERMISOS_POR_PERFIL[perfil_de(usuario)]


def exigir(usuario, permiso: Permiso, mensaje: str | None = None) -> None:
    if not tiene_permiso(usuario, permiso):
        raise PermisoDenegado(mensaje or f"Tu perfil ({NOMBRE_DE_PERFIL[perfil_de(usuario)]}) no permite esta operación.")


def requiere(permiso: Permiso, mensaje: str | None = None):
    """Decorador de vistas: sesión iniciada + permiso. Se aplica en el servidor, no solo en los botones."""
    def decorador(vista):
        @wraps(vista)
        @login_required
        def envoltura(request, *args, **kwargs):
            exigir(request.user, permiso, mensaje)
            return vista(request, *args, **kwargs)
        envoltura.permiso_requerido = permiso  # lo lee la prueba que audita todas las rutas
        return envoltura
    return decorador


def es_de_lectura(usuario) -> bool:
    return perfil_de(usuario) in PERFILES_DE_LECTURA


def nivel_de(usuario) -> int:
    """Nivel de aprobación por perfil: 2 Gerente, 1 Jefe, 0 el resto."""
    perfil = perfil_de(usuario)
    return 2 if perfil == Perfil.GERENTE else 1 if perfil == Perfil.JEFE else 0
