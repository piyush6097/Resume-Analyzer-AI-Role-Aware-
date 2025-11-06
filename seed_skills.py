
from db_mysql import init_db, seed_skills_from_dict
from skills import SKILL_DB  # your old dict

if __name__ == "__main__":
    init_db()
    seed_skills_from_dict(SKILL_DB)
    print("✅ Seeded roles & skills into MySQL.")
