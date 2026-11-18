"""Módulo "Mantenimiento" de la Plataforma COSESA (ex "Mantenimiento, App 02").

- Flota (motobombas y vehículos), planes por horas/km, órdenes de
  mantenimiento, stock de repuestos y movimientos: se guardan como un único
  documento JSON versionado en la tabla mantenimiento_app_estado. Cada guardado
  lleva la versión que tenía el usuario; si otro usuario guardó antes, se
  rechaza (409) y la pantalla se recarga, así nunca se pisan cambios de stock.
- "Solicitudes de mantenimiento": son los registros que se cargan en la
  sección Mantenimiento de cada obra (tabla mantenimientos). No se copian:
  se leen directamente, así lo que se carga en la obra aparece al instante.

Todo vive en la misma base SQLite, así que entra en los backups a GitHub.
Se registra desde app.py con init_app(app, helpers).
"""
import json
import os

from flask import flash, jsonify, redirect, request, send_file, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_HTML = os.path.join(BASE_DIR, "templates", "mantenimiento_app.html")

PERMISOS = [
    ("modulo_mantenimiento", "Usar el módulo Mantenimiento: ver solicitudes, generar órdenes y mover stock de repuestos",
     "Módulo Mantenimiento", True),
    ("modulo_mantenimiento_admin", "Administrar el módulo Mantenimiento: equipos, planes, repuestos, importar horas, anular órdenes",
     "Módulo Mantenimiento", False),
]
ENDPOINTS = {
    "mantenimiento_app": "modulo_mantenimiento",
    "mantenimiento_api_estado": "modulo_mantenimiento",
    "mantenimiento_api_guardar": "modulo_mantenimiento",
    "mantenimiento_api_solicitudes": "modulo_mantenimiento",
    "mantenimiento_api_solicitud_estado": "modulo_mantenimiento",
    "mantenimiento_easytrack_leer": "modulo_mantenimiento", "mantenimiento_easytrack_estado": "modulo_mantenimiento",
    "mantenimiento_easytrack_sincronizar": "modulo_mantenimiento",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS mantenimiento_app_estado (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    doc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    actualizado_en TEXT DEFAULT (datetime('now')),
    actualizado_por TEXT
);
"""


def migrate(conn):
    conn.executescript(SCHEMA)
    import easytrack_sync
    easytrack_sync.migrate(conn)


def init_app(app, h):
    """h: helpers de app.py (get_db, get_current_user, cosesa_required, puede, is_admin)."""
    get_db = h.get_db

    def _err(msg, code=400, **extra):
        return jsonify({"ok": False, "error": msg, **extra}), code

    def _usuario_txt(user):
        return user["nombre"] or user["username"]

    def _puede_usar(user):
        return h.puede("modulo_mantenimiento", user)

    def _puede_admin(user):
        return h.is_admin(user) or h.puede("modulo_mantenimiento_admin", user)

    def _solicitud_json(r, fotos):
        return {
            "id": r["id"],
            "obra_id": r["obra_id"],
            "obra": r["obra_nombre"],
            "obra_codigo": r["obra_codigo"] or "",
            "cliente": r["cliente_nombre"],
            "fecha": r["fecha"] or "",
            "equipo": r["equipo"] or "",
            "descripcion": r["descripcion"],
            "estado": r["estado"],
            "prioridad": r["prioridad"] or "",
            "responsable": r["responsable"] or "",
            "cargado_por": r["usuario_nombre"] or "",
            "creado_en": r["creado_en"] or "",
            "fotos": [{"url": url_for("uploaded_file", filename=f["archivo"]),
                       "nombre": f["archivo"].split("/")[-1].split("_", 1)[-1],
                       "pdf": f["archivo"].lower().endswith(".pdf")} for f in fotos],
            "url_obra": url_for("obra_mantenimiento", obra_id=r["obra_id"]),
        }

    # ------------------------------------------------------------------ página
    @app.route("/mantenimiento")
    @h.cosesa_required
    def mantenimiento_app():
        user = h.get_current_user()
        if not _puede_usar(user):
            flash("No tenés acceso al módulo Mantenimiento. Pedíselo a un administrador.", "error")
            return redirect(url_for("plataforma_inicio"))
        resp = send_file(PANEL_HTML, mimetype="text/html", max_age=0)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ----------------------------------------------------------------- estado
    @app.route("/mantenimiento/api/estado")
    @h.cosesa_required
    def mantenimiento_api_estado():
        user = h.get_current_user()
        if not _puede_usar(user):
            return _err("Sin acceso al módulo Mantenimiento.", 403)
        db = get_db()
        row = db.execute("SELECT * FROM mantenimiento_app_estado WHERE id = 1").fetchone()
        db.close()
        doc = None
        if row:
            try:
                doc = json.loads(row["doc"])
            except ValueError:
                doc = None
        return jsonify({
            "ok": True,
            "doc": doc,
            "version": row["version"] if row else 0,
            "actualizado_por": row["actualizado_por"] if row else None,
            "usuario": {"nombre": _usuario_txt(user), "admin": _puede_admin(user)},
            "urls": {"inicio": url_for("plataforma_inicio"), "logout": url_for("logout")},
        })

    @app.route("/mantenimiento/api/estado", methods=["POST"])
    @h.cosesa_required
    def mantenimiento_api_guardar():
        user = h.get_current_user()
        if not _puede_usar(user):
            return _err("Sin acceso al módulo Mantenimiento.", 403)
        body = request.get_json(silent=True) or {}
        doc = body.get("doc")
        if not isinstance(doc, dict) or not isinstance(doc.get("machines"), list) or not isinstance(doc.get("parts"), list):
            return _err("Datos inválidos.")
        try:
            version = int(body.get("version"))
        except (TypeError, ValueError):
            return _err("Falta la versión.")
        db = get_db()
        row = db.execute("SELECT version, actualizado_por FROM mantenimiento_app_estado WHERE id = 1").fetchone()
        actual = row["version"] if row else 0
        if version != actual:
            cur = db.execute("SELECT doc FROM mantenimiento_app_estado WHERE id = 1").fetchone()
            db.close()
            return _err("Otro usuario guardó cambios antes.", 409, version=actual,
                        actualizado_por=row["actualizado_por"] if row else None,
                        doc=json.loads(cur["doc"]) if cur else None)
        texto = json.dumps(doc, ensure_ascii=False)
        if row:
            db.execute(
                "UPDATE mantenimiento_app_estado SET doc = ?, version = version + 1, "
                "actualizado_en = datetime('now'), actualizado_por = ? WHERE id = 1",
                (texto, _usuario_txt(user)))
        else:
            db.execute(
                "INSERT INTO mantenimiento_app_estado (id, doc, version, actualizado_por) VALUES (1, ?, 1, ?)",
                (texto, _usuario_txt(user)))
        db.commit()
        nueva = db.execute("SELECT version FROM mantenimiento_app_estado WHERE id = 1").fetchone()["version"]
        db.close()
        return jsonify({"ok": True, "version": nueva})

    # ------------------------------------------------------------ solicitudes
    @app.route("/mantenimiento/api/solicitudes")
    @h.cosesa_required
    def mantenimiento_api_solicitudes():
        """Registros de mantenimiento cargados en las obras (todas)."""
        user = h.get_current_user()
        if not _puede_usar(user):
            return _err("Sin acceso al módulo Mantenimiento.", 403)
        db = get_db()
        rows = db.execute(
            "SELECT m.*, o.nombre AS obra_nombre, o.codigo AS obra_codigo, c.nombre AS cliente_nombre, "
            "u.nombre AS usuario_nombre "
            "FROM mantenimientos m JOIN obras o ON o.id = m.obra_id JOIN clientes c ON c.id = o.cliente_id "
            "LEFT JOIN usuarios u ON u.id = m.usuario_id "
            "ORDER BY CASE m.estado WHEN 'pendiente' THEN 0 ELSE 1 END, "
            "CASE m.prioridad WHEN 'alta' THEN 0 WHEN 'media' THEN 1 WHEN 'baja' THEN 2 ELSE 3 END, "
            "m.fecha DESC, m.id DESC"
        ).fetchall()
        fotos = {}
        for f in db.execute("SELECT * FROM mantenimiento_fotos ORDER BY id").fetchall():
            fotos.setdefault(f["mantenimiento_id"], []).append(f)
        lista = [_solicitud_json(r, fotos.get(r["id"], [])) for r in rows]
        db.close()
        return jsonify({"ok": True, "solicitudes": lista})

    @app.route("/mantenimiento/api/solicitudes/<int:mid>/estado", methods=["POST"])
    @h.cosesa_required
    def mantenimiento_api_solicitud_estado(mid):
        """Marca el registro de la obra como realizado o pendiente (lo usa la
        orden de mantenimiento al cerrarse o al anularse)."""
        user = h.get_current_user()
        if not _puede_usar(user):
            return _err("Sin acceso al módulo Mantenimiento.", 403)
        body = request.get_json(silent=True) or {}
        estado = body.get("estado")
        if estado not in ("realizado", "pendiente"):
            return _err("Estado inválido.")
        db = get_db()
        m = db.execute("SELECT * FROM mantenimientos WHERE id = ?", (mid,)).fetchone()
        if m is None:
            db.close()
            return _err("El registro ya no existe en la obra.", 404)
        fecha = (body.get("fecha") or "").strip()[:10] or m["fecha"]
        responsable = (body.get("responsable") or "").strip() or m["responsable"]
        db.execute("UPDATE mantenimientos SET estado = ?, fecha = ?, responsable = ? WHERE id = ?",
                   (estado, fecha, responsable, mid))
        db.commit()
        db.close()
        return jsonify({"ok": True})


def init_easytrack(app, h):
    """Rutas de Easytrack: leer un reporte exportado, ver el estado de la
    sincronización automática y forzar una sincronización ahora."""
    import easytrack_sync

    def _admin(user):
        return h.is_admin(user) or h.puede("modulo_mantenimiento_admin", user)

    @app.route("/mantenimiento/api/easytrack/leer", methods=["POST"])
    @h.cosesa_required
    def mantenimiento_easytrack_leer():
        user = h.get_current_user()
        if not _admin(user):
            return jsonify({"ok": False, "error": "Solo quien administra Mantenimiento puede importar."}), 403
        f = request.files.get("archivo")
        if not f:
            return jsonify({"ok": False, "error": "Elegí el archivo exportado de Easytrack."}), 400
        contenido = f.read()
        if len(contenido) > 15 * 1024 * 1024:
            return jsonify({"ok": False, "error": "El archivo es demasiado grande."}), 400
        try:
            registros, adv = easytrack_sync.leer_archivo(f.filename, contenido)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "registros": registros, "advertencias": adv})

    @app.route("/mantenimiento/api/easytrack/estado")
    @h.cosesa_required
    def mantenimiento_easytrack_estado():
        if not h.puede("modulo_mantenimiento", h.get_current_user()):
            return jsonify({"ok": False}), 403
        return jsonify({"ok": True, **easytrack_sync.estado()})

    @app.route("/mantenimiento/api/easytrack/sincronizar", methods=["POST"])
    @h.cosesa_required
    def mantenimiento_easytrack_sincronizar():
        user = h.get_current_user()
        if not _admin(user):
            return jsonify({"ok": False, "error": "Solo quien administra Mantenimiento puede sincronizar."}), 403
        ok, msg, _res = easytrack_sync.sincronizar(origen=user["nombre"] or user["username"])
        return jsonify({"ok": ok, "mensaje": msg}), (200 if ok else 400)
