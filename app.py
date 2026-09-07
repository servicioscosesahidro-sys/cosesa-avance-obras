import os
import sqlite3
import tempfile
from datetime import date, datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, g,
    send_from_directory, abort, send_file, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from db import get_db, init_db, DB_PATH
from backup import restore_latest, backup_now, iniciar_backup_periodico
from charts import gantt_svg, curve_svg, carga_diaria_planificada_svg, carga_diaria_real_svg
from excel_io import generar_plantilla_cronograma, generar_plantilla_reales, leer_cronograma, leer_reales

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Si se define DATA_DIR (por ejemplo, apuntando a un disco persistente en el
# hosting), las fotos y certificados subidos se guardan ahí en vez de en la
# carpeta del proyecto, igual que la base de datos (ver db.py).
_DATA_DIR = os.environ.get("DATA_DIR")
UPLOAD_DIR = os.path.join(_DATA_DIR, "uploads") if _DATA_DIR else os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
ALLOWED_CERT_EXT = {"pdf", "png", "jpg", "jpeg", "webp"}

CHECKLIST_ESTADOS = ("pendiente", "ok", "na")
CHECKLIST_PUNTOS_DEFAULT = ("Lavado", "Secado")
PIEZAS_SUGERIDAS = ("Tapa", "Tapa olla", "Distribuidor", "Aros de flotante", "Cabezal flotante")
CARGOS_PERSONAL = (
    ("supervisores", "Supervisor"),
    ("tecnicos_hs", "Técnico de Higiene y Seguridad"),
    ("punteros", "Puntero"),
    ("operarios", "Operario"),
)
TURNOS = (("dia", "Turno día"), ("noche", "Turno noche"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cosesa-dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB por request

DT_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def now_local_str():
    """Fecha y hora actuales en el formato que usan los inputs datetime-local."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M")


@app.template_filter("fmtdt")
def fmtdt(value):
    """Muestra una fecha/hora guardada (datetime-local) en formato dd/mm/aaaa hh:mm."""
    if not value:
        return "—"
    for fmt in DT_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            if fmt == "%Y-%m-%d":
                return dt.strftime("%d/%m/%Y")
            return dt.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            continue
    return value


def _parse_dt(value):
    if not value:
        return None
    for fmt in DT_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def fmt_duracion(total_minutos):
    """Da formato legible (ej: '1 d 3 h 20 min') a una duración en minutos."""
    if not total_minutos or total_minutos <= 0:
        return "0 min"
    total_minutos = round(total_minutos)
    dias, resto = divmod(total_minutos, 1440)
    horas, minutos = divmod(resto, 60)
    partes = []
    if dias:
        partes.append(f"{dias} d")
    if horas:
        partes.append(f"{horas} h")
    if minutos or not partes:
        partes.append(f"{minutos} min")
    return " ".join(partes)


def demora_minutos(demora):
    """Minutos que duró la demora. Si todavía está abierta (pausa en curso,
    fecha_fin es NULL), cuenta desde que empezó hasta ahora mismo, así las
    horas de trabajo dejan de sumar mientras el ítem sigue pausado."""
    ini = _parse_dt(demora["fecha_inicio"])
    fin = _parse_dt(demora["fecha_fin"]) if demora["fecha_fin"] else datetime.now()
    if not (ini and fin) or fin <= ini:
        return 0
    return (fin - ini).total_seconds() / 60


@app.template_filter("fmtdur")
def fmtdur_filter(demora):
    return fmt_duracion(demora_minutos(demora))


def get_personal(db, item_id):
    """Devuelve {'dia': fila, 'noche': fila} de personal_lavado, creando las filas
    en 0 si todavía no existen para este ítem."""
    rows = db.execute("SELECT * FROM personal_lavado WHERE item_id = ?", (item_id,)).fetchall()
    by_turno = {r["turno"]: r for r in rows}
    faltantes = [turno for turno, _ in TURNOS if turno not in by_turno]
    if faltantes:
        for turno in faltantes:
            db.execute("INSERT INTO personal_lavado (item_id, turno) VALUES (?, ?)", (item_id, turno))
        db.commit()
        rows = db.execute("SELECT * FROM personal_lavado WHERE item_id = ?", (item_id,)).fetchall()
        by_turno = {r["turno"]: r for r in rows}
    return by_turno


def total_personal(personal_by_turno):
    total_personas = 0
    total_bombas = 0
    for turno, _ in TURNOS:
        row = personal_by_turno.get(turno)
        if row:
            total_personas += row["supervisores"] + row["tecnicos_hs"] + row["punteros"] + row["operarios"]
            total_bombas += row["bombas"]
    return total_personas, total_bombas


def compute_horas_item(item, demoras):
    """Horas totales transcurridas (inicio real -> fin real, o -> ahora si sigue en
    curso), horas de demoras, y horas de trabajo netas (totales menos demoras)."""
    inicio = _parse_dt(item["fecha_inicio"])
    if item["estado"] == "finalizado" and item["fecha_fin"]:
        fin = _parse_dt(item["fecha_fin"]) or datetime.now()
    else:
        fin = datetime.now()
    if not inicio or fin < inicio:
        horas_totales = 0.0
    else:
        horas_totales = (fin - inicio).total_seconds() / 3600
    horas_demoras = sum(demora_minutos(d) for d in demoras) / 60
    horas_demoras = min(horas_demoras, horas_totales)
    horas_trabajo = max(0.0, horas_totales - horas_demoras)
    return {
        "horas_totales": round(horas_totales, 2),
        "horas_demoras": round(horas_demoras, 2),
        "horas_trabajo": round(horas_trabajo, 2),
    }


def recompute_item_horas(db, item_id):
    """Recalcula horas de trabajo netas y las horas-hombre / horas-máquina
    (horas de trabajo x personal / bombas asignados) y las guarda en el ítem."""
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return None
    demoras = db.execute("SELECT * FROM demoras WHERE item_id = ?", (item_id,)).fetchall()
    personal = get_personal(db, item_id)
    horas = compute_horas_item(item, demoras)
    total_personas, total_bombas = total_personal(personal)
    horas_hombre = round(horas["horas_trabajo"] * total_personas, 2)
    horas_maquina = round(horas["horas_trabajo"] * total_bombas, 2)
    db.execute(
        "UPDATE items SET horas_hombre = ?, horas_maquina = ? WHERE id = ?",
        (horas_hombre, horas_maquina, item_id),
    )
    db.commit()
    horas["horas_hombre"] = horas_hombre
    horas["horas_maquina"] = horas_maquina
    horas["total_personas"] = total_personas
    horas["total_bombas"] = total_bombas
    return horas


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pdf_token(user_id):
    """Token de corta duración para que Chromium headless pueda ver páginas protegidas al generar un PDF."""
    s = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="pdf-token")
    return s.dumps(user_id)


def _verify_pdf_token(token):
    s = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="pdf-token")
    try:
        return s.loads(token, max_age=120)
    except (BadSignature, SignatureExpired):
        return None


def get_current_user():
    if not hasattr(g, "_user"):
        user_id = session.get("user_id")
        if user_id is None:
            token = request.args.get("pdf_token")
            if token:
                user_id = _verify_pdf_token(token)
        if user_id is None:
            g._user = None
        else:
            db = get_db()
            g._user = db.execute(
                "SELECT * FROM usuarios WHERE id = ?", (user_id,)
            ).fetchone()
            db.close()
    return g._user


@app.context_processor
def inject_user():
    return {"current_user": get_current_user()}


def login_required(role=None):
    """role puede ser un string ('cosesa_admin') o una tupla/lista de roles permitidos
    (('cosesa', 'cosesa_admin')). Si no se pasa, solo exige estar logueado."""
    if role is not None and isinstance(role, str):
        allowed_roles = (role,)
    elif role is not None:
        allowed_roles = tuple(role)
    else:
        allowed_roles = None

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = get_current_user()
            if user is None:
                return redirect(url_for("login"))
            if allowed_roles and user["rol"] not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


# Roles de "lado COSESA" (operador y administrador) que pueden ver/cargar información.
COSESA_ROLES = ("cosesa", "cosesa_admin")
# Solo el administrador puede crear/borrar obras, ítems, clientes y usuarios.
admin_required = login_required("cosesa_admin")
cosesa_required = login_required(COSESA_ROLES)


def is_admin(user):
    return user is not None and user["rol"] == "cosesa_admin"


def get_obras_asignadas_ids(db, usuario_id):
    """IDs de las obras que un usuario tipo 'cliente' puede ver."""
    rows = db.execute(
        "SELECT obra_id FROM usuario_obras WHERE usuario_id = ?", (usuario_id,)
    ).fetchall()
    return {r["obra_id"] for r in rows}


def set_obras_asignadas(db, usuario_id, obra_ids):
    """Reemplaza por completo el conjunto de obras visibles para un usuario cliente."""
    db.execute("DELETE FROM usuario_obras WHERE usuario_id = ?", (usuario_id,))
    for obra_id in obra_ids:
        db.execute(
            "INSERT OR IGNORE INTO usuario_obras (usuario_id, obra_id) VALUES (?, ?)",
            (usuario_id, obra_id),
        )


def usuario_puede_ver_obra(db, usuario, obra_id):
    if usuario["rol"] in COSESA_ROLES:
        return True
    return db.execute(
        "SELECT 1 FROM usuario_obras WHERE usuario_id = ? AND obra_id = ?",
        (usuario["id"], obra_id),
    ).fetchone() is not None


@app.context_processor
def inject_is_admin():
    return {"is_admin": is_admin}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def allowed_cert_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_CERT_EXT


def save_photo(item_id, file_storage, tipo, comentario, usuario_id):
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        flash("Formato de imagen no permitido.", "error")
        return None
    item_dir = os.path.join(UPLOAD_DIR, str(item_id))
    os.makedirs(item_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
    filename = secure_filename(f"{tipo}_{ts}_{file_storage.filename}")
    file_storage.save(os.path.join(item_dir, filename))
    rel_path = f"{item_id}/{filename}"
    db = get_db()
    db.execute(
        "INSERT INTO fotos (item_id, tipo, filename, comentario, usuario_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, tipo, rel_path, comentario, usuario_id),
    )
    db.commit()
    db.close()
    return rel_path


def save_certificado(obra_id, file_storage, usuario_id):
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_cert_file(file_storage.filename):
        flash("Formato no permitido. Subí un PDF o una imagen (jpg, png, webp).", "error")
        return None
    cert_dir = os.path.join(UPLOAD_DIR, "certificados", str(obra_id))
    os.makedirs(cert_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
    filename = secure_filename(f"{ts}_{file_storage.filename}")
    file_storage.save(os.path.join(cert_dir, filename))
    rel_path = f"certificados/{obra_id}/{filename}"
    db = get_db()
    db.execute(
        "INSERT INTO certificados (obra_id, nombre_original, filename, usuario_id) VALUES (?, ?, ?, ?)",
        (obra_id, file_storage.filename, rel_path, usuario_id),
    )
    db.commit()
    db.close()
    return rel_path


def get_or_create_checklist(db, item_id):
    row = db.execute("SELECT * FROM checklist_lavado WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        numero = f"COSESA-CL-{item_id:05d}"
        db.execute(
            "INSERT INTO checklist_lavado (item_id, numero_reporte) VALUES (?, ?)",
            (item_id, numero),
        )
        for orden, etiqueta in enumerate(CHECKLIST_PUNTOS_DEFAULT, start=1):
            db.execute(
                "INSERT INTO checklist_puntos (item_id, orden, etiqueta) VALUES (?, ?, ?)",
                (item_id, orden, etiqueta),
            )
        db.commit()
        row = db.execute("SELECT * FROM checklist_lavado WHERE item_id = ?", (item_id,)).fetchone()
    return row


def get_checklist_puntos(db, item_id):
    return db.execute(
        "SELECT * FROM checklist_puntos WHERE item_id = ? ORDER BY orden, id", (item_id,)
    ).fetchall()


def build_obra_charts(db, items, interactive=False):
    """Devuelve (gantt_html, curve_html) para una lista de ítems de una obra.
    interactive=True agrega al Gantt un link al ítem y botones −/+ de avance
    rápido (solo tiene sentido en la ficha de la obra para COSESA, no en
    reportes/PDFs de solo lectura)."""
    items_con_avances = []
    items_con_demoras = []
    items_con_bombas = []
    for it in items:
        avances = db.execute(
            "SELECT * FROM avance_log WHERE item_id = ? ORDER BY fecha", (it["id"],)
        ).fetchall()
        items_con_avances.append({"item": it, "avances": avances})
        demoras = db.execute(
            "SELECT * FROM demoras WHERE item_id = ? ORDER BY fecha_inicio", (it["id"],)
        ).fetchall()
        items_con_demoras.append({"item": it, "demoras": demoras})
        personal = get_personal(db, it["id"])
        _, total_bombas = total_personal(personal)
        items_con_bombas.append({"item": it, "bombas": total_bombas})
    gantt_html = gantt_svg(items_con_demoras, interactive=interactive) if items else "<p class='subtitle'>Todavía no hay ítems cargados.</p>"
    curve_html = curve_svg(items_con_avances) if items else "<p class='subtitle'>Todavía no hay ítems cargados.</p>"
    if items:
        carga_plan_html = carga_diaria_planificada_svg(items_con_bombas)
        carga_real_html = carga_diaria_real_svg(items_con_bombas)
    else:
        carga_plan_html = carga_real_html = "<p class='subtitle'>Todavía no hay ítems cargados.</p>"
    return gantt_html, curve_html, carga_plan_html, carga_real_html


def render_pdf(html_url, out_path):
    """Renderiza una URL interna a PDF usando Chromium headless (Playwright para Python).
    Usa el paquete `playwright` de pip directamente, sin depender de Node ni de
    rutas específicas de una máquina — así funciona igual en el entorno de
    desarrollo y en el hosting de producción."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(html_url, wait_until="networkidle")
        page.pdf(
            path=out_path,
            format="A4",
            print_background=True,
            margin={"top": "15mm", "bottom": "15mm", "left": "12mm", "right": "12mm"},
        )
        browser.close()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    user = get_current_user()
    if user is None:
        return redirect(url_for("login"))
    return redirect(url_for("cosesa_dashboard") if user["rol"] in COSESA_ROLES else url_for("cliente_dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM usuarios WHERE username = ?", (username,)).fetchone()
        db.close()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("cosesa_dashboard") if user["rol"] in COSESA_ROLES else url_for("cliente_dashboard"))
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Lado COSESA
# ---------------------------------------------------------------------------

@app.route("/cosesa")
@cosesa_required
def cosesa_dashboard():
    db = get_db()
    clientes = db.execute(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM obras o WHERE o.cliente_id = c.id) AS n_obras "
        "FROM clientes c ORDER BY c.nombre"
    ).fetchall()
    obras_activas = db.execute(
        "SELECT o.*, c.nombre AS cliente_nombre, "
        "(SELECT COUNT(*) FROM items i WHERE i.obra_id = o.id) AS n_items, "
        "(SELECT COALESCE(AVG(avance_pct),0) FROM items i WHERE i.obra_id = o.id) AS avance_prom "
        "FROM obras o JOIN clientes c ON c.id = o.cliente_id "
        "WHERE o.estado != 'finalizada' ORDER BY o.creado_en DESC"
    ).fetchall()
    db.close()
    return render_template(
        "cosesa_dashboard.html", clientes=clientes, obras_activas=obras_activas,
    )


@app.route("/cosesa/usuarios/nuevo", methods=["POST"])
@admin_required
def crear_usuario_cosesa():
    nombre = request.form.get("nombre", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    rol = request.form.get("rol", "").strip()
    if rol not in ("cosesa", "cosesa_admin"):
        flash("Rol inválido.", "error")
        return redirect(url_for("cosesa_usuarios"))
    if not (nombre and username and password):
        flash("Completá nombre, usuario y contraseña.", "error")
        return redirect(url_for("cosesa_usuarios"))
    db = get_db()
    try:
        db.execute(
            "INSERT INTO usuarios (nombre, username, password_hash, rol) VALUES (?, ?, ?, ?)",
            (nombre, username, generate_password_hash(password), rol),
        )
        db.commit()
        etiqueta = "Administrador" if rol == "cosesa_admin" else "Operador"
        flash(f"Usuario {etiqueta.lower()} '{username}' creado.", "success")
    except Exception as e:
        flash(f"No se pudo crear el usuario (¿nombre de usuario repetido?): {e}", "error")
    db.close()
    return redirect(url_for("cosesa_usuarios"))


@app.route("/cosesa/usuarios/<int:usuario_id>/eliminar", methods=["POST"])
@admin_required
def eliminar_usuario_cosesa(usuario_id):
    user = get_current_user()
    if usuario_id == user["id"]:
        flash("No podés eliminar tu propio usuario.", "error")
        return redirect(url_for("cosesa_usuarios"))
    db = get_db()
    objetivo = db.execute(
        "SELECT * FROM usuarios WHERE id = ? AND rol IN ('cosesa','cosesa_admin')", (usuario_id,)
    ).fetchone()
    if objetivo is None:
        abort(404)
    db.execute("DELETE FROM usuarios WHERE id = ?", (usuario_id,))
    db.commit()
    db.close()
    flash(f"Usuario '{objetivo['username']}' eliminado.", "success")
    return redirect(url_for("cosesa_usuarios"))


@app.route("/cosesa/usuarios")
@admin_required
def cosesa_usuarios():
    db = get_db()
    usuarios_cosesa = db.execute(
        "SELECT * FROM usuarios WHERE rol IN ('cosesa','cosesa_admin') ORDER BY rol, nombre"
    ).fetchall()
    usuarios_cliente_rows = db.execute(
        "SELECT u.*, c.nombre AS cliente_nombre FROM usuarios u "
        "JOIN clientes c ON c.id = u.cliente_id WHERE u.rol = 'cliente' ORDER BY c.nombre, u.nombre"
    ).fetchall()
    clientes = db.execute("SELECT * FROM clientes ORDER BY nombre").fetchall()
    obras = db.execute(
        "SELECT o.*, c.nombre AS cliente_nombre FROM obras o JOIN clientes c ON c.id = o.cliente_id "
        "ORDER BY c.nombre, o.nombre"
    ).fetchall()
    usuarios_cliente = []
    for u in usuarios_cliente_rows:
        obras_asignadas = db.execute(
            "SELECT o.id, o.nombre FROM obras o JOIN usuario_obras uo ON uo.obra_id = o.id "
            "WHERE uo.usuario_id = ? ORDER BY o.nombre", (u["id"],)
        ).fetchall()
        usuarios_cliente.append({"u": u, "obras": obras_asignadas})
    db.close()
    backup_configurado = bool(os.environ.get("GITHUB_BACKUP_TOKEN") and os.environ.get("GITHUB_BACKUP_REPO"))
    return render_template(
        "cosesa_usuarios.html", usuarios_cosesa=usuarios_cosesa,
        usuarios_cliente=usuarios_cliente, clientes=clientes, obras=obras,
        backup_configurado=backup_configurado,
    )


@app.route("/cosesa/backup-ahora", methods=["POST"])
@admin_required
def backup_ahora_route():
    ok, mensaje = backup_now()
    flash(mensaje, "success" if ok else "error")
    return redirect(url_for("cosesa_usuarios"))


@app.route("/cosesa/usuarios/<int:usuario_id>/editar", methods=["GET", "POST"])
@admin_required
def editar_usuario_cliente(usuario_id):
    db = get_db()
    usuario = db.execute("SELECT * FROM usuarios WHERE id = ? AND rol = 'cliente'", (usuario_id,)).fetchone()
    if usuario is None:
        db.close()
        abort(404)
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        cliente_id = request.form.get("cliente_id", type=int)
        obra_ids = [int(x) for x in request.form.getlist("obras") if x.isdigit()]
        if not (nombre and username and cliente_id):
            flash("Completá nombre, usuario y cliente.", "error")
            db.close()
            return redirect(url_for("editar_usuario_cliente", usuario_id=usuario_id))
        try:
            if password:
                db.execute(
                    "UPDATE usuarios SET nombre = ?, username = ?, cliente_id = ?, password_hash = ? WHERE id = ?",
                    (nombre, username, cliente_id, generate_password_hash(password), usuario_id),
                )
            else:
                db.execute(
                    "UPDATE usuarios SET nombre = ?, username = ?, cliente_id = ? WHERE id = ?",
                    (nombre, username, cliente_id, usuario_id),
                )
            obras_validas = {
                r["id"] for r in db.execute(
                    "SELECT id FROM obras WHERE cliente_id = ?", (cliente_id,)
                ).fetchall()
            }
            set_obras_asignadas(db, usuario_id, [oid for oid in obra_ids if oid in obras_validas])
            db.commit()
            flash(f"Usuario '{username}' actualizado.", "success")
            db.close()
            return redirect(url_for("cosesa_usuarios"))
        except Exception as e:
            flash(f"No se pudo actualizar el usuario (¿nombre de usuario repetido?): {e}", "error")
    clientes = db.execute("SELECT * FROM clientes ORDER BY nombre").fetchall()
    obras = db.execute(
        "SELECT o.*, c.nombre AS cliente_nombre FROM obras o JOIN clientes c ON c.id = o.cliente_id "
        "ORDER BY c.nombre, o.nombre"
    ).fetchall()
    obras_asignadas_ids = get_obras_asignadas_ids(db, usuario_id)
    db.close()
    return render_template(
        "editar_usuario_cliente.html", usuario=usuario, clientes=clientes, obras=obras,
        obras_asignadas_ids=obras_asignadas_ids,
    )


@app.route("/cosesa/usuarios/<int:usuario_id>/eliminar_cliente", methods=["POST"])
@admin_required
def eliminar_usuario_cliente(usuario_id):
    db = get_db()
    objetivo = db.execute("SELECT * FROM usuarios WHERE id = ? AND rol = 'cliente'", (usuario_id,)).fetchone()
    if objetivo is None:
        db.close()
        abort(404)
    db.execute("DELETE FROM usuarios WHERE id = ?", (usuario_id,))
    db.commit()
    db.close()
    flash(f"Usuario '{objetivo['username']}' eliminado.", "success")
    return redirect(url_for("cosesa_usuarios"))


@app.route("/cosesa/clientes/nuevo", methods=["POST"])
@admin_required
def crear_cliente():
    nombre = request.form.get("nombre", "").strip()
    cuit = request.form.get("cuit", "").strip()
    contacto = request.form.get("contacto", "").strip()
    if not nombre:
        flash("El nombre del cliente es obligatorio.", "error")
        return redirect(url_for("cosesa_dashboard"))
    db = get_db()
    cur = db.execute(
        "INSERT INTO clientes (nombre, cuit, contacto) VALUES (?, ?, ?)",
        (nombre, cuit, contacto),
    )
    cliente_id = cur.lastrowid
    db.commit()
    db.close()
    flash(f"Cliente '{nombre}' creado.", "success")
    return redirect(url_for("cosesa_cliente_detail", cliente_id=cliente_id))


@app.route("/cosesa/clientes/<int:cliente_id>")
@cosesa_required
def cosesa_cliente_detail(cliente_id):
    db = get_db()
    cliente = db.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if cliente is None:
        abort(404)
    obras = db.execute(
        "SELECT o.*, "
        "(SELECT COUNT(*) FROM items i WHERE i.obra_id = o.id) AS n_items, "
        "(SELECT COALESCE(AVG(avance_pct),0) FROM items i WHERE i.obra_id = o.id) AS avance_prom "
        "FROM obras o WHERE o.cliente_id = ? ORDER BY o.creado_en DESC",
        (cliente_id,),
    ).fetchall()
    usuarios_cliente = db.execute(
        "SELECT * FROM usuarios WHERE cliente_id = ? AND rol = 'cliente'", (cliente_id,)
    ).fetchall()
    db.close()
    return render_template(
        "cosesa_cliente_detail.html", cliente=cliente, obras=obras, usuarios_cliente=usuarios_cliente
    )


@app.route("/cosesa/clientes/<int:cliente_id>/usuarios/nuevo", methods=["POST"])
@admin_required
def crear_usuario_cliente(cliente_id):
    nombre = request.form.get("nombre", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    obra_ids = [int(x) for x in request.form.getlist("obras") if x.isdigit()]
    volver = request.form.get("volver") or "cliente"
    destino = (
        url_for("cosesa_usuarios")
        if volver == "usuarios"
        else url_for("cosesa_cliente_detail", cliente_id=cliente_id)
    )
    if not (nombre and username and password):
        flash("Completá nombre, usuario y contraseña.", "error")
        return redirect(destino)
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO usuarios (nombre, username, password_hash, rol, cliente_id) "
            "VALUES (?, ?, ?, 'cliente', ?)",
            (nombre, username, generate_password_hash(password), cliente_id),
        )
        usuario_id = cur.lastrowid
        # Solo se asignan obras que efectivamente pertenecen a este cliente.
        obras_validas = {
            r["id"] for r in db.execute(
                "SELECT id FROM obras WHERE cliente_id = ?", (cliente_id,)
            ).fetchall()
        }
        set_obras_asignadas(db, usuario_id, [oid for oid in obra_ids if oid in obras_validas])
        db.commit()
        flash(f"Usuario de acceso '{username}' creado para el cliente.", "success")
    except Exception as e:
        flash(f"No se pudo crear el usuario (¿nombre de usuario repetido?): {e}", "error")
    db.close()
    return redirect(destino)


@app.route("/cosesa/clientes/<int:cliente_id>/obras/nueva", methods=["POST"])
@admin_required
def crear_obra(cliente_id):
    nombre = request.form.get("nombre", "").strip()
    descripcion = request.form.get("descripcion", "").strip()
    ubicacion = request.form.get("ubicacion", "").strip()
    fecha_inicio = request.form.get("fecha_inicio") or None
    fecha_fin_estimada = request.form.get("fecha_fin_estimada") or None
    if not nombre:
        flash("El nombre de la obra es obligatorio.", "error")
        return redirect(url_for("cosesa_cliente_detail", cliente_id=cliente_id))
    db = get_db()
    cur = db.execute(
        "INSERT INTO obras (cliente_id, nombre, descripcion, ubicacion, fecha_inicio, fecha_fin_estimada, estado) "
        "VALUES (?, ?, ?, ?, ?, ?, 'en_curso')",
        (cliente_id, nombre, descripcion, ubicacion, fecha_inicio, fecha_fin_estimada),
    )
    obra_id = cur.lastrowid
    db.commit()
    db.close()
    flash(f"Obra '{nombre}' creada.", "success")
    return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))


@app.route("/cosesa/obras/<int:obra_id>")
@cosesa_required
def cosesa_obra_detail(obra_id):
    db = get_db()
    obra = db.execute(
        "SELECT o.*, c.nombre AS cliente_nombre FROM obras o JOIN clientes c ON c.id = o.cliente_id "
        "WHERE o.id = ?", (obra_id,)
    ).fetchone()
    if obra is None:
        abort(404)
    item_ids = [r["id"] for r in db.execute("SELECT id FROM items WHERE obra_id = ?", (obra_id,)).fetchall()]
    for iid in item_ids:
        recompute_item_horas(db, iid)
    items = db.execute("SELECT * FROM items WHERE obra_id = ? ORDER BY creado_en", (obra_id,)).fetchall()
    gantt_html, curve_html, carga_plan_html, carga_real_html = build_obra_charts(db, items, interactive=True)
    certificados = db.execute(
        "SELECT * FROM certificados WHERE obra_id = ? ORDER BY subida_en DESC", (obra_id,)
    ).fetchall()
    db.close()
    return render_template(
        "cosesa_obra_detail.html", obra=obra, items=items, gantt_html=gantt_html, curve_html=curve_html,
        carga_plan_html=carga_plan_html, carga_real_html=carga_real_html,
        certificados=certificados,
    )


@app.route("/cosesa/obras/<int:obra_id>/certificados/nuevo", methods=["POST"])
@cosesa_required
def subir_certificado(obra_id):
    db = get_db()
    obra = db.execute("SELECT id FROM obras WHERE id = ?", (obra_id,)).fetchone()
    if obra is None:
        abort(404)
    db.close()
    user = get_current_user()
    archivo = request.files.get("certificado")
    saved = save_certificado(obra_id, archivo, user["id"])
    if saved:
        flash("Certificado cargado correctamente.", "success")
    return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))


