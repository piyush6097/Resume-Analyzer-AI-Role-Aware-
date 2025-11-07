# app.py
from flask import Flask, request, render_template, jsonify, redirect, url_for, send_file
import os, time, json, unicodedata
import fitz  # PyMuPDF
from analyse_pdf import analyse_resume_st, MODEL_VERSION
from hash_resume import compute_sha256
from db_mysql import (
    init_db, get_cached_score, save_score,
    add_job_description, list_job_descriptions,
    list_cached_candidates, delete_cached, get_job_description,
    list_roles
)
import pandas as pd
from io import BytesIO

app = Flask(__name__)

# Absolute paths (avoid working-dir mismatch issues)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, "uploads")
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize DB (creates tables if missing)
init_db()

@app.route("/health")
def health():
    return "OK", 200

def extract_text_from_resume(pdf_path):
    try:
        doc = fitz.open(pdf_path)
        text = " ".join([page.get_text() for page in doc])
        doc.close()
        return text
    except Exception as e:
        print("❌ PDF read error:", e)
        return ""

@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    jds = list_job_descriptions()
    roles = list_roles()

    if request.method == "POST":
        resumes = request.files.getlist("resumes")
        job_description = request.form.get("job_description", "").strip()
        selected_jd_id = request.form.get("jd_id")
        role = request.form.get("role")

        if not job_description and not selected_jd_id:
            return render_template("index.html", jds=jds, roles=roles, results=[],
                                   error="Job Description is required.")

        if selected_jd_id and not job_description:
            jd_row = get_job_description(int(selected_jd_id))
            jd_text = jd_row[2] if jd_row else ""
            jd_id_int = int(selected_jd_id)
        else:
            jd_text = job_description
            jd_id_int = None

        for resume_file in resumes:
            filename = resume_file.filename
            if not filename or not filename.lower().endswith(".pdf"):
                results.append({
                    "filename": filename or "unknown",
                    "overall": 0,
                    "cached": False,
                    "summary": "Invalid file: Only PDF allowed",
                    "role": role or ""
                })
                continue

            pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            resume_file.save(pdf_path)

            resume_hash = compute_sha256(pdf_path)
            resume_content = extract_text_from_resume(pdf_path)

            analysis = analyse_resume_st(resume_content, jd_text, role or None)
            jd_hash = analysis.get("jd_hash", "")

            cached_score = get_cached_score(
                resume_hash, jd_hash, role or "", MODEL_VERSION
            )

            if cached_score is not None:
                # Use cached value
                analysis["overall"] = cached_score
                analysis["cached"] = True
            else:
                save_score(
                    resume_hash, jd_hash, role or "",
                    analysis.get("overall", 0),
                    model_version=MODEL_VERSION,
                    jd_id=jd_id_int, source_filename=filename
                )
                analysis["cached"] = False

            results.append({
                "filename": filename,
                "role": role or "",
                "summary": analysis.get("raw_text", ""),
                "details": analysis,
                "overall": analysis.get("overall", 0),
                "cached": analysis.get("cached", False)
            })

    return render_template("index.html", jds=jds, roles=roles, results=results, error=None)

# sanitize helpers
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

@app.route("/export_results", methods=["POST"])
def export_results():
    try:
        payload = request.get_json() if request.is_json else json.loads(request.form.get("results", "[]"))
    except Exception as e:
        print("❌ JSON Decode Error:", e)
        return "Invalid results data", 400

    if not payload:
        return "No results provided", 400

    # Build tabular rows
    rows = []
    for r in payload:
        d = r.get("details", {}) if isinstance(r, dict) else {}
        overall = d.get("overall", r.get("overall", ""))
        technical = d.get("technical", r.get("technical", ""))
        experience = d.get("experience", r.get("experience", ""))
        tools = d.get("tools", r.get("tools", ""))
        soft = d.get("soft", r.get("soft", ""))
        matching_core = d.get("matching_core", r.get("matching_core", ""))
        matching_tools = d.get("matching_tools", r.get("matching_tools", ""))
        matching_soft = d.get("matching_soft", r.get("matching_soft", ""))
        missing_core = d.get("missing_core", r.get("missing_core", ""))
        missing_tools = d.get("missing_tools", r.get("missing_tools", ""))
        missing_soft = d.get("missing_soft", r.get("missing_soft", ""))
        raw_summary = d.get("raw_text", None) if d else None
        if not raw_summary:
            raw_summary = r.get("summary", "")

        rows.append({
            "filename": _safe_text(r.get("filename","")),
            "role": _safe_text(r.get("role","")),
            "overall": overall,
            "technical": technical,
            "experience": experience,
            "tools": tools,
            "soft": soft,
            "matching_core": _safe_join(matching_core),
            "matching_tools": _safe_join(matching_tools),
            "matching_soft": _safe_join(matching_soft),
            "missing_core": _safe_join(missing_core),
            "missing_tools": _safe_join(missing_tools),
            "missing_soft": _safe_join(missing_soft),
            "summary": _safe_text(raw_summary),
            "cached": r.get("cached", False)
        })

    df = pd.DataFrame(rows)
    # Create in-memory xlsx
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="results")
    output.seek(0)
    ts = int(time.time())
    filename = f"results_{ts}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# Admin APIs
@app.route("/admin/jd/add", methods=["POST"])
def admin_add_jd():
    name = request.form.get("name")
    desc = request.form.get("description")
    if not name or not desc:
        return "Name and description required", 400
    add_job_description(name, desc)
    return redirect(url_for("index"))

@app.route("/admin/jd/list", methods=["GET"])
def admin_list_jd():
    return jsonify(list_job_descriptions())

@app.route("/admin/candidates", methods=["GET"])
def admin_list_candidates():
    return jsonify(list_cached_candidates())

@app.route("/admin/candidate/delete", methods=["POST"])
def admin_delete_candidate():
    resume_hash = request.form.get("resume_hash")
    jd_hash = request.form.get("jd_hash")
    role = request.form.get("role")
    delete_cached(resume_hash, jd_hash, role)
    return "deleted", 200

if __name__ == "__main__":
    print("Starting app on http://0.0.0.0:10000")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)
