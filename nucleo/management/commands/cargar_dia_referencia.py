"""Carga el día de referencia 02-09-2026 en la base de datos local, desde
los archivos fixture del golden master. Uso:

    python manage.py cargar_dia_referencia
"""
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from nucleo.carga import cargar_pos, cargar_transbank

FIXTURES = Path(__file__).resolve().parents[3] / "pruebas" / "fixtures" / "dia_referencia_02-09-2026"
DIA_REFERENCIA = date(2026, 9, 2)


class Command(BaseCommand):
    help = "Carga el día de referencia 02-09-2026 (golden master) en la base de datos."

    def handle(self, *args, **options):
        Usuario = get_user_model()
        usuario, _ = Usuario.objects.get_or_create(
            username="carga_referencia", defaults={"is_staff": True},
        )

        lote_pos = cargar_pos(
            FIXTURES / "Captura_Ventas_Sala_de_Ventas_02.09.26.xlsx", DIA_REFERENCIA, usuario,
        )
        lote_tbk = cargar_transbank(
            FIXTURES / "Cartola_Movimientos_02-sep-2026.xlsx", DIA_REFERENCIA, usuario,
        )

        dia = lote_pos.dia
        self.stdout.write(self.style.SUCCESS(
            f"Día {dia.fecha}: {dia.ventas.count()} comandas, "
            f"{sum(v.lineas.count() for v in dia.ventas.all())} líneas, "
            f"venta total ${sum(v.total_comanda for v in dia.ventas.all()):,.0f}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"Transbank: {dia.movimientos.count()} movimientos, "
            f"${sum(m.monto_venta_valido_abono for m in dia.movimientos.all()):,.0f}"
        ))
