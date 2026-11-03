"""Lógica de auditoría de referencias (sin dependencias de Streamlit)."""
import re
import time
import unicodedata
import urllib.parse

import requests
from rapidfuzz import fuzz

UMBRAL_TITULO = 85          # similitud mínima para aceptar una obra por título
UMBRAL_TITULO_DOI = 78      # más laxo cuando el DOI ya coincide
MAX_REFERENCIAS = 150

# ---------------------------------------------------------
# Utilidades
# ---------------------------------------------------------


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


DOI_REGEX = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")


def limpiar_doi(doi: str) -> str:
    d = (doi or "").strip().rstrip(".,;]>\"' ")
    # Quitar ")" final solo si sobra (no pertenece al DOI, p. ej. "(doi:10.1000/xyz)")
    while d.endswith(")") and d.count(")") > d.count("("):
        d = d[:-1].rstrip(".,;]>\"' ")
    return d


# ---------------------------------------------------------
# 1. Aislar y dividir la sección de referencias
# ---------------------------------------------------------
HEAD_REF = re.compile(
    r"(?im)^\s*(?:\d+[\.\-\s]*)?(referencias(?:\s+bibliogr[aá]ficas)?|bibliograf[ií]a|"
    r"references|bibliography|literatura\s+citada)\s*:?\s*$"
)
# Encabezado real de anexo: la palabra sola o seguida de un identificador corto
# (A, 1, IV) y, como mucho, un subtítulo breve. No cuenta "Anexo Estadístico del INEC...".
HEAD_FIN = re.compile(
    r"(?im)^\s*(?:\d+[\.\-\s]*)?(?:anexos?|ap[eé]ndices?|appendix|appendices)\b"
    r"(?:\s+(?-i:[A-Z]|\d{1,2}|[IVX]{1,4})\b[\.:\-–]?(?:\s+[^\n]{0,40})?)?\s*$"
)


def aislar_seccion_referencias(texto: str) -> str:
    """Toma desde el último encabezado de referencias hasta anexos/apéndices."""
    coincidencias = list(HEAD_REF.finditer(texto))
    if not coincidencias:
        return texto.strip()
    resto = texto[coincidencias[-1].end():].strip()
    fin = HEAD_FIN.search(resto)
    return (resto[:fin.start()] if fin else resto).strip()


YEAR_ANY = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
INICIO_REF = re.compile(
    r"^(\[\d+\]|\d+[\.\)]\s+"
    r"|[^\W\d_][\w'’\-]*(?:\s+(?:de|del|la|las|los|van|von|der|den|di|da|bin|[^\W\d_][\w'’\-]*)){0,4},\s+[A-ZÁÉÍÓÚÑÜ]\."
    r"|[^\W\d_][^()]{1,80}\.\s*\(\s*(?:19|20)\d{2})"
)


def dividir_en_referencias(texto: str, desde_docx: bool = False) -> list[str]:
    """Devuelve una lista de citas completas.

    - DOCX: cada párrafo es una referencia (no se re-unen líneas).
    - PDF/texto pegado: se re-unen líneas cortadas cuando la cita anterior parece
      completa (termina en punto/URL/DOI y contiene año) y la línea nueva parece
      el inicio de una cita (Apellido, N. / [n] / Organización. (año)).
    """
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    if desde_docx:
        bloques = lineas
    else:
        bloques, buf = [], ""
        for l in lineas:
            if re.fullmatch(r"\d{1,3}", l):  # número de página suelto
                continue
            if not buf:
                buf = l
                continue
            completa = bool(
                re.search(r"(?:[\.\)]|https?://\S+|10\.\d{4,9}/\S+)$", buf)
            ) and bool(YEAR_ANY.search(buf))
            if completa and INICIO_REF.match(l):
                bloques.append(buf)
                buf = l
            else:
                buf += " " + l
        if buf:
            bloques.append(buf)
    limpias = (re.sub(r"\s+", " ", b).strip() for b in bloques)
    return [b for b in limpias if len(b) > 25]


