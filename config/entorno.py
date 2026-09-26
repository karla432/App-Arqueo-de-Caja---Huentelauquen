"""Qué entorno usa la aplicación cuando nadie lo indica (Karla, 2026-09-26: «por defecto producción»).

* `ARQUEO_ENTORNO` manda si está definida.
* Si no, es PRODUCCIÓN: la opción segura para cualquier servidor.
* La única excepción es un equipo de desarrollo que tenga el archivo local `.entorno_desarrollo` en la carpeta del
  proyecto (no se versiona, así que jamás llega a un servidor por el repositorio)."""
from pathlib import Path

MARCA_LOCAL = ".entorno_desarrollo"


def resolver_entorno(variable: str | None, base_dir: Path) -> str:
    if variable is not None and variable.strip():
        return variable.strip().lower()
    return "desarrollo" if (Path(base_dir) / MARCA_LOCAL).exists() else "produccion"
