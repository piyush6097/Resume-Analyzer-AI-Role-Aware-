# streamlit_app.py
import os
import time
import json
import unicodedata
import traceback
from io import BytesIO
from typing import List, Dict, Any, Optional

import streamlit as st
import pandas as pd
import fitz  # PyMuPDF

# Your project imports (keeps existing analyze + DB code)
from analyse_pdf import analyse_resume_st, MODEL_VERSION
from hash_resume import compute_sha256
from db_mysql import (
    init_db, get_cached_score, save_score,
    add_job_description, list_job_descriptions,
    list_cached_candidates, delete_cached, get_job_description,
    list_roles, upsert_role, upsert_skill, get_role_skills_dict
)

# -------------------------
# Setup & helpers
# -------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Initialize DB (db_mysql has internal fallback behaviour)
try:
    init_db()
except Exception:
    traceback.print_exc()

st.set_page_config(page_title="Resume Analyzer AI - Role Aware", layout="wide")

def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using PyMuPDF (fitz)."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        texts = []
        for page in doc:
            texts.append(page.get_text())
        doc.close()
        return " ".join(texts)
    except Exception:
        traceback.print_exc()
        return ""

def _safe_join(maybe_list_or_str):
    if maybe_list_or_str is None:
        return ""
    if isinstance(maybe_list_or_str, (list, tuple, set)):
        return ";".join([str(x) for x in maybe_list_or_str])
    return str(maybe_list_or_str)

def _safe_text(text: Optional[str]) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    return normalized.replace("\r", " ").replace("\n", " ").replace(";", ",")

def results_to_dataframe(results: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for r in results:
        d = r.get("details", {}) or {}
        rows.append({
            "filename": _safe_text(r.get("filename","")),
            "role": _safe_text(r.get("role","")),
            "overall": d.get("overall", r.get("overall", 0)),
            "technical": d.get("technical", ""),
            "experience": d.get("experience", ""),
            "tools": d.get("tools", ""),
            "soft": d.get("soft", ""),
            "matching_core": _safe_join(d.get("matching_core", [])),
            "matching_tools": _safe_join(d.get("matching_tools", [])),
            "matching_soft": _safe_join(d.get("matching_soft", [])),
            "missing_core": _safe_join(d.get("missing_core", [])),
            "missing_tools": _safe_join(d.get("missing_tools", [])),
            "missing_soft": _safe_join(d.get("missing_soft", [])),
            "cached": r.get("cached", False),
            "summary": _safe_text(r.get("summary",""))
        })
    return pd.DataFrame(rows)

def save_df_to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="results")
    out.seek(0)
    return out.read()

# -------------------------
# UI Layout
# -------------------------
st.title("Resume Analyzer — Role Aware")

col_left, col_right = st.columns([1, 3])