@app.route("/cosesa/certificados/<int:cert_id>/eliminar", methods=["POST"])
@admin_required
def eliminar_certificado(cert_id):
    db = get_db()
    cert = db.execute("SELECT * FROM certificados WHERE id = ?", (cert_id,)).fetchone()
    if cert is None:
        abort(404)
    obra_id = cert["obra_id"]
    ruta = os.path.join(UPLOAD_DIR, cert["filename"])
    db.execute("DELETE FROM certificados WHERE id = ?", (cert_id,))
    db.commit()
    db.close()
    if os.path.exists(ruta):
        try:
            os.remove(ruta)
        except OSError:
            pass
    flash("Certificado eliminado.", "success")
    return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))


@app.route("/cosesa/obras/<int:obra_id>/finalizar", methods=["POST"])
@admin_required
def finalizar_obra(obra_id):
    db = get_db()
    db.execute("UPDATE obras SET estado = 'finalizada' WHERE id = ?", (obra_id,))
    db.commit()
    db.close()
    flash("Obra marcada como finalizada.", "success")
    return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))


@app.route("/cosesa/obras/<int:obra_id>/eliminar", methods=["POST"])
@admin_required
def eliminar_obra(obra_id):
    db = get_db()
    obra = db.execute("SELECT * FROM obras WHERE id = ?", (obra_id,)).fetchone()
    if obra is None:
        abort(404)
    cliente_id = obra["cliente_id"]
    db.execute("DELETE FROM obras WHERE id = ?", (obra_id,))
    db.commit()
    db.close()
    flash(f"Obra '{obra['nombre']}' eliminada, junto con sus ítems, fotos y avances.", "success")
    return redirect(url_for("cosesa_cliente_detail", cliente_id=cliente_id))


