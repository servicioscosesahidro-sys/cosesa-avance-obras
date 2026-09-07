"""Generación de gráficos en SVG (sin dependencias externas):
   - Diagrama de Gantt por obra (planificado vs. barra de avance real).
   - Curva de avance planificado vs. avance real (día a día).
"""
import itertools
from datetime import datetime, date, timedelta


_DT_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d")
_gantt_counter = itertools.count()


def _parse(d):
    """Devuelve un datetime (con hora si está disponible, si no a medianoche)."""
    if not d:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(d, fmt)
        except ValueError:
            continue
    return None


def _fmt(d):
    return d.strftime("%d/%m %H:%M")


# ---------------------------------------------------------------------------
# Diagrama de Gantt
# ---------------------------------------------------------------------------

def gantt_svg(items_con_demoras, width=880, interactive=False):
    """Una fila por ítem. Barra clara = cronograma planificado.
    Barra azul = avance real (ancho proporcional al % de avance dentro del
    rango real transcurrido). Los segmentos rojos marcan las demoras
    registradas. Líneas punteadas verticales marcan el cambio de día.

    items_con_demoras: lista de dicts {"item": fila_item, "demoras": [filas_demora]}
    interactive: si es True (solo en la ficha de la obra para COSESA), el nombre
    del ítem es un link a su ficha y aparecen botones −/+ para ajustar el avance
    en 10 puntos sin salir de esta vista (reutiliza ajustarAvanceObra(), definido
    junto a la tabla de ítems más abajo en la misma página).
    """
    items = [d["item"] for d in items_con_demoras]
    all_dates = []
    for it in items:
        for key in ("fecha_inicio_plan", "fecha_fin_plan", "fecha_inicio", "fecha_fin"):
            d = _parse(it[key])
            if d:
                all_dates.append(d)
    today = datetime.now()
    all_dates.append(today)

    if not all_dates:
        return "<p class='subtitle'>Todavía no hay fechas cargadas para graficar el cronograma.</p>"

    start = min(all_dates)
    end = max(all_dates)
    if start == end:
        end = start + timedelta(days=7)
    total_span = (end - start).total_seconds() or 1

    label_w = 260 if interactive else 230
    chart_x0 = label_w + 10
    chart_w = width - chart_x0 - 20
    row_h = 38
    top_pad = 44
    height = top_pad + row_h * len(items) + 20

    def x(d):
        return chart_x0 + (d - start).total_seconds() / total_span * chart_w

    chart_id = f"gantt-{next(_gantt_counter)}"
    svg = [f'<svg id="{chart_id}-svg" viewBox="0 0 {width} {height}" width="100%" style="font-family:inherit;">']

    # Franjas de turno de fondo: turno día 07:00-19:00 (blanco) y turno noche
    # 19:00-07:00 del día siguiente (gris azulado), para todo el rango visible.
    banda_cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while banda_cursor <= end:
        dia_ini = banda_cursor.replace(hour=7, minute=0)
        dia_fin = banda_cursor.replace(hour=19, minute=0)
        noche_ini = dia_fin
        noche_fin = dia_ini + timedelta(days=1)
        dx0, dx1 = x(max(dia_ini, start)), x(min(dia_fin, end))
        if dx1 > dx0:
            svg.append(f'<rect x="{dx0:.1f}" y="{top_pad - 4}" width="{dx1 - dx0:.1f}" height="{height - top_pad - 6:.1f}" fill="#fffdf5"/>')
        nx0, nx1 = x(max(noche_ini, start)), x(min(noche_fin, end))
        if nx1 > nx0:
            svg.append(f'<rect x="{nx0:.1f}" y="{top_pad - 4}" width="{nx1 - nx0:.1f}" height="{height - top_pad - 6:.1f}" fill="#eef1f6"/>')
        banda_cursor += timedelta(days=1)

    # Marcas de hora cada 6hs (00/06/12/18), finitas — dan la resolución
    # horaria; se ven mejor haciendo zoom con los botones de abajo.
    hora_cursor = start.replace(minute=0, second=0, microsecond=0)
    while hora_cursor <= end:
        if hora_cursor.hour % 6 == 0:
            hx = x(hora_cursor)
            es_cambio_de_dia = hora_cursor.hour == 0
            if not es_cambio_de_dia:
                svg.append(f'<line x1="{hx:.1f}" y1="{top_pad - 2}" x2="{hx:.1f}" y2="{top_pad + 2}" stroke="#c7d0d6" stroke-width="1"/>')
                svg.append(f'<text x="{hx:.1f}" y="{top_pad - 30}" font-size="7" fill="#b7c0c6" text-anchor="middle">{hora_cursor.strftime("%H")}h</text>')
        hora_cursor += timedelta(hours=1)

    # líneas punteadas verticales para cada cambio de día + etiqueta de fecha
    day = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while day < end:
        dx = x(day)
        svg.append(f'<line x1="{dx:.1f}" y1="{top_pad - 4}" x2="{dx:.1f}" y2="{height - 10}" stroke="#d5dade" stroke-width="1" stroke-dasharray="3,3" />')
        svg.append(f'<text x="{dx:.1f}" y="{top_pad - 8}" font-size="9" fill="#9aa4ab" text-anchor="middle">{day.strftime("%d/%m")}</text>')
        day += timedelta(days=1)

    # eje de fechas arriba
    svg.append(f'<text x="{chart_x0}" y="16" font-size="11" fill="#6b7580">{_fmt(start)}</text>')
    svg.append(f'<text x="{chart_x0 + chart_w}" y="16" font-size="11" fill="#6b7580" text-anchor="end">{_fmt(end)}</text>')
    # línea de "hoy"
    today_x = x(today)
    svg.append(f'<line x1="{today_x:.1f}" y1="{top_pad - 6}" x2="{today_x:.1f}" y2="{height - 10}" stroke="#c0392b" stroke-width="1.5" stroke-dasharray="4,3" />')
    svg.append(f'<text x="{today_x:.1f}" y="{top_pad - 22}" font-size="10" fill="#c0392b" text-anchor="middle">hoy</text>')

    y = top_pad
    for d in items_con_demoras:
        it = d["item"]
        nombre = it["nombre"]
        tag = (it["tag_equipo"] or "").strip() if "tag_equipo" in it.keys() else ""
        plan_ini, plan_fin = _parse(it["fecha_inicio_plan"]), _parse(it["fecha_fin_plan"])
        real_ini, real_fin = _parse(it["fecha_inicio"]), _parse(it["fecha_fin"]) or today
        avance = it["avance_pct"]

        max_chars = 34 if not interactive else 26
        nombre_corto = nombre[:max_chars]
        tag_corto = tag[:max_chars]
        # Con TAG: línea de arriba en negrita con el TAG (lo más importante para
        # identificar el equipo en planta), línea de abajo con el nombre en gris.
        # Sin TAG: una sola línea centrada con el nombre, como antes.
        tag_y = y + row_h / 2 - 5
        nombre_y = y + row_h / 2 + 9
        sola_y = y + row_h / 2 + 4
        if interactive:
            if tag_corto:
                svg.append(
                    f'<a href="/cosesa/items/{it["id"]}">'
                    f'<text x="0" y="{tag_y:.1f}" font-size="12" font-weight="700" fill="#33456b" '
                    f'text-decoration="underline" style="cursor:pointer;">{tag_corto}</text>'
                    f'<text x="0" y="{nombre_y:.1f}" font-size="10" fill="#7a848c" '
                    f'style="cursor:pointer;">{nombre_corto}</text>'
                    f'</a>'
                )
            else:
                svg.append(
                    f'<a href="/cosesa/items/{it["id"]}"><text x="0" y="{sola_y:.1f}" '
                    f'font-size="12" fill="#23272b" text-decoration="underline" style="cursor:pointer;">'
                    f'{nombre_corto}</text></a>'
                )
            btn_y = y + row_h / 2
            svg.append(
                f'<g style="cursor:pointer;" onclick="ajustarAvanceObra({it["id"]}, -10)">'
                f'<circle cx="205" cy="{btn_y:.1f}" r="10" fill="#eef2f5" stroke="#c7d0d6"/>'
                f'<text x="205" y="{btn_y + 4:.1f}" font-size="13" fill="#33456b" text-anchor="middle">-</text>'
                f'</g>'
            )
            svg.append(
                f'<g style="cursor:pointer;" onclick="ajustarAvanceObra({it["id"]}, 10)">'
                f'<circle cx="230" cy="{btn_y:.1f}" r="10" fill="#eef2f5" stroke="#c7d0d6"/>'
                f'<text x="230" y="{btn_y + 4:.1f}" font-size="13" fill="#33456b" text-anchor="middle">+</text>'
                f'</g>'
            )
        else:
            if tag_corto:
                svg.append(f'<text x="0" y="{tag_y:.1f}" font-size="12" font-weight="700" fill="#33456b">{tag_corto}</text>')
                svg.append(f'<text x="0" y="{nombre_y:.1f}" font-size="10" fill="#7a848c">{nombre_corto}</text>')
            else:
                svg.append(f'<text x="0" y="{sola_y:.1f}" font-size="12" fill="#23272b">{nombre_corto}</text>')

        bar_y = y + 6
        bar_h = row_h - 16

        if plan_ini and plan_fin:
            px0, px1 = x(plan_ini), x(plan_fin)
            svg.append(f'<rect x="{px0:.1f}" y="{bar_y}" width="{max(px1-px0,2):.1f}" height="{bar_h}" rx="4" fill="#eef2f5" stroke="#c7d0d6" stroke-width="1"/>')

        if real_ini:
            rx0, rx1 = x(real_ini), x(real_fin)
            full_w = max(rx1 - rx0, 2)
            svg.append(f'<rect x="{rx0:.1f}" y="{bar_y}" width="{full_w:.1f}" height="{bar_h}" rx="4" fill="#e6eaf1" stroke="#33456b" stroke-width="1"/>')
            fill_w = full_w * (avance / 100)
            svg.append(f'<rect x="{rx0:.1f}" y="{bar_y}" width="{fill_w:.1f}" height="{bar_h}" rx="4" fill="#33456b"/>')

            # demoras en rojo, superpuestas sobre la barra de avance real. Una
            # demora sin fecha_fin es una pausa todavía abierta: se dibuja
            # hasta "hoy" con un borde punteado para diferenciarla de una
            # demora ya cerrada.
            for dem in d["demoras"]:
                dini = _parse(dem["fecha_inicio"])
                abierta = not dem["fecha_fin"]
                dfin = _parse(dem["fecha_fin"]) if not abierta else today
                if not dini or not dfin or dfin <= dini:
                    continue
                ddx0, ddx1 = max(x(dini), rx0), min(x(dfin), rx1)
                if ddx1 <= ddx0:
                    continue
                borde = ' stroke="#7a1f14" stroke-width="1.5" stroke-dasharray="3,2"' if abierta else ''
                svg.append(f'<rect x="{ddx0:.1f}" y="{bar_y}" width="{(ddx1-ddx0):.1f}" height="{bar_h}" fill="#c0392b"{borde}/>')

            etiqueta_avance = f'{avance}%' + (' ⏸ pausado' if it["pausado_en"] else '')
            svg.append(f'<text x="{rx1 + 6:.1f}" y="{y + row_h/2 + 4}" font-size="11" fill="#33456b" font-weight="600">{etiqueta_avance}</text>')
        else:
            svg.append(f'<text x="{chart_x0}" y="{y + row_h/2 + 4}" font-size="11" fill="#9aa4ab">Pendiente de inicio</text>')

        y += row_h

    svg.append('</svg>')
    legend = (
        '<div style="display:flex;gap:18px;font-size:12px;color:#6b7580;margin-top:6px;flex-wrap:wrap;">'
        '<span><span style="display:inline-block;width:12px;height:12px;background:#eef2f5;border:1px solid #c7d0d6;border-radius:3px;vertical-align:middle;"></span> Planificado</span>'
        '<span><span style="display:inline-block;width:12px;height:12px;background:#33456b;border-radius:3px;vertical-align:middle;"></span> Avance real</span>'
        '<span><span style="display:inline-block;width:12px;height:12px;background:#c0392b;border-radius:3px;vertical-align:middle;"></span> Demora</span>'
        '<span><span style="display:inline-block;width:12px;height:12px;background:#c0392b;border:1.5px dashed #7a1f14;border-radius:3px;vertical-align:middle;"></span> Pausa en curso</span>'
        '<span><span style="display:inline-block;width:12px;height:12px;border:1px dashed #c0392b;border-radius:3px;vertical-align:middle;"></span> Hoy</span>'
        '<span><span style="display:inline-block;width:12px;height:12px;border:1px dashed #d5dade;border-radius:3px;vertical-align:middle;"></span> Cambio de día</span>'
        '<span><span style="display:inline-block;width:12px;height:12px;background:#fffdf5;border:1px solid #e5decf;border-radius:3px;vertical-align:middle;"></span> Turno día (07-19h)</span>'
        '<span><span style="display:inline-block;width:12px;height:12px;background:#eef1f6;border:1px solid #ced6e0;border-radius:3px;vertical-align:middle;"></span> Turno noche (19-07h)</span>'
        '</div>'
    )
    toolbar = (
        f'<div class="no-print" style="display:flex;gap:8px;align-items:center;margin-bottom:8px;">'
        f'<span class="subtitle" style="margin:0;">Zoom del cronograma:</span>'
        f'<button type="button" class="btn btn-sm btn-outline" onclick="gnttZoom(\'{chart_id}\', -1)">−</button>'
        f'<span class="subtitle" id="{chart_id}-zoomlabel" style="margin:0;min-width:40px;text-align:center;display:inline-block;">100%</span>'
        f'<button type="button" class="btn btn-sm btn-outline" onclick="gnttZoom(\'{chart_id}\', 1)">+</button>'
        f'<button type="button" class="btn btn-sm btn-outline" onclick="gnttZoom(\'{chart_id}\', 0)">Restablecer</button>'
        f'<span class="subtitle" style="margin:0;">(o hacé scroll horizontal una vez que hagas zoom)</span>'
        f'</div>'
        f'<div style="overflow-x:auto;">{"".join(svg)}</div>'
        f'<script>'
        f'(function(){{'
        f'if (!window.__gnttZoomState) window.__gnttZoomState = {{}};'
        f'window.__gnttZoomState["{chart_id}"] = {{ factor: 1, base: {width} }};'
        f'window.gnttZoom = window.gnttZoom || function (id, dir) {{'
        f'var st = window.__gnttZoomState[id]; if (!st) return;'
        f'if (dir === 0) {{ st.factor = 1; }} else {{ st.factor = Math.max(1, Math.min(6, st.factor + dir * 0.5)); }}'
        f'var svgEl = document.getElementById(id + "-svg");'
        f'if (svgEl) {{ svgEl.style.width = (st.base * st.factor) + "px"; svgEl.style.maxWidth = "none"; }}'
        f'var label = document.getElementById(id + "-zoomlabel");'
        f'if (label) label.textContent = Math.round(st.factor * 100) + "%";'
        f'}};'
        f'}})();'
        f'</script>'
    )
    return toolbar + legend


