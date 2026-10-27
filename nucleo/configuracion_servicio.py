"""Mantenedores de Configuración (Karla, 2026-09-21): cuentas contables,
cajeras, umbrales y aprobadores, editables sin tocar el código.

Solo la Gerente de Administración y Finanzas puede cambiarlos, y cada cambio
deja un evento en la bitácora con el valor anterior y el posterior.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import Group
from django.db import transaction

from nucleo.documentos_servicio import GRUPO_GERENTE_ADM_FINANZAS, GRUPO_JEFE_CONTABILIDAD, nivel_usuario  # noqa: F401
from nucleo.permisos import (
    GRUPO_DE_PERFIL, GRUPOS_HEREDADOS, NOMBRE_DE_PERFIL, Perfil, Permiso, exigir, perfil_de, tiene_permiso,
)
from nucleo.models import (
    Cajera, ConfiguracionArqueoCaja, CUENTA_GANANCIA_CODIGO, CUENTA_PERDIDA_CODIGO, CuentaContable,
    EventoAuditoria, Parametro, ResponsableTraslado,
)
from nucleo.umbrales import DEFINICIONES, umbral, umbrales_vigentes  # noqa: F401

# Karla, 2026-09-21: "solo ocuparemos dos cuentas contables, una de pérdida
# y otra de ganancia" — siempre estas dos, nada adicional. Ninguna se puede
# desactivar ni cambiar de tipo o nombre: todo el cálculo de diferencias
# (de caja o de tarjeta) las da por hecho.
CUENTAS_DEL_SISTEMA = {CUENTA_GANANCIA_CODIGO, CUENTA_PERDIDA_CODIGO}

# Perfiles (Karla, 2026-09-26; ver nucleo.permisos). Cinco grupos que se excluyen entre sí. El Administrador técnico
# NO se asigna desde esta pantalla: lo maneja la administración técnica, no la Gerente.
ROLES = {perfil.value: NOMBRE_DE_PERFIL[perfil] for perfil in (
    Perfil.ENCARGADO, Perfil.JEFE, Perfil.GERENTE, Perfil.SOLO_LECTURA, Perfil.ADMIN_TECNICO,
)} | {Perfil.SIN_PERFIL.value: NOMBRE_DE_PERFIL[Perfil.SIN_PERFIL]}
ROLES_ASIGNABLES = {clave: texto for clave, texto in ROLES.items() if clave != Perfil.ADMIN_TECNICO.value}


class ConfiguracionInvalida(Exception):
    pass


def puede_configurar(usuario) -> bool:
    """Solo la Gerente: ser superusuario o administrador técnico no alcanza."""
    return usuario.is_authenticated and tiene_permiso(usuario, Permiso.CONFIGURAR)


def _bitacora(usuario, entidad, entidad_id, accion, anterior, posterior):
    # Todo cambio de configuración pasa por acá dentro de su transacción: si la persona no tiene el permiso, el
    # cambio se deshace. (Cambiar perfiles exige además ASIGNAR_PERFILES, ver `asignar_rol`.)
    exigir(usuario, Permiso.CONFIGURAR, "Solo la Gerente de Administración y Finanzas puede cambiar la configuración.")
    EventoAuditoria.objects.create(
        dia=None, entidad=entidad, entidad_id=str(entidad_id), accion=accion,
        valor_anterior=anterior, valor_posterior=posterior, usuario=usuario,
    )


# ---------------------------------------------------------------- umbrales

@transaction.atomic
def actualizar_umbral(clave: str, valor, usuario) -> Parametro:
    if clave not in DEFINICIONES:
        raise ConfiguracionInvalida("Ese parámetro no existe.")
    d = DEFINICIONES[clave]
    try:
        nuevo = Decimal(str(valor).replace("$", "").replace(".", "").replace(" ", "").strip())
    except InvalidOperation:
        raise ConfiguracionInvalida(f"«{d['nombre']}»: escribe un número entero, sin decimales.")
    if not (d["minimo"] <= nuevo <= d["maximo"]):
        raise ConfiguracionInvalida(
            f"«{d['nombre']}»: el valor debe estar entre {d['minimo']:,} y {d['maximo']:,}.".replace(",", ".")
        )
    anterior = umbral(clave)
    fila, _ = Parametro.objects.update_or_create(clave=clave, defaults={"valor": nuevo})
    if anterior != nuevo:
        _bitacora(usuario, "Parametro", clave, "cambiar_umbral", {"valor": str(anterior)}, {"valor": str(nuevo)})
    return fila


# ---------------------------------------------------------- cuentas contables

@transaction.atomic
def crear_cuenta(codigo: str, nombre: str, tipo: str, usuario, *, diario_odoo: str = "", metodo_pago_odoo: str = "") -> CuentaContable:
    """Karla, 2026-09-21: solo existen las dos cuentas fijas de pérdida y
    ganancia — no se agrega una tercera de esas. Si el nombre real en Odoo
    cambia, se edita una de las dos existentes.

    Las cuentas de Banco son la excepción (Karla, 2026-09-22): puede haber
    varias, una por cada banco real con el que trabaja la empresa —
    alimentan los seleccionables de "Banco" en Control de Depósitos,
    Reintegros de Caja y el asiento de Reintegro."""
    if tipo != CuentaContable.Tipo.BANCO:
        raise ConfiguracionInvalida(
            "Solo existen dos cuentas para pérdida y ganancia — no se agregan más de esas. "
            "Si el nombre cambió en Odoo, edita la que corresponda en vez de crear una nueva. "
            "Para un banco nuevo, usa el formulario de cuentas de Banco."
        )
    codigo = codigo.strip()
    nombre = nombre.strip()
    if not codigo or not nombre:
        raise ConfiguracionInvalida("Escribe el código y el nombre de la cuenta de banco.")
    if CuentaContable.objects.filter(codigo=codigo).exists():
        raise ConfiguracionInvalida(f"Ya existe una cuenta con el código {codigo}.")
    cuenta = CuentaContable.objects.create(
        codigo=codigo, nombre=nombre, tipo=tipo, activo=True,
        diario_odoo=diario_odoo.strip(), metodo_pago_odoo=metodo_pago_odoo.strip(),
    )
    _bitacora(usuario, "CuentaContable", cuenta.pk, "crear_cuenta_banco", None, {
        "codigo": codigo, "nombre": nombre, "diario_odoo": cuenta.diario_odoo, "metodo_pago_odoo": cuenta.metodo_pago_odoo,
    })
    return cuenta


def cuenta_banco_en_uso(cuenta: CuentaContable) -> bool:
    """¿Algún depósito o reintegro quedó registrado con el nombre de este banco?"""
    from nucleo.models import DepositoBancario, ReintegroCaja
    return (
        DepositoBancario.objects.filter(banco=cuenta.nombre).exists()
        or ReintegroCaja.objects.filter(banco=cuenta.nombre).exists()
    )


@transaction.atomic
def editar_cuenta(
    cuenta: CuentaContable, *, nombre: str, tipo: str, activo: bool, usuario,
    diario_odoo: str | None = None, metodo_pago_odoo: str | None = None,
) -> CuentaContable:
    nombre = nombre.strip()
    if not nombre:
        raise ConfiguracionInvalida("Escribe el nombre de la cuenta.")
    if tipo not in CuentaContable.Tipo.values:
        raise ConfiguracionInvalida("Elige un tipo de cuenta válido.")
    # El nombre sí se puede ajustar (ej. si en Odoo quedó redactado distinto)
    # — lo que no puede cambiar es su rol: siempre debe existir exactamente
    # una cuenta de pérdida y una de ganancia (Karla, 2026-09-21).
    if cuenta.codigo in CUENTAS_DEL_SISTEMA and (not activo or tipo != cuenta.tipo):
        raise ConfiguracionInvalida(
            f"La cuenta {cuenta.codigo} la usa el programa en cada diferencia de caja o de tarjeta: "
            "no se puede desactivar ni cambiar de tipo."
        )
    if tipo in (CuentaContable.Tipo.PERDIDA, CuentaContable.Tipo.GANANCIA) and cuenta.codigo not in CUENTAS_DEL_SISTEMA:
        raise ConfiguracionInvalida("Esa cuenta no puede pasar a ser de pérdida o de ganancia — esos dos tipos son fijos.")
    if cuenta.tipo == CuentaContable.Tipo.BANCO and nombre != cuenta.nombre and cuenta_banco_en_uso(cuenta):
        raise ConfiguracionInvalida(
            f"El banco «{cuenta.nombre}» ya tiene depósitos o reintegros registrados con ese nombre — "
            "no se puede renombrar sin desordenar el historial. Crea una cuenta nueva y desactiva esta."
        )
    diario = cuenta.diario_odoo if diario_odoo is None else diario_odoo.strip()
    metodo = cuenta.metodo_pago_odoo if metodo_pago_odoo is None else metodo_pago_odoo.strip()
    anterior = {"nombre": cuenta.nombre, "tipo": cuenta.tipo, "activo": cuenta.activo,
                "diario_odoo": cuenta.diario_odoo, "metodo_pago_odoo": cuenta.metodo_pago_odoo}
    posterior = {"nombre": nombre, "tipo": tipo, "activo": activo, "diario_odoo": diario, "metodo_pago_odoo": metodo}
    if anterior == posterior:
        return cuenta
    cuenta.nombre, cuenta.tipo, cuenta.activo = nombre, tipo, activo
    cuenta.diario_odoo, cuenta.metodo_pago_odoo = diario, metodo
    cuenta.save(update_fields=["nombre", "tipo", "activo", "diario_odoo", "metodo_pago_odoo"])
    _bitacora(usuario, "CuentaContable", cuenta.pk, "editar_cuenta", anterior, posterior)
    return cuenta


# ------------------------------------------------------------------ cajeras

@transaction.atomic
def crear_cajera(nombre: str, usuario) -> Cajera:
    nombre = " ".join(nombre.split())
    if not nombre:
        raise ConfiguracionInvalida("Escribe el nombre de la cajera.")
    if Cajera.objects.filter(nombre__iexact=nombre).exists():
        raise ConfiguracionInvalida(f"Ya existe una cajera llamada {nombre}.")
    cajera = Cajera.objects.create(nombre=nombre)
    _bitacora(usuario, "Cajera", cajera.pk, "crear_cajera", None, {"nombre": nombre})
    return cajera


@transaction.atomic
def editar_cajera(cajera: Cajera, *, nombre: str, activo: bool, usuario) -> Cajera:
    nombre = " ".join(nombre.split())
    if not nombre:
        raise ConfiguracionInvalida("Escribe el nombre de la cajera.")
    if Cajera.objects.filter(nombre__iexact=nombre).exclude(pk=cajera.pk).exists():
        raise ConfiguracionInvalida(f"Ya existe otra cajera llamada {nombre}.")
    anterior = {"nombre": cajera.nombre, "activo": cajera.activo}
    posterior = {"nombre": nombre, "activo": activo}
    if anterior == posterior:
        return cajera
    cajera.nombre, cajera.activo = nombre, activo
    cajera.save(update_fields=["nombre", "activo"])
    _bitacora(usuario, "Cajera", cajera.pk, "editar_cajera", anterior, posterior)
    return cajera


# ---------------------------------------------------- responsables de traslado

def responsables_traslado_activos():
    return ResponsableTraslado.objects.filter(activo=True)


@transaction.atomic
def crear_responsable_traslado(nombre: str, usuario) -> ResponsableTraslado:
    nombre = " ".join(nombre.split())
    if not nombre:
        raise ConfiguracionInvalida("Escribe el nombre de la persona.")
    if ResponsableTraslado.objects.filter(nombre__iexact=nombre).exists():
        raise ConfiguracionInvalida(f"Ya existe {nombre} en la lista.")
    responsable = ResponsableTraslado.objects.create(nombre=nombre)
    _bitacora(usuario, "ResponsableTraslado", responsable.pk, "crear_responsable_traslado", None, {"nombre": nombre})
    return responsable


@transaction.atomic
def editar_responsable_traslado(responsable: ResponsableTraslado, *, nombre: str, activo: bool, usuario) -> ResponsableTraslado:
    nombre = " ".join(nombre.split())
    if not nombre:
        raise ConfiguracionInvalida("Escribe el nombre de la persona.")
    if ResponsableTraslado.objects.filter(nombre__iexact=nombre).exclude(pk=responsable.pk).exists():
        raise ConfiguracionInvalida(f"Ya existe otra persona llamada {nombre}.")
    anterior = {"nombre": responsable.nombre, "activo": responsable.activo}
    posterior = {"nombre": nombre, "activo": activo}
    if anterior == posterior:
        return responsable
    responsable.nombre, responsable.activo = nombre, activo
    responsable.save(update_fields=["nombre", "activo"])
    _bitacora(usuario, "ResponsableTraslado", responsable.pk, "editar_responsable_traslado", anterior, posterior)
    return responsable


# --------------------------------------------------------------- aprobadores

def rol_usuario(usuario) -> str:
    return perfil_de(usuario).value


def usuarios_con_rol(modelo_usuario) -> list[dict]:
    return [
        {"usuario": u, "rol": rol_usuario(u), "rol_texto": ROLES[rol_usuario(u)], "es_admin_tecnico": perfil_de(u) == Perfil.ADMIN_TECNICO}
        for u in modelo_usuario.objects.filter(is_active=True).order_by("username")
    ]


@transaction.atomic
def asignar_rol(objetivo, rol: str, por) -> None:
    """Asigna UN perfil funcional (los grupos se excluyen entre sí). Solo quien tiene ASIGNAR_PERFILES (la Gerente).
    No se puede cambiar el propio perfil, ni dejar al sistema sin Gerente, ni asignar o quitar el perfil de
    Administrador técnico desde acá."""
    exigir(por, Permiso.ASIGNAR_PERFILES, "Solo la Gerente de Administración y Finanzas puede asignar perfiles.")
    if rol not in ROLES_ASIGNABLES:
        raise ConfiguracionInvalida("Perfil desconocido o que no se asigna desde esta pantalla.")
    if objetivo.pk == por.pk:
        raise ConfiguracionInvalida("No puedes cambiar tu propio perfil: pídeselo a otra persona con perfil de Gerente.")
    anterior_perfil = perfil_de(objetivo)
    if anterior_perfil == Perfil.ADMIN_TECNICO:
        raise ConfiguracionInvalida("El perfil de Administrador técnico lo maneja la administración técnica, no esta pantalla.")
    anterior = anterior_perfil.value
    if anterior == rol:
        return
    if anterior == Perfil.GERENTE.value and rol != Perfil.GERENTE.value:
        gerentes = Group.objects.get(name=GRUPO_GERENTE_ADM_FINANZAS).user_set.filter(is_active=True).count()
        if gerentes <= 1:
            raise ConfiguracionInvalida("Debe quedar al menos una persona con el perfil Gerente.")

    grupos_funcionales = {p: Group.objects.get_or_create(name=nombre)[0] for p, nombre in GRUPO_DE_PERFIL.items()}
    objetivo.groups.remove(*grupos_funcionales.values())
    objetivo.groups.remove(*Group.objects.filter(name__in=GRUPOS_HEREDADOS))
    if rol != Perfil.SIN_PERFIL.value:
        objetivo.groups.add(grupos_funcionales[Perfil(rol)])
    _bitacora(por, "Usuario", objetivo.pk, "cambiar_rol_usuario",
              {"usuario": objetivo.username, "rol": ROLES[anterior]},
              {"usuario": objetivo.username, "rol": ROLES[rol]})


# --------------------------------------------------------- asiento de arqueo

def config_arqueo_caja() -> ConfiguracionArqueoCaja:
    return ConfiguracionArqueoCaja.vigente()


def cuentas_banco():
    """Karla, 2026-09-22: las cuentas de Banco activas — alimentan el
    seleccionable de la Cuenta Banco del asiento de Reintegro, además de
    los de "Banco" en Control de Depósitos y Reintegros de Caja."""
    return CuentaContable.objects.filter(tipo=CuentaContable.Tipo.BANCO, activo=True).order_by("nombre")


@transaction.atomic
def actualizar_config_arqueo_caja(*, diario: str, cuenta_efectivo_codigo: str, cuenta_efectivo_nombre: str,
                                   cuenta_transbank_codigo: str, cuenta_transbank_nombre: str,
                                   cuenta_mercado_pago_codigo: str, cuenta_mercado_pago_nombre: str,
                                   codigo_analitico: str, usuario, cuenta_banco_id=None,
                                   razon_social: str | None = None) -> ConfiguracionArqueoCaja:
    # Karla, 2026-09-24: la cuenta del asiento de reintegro ya NO viene de acá
    # sino del banco que elige el usuario en cada reintegro; `cuenta_banco_id`
    # se acepta solo por compatibilidad y es opcional.
    config = config_arqueo_caja()
    cuenta_banco_codigo, cuenta_banco_nombre = config.cuenta_banco_codigo, config.cuenta_banco_nombre
    if cuenta_banco_id:
        try:
            cuenta_banco = CuentaContable.objects.get(pk=cuenta_banco_id, tipo=CuentaContable.Tipo.BANCO, activo=True)
        except (CuentaContable.DoesNotExist, ValueError, TypeError):
            raise ConfiguracionInvalida("Elige una cuenta de Banco válida de la lista.")
        cuenta_banco_codigo, cuenta_banco_nombre = cuenta_banco.codigo, cuenta_banco.nombre

    campos = {
        "diario": diario.strip(), "cuenta_efectivo_codigo": cuenta_efectivo_codigo.strip(),
        "cuenta_efectivo_nombre": cuenta_efectivo_nombre.strip(),
        "cuenta_transbank_codigo": cuenta_transbank_codigo.strip(),
        "cuenta_transbank_nombre": cuenta_transbank_nombre.strip(),
        "cuenta_mercado_pago_codigo": cuenta_mercado_pago_codigo.strip(),
        "cuenta_mercado_pago_nombre": cuenta_mercado_pago_nombre.strip(),
        "cuenta_banco_codigo": cuenta_banco_codigo, "cuenta_banco_nombre": cuenta_banco_nombre,
        "codigo_analitico": codigo_analitico.strip(),
        "razon_social": config.razon_social if razon_social is None else razon_social.strip(),
    }
    if not all(campos[c] for c in campos if c != "codigo_analitico"):
        raise ConfiguracionInvalida("Completa el diario, las cuentas (código y nombre) y el nombre de la empresa antes de guardar.")

    anterior = {k: getattr(config, k) for k in campos}
    if anterior == campos:
        return config
    for k, v in campos.items():
        setattr(config, k, v)
    config.save()
    _bitacora(usuario, "ConfiguracionArqueoCaja", config.pk, "editar_config_arqueo_caja", anterior, campos)
    return config


# ---------------------------------------------------- descuentos a trabajadores (Karla, 2026-10-27)

def _porcentaje(valor) -> Decimal:
    try:
        pct = Decimal(str(valor).replace("%", "").replace(",", ".").strip())
    except InvalidOperation:
        raise ConfiguracionInvalida("Escribe el descuento como un número, ej. 15 para 15%.")
    if not (0 < pct <= 100):
        raise ConfiguracionInvalida("El descuento debe estar entre 0 y 100%.")
    return pct.quantize(Decimal("0.01"))


@transaction.atomic
def crear_descuento(producto: str, porcentaje, usuario):
    from nucleo.descuentos_servicio import normalizar
    from nucleo.models import DescuentoTrabajador
    producto = " ".join(str(producto or "").upper().split())
    if not producto:
        raise ConfiguracionInvalida("Escribe el nombre del producto tal como aparece en Resto.")
    if any(normalizar(d.producto) == producto for d in DescuentoTrabajador.objects.all()):
        raise ConfiguracionInvalida(f"{producto} ya está en la tabla de descuentos.")
    pct = _porcentaje(porcentaje)
    _bitacora(usuario, "DescuentoTrabajador", producto, "crear_descuento_trabajador", None, {"producto": producto, "porcentaje": str(pct)})
    return DescuentoTrabajador.objects.create(producto=producto, porcentaje=pct)


@transaction.atomic
def editar_descuento(descuento, *, porcentaje, activo: bool, usuario):
    pct = _porcentaje(porcentaje)
    anterior = {"porcentaje": str(descuento.porcentaje), "activo": descuento.activo}
    posterior = {"porcentaje": str(pct), "activo": activo}
    if anterior == posterior:
        return descuento
    _bitacora(usuario, "DescuentoTrabajador", descuento.pk, "editar_descuento_trabajador", anterior, posterior)
    descuento.porcentaje, descuento.activo = pct, activo
    descuento.save(update_fields=["porcentaje", "activo"])
    return descuento