@app.route("/cosesa/items/<int:item_id>/eliminar", methods=["POST"])
@admin_required
def eliminar_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    obra_id = item["obra_id"]
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    db.close()
    flash(f"Ítem '{item['nombre']}' eliminado.", "success")
    return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))


@app.route("/cosesa/obras/<int:obra_id>/items/nuevo", methods=["POST"])
@admin_required
def crear_item(obra_id):
    nombre = request.form.get("nombre", "").strip()
    descripcion = request.form.get("descripcion", "").strip()
    tag_equipo = request.form.get("tag_equipo", "").strip()
    observaciones = request.form.get("observaciones", "").strip()
    fecha_inicio = request.form.get("fecha_inicio") or None
    fecha_inicio_plan = request.form.get("fecha_inicio_plan") or None
    fecha_fin_plan = request.form.get("fecha_fin_plan") or None
    if not nombre:
        flash("El nombre del ítem/equipo es obligatorio.", "error")
        return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))
    db = get_db()
    cur = db.execute(
        "INSERT INTO items (obra_id, nombre, descripcion, tag_equipo, fecha_inicio, fecha_inicio_plan, fecha_fin_plan, estado, observaciones) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pendiente', ?)",
        (obra_id, nombre, descripcion, tag_equipo, fecha_inicio, fecha_inicio_plan, fecha_fin_plan, observaciones),
    )
    db.commit()
    db.close()
    flash(f"Ítem '{nombre}' agregado.", "success")
    return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))


