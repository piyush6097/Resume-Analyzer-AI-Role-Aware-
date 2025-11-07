# streamlit_app.py
import os
import time
import json
import unicodedata
from io import BytesIO

import streamlit as st
import pandas as pd
import fitz  # PyMuPDF

from analyse_pdf import analyse_resume_st, MODEL_VERSION
from hash_resume import compute_sha256
from db_mysql import (
    init_db, get_cached_score, save_score,
    add_job_description, list_job_descriptions,
    list_cached_candidates, delete_cached, get_job_description,
    list_roles
)

# Ensure uploads dir exists
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Initialize DB (db_mysql handles fallback). Do not crash UI.
try:
    init_db()
except Exception as e:
    # init_db in db_mysql raises a sanitized RuntimeError on failure; ignore here
    st.warning("Database initialization failed or DB not available — running in limited mode.")

st.set_page_config(page_title="Resume Analyzer", layout="wide")
st.title("Resume Analyzer — Role Aware")

# Sidebar: Job/Role controls
with st.sidebar:
    st.header("Job / Role")
    # load saved JDs
    try:
        jds = list_job_descriptions()
    except Exception:
        jds = []
    jd_names = ["(paste JD)"]
    jd_map = {"(paste JD)": None}
    for jd in jds:
        # jd tuple: (id, name, description, created_at)
        label = f"{jd[0]}: {jd[1]}"
        jd_names.append(label)
        jd_map[label] = jd[0]

    selected_jd = st.selectbox("Choose saved JD (optional)", jd_names)
    role_list = []
    try:
        role_list = list_roles()
    except Exception:
        role_list = []
    role_choice = st.selectbox("Role (optional)", [""] + role_list)

    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "10"))
    st.markdown(f"Max upload per file: **{max_upload_mb} MB**")

st.subheader("Job Description")
job_description = st.text_area("Paste job description here (leave blank to use selected saved JD)", height=200)

uploaded_files = st.file_uploader("Upload one or more resume PDFs", type=["pdf"], accept_multiple_files=True)

def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Return extracted text from PDF bytes using PyMuPDF."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        texts = []
        for page in doc:
            texts.append(page.get_text())
        doc.close()
        return " ".join(texts)
    except Exception as e:
        st.error(f"PDF text extraction error: {e}")
        return ""

def _safe_join(maybe_list_or_str):
    if maybe_list_or_str is None:
        return ""
    if isinstance(maybe_list_or_str, (list, tuple, set)):
        return ";".join([str(x) for x in maybe_list_or_str])
    return str(maybe_list_or_str)

def _safe_text(text):
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.replace("\r", " ").replace("\n", " ").replace(";", ",")

if st.button("Analyze resumes"):
    if not uploaded_files:
        st.error("Please upload at least one PDF resume.")
    else:
        # Determine JD text and id
        jd_id = None
        jd_text = job_description.strip()
        if selected_jd and selected_jd != "(paste JD)":
            jd_id = jd_map.get(selected_jd)
            if not jd_text:
                try:
                    row = get_job_description(jd_id)
                    jd_text = row[2] if row else ""
                except Exception:
                    jd_text = ""

        results = []
        for up in uploaded_files:
            filename = up.name
            # size check
            try:
                up.seek(0, os.SEEK_END)
                size_bytes = up.tell()
                up.seek(0)
            except Exception:
                size_bytes = None

            if size_bytes and size_bytes > max_upload_mb * 1024 * 1024:
                st.warning(f"Skipping {filename}: file > {max_upload_mb} MB")
                continue

            # Save uploaded file to disk (so compute_sha256 and any other code that expects a path works)
            file_path = os.path.join(UPLOAD_DIR, filename)
            try:
                with open(file_path, "wb") as f:
                    f.write(up.read())
            except Exception as e:
                st.error(f"Failed to save {filename}: {e}")
                continue

            # compute hash
            try:
                resume_hash = compute_sha256(file_path)
            except Exception as e:
                st.error(f"Failed to compute hash for {filename}: {e}")
                resume_hash = ""

            # extract text
            try:
                with open(file_path, "rb") as fh:
                    pdf_bytes = fh.read()
                resume_text = extract_text_from_pdf_bytes(pdf_bytes)
            except Exception as e:
                st.error(f"Failed to read {filename}: {e}")
                resume_text = ""

            # Call your analyser (it expects text in the Flask app); pass resume_text
            try:
                analysis = analyse_resume_st(resume_text, jd_text, role_choice or None)
            except Exception as e:
                st.error(f"Analysis error for {filename}: {e}")
                analysis = {"overall": 0, "raw_text": resume_text, "jd_hash": ""}

            jd_hash = analysis.get("jd_hash", "")

            # Try cache lookup
            cached_score = None
            try:
                cached_score = get_cached_score(resume_hash, jd_hash, role_choice or "", MODEL_VERSION)
            except Exception:
                cached_score = None

            if cached_score is not None:
                analysis["overall"] = cached_score
                analysis["cached"] = True
            else:
                try:
                    save_score(
                        resume_hash, jd_hash, role_choice or "",
                        analysis.get("overall", 0),
                        model_version=MODEL_VERSION,
                        jd_id=jd_id, source_filename=filename
                    )
                except Exception:
                    # ignore DB save errors
                    pass
                analysis["cached"] = False

            results.append({
                "filename": filename,
                "role": role_choice or "",
                "summary": analysis.get("raw_text", ""),
                "details": analysis,
                "overall": analysis.get("overall", 0),
                "cached": analysis.get("cached", False)
            })

        if results:
            st.success("Analysis complete")
            # show a dataframe summary
            rows = []
            for r in results:
                d = r.get("details", {}) or {}
                rows.append({
                    "filename": _safe_text(r.get("filename","")),
                    "role": _safe_text(r.get("role","")),
                    "overall": d.get("overall", r.get("overall", 0)),
                    "matching_core": ", ".join(sorted(d.get("matching_core", []))) if isinstance(d.get("matching_core", []), (list,set)) else d.get("matching_core",""),
                    "missing_core": ", ".join(sorted(d.get("missing_core", []))) if isinstance(d.get("missing_core", []), (list,set)) else d.get("missing_core",""),
                    "cached": r.get("cached", False)
                })
            df = pd.DataFrame(rows)
            st.dataframe(df)

            # Excel export
            def to_excel_bytes(df_obj):
                out = BytesIO()
                with pd.ExcelWriter(out, engine="openpyxl") as writer:
                    df_obj.to_excel(writer, index=False, sheet_name="results")
                out.seek(0)
                return out.read()

            excel_bytes = to_excel_bytes(df)
            st.download_button("Download results (.xlsx)", data=excel_bytes, file_name=f"results_{int(time.time())}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            st.info("No results produced (all files skipped or errors occurred).")

# Admin tools (small)
st.sidebar.markdown("---")
st.sidebar.markdown("Admin tools")
if st.sidebar.button("List saved JDs"):
    try:
        jds = list_job_descriptions()
        st.sidebar.write(jds)
    except Exception as e:
        st.sidebar.error("Failed to list JDs: " + str(e))
