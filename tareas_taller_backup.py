"""Copia de seguridad diaria de los DATOS de la app "Tareas de Taller".

La app Tareas de Taller (https://tareastaller.netlify.app) guarda sus datos en
Firebase (Realtime Database). Este módulo, una vez por día de madrugada, baja
TODA la base de Firebase en formato JSON y la sube al repositorio PRIVADO de
backups de GitHub (el mismo que usa backup.py), en:

    tareas-taller/datos/latest.json         (siempre la última)
    tareas-taller/datos/AAAA-MM-DD.json     (una por día, historial)

Solo LEE Firebase: nunca escribe ni borra nada allá.

Variables de entorno (en Render):
  TAREAS_FIREBASE_APIKEY  -> "apiKey" de la configuración de Firebase de la app
  TAREAS_FIREBASE_URL     -> "databaseURL" de la configuración de Firebase
  TAREAS_RESPALDO_HORA    -> hora de Argentina (opcional, default 3)
Además usa GITHUB_BACKUP_TOKEN / GITHUB_BACKUP_REPO de backup.py.
"""
import base64
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from db import DATA_DIR

TZ_AR = timezone(timedelta(hours=-3))
ESTADO_PATH = os.path.join(DATA_DIR, "tareas_taller_respaldo.json")
REFERER = "https://tareastaller.netlify.app/"
_lock = threading.Lock()


def _cfg():
    key = os.environ.get("TAREAS_FIREBASE_APIKEY", "").strip()
    url = os.environ.get("TAREAS_FIREBASE_URL", "").strip().rstrip("/")
    if not (key and url):
        return None
    try:
        hora = int(os.environ.get("TAREAS_RESPALDO_HORA", "3"))
    except ValueError:
        hora = 3
    return {"key": key, "url": url, "hora": hora}


def estado():
    try:
        with open(ESTADO_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _guardar_estado(d):
    try:
        with open(ESTADO_PATH, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
    except Exception:
        pass


def descargar(cfg):
    """Entra a Firebase como usuario anónimo (igual que la app) y baja todo."""
    h = {"Referer": REFERER, "Origin": REFERER.rstrip("/")}
    r = requests.post("https://identitytoolkit.googleapis.com/v1/accounts:signUp",
                      params={"key": cfg["key"]}, json={"returnSecureToken": True}, headers=h, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Firebase no dejó entrar ({r.status_code}): {r.text[:200]}")
    token = r.json().get("idToken")
    r = requests.get(cfg["url"] + "/.json", params={"auth": token}, headers=h, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Firebase no dejó leer los datos ({r.status_code}): {r.text[:200]}")
    datos = r.json()
    if not isinstance(datos, dict) or not datos:
        raise RuntimeError("Firebase devolvió la base vacía: no se sube nada para no pisar un respaldo bueno.")
    return datos


def _resumen(datos):
    def n(k):
        v = datos.get(k)
        return len(v) if isinstance(v, dict) else 0
    subt = sum(len(v) for v in (datos.get("subtareas_de_tareas") or {}).values() if isinstance(v, dict))
    return {"tareas": n("tareas"), "subtareas": subt, "personal": n("personal"), "equipos": n("equipos"),
            "tareas_privadas": n("tareas_gf")}


def _subir(gh, ruta, contenido, mensaje):
    import backup
    url = f"{backup.GITHUB_API}/repos/{gh['repo']}/contents/{ruta}"
    headers = backup._headers(gh["token"])
    sha = None
    r = requests.get(url, headers=headers, params={"ref": gh["branch"]}, timeout=30)
    if r.status_code == 200:
        sha = r.json().get("sha")
    payload = {"message": mensaje, "content": base64.b64encode(contenido).decode("ascii"), "branch": gh["branch"]}
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, json=payload, timeout=120)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub respondió {r.status_code}: {r.text[:200]}")


def respaldar(origen="automatico"):
    """Devuelve (ok, mensaje)."""
    import backup
    cfg, gh = _cfg(), backup._config()
    if cfg is None:
        return False, "Falta configurar TAREAS_FIREBASE_APIKEY y TAREAS_FIREBASE_URL en Render."
    if gh is None:
        return False, "Falta la configuración del repositorio de backups de GitHub."
    if not _lock.acquire(blocking=False):
        return False, "Ya hay un respaldo en curso."
    ahora = datetime.now(TZ_AR)
    est = estado()
    try:
        datos = descargar(cfg)
        res = _resumen(datos)
        contenido = json.dumps(datos, ensure_ascii=False, indent=1).encode("utf-8")
        msg = f"Tareas de Taller: respaldo de datos {ahora:%Y-%m-%d %H:%M} ({origen})"
        _subir(gh, f"tareas-taller/datos/{ahora:%Y-%m-%d}.json", contenido, msg)
        _subir(gh, "tareas-taller/datos/latest.json", contenido, msg)
        est.update({"ultimo_ok": ahora.isoformat(timespec="minutes"), "ultimo_dia_auto": ahora.date().isoformat() if origen == "automatico" else est.get("ultimo_dia_auto"),
                    "resumen": res, "bytes": len(contenido), "ultimo_error": None, "origen": origen})
        _guardar_estado(est)
        return True, (f"Respaldo subido: {res['tareas']} tareas, {res['subtareas']} subtareas, "
                      f"{res['personal']} personas, {res['equipos']} equipos ({round(len(contenido)/1024)} KB).")
    except Exception as e:  # noqa: BLE001
        est.update({"ultimo_error": f"{ahora:%Y-%m-%d %H:%M}: {e}"})
        if origen == "automatico":
            est["ultimo_intento_auto"] = ahora.date().isoformat()
        _guardar_estado(est)
        return False, str(e)
    finally:
        _lock.release()


def iniciar_programador():
    """Una vez por día, después de la hora configurada (hora de Argentina)."""
    if _cfg() is None:
        return

    def loop():
        time.sleep(120)
        while True:
            try:
                cfg = _cfg()
                ahora = datetime.now(TZ_AR)
                hoy = ahora.date().isoformat()
                est = estado()
                if cfg and ahora.hour >= cfg["hora"] and est.get("ultimo_dia_auto") != hoy:
                    # si falló, reintenta como mucho cada 30 minutos
                    if est.get("ultimo_intento_auto") != hoy or (time.time() - os.path.getmtime(ESTADO_PATH)) > 1800:
                        respaldar("automatico")
            except Exception:
                pass
            time.sleep(300)

    threading.Thread(target=loop, name="tareas-taller-respaldo", daemon=True).start()


def init_app(app, h):
    from flask import flash, redirect, render_template, request, url_for

    @app.route("/cosesa/tareas-taller/respaldo", methods=["GET", "POST"])
    @h.admin_required
    def tareas_taller_respaldo():
        if request.method == "POST":
            ok, msg = respaldar("manual")
            flash(msg, "success" if ok else "error")
            return redirect(url_for("tareas_taller_respaldo"))
        return render_template("tareas_taller_respaldo.html", est=estado(), configurado=_cfg() is not None,
                               hora=(_cfg() or {}).get("hora", 3))
