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
import json
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
INTERVALO_BACKUP_SEGUNDOS = 12 * 3600  # backup de seguridad aunque no haya cambios (ya se hace uno con cada cambio)
REVISION_SEGUNDOS = 5                 # cada cuánto se mira si hubo cambios
QUIETO_SEGUNDOS = 8                   # se espera a que los cambios "se calmen"
ESPERA_MAXIMA_SEGUNDOS = 60           # tope si hay cambios continuos

_backup_lock = threading.Lock()
# Si el último intento de restaurar falló (y había un backup), NO se sube ningún
# backup nuevo: una base vacía no debe pisar el backup bueno.
_restore_fallo = False


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


def _zip_data_dir(dest_zip):
    """Comprime toda la carpeta DATA_DIR (base de datos + fotos subidas) en
    el archivo dest_zip. Se escribe en disco, no en memoria: con las fotos el
    zip pesa decenas de MB y armarlo en memoria (más su copia en base64 para
    GitHub) hacía que el servidor se quedara sin memoria y se reiniciara."""
    db_abs = os.path.abspath(DB_PATH)
    dest_abs = os.path.abspath(dest_zip)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(DATA_DIR):
                for name in files:
                    if name == ".init.lock" or name.endswith(("-wal", "-shm", "-journal")):
                        continue
                    full_path = os.path.join(root, name)
                    if os.path.abspath(full_path) == dest_abs:
                        continue
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


def _cuerpo_put(zip_path, dest_json, message, branch, sha):
    """Arma en disco el JSON que espera la API de GitHub, pasando el zip a
    base64 de a pedazos (así nunca está entero en memoria)."""
    with open(dest_json, "wb") as out:
        out.write(b'{"message": ' + json.dumps(message).encode() + b', "branch": ' + json.dumps(branch).encode())
        if sha:
            out.write(b', "sha": ' + json.dumps(sha).encode())
        out.write(b', "content": "')
        with open(zip_path, "rb") as f:
            while True:
                pedazo = f.read(3 * 1024 * 1024)   # múltiplo de 3: el base64 queda continuo
                if not pedazo:
                    break
                out.write(base64.b64encode(pedazo))
        out.write(b'"}')


def _log(msg):
    print("[backup] " + msg, flush=True)


ultimo_resultado = {"ok": None, "mensaje": "", "cuando": None, "mb": None}


def backup_now():
    """Sube un .zip con el estado actual de DATA_DIR al repo de backups.
    Devuelve (ok: bool, mensaje: str)."""
    cfg = _config()
    if cfg is None:
        return False, "El backup automático no está configurado (faltan variables de entorno)."
    if not os.path.exists(DB_PATH):
        return False, "Todavía no hay base de datos para respaldar."
    if _restore_fallo:
        return False, "No se sube backup: falló la restauración y no se quiere pisar el backup bueno con una base vacía."
    try:
        _c = sqlite3.connect(DB_PATH, timeout=30)
        try:
            n_obras = _c.execute("SELECT COUNT(*) FROM obras").fetchone()[0]
        finally:
            _c.close()
    except Exception:
        n_obras = 0
    if n_obras == 0:
        return False, "La base no tiene ninguna obra: no se sube backup (para no pisar uno bueno con una base vacía)."

    with _backup_lock:
        ok, mensaje, mb = False, "", None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = os.path.join(tmp, "latest.zip")
                _zip_data_dir(zip_path)
                mb = round(os.path.getsize(zip_path) / 1048576, 1)
                url = f"{GITHUB_API}/repos/{cfg['repo']}/contents/{BACKUP_PATH_IN_REPO}"
                headers = _headers(cfg["token"])

                # Hace falta el sha del archivo anterior para poder sobreescribirlo
                # (con archivos de más de 1 MB GitHub no manda el contenido, solo los datos).
                sha = None
                r = requests.get(url, headers=headers, params={"ref": cfg["branch"]}, timeout=30)
                if r.status_code == 200:
                    sha = r.json().get("sha")

                body_path = os.path.join(tmp, "body.json")
                _cuerpo_put(zip_path, body_path, f"backup automático ({time.strftime('%Y-%m-%d %H:%M:%S')})", cfg["branch"], sha)
                h = dict(headers)
                h["Content-Type"] = "application/json"
                with open(body_path, "rb") as body:
                    r = requests.put(url, headers=h, data=body, timeout=300)
                if r.status_code in (200, 201):
                    ok, mensaje = True, f"Backup subido correctamente ({mb} MB)."
                else:
                    mensaje = f"GitHub respondió {r.status_code}: {r.text[:300]}"
        except Exception as e:
            mensaje = f"Error al hacer el backup: {e}"
        ultimo_resultado.update(ok=ok, mensaje=mensaje, cuando=time.strftime("%Y-%m-%d %H:%M:%S"), mb=mb)
        _log(mensaje)
        return ok, mensaje


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
        global _restore_fallo
        url = f"{GITHUB_API}/repos/{cfg['repo']}/contents/{BACKUP_PATH_IN_REPO}"
        headers = _headers(cfg["token"])
        # Accept "raw": devuelve el archivo tal cual (hasta 100 MB). Con el
        # formato normal, GitHub devuelve el contenido VACÍO si pesa más de 1 MB,
        # y la restauración fallaba en silencio dejando la base vacía.
        headers_raw = dict(headers)
        headers_raw["Accept"] = "application/vnd.github.raw+json"
        r = requests.get(url, headers=headers_raw, params={"ref": cfg["branch"]}, timeout=300, stream=True)
        if r.status_code == 404:
            return False, "Todavía no hay ningún backup guardado (primera vez)."
        if r.status_code != 200:
            _restore_fallo = True
            return False, f"GitHub respondió {r.status_code} al buscar el backup."

        os.makedirs(DATA_DIR, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "backup.zip")
            with open(zip_path, "wb") as f:
                for pedazo in r.iter_content(1024 * 1024):
                    f.write(pedazo)
            if os.path.getsize(zip_path) == 0:
                _restore_fallo = True
                return False, "El backup bajó vacío."
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(DATA_DIR)
        return True, "Backup restaurado correctamente."
    except Exception as e:
        _restore_fallo = True
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
    cada 12 horas de todas formas. No hace nada si no está configurado."""
    if _config() is None:
        return

    def loop():
        ultima_firma = _firma_datos()   # lo que ya existe al arrancar no se re-sube
        ultimo_backup = time.time()
        cambio_desde = None             # desde cuándo hay cambios pendientes
        ultimo_cambio_visto = None
        proximo_intento = 0             # después de un backup fallido se espera antes de reintentar
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

                if (calmado or demasiado or toca_seguridad) and ahora >= proximo_intento:
                    ok, _msg = backup_now()
                    if ok:
                        ultimo_backup = time.time()
                        cambio_desde = None
                        ultimo_cambio_visto = None
                        # el propio backup no modifica datos, pero por las dudas:
                        ultima_firma = _firma_datos()
                    else:
                        # no pierde lo pendiente, pero espera 5 minutos antes de reintentar
                        # (antes reintentaba cada 5 segundos y saturaba el servidor)
                        if cambio_desde is None:
                            cambio_desde = ahora
                        proximo_intento = time.time() + 300
            except Exception:
                pass

    hilo = threading.Thread(target=loop, daemon=True)
    hilo.start()