with col_left:
    st.header("Controls")
    # Load saved JDs
    try:
        jds = list_job_descriptions()
    except Exception:
        traceback.print_exc()
        jds = []
    jd_options = ["(paste JD)"]
    jd_map = {"(paste JD)": None}
    for jd in jds:
        label = f"{jd[0]}: {jd[1]}"
        jd_options.append(label)
        jd_map[label] = jd[0]

    selected_jd_label = st.selectbox("Choose saved JD (optional)", jd_options)
    try:
        role_list = list_roles()
    except Exception:
        traceback.print_exc()
        role_list = []
    role_choice = st.selectbox("Role (optional)", options=[""] + role_list)

    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "10"))
    st.write(f"Max upload per file: **{max_upload_mb} MB**")

    st.markdown("---")
    st.subheader("Admin")
    if st.button("List saved JDs"):
        try:
            jds = list_job_descriptions()
            st.write(jds)
        except Exception:
            traceback.print_exc()
            st.error("Failed to list JDs (check logs).")

    if st.button("List cached candidates"):
        try:
            candidates = list_cached_candidates()
            st.write(candidates)
        except Exception:
            traceback.print_exc()
            st.error("Failed to list cached candidates (check logs).")

    st.markdown("Delete cached result")
    del_resume_hash = st.text_input("Resume hash (leave blank to skip)")
    del_jd_hash = st.text_input("JD hash (optional)")
    del_role = st.text_input("Role (optional)")
    if st.button("Delete cached"):
        if not del_resume_hash:
            st.warning("Provide resume_hash to delete (or use admin API externally).")
        else:
            try:
                delete_cached(del_resume_hash, del_jd_hash or None, del_role or None)
                st.success("Deleted cached rows (if existed).")
            except Exception:
                traceback.print_exc()
                st.error("Failed to delete cached rows (check logs).")

    st.markdown("---")
    # === Add Role ===
    st.subheader("Add Role")
    new_role_name = st.text_input("Role name", key="new_role_name", placeholder="e.g., Full Stack Developer")
    if st.button("Add Role"):
        if not new_role_name or not new_role_name.strip():
            st.warning("Provide a role name.")
        else:
            try:
                upsert_role(new_role_name.strip())
                st.success(f"Role '{new_role_name.strip()}' added (or already exists).")
                # refresh local role_list
                role_list = list_roles()
            except Exception:
                traceback.print_exc()
                st.error("Failed to add role. Check logs.")

    st.markdown("---")
    # === Add Skill ===
    st.subheader("Add Skill to Role")
    # ensure fresh roles list
    try:
        current_roles = list_roles()
    except Exception:
        traceback.print_exc()
        current_roles = []
    select_role_for_skill = st.selectbox("Role", options=[""] + current_roles, key="select_role_for_skill")
    skill_type = st.selectbox("Type", options=["core", "tools", "soft"], key="skill_type")
    skill_name = st.text_input("Skill name", key="skill_name_input", placeholder="e.g., react, docker, communication")
    if st.button("Add Skill"):
        if not select_role_for_skill:
            st.warning("Select a role first.")
        elif not skill_name or not skill_name.strip():
            st.warning("Provide a skill name.")
        else:
            try:
                upsert_skill(select_role_for_skill, skill_name.strip(), skill_type)
                st.success(f"Skill '{skill_name.strip()}' added to role '{select_role_for_skill}' as {skill_type}.")
            except Exception:
                traceback.print_exc()
                st.error("Failed to add skill. Check logs.")

with col_right:
    st.header("Job Description")
    job_description = st.text_area("Paste job description here (leave blank to use selected saved JD)", height=200)
    uploaded_files = st.file_uploader("Upload one or more resume PDFs", type=["pdf"], accept_multiple_files=True)

    analyze_button, export_button = st.columns([1, 1])
    with analyze_button:
        do_analyze = st.button("Analyze")
    with export_button:
        do_export = st.button("Export last results (XLSX)")

    st.markdown("---")
    st.info("If you select a saved JD and leave the JD text blank, the saved JD will be used.")

# -------------------------
# Main actions
# -------------------------
if "last_results" not in st.session_state:
    st.session_state["last_results"] = []

# Helper: resolve selected_jd -> jd_id and text
def resolve_jd_and_id(selected_label: str, pasted_text: str) -> (Optional[int], str):
    jd_id = None
    jd_text = pasted_text.strip() if pasted_text else ""
    if selected_label and selected_label != "(paste JD)":
        jd_id = jd_map.get(selected_label)
        if not jd_text and jd_id:
            try:
                row = get_job_description(jd_id)
                jd_text = row[2] if row else ""
            except Exception:
                traceback.print_exc()
                jd_text = ""
    return jd_id, jd_text