@app.route("/cosesa/obras/<int:obra_id>/plantilla-cronograma.xlsx")
@cosesa_required
def descargar_plantilla_cronograma(obra_id):
    db = get_db()
    obra = db.execute("SELECT * FROM obras WHERE id = ?", (obra_id,)).fetchone()
    if obra is None:
        abort(404)
    items_existentes = db.execute(
        "SELECT * FROM items WHERE obra_id = ? ORDER BY creado_en", (obra_id,)
    ).fetchall()
    db.close()
    buf = generar_plantilla_cronograma(items_existentes)
    nombre_archivo = f"cronograma_{obra['nombre'][:30]}.xlsx".replace(" ", "_")
    return send_file(
        buf, as_attachment=True, download_name=nombre_archivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/cosesa/obras/<int:obra_id>/plantilla-reales.xlsx")
@cosesa_required
def descargar_plantilla_reales(obra_id):
    db = get_db()
    obra = db.execute("SELECT * FROM obras WHERE id = ?", (obra_id,)).fetchone()
    if obra is None:
        abort(404)
    items_existentes = db.execute(
        "SELECT * FROM items WHERE obra_id = ? ORDER BY creado_en", (obra_id,)
    ).fetchall()
    demoras_por_item = {}
    for it in items_existentes:
        demoras_por_item[it["id"]] = db.execute(
            "SELECT * FROM demoras WHERE item_id = ? ORDER BY fecha_inicio", (it["id"],)
        ).fetchall()
    db.close()
    buf = generar_plantilla_reales(items_existentes, demoras_por_item)
    nombre_archivo = f"tiempos_reales_{obra['nombre'][:30]}.xlsx".replace(" ", "_")
    return send_file(
        buf, as_attachment=True, download_name=nombre_archivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/cosesa/obras/<int:obra_id>/importar-cronograma", methods=["POST"])
@admin_required
def importar_cronograma_excel(obra_id):
    db = get_db()
    obra = db.execute("SELECT * FROM obras WHERE id = ?", (obra_id,)).fetchone()
    if obra is None:
        abort(404)
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        flash("Elegí un archivo .xlsx de cronograma para importar.", "error")
        db.close()
        return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))
    if not archivo.filename.lower().endswith(".xlsx"):
        flash("El archivo debe ser un Excel (.xlsx) generado con la plantilla de cronograma.", "error")
        db.close()
        return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))

    try:
        tareas, errores = leer_cronograma(archivo)
    except Exception:
        flash("No se pudo leer el archivo. Verificá que sea un .xlsx válido basado en la plantilla.", "error")
        db.close()
        return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))

    items_existentes = db.execute(
        "SELECT id, tag_equipo FROM items WHERE obra_id = ?", (obra_id,)
    ).fetchall()
    tag_a_id = {i["tag_equipo"]: i["id"] for i in items_existentes if i["tag_equipo"]}

    creados, actualizados = 0, 0
    for t in tareas:
        existente_id = tag_a_id.get(t["tag"]) if t["tag"] else None
        if existente_id:
            db.execute(
                "UPDATE items SET nombre=?, descripcion=?, fecha_inicio_plan=?, fecha_fin_plan=? WHERE id=?",
                (t["nombre"], t["descripcion"], t["fecha_inicio_plan"], t["fecha_fin_plan"], existente_id),
            )
            actualizados += 1
        else:
            cur = db.execute(
                "INSERT INTO items (obra_id, nombre, descripcion, tag_equipo, fecha_inicio_plan, fecha_fin_plan, estado) "
                "VALUES (?,?,?,?,?,?,'pendiente')",
                (obra_id, t["nombre"], t["descripcion"], t["tag"], t["fecha_inicio_plan"], t["fecha_fin_plan"]),
            )
            if t["tag"]:
                tag_a_id[t["tag"]] = cur.lastrowid
            creados += 1

    db.commit()
    db.close()

    flash(f"Cronograma importado: {creados} tarea(s) nueva(s), {actualizados} actualizada(s).", "success")
    if errores:
        flash(f"{len(errores)} fila(s) se saltearon: " + " | ".join(errores[:8]) + (" ..." if len(errores) > 8 else ""), "error")
    return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))


