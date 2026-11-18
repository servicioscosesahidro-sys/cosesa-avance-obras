"""Sincronización automática con Easytrack (rastreo satelital).

Todas las madrugadas (por defecto a las 04:00, hora de Argentina) el servidor:
  1. inicia sesión en Easytrack con el usuario configurado,
  2. pide el "Reporte Kilómetros/Horas" de los últimos días (hasta ayer),
  3. suma a cada unidad de Mantenimiento (por patente) los km y las horas de
     motor de cada día que todavía no tenía. Si un día ya estaba, solo corrige
     la diferencia: nunca se suma dos veces.

Configuración (variables de entorno en Render; las claves NO van en el código):
  EASYTRACK_USUARIO, EASYTRACK_CLAVE   -> usuario y contraseña de Easytrack
  EASYTRACK_HORA (opcional, 0-23)      -> hora de la corrida, por defecto 4
  EASYTRACK_DIAS (opcional)            -> días hacia atrás que revisa, por defecto 7
  EASYTRACK_API  (opcional)            -> por defecto https://avlti2.easytrack.com.ar/
Sin usuario y clave no hace nada.

Ojo: Easytrack no publica una API oficial; esto usa el mismo servicio que su
página web. Si Easytrack cambia su página, la sincronización puede fallar: el
error queda registrado y se ve en Mantenimiento → Máquinas y vehículos.
"""
import io
import json
import os
import re
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

import requests

from db import get_db

TZ_AR = timezone(timedelta(hours=-3))
UNIDADES_CONOCIDAS = {
    "AA953OH": "Berlingo Uno", "AF134IJ": "Berlingo Dos", "AF799IF": "Berlingo Tres", "AG899RR": "Kangoo RR",
    "MZF033": "Mercedes Sprinter", "AC285LK": "Diecisiete Veintiuno",
}
_lock = threading.Lock()


def _cfg():
    usuario = os.environ.get("EASYTRACK_USUARIO", "").strip()
    clave = os.environ.get("EASYTRACK_CLAVE", "")
    if not usuario or not clave:
        return None
    try:
        hora = min(23, max(0, int(os.environ.get("EASYTRACK_HORA", "4"))))
    except ValueError:
        hora = 4
    try:
        dias = min(60, max(1, int(os.environ.get("EASYTRACK_DIAS", "7"))))
    except ValueError:
        dias = 7
    api = os.environ.get("EASYTRACK_API", "https://avlti2.easytrack.com.ar/").strip()
    if not api.endswith("/"):
        api += "/"
    return {"usuario": usuario, "clave": clave, "hora": hora, "dias": dias, "api": api}


def configurado():
    return _cfg() is not None


def migrate(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS easytrack_corridas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,             -- día de la corrida programada (AAAA-MM-DD) o 'manual'
            inicio TEXT DEFAULT (datetime('now')),
            fin TEXT,
            ok INTEGER,
            detalle TEXT,
            origen TEXT                       -- 'automatica' o el usuario que la pidió
        );
        CREATE UNIQUE INDEX IF NOT EXISTS easytrack_corridas_auto
            ON easytrack_corridas(fecha) WHERE origen = 'automatica';
        """
    )


# ----------------------------------------------------------------- utilidades
def norm_patente(p):
    return re.sub(r"[^A-Z0-9]", "", str(p or "").upper())


def a_horas(v):
    """'01:49:04' / '1 day 02:00:00' / segundos / fracción de día -> horas (float)."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, timedelta):
        return v.total_seconds() / 3600
    if hasattr(v, "hour") and hasattr(v, "minute") and not isinstance(v, (datetime, date)):
        return v.hour + v.minute / 60 + getattr(v, "second", 0) / 3600
    if isinstance(v, (int, float)):
        return float(v) * 24 if 0 <= float(v) < 1.5 else float(v) / 3600
    s = str(v).strip()
    dias = 0
    m = re.match(r"^(\d+)\s*(?:days|day|días|día|d)[, ]*\s*(.*)$", s, re.I)
    if m:
        dias, s = int(m.group(1)), m.group(2).strip()
        if not s:
            return dias * 24.0
    m = re.match(r"^(\d+):(\d{1,2})(?::(\d{1,2}(?:\.\d+)?))?$", s)
    if m:
        return dias * 24 + int(m.group(1)) + int(m.group(2)) / 60 + float(m.group(3) or 0) / 3600
    try:
        return dias * 24 + float(s.replace(",", "."))
    except ValueError:
        return 0.0