# ---------------------------------------------------------------------------
# Curva de avance: planificado (lineal) vs. real (según bitácora)
# ---------------------------------------------------------------------------

def _expected_pct(day, plan_ini, plan_fin):
    if not (plan_ini and plan_fin) or plan_fin <= plan_ini:
        return None
    if day <= plan_ini:
        return 0.0
    if day >= plan_fin:
        return 100.0
    return (day - plan_ini).total_seconds() / (plan_fin - plan_ini).total_seconds() * 100


def _actual_pct_series(item_avances, day):
    """Último % conocido en o antes de `day` según la bitácora (carry-forward)."""
    val = 0.0
    for a in item_avances:
        fecha = _parse(a["fecha"])
        if fecha and fecha <= day:
            val = a["avance_pct"]
    return val


def curve_svg(items_con_avances, width=880):
    """items_con_avances: lista de dicts {item: row, avances: [rows...]}"""
    all_dates = []
    for d in items_con_avances:
        it = d["item"]
        for key in ("fecha_inicio_plan", "fecha_fin_plan", "fecha_inicio", "fecha_fin"):
            dt = _parse(it[key])
            if dt:
                all_dates.append(dt)
        for a in d["avances"]:
            dt = _parse(a["fecha"])
            if dt:
                all_dates.append(dt)
    today = datetime.now()
    all_dates.append(today)
    if not all_dates:
        return "<p class='subtitle'>Cargá fechas de inicio/fin planificadas para poder comparar el avance.</p>"

    start = min(all_dates)
    end = max(all_dates)
    if start == end:
        end = start + timedelta(days=7)
    total_days = (end - start).days
    step = max(1, total_days // 60)  # limitar cantidad de puntos
    days = [start + timedelta(days=i) for i in range(0, total_days + 1, step)]
    if days[-1] != end:
        days.append(end)

    expected_points = []
    actual_points = []
    for day in days:
        exp_vals = []
        act_vals = []
        for d in items_con_avances:
            it = d["item"]
            plan_ini, plan_fin = _parse(it["fecha_inicio_plan"]), _parse(it["fecha_fin_plan"])
            exp = _expected_pct(day, plan_ini, plan_fin)
            if exp is not None:
                exp_vals.append(exp)
            act_vals.append(_actual_pct_series(d["avances"], day))
        expected_points.append(sum(exp_vals) / len(exp_vals) if exp_vals else None)
        actual_points.append(sum(act_vals) / len(act_vals) if act_vals else 0)

    if all(v is None for v in expected_points):
        return "<p class='subtitle'>Todavía no cargaste fechas de inicio/fin planificadas para los ítems: no se puede trazar el cronograma comparativo.</p>"

    chart_x0, chart_x1 = 50, width - 20
    chart_y0, chart_y1 = 20, 220
    chart_w = chart_x1 - chart_x0
    chart_h = chart_y1 - chart_y0

    def x(i):
        return chart_x0 + (i / (len(days) - 1 if len(days) > 1 else 1)) * chart_w

    def y(v):
        return chart_y1 - (v / 100) * chart_h

    svg = [f'<svg viewBox="0 0 {width} {chart_y1 + 40}" width="100%" style="font-family:inherit;">']
    # grilla horizontal 0/25/50/75/100
    for pct in (0, 25, 50, 75, 100):
        gy = y(pct)
        svg.append(f'<line x1="{chart_x0}" y1="{gy:.1f}" x2="{chart_x1}" y2="{gy:.1f}" stroke="#eceff1" stroke-width="1"/>')
        svg.append(f'<text x="{chart_x0-8}" y="{gy+3:.1f}" font-size="10" fill="#6b7580" text-anchor="end">{pct}%</text>')

    # línea planificada (punteada)
    pts = [(x(i), y(v)) for i, v in enumerate(expected_points) if v is not None]
    if len(pts) > 1:
        path = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        svg.append(f'<polyline points="{path}" fill="none" stroke="#9aa4ab" stroke-width="2" stroke-dasharray="6,4"/>')

    # línea real (sólida, azul)
    pts2 = [(x(i), y(v)) for i, v in enumerate(actual_points)]
    path2 = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts2)
    svg.append(f'<polyline points="{path2}" fill="none" stroke="#33456b" stroke-width="2.5"/>')

    # eje de fechas
    svg.append(f'<text x="{chart_x0}" y="{chart_y1+18}" font-size="11" fill="#6b7580">{_fmt(start)}</text>')
    svg.append(f'<text x="{chart_x1}" y="{chart_y1+18}" font-size="11" fill="#6b7580" text-anchor="end">{_fmt(end)}</text>')

    svg.append('</svg>')
    legend = (
        '<div style="display:flex;gap:18px;font-size:12px;color:#6b7580;margin-top:6px;">'
        '<span><span style="display:inline-block;width:18px;border-top:2px dashed #9aa4ab;vertical-align:middle;"></span> Avance planificado</span>'
        '<span><span style="display:inline-block;width:18px;border-top:2.5px solid #33456b;vertical-align:middle;"></span> Avance real</span>'
        '</div>'
    )
    return "".join(svg) + legend


