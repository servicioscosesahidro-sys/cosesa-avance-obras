"""Conexión Mantenimiento ⇄ Tareas de Taller.

La app Tareas de Taller sigue funcionando igual (Netlify + Firebase). Desde la
plataforma solo se hacen tres cosas, todas desde el servidor:

  * leer la lista de Equipos, Personal y Áreas (para vincular unidades y armar
    el formulario),
  * CREAR una tarea nueva en la sección "Tareas Taller" (colección `tareas`),
    con el mismo formato y la misma numeración (meta/counter) que usa la app,
  * leer el avance de las tareas que se crearon desde una orden.

Nunca edita ni borra tareas existentes. Usa las mismas variables de entorno
que el respaldo (TAREAS_FIREBASE_APIKEY / TAREAS_FIREBASE_URL).
"""
import os
import threading
import time
from datetime import datetime

import requests

import tareas_taller_backup as ttb

_tok = {"token": None, "vence": 0}
_tok_lock = threading.Lock()
_cache = {"t": 0, "data": None}
_num_lock = threading.Lock()
H = {"Referer": ttb.REFERER, "Origin": ttb.REFERER.rstrip("/")}


class TTError(Exception):
    pass


def _cfg():
    cfg = ttb._cfg()
    if cfg is None:
        raise TTError("La conexión con Tareas de Taller no está configurada en el servidor.")
    return cfg


def _token(cfg, forzar=False):
    with _tok_lock:
        if not forzar and _tok["token"] and time.time() < _tok["vence"]:
            return _tok["token"]
        r = requests.post(os.environ.get("TAREAS_FIREBASE_AUTH_URL", "https://identitytoolkit.googleapis.com/v1/accounts:signUp"),
                          params={"key": cfg["key"]}, json={"returnSecureToken": True}, headers=H, timeout=20)
        if r.status_code != 200:
            raise TTError(f"Tareas de Taller no dejó entrar ({r.status_code}).")
        d = r.json()
        _tok["token"] = d["idToken"]
        _tok["vence"] = time.time() + int(d.get("expiresIn", 3600)) - 300
        return _tok["token"]


def _req(metodo, ruta, **kw):
    cfg = _cfg()
    base_params = dict(kw.pop("params", {}) or {})
    extra_headers = dict(kw.pop("headers", {}) or {})
    for intento in (0, 1):
        params = dict(base_params)
        params["auth"] = _token(cfg, forzar=intento == 1)
        headers = dict(H)
        headers.update(extra_headers)
        r = requests.request(metodo, f"{cfg['url']}/{ruta}.json", params=params, headers=headers, timeout=30, **kw)
        if r.status_code == 401 and intento == 0:
            continue
        return r
    return r


def catalogo(refrescar=False):
    if not refrescar and _cache["data"] and time.time() - _cache["t"] < 120:
        return _cache["data"]
    def leer(ruta):
        r = _req("GET", ruta)
        if r.status_code != 200:
            raise TTError(f"No se pudo leer {ruta} de Tareas de Taller ({r.status_code}).")
        return r.json() or {}
    eq, pe, ar = leer("equipos"), leer("personal"), leer("meta/areas")
    data = {
        "equipos": sorted([{"id": k, "codigo": v.get("codigo", ""), "titulo": v.get("titulo", ""), "tipo": v.get("observaciones", "")}
                           for k, v in eq.items() if isinstance(v, dict)], key=lambda x: (x["tipo"], x["titulo"])),
        "personal": sorted([{"id": k, "nombre": v.get("nombre", ""), "area": v.get("area", "")}
                            for k, v in pe.items() if isinstance(v, dict) and v.get("activo", True) is not False],
                           key=lambda x: x["nombre"]),
        "areas": (ar.get("lista") if isinstance(ar, dict) else None) or [],
    }
    _cache.update(t=time.time(), data=data)
    return data


