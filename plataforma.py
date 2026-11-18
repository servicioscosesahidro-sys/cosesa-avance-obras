"""Plataforma COSESA: pantalla de inicio con los módulos.

Para sumar una app nueva a la plataforma, alcanza con cambiar su entrada en
MODULOS:
  - estado "activo":       módulo integrado; "endpoint" es la ruta de Flask.
  - estado "externo":      todavía es una app aparte; "url" la abre en otra pestaña.
  - estado "proximamente": botón visible pero deshabilitado.
"permiso" es el permiso de operador que da acceso (None = todos los usuarios
COSESA). Los administradores ven todo.

Se registra desde app.py con init_app(app, helpers).
"""
from flask import render_template, url_for

# Íconos (trazo, 24x24)
_I = {
    "comercial": '<path d="M3 7h18v12a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/><path d="M8 7V5a2 2 0 012-2h4a2 2 0 012 2v2"/><path d="M3 12h18"/>',
    "preparacion": '<path d="M9 4h6a1 1 0 011 1v1H8V5a1 1 0 011-1z"/><path d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h12a2 2 0 002-2V7a2 2 0 00-2-2h-2"/><path d="M8.5 12.5l2 2 5-5"/>',
    "avance": '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/>',
    "mantenimiento": '<path d="M14.7 6.3a4 4 0 00-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 005.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/>',
    "taller": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/>',
    "stock": '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
    "compras": '<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/><path d="M2 3h3l2.7 12.4a1.5 1.5 0 001.5 1.1h8.9a1.5 1.5 0 001.5-1.1L22 7H6"/>',
}

MODULOS = [
    {"clave": "comercial", "nombre": "Comercial",
     "descripcion": "Cotizaciones y seguimiento comercial de cada obra, de principio a fin.",
     "estado": "proximamente", "permiso": None},
    {"clave": "preparacion", "nombre": "Preparación de obras",
     "descripcion": "Checklist de las 12 etapas, equipos y personal antes de empezar la obra.",
     "estado": "activo", "endpoint": "preparacion_panel", "permiso": "preparacion"},
    {"clave": "avance", "nombre": "Avance de obras",
     "descripcion": "Cronograma, avance diario, bitácora, reportes y certificados.",
     "estado": "activo", "endpoint": "cosesa_dashboard", "permiso": None},
    {"clave": "mantenimiento", "nombre": "Mantenimiento",
     "descripcion": "Mantenimiento de equipos y solicitudes de mantenimiento.",
     "estado": "proximamente", "permiso": None},
    {"clave": "taller", "nombre": "Tareas de taller",
     "descripcion": "Tareas del taller por equipo, responsable y prioridad.",
     "estado": "externo", "url": "https://tareastaller.netlify.app/", "permiso": None},
    {"clave": "stock", "nombre": "Control de stock",
     "descripcion": "Inventario de materiales, repuestos e insumos.",
     "estado": "proximamente", "permiso": None},
    {"clave": "compras", "nombre": "Compras",
     "descripcion": "Pedidos de compra, proveedores y seguimiento.",
     "estado": "proximamente", "permiso": None},
]


def init_app(app, h):
    """h: helpers de app.py (cosesa_required, get_current_user, puede)."""

    @app.route("/inicio")
    @h.cosesa_required
    def plataforma_inicio():
        user = h.get_current_user()
        modulos = []
        for m in MODULOS:
            item = dict(m)
            item["icono"] = _I.get(m["clave"], "")
            item["con_acceso"] = m.get("permiso") is None or h.puede(m["permiso"], user)
            item["href"] = None
            if item["con_acceso"]:
                if m["estado"] == "activo":
                    item["href"] = url_for(m["endpoint"])
                elif m["estado"] == "externo":
                    item["href"] = m["url"]
            modulos.append(item)
        return render_template("inicio.html", modulos=modulos)