# ---------------------------------------------------------------------------
# Carga diaria: cuántos ítems están "a lavar" cada día, y cuántas bombas
# asignadas suman esos ítems ese día. Una versión según el cronograma
# planificado y otra según las fechas reales de ejecución.
# ---------------------------------------------------------------------------

def _carga_diaria_datos(items_con_bombas, campo_inicio, campo_fin, contar_abiertos_hasta_hoy):
    """Devuelve (dias, items_serie, bombas_serie) o None si no hay datos
    suficientes. items_con_bombas: lista de dicts {"item": fila, "bombas": int}."""
    hoy = datetime.now()
    rangos = []
    for d in items_con_bombas:
        it = d["item"]
        ini = _parse(it[campo_inicio])
        if not ini:
            continue
        fin = _parse(it[campo_fin])
        if not fin:
            if not contar_abiertos_hasta_hoy:
                continue
            fin = hoy
        if fin < ini:
            continue
        rangos.append((ini, fin, d["bombas"]))

    if not rangos:
        return None

    start = min(r[0] for r in rangos).date()
    end = max(r[1] for r in rangos).date()
    if start == end:
        end = start + timedelta(days=1)

    dias = []
    cur = start
    while cur <= end:
        dias.append(cur)
        cur += timedelta(days=1)

    items_serie, bombas_serie = [], []
    for day in dias:
        day_ini = datetime.combine(day, datetime.min.time())
        day_fin = day_ini + timedelta(days=1)
        activos = [r for r in rangos if r[0] < day_fin and r[1] >= day_ini]
        items_serie.append(len(activos))
        bombas_serie.append(sum(r[2] for r in activos))

    return dias, items_serie, bombas_serie


