"""
Agente de previas del AC Milan (plantilla mpv2).

Cada mañana comprueba si el Milan juega MAÑANA (hora de Madrid). Si juega:
  1. Python calcula con football-data.org los bloques de datos (cabecera, puestos,
     tarjetas de forma, historial). Así salen siempre exactos y con el formato de la plantilla.
  2. Claude, con búsqueda web, redacta el resto y lo devuelve como JSON: contexto con
     probabilidades, claves, jugadores del rival, alineaciones, árbitro, estadio, tiempo y TV.
  3. Se rellena plantilla.html y se crea un BORRADOR en WordPress.

Variables de entorno:
  FOOTBALL_DATA_TOKEN, ANTHROPIC_API_KEY, WP_URL, WP_USER, WP_APP_PASSWORD
  Opcionales: WP_CATEGORY_ID, FECHA_OBJETIVO (YYYY-MM-DD), DRY_RUN=1, CLAUDE_MODEL
"""

import datetime as dt
import html
import json
import os
import re
import sys
import unicodedata
from zoneinfo import ZoneInfo

import anthropic
import requests

TZ = ZoneInfo("Europe/Madrid")
MILAN_ID = 98
FD_BASE = "https://api.football-data.org/v4"
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plantilla.html")

MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
         "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
MESES_CORTO = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
PAISES_TV = ["🇪🇸 España", "🇦🇩 Andorra", "🇦🇷 Argentina", "🇲🇽 México",
             "🇨🇴 Colombia", "🇨🇱 Chile", "🇵🇪 Perú", "🌎 Resto de Latam"]
COLOR = {"W": "#2e7d32", "D": "#1565c0", "L": "#c62828"}
ETIQUETA = {"W": "VICTORIA", "D": "EMPATE", "L": "DERROTA"}