@app.route("/cosesa/obras/<int:obra_id>/importar-reales", methods=["POST"])
@cosesa_required
def importar_reales_excel(obra_id):
    db = get_db()
    obra = db.execute("SELECT * FROM obras WHERE id = ?", (obra_id,)).fetchone()
    if obra is None:
        abort(404)
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        flash("Elegí un archivo .xlsx de tiempos reales para importar.", "error")
        db.close()
        return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))
    if not archivo.filename.lower().endswith(".xlsx"):
        flash("El archivo debe ser un Excel (.xlsx) generado con la plantilla de tiempos reales.", "error")
        db.close()
        return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))

    try:
        filas, errores = leer_reales(archivo)
    except Exception:
        flash("No se pudo leer el archivo. Verificá que sea un .xlsx válido basado en la plantilla.", "error")
        db.close()
        return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))

    items_existentes = db.execute(
        "SELECT id, tag_equipo, fecha_inicio, fecha_fin, avance_pct FROM items WHERE obra_id = ?", (obra_id,)
    ).fetchall()
    tag_a_item = {i["tag_equipo"]: i for i in items_existentes if i["tag_equipo"]}

    items_actualizados, demoras_creadas, demoras_repetidas = 0, 0, 0
    ya_tocados = set()
    for f in filas:
        item = tag_a_item.get(f["tag"])
        if not item:
            errores.append(f"Tiempos reales: no se encontró ninguna tarea con tag '{f['tag']}', se saltea.")
            continue

        if f["fecha_inicio"] or f["fecha_fin"]:
            fecha_inicio = f["fecha_inicio"] or item["fecha_inicio"]
            fecha_fin = f["fecha_fin"] or item["fecha_fin"]
            estado = "pendiente"
            if fecha_fin:
                estado = "finalizado"
            elif fecha_inicio:
                estado = "en_curso"
            db.execute(
                "UPDATE items SET fecha_inicio=?, fecha_fin=?, estado=? WHERE id=?",
                (fecha_inicio, fecha_fin, estado, item["id"]),
            )
            if item["id"] not in ya_tocados:
                items_actualizados += 1
                ya_tocados.add(item["id"])

        if f["demora_inicio"] and f["demora_fin"]:
            ya_existe = db.execute(
                "SELECT 1 FROM demoras WHERE item_id = ? AND fecha_inicio = ? AND fecha_fin = ? LIMIT 1",
                (item["id"], f["demora_inicio"], f["demora_fin"]),
            ).fetchone()
            if ya_existe:
                demoras_repetidas += 1
            else:
                db.execute(
                    "INSERT INTO demoras (item_id, fecha_inicio, fecha_fin, motivo) VALUES (?,?,?,?)",
                    (item["id"], f["demora_inicio"], f["demora_fin"], f["motivo_demora"]),
                )
                demoras_creadas += 1

    db.commit()
    db.close()

    resumen = (
        f"Tiempos reales importados: {items_actualizados} tarea(s) actualizada(s), "
        f"{demoras_creadas} demora(s) cargada(s)"
        + (f", {demoras_repetidas} demora(s) ya existente(s) omitida(s)." if demoras_repetidas else ".")
    )
    flash(resumen, "success")
    if errores:
        flash(f"{len(errores)} fila(s) se saltearon: " + " | ".join(errores[:8]) + (" ..." if len(errores) > 8 else ""), "error")
    return redirect(url_for("cosesa_obra_detail", obra_id=obra_id))


@app.route("/cosesa/items/<int:item_id>/editar", methods=["POST"])
@cosesa_required
def editar_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    nombre = request.form.get("nombre", "").strip()
    tag_equipo = request.form.get("tag_equipo", "").strip()
    descripcion = request.form.get("descripcion", "").strip()
    if not nombre:
        flash("El nombre del ítem/equipo es obligatorio.", "error")
        db.close()
        return redirect(url_for("cosesa_item_detail", item_id=item_id))
    db.execute(
        "UPDATE items SET nombre = ?, tag_equipo = ?, descripcion = ? WHERE id = ?",
        (nombre, tag_equipo, descripcion, item_id),
    )
    db.commit()
    db.close()
    flash("Datos del ítem actualizados.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>")