# ANALYZE flow
if do_analyze:
    if (not uploaded_files or len(uploaded_files) == 0):
        st.error("Please upload at least one PDF resume.")
    else:
        if not job_description.strip() and (selected_jd_label == "(paste JD)"):
            st.error("Provide a Job Description or select a saved JD.")
        else:
            st.info("Starting analysis... (this may take time)")
            jd_id, jd_text = resolve_jd_and_id(selected_jd_label, job_description)
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

                # Save to disk (so compute_sha256 works)
                file_path = os.path.join(UPLOAD_DIR, filename)
                try:
                    with open(file_path, "wb") as f:
                        f.write(up.read())
                except Exception:
                    traceback.print_exc()
                    st.error(f"Failed to save uploaded file {filename}. Skipping.")
                    continue

                # compute hash
                try:
                    resume_hash = compute_sha256(file_path)
                except Exception:
                    traceback.print_exc()
                    resume_hash = ""

                # extract text
                try:
                    with open(file_path, "rb") as fh:
                        pdf_bytes = fh.read()
                    resume_text = extract_text_from_pdf_bytes(pdf_bytes)
                except Exception:
                    traceback.print_exc()
                    resume_text = ""

                # run analysis
                try:
                    analysis = analyse_resume_st(resume_text, jd_text, role_choice or None)
                except Exception:
                    traceback.print_exc()
                    # fallback: minimal analysis object
                    analysis = {"overall": 0, "raw_text": resume_text, "jd_hash": ""}

                jd_hash = analysis.get("jd_hash", "")

                # check cache
                cached_score = None
                try:
                    cached_score = get_cached_score(resume_hash, jd_hash, role_choice or "", MODEL_VERSION)
                except Exception:
                    traceback.print_exc()
                    cached_score = None

                if cached_score is not None:
                    analysis["overall"] = cached_score
                    analysis["cached"] = True
                else:
                    try:
                        save_score(resume_hash, jd_hash, role_choice or "", analysis.get("overall", 0),
                                   model_version=MODEL_VERSION, jd_id=jd_id, source_filename=filename)
                        analysis["cached"] = False
                    except Exception:
                        traceback.print_exc()
                        analysis["cached"] = False

                results.append({
                    "filename": filename,
                    "role": role_choice or "",
                    "summary": analysis.get("raw_text", ""),
                    "details": analysis,
                    "overall": analysis.get("overall", 0),
                    "cached": analysis.get("cached", False)
                })

            # Save results to session and display
            st.session_state["last_results"] = results
            if results:
                df = results_to_dataframe(results)
                st.success("Analysis complete")
                st.dataframe(df, use_container_width=True)
            else:
                st.info("No results produced (all files skipped or errors).")

# EXPORT flow
if do_export:
    last = st.session_state.get("last_results", [])
    if not last:
        st.warning("No results to export. Run analysis first.")
    else:
        df = results_to_dataframe(last)
        xlsx_bytes = save_df_to_xlsx_bytes(df)
        ts = int(time.time())
        st.download_button("Download results (.xlsx)", data=xlsx_bytes, file_name=f"results_{ts}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# -------------------------
# Admin: show roles & skills (visual)
# -------------------------
st.markdown("---")
st.header("Roles & Skills (existing)")
try:
    roles = list_roles()
except Exception:
    traceback.print_exc()
    roles = []

if roles:
    for r in roles:
        try:
            buckets = get_role_skills_dict(r)
        except Exception:
            traceback.print_exc()
            buckets = {"core": set(), "tools": set(), "soft": set()}
        with st.expander(r, expanded=False):
            st.write("**Core:**", ", ".join(sorted(buckets.get("core", []))) or "None")
            st.write("**Tools:**", ", ".join(sorted(buckets.get("tools", []))) or "None")
            st.write("**Soft:**", ", ".join(sorted(buckets.get("soft", []))) or "None")
else:
    st.info("No roles found. Add roles using the 'Add Role' admin panel on the left.")

# small footer admin actions
st.markdown("---")
st.caption("To remove skills/roles you'll need to run SQL or add a removal endpoint. I can add this if you'd like.")

# End of file
