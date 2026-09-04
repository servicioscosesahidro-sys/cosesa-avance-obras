"""Carga datos de ejemplo para poder probar la aplicación de punta a punta."""
import os
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash
from db import init_db, get_db, DB_PATH

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
init_db()

db = get_db()


def dt(days_offset, hour=8, minute=0):
    """Fecha y hora de ejemplo, relativa a hoy (formato datetime-local)."""
    base = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (base + timedelta(days=days_offset)).strftime("%Y-%m-%dT%H:%M")

# --- Usuarios de COSESA (empresa): administrador + operador ---
db.execute(
    "INSERT INTO usuarios (nombre, username, password_hash, rol) VALUES (?,?,?,'cosesa_admin')",
    ("Administrador COSESA", "cosesa", generate_password_hash("cosesa123")),
)
db.execute(
    "INSERT INTO usuarios (nombre, username, password_hash, rol) VALUES (?,?,?,'cosesa')",
    ("Operador COSESA", "operador", generate_password_hash("operador123")),
)

# --- Cliente de ejemplo ---
cur = db.execute(
    "INSERT INTO clientes (nombre, cuit, contacto) VALUES (?,?,?)",
    ("Planta Industrial Demo S.A.", "30-12345678-9", "Juan Pérez - Mantenimiento"),
)
cliente_id = cur.lastrowid

db.execute(
    "INSERT INTO usuarios (nombre, username, password_hash, rol, cliente_id) VALUES (?,?,?,'cliente',?)",
    ("Juan Pérez", "cliente", generate_password_hash("cliente123"), cliente_id),
)

cur = db.execute(
    "INSERT INTO obras (cliente_id, nombre, descripcion, ubicacion, fecha_inicio, fecha_fin_estimada, estado) "
    "VALUES (?,?,?,?,?,?, 'en_curso')",
    (
        cliente_id,
        "Parada de planta - Sector caldera",
        "Mantenimiento preventivo y correctivo de equipos rotativos e hidráulicos del sector caldera.",
        "Planta Demo - San Lorenzo, Santa Fe",
        dt(-10, 7, 0),
        dt(10, 18, 0),
    ),
)
obra_id = cur.lastrowid

usuario_cliente_id = db.execute("SELECT id FROM usuarios WHERE username = 'cliente'").fetchone()["id"]
db.execute(
    "INSERT INTO usuario_obras (usuario_id, obra_id) VALUES (?,?)",
    (usuario_cliente_id, obra_id),
)

items_demo = [
    # nombre, tag, descripcion, avance, hh, hm, plan_inicio(dias resp. a hoy), plan_fin(dias)
    ("Bomba centrífuga P-101", "P-101", "Desmontaje, limpieza, cambio de sellos y alineación.", 100, 24, 8, -10, -2),
    ("Válvula de seguridad V-204", "V-204", "Prueba hidráulica y calibración de válvula de seguridad.", 60, 10, 2, -9, 1),
    ("Intercambiador de calor E-305", "E-305", "Limpieza química y prueba de estanqueidad.", 20, 6, 4, -6, 4),
]

user = db.execute("SELECT id FROM usuarios WHERE username = 'cosesa'").fetchone()

for nombre, tag, desc, avance, hh, hm, plan_ini_d, plan_fin_d in items_demo:
    cur = db.execute(
        "INSERT INTO items (obra_id, nombre, tag_equipo, descripcion, fecha_inicio, fecha_inicio_plan, fecha_fin_plan, estado, avance_pct, horas_hombre, horas_maquina, observaciones) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            obra_id, nombre, tag, desc,
            dt(-8, 8, 30),
            dt(plan_ini_d, 8, 0),
            dt(plan_fin_d, 17, 0),
            "finalizado" if avance == 100 else "en_curso",
            avance, hh, hm,
            "Trabajo realizado sin observaciones de seguridad." if avance == 100 else "En progreso, sin novedades.",
        ),
    )
    item_id = cur.lastrowid
    # bitácora de avance
    pasos = [20, 50, 80, 100] if avance == 100 else ([20, 45, 60] if avance == 60 else [10, 20])
    for idx, pct in enumerate(pasos):
        if pct > avance:
            break
        fecha = dt(-8 + idx * 2, 16, 0)
        db.execute(
            "INSERT INTO avance_log (item_id, fecha, avance_pct, comentario, horas_hombre_dia, horas_maquina_dia, usuario_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (item_id, fecha, pct, f"Avance registrado: {pct}% completado.", hh / len(pasos), hm / len(pasos), user["id"]),
        )
    if avance == 100:
        db.execute("UPDATE items SET fecha_fin = ? WHERE id = ?", (dt(-1, 15, 30), item_id))
    if tag == "P-101":
        db.execute(
            "INSERT INTO demoras (item_id, fecha_inicio, fecha_fin, motivo, usuario_id) VALUES (?,?,?,?,?)",
            (item_id, dt(-6, 12, 0), dt(-6, 13, 30), "Espera de repuesto (sellos) en depósito.", user["id"]),
        )

db.commit()
db.close()

print("Base de datos de ejemplo creada con éxito.")
print("Administrador COSESA -> usuario: cosesa / contraseña: cosesa123")
print("Operador COSESA -> usuario: operador / contraseña: operador123")
print("Usuario CLIENTE -> usuario: cliente / contraseña: cliente123")
