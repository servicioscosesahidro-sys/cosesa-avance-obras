"""Backups automáticos de la base de datos y las fotos hacia un repositorio
PRIVADO de GitHub aparte, usando la API de contenidos de GitHub (sin
necesitar git instalado). Esto resuelve dos problemas a la vez:

1. Sirve como copia de seguridad externa, por si algo sale mal.
2. Como el hosting gratuito (Render) borra el disco en cada actualización o
   reinicio de la app, al arrancar la app intenta restaurar automáticamente
   el último backup disponible ANTES de crear una base de datos vacía — así
   los datos sobreviven sin pagar un disco persistente.

El backup se dispara solo, unos segundos después de cada cambio en los datos
(se carga un avance, se edita un ítem, se sube una foto, etc.), así que ante
un reinicio se pierde, como mucho, lo de los últimos segundos. Además hay un
backup de seguridad cada 20 minutos aunque no se detecten cambios.

Variables de entorno necesarias:
  GITHUB_BACKUP_TOKEN  -> token de acceso personal con permiso de escritura
                          sobre el repo de backups (scope "repo").
  GITHUB_BACKUP_REPO   -> "usuario/repositorio", ej:
                          "servicioscosesahidro-sys/cosesa-avance-obras-backups"
  GITHUB_BACKUP_BRANCH -> rama a usar (opcional, default "main").

Si no están configuradas, todas las funciones de este módulo no hacen nada
— la app sigue funcionando igual que antes, solo sin backups.
"""
import base64
import io
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile

import requests

from db import DATA_DIR, DB_PATH

GITHUB_API = "https://api.github.com"
BACKUP_PATH_IN_REPO = "backup/latest.zip"
INTERVALO_BACKUP_SEGUNDOS = 20 * 60   # backup de seguridad aunque no haya cambios
REVISION_SEGUNDOS = 5                 # cada cuánto se mira si hubo cambios
QUIETO_SEGUNDOS = 8                   # se espera a que los cambios "se calmen"
ESPERA_MAXIMA_SEGUNDOS = 60           # tope si hay cambios continuos

_backup_lock = threading.Lock()


def _config():
    token = os.environ.get("GITHUB_BACKUP_TOKEN")
    repo = os.environ.get("GITHUB_BACKUP_REPO")
    branch = os.environ.get("GITHUB_BACKUP_BRANCH", "main")
    if not (token and repo):
        return None
    return {"token": token, "repo": repo, "branch": branch}


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _snapshot_db(dest_path):
    """Copia consistente de la base de datos (segura aunque otra conexión
    esté escribiendo en ese momento), usando la API de backup de SQLite."""
    src = sqlite3.connect(DB_PATH, timeout=30)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _zip_data_dir():
    """Comprime toda la carpeta DATA_DIR (base de datos + fotos subidas) en
    un .zip en memoria y devuelve los bytes."""
    buf = io.BytesIO()
    db_abs = os.path.abspath(DB_PATH)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(DATA_DIR):
                for name in files:
                    if name == ".init.lock" or name.endswith(("-wal", "-shm", "-journal")):
                        continue
                    full_path = os.path.join(root, name)
                    rel_path = os.path.relpath(full_path, DATA_DIR)
                    if os.path.abspath(full_path) == db_abs:
                        snap = os.path.join(tmp, "snapshot.db")
                        _snapshot_db(snap)
                        zf.write(snap, rel_path)
                    else:
                        try:
                            zf.write(full_path, rel_path)
                        except FileNotFoundError:
                            pass  # la borraron justo en este momento
    buf.seek(0)
    return buf.read()


def backup_now():
    """Sube un .zip con el estado actual de DATA_DIR al repo de backups.
    Devuelve (ok: bool, mensaje: str)."""
    cfg = _config()
    if cfg is None:
        return False, "El backup automático no está configurado (faltan variables de entorno)."
    if not os.path.exists(DB_PATH):
        return False, "Todavía no hay base de datos para respaldar."

    with _backup_lock:
        try:
            zip_bytes = _zip_data_dir()
            url = f"{GITHUB_API}/repos/{cfg['repo']}/contents/{BACKUP_PATH_IN_REPO}"
            headers = _headers(cfg["token"])

            # Hace falta el sha del archivo anterior para poder sobreescribirlo.
            sha = None
            r = requests.get(url, headers=headers, params={"ref": cfg["branch"]}, timeout=30)
            if r.status_code == 200:
                sha = r.json().get("sha")

            payload = {
                "message": f"backup automático ({time.strftime('%Y-%m-%d %H:%M:%S')})",
                "content": base64.b64encode(zip_bytes).decode("ascii"),
                "branch": cfg["branch"],
            }
            if sha:
                payload["sha"] = sha

            r = requests.put(url, headers=headers, json=payload, timeout=60)
            if r.status_code in (200, 201):
                return True, "Backup subido correctamente."
            return False, f"GitHub respondió {r.status_code}: {r.text[:300]}"
        except Exception as e:
            return False, f"Error al hacer el backup: {e}"


