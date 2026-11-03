import io
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st
from docx import Document
from pypdf import PdfReader
from rapidfuzz import fuzz

from auditor_core import (
    MAX_REFERENCIAS,
    aislar_seccion_referencias,
    auditar_referencia,
    dividir_en_referencias,
    norm,
    parsear_referencia,
)

st.set_page_config(page_title="Auditor de Referencias Académicas", page_icon="📚", layout="wide")


def correo_por_defecto() -> str:
    """Correo opcional guardado en Settings > Secrets de Streamlit Cloud (CONTACT_EMAIL)."""
    try:
        return st.secrets.get("CONTACT_EMAIL", "")
    except Exception:
        return ""


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------- Lectura de archivos ----------
def extraer_texto_docx(archivo) -> str:
    doc = Document(archivo)
    return "\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())


def extraer_texto_pdf(archivo) -> str:
    reader = PdfReader(archivo)
    return "\n".join(filter(None, (p.extract_text() for p in reader.pages)))


# ---------- Extracción opcional con Gemini ----------
def extraer_con_gemini(texto_ref: str, api_key: str) -> tuple[list[dict], int]:
    """Devuelve (referencias válidas, cantidad descartada por no figurar en el texto original)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    prompt = (
        "Separa las referencias bibliográficas del texto. Devuelve SOLO un arreglo JSON; cada objeto "
        "con las claves: 'cita_original' (copia TEXTUAL, sin corregir nada), 'titulo_busqueda', "
        "'anio' (4 dígitos o vacío), 'doi_declarado' (solo 10.xxxx/... o vacío). "
        "No completes, corrijas ni inventes datos.\n\nTEXTO:\n" + texto_ref[:35000]
    )
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
    )
    datos = json.loads(resp.text)
    if not isinstance(datos, list):
        raise ValueError("La respuesta de Gemini no es una lista JSON")

    texto_norm = norm(texto_ref)
    validas, descartadas = [], 0
    for d in datos:
        if not isinstance(d, dict) or not d.get("cita_original"):
            descartadas += 1
            continue
        cita = re.sub(r"\s+", " ", str(d["cita_original"])).strip()
        # Anti-"corrección" por IA: la cita debe existir textualmente en el documento
        if fuzz.partial_ratio(norm(cita), texto_norm) < 92:
            descartadas += 1
            continue
        base = parsear_referencia(cita)  # rellena autor_citado y huecos
        base["titulo_busqueda"] = str(d.get("titulo_busqueda") or base["titulo_busqueda"])
        base["anio"] = str(d.get("anio") or base["anio"])
        base["doi_declarado"] = str(d.get("doi_declarado") or base["doi_declarado"])
        validas.append(base)
    return validas, descartadas


# ---------- Interfaz ----------
st.title("📚 Auditor de Referencias Bibliográficas")
st.markdown(
    "Sube tu manuscrito (**.docx** o **.pdf**) o pega tu lista de referencias para comprobar si existen en "
    "Crossref, DataCite y OpenAlex, y si autor, año y DOI coinciden. "
    "**Un resultado ⚪ no significa que la referencia sea falsa:** libros, informes, leyes y tesis "
    "suelen no estar indexados; deben revisarse a mano."
)

with st.sidebar:
    st.header("⚙️ Configuración")
    email_usuario = st.text_input(
        "Tu correo (requerido por Crossref/OpenAlex):",
        value=correo_por_defecto(),
        help="Solo se usa para identificarte ante las APIs (Polite Pool). No se almacena. Pulsa Enter para confirmarlo.",
    )
    # Confirmación visible del correo
    if not email_usuario.strip():
        st.caption("✉️ Escribe tu correo y pulsa Enter.")
    elif EMAIL_RE.match(email_usuario.strip()):
        st.success(f"✅ Correo registrado: {email_usuario.strip()}")
    else:
        st.warning("⚠️ El correo no parece válido.")
    metodo = st.radio(
        "Separación de citas:",
        ["Automática (sin API Key)", "Con Gemini 2.5 Flash (opcional)"],
    )
    gemini_key = ""
    if metodo.startswith("Con Gemini"):
        gemini_key = st.text_input("API Key de Google AI Studio:", type="password")

    st.markdown("---")
    st.markdown(
        "**Leyenda**\n"
        "- 🟢 Verificada: título, autor y año coinciden.\n"
        "- 🟡 Existe, pero hay discrepancias (autor, año o DOI).\n"
        "- 🟠 DOI cruzado: el DOI pertenece a otra obra.\n"
        "- 🔴 DOI inexistente y obra no localizada.\n"
        "- ⚪ No verificable automáticamente: revisar a mano."
    )

tab_archivo, tab_texto = st.tabs(["📄 Subir archivo", "📋 Pegar referencias"])
texto_para_auditar, desde_docx = "", False

with tab_archivo:
    archivo = st.file_uploader("Artículo en Word o PDF", type=["docx", "pdf"])
    if archivo is not None:
        es_docx = archivo.name.lower().endswith(".docx")
        bruto = extraer_texto_docx(archivo) if es_docx else extraer_texto_pdf(archivo)
        sec = aislar_seccion_referencias(bruto)
        with st.expander("Sección de referencias detectada (editable)", expanded=False):
            texto_para_auditar = st.text_area("Ajusta el texto antes de auditar:", value=sec, height=220)
        desde_docx = es_docx

with tab_texto:
    manual = st.text_area("Una referencia por línea o párrafo:", height=250)
    if manual.strip():
        texto_para_auditar, desde_docx = manual.strip(), True

if st.button("🔍 Auditar referencias", type="primary", use_container_width=True):
    if not EMAIL_RE.match(email_usuario.strip()):
        st.warning("Escribe un correo válido en la barra lateral.")
    elif not texto_para_auditar.strip():
        st.warning("Sube un documento o pega al menos una referencia.")
    else:
        email = email_usuario.strip()
        lista = None
        if metodo.startswith("Con Gemini") and gemini_key:
            try:
                lista, desc = extraer_con_gemini(texto_para_auditar, gemini_key)
                if desc:
                    st.warning(f"Gemini devolvió {desc} elemento(s) que no figuran textualmente en el documento; se descartaron.")
            except Exception as e:
                st.error(f"Error con Gemini ({e}). Se usa el extractor automático.")
        if lista is None:
            lista = [parsear_referencia(c) for c in dividir_en_referencias(texto_para_auditar, desde_docx)]

        if not lista:
            st.error("No se detectaron referencias en el texto.")
        else:
            if len(lista) > MAX_REFERENCIAS:
                st.warning(f"Se auditarán solo las primeras {MAX_REFERENCIAS} de {len(lista)} referencias.")
                lista = lista[:MAX_REFERENCIAS]
            st.info(f"Se detectaron **{len(lista)}** referencias. Consultando bases de datos…")
            barra = st.progress(0.0)
            resultados = [None] * len(lista)
            with ThreadPoolExecutor(max_workers=4) as ex:
                futuros = {ex.submit(auditar_referencia, it, email): i for i, it in enumerate(lista)}
                for k, fut in enumerate(as_completed(futuros), 1):
                    resultados[futuros[fut]] = fut.result()
                    barra.progress(k / len(lista))
            df = pd.DataFrame(resultados)
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as w:
                df.to_excel(w, index=False, sheet_name="Auditoria")
            # Se guardan para que sobrevivan al rerun del botón de descarga
            st.session_state["df"] = df
            st.session_state["xlsx"] = buffer.getvalue()

# ---------- Resultados (persisten en session_state) ----------
if "df" in st.session_state:
    df = st.session_state["df"]
    est = df["Estado"]
    c = st.columns(5)
    c[0].metric("🟢 Verificadas", int(est.str.startswith("🟢").sum()))
    c[1].metric("🟡 Discrepancias", int(est.str.startswith("🟡").sum()))
    c[2].metric("🟠 DOI cruzado", int(est.str.startswith("🟠").sum()))
    c[3].metric("🔴 DOI inexistente", int(est.str.startswith("🔴").sum()))
    c[4].metric("⚪ Revisar a mano", int(est.str.startswith("⚪").sum()))
    retr = int((df["Retractado"] == "⚠️ SÍ").sum())
    if retr:
        st.error(f"⚠️ {retr} referencia(s) figuran como RETRACTADAS. Revisa la columna 'Retractado'.")

    st.subheader("Resultados detallados")
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={"Verificar a mano": st.column_config.LinkColumn("Verificar a mano", display_text="Google Scholar")},
    )
    st.download_button(
        "📥 Descargar reporte (.xlsx)",
        data=st.session_state["xlsx"],
        file_name="Reporte_Auditoria_Referencias.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
