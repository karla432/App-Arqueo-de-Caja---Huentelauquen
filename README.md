# Handoff · Arqueo Diario de Caja y Conciliación de Ventas

**Hacienda Huentelauquén · Sala de Ventas**
Product Owner: Karla, Gerente de Administración y Finanzas.

## Cómo usar este bundle

1. Abre **`PROMPT.md`** y pégalo completo como primer mensaje en Claude Code. Ese archivo es
   el encargo: define el producto, los roles, los sub-agentes, las cifras ya medidas, los
   controles no negociables y la instrucción explícita de **preguntar antes de desarrollar**.
2. Los dos HTML de `design/` son las **referencias de diseño**. Ábrelos en el navegador.
   No son código de producción: hay que recrearlos en el stack que se acuerde con Karla.

## Overview

La aplicación reconstruye la venta diaria de la Sala de Ventas desde el export del POS, la
coteja contra los movimientos del adquirente (Transbank), resuelve las excepciones con
decisión humana trazable, determina el efectivo esperado, lo controla contra el conteo físico
y el depósito, y —solo una vez aprobado el día— genera el archivo de importación a Odoo.

El principio rector es que **la exportación a Odoo es la consecuencia de un día conciliado y
aprobado, no el objetivo del sistema**. Un día con excepciones sin resolver no se exporta.

Hoy el proceso se hace a mano en un Excel de 12 MB con 8 hojas ocultas y 3 tablas dinámicas.
La aplicación reemplaza ese andamiaje.

## About the design files

Los archivos de `design/` son **prototipos en HTML** que muestran la intención visual y
funcional. No se despliegan tal cual. La tarea es recrearlos en el entorno del proyecto
—React, Vue, Blazor, lo que se defina— con sus patrones y librerías establecidas; si no hay
entorno todavía, elegir el framework más adecuado y proponerlo a Karla antes de empezar.

Se abren directamente en el navegador (doble clic). `support.js` y `doc-page.js` son runtimes
del prototipo, no dependencias del producto final: no los lleves al proyecto real.

## Fidelity

**Alta fidelidad.** Colores, tipografía, jerarquía, densidad y contenido son definitivos y
están tomados de la identidad de marca oficial de Huentelauquén. La UI debe recrearse con
fidelidad de píxel usando las librerías del stack destino.

Excepción: las cifras mensuales y anuales de la Pantalla 1, el conteo físico de efectivo, el
fondo de caja y el depósito son **ilustrativos** —no existen en los archivos entregados—.
Todo el resto de los datos mostrados es real (02‑09‑2026).

## Pantallas

Orden del menú lateral, que es también el orden de las secciones en el archivo de UI.

### 1 · Ventas · mensual y anual
Tablero de gestión. Todo cuelga de un único selector **desde–hasta**; los atajos (Mes,
Trimestre, Año en curso, 12 meses, Personalizado) cargan un rango en esas mismas dos fechas,
así que cualquier vista es el mismo tablero con otro rango, siempre comparado contra el mismo
rango del año anterior.

Contenido: 5 KPI (venta neta, comandas, ticket promedio, venta diaria promedio, unidades) ·
barras mensuales 2026 vs 2025 con acumulado · venta por día del período · concentración
horaria · mix de medios de pago con Δ año · venta por caja · top 8 productos con % acumulado ·
bloque de control del período. Cierra con la **regla de la fuente única**: el tablero solo
suma días en estado Cerrado o Exportado, y muestra explícitamente los días excluidos y su
monto provisorio para que nadie explique una caída de venta que en realidad es un arqueo
pendiente.

### 2 · Dashboard del día
Responde en cinco segundos: ¿cuadró el día, cuánto falta por resolver, puedo aprobarlo?
Cabecera con estado · banda de alerta que explica por qué no se puede aprobar · 4 KPI ·
cuadre por medio de pago (declarado POS / proveedor / conciliado / diferencia) · cajas y
turnos · calidad de la conciliación por nivel · avisos del día · lista de verificación para
cerrar con botones habilitados según estado y rol.

