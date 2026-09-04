# COSESA · Seguimiento de Obras

Aplicación web para que COSESA cargue el avance de sus obras y sus clientes lo sigan en tiempo real.

## Qué incluye este prototipo

- **Login separado** para personal de COSESA (rol `cosesa`) y para clientes (rol `cliente`).
- Estructura **Cliente → Obra → Ítem/equipo a intervenir**.
- Carga de **% de avance** por ítem, con bitácora día a día (cronograma).
- Carga de **fotos antes / después / progreso** por ítem.
- **Horas hombre y horas máquina** por avance registrado (se acumulan por ítem y por obra).
- **Reporte de cliente**: resumen por obra e ítem con fechas de inicio/fin, cronograma, fotos y observaciones. Se puede ver en pantalla, imprimir o **descargar en PDF**.
- **Informe interno de COSESA**: horas hombre/máquina totales y por ítem (no visible para el cliente). También descargable en PDF.
- Diseño con el logo y la paleta de colores de COSESA (negro, azul y blanco).

## Cómo probarlo

Requisitos ya incluidos en este entorno: Python 3, Flask, Playwright (para los PDF).

```bash
cd cosesa-obras
python3 seed.py      # crea la base de datos con datos de ejemplo
python3 app.py        # levanta el servidor en http://localhost:5050
```

Usuarios de prueba:

| Rol     | Usuario  | Contraseña   |
|---------|----------|--------------|
| COSESA  | `cosesa` | `cosesa123`  |
| Cliente | `cliente`| `cliente123` |

## Estructura del proyecto

```
cosesa-obras/
  app.py          -> rutas y lógica (Flask)
  db.py           -> conexión a SQLite
  schema.sql       -> modelo de datos
  seed.py          -> datos de ejemplo
  templates/        -> páginas (Jinja2)
  static/css        -> estilos con la identidad de COSESA
  static/img/logo.png
  static/uploads/    -> fotos subidas por ítem
```

## Despliegue en producción (Render)

La app corre en un contenedor Docker (ver `Dockerfile`) con `gunicorn` como servidor WSGI y Chromium ya instalado para generar los PDF. `render.yaml` define el servicio.

Variables de entorno:

| Variable         | Para qué sirve                                                                 |
|------------------|---------------------------------------------------------------------------------|
| `SECRET_KEY`     | Firma las sesiones y los links de PDF. Se genera sola en Render.                |
| `DATA_DIR`       | Carpeta donde se guardan la base de datos y las fotos. En Render: `/var/data`.  |
| `ADMIN_USERNAME` | Usuario del primer administrador (se crea solo la primera vez que arranca).     |
| `ADMIN_PASSWORD` | Contraseña de ese primer administrador.                                        |
| `ADMIN_NOMBRE`   | Nombre visible de ese usuario (opcional).                                       |

Con esas variables, la primera vez que la app arranca en un disco vacío crea la base de datos y el usuario administrador automáticamente — no hace falta consola ni `seed.py` en el servidor. Desde ahí, ese administrador crea los clientes, obras, operadores y usuarios de cliente desde la propia interfaz.

**Importante — persistencia en el plan gratuito**: sin un disco persistente pago, `/var/data` se reinicia en cada nuevo despliegue (cuando se sube una actualización de la app), y se pierde la base de datos y las fotos. Para que los datos sobrevivan a las actualizaciones hace falta sumar un disco persistente (pago) en Render.

## Próximos pasos sugeridos

1. **Notificaciones**: avisar por email al cliente cuando se carga un nuevo avance o se finaliza un ítem.
2. **App / acceso móvil**: el diseño ya es responsive, pero se puede empaquetar como PWA para que el personal de campo cargue avances y fotos desde el celular incluso con conectividad limitada.
3. **Backups automáticos** de la base de datos y de las fotos.
4. **Dominio propio** (ej. obras.cosesaservicios.com) y certificado HTTPS — Render lo da gratis para subdominios `.onrender.com`, y admite dominios propios con HTTPS también gratis.
