# streamlit_app.py (updated - includes JD Manager page)
import os
import time
import json
import unicodedata
import traceback
from io import BytesIO
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta

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

# Template JSON path for role presets
TEMPLATE_PATH = "/mnt/data/templates_all_positions_enhanced.json"

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
# Template JSON helpers
# -------------------------
def load_template_json() -> Dict[str, Any]:
    if os.path.exists(TEMPLATE_PATH):
        try:
            with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            traceback.print_exc()
            return {"template_name":"All_Positions_Screening_Checklist","criteria":[], "skill_bank":[], "roles": [], "metadata": {"version":1}}
    else:
        return {"template_name":"All_Positions_Screening_Checklist","criteria":[], "skill_bank":[], "roles": [], "metadata": {"version":1}}

def save_template_json(tpl: Dict[str, Any]) -> None:
    tpl.setdefault("metadata", {})
    tpl["metadata"]["last_updated"] = datetime.now(timezone(timedelta(hours=5,minutes=30))).isoformat()
    tpl["metadata"]["version"] = tpl["metadata"].get("version", 0) + 1
    with open(TEMPLATE_PATH, "w", encoding="utf-8") as f:
        json.dump(tpl, f, indent=2, ensure_ascii=False)

def generate_role_from_jd(jd: Dict[str, Any]) -> (Dict[str, Any], Dict[str, Any]):
    """
    Build a role preset JSON (role dict) from JD fields.
    Return (template_object, role_object) so template skill_bank can be updated.
    """
    tpl = load_template_json()
    rid = f"R{len(tpl.get('roles', [])) + 1}"
    role_name = jd.get("position_title", "New Role")
    role = {
        "role_id": rid,
        "role_name": role_name,
        "description": jd.get("job_description",""),
        "criteria_presets": {},
        "active": True,
        "version": 1,
        "editable_by_recruiter": True
    }
    # Experience
    exp_min = jd.get("experience_min", None)
    exp_max = jd.get("experience_max", None)
    if exp_min is not None or exp_max is not None:
        role["criteria_presets"]["experience_years"] = {"params": {"min_years": exp_min or 0, "max_years": exp_max or None}, "override_weight": 15, "editable": True}
    # Technical skills
    req_skills = [s.strip() for s in jd.get("technical_required","").split(",") if s.strip()] if jd.get("technical_required") else []
    des_skills = [s.strip() for s in jd.get("technical_desirable","").split(",") if s.strip()] if jd.get("technical_desirable") else []
    if req_skills or des_skills:
        role["criteria_presets"]["technical_skills"] = {"params": {"required_skills": req_skills, "desired_skills": des_skills, "min_occurrences":1}, "override_weight": 25, "editable": True}
        tpl.setdefault("skill_bank", [])
        for s in req_skills + des_skills:
            if s and s.lower() not in [x.lower() for x in tpl["skill_bank"]]:
                tpl["skill_bank"].append(s)
    # Soft skills
    soft_req = [s.strip() for s in jd.get("soft_required","").split(",") if s.strip()] if jd.get("soft_required") else []
    soft_des = [s.strip() for s in jd.get("soft_desirable","").split(",") if s.strip()] if jd.get("soft_desirable") else []
    if soft_req or soft_des:
        role["criteria_presets"]["soft_skills"] = {"params": {"required_skills": soft_req, "desired_skills": soft_des}, "override_weight": 10, "editable": True}
    # Education
    degrees = [s.strip() for s in jd.get("education","").split(",") if s.strip()] if jd.get("education") else []
    if degrees:
        role["criteria_presets"]["degree"] = {"params": {"degrees": degrees, "required": True}, "override_weight": 10, "editable": True}
    # Priority -> weight multiplier heuristic
    priority = jd.get("priority","Medium")
    priority_map = {"Low":0.8, "Medium":1.0, "High":1.2, "Critical":1.4}
    mult = priority_map.get(priority, 1.0)
    for k,v in role["criteria_presets"].items():
        if isinstance(v, dict) and "override_weight" in v:
            v["override_weight"] = float(v["override_weight"]) * mult
    # Semantic matcher from job description
    if jd.get("job_description",""):
        role["criteria_presets"]["semantic_role_match"] = {"params": {"role_description": jd.get("job_description","")}, "override_weight": 20 * mult, "editable": True}
    # JD metadata
    role["_jd_meta"] = {
        "requisition_raised_by": jd.get("requisition_raised_by",""),
        "location": jd.get("location",""),
        "recruitment_type": jd.get("recruitment_type",""),
        "project_name": jd.get("project_name",""),
        "no_of_vacancies": jd.get("no_of_vacancies",""),
        "required_by": jd.get("required_by",""),
        "priority": priority
    }
    return tpl, role

# -------------------------
# UI Layout - Page selector
# -------------------------
PAGES = ["Analyse", "JD Manager", "Admin"]
page = st.sidebar.selectbox("Page", PAGES)