# ================================================================ utilidades
def fd_get(path, **params):
    r = requests.get(FD_BASE + path, headers={"X-Auth-Token": os.environ["FOOTBALL_DATA_TOKEN"]},
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def slugify(t):
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def plano(t):
    return html.escape(str(t if t is not None else "por confirmar"), quote=False)


def rico(t):
    """Escapa el texto, pero deja pasar <strong>, <b> y <em>."""
    t = plano(t)
    for tag in ("strong", "b", "em"):
        t = t.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return t


def local_dt(m):
    return dt.datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(TZ)


def resultado(m, team_id):
    s = m["score"]["fullTime"]
    gl, gv = s["home"], s["away"]
    if gl == gv:
        return "D"
    gana_local = gl > gv
    es_local = m["homeTeam"]["id"] == team_id
    return "W" if gana_local == es_local else "L"


def fecha_objetivo():
    valor = os.getenv("FECHA_OBJETIVO", "").strip()
    if valor:
        try:
            return dt.date.fromisoformat(valor)
        except ValueError:
            raise SystemExit(f"FECHA_OBJETIVO no válida: '{valor}'. Usa el formato YYYY-MM-DD, p. ej. 2026-10-25.")
    return (dt.datetime.now(TZ) + dt.timedelta(days=1)).date()


# ================================================================ datos
def partido_de(fecha):
    data = fd_get(f"/teams/{MILAN_ID}/matches",
                  dateFrom=(fecha - dt.timedelta(days=1)).isoformat(),
                  dateTo=(fecha + dt.timedelta(days=1)).isoformat())
    for m in data.get("matches", []):
        if local_dt(m).date() == fecha and m["status"] in ("SCHEDULED", "TIMED"):
            return m
    return None


def puestos(partido):
    """{team_id: '6º puesto (12 puntos)'} de la competición del partido (o de la Serie A)."""
    out = {}
    for code in dict.fromkeys([partido["competition"]["code"], "SA"]):
        try:
            st = fd_get(f"/competitions/{code}/standings")
            tabla = next(t for t in st["standings"] if t["type"] == "TOTAL")["table"]
            for f in tabla:
                out.setdefault(f["team"]["id"], f'{f["position"]}º puesto ({f["points"]} puntos)')
        except Exception:
            pass
    return out


def ultimos_liga(team_id, season, n=3):
    data = fd_get(f"/teams/{team_id}/matches", competitions="SA", status="FINISHED", season=season)
    ms = sorted(data.get("matches", []), key=lambda m: m["utcDate"], reverse=True)
    return ms[:n]


def ultimos_todos(team_id, n=5):
    hoy = dt.date.today()
    data = fd_get(f"/teams/{team_id}/matches", status="FINISHED",
                  dateFrom=(hoy - dt.timedelta(days=45)).isoformat(), dateTo=hoy.isoformat())
    return sorted(data.get("matches", []), key=lambda m: m["utcDate"], reverse=True)[:n]


def h2h(partido, n=3):
    try:
        data = fd_get(f'/matches/{partido["id"]}/head2head', limit=10)
        return sorted(data.get("matches", []), key=lambda m: m["utcDate"], reverse=True)[:n]
    except Exception:
        return []


def linea(m):
    s = m["score"]["fullTime"]
    return (f'{local_dt(m):%d/%m/%Y} {m["competition"]["name"]}: '
            f'{m["homeTeam"]["shortName"]} {s["home"]}-{s["away"]} {m["awayTeam"]["shortName"]}')


# ================================================================ bloques HTML deterministas
def html_nombre(nombre, es_milan):
    if es_milan:
        return f'<div class="eq-nombre eq-nombre--local">{plano(nombre)}</div>'
    return f'<div class="eq-nombre eq-nombre--visitante"><em>{plano(nombre)}</em></div>'


def html_forma(partidos, team_id):
    tarjetas = []
    for m in partidos:
        r = resultado(m, team_id)
        c = COLOR[r]
        s = m["score"]["fullTime"]
        filas = []
        for lado, goles, mb in (("homeTeam", s["home"], "4px"), ("awayTeam", s["away"], "12px")):
            nombre = f'{plano(m[lado]["shortName"])} {goles}'
            if m[lado]["id"] == team_id:
                filas.append(f'    <div style="font-weight: bold; font-size: 16px; margin-bottom: {mb}; color: {c};">{nombre}</div>')
            else:
                filas.append(f'    <div style="font-size: 15px; color: #444444; margin-bottom: {mb};">{nombre}</div>')
        tarjetas.append(
            f'  <div style="flex: 1; background: #f9f9f9; padding: 15px; border-radius: 8px; border-top: 4px solid {c}; text-align: center; border: 1px solid #f0f0f0;">\n'
            f'    <div style="font-size: 12px; color: #666666; margin-bottom: 8px; font-weight: 500;">{local_dt(m):%d/%m/%Y}</div>\n'
            + "\n".join(filas) + "\n"
            f'    <span style="background: {c}; color: #ffffff; padding: 4px 12px; border-radius: 4px; font-weight: bold; font-size: 12px; display: inline-block;">{ETIQUETA[r]}</span>\n'
            f'  </div>')
    return "\n".join(tarjetas) or '  <p style="font-size:12px;color:var(--g4);">Sin partidos de liga todavía.</p>'


def html_historial(partidos):
    filas = []
    for m in partidos:
        s = m["score"]["fullTime"]
        r = resultado(m, MILAN_ID)
        gl, gv = s["home"], s["away"]
        dim_l = "" if gl > gv else " hist-dim"
        dim_v = "" if gv > gl else " hist-dim"
        d = local_dt(m)
        filas.append(
            '          <div class="hist-fila">\n'
            f'            <span class="hist-fecha">{MESES_CORTO[d.month - 1]} {d:%y}</span>\n'
            f'            <span class="hist-eq hist-eq--local{dim_l}">{plano(m["homeTeam"]["shortName"])}</span>\n'
            f'            <span class="hist-score">{gl}–{gv}</span>\n'
            f'            <span class="hist-eq hist-eq--visit{dim_v}">{plano(m["awayTeam"]["shortName"])}</span>\n'
            f'            <span class="hist-res hist-res--{r.lower()}">{r}</span>\n'
            '          </div>')
    return "\n".join(filas) or '          <p style="font-size:12px;color:var(--g4);">Sin precedentes recientes.</p>'


def placeholder_img(texto):
    return (f'<div style="border: 2px dashed var(--g3); padding: 28px; color: var(--g4); '
            f'font-size: 12px; text-transform: uppercase; letter-spacing: 0.1em;">📷 {plano(texto)}</div>')


# ================================================================ Claude
SYSTEM = """Eres el redactor de previas de un blog en español de España sobre el AC Milan.
Recibes datos verificados del partido. Usa la búsqueda web para completar lo que falta
(bajas y sanciones, alineaciones probables, árbitro y sus medias de tarjetas, estadio y aforo,
previsión del tiempo a la hora del partido, televisiones y cuotas o probabilidades de victoria).

Reglas:
- Los datos que te doy (clasificación, resultados, historial) mandan sobre lo que encuentres.
- No inventes. Si algo no se puede confirmar, escribe "por confirmar".
- Tono periodístico, frases propias, sin copiar textos de las fuentes.
- En los textos solo puedes usar las etiquetas <strong> y <em>.

Responde SOLO con un JSON dentro de <json></json>, con esta forma:
{
  "titulo": "Previa Milan - Atalanta: ...",
  "color_rival": "#1E71B8",                      // color principal del rival en hex
  "estadio": "San Siro",
  "estadio_sub": "Milán · 75.817 af.",
  "arbitro": "Nombre Apellido",
  "arbitro_rojas": "0.23",                        // rojas por partido esta temporada o la anterior
  "arbitro_amarillas": "4.19",                    // amarillas por partido
  "tiempo_ciudad": "Milán",
  "tiempo_temp": "17°C",
  "tiempo_desc": "Nublado · 45% de lluvia",
  "contexto": ["párrafo 1", "párrafo 2", "párrafo 3"],
      // 1: situación del Milan y cómo llega. 2: el rival, bajas y precedentes.
      // 3: probabilidades (victoria Milan / empate / rival en %) y qué se juega cada uno en la tabla.
      // Nombres de equipo en <strong>.
  "claves": [{"titulo": "...", "texto": "..."}],  // exactamente 5, títulos cortos
  "jugadores_rival": [{"nombre": "...", "posicion": "...", "texto": "..."}],  // 5 del rival
  "alineacion": ["párrafo sobre el once del Milan", "párrafo sobre el once del rival"],
  "tv": [{"pais": "🇪🇸 España", "canal": "DAZN", "tipo": "Cable/SAT"}]
      // una entrada por cada país de la lista que te paso, en ese orden;
      // tipo: Cable/SAT, Cable/STR, STR o Abierto
}"""


def pedir_a_claude(datos):
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": datos}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 12}]
    for _ in range(5):
        resp = client.messages.create(model=MODEL, max_tokens=12000, system=SYSTEM,
                                      tools=tools, messages=messages)
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    texto = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"<json>(.*?)</json>", texto, re.S)
    if not m:
        raise RuntimeError("Respuesta sin JSON:\n" + texto[:2000])
    bruto = re.sub(r"^\s*```(?:json)?|```\s*$", "", m.group(1).strip())
    return json.loads(bruto)