@cosesa_required
def cosesa_item_detail(item_id):
    db = get_db()
    item = db.execute(
        "SELECT i.*, o.id AS obra_id, o.nombre AS obra_nombre, o.cliente_id, c.nombre AS cliente_nombre "
        "FROM items i JOIN obras o ON o.id = i.obra_id JOIN clientes c ON c.id = o.cliente_id "
        "WHERE i.id = ?", (item_id,)
    ).fetchone()
    if item is None:
        abort(404)
    avances = db.execute(
        "SELECT a.*, u.nombre AS usuario_nombre FROM avance_log a "
        "LEFT JOIN usuarios u ON u.id = a.usuario_id "
        "WHERE a.item_id = ? ORDER BY a.fecha DESC, a.id DESC", (item_id,)
    ).fetchall()
    fotos_todas = db.execute("SELECT * FROM fotos WHERE item_id = ? ORDER BY subida_en DESC", (item_id,)).fetchall()
    fotos = [f for f in fotos_todas if f["tipo"] != "certificado_lavado"]
    fotos_checklist = [f for f in fotos_todas if f["tipo"] == "certificado_lavado"]
    todas_las_demoras = db.execute(
        "SELECT * FROM demoras WHERE item_id = ? ORDER BY fecha_inicio", (item_id,)
    ).fetchall()
    total_demoras_min = sum(demora_minutos(d) for d in todas_las_demoras)
    pausa_actual = db.execute(
        "SELECT * FROM demoras WHERE item_id = ? AND fecha_fin IS NULL ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    # La demora todavía abierta (pausa en curso) se maneja con los botones de
    # pausar/reanudar, no desde la lista genérica de demoras editables.
    demoras = [d for d in todas_las_demoras if d["fecha_fin"]]
    checklist = get_or_create_checklist(db, item_id)
    checklist_puntos = get_checklist_puntos(db, item_id)
    piezas = db.execute(
        "SELECT * FROM piezas_lavado WHERE item_id = ? ORDER BY creado_en", (item_id,)
    ).fetchall()
    personal = get_personal(db, item_id)
    horas = recompute_item_horas(db, item_id)
    item = db.execute(
        "SELECT i.*, o.id AS obra_id, o.nombre AS obra_nombre, o.cliente_id, c.nombre AS cliente_nombre "
        "FROM items i JOIN obras o ON o.id = i.obra_id JOIN clientes c ON c.id = o.cliente_id "
        "WHERE i.id = ?", (item_id,)
    ).fetchone()
    db.close()
    return render_template(
        "cosesa_item_detail.html", item=item, avances=avances, fotos=fotos, today=now_local_str(),
        demoras=demoras, total_demoras=fmt_duracion(total_demoras_min), pausa_actual=pausa_actual,
        checklist=checklist, checklist_puntos=checklist_puntos, checklist_estados=CHECKLIST_ESTADOS,
        fotos_checklist=fotos_checklist, piezas=piezas, piezas_sugeridas=PIEZAS_SUGERIDAS,
        personal=personal, cargos_personal=CARGOS_PERSONAL, turnos=TURNOS, horas=horas,
    )


@app.route("/cosesa/items/<int:item_id>/avance", methods=["POST"])
@cosesa_required
def registrar_avance(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)

    avance_pct = int(request.form.get("avance_pct", item["avance_pct"]))
    avance_pct = max(0, min(100, avance_pct))
    comentario = request.form.get("comentario", "").strip()
    fecha = request.form.get("fecha") or now_local_str()
    user = get_current_user()

    db.execute(
        "INSERT INTO avance_log (item_id, fecha, avance_pct, comentario, usuario_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, fecha, avance_pct, comentario, user["id"]),
    )

    fecha_inicio = item["fecha_inicio"]
    fecha_fin = item["fecha_fin"]
    if avance_pct >= 100:
        nuevo_estado = "finalizado"
        fecha_fin = fecha
    elif avance_pct > 0:
        nuevo_estado = "en_curso"
        fecha_inicio = fecha_inicio or fecha
        # Si la tarea ya estaba finalizada y el avance bajó de 100%, se está
        # reabriendo: ahí sí hay que borrar la fecha de fin real, porque ya
        # no está terminada. Si NO estaba finalizada, no se toca la fecha de
        # fin real que se haya cargado a mano (por ejemplo, planificando el
        # cierre con anticipación) — antes se borraba siempre, y eso hacía
        # que el cronograma "retrocediera" cada vez que se sumaba avance.
        if item["estado"] == "finalizado":
            fecha_fin = None
    else:
        nuevo_estado = "pendiente"

    db.execute(
        "UPDATE items SET avance_pct = ?, estado = ?, fecha_inicio = ?, fecha_fin = ? WHERE id = ?",
        (avance_pct, nuevo_estado, fecha_inicio, fecha_fin, item_id),
    )
    db.commit()
    # Las horas hombre/máquina se recalculan solas: horas de trabajo (real - demoras) x personal/bombas asignados.
    recompute_item_horas(db, item_id)
    db.close()

    # Fotos de progreso opcionales junto con el avance
    foto_progreso = request.files.get("foto_progreso")
    if foto_progreso and foto_progreso.filename:
        save_photo(item_id, foto_progreso, "progreso", comentario, user["id"])

    flash("Avance registrado.", "success")
    if request.form.get("volver") == "obra":
        return redirect(url_for("cosesa_obra_detail", obra_id=item["obra_id"]))
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/pausar", methods=["POST"])
@cosesa_required
def pausar_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    if item["estado"] != "en_curso":
        flash("Solo se puede pausar una tarea que esté en curso.", "error")
    elif item["pausado_en"]:
        flash("Esta tarea ya está pausada.", "error")
    else:
        motivo = request.form.get("motivo", "").strip()
        if not motivo:
            flash("Contá brevemente el motivo de la pausa.", "error")
        else:
            user = get_current_user()
            ahora = now_local_str()
            db.execute(
                "INSERT INTO demoras (item_id, fecha_inicio, fecha_fin, motivo, usuario_id) "
                "VALUES (?, ?, NULL, ?, ?)",
                (item_id, ahora, motivo, user["id"]),
            )
            db.execute("UPDATE items SET pausado_en = ? WHERE id = ?", (ahora, item_id))
            db.commit()
            recompute_item_horas(db, item_id)
            flash("Tarea pausada. Deja de sumar horas hombre y horas máquina hasta que la reanudes.", "success")
    db.close()
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/reanudar", methods=["POST"])
@cosesa_required
def reanudar_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    demora_abierta = db.execute(
        "SELECT * FROM demoras WHERE item_id = ? AND fecha_fin IS NULL ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    if not item["pausado_en"] and demora_abierta is None:
        flash("Esta tarea no está pausada.", "error")
    else:
        ahora = now_local_str()
        if demora_abierta:
            db.execute("UPDATE demoras SET fecha_fin = ? WHERE id = ?", (ahora, demora_abierta["id"]))
        db.execute("UPDATE items SET pausado_en = NULL WHERE id = ?", (item_id,))
        db.commit()
        recompute_item_horas(db, item_id)
        flash("Tarea reanudada.", "success")
    db.close()
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/finalizar", methods=["POST"])
@cosesa_required
def finalizar_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    if item["estado"] == "finalizado":
        flash("Esta tarea ya estaba finalizada.", "error")
        db.close()
        return redirect(url_for("cosesa_item_detail", item_id=item_id))

    user = get_current_user()
    ahora = now_local_str()
    # Si estaba pausada, cerramos la pausa abierta para que no quede un hueco sin fin.
    demora_abierta = db.execute(
        "SELECT * FROM demoras WHERE item_id = ? AND fecha_fin IS NULL ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    if demora_abierta:
        db.execute("UPDATE demoras SET fecha_fin = ? WHERE id = ?", (ahora, demora_abierta["id"]))
    fecha_inicio = item["fecha_inicio"] or ahora
    db.execute(
        "UPDATE items SET estado = 'finalizado', fecha_inicio = ?, fecha_fin = ?, pausado_en = NULL WHERE id = ?",
        (fecha_inicio, ahora, item_id),
    )
    db.execute(
        "INSERT INTO avance_log (item_id, fecha, avance_pct, comentario, usuario_id) VALUES (?, ?, ?, ?, ?)",
        (item_id, ahora, item["avance_pct"], "Tarea finalizada manualmente, con el avance que tenía.", user["id"]),
    )
    db.commit()
    recompute_item_horas(db, item_id)
    db.close()
    flash(f"Tarea finalizada con {item['avance_pct']}% de avance.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/plan", methods=["POST"])
@cosesa_required
def actualizar_plan_item(item_id):
    fecha_inicio_plan = request.form.get("fecha_inicio_plan") or None
    fecha_fin_plan = request.form.get("fecha_fin_plan") or None
    db = get_db()
    item = db.execute("SELECT obra_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    db.execute(
        "UPDATE items SET fecha_inicio_plan = ?, fecha_fin_plan = ? WHERE id = ?",
        (fecha_inicio_plan, fecha_fin_plan, item_id),
    )
    db.commit()
    db.close()
    flash("Cronograma planificado actualizado.", "success")
    return redirect(url_for("cosesa_obra_detail", obra_id=item["obra_id"]))


@app.route("/cosesa/items/<int:item_id>/fechas-reales", methods=["POST"])
@cosesa_required
def actualizar_fechas_reales(item_id):
    fecha_inicio = request.form.get("fecha_inicio") or None
    fecha_fin = request.form.get("fecha_fin") or None
    db = get_db()
    item = db.execute("SELECT obra_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    db.execute(
        "UPDATE items SET fecha_inicio = ?, fecha_fin = ? WHERE id = ?",
        (fecha_inicio, fecha_fin, item_id),
    )
    db.commit()
    recompute_item_horas(db, item_id)
    db.close()
    flash("Fechas reales de la tarea actualizadas.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/demoras/nueva", methods=["POST"])
@cosesa_required
def agregar_demora(item_id):
    fecha_inicio = request.form.get("fecha_inicio")
    fecha_fin = request.form.get("fecha_fin")
    motivo = request.form.get("motivo", "").strip()
    if not fecha_inicio or not fecha_fin:
        flash("Completá la fecha y hora de inicio y de fin de la demora.", "error")
        return redirect(url_for("cosesa_item_detail", item_id=item_id))
    if fecha_fin < fecha_inicio:
        flash("La fecha de fin de la demora no puede ser anterior a la de inicio.", "error")
        return redirect(url_for("cosesa_item_detail", item_id=item_id))
    db = get_db()
    item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    user = get_current_user()
    db.execute(
        "INSERT INTO demoras (item_id, fecha_inicio, fecha_fin, motivo, usuario_id) VALUES (?, ?, ?, ?, ?)",
        (item_id, fecha_inicio, fecha_fin, motivo, user["id"]),
    )
    db.commit()
    recompute_item_horas(db, item_id)
    db.close()
    flash("Demora registrada.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/demoras/<int:demora_id>/eliminar", methods=["POST"])
@cosesa_required
def eliminar_demora(demora_id):
    db = get_db()
    demora = db.execute("SELECT * FROM demoras WHERE id = ?", (demora_id,)).fetchone()
    if demora is None:
        abort(404)
    item_id = demora["item_id"]
    db.execute("DELETE FROM demoras WHERE id = ?", (demora_id,))
    db.commit()
    recompute_item_horas(db, item_id)
    db.close()
    flash("Demora eliminada.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/demoras/<int:demora_id>/editar", methods=["POST"])
@cosesa_required
def editar_demora(demora_id):
    fecha_inicio = request.form.get("fecha_inicio")
    fecha_fin = request.form.get("fecha_fin")
    motivo = request.form.get("motivo", "").strip()
    db = get_db()
    demora = db.execute("SELECT * FROM demoras WHERE id = ?", (demora_id,)).fetchone()
    if demora is None:
        abort(404)
    item_id = demora["item_id"]
    if not fecha_inicio or not fecha_fin:
        flash("Completá la fecha y hora de inicio y de fin de la demora.", "error")
        db.close()
        return redirect(url_for("cosesa_item_detail", item_id=item_id))
    if fecha_fin < fecha_inicio:
        flash("La fecha de fin de la demora no puede ser anterior a la de inicio.", "error")
        db.close()
        return redirect(url_for("cosesa_item_detail", item_id=item_id))
    db.execute(
        "UPDATE demoras SET fecha_inicio = ?, fecha_fin = ?, motivo = ? WHERE id = ?",
        (fecha_inicio, fecha_fin, motivo, demora_id),
    )
    db.commit()
    recompute_item_horas(db, item_id)
    db.close()
    flash("Demora actualizada.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/personal", methods=["POST"])
@cosesa_required
def actualizar_personal(item_id):
    db = get_db()
    item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    get_personal(db, item_id)  # asegura que existan las dos filas (día / noche)
    for turno, _ in TURNOS:
        valores = []
        for campo, _ in CARGOS_PERSONAL:
            try:
                v = max(0, int(request.form.get(f"{turno}_{campo}", "0") or 0))
            except ValueError:
                v = 0
            valores.append(v)
        try:
            bombas = max(0, int(request.form.get(f"{turno}_bombas", "0") or 0))
        except ValueError:
            bombas = 0
        db.execute(
            "UPDATE personal_lavado SET supervisores = ?, tecnicos_hs = ?, punteros = ?, operarios = ?, "
            "bombas = ?, actualizado_en = datetime('now') WHERE item_id = ? AND turno = ?",
            (*valores, bombas, item_id, turno),
        )
    db.commit()
    recompute_item_horas(db, item_id)
    db.close()
    flash("Personal asignado actualizado.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/observaciones", methods=["POST"])
@cosesa_required
def actualizar_observaciones(item_id):
    observaciones = request.form.get("observaciones", "").strip()
    db = get_db()
    db.execute("UPDATE items SET observaciones = ? WHERE id = ?", (observaciones, item_id))
    db.commit()
    db.close()
    flash("Observaciones actualizadas.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/foto", methods=["POST"])
@cosesa_required
def subir_foto(item_id):
    user = get_current_user()
    tipo = request.form.get("tipo", "progreso")
    comentario = request.form.get("comentario", "").strip()
    file_storage = request.files.get("foto")
    saved = save_photo(item_id, file_storage, tipo, comentario, user["id"])
    if saved:
        flash("Foto subida correctamente.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/checklist", methods=["POST"])
@cosesa_required
def actualizar_checklist(item_id):
    db = get_db()
    item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    get_or_create_checklist(db, item_id)
    user = get_current_user()
    tipo_servicio = request.form.get("tipo_servicio", "").strip() or "Lavado hidrocinético industrial"
    db.execute(
        "UPDATE checklist_lavado SET tipo_servicio = ?, actualizado_en = datetime('now'), usuario_id = ? "
        "WHERE item_id = ?",
        (tipo_servicio, user["id"], item_id),
    )
    db.commit()
    db.close()
    flash("Checklist de lavado actualizado.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/checklist/puntos/nuevo", methods=["POST"])
@cosesa_required
def crear_punto_checklist(item_id):
    db = get_db()
    item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    etiqueta = request.form.get("etiqueta", "").strip()
    if not etiqueta:
        flash("Ingresá un nombre para el punto del checklist.", "error")
        return redirect(url_for("cosesa_item_detail", item_id=item_id))
    get_or_create_checklist(db, item_id)
    orden = db.execute(
        "SELECT COALESCE(MAX(orden), 0) + 1 FROM checklist_puntos WHERE item_id = ?", (item_id,)
    ).fetchone()[0]
    db.execute(
        "INSERT INTO checklist_puntos (item_id, orden, etiqueta) VALUES (?, ?, ?)",
        (item_id, orden, etiqueta),
    )
    db.commit()
    db.close()
    flash("Punto de checklist agregado.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/checklist/puntos/<int:punto_id>/editar", methods=["POST"])
@cosesa_required
def editar_punto_checklist(punto_id):
    db = get_db()
    punto = db.execute("SELECT * FROM checklist_puntos WHERE id = ?", (punto_id,)).fetchone()
    if punto is None:
        abort(404)
    etiqueta = request.form.get("etiqueta", "").strip() or punto["etiqueta"]
    estado = request.form.get("estado", "pendiente")
    estado = estado if estado in CHECKLIST_ESTADOS else "pendiente"
    db.execute(
        "UPDATE checklist_puntos SET etiqueta = ?, estado = ? WHERE id = ?",
        (etiqueta, estado, punto_id),
    )
    db.commit()
    item_id = punto["item_id"]
    db.close()
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/checklist/puntos/<int:punto_id>/eliminar", methods=["POST"])
@cosesa_required
def eliminar_punto_checklist(punto_id):
    db = get_db()
    punto = db.execute("SELECT * FROM checklist_puntos WHERE id = ?", (punto_id,)).fetchone()
    if punto is None:
        abort(404)
    item_id = punto["item_id"]
    db.execute("DELETE FROM checklist_puntos WHERE id = ?", (punto_id,))
    db.commit()
    db.close()
    flash("Punto de checklist eliminado.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/checklist/imprimir")
@cosesa_required
def checklist_item_html(item_id):
    db = get_db()
    item = db.execute(
        "SELECT i.*, o.nombre AS obra_nombre, o.ubicacion, c.nombre AS cliente_nombre "
        "FROM items i JOIN obras o ON o.id = i.obra_id JOIN clientes c ON c.id = o.cliente_id "
        "WHERE i.id = ?", (item_id,)
    ).fetchone()
    if item is None:
        abort(404)
    checklist = get_or_create_checklist(db, item_id)
    checklist_puntos = get_checklist_puntos(db, item_id)
    piezas = db.execute(
        "SELECT * FROM piezas_lavado WHERE item_id = ? ORDER BY creado_en", (item_id,)
    ).fetchall()
    db.close()
    return render_template(
        "checklist_lavado.html", item=item, checklist=checklist, checklist_puntos=checklist_puntos,
        piezas=piezas, generado_en=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )


@app.route("/cosesa/items/<int:item_id>/checklist/pdf")
@cosesa_required
def checklist_item_pdf(item_id):
    user = get_current_user()
    token = make_pdf_token(user["id"])
    url = url_for("checklist_item_html", item_id=item_id, pdf_token=token, _external=True)
    out_path = os.path.join(tempfile.gettempdir(), f"checklist_lavado_item_{item_id}.pdf")
    render_pdf(url, out_path)
    return send_file(out_path, as_attachment=True, download_name=f"checklist_lavado_item_{item_id}.pdf")


@app.route("/cosesa/items/<int:item_id>/checklist/foto", methods=["POST"])
@cosesa_required
def subir_foto_checklist(item_id):
    user = get_current_user()
    file_storage = request.files.get("foto")
    saved = save_photo(item_id, file_storage, "certificado_lavado", "Documento firmado", user["id"])
    if saved:
        flash("Foto del documento firmado subida correctamente.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/items/<int:item_id>/piezas/nueva", methods=["POST"])
@cosesa_required
def crear_pieza(item_id):
    db = get_db()
    item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    nombre = request.form.get("nombre", "").strip()
    if not nombre:
        flash("Ingresá el nombre de la pieza.", "error")
        return redirect(url_for("cosesa_item_detail", item_id=item_id))
    fecha_recepcion = request.form.get("fecha_recepcion") or None
    fecha_lavado = request.form.get("fecha_lavado") or None
    precinto = request.form.get("precinto", "").strip()
    confirmado = "ok" if request.form.get("confirmado") else "pendiente"
    firmado_por_cosesa = request.form.get("firmado_por_cosesa", "").strip()
    firmado_por_cliente = request.form.get("firmado_por_cliente", "").strip()
    db.execute(
        "INSERT INTO piezas_lavado (item_id, nombre, fecha_recepcion, fecha_lavado, precinto, confirmado, "
        "firmado_por_cosesa, firmado_por_cliente) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, nombre, fecha_recepcion, fecha_lavado, precinto, confirmado,
         firmado_por_cosesa, firmado_por_cliente),
    )
    db.commit()
    db.close()
    flash("Pieza agregada.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/piezas/<int:pieza_id>/editar", methods=["POST"])
@cosesa_required
def editar_pieza(pieza_id):
    db = get_db()
    pieza = db.execute("SELECT * FROM piezas_lavado WHERE id = ?", (pieza_id,)).fetchone()
    if pieza is None:
        abort(404)
    nombre = request.form.get("nombre", "").strip() or pieza["nombre"]
    fecha_recepcion = request.form.get("fecha_recepcion") or None
    fecha_lavado = request.form.get("fecha_lavado") or None
    precinto = request.form.get("precinto", "").strip()
    confirmado = "ok" if request.form.get("confirmado") else "pendiente"
    firmado_por_cosesa = request.form.get("firmado_por_cosesa", "").strip()
    firmado_por_cliente = request.form.get("firmado_por_cliente", "").strip()
    db.execute(
        "UPDATE piezas_lavado SET nombre = ?, fecha_recepcion = ?, fecha_lavado = ?, precinto = ?, "
        "confirmado = ?, firmado_por_cosesa = ?, firmado_por_cliente = ? WHERE id = ?",
        (nombre, fecha_recepcion, fecha_lavado, precinto, confirmado,
         firmado_por_cosesa, firmado_por_cliente, pieza_id),
    )
    db.commit()
    item_id = pieza["item_id"]
    db.close()
    flash("Pieza actualizada.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


@app.route("/cosesa/piezas/<int:pieza_id>/eliminar", methods=["POST"])
@cosesa_required
def eliminar_pieza(pieza_id):
    db = get_db()
    pieza = db.execute("SELECT * FROM piezas_lavado WHERE id = ?", (pieza_id,)).fetchone()
    if pieza is None:
        abort(404)
    item_id = pieza["item_id"]
    db.execute("DELETE FROM piezas_lavado WHERE id = ?", (pieza_id,))
    db.commit()
    db.close()
    flash("Pieza eliminada.", "success")
    return redirect(url_for("cosesa_item_detail", item_id=item_id))


# ---------------------------------------------------------------------------
# Lado CLIENTE (solo lectura)
# ---------------------------------------------------------------------------

@app.route("/cliente")
@login_required("cliente")
def cliente_dashboard():
    user = get_current_user()
    db = get_db()
    obras = db.execute(
        "SELECT o.*, "
        "(SELECT COUNT(*) FROM items i WHERE i.obra_id = o.id) AS n_items, "
        "(SELECT COALESCE(AVG(avance_pct),0) FROM items i WHERE i.obra_id = o.id) AS avance_prom "
        "FROM obras o JOIN usuario_obras uo ON uo.obra_id = o.id "
        "WHERE uo.usuario_id = ? ORDER BY o.creado_en DESC",
        (user["id"],),
    ).fetchall()
    db.close()
    return render_template("cliente_dashboard.html", obras=obras)


def _check_obra_asignada(db, obra_id, usuario_id):
    obra = db.execute(
        "SELECT o.* FROM obras o JOIN usuario_obras uo ON uo.obra_id = o.id "
        "WHERE o.id = ? AND uo.usuario_id = ?",
        (obra_id, usuario_id),
    ).fetchone()
    if obra is None:
        abort(403)
    return obra


@app.route("/cliente/obras/<int:obra_id>")
@login_required("cliente")
def cliente_obra_detail(obra_id):
    user = get_current_user()
    db = get_db()
    obra = _check_obra_asignada(db, obra_id, user["id"])
    items = db.execute("SELECT * FROM items WHERE obra_id = ? ORDER BY creado_en", (obra_id,)).fetchall()
    gantt_html, curve_html, carga_plan_html, carga_real_html = build_obra_charts(db, items)
    certificados = db.execute(
        "SELECT * FROM certificados WHERE obra_id = ? ORDER BY subida_en DESC", (obra_id,)
    ).fetchall()
    db.close()
    return render_template(
        "cliente_obra_detail.html", obra=obra, items=items, gantt_html=gantt_html, curve_html=curve_html,
        carga_plan_html=carga_plan_html, carga_real_html=carga_real_html,
        certificados=certificados,
    )


@app.route("/cliente/items/<int:item_id>")
@login_required("cliente")
def cliente_item_detail(item_id):
    user = get_current_user()
    db = get_db()
    item = db.execute(
        "SELECT i.*, o.nombre AS obra_nombre, o.cliente_id FROM items i "
        "JOIN obras o ON o.id = i.obra_id WHERE i.id = ?", (item_id,)
    ).fetchone()
    if item is None or not usuario_puede_ver_obra(db, user, item["obra_id"]):
        abort(403)
    avances = db.execute(
        "SELECT * FROM avance_log WHERE item_id = ? ORDER BY fecha DESC, id DESC", (item_id,)
    ).fetchall()
    fotos_todas = db.execute(
        "SELECT * FROM fotos WHERE item_id = ? ORDER BY subida_en DESC", (item_id,)
    ).fetchall()
    fotos = [f for f in fotos_todas if f["tipo"] != "certificado_lavado"]
    fotos_checklist = [f for f in fotos_todas if f["tipo"] == "certificado_lavado"]
    checklist = db.execute("SELECT * FROM checklist_lavado WHERE item_id = ?", (item_id,)).fetchone()
    checklist_puntos = get_checklist_puntos(db, item_id)
    piezas = db.execute(
        "SELECT * FROM piezas_lavado WHERE item_id = ? ORDER BY creado_en", (item_id,)
    ).fetchall()
    db.close()
    return render_template(
        "cliente_item_detail.html", item=item, avances=avances, fotos=fotos,
        checklist=checklist, checklist_puntos=checklist_puntos,
        fotos_checklist=fotos_checklist, piezas=piezas,
    )


# ---------------------------------------------------------------------------
# Reportes
# ---------------------------------------------------------------------------

def _puede_ver_obra(db, user, obra):
    return usuario_puede_ver_obra(db, user, obra["id"])


@app.route("/reporte/obra/<int:obra_id>")
def reporte_obra(obra_id):
    user = get_current_user()
    if user is None:
        return redirect(url_for("login"))
    db = get_db()
    obra = db.execute(
        "SELECT o.*, c.nombre AS cliente_nombre FROM obras o JOIN clientes c ON c.id = o.cliente_id WHERE o.id = ?",
        (obra_id,),
    ).fetchone()
    if obra is None or not _puede_ver_obra(db, user, obra):
        abort(403)
    items = db.execute("SELECT * FROM items WHERE obra_id = ? ORDER BY creado_en", (obra_id,)).fetchall()
    gantt_html, curve_html, carga_plan_html, carga_real_html = build_obra_charts(db, items)
    items_data = []
    for item in items:
        fotos = db.execute(
            "SELECT * FROM fotos WHERE item_id = ? ORDER BY tipo, subida_en", (item["id"],)
        ).fetchall()
        avances = db.execute(
            "SELECT * FROM avance_log WHERE item_id = ? ORDER BY fecha", (item["id"],)
        ).fetchall()
        checklist = db.execute(
            "SELECT * FROM checklist_lavado WHERE item_id = ?", (item["id"],)
        ).fetchone()
        checklist_puntos = get_checklist_puntos(db, item["id"])
        piezas = db.execute(
            "SELECT * FROM piezas_lavado WHERE item_id = ? ORDER BY creado_en", (item["id"],)
        ).fetchall()
        items_data.append({
            "item": item,
            "fotos_antes": [f for f in fotos if f["tipo"] == "antes"],
            "fotos_despues": [f for f in fotos if f["tipo"] == "despues"],
            "fotos_progreso": [f for f in fotos if f["tipo"] == "progreso"],
            "fotos_checklist": [f for f in fotos if f["tipo"] == "certificado_lavado"],
            "checklist": checklist,
            "checklist_puntos": checklist_puntos,
            "piezas": piezas,
            "avances": avances,
        })
    certificados = db.execute(
        "SELECT * FROM certificados WHERE obra_id = ? ORDER BY subida_en DESC", (obra_id,)
    ).fetchall()
    db.close()
    return render_template(
        "reporte_cliente.html", obra=obra, items_data=items_data,
        gantt_html=gantt_html, curve_html=curve_html,
        generado_en=datetime.now().strftime("%d/%m/%Y %H:%M"),
        certificados=certificados,
    )


@app.route("/reporte/obra/<int:obra_id>/pdf")
def reporte_obra_pdf(obra_id):
    user = get_current_user()
    if user is None:
        return redirect(url_for("login"))
    db = get_db()
    obra = db.execute("SELECT * FROM obras WHERE id = ?", (obra_id,)).fetchone()
    puede_ver = obra is not None and _puede_ver_obra(db, user, obra)
    db.close()
    if not puede_ver:
        abort(403)
    token = make_pdf_token(user["id"])
    url = url_for("reporte_obra", obra_id=obra_id, pdf_token=token, _external=True)
    out_path = os.path.join(tempfile.gettempdir(), f"reporte_obra_{obra_id}.pdf")
    render_pdf(url, out_path)
    return send_file(out_path, as_attachment=True, download_name=f"reporte_obra_{obra_id}.pdf")


@app.route("/cosesa/reporte-interno/obra/<int:obra_id>")
@cosesa_required
def reporte_interno_obra(obra_id):
    db = get_db()
    obra = db.execute(
        "SELECT o.*, c.nombre AS cliente_nombre FROM obras o JOIN clientes c ON c.id = o.cliente_id WHERE o.id = ?",
        (obra_id,),
    ).fetchone()
    if obra is None:
        abort(404)
    item_ids = [r["id"] for r in db.execute("SELECT id FROM items WHERE obra_id = ?", (obra_id,)).fetchall()]
    for iid in item_ids:
        recompute_item_horas(db, iid)
    items = db.execute("SELECT * FROM items WHERE obra_id = ? ORDER BY creado_en", (obra_id,)).fetchall()
    total_hh = sum(i["horas_hombre"] for i in items)
    total_hm = sum(i["horas_maquina"] for i in items)
    demoras_por_item = {}
    total_demoras_por_item = {}
    total_demoras_min = 0
    for i in items:
        d_item = db.execute(
            "SELECT * FROM demoras WHERE item_id = ? ORDER BY fecha_inicio", (i["id"],)
        ).fetchall()
        demoras_por_item[i["id"]] = d_item
        item_min = sum(demora_minutos(d) for d in d_item)
        total_demoras_por_item[i["id"]] = fmt_duracion(item_min)
        total_demoras_min += item_min
    gantt_html, curve_html, carga_plan_html, carga_real_html = build_obra_charts(db, items)
    db.close()
    return render_template(
        "reporte_interno.html", obra=obra, items=items, total_hh=total_hh, total_hm=total_hm,
        gantt_html=gantt_html, curve_html=curve_html,
        carga_plan_html=carga_plan_html, carga_real_html=carga_real_html,
        demoras_por_item=demoras_por_item, total_demoras_por_item=total_demoras_por_item,
        total_demoras=fmt_duracion(total_demoras_min),
        generado_en=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )


@app.route("/cosesa/reporte-interno/obra/<int:obra_id>/pdf")
@cosesa_required
def reporte_interno_obra_pdf(obra_id):
    user = get_current_user()
    token = make_pdf_token(user["id"])
    url = url_for("reporte_interno_obra", obra_id=obra_id, pdf_token=token, _external=True)
    out_path = os.path.join(tempfile.gettempdir(), f"reporte_interno_{obra_id}.pdf")
    render_pdf(url, out_path)
    return send_file(out_path, as_attachment=True, download_name=f"reporte_interno_obra_{obra_id}.pdf")


# ---------------------------------------------------------------------------
# Archivos subidos
# ---------------------------------------------------------------------------

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# Se ejecuta siempre al importar el módulo (tanto con `python3 app.py` en
# desarrollo como al arrancar con gunicorn en producción, con uno o varios
# workers). Primero intenta restaurar el último backup guardado en GitHub
# (si el backup está configurado y hay uno disponible) — así, en un hosting
# que borra el disco en cada despliegue, los datos reales sobreviven. Recién
# si no hay nada que restaurar se crea una base de datos vacía. init_db() ya
# es segura para llamar siempre: si la base ya existe no hace nada, y si
# varios workers arrancan a la vez, un lock de archivo evita que choquen.
restore_latest()
init_db()
iniciar_backup_periodico()


def _bootstrap_admin():
    """En un despliegue nuevo (base de datos vacía, sin usuarios) crea el primer
    administrador a partir de variables de entorno, para poder entrar por
    primera vez sin acceso a una consola del servidor. No hace nada si ya
    existe algún usuario, ni si faltan las variables de entorno. Como varios
    workers de gunicorn pueden llegar acá al mismo tiempo, se ignora el error
    si otro worker ya insertó el mismo usuario un instante antes."""
    admin_user = os.environ.get("ADMIN_USERNAME")
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if not (admin_user and admin_pass):
        return
    db = get_db()
    try:
        existe_alguno = db.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone()
        if existe_alguno is not None:
            return
        db.execute(
            "INSERT INTO usuarios (nombre, username, password_hash, rol) VALUES (?, ?, ?, 'cosesa_admin')",
            (os.environ.get("ADMIN_NOMBRE", "Administrador"), admin_user, generate_password_hash(admin_pass)),
        )
        db.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        db.close()


_bootstrap_admin()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True)