# ---------------------------------------------------------
# 2. Parseo heurístico de una cita
# ---------------------------------------------------------
def parsear_referencia(cita: str) -> dict:
    cita = re.sub(r"\s+", " ", cita).strip()
    cita = re.sub(r"^(\[\d+\]|\d+[\.\)])\s*", "", cita)

    m_doi = DOI_REGEX.search(cita)
    doi = limpiar_doi(m_doi.group(1)) if m_doi else ""

    m_y = re.search(r"\(\s*((?:19|20)\d{2})[a-z]?[^)]*\)", cita) or re.search(
        r"\b((?:19|20)\d{2})\b", cita
    )
    anio = m_y.group(1) if m_y else ""

    sin_urls = re.sub(r"https?://\S+|10\.\d{4,9}/\S+", "", cita).strip()
    titulo = ""
    m_apa = re.search(
        r"\(\s*(?:19|20)\d{2}[a-z]?[^)]*\)\s*\.?\s*(.+?)(?:\.\s|\?\s|$)", sin_urls
    )
    if m_apa and len(m_apa.group(1).strip()) > 12:
        titulo = m_apa.group(1).strip()
    else:
        m_q = re.search(r"[\"“«](.{15,}?)[\"”»]", sin_urls)
        if m_q:
            titulo = m_q.group(1).strip()
        else:
            segs = [s.strip() for s in sin_urls.split(".") if len(s.strip()) > 20]
            titulo = (segs[1] if len(segs) >= 2 else (max(segs, key=len) if segs else sin_urls))

    m_a = re.match(r"^\s*([^,\(\.]+?)\s*[,\.\(]", cita)
    autor = m_a.group(1).strip() if m_a else ""

    return {
        "cita_original": cita,
        "titulo_busqueda": titulo,
        "anio": anio,
        "doi_declarado": doi,
        "autor_citado": autor,
    }


# ---------------------------------------------------------
# 3. HTTP robusto
# ---------------------------------------------------------
def headers(email: str) -> dict:
    return {"User-Agent": f"AuditorReferencias/2.0 (mailto:{email})"}


def http_get(url: str, email: str, params: dict | None = None, intentos: int = 3):
    """GET con reintentos. Devuelve Response o None si hubo fallo de red/límite."""
    for i in range(intentos):
        try:
            r = requests.get(url, headers=headers(email), params=params, timeout=15)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (i + 1))
                continue
            return r
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return None


# ---------------------------------------------------------
# 4. Comparaciones
# ---------------------------------------------------------
def similitud_titulo(titulo_citado: str, titulo_real: str, cita: str) -> int:
    a, b, c = norm(titulo_citado), norm(titulo_real), norm(cita)
    if not b:
        return 0
    s1 = fuzz.token_sort_ratio(a, b)
    s2 = fuzz.partial_ratio(b, c) if len(b) >= 25 else 0
    return int(max(s1, s2))


def discrepancias(cita: str, anio_citado: str, familia_oficial: str, anio_oficial: str) -> list[str]:
    out = []
    inicio = norm(cita[:250])
    # Apellidos compuestos ("Sornoza Parrales"): basta con que aparezca alguno de sus componentes
    tokens = [t for t in norm(familia_oficial).split() if len(t) > 2]
    if tokens and not any(t in inicio for t in tokens):
        out.append(f"el primer autor oficial ({familia_oficial}) no aparece al inicio de la cita")
    if anio_citado.isdigit() and str(anio_oficial).isdigit():
        if abs(int(anio_citado) - int(anio_oficial)) > 1:
            out.append(f"año citado {anio_citado} ≠ año oficial {anio_oficial}")
    return out


def _anio_crossref(msg: dict) -> str:
    for campo in ("published-print", "published-online", "issued"):
        partes = (msg.get(campo) or {}).get("date-parts") or [[None]]
        if partes and partes[0] and partes[0][0]:
            return str(partes[0][0])
    return ""