# ================================================================ render
def render(plantilla, valores):
    def sub(m):
        return valores[m.group(1)]
    return re.sub(r"\{\{(\w+)\}\}", sub, plantilla)


def construir(partido):
    ko = local_dt(partido)
    home, away = partido["homeTeam"], partido["awayTeam"]
    milan_local = home["id"] == MILAN_ID
    rival = away if milan_local else home
    comp = partido["competition"]
    anio = int(partido["season"]["startDate"][:4])
    temporada_corta = f"{anio % 100:02d}/{(anio + 1) % 100:02d}"

    if partido.get("matchday") and comp["code"] == "SA":
        jornada_largo, jornada_corto = f'Jornada {partido["matchday"]}', f'J{partido["matchday"]}'
    else:
        fase = (partido.get("stage") or "").replace("_", " ").title()
        jornada_largo = fase + (f' · J{partido["matchday"]}' if partido.get("matchday") else "")
        jornada_corto = fase

    tabla = puestos(partido)
    forma_h = ultimos_liga(home["id"], anio)
    forma_a = ultimos_liga(away["id"], anio)
    precedentes = h2h(partido)

    datos = "\n".join([
        f'PARTIDO: {home["name"]} vs {away["name"]}',
        f'Competición: {comp["name"]} · {jornada_largo} · temporada {anio}/{anio + 1}',
        f'Fecha y hora (Madrid): {ko:%d/%m/%Y %H:%M}',
        f'Puesto {home["shortName"]}: {tabla.get(home["id"], "?")}',
        f'Puesto {away["shortName"]}: {tabla.get(away["id"], "?")}',
        "", f'ÚLTIMOS PARTIDOS {home["shortName"]} (todas las competiciones):',
        *map(linea, ultimos_todos(home["id"])),
        "", f'ÚLTIMOS PARTIDOS {away["shortName"]} (todas las competiciones):',
        *map(linea, ultimos_todos(away["id"])),
        "", "PRECEDENTES:", *map(linea, precedentes),
        "", "PAÍSES PARA LA TABLA DE TV: " + ", ".join(PAISES_TV),
    ])
    ia = pedir_a_claude(datos)

    color = ia.get("color_rival", "")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color or ""):
        color = "#1E71B8"

    tv = ia.get("tv") or [{"pais": p, "canal": "por confirmar", "tipo": ""} for p in PAISES_TV]
    tv_html = "\n".join(
        f'        <div class="tv-fila{" tv-fila--destacado" if i == 0 else ""}"><span class="tv-pais">{plano(t.get("pais"))}</span>'
        f'<div style="display:flex; align-items:center;"><span class="tv-canal">{plano(t.get("canal"))}</span>'
        f'<span class="tv-tipo">{plano(t.get("tipo"))}</span></div></div>'
        for i, t in enumerate(tv))

    claves_html = "\n".join(
        '          <div class="clave">\n'
        f'            <div class="clave-n">{i}</div>\n'
        '            <div class="clave-body">\n'
        f'              <strong>{plano(c.get("titulo"))}</strong>\n'
        f'              <p>{rico(c.get("texto"))}</p>\n'
        '            </div>\n'
        '          </div>'
        for i, c in enumerate(ia.get("claves", [])[:5], 1))

    jugadores_html = "\n".join(
        f'          <li><b>{plano(j.get("nombre"))} ({plano(j.get("posicion"))}):</b> {rico(j.get("texto"))}</li>'
        for j in ia.get("jugadores_rival", [])[:5])

    valores = {
        "color_rival": color,
        "competicion_temporada": plano(f'{comp["name"]} {temporada_corta}'),
        "competicion_temporada_larga": plano(f'{comp["name"]} {anio}/{(anio + 1) % 100:02d}'),
        "competicion": plano(comp["name"]),
        "temporada": f"Temporada {anio}/{(anio + 1) % 100:02d}",
        "jornada_largo": plano(jornada_largo),
        "jornada_corto": plano(jornada_corto),
        "fecha_larga": f"{ko.day} {MESES[ko.month - 1]} {ko.year}",
        "hora": f"{ko:%H:%M}",
        "zona_horaria": ko.tzname(),
        "nombre_local": plano(home["shortName"]),
        "nombre_visitante": plano(away["shortName"]),
        "nombre_local_html": html_nombre(home["shortName"], milan_local),
        "nombre_visitante_html": html_nombre(away["shortName"], not milan_local),
        "puesto_local": plano(tabla.get(home["id"], "")),
        "puesto_visitante": plano(tabla.get(away["id"], "")),
        "rival_mayus": plano(rival["shortName"].upper()),
        "estadio": plano(ia.get("estadio")),
        "estadio_sub": plano(ia.get("estadio_sub")),
        "arbitro": plano(ia.get("arbitro")),
        "arbitro_rojas": plano(ia.get("arbitro_rojas", "–")),
        "arbitro_amarillas": plano(ia.get("arbitro_amarillas", "–")),
        "tiempo_ciudad": plano(ia.get("tiempo_ciudad", "")),
        "tiempo_temp": plano(ia.get("tiempo_temp")),
        "tiempo_desc": plano(ia.get("tiempo_desc")),
        "contexto_html": "\n\n".join(f"        <p>{rico(p)}</p>" for p in ia.get("contexto", [])),
        "forma_local_html": html_forma(forma_h, home["id"]),
        "forma_visitante_html": html_forma(forma_a, away["id"]),
        "historial_html": html_historial(precedentes),
        "claves_html": claves_html,
        "jugadores_html": jugadores_html,
        "alineacion_html": "\n".join(f"        <p>{rico(p)}</p>" for p in ia.get("alineacion", [])),
        "tv_html": tv_html,
        "placeholder_img_jugadores": placeholder_img(f"Insertar imagen de jugadores del {rival['shortName']}"),
        "placeholder_img_alineaciones": placeholder_img("Insertar imágenes de las alineaciones probables"),
    }
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        cuerpo = render(f.read(), valores)
    titulo = ia.get("titulo") or f'Previa {home["shortName"]} - {away["shortName"]}'
    return titulo, cuerpo


