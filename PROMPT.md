# PROMPT PARA CLAUDE CODE — pegar completo al iniciar la sesión

---

Vas a construir una aplicación interna para **Hacienda Huentelauquén** llamada
**Arqueo Diario de Caja y Conciliación de Ventas** (Sala de Ventas).

En este bundle vienen dos archivos HTML que son **referencias de diseño**, no código de
producción:

- `design/Arqueo Diario - Diseno Funcional.dc.html` — el diseño funcional completo: alcance,
  arquitectura, modelo de datos, motor de conciliación por niveles, excepciones, arqueo,
  auditoría, exportación a Odoo, criterios de aceptación, casos de prueba, riesgos y **28
  secciones** incluyendo las decisiones pendientes.
- `design/Arqueo Diario - App UI.dc.html` — el diseño visual de las **8 pantallas** con la
  identidad de marca de Huentelauquén y datos reales del 02‑09‑2026.

Ábrelos en el navegador y léelos completos antes de cualquier otra cosa. Son la
especificación. No los copies como código: recréalos en el stack que definamos.

## REGLA NÚMERO UNO — NO ESCRIBAS CÓDIGO TODAVÍA

**Antes de desarrollar una sola línea, debes consultarme.** Tu primera respuesta en esta
sesión no es código ni scaffolding: es una **ronda de preguntas** dirigida a mí.

No asumas stack, no asumas base de datos, no asumas despliegue, no asumas cómo se resuelven
las 12 decisiones funcionales pendientes de la sección 28. Pregunta.

Cuando yo responda, **resume lo que entendiste y espera mi confirmación explícita** antes de
empezar. Si en cualquier momento del desarrollo aparece una decisión de negocio que el diseño
funcional no cierra, **te detienes y me preguntas** — no eliges por mí y no dejas un `TODO`
silencioso.

## ROLES DEL EQUIPO

### Product Owner — **Karla** (yo)

Soy Gerente de Administración y Finanzas de Hacienda Huentelauquén. Soy la dueña del producto
y la única que decide sobre reglas de negocio, umbrales, alcance y prioridades. Trátame como
tal: hazme preguntas de negocio en lenguaje de negocio, no me pidas decisiones técnicas que
te corresponden a ti, y no me entregues jerga sin traducir.

### Sub-agentes que debes crear y usar

Crea estos **ocho** sub-agentes y **hazlos participar de verdad** — cada entregable relevante
pasa por el que corresponde antes de llegar a mí. Cuando dos discrepan, me presentas ambas
posiciones en una línea cada una y yo decido.

| Sub-agente | Responsabilidad | Poder de veto sobre |
|---|---|---|
| **Arquitecto de Software** | Stack, límites entre capas, contrato de los importadores, modelo de datos, extensibilidad a nuevos proveedores, rendimiento y estrategia de migración. Dueño de que el motor **no** contenga lógica de ningún proveedor. | Diseño técnico y elección de stack |
| **Desarrolladores** | Implementación: importadores, normalización, motor de conciliación, pantallas, exportador. Escriben las pruebas unitarias de lo que construyen. Dueños de la deuda técnica que generan. | Nada — proponen, no deciden |
| **UI** | Fidelidad a las 8 pantallas y a la identidad de marca; accesibilidad, densidad, estados de error y de carga; que ninguna acción bloqueada quede sin explicar. | Cambios visuales |
| **Contador Senior** | Correctitud contable: tratamiento de tarjetas, efectivo, anulaciones, documentos tributarios, y que el archivo de exportación a Odoo sea contablemente válido. | Reglas de asiento y de exportación |
| **Tesorero experto en riesgos** | Control interno: segregación de funciones, umbrales de diferencia, trazabilidad, inmutabilidad de la evidencia, riesgo de fraude y de manipulación del arqueo. | Cualquier atajo que debilite un control |
| **Experto en QA** | Los 16 criterios de aceptación y los 20 casos de prueba de las secciones 23 y 24. Ninguna entrega se declara lista sin su firma. | Declarar «listo» |
| **QA Golden Master** | Custodia el **día de referencia 02‑09‑2026** como caso maestro: fija la salida esperada (venta $5.274.235 en 336 comandas, $4.968.393 cotejables, brecha $195.619, cuadre por caja y por medio) y la compara byte a byte en cada cambio. Cualquier diferencia contra el golden master detiene la entrega hasta que se explique o se apruebe una nueva línea base. Mantiene además un golden master por cada mes real que se vaya cargando. | Cualquier cambio que altere la salida del día de referencia sin explicación |
| **Auditor de Seguridad** | Autenticación y roles, control de acceso por operación, protección de la evidencia y de la bitácora, cifrado en tránsito y en reposo, manejo de datos de tarjeta (que **no** deben almacenarse más allá de lo que el archivo ya trae), inyección y validación de archivos subidos, secretos y credenciales, registro de accesos. | Cualquier brecha de acceso o de protección de evidencia |