def _autor_crossref(msg: dict) -> str:
    autores = msg.get("author") or []
    if not autores:
        return ""
    a = autores[0]
    return a.get("family") or a.get("name") or ""


def _retractado_crossref(msg: dict, titulo: str) -> bool:
    for u in msg.get("updated-by") or []:
        if str(u.get("type", "")).lower() in ("retraction", "withdrawal", "removal"):
            return True
    return titulo.strip().upper().startswith(("RETRACTED", "WITHDRAWN"))


def _scholar(titulo: str) -> str:
    return "https://scholar.google.com/scholar?q=" + urllib.parse.quote(titulo[:200])


# ---------------------------------------------------------
# 5. Auditoría de una referencia
# ---------------------------------------------------------
def _fila(estado, sim, item, titulo_of="—", autor_of="—", anio_of="—", doi_of="—",
          fuente="—", obs="", retractado="", citas=""):
    return {
        "Estado": estado,
        "Similitud": f"{sim}%" if isinstance(sim, int) else sim,
        "Cita en el Documento": item["cita_original"],
        "Título Oficial Encontrado": titulo_of,
        "Primer Autor Oficial": autor_of or "—",
        "Año Oficial": anio_of or "—",
        "DOI Oficial": doi_of,
        "Fuente": fuente,
        "Observaciones": obs,
        "Retractado": retractado,
        "Citas (OpenAlex)": citas,
        "Verificar a mano": "" if estado.startswith("🟢") else _scholar(
            item.get("titulo_busqueda") or item["cita_original"]
        ),
    }