# ================================================================ WordPress
def wp(method, path, **kw):
    r = requests.request(method, f'{os.environ["WP_URL"].rstrip("/")}/wp-json/wp/v2{path}',
                         auth=(os.environ["WP_USER"], os.environ["WP_APP_PASSWORD"]),
                         timeout=60, **kw)
    if not r.ok:
        raise RuntimeError(f"WordPress respondió {r.status_code} en {path}: {r.text[:500]}")
    return r.json()


def main():
    fecha = fecha_objetivo()
    partido = partido_de(fecha)
    if not partido:
        print(f"El Milan no juega el {fecha}. Nada que hacer.")
        return

    home, away = partido["homeTeam"]["shortName"], partido["awayTeam"]["shortName"]
    slug = slugify(f"previa {home} {away} {fecha}")
    dry = os.getenv("DRY_RUN") == "1"

    if not dry and wp("GET", "/posts", params={"slug": slug, "status": "draft,publish,future,pending"}):
        print(f"Ya existe un post '{slug}'. No se duplica.")
        return

    print(f"Generando previa {home} - {away}...")
    titulo, cuerpo = construir(partido)

    if dry:
        with open("salida_previa.html", "w", encoding="utf-8") as f:
            f.write(cuerpo)
        print(f"DRY_RUN: '{titulo}' guardado en salida_previa.html")
        return

    payload = {"title": titulo, "content": cuerpo, "status": "draft", "slug": slug}
    if os.getenv("WP_CATEGORY_ID"):
        payload["categories"] = [int(os.environ["WP_CATEGORY_ID"])]
    post = wp("POST", "/posts", json=payload)
    print(f"Borrador creado: {post['id']} -> {post['link']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
