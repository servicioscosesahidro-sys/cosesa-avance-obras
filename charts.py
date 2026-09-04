"""Generación de gráficos en SVG (sin dependencias externas):
   - Diagrama de Gantt por obra (planificado vs. barra de avance real).
   - Curva de avance planificado vs. avance real (día a día).
"""
from datetime import datetime, date, timedelta


_DT_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d")


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

    svg = [f'<svg viewBox="0 0 {width} {height}" width="100%" style="font-family:inherit;">']

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
        '</div>'
    )
    return "".join(svg) + legend


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