Reglas de operación del equipo:

1. **Contador Senior, Tesorero y Auditor de Seguridad mandan sobre Desarrolladores y
   Arquitecto** en materia de reglas, controles y protección. Si una regla contable, de
   control o de seguridad encarece el desarrollo, se implementa igual o se me escala; no se
   simplifica por comodidad técnica.
2. **El Arquitecto define, los Desarrolladores implementan.** Un desarrollador que quiere
   salirse del diseño técnico lo escala al Arquitecto, no lo hace por su cuenta.
3. **Ningún QA reporta a Desarrollo ni al Arquitecto.** Sus firmas son independientes y son
   dos: la de casos de aceptación y la del golden master.
4. **El golden master del 02‑09‑2026 se fija en la primera entrega** y desde ahí toda
   entrega se compara contra él. Es la red de seguridad de todo el proyecto.
5. **Ningún sub-agente me habla en jerga.** Todo lo que llega a mí viene traducido a decisión
   de negocio con sus consecuencias.
6. Cada vez que me pidas una decisión, dime **qué recomienda cada sub-agente afectado**.

## LO QUE YA ESTÁ MEDIDO — no lo vuelvas a suponer

Se analizaron los dos archivos reales del **02 de septiembre de 2026**. Estas cifras son
mediciones, no estimaciones, y el sistema debe reproducirlas exactamente:

**POS — hoja VENTAS POR ARTICULO**

- 336 comandas · 686 líneas de artículo · 31 artículos distintos.
- Venta total **$5.274.235**, que cuadra exacto: efectivo **$501.461** + débito
  **$3.770.006** + crédito **$1.002.768** + transferencias **$0**.
- La columna **TRANSFERENCIAS existe pero llega en cero** ese día. Normalízala igual.
- Las 336 comandas son `COBRADA`, todas `FAC`, todas `MOSTRADOR`, con turnos
  `TURNO CAJA 1..4`. **El POS no identifica al cajero** — solo la caja.
- Cuatro cajas: Caja 4 (111 comandas, $2.022.579), Caja 3 (102, $1.459.759),
  Caja 2 (62, $757.033), Caja 1 (61, $1.034.864).
- ⚠️ **Trampa crítica:** las columnas de pago y `TOTAL_COMADA` se repiten idénticas en cada
  línea de artículo de la misma comanda. **Deduplicar por `N_INTERNO` antes de sumar
  cualquier cosa.** Sumar por línea multiplica la venta. Es el error número uno.

**POS — hoja VENTA CONSOLIDADA**

- 341 filas, encabezado corrido, y **contradice** a VENTAS POR ARTICULO en monto, medio y
  caja (verificado en la comanda 654747). No es confiable para cuadrar: úsala solo para
  enriquecer autorización, cupón, tipo de tarjeta y hora de cierre, validando cada campo.

**Transbank — cartola de movimientos**

- 295 movimientos, todos «Venta», 0 anulaciones. Monto cotejable **$4.968.393**.
- Débito 223 mov. $3.821.086 · Crédito Mastercard 33 · Crédito VISA 27 · Prepago 11 ·
  American 1. Ventana 10:24–19:30, peak a las 14:00 con 54 movimientos.