def a_km(v):
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def a_fecha(v):
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


# ------------------------------------------------------- leer archivo exportado
def _norm_txt(t):
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", t).strip()


def leer_archivo(nombre, contenido):
    """Lee el Excel/CSV exportado del Reporte Kilómetros/Horas.
    Devuelve (registros, advertencias)."""
    nombre = (nombre or "").lower()
    filas = None
    if not nombre.endswith(".csv"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(contenido), data_only=True, read_only=True)
            filas = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        except Exception:
            filas = None
    if filas is None:
        import csv
        texto = None
        for enc in ("utf-8-sig", "latin-1"):
            try:
                texto = contenido.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if texto is None:
            raise ValueError("No se pudo leer el archivo. Exportalo como Excel (.xlsx) o CSV.")
        delim = ";" if texto.count(";") > texto.count(",") else ","
        filas = [r for r in csv.reader(io.StringIO(texto), delimiter=delim)]
    # encabezado
    idx = None
    cols = {}
    for i, fila in enumerate(filas[:30]):
        txt = [_norm_txt(c) for c in fila]
        if any("fecha" in c for c in txt) and any("km" in c for c in txt):
            idx = i
            for j, c in enumerate(txt):
                if ("matric" in c or "patente" in c or "domain" in c or "dominio" in c or "group" in c or "grupo" in c) and "pat" not in cols:
                    cols["pat"] = j
                elif "fecha" in c and "fecha" not in cols:
                    cols["fecha"] = j
                elif "motor" in c and "horas" not in cols:
                    cols["horas"] = j
                elif "km" in c and "km" not in cols:
                    cols["km"] = j
            break
    if idx is None or "fecha" not in cols or "km" not in cols:
        raise ValueError("No encontré las columnas Fecha y Km. ¿Es el 'Reporte Kilómetros/Horas' de Easytrack?")
    if "pat" not in cols:
        cols["pat"] = 0
    registros, advertencias, actual = [], [], None
    rx_pat = re.compile(r"^([A-Z]{2}\d{3}[A-Z]{2}|[A-Z]{3}\d{3})$")
    for fila in filas[idx + 1:]:
        def celda(k):
            j = cols.get(k)
            return fila[j] if j is not None and j < len(fila) else None
        bruto = str(celda("pat") or "").strip()
        bruto = re.sub(r"^[-> ]+", "", bruto)
        bruto = re.sub(r"\s*\(\d+\)\s*$", "", bruto)
        cand = norm_patente(bruto)
        if cand and rx_pat.match(cand):
            actual = cand
        fecha = a_fecha(celda("fecha"))
        if not fecha:
            continue          # filas de totales del grupo
        if not actual:
            continue
        registros.append({"patente": actual, "fecha": fecha,
                          "horas": round(a_horas(celda("horas")), 4), "km": round(a_km(celda("km")), 2)})
    if not registros:
        advertencias.append("El archivo no tiene filas con fecha y patente.")
    return registros, advertencias


# ---------------------------------------------------------- pedir a Easytrack
def descargar(cfg, desde, hasta):
    s = requests.Session()
    s.headers["User-Agent"] = "PlataformaCOSESA/1.0 (sincronizacion de horas y km)"
    r = s.post(cfg["api"] + "sessions/auth/", json={"username": cfg["usuario"], "password": cfg["clave"], "menu": "dsk"}, timeout=40)
    r.raise_for_status()
    d = r.json()
    if d.get("error") or not d.get("jwt"):
        raise RuntimeError("Easytrack rechazó el usuario o la contraseña: " + str(d.get("error_message") or "sin detalle"))
    user = d.get("user") or {}
    company = user.get("company_id")
    if company is None:
        raise RuntimeError("Easytrack no devolvió la empresa del usuario.")
    h = {"Authorization": "Bearer " + d["jwt"], "Content-Type": "application/json"}
    r = s.post(cfg["api"] + "reports/km-report", headers=h, timeout=90,
               json={"company_id": company, "vehicle_ids": None, "drivers": None,
                     "date_from": desde.isoformat(), "date_to": hasta.isoformat()})
    r.raise_for_status()
    datos = r.json()
    try:
        s.post(cfg["api"] + "sessions/logout/", headers=h, json={"refreshToken": d.get("rjwt") or d.get("refreshToken")}, timeout=15)
    except Exception:
        pass
    if isinstance(datos, dict) and datos.get("error"):
        raise RuntimeError("Easytrack devolvió un error al pedir el reporte.")
    if isinstance(datos, dict):
        for k in ("data", "rows", "result", "records", "items"):
            if isinstance(datos.get(k), list):
                datos = datos[k]
                break
    if not isinstance(datos, list):
        raise RuntimeError("Respuesta inesperada de Easytrack (" + type(datos).__name__ + ": " + ", ".join(list(datos)[:8] if isinstance(datos, dict) else []) + ").")
    if not datos:
        raise RuntimeError(f"Easytrack devolvió el reporte vacío (empresa {company}, {desde:%d/%m} al {hasta:%d/%m}).")
    registros = []
    for x in datos:
        pat = norm_patente(x.get("domain_veh"))
        fecha = a_fecha(x.get("date"))
        if not pat or not fecha:
            continue
        registros.append({"patente": pat, "fecha": fecha, "horas": round(a_horas(x.get("time_er")), 4),
                          "km": round(a_km(x.get("km_total")), 2)})
    if not registros:
        ej = datos[0] if isinstance(datos[0], dict) else {}
        campos = ", ".join(f"{k}={str(v)[:16]!r}" for k, v in list(ej.items())[:10])
        raise RuntimeError(f"Easytrack devolvió {len(datos)} fila(s) pero con otro formato. Campos de la primera: {campos}")
    return registros