def _proximo_numero():
    """Mismo formato que la app: AAAA + correlativo de 4 dígitos (meta/counter).
    Usa ETag para no pisar un número que se asignó al mismo tiempo desde la app."""
    year = datetime.now().year
    with _num_lock:
        for _ in range(5):
            r = _req("GET", "meta/counter", headers={"X-Firebase-ETag": "true"})
            if r.status_code != 200:
                raise TTError("No se pudo leer la numeración de Tareas de Taller.")
            etag = r.headers.get("ETag")
            d = r.json() or {}
            seq = d.get("seq") if d.get("year") == year and isinstance(d.get("seq"), int) else 1
            w = _req("PUT", "meta/counter", json={"year": year, "seq": seq + 1}, headers={"if-match": etag} if etag else {})
            if w.status_code == 200:
                return int(f"{year}{seq:04d}")
            if w.status_code != 412:
                raise TTError(f"No se pudo reservar el número de tarea ({w.status_code}).")
        raise TTError("No se pudo reservar el número de tarea (muchos cambios a la vez). Probá de nuevo.")


def crear_tarea(datos, creado_por):
    titulo = (datos.get("titulo") or "").strip()
    if not titulo:
        raise TTError("La tarea necesita un título.")
    try:
        prioridad = min(5, max(1, int(datos.get("prioridad") or 3)))
    except (TypeError, ValueError):
        prioridad = 3
    numero = _proximo_numero()
    ahora = int(time.time() * 1000)
    tarea = {
        "numero": numero,
        "prioridad": prioridad,
        "titulo": titulo[:200],
        "observaciones": (datos.get("observaciones") or "")[:4000],
        "fechaObjetivo": datos.get("fechaObjetivo") or None,
        "responsableId": datos.get("responsableId") or "",
        "area": datos.get("area") or "Mantenimiento",
        "equipoId": datos.get("equipoId") or None,
        "avance": 0,
        "createdAt": ahora,
        "updatedAt": ahora,
        "createdBy": creado_por,
        "nueva": True,
        "ordenMantenimiento": (datos.get("orden") or "")[:40],
    }
    tarea = {k: v for k, v in tarea.items() if v is not None}
    r = _req("POST", "tareas", json=tarea)
    if r.status_code != 200:
        raise TTError(f"No se pudo crear la tarea ({r.status_code}).")
    return {"id": r.json().get("name"), "numero": numero}


def estado_tareas(ids):
    out = {}
    for i in ids[:60]:
        if not isinstance(i, str) or not i or "/" in i or "." in i:
            continue
        r = _req("GET", f"tareas/{i}")
        if r.status_code == 200:
            t = r.json()
            out[i] = None if t is None else {"numero": t.get("numero"), "avance": t.get("avance", 0),
                                             "archivada": bool(t.get("archivada")), "titulo": t.get("titulo", "")}
    return out


def init_app(app, h):
    from flask import jsonify, request

    def _usuario_ok():
        user = h.get_current_user()
        return user if user and h.puede("modulo_mantenimiento", user) else None

    def _err(e, code=502):
        return jsonify({"ok": False, "error": str(e)}), code

    @app.route("/mantenimiento/api/tt/catalogo")
    @h.cosesa_required
    def mantenimiento_tt_catalogo():
        if not _usuario_ok():
            return _err("Sin permiso.", 403)
        try:
            return jsonify({"ok": True, **catalogo(request.args.get("refrescar") == "1")})
        except TTError as e:
            return _err(e)
        except Exception:  # noqa: BLE001
            return _err("No se pudo conectar con Tareas de Taller.")

    @app.route("/mantenimiento/api/tt/tareas", methods=["POST"])
    @h.cosesa_required
    def mantenimiento_tt_crear():
        user = _usuario_ok()
        if not user:
            return _err("Sin permiso.", 403)
        try:
            nombre = user["nombre"] or user["username"]
            res = crear_tarea(request.get_json(silent=True) or {}, f"{nombre} (Mantenimiento)")
            return jsonify({"ok": True, **res})
        except TTError as e:
            return _err(e)
        except Exception:  # noqa: BLE001
            return _err("No se pudo conectar con Tareas de Taller.")

    @app.route("/mantenimiento/api/tt/estado", methods=["POST"])
    @h.cosesa_required
    def mantenimiento_tt_estado():
        if not _usuario_ok():
            return _err("Sin permiso.", 403)
        try:
            ids = (request.get_json(silent=True) or {}).get("ids") or []
            return jsonify({"ok": True, "tareas": estado_tareas(ids)})
        except TTError as e:
            return _err(e)
        except Exception:  # noqa: BLE001
            return _err("No se pudo conectar con Tareas de Taller.")