### 3 · Carga de archivos
Dos zonas de arrastre (POS y adquirente) con huella, usuario y hora · resumen de lo leído por
hoja · zona punteada para una fuente adicional futura · tabla de 6 validaciones de carga ·
descarga de los originales íntegros.

### 4 · Excepciones
Cabecera maroon con total abierto y monto en juego · filtros por tipo · lista ordenada **por
monto en juego, no por orden de aparición** · panel de evidencia con lado POS y candidato del
proveedor lado a lado, score y nivel · decisión propuesta explicada · motivo obligatorio
desde lista cerrada + comentario libre · tres resoluciones (aceptar, rechazar, vincular otro
movimiento).

### 5 · Arqueo de efectivo
Teórico → ajustes → esperado · tabla por caja con fondo, gastos, contado y diferencia, más
consolidado · formulario de depósito · panel de autorización de la diferencia cuando excede
el umbral, con motivo desde lista cerrada.

### 6 · Cierre y exportación
Lista de verificación previa al cierre · correspondencia de campos hacia el formato de la
hoja *Captura Odoo1* · resumen de lo que se exporta · historial de versiones con acuse de
importación en Odoo.

### 7 · Histórico
Calendario del mes con semáforo por día y pendientes visibles · tabla de últimos días con
**desglose por forma de pago** (Transbank / Efectivo / Mercado Pago) que siempre suma la venta
del día, más fila de totales · búsqueda transversal por comanda, comprobante, autorización o
monto · apertura de cualquier día en modo lectura.

### 8 · Calidad de registro
Módulo de gestión, no de arqueo: nada aquí bloquea un cierre. KPI de conciliación automática,
score, ventas sin autorización y medios mal declarados · tabla por caja y turno · excepciones
del mes por tipo · acciones sugeridas · días con observaciones.

## Design tokens

| Token | Valor | Uso |
|---|---|---|
| Maroon | `#8C0026` | Estructura: barra lateral, encabezados de pantalla, títulos. Nunca en botones. |
| Rojo | `#ED0035` | Acción y foco: botón primario, kickers, encabezado de tabla, indicador activo. Uno por zona. |
| Rosado | `#F2DCDB` | Dato que pide atención: cifra clave, tarjeta destacada. |
| Rosado claro | `#FDEAEE` | Recuadro «Importante», fila en excepción, banda de alerta. |
| Borde rosado | `#E4B5BF` | Bordes de tabla y de superficie rosada. |
| Tarjeta neutra | `#FAFAFA` | Tarjetas de KPI y de contenido. |
| Celda etiqueta | `#F4F4F4` | Fondos de lista y pistas de barra. |
| Borde neutro | `#E8E8E8` | Borde de tarjeta neutra. |
| Texto cuerpo | `#3A3A3A` | Todo el cuerpo. |
| Leyendas | `#6B6B6B` | Subtítulos, notas al pie de tabla. |
| Tinta fuerte | `#222222` | Énfasis. |
| Verde estado | `#1B7A4B` | Solo semáforo. Fondo `#E9F5EE`, borde `#BFE0CC`. |
| Ámbar estado | `#B26B00` | Solo semáforo. Fondo `#FDF3E3`, borde `#EAD6AE`. |

**El dato conciliado es neutro, no verde.** El verde y el ámbar son funcionales y no entran
en gráficos, títulos ni superficies. La marca sigue siendo rojo, maroon y rosado.

### Tipografía

- Títulos y cifras de cabecera: **Caladea** 700 — equivalente métrico exacto de Cambria.
- Todo lo demás: **Carlito** 400/700 — equivalente métrico exacto de Calibri.
- Ambas son libres y están en Google Fonts. Si el stack destino tiene Cambria y Calibri
  disponibles, úsalas directamente: el reemplazo es 1:1 y un pantallazo pegado en un Word o
  PPT de Huentelauquén calza sin recomponer.
- Escala usada: cifra de KPI 28–31 px / título de pantalla 24–29 px / kicker 11 px con
  `letter-spacing: 1.4px` en mayúsculas / cuerpo 13,5 px / tabla 13 px / encabezado de tabla
  11 px mayúsculas / nota al pie 12 px.

### Componentes

