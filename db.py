import sqlite3
import os
import fcntl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR permite apuntar la base de datos a un disco persistente montado
# aparte (por ejemplo en un hosting como Render). Si no se define, se guarda
# dentro del proyecto como hasta ahora.
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "instance"))
DB_PATH = os.path.join(DATA_DIR, "cosesa.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_exists(conn, table, column):
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def _column_is_not_null(conn, table, column):
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if row[1] == column:
            return bool(row[3])
    return False


def _migrate_schema(conn):
    """Ajustes al esquema para bases de datos creadas con una versión anterior
    de schema.sql, sin perder los datos que ya tengan cargados. Se puede
    llamar las veces que sea: cada paso primero revisa si hace falta."""
    changed = False

    if not _column_exists(conn, "items", "pausado_en"):
        conn.execute("ALTER TABLE items ADD COLUMN pausado_en TEXT")
        changed = True

    if _column_is_not_null(conn, "demoras", "fecha_fin"):
        # SQLite no permite quitar un NOT NULL con ALTER TABLE: se recrea la
        # tabla con el nuevo esquema y se copian los datos existentes.
        conn.executescript(
            """
            CREATE TABLE demoras_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                fecha_inicio TEXT NOT NULL,
                fecha_fin TEXT,
                motivo TEXT,
                usuario_id INTEGER REFERENCES usuarios(id),
                creado_en TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO demoras_new (id, item_id, fecha_inicio, fecha_fin, motivo, usuario_id, creado_en)
                SELECT id, item_id, fecha_inicio, fecha_fin, motivo, usuario_id, creado_en FROM demoras;
            DROP TABLE demoras;
            ALTER TABLE demoras_new RENAME TO demoras;
            """
        )
        changed = True

    if changed:
        conn.commit()


def init_db():
    """Crea la base de datos si todavía no existe, y aplica migraciones
    livianas si ya existe pero es de una versión anterior. Con gunicorn
    corriendo varios workers, más de un proceso puede llamar a esto al mismo
    tiempo en el arranque — se usa un lock de archivo para que solo uno
    escriba a la vez, y el resto no choquen entre sí."""
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_path = os.path.join(DATA_DIR, ".init.lock")
    with open(lock_path, "w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            need_create = not (os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0)
            conn = get_db()
            if need_create:
                with open(os.path.join(BASE_DIR, "schema.sql"), "r", encoding="utf-8") as f:
                    conn.executescript(f.read())
                conn.commit()
            _migrate_schema(conn)
            conn.close()
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)
