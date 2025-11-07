# analyse_pdf.py
import re
import hashlib
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
from db_mysql import get_role_skills_dict

MODEL_NAME = "all-MiniLM-L6-v2"
MODEL_VERSION = "st-role-aware-v1"

# load model once
model = SentenceTransformer(MODEL_NAME)

STOPWORDS = {
    "the","and","with","for","that","this","have","has","are","is","in","on","of","to","by","as","be","will","a","an","role"
}

def clean_text(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or "")).strip()

def jd_hash_func(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

def extract_tokens(text: str):
    tokens = [w.lower().strip(".,()[]{}:;\"'") for w in text.split()]
    tokens = [t for t in tokens if t and len(t) > 1 and t not in STOPWORDS]
    return tokens

def extract_skills_from_text(text: str):
    tokens = extract_tokens(text)
    return set(tokens)

def analyse_resume_st(resume_content: str, job_description: str, role: str = None):
    resume_text = clean_text(resume_content)
    jd_text = clean_text(job_description or "")

    role_key = role or "Software Developer (Generic)"
    role_skills = get_role_skills_dict(role_key)
    core_required = set([s.lower() for s in role_skills.get("core", [])])
    tools_required = set([s.lower() for s in role_skills.get("tools", [])])
    soft_required = set([s.lower() for s in role_skills.get("soft", [])])

    resume_tokens = extract_skills_from_text(resume_text)
    jd_tokens = extract_skills_from_text(jd_text)

    emb = model.encode([resume_text, jd_text])
    sim = 0.0
    try:
        sim = float(cosine_similarity([emb[0]],[emb[1]])[0][0])
    except Exception:
        sim = 0.0
    experience_score = round(sim * 100.0, 2)

    jd_core_overlap = core_required & jd_tokens
    jd_tools_overlap = tools_required & jd_tokens

    tech_required = jd_core_overlap if jd_core_overlap else core_required
    tools_required_final = jd_tools_overlap if jd_tools_overlap else tools_required

    def compute_presence_score(required_set, resume_tokens_set):
        if not required_set:
            return 0.0
        present = len(required_set & resume_tokens_set)
        return round((present / len(required_set)) * 100.0, 2)

    technical_score = compute_presence_score(tech_required, resume_tokens)
    tools_score = compute_presence_score(tools_required_final, resume_tokens)
    soft_score = compute_presence_score(soft_required, resume_tokens)

    overall = round(
        (technical_score * 0.45) +
        (experience_score * 0.30) +
        (tools_score * 0.15) +
        (soft_score * 0.10),
        2
    )

    matching_core = sorted(list(tech_required & resume_tokens))
    matching_tools = sorted(list(tools_required_final & resume_tokens))
    matching_soft = sorted(list(soft_required & resume_tokens))

    missing_core = sorted(list(tech_required - resume_tokens))
    missing_tools = sorted(list(tools_required_final - resume_tokens))
    missing_soft = sorted(list(soft_required - resume_tokens))

    raw_text_lines = [
        "✅ Resume Fit Report",
        f"📌 Role: {role_key}",
        f"📌 Overall Fit Score: {overall}/100",
        "",
        "🧠 Category Scores:",
        f"- Technical Skills: {technical_score}/100",
        f"- Experience Relevance: {experience_score}/100",
        f"- Tools & Platforms: {tools_score}/100",
        f"- Soft Skills: {soft_score}/100",
        "",
        "🔥 Strong Matches (Core skills):",
        f"- {', '.join(matching_core) if matching_core else 'None detected'}",
        "",
        "🔧 Tools / Platforms Matched:",
        f"- {', '.join(matching_tools) if matching_tools else 'None detected'}",
        "",
        "✨ Soft/Domain Matches:",
        f"- {', '.join(matching_soft) if matching_soft else 'None detected'}",
        "",
        "⚠️ Missing (priority) Core Skills:",
        f"- {', '.join(missing_core) if missing_core else 'None'}",
        "",
        "⚠️ Missing Tools:",
        f"- {', '.join(missing_tools) if missing_tools else 'None'}",
        "",
        "🔍 Method:",
        "This evaluation combines: role-aware required-skills matching, and semantic experience relevance using Transformer embeddings."
    ]
    raw_text = "\n".join(raw_text_lines)

    return {
        "raw_text": raw_text,
        "overall": overall,
        "technical": technical_score,
        "experience": experience_score,
        "tools": tools_score,
        "soft": soft_score,
        "matching_core": matching_core,
        "matching_tools": matching_tools,
        "matching_soft": matching_soft,
        "missing_core": missing_core,
        "missing_tools": missing_tools,
        "missing_soft": missing_soft,
        "model_version": MODEL_VERSION,
        "jd_hash": jd_hash_func(jd_text)
    }