# -------------------------
# COMMON left column controls used in Analyse page
# -------------------------
def analyse_left_controls():
    col = st.sidebar
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

    selected_jd_label = col.selectbox("Choose saved JD (optional)", jd_options)
    try:
        role_list = list_roles()
    except Exception:
        traceback.print_exc()
        role_list = []
    role_choice = col.selectbox("Role (optional)", options=[""] + role_list)

    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "10"))
    col.write(f"Max upload per file: **{max_upload_mb} MB**")

    return selected_jd_label, jd_map, role_choice, max_upload_mb

# -------------------------
# PAGE: JD Manager
# -------------------------
if page == "JD Manager":
    st.title("JD Input → Generate Role Preset")
    st.markdown("Fill the JD form below and generate a role preset. You can edit generated presets before saving.")
    with st.form("jd_form"):
        st.header("Requisition Details")
        requisition_raised_by = st.text_input("Requisition Raised By")
        location = st.text_input("Location")
        recruitment_type = st.selectbox("Recruitment Type", ["New/Replacement","New","Replacement"])
        project_name = st.text_input("Project Name")
        no_of_vacancies = st.number_input("No. of Vacancies", min_value=1, value=1)
        date_of_request = st.date_input("Date of Request")
        required_by = st.date_input("Required By")
        priority = st.selectbox("Priority", ["Low","Medium","High","Critical"])
        st.markdown("---")
        st.header("Position & Qualification Details")
        position_title = st.text_input("Position Title", value="Data Analyst")
        educational_qualification = st.text_input("Educational Qualification (comma-separated)")
        professional_cert = st.text_input("Professional Certification (if any, comma-separated)")
        salary_target = st.text_input("Salary Target (Min - Max)")
        exp_range = st.text_input("Experience (Years, Min - Max) e.g. 2-4")
        exp_min, exp_max = None, None
        if exp_range:
            try:
                parts = [p.strip() for p in exp_range.split("-")]
                if len(parts) >= 1 and parts[0]: exp_min = float(parts[0])
                if len(parts) >= 2 and parts[1]: exp_max = float(parts[1])
            except:
                pass
        st.markdown("---")
        st.header("Technical Skills")
        technical_required = st.text_input("Required (comma-separated)", value="Python, SQL, Excel, Tableau")
        technical_desirable = st.text_input("Desirable (comma-separated)", value="PowerBI, R")
        st.markdown("---")
        st.header("Soft Skills / Competencies")
        soft_required = st.text_input("Required (comma-separated)", value="Communication, Problem Solving")
        soft_desirable = st.text_input("Desirable (comma-separated)", value="Teamwork, Time Management")
        st.markdown("---")
        st.header("Job Description / Key Responsibilities")
        job_description = st.text_area("Job Description / Key Responsibilities", height=200, value="Analyze datasets, build dashboards, automate reporting and create insights for stakeholders.")
        essential_skills = st.text_input("Essential Skills (comma-separated)")
        desired_skills = st.text_input("Desired Skills (comma-separated)")
        domain_knowledge = st.text_input("Domain Knowledge (comma-separated)")
        st.markdown("---")
        st.header("Additional information")
        travel_requirement = st.text_input("Travel Requirement / client visit etc.")
        submitted = st.form_submit_button("Generate Role Preset")

    if submitted:
        jd = {
            "requisition_raised_by": requisition_raised_by,
            "location": location,
            "recruitment_type": recruitment_type,
            "project_name": project_name,
            "no_of_vacancies": no_of_vacancies,
            "date_of_request": date_of_request.isoformat(),
            "required_by": required_by.isoformat(),
            "priority": priority,
            "position_title": position_title,
            "education": educational_qualification,
            "professional_cert": professional_cert,
            "salary_target": salary_target,
            "experience_min": exp_min,
            "experience_max": exp_max,
            "technical_required": technical_required,
            "technical_desirable": technical_desirable,
            "soft_required": soft_required,
            "soft_desirable": soft_desirable,
            "job_description": job_description,
            "essential_skills": essential_skills,
            "desired_skills": desired_skills,
            "domain_knowledge": domain_knowledge,
            "travel_requirement": travel_requirement
        }
        tpl, role = generate_role_from_jd(jd)
        st.success(f"Generated role preset: {role['role_name']} (id: {role['role_id']})")
        st.subheader("Preview: Role Preset (editable)")
        st.json(role)
        st.markdown("You can edit the preset below before saving.")
        # Simple editor for weights and required skills
        if "technical_skills" in role["criteria_presets"]:
            st.subheader("Technical Skills Preset")
            techp = role["criteria_presets"]["technical_skills"]
            req = techp["params"].get("required_skills", [])
            new_req = st.text_input("Required skills (comma-separated)", value=", ".join(req), key="req_skills")
            techp["params"]["required_skills"] = [s.strip() for s in new_req.split(",") if s.strip()]
            w = st.number_input("Weight for technical_skills", value=float(techp.get("override_weight",25.0)), key="w_tech")
            techp["override_weight"] = float(w)
        # Experience editor
        if "experience_years" in role["criteria_presets"]:
            st.subheader("Experience Preset")
            expp = role["criteria_presets"]["experience_years"]
            miny = st.number_input("Min years", value=float(expp["params"].get("min_years",0)), key="min_y")
            maxy_val = expp["params"].get("max_years", None)
            maxy = st.text_input("Max years (leave blank for no max)", value=str(maxy_val) if maxy_val else "", key="max_y")
            expp["params"]["min_years"] = float(miny)
            expp["params"]["max_years"] = float(maxy) if maxy else None
            w = st.number_input("Weight for experience_years", value=float(expp.get("override_weight",15.0)), key="w_exp")
            expp["override_weight"] = float(w)
        # Semantic matcher editor
        if "semantic_role_match" in role["criteria_presets"]:
            st.subheader("Semantic Role Matcher")
            sem = role["criteria_presets"]["semantic_role_match"]
            rd = st.text_area("Role description for semantic matching", value=sem["params"].get("role_description",""), key="rd")
            sem["params"]["role_description"] = rd
            w = st.number_input("Weight for semantic_role_match", value=float(sem.get("override_weight",20.0)), key="w_sem")
            sem["override_weight"] = float(w)

        if st.button("Save role preset (to JSON + DB)"):
            # append role to template JSON and save
            try:
                tpl.setdefault("roles", []).append(role)
                save_template_json(tpl)
                st.success(f"Saved role preset {role['role_name']} to template store: {TEMPLATE_PATH}")
            except Exception:
                traceback.print_exc()
                st.error("Failed to save role preset JSON (check server permissions).")
            # Also attempt to add role + skills to DB so they appear in admin lists
            try:
                upsert_role(role["role_name"])
                # add skills to DB for this role
                cps = role.get("criteria_presets", {})
                tech = cps.get("technical_skills", {}).get("params", {}).get("required_skills", [])
                for s in tech:
                    if s:
                        upsert_skill(role["role_name"], s, "tools")
                softs = cps.get("soft_skills", {}).get("params", {}).get("required_skills", [])
                for s in softs:
                    if s:
                        upsert_skill(role["role_name"], s, "soft")
                st.success("Role and skills synced to DB (upsert).")
            except Exception:
                traceback.print_exc()
                st.warning("Saved JSON but failed to upsert role/skills to DB. Check logs.")

            # Optionally save JD as job_description entry in DB
            try:
                add_job_description(role.get("role_name"), job_description)
            except Exception:
                # not critical
                pass