- Cuatro terminales: A2885575, A2885576, A2885577 y **A2380302** (serie distinta).
  **Ningún archivo relaciona terminal con caja.**
- 197 montos distintos en 295 movimientos: **el monto solo no identifica**. Pero solo 18 de
  295 comparten monto dentro de ±10 min y 9 dentro de ±3 min: la ambigüedad real es ≈6 %.
- **9 autorizaciones alfanuméricas** (B93179, FH00DZ, ORAVPZ…) y **0 duplicadas**.
- 233 PENDIENTE / 62 ABONADA con fecha de abono 04‑09: **concilia por fecha de movimiento,
  nunca por fecha ni estado de abono.**

**El hallazgo que define el primer día de uso**

Transbank cobró **$195.619 más** que lo declarado en tarjetas por el POS
(`$4.968.393 − $4.772.774`), y las dos diferencias van en el mismo sentido: **no es medio mal
declarado, es venta cobrada que el POS no registró.** El sistema debe abrir ese día con esa
brecha íntegramente explicada en excepciones.

## LAS 12 DECISIONES QUE DEBES PREGUNTARME

Están en la sección 28 del diseño funcional. **Ninguna se puede deducir de los archivos.**
Las cinco primeras bloquean el diseño detallado; tráemelas en tu primera ronda:

| Id | Decisión |
|---|---|
| **D1** | ¿Mantenemos VENTA CONSOLIDADA como fuente de autorización y cupón, pese a que contradice montos, medios y cajas? |
| **D2** | ¿Qué terminal corresponde a qué caja, y qué es A2380302? |
| **D3** | Umbral de diferencia de caja: ¿monto fijo, porcentaje, ambos? ¿Quién autoriza por sobre el umbral? |
| **D4** | ¿El día operativo corta a medianoche o al cierre de la sala? |
| **D5** | ¿Qué cliente se usa en la exportación a Odoo para venta de sala sin identificación? |
| D6 | ¿La hoja Tabla Descuentos es referencia vigente o residuo del trabajo manual? |
| D7 | ¿Las transferencias declaradas en el POS se cotejan alguna vez contra cartola bancaria? |
| D8 | ¿Conteo de efectivo por denominación o solo total por caja? |
| D9 | Lista cerrada de motivos de resolución de excepciones y de autorización de diferencias. |
| D10 | ¿Quiénes son los aprobadores designados y qué pasa en su ausencia? |
| D11 | Plazo máximo entre cierre operativo y aprobación. |
| D12 | ¿Mercado Pago entra en Fase 2 y con qué reporte disponible? |

Además de esas, **pregúntame lo técnico que te falte**: dónde corre esto, si hay
infraestructura o base de datos existente, cuántos usuarios concurrentes, si debe funcionar
sin internet en la sala, cómo se autentican las personas, si la exportación a Odoo debe ser
archivo o API, y qué pasa con los meses históricos ya arqueados a mano.

## LO QUE NO ES NEGOCIABLE

Estos son controles, no preferencias. El Tesorero tiene veto sobre cualquiera de ellos:

1. **Quien opera no aprueba.** Si una misma persona tiene ambos roles, se permite pero se
   registra como excepción de control.
2. **La reclasificación de medio de pago es siempre manual.** El motor propone; nunca cambia
   un medio por su cuenta.
3. **Ningún emparejamiento automático bajo score 85.** Niveles 1 a 3 se aceptan solos; del 4
   al 6 siempre pasan por decisión humana con motivo obligatorio.
4. **El resultado no depende del orden de las filas.** Reordenar el Excel debe producir
   exactamente la misma conciliación. Esto exige matching global por score total máximo, no
   «primer candidato que calza».
5. **Ningún importador descarta filas.** Lo que no se entiende se marca «no interpretado» y
   se cuenta. Un archivo del que se descartan filas en silencio es un arqueo que cuadra por
   omisión.