# ------------------------------------------------- aplicar al estado del módulo
def _unidad_nueva(p, alias):
    return {"id": "ET_" + p, "nombre": f"{alias} ({p})" if alias else p, "patente": p, "tipo": "vehiculo",
            "motor": "", "serie": "", "meterType": "km", "currentMeter": 0, "meters": {"km": None, "horas": None},
            "tasks": [{"id": f"ET_{p}_T1", "nombre": "Service general", "intervalo": 10000, "unidad": "km",
                       "parts": [], "lastMeter": None, "lastDate": None}],
            "notes": "", "easytrack": {"dias": {}}}


def aplicar(doc, registros, hoy_iso):
    """Suma al documento de Mantenimiento lo nuevo. Devuelve un resumen."""
    porpat = {}
    for r in registros:
        if r["fecha"] >= hoy_iso:
            continue                      # el día de hoy todavía no terminó
        porpat.setdefault(r["patente"], {})[r["fecha"]] = {"h": r["horas"], "km": r["km"]}
    res = {"dias": 0, "unidades": 0, "creadas": 0, "detalle": []}
    maquinas = doc.setdefault("machines", [])
    for pat, dias in sorted(porpat.items()):
        m = next((x for x in maquinas if norm_patente(x.get("patente")) == pat), None)
        if m is None:
            m = _unidad_nueva(pat, UNIDADES_CONOCIDAS.get(pat, ""))
            maquinas.append(m)
            res["creadas"] += 1
        meters = m.get("meters") if isinstance(m.get("meters"), dict) else None
        if meters is None:
            meters = {"km": None, "horas": None}
            if isinstance(m.get("currentMeter"), (int, float)) and m.get("currentMeter"):
                meters[m.get("meterType") or "km"] = m["currentMeter"]
            m["meters"] = meters
        et = m.setdefault("easytrack", {})
        previos = et.setdefault("dias", {})
        dk = dh = 0.0
        n = 0
        for f, b in dias.items():
            a = previos.get(f)
            if a and abs(a.get("km", 0) - b["km"]) <= 0.05 and abs(a.get("h", 0) - b["h"]) <= 0.005:
                continue
            dk += b["km"] - (a.get("km", 0) if a else 0)
            dh += b["h"] - (a.get("h", 0) if a else 0)
            previos[f] = b
            n += 1
        if not n:
            continue
        meters["km"] = round((meters.get("km") or 0) + dk, 1)
        meters["horas"] = round((meters.get("horas") or 0) + dh, 1)
        m["currentMeter"] = meters.get(m.get("meterType") or "km") or 0
        et["ultimaImportacion"] = hoy_iso
        res["dias"] += n
        res["unidades"] += 1
        res["detalle"].append(f"{pat}: +{round(dk, 1)} km, +{round(dh, 1)} h ({n} día/s)")
    return res


