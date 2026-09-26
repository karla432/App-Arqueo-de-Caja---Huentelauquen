"""Comprueba que la bitácora no fue alterada: recalcula el sello encadenado de cada evento y busca huecos.

    python manage.py verificar_bitacora

Sale con código 1 si encuentra algo. Los eventos anteriores a la protección (sin sello) se cuentan aparte."""
from django.core.management.base import BaseCommand
from django.db import connection

from nucleo.models import EventoAuditoria


class Command(BaseCommand):
    help = "Verifica la integridad de la bitácora de auditoría (sellos encadenados, huecos y triggers)."

    def handle(self, *args, **opciones):
        problemas = []
        anterior_sello = ""
        anterior_id = None
        sin_sello = con_sello = 0
        for e in EventoAuditoria.objects.order_by("id").iterator():
            if anterior_id is not None and e.id != anterior_id + 1:
                problemas.append(f"Faltan eventos entre el id {anterior_id} y el {e.id} (¿borrados?).")
            anterior_id = e.id
            if not e.sello:
                sin_sello += 1
                continue
            con_sello += 1
            if e.sello_previo != anterior_sello:
                problemas.append(f"Evento {e.id}: la cadena no coincide con el evento anterior.")
            esperado = EventoAuditoria.calcular_sello(e.sello_previo, e.contenido_para_sello())
            if esperado != e.sello:
                problemas.append(f"Evento {e.id}: su contenido no coincide con su sello (alterado).")
            anterior_sello = e.sello

        if connection.vendor == "sqlite":
            with connection.cursor() as c:
                c.execute("select name from sqlite_master where type='trigger' and tbl_name='nucleo_eventoauditoria'")
                triggers = {fila[0] for fila in c.fetchall()}
            for nombre in ("bitacora_sin_borrado", "bitacora_sin_edicion"):
                if nombre not in triggers:
                    problemas.append(f"Falta el trigger de protección «{nombre}».")

        self.stdout.write(f"Eventos con sello verificados: {con_sello} · anteriores a la protección (sin sello): {sin_sello}")
        if problemas:
            for p in problemas:
                self.stderr.write(self.style.ERROR(p))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Bitácora íntegra."))
