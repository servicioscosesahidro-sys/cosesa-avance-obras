"""Módulo "Compras" de la Plataforma COSESA.

Solicitudes de compra, órdenes de compra (OC), recepción en depósito con
remitos/facturas y gastos por centro de costos. La pantalla es una sola página
(templates/compras_app.html) que lee y guarda documentos JSON:

- maestros/<tipo>-<n>: proveedores, artículos, centros de costos, depósitos,
  compradores, formas de pago (partidos en bloques de hasta 450).
- solicitudes/<id>: solicitudes de compra.
- ordenes/<OC0000077>: órdenes de compra (el id es el número).

Cada documento tiene una versión: si dos personas guardan el mismo documento
a la vez, el segundo guardado se rechaza (409) y la pantalla recarga lo último,
así nunca se pisan cambios (por ejemplo un remito cargado en depósito mientras
el comprador edita la OC). Cada cambio suma un número de secuencia global, y la
pantalla pide solo lo que cambió desde la última vez.

El número de OC lo reserva el servidor dentro de una transacción, así nunca se
repite. Aprobar solicitudes y OC exige el permiso "modulo_compras_aprobar" y la
firma se toma siempre del usuario que inició sesión.

Todo vive en la misma base SQLite, así que entra en los backups a GitHub.
Se registra desde app.py con init_app(app, helpers).
"""
import json
import os
import re

from flask import flash, jsonify, redirect, request, send_file, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_HTML = os.path.join(BASE_DIR, "templates", "compras_app.html")

PRIMER_NUMERO_OC = 77          # la primera OC real es la OC0000077
COLECCIONES = ("maestros", "solicitudes", "ordenes")
# Datos maestros que solo cambia un administrador del módulo (los proveedores y
# artículos los puede agregar cualquier usuario de Compras mientras arma una OC).
MAESTROS_ADMIN = ("centros", "depositos", "compradores", "formasPago", "aprobadores")

SCHEMA = """
CREATE TABLE IF NOT EXISTS compras_docs (
    col TEXT NOT NULL,
    id TEXT NOT NULL,
    data TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    seq INTEGER NOT NULL,
    borrado INTEGER NOT NULL DEFAULT 0,
    actualizado_en TEXT DEFAULT (datetime('now')),
    actualizado_por TEXT,
    PRIMARY KEY (col, id)
);
CREATE INDEX IF NOT EXISTS idx_compras_docs_seq ON compras_docs(seq);
CREATE TABLE IF NOT EXISTS compras_meta (
    clave TEXT PRIMARY KEY,
    valor TEXT
);
"""


def migrate(conn):
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO compras_meta (clave, valor) VALUES ('seq', '0')")
    conn.execute("INSERT OR IGNORE INTO compras_meta (clave, valor) VALUES ('prox_oc', ?)", (str(PRIMER_NUMERO_OC),))


def _oc_num(n):
    return "OC" + str(n).zfill(7)