# -------------------------
# PAGE: Analyse (original upload & scoring)
# -------------------------
elif page == "Analyse":
    st.title("Resume Analyzer — Role Aware")
    # Left controls (use same layout but embedded in page)
    col_left, col_right = st.columns([1, 3])
    with col_left:
        st.header("Controls")
        # Load saved JDs for picker
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
    # Main actions (Analyse flow)
    # -------------------------
    if "last_results" not in st.session_state:
        st.session_state["last_results"] = []

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

    # Run analysis
    if 'do_analyze' in locals() and do_analyze:
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
    if 'do_export' in locals() and do_export:
        last = st.session_state.get("last_results", [])
        if not last:
            st.warning("No results to export. Run analysis first.")
        else:
            df = results_to_dataframe(last)
            xlsx_bytes = save_df_to_xlsx_bytes(df)
            ts = int(time.time())
            st.download_button("Download results (.xlsx)", data=xlsx_bytes, file_name=f"results_{ts}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # Roles visual (kept from previous)
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

# -------------------------
# PAGE: Admin (extra admin actions)
# -------------------------
elif page == "Admin":
    st.title("Admin Console")
    st.markdown("Administrative actions: list JDs, cached candidates, delete cache, inspect templates.")
    # list saved JDs
    if st.button("List saved JDs"):
        try:
            jds = list_job_descriptions()
            st.write(jds)
        except Exception:
            traceback.print_exc()
            st.error("Failed to list JDs (check logs).")
    if st.button("Show template JSON (server)"):
        try:
            tpl = load_template_json()
            st.json(tpl)
        except Exception:
            traceback.print_exc()
            st.error("Failed to load template JSON.")
    if st.button("List cached candidates"):
        try:
            cands = list_cached_candidates()
            st.write(cands)
        except Exception:
            traceback.print_exc()
            st.error("Failed to list cached candidates.")

    st.markdown("---")
    st.header("Delete cached results")
    del_resume_hash = st.text_input("Resume hash (leave blank to skip)", key="admin_del_resume")
    del_jd_hash = st.text_input("JD hash (optional)", key="admin_del_jdhash")
    del_role = st.text_input("Role (optional)", key="admin_del_role")
    if st.button("Delete cached (admin)"):
        if not del_resume_hash:
            st.warning("Provide resume_hash to delete (or use admin API externally).")
        else:
            try:
                delete_cached(del_resume_hash, del_jd_hash or None, del_role or None)
                st.success("Deleted cached rows (if existed).")
            except Exception:
                traceback.print_exc()
                st.error("Failed to delete cached rows (check logs).")

# footer note
st.markdown("---")
st.caption("Use the JD Manager to create role presets (JSON + DB). Use Analyse page to upload resumes and apply presets.")
