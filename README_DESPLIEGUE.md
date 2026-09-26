# Despliegue y migración de cuentas (seguridad y perfiles)

Este documento no contiene credenciales. Las contraseñas y la clave secreta se ingresan por teclado o por variables
de entorno del servidor, nunca en archivos del repositorio.

## 1. Perfiles

| Perfil | Qué hace | Qué NO hace |
|---|---|---|
| Encargado de Ingresos | Carga archivos, registra ventas/arqueos/depósitos/reintegros, resuelve incidencias operativas, genera capturas | No valida, no visa, no aprueba, no reabre, no configura |
| Jefe de Contabilidad | Valida depósitos/reintegros/documentos/notas de crédito, visa diferencias, aprueba cierres ordinarios, genera archivos finales | No aprueba ni visa lo que él preparó; no opera; no autoriza excepciones; no reabre |
| Gerente de Administración y Finanzas | Todo lo funcional: autoriza excepciones y diferencias sobre el umbral, reabre días (con motivo), configura parámetros y asigna perfiles | Si aprobó o visó algo que ella preparó, debe indicar el motivo (queda como excepción de control) |
| Administrador técnico | Usuarios, grupos e infraestructura en /admin/ | No ve ni opera pantallas financieras; no aprueba nada, aunque sea superusuario |
| Solo Lectura | Consulta días, ventas, arqueos, depósitos, reintegros, estados y reportes; descarga solo archivos ya generados y autorizados | No carga, crea, edita, valida, aprueba, reabre ni genera nada. No descarga archivos originales cargados |
| Sin perfil | Solo la pantalla «Sin perfil asignado» | Nada financiero |

«Solo Visualización» fue retirado: quien lo tenía funciona como Solo Lectura y ya no se puede asignar.

Ser superusuario o staff **no** otorga ningún permiso de la aplicación.

## 2. Variables de entorno de producción

**Por defecto la aplicación arranca en PRODUCCIÓN.** Un equipo de desarrollo se declara con `ARQUEO_ENTORNO=desarrollo` o con un archivo vacío `.entorno_desarrollo` en la carpeta del proyecto (no se versiona; no debe existir en el servidor).

Ver `.env.example`. En producción la aplicación se niega a arrancar sin `DJANGO_SECRET_KEY` y
`DJANGO_ALLOWED_HOSTS`. Activa: `DEBUG=False`, redirección a HTTPS, cookies seguras, HSTS, sesión de 8 horas que expira
al cerrar el navegador.

Verificación previa a poner en marcha (con las variables ya cargadas):

```bash
python manage.py check --deploy
```

## 2b. Bloqueo por intentos fallidos

5 intentos fallidos seguidos bloquean el usuario 15 minutos (se levanta solo; ni la clave correcta entra mientras dure). Aplica también a `/admin/login/`, cuenta usuarios inexistentes igual que reales y deja en la bitácora el bloqueo de cuentas reales (`cuenta_bloqueada_por_intentos`). Para levantarlo antes: `python manage.py recuperar_acceso USUARIO --password-desde-entorno VARIABLE`. No protege contra ataques distribuidos desde muchas cuentas ni sustituye al doble factor.

## 3. Puesta en marcha

1. Respaldar `db.sqlite3` (copia del archivo con la aplicación detenida) y la carpeta de archivos cargados/exportaciones.
2. Actualizar el código y dependencias.
3. `python manage.py migrate` (aplica 0031: grupos de perfiles y sello de la bitácora; 0032: triggers que impiden borrar o editar la bitácora).
4. `python manage.py verificar_bitacora` → debe decir «Bitácora íntegra».
5. HTTPS: terminar TLS en el servidor web o proxy (nginx/IIS/etc.). Si es un proxy, `DJANGO_PROXY_SSL=1`.

## 4. Migración de cuentas SIN interrumpir el trabajo diario

No se borra ningún usuario, dato financiero ni evento. El orden importa:

1. **Informe** (no cambia nada): `python manage.py migrar_perfiles` → muestra, por cuenta, sus permisos efectivos y qué privilegios técnicos tiene hoy.
2. **Crear el Administrador técnico** (sin contraseña todavía):
   `python manage.py crear_admin_tecnico NOMBRE_DE_USUARIO`
3. **Fijar su contraseña** por teclado: `python manage.py changepassword NOMBRE_DE_USUARIO`
4. **Probar** que entra a `/admin/`, ve usuarios y grupos, y que los datos del negocio aparecen de solo consulta.
5. **Probar la recuperación**: `python manage.py recuperar_acceso NOMBRE_DE_USUARIO --reactivar` (y, si hace falta, `--password-desde-entorno VARIABLE`).
6. **Aplicar**: `python manage.py migrar_perfiles --aplicar`. Solo funciona si el paso 2–3 está listo. Retira superusuario/staff a las cuentas que ya tienen un perfil (Gerente, Jefe, etc.). Las cuentas sin perfil quedan sin acceso hasta que la Gerente les asigne uno en Configuración.
7. Cada cambio de grupos o privilegios queda en la bitácora.

Recuperación si se pierde el acceso: `recuperar_acceso` exige acceso a la consola del servidor (no es una pantalla).

## 5. Datos de prueba (solo desarrollo)

`python manage.py reiniciar_datos_de_prueba --confirmo-que-es-un-ambiente-de-pruebas` borra los días cargados y conserva la
configuración. **Se niega a correr en producción.**

## 6. Lo que este trabajo NO resuelve (declarado)

- Doble factor de autenticación: no implementado.
- Protección contra fórmulas maliciosas en archivos Excel cargados y validación profunda de xlsx: no verificada.
- Respaldos automáticos de la base y de los archivos: no configurados aquí.
- La protección de la bitácora por triggers está implementada solo para SQLite; con otro motor hay que replicarla.
- La configuración real de HTTPS del servidor no se puede verificar desde el desarrollo.
