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


def init_db():
    """Crea la base de datos si todavía no existe. Con gunicorn corriendo
    varios workers, más de un proceso puede llamar a esto al mismo tiempo en
    el arranque — se usa un lock de archivo para que solo uno cree las
    tablas, y el resto no choquen contra un "table already exists"."""
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_path = os.path.join(DATA_DIR, ".init.lock")
    with open(lock_path, "w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
                return
            conn = get_db()
            with open(os.path.join(BASE_DIR, "schema.sql"), "r", encoding="utf-8") as f:
                conn.executescript(f.read())
            conn.commit()
            conn.close()
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)