def init_app(app, h):
    """h: helpers de app.py (get_db, get_current_user, cosesa_required, puede, is_admin)."""
    get_db = h.get_db

    def _err(msg, code=400, **extra):
        return jsonify({"ok": False, "error": msg, **extra}), code

    def _nombre(user):
        return user["nombre"] or user["username"]

    def _usa(user):
        return h.puede("modulo_compras", user)

    def _aprueba(user):
        return h.is_admin(user) or h.puede("modulo_compras_aprobar", user)

    def _admin(user):
        return h.is_admin(user) or h.puede("modulo_compras_admin", user)

    def _meta(db, clave, defecto="0"):
        r = db.execute("SELECT valor FROM compras_meta WHERE clave = ?", (clave,)).fetchone()
        return r["valor"] if r else defecto

    def _set_meta(db, clave, valor):
        db.execute("INSERT INTO compras_meta (clave, valor) VALUES (?, ?) "
                   "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (clave, str(valor)))

    def _nuevo_seq(db):
        n = int(_meta(db, "seq", "0")) + 1
        _set_meta(db, "seq", n)
        return n

    def _valida(col, did):
        return col in COLECCIONES and isinstance(did, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", did or "")

    # ------------------------------------------------------------------ página
    @app.route("/compras")
    @h.cosesa_required
    def compras_app():
        user = h.get_current_user()
        if not _usa(user):
            flash("No tenés acceso al módulo Compras. Pedíselo a un administrador.", "error")
            return redirect(url_for("plataforma_inicio"))
        resp = send_file(PANEL_HTML, mimetype="text/html", max_age=0)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------------ datos
    @app.route("/compras/api/datos")
    @h.cosesa_required
    def compras_api_datos():
        """Documentos cambiados desde 'desde' (0 = todos). Incluye los borrados
        para que la pantalla los saque."""
        user = h.get_current_user()
        if not _usa(user):
            return _err("Sin acceso al módulo Compras.", 403)
        try:
            desde = int(request.args.get("desde", "0"))
        except ValueError:
            desde = 0
        db = get_db()
        if desde <= 0:
            rows = db.execute("SELECT col, id, data, version, seq, borrado FROM compras_docs WHERE borrado = 0").fetchall()
        else:
            rows = db.execute("SELECT col, id, data, version, seq, borrado FROM compras_docs WHERE seq > ?", (desde,)).fetchall()
        seq = int(_meta(db, "seq", "0"))
        prox = int(_meta(db, "prox_oc", str(PRIMER_NUMERO_OC)))
        db.close()
        docs = []
        for r in rows:
            d = None
            if not r["borrado"]:
                try:
                    d = json.loads(r["data"])
                except ValueError:
                    continue
            docs.append({"col": r["col"], "id": r["id"], "data": d, "v": r["version"], "borrado": bool(r["borrado"])})
        return jsonify({
            "ok": True, "seq": seq, "completo": desde <= 0, "docs": docs, "prox_oc": prox,
            "usuario": {"nombre": _nombre(user), "username": user["username"],
                        "aprobar": _aprueba(user), "admin": _admin(user)},
            "urls": {"inicio": url_for("plataforma_inicio"), "logout": url_for("logout")},
        })

    def _leer(db, col, did):
        return db.execute("SELECT data, version, borrado FROM compras_docs WHERE col = ? AND id = ?", (col, did)).fetchone()

    @app.route("/compras/api/doc", methods=["POST"])
    @h.cosesa_required
    def compras_api_guardar():
        user = h.get_current_user()
        if not _usa(user):
            return _err("Sin acceso al módulo Compras.", 403)
        body = request.get_json(silent=True) or {}
        col, did, data, v = body.get("col"), body.get("id"), body.get("data"), body.get("v")
        if not _valida(col, did) or not isinstance(data, dict):
            return _err("Datos inválidos.")
        if col == "maestros" and did.split("-")[0] in MAESTROS_ADMIN and not _admin(user):
            return _err("Solo un administrador de Compras puede cambiar " + did.split("-")[0] + ".", 403)
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = _leer(db, col, did)
            actual = row["version"] if row and not row["borrado"] else 0
            if v is not None and int(v) != actual:
                db.rollback()
                return _err("Otra persona guardó cambios en este documento recién. Se recargó lo último: revisalo y volvé a intentar.", 409)
            viejo = {}
            if row and not row["borrado"]:
                try:
                    viejo = json.loads(row["data"])
                except ValueError:
                    viejo = {}
            # Firmas: solo con permiso, y siempre con el usuario que inició sesión
            if col == "ordenes":
                if json.dumps(viejo.get("aprobacion"), sort_keys=True) != json.dumps(data.get("aprobacion"), sort_keys=True):
                    if data.get("aprobacion"):
                        if not _aprueba(user):
                            db.rollback()
                            return _err("No tenés permiso para aprobar órdenes de compra.", 403)
                        data["aprobacion"] = {"userId": user["username"], "nombre": _nombre(user),
                                              "fecha": str(data["aprobacion"].get("fecha") or "")[:16]}
                    elif viejo.get("aprobacion") and viejo["aprobacion"].get("userId") != user["username"] \
                            and not _aprueba(user) and not data.get("_quitarPorEdicion"):
                        db.rollback()
                        return _err("No tenés permiso para quitar la aprobación.", 403)
                data.pop("_quitarPorEdicion", None)
            if col == "solicitudes":
                if json.dumps(viejo.get("decision"), sort_keys=True) != json.dumps(data.get("decision"), sort_keys=True) \
                        and data.get("decision"):
                    if not _aprueba(user):
                        db.rollback()
                        return _err("No tenés permiso para aprobar o rechazar solicitudes.", 403)
                    data["decision"].update({"usuario": user["username"], "nombre": _nombre(user)})
            seq = _nuevo_seq(db)
            texto = json.dumps(data, ensure_ascii=False)
            if row:
                db.execute("UPDATE compras_docs SET data = ?, version = ?, seq = ?, borrado = 0, "
                           "actualizado_en = datetime('now'), actualizado_por = ? WHERE col = ? AND id = ?",
                           (texto, actual + 1, seq, _nombre(user), col, did))
            else:
                db.execute("INSERT INTO compras_docs (col, id, data, version, seq, actualizado_por) VALUES (?, ?, ?, 1, ?, ?)",
                           (col, did, texto, seq, _nombre(user)))
            db.commit()
            return jsonify({"ok": True, "v": actual + 1, "seq": seq, "data": data})
        finally:
            db.close()

    @app.route("/compras/api/borrar", methods=["POST"])
    @h.cosesa_required
    def compras_api_borrar():
        user = h.get_current_user()
        if not _usa(user):
            return _err("Sin acceso al módulo Compras.", 403)
        body = request.get_json(silent=True) or {}
        col, did = body.get("col"), body.get("id")
        if not _valida(col, did):
            return _err("Datos inválidos.")
        if col == "ordenes":
            return _err("Las órdenes de compra no se borran: se anulan.", 403)
        if col == "maestros" and did.split("-")[0] in MAESTROS_ADMIN and not _admin(user):
            return _err("Solo un administrador de Compras puede cambiar " + did.split("-")[0] + ".", 403)
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = _leer(db, col, did)
            if row and not row["borrado"]:
                seq = _nuevo_seq(db)
                db.execute("UPDATE compras_docs SET borrado = 1, data = '{}', version = version + 1, seq = ?, "
                           "actualizado_en = datetime('now'), actualizado_por = ? WHERE col = ? AND id = ?",
                           (seq, _nombre(user), col, did))
            db.commit()
            return jsonify({"ok": True})
        finally:
            db.close()

    # ------------------------------------------------------------ numeración
    @app.route("/compras/api/oc-numero", methods=["POST"])
    @h.cosesa_required
    def compras_api_numero():
        """Reserva el próximo número de OC (nunca se repite, aunque dos
        personas emitan a la vez)."""
        user = h.get_current_user()
        if not _usa(user):
            return _err("Sin acceso al módulo Compras.", 403)
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            n = int(_meta(db, "prox_oc", str(PRIMER_NUMERO_OC)))
            while db.execute("SELECT 1 FROM compras_docs WHERE col = 'ordenes' AND id = ?", (_oc_num(n),)).fetchone():
                n += 1
            _set_meta(db, "prox_oc", n + 1)
            db.commit()
            return jsonify({"ok": True, "n": n, "numero": _oc_num(n)})
        finally:
            db.close()

    @app.route("/compras/api/oc-numero/proximo", methods=["POST"])
    @h.cosesa_required
    def compras_api_numero_proximo():
        user = h.get_current_user()
        if not _admin(user):
            return _err("Solo un administrador de Compras puede cambiar la numeración.", 403)
        try:
            n = int((request.get_json(silent=True) or {}).get("prox"))
        except (TypeError, ValueError):
            return _err("Número inválido.")
        if n < 1:
            return _err("Número inválido.")
        db = get_db()
        try:
            if db.execute("SELECT 1 FROM compras_docs WHERE col = 'ordenes' AND id = ?", (_oc_num(n),)).fetchone():
                return _err(_oc_num(n) + " ya existe.")
            _set_meta(db, "prox_oc", n)
            _nuevo_seq(db)
            db.commit()
            return jsonify({"ok": True, "prox_oc": n})
        finally:
            db.close()
