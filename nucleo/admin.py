"""Registro en el admin de Django — útil para inspeccionar datos durante el
desarrollo. No es el Mantenedor de usuarios ni ninguna pantalla del
producto final (esas vienen en etapas posteriores)."""
from django.contrib import admin

from . import models


@admin.register(models.DiaArqueo)
class DiaArqueoAdmin(admin.ModelAdmin):
    list_display = ("fecha", "estado", "operado_por", "aprobado_por")
    list_filter = ("estado",)


@admin.register(models.LoteCarga)
class LoteCargaAdmin(admin.ModelAdmin):
    list_display = ("dia", "fuente", "nombre_archivo", "version", "cargado_por", "cargado_en")
    list_filter = ("fuente",)


@admin.register(models.Venta)
class VentaAdmin(admin.ModelAdmin):
    list_display = ("n_interno", "dia", "caja", "turno", "total_comanda", "estado")
    list_filter = ("dia", "caja")
    search_fields = ("n_interno", "nro_comprobante")


@admin.register(models.PagoDeclarado)
class PagoDeclaradoAdmin(admin.ModelAdmin):
    list_display = ("venta", "medio", "monto", "medio_aprobado")
    list_filter = ("medio",)


@admin.register(models.MovimientoProveedor)
class MovimientoProveedorAdmin(admin.ModelAdmin):
    list_display = ("dia", "proveedor", "fecha_hora", "tipo_tarjeta", "monto_venta_valido_abono", "terminal")
    list_filter = ("proveedor", "tipo_tarjeta", "terminal")
    search_fields = ("autorizacion",)


@admin.register(models.MotivoResolucion)
class MotivoResolucionAdmin(admin.ModelAdmin):
    list_display = ("codigo", "texto", "aplica_a", "activo")


@admin.register(models.Excepcion)
class ExcepcionAdmin(admin.ModelAdmin):
    list_display = ("dia", "tipo", "monto_en_juego", "abierta", "resolucion")
    list_filter = ("tipo", "abierta")


@admin.register(models.ArqueoEfectivo)
class ArqueoEfectivoAdmin(admin.ModelAdmin):
    list_display = ("dia", "caja", "esperado", "contado", "diferencia")


@admin.register(models.Exportacion)
class ExportacionAdmin(admin.ModelAdmin):
    list_display = ("dia", "version", "generado_por", "generado_en", "acuse_estado", "invalidada")


@admin.register(models.EventoAuditoria)
class EventoAuditoriaAdmin(admin.ModelAdmin):
    list_display = ("hora", "usuario", "entidad", "accion")
    list_filter = ("entidad", "accion")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(models.LineaVenta)
admin.site.register(models.VersionMotor)
admin.site.register(models.Coincidencia)
admin.site.register(models.ConteoDenominacion)
admin.site.register(models.Deposito)
