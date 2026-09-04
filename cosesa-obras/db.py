import sqlite3
import os

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
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_db()
    with open(os.path.join(BASE_DIR, "schema.sql"), "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