def sincronizar(origen="automatica", fecha_programada=None):
    """Corre una sincronización completa. Devuelve (ok, mensaje, resumen)."""
    cfg = _cfg()
    if cfg is None:
        return False, "Falta configurar EASYTRACK_USUARIO y EASYTRACK_CLAVE en el servidor.", None
    if not _lock.acquire(blocking=False):
        return False, "Ya hay una sincronización en curso.", None
    db = get_db()
    corrida_id = None
    try:
        hoy = datetime.now(TZ_AR).date()
        if origen == "automatica":
            try:
                corrida_id = db.execute(
                    "INSERT INTO easytrack_corridas (fecha, origen) VALUES (?, 'automatica')",
                    (fecha_programada or hoy.isoformat(),)).lastrowid
                db.commit()
            except Exception:
                return False, "La corrida de hoy ya se hizo.", None
        else:
            corrida_id = db.execute("INSERT INTO easytrack_corridas (fecha, origen) VALUES ('manual', ?)", (origen,)).lastrowid
            db.commit()
        hasta = hoy - timedelta(days=1)
        desde = hoy - timedelta(days=cfg["dias"])
        registros = descargar(cfg, desde, hasta)
        res = None
        for _intento in range(5):
            row = db.execute("SELECT doc, version FROM mantenimiento_app_estado WHERE id = 1").fetchone()
            if row is None:
                raise RuntimeError("El módulo Mantenimiento todavía no se abrió nunca: abrilo una vez y volvé a sincronizar.")
            doc = json.loads(row["doc"])
            res = aplicar(doc, registros, hoy.isoformat())
            if res["dias"] == 0 and res["creadas"] == 0:
                break
            cur = db.execute(
                "UPDATE mantenimiento_app_estado SET doc = ?, version = version + 1, actualizado_en = datetime('now'), "
                "actualizado_por = 'Easytrack (automático)' WHERE id = 1 AND version = ?",
                (json.dumps(doc, ensure_ascii=False), row["version"]))
            db.commit()
            if cur.rowcount == 1:
                break
            time.sleep(1)                 # alguien guardó en el medio: se recalcula sobre lo nuevo
        else:
            raise RuntimeError("No se pudo guardar: el módulo se estaba modificando. Se reintenta en la próxima corrida.")
        msg = (f"{len(registros)} registro(s) leídos de Easytrack ({desde:%d/%m} al {hasta:%d/%m}); "
               f"{res['dias']} día(s) nuevos en {res['unidades']} unidad(es)"
               + (f"; {res['creadas']} unidad(es) creada(s)" if res["creadas"] else "") + ".")
        db.execute("UPDATE easytrack_corridas SET fin = datetime('now'), ok = 1, detalle = ? WHERE id = ?",
                   (msg + ("\n" + "\n".join(res["detalle"]) if res["detalle"] else ""), corrida_id))
        db.commit()
        return True, msg, res
    except Exception as e:  # noqa: BLE001 - se registra cualquier falla para verla en la app
        msg = f"Error: {e}"
        if corrida_id:
            db.execute("UPDATE easytrack_corridas SET fin = datetime('now'), ok = 0, detalle = ? WHERE id = ?", (msg, corrida_id))
            db.commit()
        return False, msg, None
    finally:
        db.close()
        _lock.release()


def estado():
    db = get_db()
    rows = db.execute("SELECT * FROM easytrack_corridas ORDER BY id DESC LIMIT 10").fetchall()
    db.close()
    cfg = _cfg()
    return {
        "configurado": cfg is not None,
        "hora": cfg["hora"] if cfg else None,
        "corridas": [{"inicio": r["inicio"], "fin": r["fin"], "ok": r["ok"], "detalle": r["detalle"], "origen": r["origen"]} for r in rows],
    }


def iniciar_programador():
    """Hilo que corre la sincronización una vez por día a la hora configurada
    (hora de Argentina). Si el servidor estuvo apagado a esa hora, la corre al
    volver, mientras sea el mismo día."""
    if _cfg() is None:
        return

    def loop():
        time.sleep(60)
        while True:
            try:
                cfg = _cfg()
                ahora = datetime.now(TZ_AR)
                if cfg and ahora.hour >= cfg["hora"]:
                    hoy = ahora.date().isoformat()
                    db = get_db()
                    hecha = db.execute("SELECT 1 FROM easytrack_corridas WHERE origen = 'automatica' AND fecha = ?", (hoy,)).fetchone()
                    db.close()
                    if not hecha:
                        sincronizar("automatica", hoy)
            except Exception:
                pass
            time.sleep(300)

    threading.Thread(target=loop, name="easytrack-sync", daemon=True).start()