def auditar_referencia(item: dict, email: str) -> dict:
    cita = item["cita_original"]
    titulo_c = (item.get("titulo_busqueda") or "").strip()
    anio_c = str(item.get("anio") or "").strip()
    doi_c = limpiar_doi(str(item.get("doi_declarado") or ""))
    error_red = False
    doi_inexistente = False
    nota_doi = ""

    # ---- PASO 1: DOI declarado (Crossref -> DataCite) ----
    if doi_c:
        r = http_get(f"https://api.crossref.org/works/{urllib.parse.quote(doi_c)}", email)
        info = None
        if r is None:
            error_red = True
        elif r.status_code == 200:
            msg = r.json().get("message", {})
            t = (msg.get("title") or [""])[0]
            info = dict(titulo=t, autor=_autor_crossref(msg), anio=_anio_crossref(msg),
                        fuente="Crossref (DOI)", retr=_retractado_crossref(msg, t))
        elif r.status_code == 404:
            r2 = http_get(f"https://api.datacite.org/dois/{urllib.parse.quote(doi_c)}", email)
            if r2 is None:
                error_red = True
            elif r2.status_code == 200:
                at = r2.json().get("data", {}).get("attributes", {})
                t = ((at.get("titles") or [{}])[0]).get("title", "")
                cr = (at.get("creators") or [{}])[0]
                info = dict(titulo=t, autor=cr.get("familyName") or cr.get("name", ""),
                            anio=str(at.get("publicationYear") or ""),
                            fuente="DataCite (DOI)", retr=False)
            else:
                doi_inexistente = True
        if info:
            sim = similitud_titulo(titulo_c, info["titulo"], cita)
            retr = "⚠️ SÍ" if info["retr"] else "No"
            if sim >= UMBRAL_TITULO_DOI:
                disc = discrepancias(cita, anio_c, info["autor"], info["anio"])
                estado = "🟡 Existe, con discrepancias" if disc else "🟢 Verificada (DOI exacto)"
                return _fila(estado, sim, item, info["titulo"], info["autor"], info["anio"],
                             f"https://doi.org/{doi_c}", info["fuente"], "; ".join(disc), retr)
            return _fila("🟠 DOI cruzado (pertenece a otra obra)", sim, item,
                         f"[El DOI pertenece a]: {info['titulo']}", info["autor"], info["anio"],
                         f"https://doi.org/{doi_c}", info["fuente"],
                         "El DOI existe pero el título no coincide con la cita", retr)
        if doi_inexistente:
            nota_doi = "El DOI declarado no existe en Crossref ni DataCite. "

    # ---- PASO 2: búsqueda por título (Crossref + OpenAlex) ----
    candidatos = []
    consulta = titulo_c if len(titulo_c) > 15 else cita

    r = http_get("https://api.crossref.org/works", email,
                 params={"query.bibliographic": consulta[:300], "rows": 5, "mailto": email})
    if r is None:
        error_red = True
    elif r.status_code == 200:
        for c in r.json().get("message", {}).get("items", []):
            t = (c.get("title") or [""])[0]
            if t:
                candidatos.append(dict(titulo=t, autor=_autor_crossref(c), anio=_anio_crossref(c),
                                       doi=c.get("DOI", ""), fuente="Crossref (búsqueda)",
                                       retr=_retractado_crossref(c, t), citas=""))

    mejor = max(candidatos, key=lambda c: similitud_titulo(titulo_c, c["titulo"], cita), default=None)
    if not mejor or similitud_titulo(titulo_c, mejor["titulo"], cita) < UMBRAL_TITULO:
        limpio = re.sub(r"[^\w\sáéíóúñÁÉÍÓÚÑ]", " ", consulta).strip()
        r = http_get("https://api.openalex.org/works", email,
                     params={"search": limpio[:250], "per-page": 5, "mailto": email})
        if r is None:
            error_red = True
        elif r.status_code == 200:
            for w in r.json().get("results", []):
                t = w.get("title") or ""
                if not t:
                    continue
                au = (w.get("authorships") or [{}])
                nombre = (au[0].get("author") or {}).get("display_name", "") if au else ""
                partes = nombre.split()  # "Nombre Apellido1 Apellido2" -> conserva ambos apellidos
                familia = " ".join(partes[1:]) if len(partes) > 1 else nombre
                candidatos.append(dict(titulo=t, autor=familia, anio=str(w.get("publication_year") or ""),
                                       doi=(w.get("doi") or "").replace("https://doi.org/", ""),
                                       fuente="OpenAlex", retr=bool(w.get("is_retracted")),
                                       citas=w.get("cited_by_count", "")))
        mejor = max(candidatos, key=lambda c: similitud_titulo(titulo_c, c["titulo"], cita), default=None)

    if mejor:
        sim = similitud_titulo(titulo_c, mejor["titulo"], cita)
        if sim >= UMBRAL_TITULO:
            disc = discrepancias(cita, anio_c, mejor["autor"], mejor["anio"])
            if doi_c and not doi_inexistente and not error_red:
                disc.append("el DOI declarado no coincide con la obra encontrada")
            obs = nota_doi + "; ".join(disc)
            estado = "🟡 Existe, con discrepancias" if (disc or nota_doi) else "🟢 Verificada (por título)"
            return _fila(estado, sim, item, mejor["titulo"], mejor["autor"], mejor["anio"],
                         f"https://doi.org/{mejor['doi']}" if mejor["doi"] else "Sin DOI",
                         mejor["fuente"], obs.strip(), "⚠️ SÍ" if mejor["retr"] else "No", mejor["citas"])

    # ---- PASO 3: sin coincidencia ----
    if doi_inexistente:
        return _fila("🔴 DOI inexistente y obra no localizada", 0, item, fuente="Ninguna",
                     obs=nota_doi + "Revisar con urgencia: posible referencia inventada.")
    if error_red:
        return _fila("⚪ No se pudo verificar (error de red o límite de la API)", "—", item,
                     fuente="—", obs="Reintentar más tarde; no implica que la referencia sea falsa.")
    return _fila("⚪ No encontrada en Crossref/OpenAlex: revisar manualmente", 0, item,
                 fuente="Ninguna",
                 obs="Libros, informes, leyes, tesis y páginas web suelen no estar indexados. "
                     "No implica que sea falsa.")
