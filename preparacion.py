"""Módulo "Preparación de obra" (ex Panel de Obras COSESA).

Trabaja sobre las MISMAS obras y clientes que Avance de obras: no hay una
lista de obras aparte. Cada obra puede tener su preparación (checklist de 12
etapas, equipos y personal). Cuando está lista, con "Pasar a ejecución" la
obra pasa de 'planificada' a 'en_curso' y se sigue el avance en la ficha de
siempre.

Todo se guarda en la misma base SQLite (tablas preparacion, catalogo_equipos y
obra_equipos), así que entra solo en los backups automáticos a GitHub.

Se registra desde app.py con init_app(app, helpers).
"""
import json
import os
import re
import unicodedata
from datetime import date

from flask import abort, jsonify, redirect, render_template, request, send_file, url_for, flash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_HTML = os.path.join(BASE_DIR, "templates", "preparacion_panel.html")

# Permiso nuevo para operadores (los administradores pueden todo).
PERMISO = ("preparacion", "Ver y editar la preparación de obra (checklist, equipos y personal)",
           "Preparación de obra", True)

ESTADOS_OBRA = ("planificada", "en_curso", "finalizada")


def _norm(texto):
    """Texto normalizado para comparar nombres: minúsculas, sin acentos ni símbolos."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _json(valor, defecto):
    try:
        v = json.loads(valor) if valor else defecto
        return v if isinstance(v, type(defecto)) else defecto
    except (ValueError, TypeError):
        return defecto


def init_app(app, h):
    """h: helpers de app.py (get_db, get_current_user, cosesa_required, admin_required,
    puede, obras_visibles_ids, ve_todas_las_obras, is_admin)."""

    get_db = h.get_db

    # ------------------------------------------------------------------ datos
    def _obra_json(db, row, prep):
        equipos = [r["equipo_id"] for r in db.execute(
            "SELECT equipo_id FROM obra_equipos WHERE obra_id = ?", (row["id"],)).fetchall()]
        fecha = (row["fecha_inicio"] or "")[:10]
        return {
            "uid": f"obra{row['id']}",
            "obra_id": row["id"],
            "id": row["codigo"] or "",
            "nombre": row["nombre"],
            "cliente": row["cliente_nombre"],
            "fecha": fecha,
            "estado_obra": row["estado"],
            "en_preparacion": prep is not None,
            "responsable": (prep["responsable"] if prep else "") or "",
            "fases": _json(prep["fases"], []) if prep else [],
            "recursos": _json(prep["recursos"], {}) if prep else {},
            "equipos": equipos,
            "version": prep["version"] if prep else 0,
            "actualizado_en": prep["actualizado_en"] if prep else None,
            "actualizado_por": prep["actualizado_por"] if prep else None,
            "n_items": row["n_items"],
            "url_avance": url_for("cosesa_obra_detail", obra_id=row["id"]),
        }

    def _obras_sql(where_extra="", params=()):
        return (
            "SELECT o.*, c.nombre AS cliente_nombre, "
            "(SELECT COUNT(*) FROM items i WHERE i.obra_id = o.id) AS n_items "
            "FROM obras o JOIN clientes c ON c.id = o.cliente_id WHERE 1=1" + where_extra
        ), params

    def _filtro_visibles(db, user):
        ids = h.obras_visibles_ids(db, user)
        if ids is None:
            return ""
        if not ids:
            return " AND 0"
        return f" AND o.id IN ({','.join(str(int(i)) for i in ids)})"

    def _catalogo(db):
        return [dict(r) for r in db.execute(
            "SELECT id, nombre, tipo, orden FROM catalogo_equipos ORDER BY orden, nombre").fetchall()]

    def _cliente_id(db, nombre):
        nombre = (nombre or "").strip()
        if not nombre:
            return None
        for c in db.execute("SELECT id, nombre FROM clientes").fetchall():
            if _norm(c["nombre"]) == _norm(nombre):
                return c["id"]
        return db.execute("INSERT INTO clientes (nombre) VALUES (?)", (nombre,)).lastrowid

    def _usuario_txt(user):
        return user["nombre"] or user["username"]

    def _puede_editar(user):
        return h.puede("preparacion", user)

    def _err(msg, code=400, **extra):
        return jsonify({"ok": False, "error": msg, **extra}), code

    def _guardar_equipos_obra(db, obra_id, equipos):
        validos = {r["id"] for r in db.execute("SELECT id FROM catalogo_equipos").fetchall()}
        db.execute("DELETE FROM obra_equipos WHERE obra_id = ?", (obra_id,))
        for eid in dict.fromkeys(equipos or []):
            if eid in validos:
                db.execute("INSERT OR IGNORE INTO obra_equipos (obra_id, equipo_id) VALUES (?, ?)", (obra_id, eid))

    def _obra_completa(db, obra_id):
        sql, params = _obras_sql(" AND o.id = ?", (obra_id,))
        row = db.execute(sql, params).fetchone()
        if row is None:
            return None
        prep = db.execute("SELECT * FROM preparacion WHERE obra_id = ?", (obra_id,)).fetchone()
        return _obra_json(db, row, prep)

    # ------------------------------------------------------------------ página
    @app.route("/cosesa/preparacion")
    @h.cosesa_required
    def preparacion_panel():
        user = h.get_current_user()
        if not _puede_editar(user):
            flash("No tenés permiso para ver la preparación de obra. Pedíselo a un administrador.", "error")
            return redirect(url_for("cosesa_dashboard"))
        resp = send_file(PANEL_HTML, mimetype="text/html", max_age=0)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/cosesa/preparacion/api/estado")
    @h.cosesa_required
    def preparacion_api_estado():
        user = h.get_current_user()
        if not _puede_editar(user):
            return _err("Sin permiso para la preparación de obra.", 403)
        db = get_db()
        filtro = _filtro_visibles(db, user)
        # Obras que están en preparación (tienen checklist)
        sql, params = _obras_sql(" AND o.id IN (SELECT obra_id FROM preparacion)" + filtro)
        rows = db.execute(sql + " ORDER BY o.fecha_inicio DESC, o.id DESC", params).fetchall()
        preps = {r["obra_id"]: r for r in db.execute("SELECT * FROM preparacion").fetchall()}
        obras = [_obra_json(db, r, preps.get(r["id"])) for r in rows]
        # Obras de Avance que no están en preparación (para poder sumarlas sin duplicarlas)
        sql2, p2 = _obras_sql(" AND o.id NOT IN (SELECT obra_id FROM preparacion)" + filtro)
        disponibles = [
            {"obra_id": r["id"], "nombre": r["nombre"], "cliente": r["cliente_nombre"],
             "estado_obra": r["estado"], "codigo": r["codigo"] or ""}
            for r in db.execute(sql2 + " ORDER BY c.nombre, o.nombre", p2).fetchall()
        ]
        clientes = [r["nombre"] for r in db.execute("SELECT nombre FROM clientes ORDER BY nombre").fetchall()]
        data = {
            "ok": True,
            "obras": obras,
            "equipos": _catalogo(db),
            "clientes": clientes,
            "obras_disponibles": disponibles,
            "usuario": {
                "nombre": _usuario_txt(user),
                "admin": h.is_admin(user),
                "puede_crear_obras": h.puede("crear_obras", user),
                "puede_ejecutar": h.puede("editar_obra", user),
                "puede_eliminar_obras": h.puede("eliminar_obra", user),
            },
            "urls": {
                "avance": url_for("cosesa_dashboard"),
                "logout": url_for("logout"),
                "importar": url_for("preparacion_importar") if h.is_admin(user) else None,
            },
        }
        db.close()
        return jsonify(data)

    # ------------------------------------------------------------- guardar obra
    @app.route("/cosesa/preparacion/api/obras/<int:obra_id>", methods=["POST"])
    @h.cosesa_required
    def preparacion_guardar_obra(obra_id):
        user = h.get_current_user()
        if not _puede_editar(user):
            return _err("Sin permiso para la preparación de obra.", 403)
        body = request.get_json(silent=True) or {}
        db = get_db()
        obra = db.execute("SELECT * FROM obras WHERE id = ?", (obra_id,)).fetchone()
        if obra is None:
            db.close()
            return _err("La obra ya no existe.", 404)
        prep = db.execute("SELECT * FROM preparacion WHERE obra_id = ?", (obra_id,)).fetchone()
        version_actual = prep["version"] if prep else 0
        if int(body.get("version", -1)) != version_actual:
            actual = _obra_completa(db, obra_id)
            db.close()
            return _err("Otro usuario modificó esta obra mientras la editabas.", 409, obra=actual)

        # Datos generales de la obra (son los mismos que ve Avance de obras)
        nombre = (body.get("nombre") or "").strip() or obra["nombre"]
        cliente_id = _cliente_id(db, body.get("cliente")) or obra["cliente_id"]
        codigo = (body.get("id") or "").strip() or None
        fecha = (body.get("fecha") or "").strip()
        fecha_inicio = obra["fecha_inicio"]
        if fecha and fecha != (obra["fecha_inicio"] or "")[:10]:
            fecha_inicio = fecha
        elif not fecha:
            fecha_inicio = None
        db.execute(
            "UPDATE obras SET nombre = ?, cliente_id = ?, codigo = ?, fecha_inicio = ? WHERE id = ?",
            (nombre, cliente_id, codigo, fecha_inicio, obra_id),
        )
        fases = json.dumps(body.get("fases") or [], ensure_ascii=False)
        recursos = json.dumps(body.get("recursos") or {}, ensure_ascii=False)
        responsable = (body.get("responsable") or "").strip()
        if prep:
            db.execute(
                "UPDATE preparacion SET responsable = ?, fases = ?, recursos = ?, version = version + 1, "
                "actualizado_en = datetime('now'), actualizado_por = ? WHERE obra_id = ?",
                (responsable, fases, recursos, _usuario_txt(user), obra_id),
            )
        else:
            db.execute(
                "INSERT INTO preparacion (obra_id, responsable, fases, recursos, actualizado_por) "
                "VALUES (?, ?, ?, ?, ?)",
                (obra_id, responsable, fases, recursos, _usuario_txt(user)),
            )
        _guardar_equipos_obra(db, obra_id, body.get("equipos"))
        db.commit()
        actual = _obra_completa(db, obra_id)
        db.close()
        return jsonify({"ok": True, "obra": actual})

    # --------------------------------------------------------------- crear obra
    @app.route("/cosesa/preparacion/api/obras", methods=["POST"])
    @h.cosesa_required
    def preparacion_crear_obra():
        user = h.get_current_user()
        if not _puede_editar(user):
            return _err("Sin permiso para la preparación de obra.", 403)
        body = request.get_json(silent=True) or {}
        db = get_db()
        vincular = body.get("vincular_obra_id")
        if vincular:
            # Sumar a preparación una obra que ya existe en Avance de obras
            obra_id = int(vincular)
            visibles = h.obras_visibles_ids(db, user)
            existe = db.execute("SELECT id FROM obras WHERE id = ?", (obra_id,)).fetchone()
            if existe is None or (visibles is not None and obra_id not in visibles):
                db.close()
                return _err("No se encontró la obra.", 404)
            if db.execute("SELECT 1 FROM preparacion WHERE obra_id = ?", (obra_id,)).fetchone():
                db.close()
                return _err("Esa obra ya está en preparación.", 409)
        else:
            if not h.puede("crear_obras", user):
                db.close()
                return _err("No tenés permiso para crear obras.", 403)
            nombre = (body.get("nombre") or "").strip()
            cliente = (body.get("cliente") or "").strip()
            if not nombre or not cliente:
                db.close()
                return _err("Nombre de la obra y cliente son obligatorios.")
            cliente_id = _cliente_id(db, cliente)
            obra_id = db.execute(
                "INSERT INTO obras (cliente_id, nombre, codigo, fecha_inicio, estado) VALUES (?, ?, ?, ?, 'planificada')",
                (cliente_id, nombre, (body.get("id") or "").strip() or None, (body.get("fecha") or "").strip() or None),
            ).lastrowid
            if not h.ve_todas_las_obras(user):
                db.execute("INSERT OR IGNORE INTO usuario_obras (usuario_id, obra_id) VALUES (?, ?)", (user["id"], obra_id))
        db.execute(
            "INSERT INTO preparacion (obra_id, responsable, fases, recursos, actualizado_por) VALUES (?, ?, ?, ?, ?)",
            (obra_id, (body.get("responsable") or "").strip(),
             json.dumps(body.get("fases") or [], ensure_ascii=False),
             json.dumps(body.get("recursos") or {}, ensure_ascii=False), _usuario_txt(user)),
        )
        db.commit()
        obra = _obra_completa(db, obra_id)
        db.close()
        return jsonify({"ok": True, "obra": obra})

    # ------------------------------------------------- quitar de preparación
    @app.route("/cosesa/preparacion/api/obras/<int:obra_id>/quitar", methods=["POST"])
    @h.cosesa_required
    def preparacion_quitar_obra(obra_id):
        """Quita la obra del tablero de preparación. Solo se borra la obra
        entera si se creó para preparación (planificada, sin tareas de avance)
        y el usuario puede eliminar obras; si no, la obra sigue en Avance."""
        user = h.get_current_user()
        if not _puede_editar(user):
            return _err("Sin permiso para la preparación de obra.", 403)
        db = get_db()
        obra = db.execute(
            "SELECT o.*, (SELECT COUNT(*) FROM items i WHERE i.obra_id = o.id) AS n_items FROM obras o WHERE o.id = ?",
            (obra_id,)).fetchone()
        if obra is None:
            db.close()
            return jsonify({"ok": True, "eliminada": True})
        borrar_obra = obra["estado"] == "planificada" and obra["n_items"] == 0 and h.puede("eliminar_obra", user)
        if borrar_obra:
            db.execute("DELETE FROM obras WHERE id = ?", (obra_id,))
        else:
            db.execute("DELETE FROM preparacion WHERE obra_id = ?", (obra_id,))
            db.execute("DELETE FROM obra_equipos WHERE obra_id = ?", (obra_id,))
        db.commit()
        db.close()
        return jsonify({"ok": True, "eliminada": borrar_obra})

    # ------------------------------------------------------ pasar a ejecución
    @app.route("/cosesa/preparacion/api/obras/<int:obra_id>/ejecutar", methods=["POST"])
    @h.cosesa_required
    def preparacion_pasar_a_ejecucion(obra_id):
        user = h.get_current_user()
        if not h.puede("editar_obra", user):
            return _err("No tenés permiso para pasar obras a ejecución.", 403)
        db = get_db()
        obra = db.execute("SELECT * FROM obras WHERE id = ?", (obra_id,)).fetchone()
        if obra is None:
            db.close()
            return _err("La obra ya no existe.", 404)
        fecha_inicio = obra["fecha_inicio"] or date.today().isoformat()
        db.execute("UPDATE obras SET estado = 'en_curso', fecha_inicio = ? WHERE id = ?", (fecha_inicio, obra_id))
        db.commit()
        obra_json = _obra_completa(db, obra_id)
        db.close()
        return jsonify({"ok": True, "obra": obra_json, "url": url_for("cosesa_obra_detail", obra_id=obra_id)})

    # ---------------------------------------------------- catálogo de equipos
    @app.route("/cosesa/preparacion/api/equipos", methods=["POST"])
    @h.cosesa_required
    def preparacion_guardar_equipos():
        """Reemplaza el catálogo completo de equipos (agregar, renombrar, borrar)."""
        user = h.get_current_user()
        if not _puede_editar(user):
            return _err("Sin permiso para la preparación de obra.", 403)
        body = request.get_json(silent=True) or {}
        lista = body.get("equipos")
        if not isinstance(lista, list):
            return _err("Formato inválido.")
        db = get_db()
        vistos, nombres = set(), set()
        for pos, e in enumerate(lista, start=1):
            eid = str(e.get("id") or "").strip()[:60]
            nombre = str(e.get("nombre") or "").strip()[:40]
            if not eid or not nombre or eid in vistos or _norm(nombre) in nombres:
                continue
            vistos.add(eid)
            nombres.add(_norm(nombre))
            tipo = str(e.get("tipo") or "").strip()[:60] or None
            db.execute(
                "INSERT INTO catalogo_equipos (id, nombre, tipo, orden) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET nombre = excluded.nombre, tipo = excluded.tipo, orden = excluded.orden",
                (eid, nombre, tipo, pos),
            )
        existentes = [r["id"] for r in db.execute("SELECT id FROM catalogo_equipos").fetchall()]
        for eid in existentes:
            if eid not in vistos:
                db.execute("DELETE FROM obra_equipos WHERE equipo_id = ?", (eid,))
                db.execute("DELETE FROM catalogo_equipos WHERE id = ?", (eid,))
        db.commit()
        cat = _catalogo(db)
        db.close()
        return jsonify({"ok": True, "equipos": cat})

    # -------------------------------------------------- importar panel viejo
    @app.route("/cosesa/preparacion/importar", methods=["GET", "POST"])
    @h.admin_required
    def preparacion_importar():
        """Importa las obras del Panel de Obras COSESA anterior (archivo JSON),
        vinculando cada una con una obra existente o creándola como planificada."""
        db = get_db()
        obras_existentes = db.execute(
            "SELECT o.id, o.nombre, o.codigo, o.estado, c.nombre AS cliente_nombre, "
            "(SELECT 1 FROM preparacion p WHERE p.obra_id = o.id) AS tiene_prep "
            "FROM obras o JOIN clientes c ON c.id = o.cliente_id ORDER BY c.nombre, o.nombre"
        ).fetchall()

        if request.method == "POST" and request.form.get("paso") == "confirmar":
            try:
                datos = json.loads(request.form.get("datos") or "{}")
            except ValueError:
                db.close()
                flash("No se pudo leer el archivo. Volvé a subirlo.", "error")
                return redirect(url_for("preparacion_importar"))
            panel_obras = datos.get("obras") or []
            destinos = [request.form.get(f"destino_{i}", "saltear") for i in range(len(panel_obras))]
            repetidos = {d for d in destinos if d not in ("nueva", "saltear") and destinos.count(d) > 1}
            if repetidos:
                nombres = ", ".join(o["nombre"] for o in obras_existentes if str(o["id"]) in repetidos)
                db.close()
                flash(f"Elegiste la misma obra existente para más de una obra del panel ({nombres}). "
                      "Cada obra existente se puede vincular una sola vez. Volvé a subir el archivo y corregilo.", "error")
                return redirect(url_for("preparacion_importar"))
            # 1) Catálogo de equipos: se suman los que falten (por nombre)
            cat = {_norm(r["nombre"]): r["id"] for r in db.execute("SELECT id, nombre FROM catalogo_equipos").fetchall()}
            orden = db.execute("SELECT COALESCE(MAX(orden),0) FROM catalogo_equipos").fetchone()[0]
            mapa_eq = {}
            for e in datos.get("equipos") or []:
                n = _norm(e.get("nombre"))
                if not n:
                    continue
                if n not in cat:
                    orden += 1
                    nuevo_id = str(e.get("id") or "")[:60] or ("eq_" + n.replace(" ", "_"))
                    if db.execute("SELECT 1 FROM catalogo_equipos WHERE id = ?", (nuevo_id,)).fetchone():
                        nuevo_id = "eq_imp_" + n.replace(" ", "_")
                    db.execute("INSERT INTO catalogo_equipos (id, nombre, tipo, orden) VALUES (?, ?, ?, ?)",
                               (nuevo_id, e.get("nombre").strip(), (e.get("tipo") or "").strip() or None, orden))
                    cat[n] = nuevo_id
                mapa_eq[e.get("id")] = cat[n]
            # 2) Obras
            creadas = vinculadas = salteadas = 0
            for idx, po in enumerate(panel_obras):
                destino = request.form.get(f"destino_{idx}", "saltear")
                if destino == "saltear":
                    salteadas += 1
                    continue
                if destino == "nueva":
                    cliente_id = _cliente_id(db, po.get("cliente") or po.get("nombre"))
                    obra_id = db.execute(
                        "INSERT INTO obras (cliente_id, nombre, codigo, fecha_inicio, estado) VALUES (?, ?, ?, ?, 'planificada')",
                        (cliente_id, (po.get("nombre") or "Obra sin nombre").strip(),
                         (po.get("id") or "").strip() or None, (po.get("fecha") or "").strip() or None),
                    ).lastrowid
                    creadas += 1
                else:
                    obra_id = int(destino)
                    obra = db.execute("SELECT * FROM obras WHERE id = ?", (obra_id,)).fetchone()
                    if obra is None:
                        salteadas += 1
                        continue
                    cambios = {}
                    if not obra["codigo"] and (po.get("id") or "").strip():
                        cambios["codigo"] = po["id"].strip()
                    if not obra["fecha_inicio"] and (po.get("fecha") or "").strip():
                        cambios["fecha_inicio"] = po["fecha"].strip()
                    for col, val in cambios.items():
                        db.execute(f"UPDATE obras SET {col} = ? WHERE id = ?", (val, obra_id))
                    vinculadas += 1
                db.execute("DELETE FROM preparacion WHERE obra_id = ?", (obra_id,))
                db.execute(
                    "INSERT INTO preparacion (obra_id, responsable, fases, recursos, actualizado_por) VALUES (?, ?, ?, ?, ?)",
                    (obra_id, (po.get("responsable") or "").strip(),
                     json.dumps(po.get("fases") or [], ensure_ascii=False),
                     json.dumps(po.get("recursos") or {}, ensure_ascii=False), "Importación"),
                )
                _guardar_equipos_obra(db, obra_id, [mapa_eq.get(x, x) for x in (po.get("equipos") or [])])
            db.commit()
            db.close()
            flash(f"Importación terminada: {creadas} obras nuevas, {vinculadas} vinculadas con obras existentes, "
                  f"{salteadas} salteadas.", "success")
            return redirect(url_for("preparacion_importar", listo=1))

        propuestas, datos_txt, error = None, "", None
        if request.method == "POST":
            archivo = request.files.get("archivo")
            try:
                datos = json.load(archivo) if archivo else None
                if not isinstance(datos, dict) or not isinstance(datos.get("obras"), list):
                    raise ValueError
            except Exception:
                datos, error = None, "El archivo no es una exportación válida del Panel de Obras."
            if datos:
                datos.pop("usuarios", None)  # las contraseñas del panel viejo no se importan
                datos_txt = json.dumps(datos, ensure_ascii=False)
                propuestas, usadas = [], set()
                for po in datos["obras"]:
                    n_obra, n_cli, cod = _norm(po.get("nombre")), _norm(po.get("cliente")), (po.get("id") or "").strip()
                    mejor = None
                    for o in obras_existentes:
                        if o["id"] in usadas:
                            continue
                        if cod and o["codigo"] and o["codigo"].strip() == cod:
                            mejor = o["id"]
                            break
                        if (mejor is None and n_obra and _norm(o["nombre"]) == n_obra
                                and (not n_cli or _norm(o["cliente_nombre"]) == n_cli)):
                            mejor = o["id"]
                    if mejor:
                        usadas.add(mejor)
                    propuestas.append({
                        "nombre": po.get("nombre") or "", "cliente": po.get("cliente") or "",
                        "codigo": cod, "fecha": po.get("fecha") or "", "sugerida": mejor,
                        "n_equipos": len(po.get("equipos") or []),
                        "avance": _avance_panel(po.get("fases") or []),
                    })
        db.close()
        return render_template(
            "preparacion_importar.html", propuestas=propuestas, datos_txt=datos_txt,
            obras_existentes=obras_existentes, error=error, listo=request.args.get("listo") == "1",
        )


def _avance_panel(fases):
    total = ok = 0
    for f in fases:
        for it in f.get("items") or []:
            if it.get("estado") in ("NA", "N/A"):
                continue
            total += 1
            ok += it.get("estado") == "SI"
    return round(100 * ok / total) if total else 0