def restore_latest():
    """Si todavía no hay base de datos local, intenta bajar y descomprimir
    el último backup disponible. No hace nada si ya existe una base de datos,
    si no está configurado el backup, o si no hay ningún backup guardado
    todavía (primera vez)."""
    if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
        return False, "Ya había una base de datos local, no se restauró nada."
    cfg = _config()
    if cfg is None:
        return False, "El backup automático no está configurado."

    try:
        url = f"{GITHUB_API}/repos/{cfg['repo']}/contents/{BACKUP_PATH_IN_REPO}"
        headers = _headers(cfg["token"])
        r = requests.get(url, headers=headers, params={"ref": cfg["branch"]}, timeout=30)
        if r.status_code == 404:
            return False, "Todavía no hay ningún backup guardado (primera vez)."
        if r.status_code != 200:
            return False, f"GitHub respondió {r.status_code} al buscar el backup."

        zip_bytes = base64.b64decode(r.json()["content"])
        os.makedirs(DATA_DIR, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "backup.zip")
            with open(zip_path, "wb") as f:
                f.write(zip_bytes)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(DATA_DIR)
        return True, "Backup restaurado correctamente."
    except Exception as e:
        return False, f"Error al restaurar el backup: {e}"


def _firma_datos():
    """Huella de la carpeta de datos: cambia cada vez que se modifica la base
    de datos o se agrega/borra una foto. Devuelve (mtime_máximo, cantidad)."""
    ultimo = 0.0
    cantidad = 0
    for root, _dirs, files in os.walk(DATA_DIR):
        for name in files:
            if name == ".init.lock":
                continue
            try:
                m = os.path.getmtime(os.path.join(root, name))
            except OSError:
                continue
            cantidad += 1
            if m > ultimo:
                ultimo = m
    return ultimo, cantidad


def iniciar_backup_periodico():
    """Arranca un hilo en segundo plano que vigila la carpeta de datos: apenas
    detecta cambios (y se calman unos segundos) hace un backup. Además hace uno
    cada 20 minutos de todas formas. No hace nada si no está configurado."""
    if _config() is None:
        return

    def loop():
        ultima_firma = _firma_datos()   # lo que ya existe al arrancar no se re-sube
        ultimo_backup = time.time()
        cambio_desde = None             # desde cuándo hay cambios pendientes
        ultimo_cambio_visto = None
        while True:
            time.sleep(REVISION_SEGUNDOS)
            try:
                ahora = time.time()
                firma = _firma_datos()
                if firma != ultima_firma:
                    if cambio_desde is None:
                        cambio_desde = ahora
                    ultimo_cambio_visto = ahora
                    ultima_firma = firma

                hay_pendiente = cambio_desde is not None
                calmado = hay_pendiente and (ahora - ultimo_cambio_visto) >= QUIETO_SEGUNDOS
                demasiado = hay_pendiente and (ahora - cambio_desde) >= ESPERA_MAXIMA_SEGUNDOS
                toca_seguridad = (ahora - ultimo_backup) >= INTERVALO_BACKUP_SEGUNDOS

                if calmado or demasiado or toca_seguridad:
                    ok, _msg = backup_now()
                    if ok:
                        ultimo_backup = time.time()
                        cambio_desde = None
                        ultimo_cambio_visto = None
                        # el propio backup no modifica datos, pero por las dudas:
                        ultima_firma = _firma_datos()
                    else:
                        # reintenta en el próximo ciclo sin perder lo pendiente
                        if cambio_desde is None:
                            cambio_desde = ahora
                        ultimo_cambio_visto = ahora + 30  # espera ~40s antes de reintentar
            except Exception:
                pass

    hilo = threading.Thread(target=loop, daemon=True)
    hilo.start()