6. **El archivo original es inmutable** y descargable siempre. Toda corrección es un registro
   nuevo, nunca una sobrescritura.
7. **La bitácora de auditoría no la edita nadie**, en ningún rol.
8. **Un día con excepciones bloqueantes abiertas no se aprueba ni se exporta.**
9. **El arqueo de efectivo se controla por caja**, no solo consolidado: un total en $0 que
   esconde +$8.000 y −$11.800 en dos cajas no es un día cuadrado.
10. **Se guarda la versión de reglas y los parámetros de cada conciliación**, para que un día
    de hace seis meses pueda explicarse con las reglas de entonces.
11. **El motor no contiene ninguna regla que mencione a Transbank por nombre.** Incorporar un
    proveedor nuevo debe ser escribir un importador.
12. **Cada operación se autoriza por rol en el servidor**, no solo escondiendo botones en la
    pantalla. Aprobar, cerrar, reabrir, autorizar diferencias y exportar son operaciones
    privilegiadas y quedan registradas con usuario, hora y origen.
13. **No se almacena ningún dato de tarjeta más allá de lo que el archivo del adquirente ya
    trae.** Nada de números completos, nada de datos sensibles derivados.
14. **Los archivos subidos se validan antes de procesarse** (tipo, tamaño, estructura) y se
    guardan fuera de cualquier ruta servida al navegador.

## FIDELIDAD DEL DISEÑO

El archivo de UI es **alta fidelidad**: colores, tipografía y jerarquía son definitivos.
Recréalo con las librerías y patrones del stack que acordemos, respetando los tokens:

- Maroon `#8C0026` — estructura: barra lateral, encabezados, títulos. **Nunca en botones.**
- Rojo `#ED0035` — acción y foco: botón primario, kickers, encabezado de tabla, indicador
  activo. Uno por zona.
- Rosados `#F2DCDB` / `#FDEAEE` — el dato que pide atención; borde `#E4B5BF`.
- Neutros `#FAFAFA` / `#F4F4F4`, borde `#E8E8E8`. **El dato conciliado es neutro, no verde.**
- Texto `#3A3A3A`, leyendas `#6B6B6B`, tinta fuerte `#222222`.
- Verde `#1B7A4B` y ámbar `#B26B00` **solo** como semáforo de estado. No en gráficos ni
  títulos.
- Tipografía: **Caladea** (equivalente métrico de Cambria) para títulos y cifras de cabecera;
  **Carlito** (equivalente métrico de Calibri) para todo lo demás. Si el stack ya tiene
  Cambria y Calibri disponibles, úsalas directamente.
- Las 8 pantallas, en el orden del menú: **Ventas · mensual y anual**, Dashboard del día,
  Carga de archivos, Excepciones, Arqueo de efectivo, Cierre y exportación, Histórico,
  Calidad de registro.
- Principio de interacción transversal: **una acción bloqueada siempre explica su
  condición.** Nunca un botón gris sin decir qué falta.

## CÓMO QUIERO QUE TRABAJES

- Entregas incrementales y verificables. La primera no es «la app»: es el **importador con la
  deduplicación por `N_INTERNO` correcta**, probado contra el archivo real hasta reproducir
  $5.274.235 en 336 comandas. Si eso no cuadra, nada de lo demás importa.
- Después: modelo de datos, motor de conciliación con los seis niveles y el score, bandeja de
  excepciones, arqueo, cierre, exportación, y al final los tableros.
- Cada entrega llega con **dos firmas**: la del Experto en QA contra los casos de prueba que
  le correspondan, y la del QA Golden Master confirmando que el día de referencia sigue dando
  exactamente lo mismo.
- Explícame las decisiones técnicas en una línea, en castellano, sin jerga.
- Cuando no sepas, pregunta. Prefiero cinco preguntas hoy que un motor de conciliación que
  adivina.

---

**Ahora: no escribas código. Lee los dos archivos de diseño, crea los ocho sub-agentes,
y hazme tu primera ronda de preguntas.**