- **Botón primario** — relleno rojo, texto blanco, 10/18 px de padding, sin radio. Hover:
  maroon.
- **Botón secundario** — fondo blanco, borde y texto rojo. Hover: fondo rosado claro, texto
  maroon.
- **Botón deshabilitado** — `#F2F2F2`, borde neutro, texto `#A8A8A8`, y **siempre explica su
  condición** al pasar el cursor.
- **Pills de estado** — 4 variantes: conciliado (verde), por revisar (ámbar), excepción
  (rosado/maroon), sin cargar (gris). 11 px, 700, radio completo.
- **Tabla** — encabezado rojo con texto blanco en mayúsculas, bordes `#E4B5BF`, filas pares
  `#FCFAFA`, cifras con `font-variant-numeric: tabular-nums` alineadas a la derecha.
- **Recuadro importante** — fondo `#FDEAEE` con barra izquierda roja de 3 px.
- **Barra lateral** — maroon, ítem activo con fondo `#7A0021` y barra izquierda roja; ítems
  inactivos en `#E8C9CF`.
- **Gráficos** — barras CSS planas, rojo para el período actual y rosado para el comparativo.
  Sin gradientes, sin sombras, sin ejes decorativos.

### Layout

Marco de aplicación de **1440 px** con `box-sizing: border-box`. Barra lateral fija de 244 px.
Contenido en grillas de 2 a 5 columnas con `gap` de 14 a 26 px. Separación entre secciones por
espacio en blanco, no por líneas.

## Datos reales medidos (02‑09‑2026)

Ver el detalle completo en `PROMPT.md`. Lo esencial:

- POS: **336 comandas**, 686 líneas, 31 artículos, venta **$5.274.235** = efectivo $501.461 +
  débito $3.770.006 + crédito $1.002.768 + transferencias $0.
- Transbank: **295 movimientos**, monto cotejable **$4.968.393**, 4 terminales, 9
  autorizaciones alfanuméricas, 0 duplicadas.
- Brecha POS↔Transbank en tarjetas: **$195.619** en el mismo sentido — venta cobrada que el
  POS no registró.
- ⚠️ Deduplicar por **`N_INTERNO`** antes de sumar: las columnas de pago se repiten en cada
  línea de artículo.
- El POS **no identifica al cajero**: solo caja y turno. Ningún archivo relaciona terminal
  con caja.

## State management

Estados del día, con transición estricta y sin retroceso silencioso:

`Sin cargar → Cargado → Normalizado → Conciliado con excepciones → Excepciones resueltas →
Aprobado → Cerrado → Exportado`

Reabrir un día aprobado **invalida su exportación** y obliga a regenerarla. Una exportación
nunca se edita. Reconciliar es idempotente: no borra decisiones manuales, solo recalcula lo
automático.

## Entidades

Día de arqueo · Lote de carga · Venta (clave `N_INTERNO`) · Línea de venta · **Pago declarado
(un registro por medio con monto > 0** — esta es la decisión estructural más importante del
modelo: permite que una comanda mixta concilie parcialmente) · Movimiento de proveedor ·
Coincidencia · Excepción · Arqueo de efectivo · Exportación · Evento de auditoría.

## Criterios de aceptación y casos de prueba

**16 criterios** en la sección 23 y **20 casos de prueba** en la sección 24 del diseño
funcional. Todos son verificables contra los archivos reales del 02‑09‑2026. El sub-agente de
QA es el dueño de esa lista y su firma es independiente de Desarrollo.

## Assets

`design/assets/logo-huentelauquen.png` — logo oficial, entregado por Karla.
Íconos: usar el set del stack destino; el prototipo no dibuja iconografía.

## Files

```
PROMPT.md                                  ← pegar en Claude Code
README.md                                  ← este archivo
design/Arqueo Diario - Diseno Funcional.dc.html   ← 28 secciones, especificación funcional
design/Arqueo Diario - App UI.dc.html             ← 8 pantallas, alta fidelidad
design/assets/logo-huentelauquen.png
design/support.js                          ← runtime del prototipo, no del producto
design/doc-page.js                         ← runtime del prototipo, no del producto
```