def _carga_diaria_svg(dias, items_serie, bombas_serie, width=880):
    max_val = max(max(items_serie, default=0), max(bombas_serie, default=0), 1)
    n = len(dias)

    chart_x0, chart_x1 = 40, width - 20
    chart_y0, chart_y1 = 16, 190
    chart_w = chart_x1 - chart_x0
    chart_h = chart_y1 - chart_y0

    def x(i):
        return chart_x0 + (i / (n - 1 if n > 1 else 1)) * chart_w

    def y(v):
        return chart_y1 - (v / max_val) * chart_h

    svg = [f'<svg viewBox="0 0 {width} {chart_y1 + 32}" width="100%" style="font-family:inherit;">']

    pasos_y = sorted(set([0, max(1, max_val // 2), max_val]))
    for val in pasos_y:
        gy = y(val)
        svg.append(f'<line x1="{chart_x0}" y1="{gy:.1f}" x2="{chart_x1}" y2="{gy:.1f}" stroke="#eceff1" stroke-width="1"/>')
        svg.append(f'<text x="{chart_x0-6}" y="{gy+3:.1f}" font-size="10" fill="#6b7580" text-anchor="end">{val}</text>')

    pts_items = [(x(i), y(v)) for i, v in enumerate(items_serie)]
    path_items = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts_items)
    svg.append(f'<polyline points="{path_items}" fill="none" stroke="#33456b" stroke-width="2.5"/>')

    hay_bombas = any(bombas_serie)
    if hay_bombas:
        pts_bombas = [(x(i), y(v)) for i, v in enumerate(bombas_serie)]
        path_bombas = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts_bombas)
        svg.append(f'<polyline points="{path_bombas}" fill="none" stroke="#c98a2b" stroke-width="2.5" stroke-dasharray="6,3"/>')

    label_step = max(1, n // 10)
    for i, day in enumerate(dias):
        if i % label_step == 0 or i == n - 1:
            svg.append(f'<text x="{x(i):.1f}" y="{chart_y1+16}" font-size="9" fill="#6b7580" text-anchor="middle">{day.strftime("%d/%m")}</text>')

    svg.append('</svg>')
    legend_items = [
        '<span><span style="display:inline-block;width:18px;border-top:2.5px solid #33456b;vertical-align:middle;"></span> Ítems a lavar</span>'
    ]
    if hay_bombas:
        legend_items.append(
            '<span><span style="display:inline-block;width:18px;border-top:2.5px dashed #c98a2b;vertical-align:middle;"></span> Bombas en uso</span>'
        )
    legend = '<div style="display:flex;gap:18px;font-size:12px;color:#6b7580;margin-top:6px;flex-wrap:wrap;">' + "".join(legend_items) + '</div>'
    return "".join(svg) + legend


def carga_diaria_planificada_svg(items_con_bombas, width=880):
    """Ítems y bombas activos por día, según el cronograma planificado
    (fecha_inicio_plan / fecha_fin_plan)."""
    datos = _carga_diaria_datos(items_con_bombas, "fecha_inicio_plan", "fecha_fin_plan", contar_abiertos_hasta_hoy=False)
    if datos is None:
        return "<p class='subtitle'>Cargá fechas de inicio y fin planificadas en los ítems para ver este gráfico.</p>"
    return _carga_diaria_svg(*datos, width=width)


def carga_diaria_real_svg(items_con_bombas, width=880):
    """Ítems y bombas activos por día, según las fechas reales de ejecución
    (fecha_inicio / fecha_fin). Un ítem en curso sin fecha de fin todavía se
    cuenta como activo hasta hoy."""
    datos = _carga_diaria_datos(items_con_bombas, "fecha_inicio", "fecha_fin", contar_abiertos_hasta_hoy=True)
    if datos is None:
        return "<p class='subtitle'>Todavía no hay fechas reales de inicio cargadas en los ítems.</p>"
    return _carga_diaria_svg(*datos, width=width)
