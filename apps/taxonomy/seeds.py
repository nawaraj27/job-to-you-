"""
Curated seed data.

This is not the full taxonomy. ESCO, O*NET and any national classification are
the real sources and arrive through `import_taxonomy` (ESCO and O*NET are both
CC-BY and require attribution, which is why every imported skill keeps its
source id in `SkillExternalId`). What lives here is a working core, chosen to
cover the domains the product already talks about and — deliberately — to
include every case that breaks a naive implementation:

  * aliases that must beat fuzzy matching: JS, ECMAScript, K8s, Postgres
  * near-neighbours that must NOT collapse: Java vs JavaScript, C vs C# vs C++
  * implications the matcher spends: Django implies Python, React implies
    JavaScript, Kubernetes implies Docker
  * a genuinely deprecated skill with a successor: AngularJS, which was never
    Angular and must not be an alias of it
  * non-English display names, so nothing in the stack can quietly assume the
    canonical English label is the only one

Weights are judgements, not measurements, and are meant to be revised once
P16 can correlate them against hiring outcomes. The same goes for the
translations: they are a starting point for a curator, not a certified
localisation.
"""

from __future__ import annotations

#: (slug, name, parent_slug)
SKILL_CATEGORIES: list[tuple[str, str, str | None]] = [
    ("engineering", "Engineering", None),
    ("languages-programming", "Programming languages", "engineering"),
    ("frameworks", "Frameworks and libraries", "engineering"),
    ("data", "Data and databases", "engineering"),
    ("infrastructure", "Infrastructure and operations", "engineering"),
    ("quality", "Testing and quality", "engineering"),
    ("design", "Design", None),
    ("product", "Product and delivery", None),
    ("business", "Business", None),
    ("finance", "Finance and accounting", "business"),
    ("marketing", "Marketing and growth", "business"),
    ("people", "People and hiring", "business"),
    ("healthcare", "Healthcare", None),
    ("education", "Education", None),
    ("operations", "Operations and logistics", None),
    ("soft", "Ways of working", None),
]

#: category slug -> default kind. A default rather than a per-skill value on
#: every row: 108 hand-written kinds would be 108 chances to disagree with the
#: category, and the overrides below carry the cases that genuinely differ.
CATEGORY_KINDS: dict[str, str] = {
    "engineering": "hard_skill",
    "languages-programming": "programming_language",
    "frameworks": "framework",
    "data": "technology",
    "infrastructure": "platform",
    "quality": "methodology",
    "design": "hard_skill",
    "product": "methodology",
    "business": "domain_knowledge",
    "finance": "domain_knowledge",
    "marketing": "domain_knowledge",
    "people": "domain_knowledge",
    "healthcare": "domain_knowledge",
    "education": "domain_knowledge",
    "operations": "domain_knowledge",
    "soft": "soft_skill",
}

#: slug -> kind, where the category's default is wrong. Docker is a tool, not a
#: platform; pandas is a library, not a technology; SQL is a language.
SKILL_KINDS: dict[str, str] = {
    "sql": "programming_language",
    "bash": "programming_language",
    "pandas": "library",
    "celery": "library",
    "htmx": "library",
    "alpine-js": "library",
    "tailwind-css": "library",
    "rest-apis": "methodology",
    "graphql": "technology",
    "docker": "tool",
    "kubernetes": "tool",
    "terraform": "tool",
    "nginx": "tool",
    "git": "tool",
    "linux": "platform",
    "aws": "platform",
    "azure": "platform",
    "gcp": "platform",
    "postgresql": "technology",
    "mysql": "technology",
    "mongodb": "technology",
    "redis": "technology",
    "elasticsearch": "technology",
    "power-bi": "tool",
    "tableau": "tool",
    "figma": "tool",
    "pytest": "tool",
    "playwright": "tool",
    "selenium": "tool",
    "ci-cd": "methodology",
    "agile": "methodology",
    "monitoring": "methodology",
    "data-modelling": "hard_skill",
    "system-design": "hard_skill",
    "machine-learning": "hard_skill",
    "accessibility": "methodology",
    "flash": "technology",
    "angularjs": "framework",
    "dotnet": "framework",
    "spring": "framework",
}

#: (slug, name, category_slug, [aliases])
SKILLS: list[tuple[str, str, str, list[str]]] = [
    # --- programming languages -------------------------------------------
    ("python", "Python", "languages-programming", ["py", "python3"]),
    ("javascript", "JavaScript", "languages-programming", ["js", "ecmascript", "es6"]),
    ("typescript", "TypeScript", "languages-programming", ["ts"]),
    ("java", "Java", "languages-programming", ["java se", "core java"]),
    ("c-sharp", "C#", "languages-programming", ["csharp", "c sharp", "dotnet language"]),
    ("c-plus-plus", "C++", "languages-programming", ["cpp", "c plus plus"]),
    ("c", "C", "languages-programming", ["ansi c"]),
    ("go", "Go", "languages-programming", ["golang"]),
    ("rust", "Rust", "languages-programming", []),
    ("php", "PHP", "languages-programming", []),
    ("ruby", "Ruby", "languages-programming", []),
    ("kotlin", "Kotlin", "languages-programming", []),
    ("swift", "Swift", "languages-programming", []),
    ("sql", "SQL", "languages-programming", ["structured query language"]),
    ("bash", "Bash", "languages-programming", ["shell scripting", "shell"]),
    # --- frameworks -------------------------------------------------------
    ("django", "Django", "frameworks", ["django framework"]),
    ("flask", "Flask", "frameworks", []),
    ("fastapi", "FastAPI", "frameworks", []),
    ("react", "React", "frameworks", ["reactjs", "react.js"]),
    ("vue", "Vue", "frameworks", ["vuejs", "vue.js"]),
    ("angular", "Angular", "frameworks", []),
    ("htmx", "HTMX", "frameworks", []),
    ("alpine-js", "Alpine.js", "frameworks", ["alpinejs"]),
    ("tailwind-css", "Tailwind CSS", "frameworks", ["tailwind"]),
    ("spring", "Spring", "frameworks", ["spring boot"]),
    ("laravel", "Laravel", "frameworks", []),
    ("dotnet", ".NET", "frameworks", ["net core", "asp net"]),
    ("celery", "Celery", "frameworks", []),
    ("rest-apis", "REST APIs", "frameworks", ["rest", "restful apis"]),
    ("graphql", "GraphQL", "frameworks", []),
    # --- data -------------------------------------------------------------
    ("postgresql", "PostgreSQL", "data", ["postgres", "psql"]),
    ("mysql", "MySQL", "data", ["mariadb"]),
    ("mongodb", "MongoDB", "data", ["mongo"]),
    ("redis", "Redis", "data", []),
    ("elasticsearch", "Elasticsearch", "data", ["opensearch"]),
    ("data-modelling", "Data modelling", "data", ["schema design", "database design"]),
    ("etl", "ETL", "data", ["data pipelines"]),
    ("pandas", "pandas", "data", []),
    ("power-bi", "Power BI", "data", ["powerbi"]),
    ("tableau", "Tableau", "data", []),
    ("data-analysis", "Data analysis", "data", ["analytics"]),
    ("machine-learning", "Machine learning", "data", ["ml"]),
    # --- infrastructure ---------------------------------------------------
    ("docker", "Docker", "infrastructure", ["containers"]),
    ("kubernetes", "Kubernetes", "infrastructure", ["k8s"]),
    ("aws", "AWS", "infrastructure", ["amazon web services"]),
    ("azure", "Azure", "infrastructure", ["microsoft azure"]),
    ("gcp", "Google Cloud", "infrastructure", ["gcp", "google cloud platform"]),
    ("linux", "Linux", "infrastructure", ["unix"]),
    ("ci-cd", "CI/CD", "infrastructure", ["continuous integration", "continuous delivery"]),
    ("terraform", "Terraform", "infrastructure", ["infrastructure as code"]),
    ("nginx", "nginx", "infrastructure", []),
    ("git", "Git", "infrastructure", ["version control"]),
    ("monitoring", "Monitoring and observability", "infrastructure", ["observability"]),
    # --- quality ----------------------------------------------------------
    ("pytest", "pytest", "quality", []),
    ("test-automation", "Test automation", "quality", ["automated testing"]),
    ("playwright", "Playwright", "quality", []),
    ("selenium", "Selenium", "quality", []),
    ("test-planning", "Test planning", "quality", ["test strategy"]),
    ("manual-testing", "Manual testing", "quality", []),
    # --- design -----------------------------------------------------------
    ("figma", "Figma", "design", []),
    ("ui-design", "UI design", "design", ["interface design"]),
    ("ux-research", "UX research", "design", ["user research"]),
    ("accessibility", "Accessibility", "design", ["a11y", "wcag"]),
    ("design-systems", "Design systems", "design", []),
    ("prototyping", "Prototyping", "design", []),
    # --- product ----------------------------------------------------------
    ("product-management", "Product management", "product", ["product ownership"]),
    ("agile", "Agile", "product", ["scrum", "kanban"]),
    ("roadmapping", "Roadmapping", "product", []),
    ("stakeholder-management", "Stakeholder management", "product", []),
    ("requirements-gathering", "Requirements gathering", "product", []),
    # --- business ---------------------------------------------------------
    ("accounting", "Accounting", "finance", []),
    ("financial-reporting", "Financial reporting", "finance", []),
    ("audit", "Audit", "finance", ["auditing"]),
    ("taxation", "Taxation", "finance", ["tax"]),
    ("payroll", "Payroll", "finance", []),
    ("bookkeeping", "Bookkeeping", "finance", []),
    ("seo", "SEO", "marketing", ["search engine optimisation"]),
    ("content-marketing", "Content marketing", "marketing", []),
    ("social-media", "Social media marketing", "marketing", []),
    ("email-marketing", "Email marketing", "marketing", []),
    ("brand-strategy", "Brand strategy", "marketing", ["branding"]),
    ("recruiting", "Recruiting", "people", ["talent acquisition", "hiring"]),
    ("interviewing", "Interviewing", "people", []),
    ("onboarding", "Onboarding", "people", []),
    ("performance-management", "Performance management", "people", []),
    # --- healthcare -------------------------------------------------------
    ("patient-care", "Patient care", "healthcare", ["nursing care"]),
    ("clinical-research", "Clinical research", "healthcare", []),
    ("medical-records", "Medical records", "healthcare", ["emr", "ehr"]),
    ("phlebotomy", "Phlebotomy", "healthcare", []),
    ("pharmacology", "Pharmacology", "healthcare", []),
    # --- education --------------------------------------------------------
    ("curriculum-design", "Curriculum design", "education", []),
    ("classroom-management", "Classroom management", "education", []),
    ("lesson-planning", "Lesson planning", "education", []),
    ("tutoring", "Tutoring", "education", []),
    # --- operations -------------------------------------------------------
    ("supply-chain", "Supply chain", "operations", ["logistics"]),
    ("inventory-management", "Inventory management", "operations", ["stock control"]),
    ("procurement", "Procurement", "operations", ["purchasing"]),
    ("warehouse-operations", "Warehouse operations", "operations", []),
    ("quality-control", "Quality control", "operations", ["qc"]),
    # --- ways of working --------------------------------------------------
    ("communication", "Communication", "soft", ["written communication"]),
    ("mentoring", "Mentoring", "soft", ["coaching"]),
    ("problem-solving", "Problem solving", "soft", []),
    ("teamwork", "Teamwork", "soft", ["collaboration"]),
    ("time-management", "Time management", "soft", []),
    ("customer-service", "Customer service", "soft", ["client service"]),
    ("system-design", "System design", "engineering", ["architecture"]),
    ("code-review", "Code review", "engineering", []),
    ("technical-writing", "Technical writing", "engineering", ["documentation"]),
]

#: (from_slug, to_slug, kind, weight)
#:
#: `implies` is the edge the matcher spends: a candidate listing Django gets
#: partial credit for a Python requirement rather than a zero. Weights are
#: judgements — Django is unusable without Python, so 0.85; Kubernetes usually
#: but not always accompanies Docker, so 0.7.
SKILL_RELATIONS: list[tuple[str, str, str, str]] = [
    ("django", "python", "implies", "0.85"),
    ("flask", "python", "implies", "0.85"),
    ("fastapi", "python", "implies", "0.85"),
    ("pandas", "python", "implies", "0.80"),
    ("pytest", "python", "implies", "0.75"),
    ("celery", "python", "implies", "0.70"),
    ("react", "javascript", "implies", "0.85"),
    ("vue", "javascript", "implies", "0.85"),
    ("angular", "javascript", "implies", "0.80"),
    ("typescript", "javascript", "implies", "0.90"),
    ("htmx", "javascript", "implies", "0.40"),
    ("alpine-js", "javascript", "implies", "0.60"),
    ("spring", "java", "implies", "0.85"),
    ("kotlin", "java", "implies", "0.55"),
    ("laravel", "php", "implies", "0.85"),
    ("dotnet", "c-sharp", "implies", "0.85"),
    ("kubernetes", "docker", "implies", "0.70"),
    ("terraform", "ci-cd", "implies", "0.45"),
    ("postgresql", "sql", "implies", "0.85"),
    ("mysql", "sql", "implies", "0.85"),
    ("etl", "sql", "implies", "0.60"),
    ("data-modelling", "sql", "implies", "0.55"),
    ("playwright", "test-automation", "implies", "0.80"),
    ("selenium", "test-automation", "implies", "0.80"),
    ("machine-learning", "data-analysis", "implies", "0.60"),
    ("design-systems", "ui-design", "implies", "0.70"),
    ("audit", "accounting", "implies", "0.70"),
    ("financial-reporting", "accounting", "implies", "0.75"),
    ("bookkeeping", "accounting", "implies", "0.65"),
    ("interviewing", "recruiting", "implies", "0.60"),
    # Related, not implied: knowing one says nothing about the other, but they
    # co-occur often enough to be worth suggesting.
    ("react", "typescript", "related", "0.50"),
    ("docker", "linux", "related", "0.50"),
    ("aws", "terraform", "related", "0.40"),
    ("figma", "prototyping", "related", "0.60"),
    ("agile", "product-management", "related", "0.50"),
]

#: (child_slug, parent_slug) — genuine hierarchy, distinct from implication.
#:
#: "Broader" answers "what family is this in", which is how a curator browses and
#: how a job asking for the parent can find candidates listing a child. Some
#: pairs carry both a hierarchy edge and an implication edge, and they mean
#: different things: PostgreSQL *is a kind of* SQL skill (broader) and *partly
#: satisfies* a SQL requirement (implies, 0.85).
SKILL_HIERARCHY: list[tuple[str, str]] = [
    ("postgresql", "sql"),
    ("mysql", "sql"),
    ("typescript", "javascript"),
    ("pytest", "test-automation"),
    ("playwright", "test-automation"),
    ("selenium", "test-automation"),
    ("design-systems", "ui-design"),
    ("financial-reporting", "accounting"),
    ("bookkeeping", "accounting"),
    ("audit", "accounting"),
    ("taxation", "accounting"),
    ("payroll", "accounting"),
    ("interviewing", "recruiting"),
    ("machine-learning", "data-analysis"),
]

#: (slug, name, category_slug, replacement_slug, reason)
#:
#: Deprecation is not merging. AngularJS and Angular are different technologies
#: with different APIs; someone who lists AngularJS did not list Angular, and
#: flattening the two with an alias would rewrite what their profile claims.
#: Deprecation keeps the distinction and forwards resolution to the successor.
DEPRECATED_SKILLS: list[tuple[str, str, str, str | None, str]] = [
    (
        "angularjs",
        "AngularJS",
        "frameworks",
        "angular",
        "Superseded by Angular (2+); AngularJS reached end of life in 2022.",
    ),
    (
        "flash",
        "Adobe Flash",
        "design",
        None,
        "Discontinued in 2020 with no successor skill.",
    ),
]

#: (skill_slug, locale, name)
#:
#: Display names only — an alias is what resolution *accepts*, a translation is
#: what the interface *shows*. Nepali first, because the product launches into a
#: market where the job title is English and the conversation is not.
SKILL_TRANSLATIONS: list[tuple[str, str, str]] = [
    ("accounting", "ne", "लेखा"),
    ("communication", "ne", "सञ्चार"),
    ("teamwork", "ne", "टोली कार्य"),
    ("customer-service", "ne", "ग्राहक सेवा"),
    ("patient-care", "ne", "बिरामी हेरचाह"),
    ("time-management", "ne", "समय व्यवस्थापन"),
    ("problem-solving", "ne", "समस्या समाधान"),
    ("mentoring", "ne", "मार्गदर्शन"),
    ("tutoring", "ne", "ट्युसन"),
    ("taxation", "ne", "कर"),
    ("payroll", "ne", "तलब व्यवस्थापन"),
    ("procurement", "ne", "खरिद"),
    ("inventory-management", "ne", "मौज्दात व्यवस्थापन"),
    ("recruiting", "ne", "कर्मचारी भर्ना"),
    ("accounting", "hi", "लेखांकन"),
    ("communication", "hi", "संचार"),
    ("teamwork", "hi", "टीम वर्क"),
]

#: (slug, name, parent_slug)
JOB_CATEGORIES: list[tuple[str, str, str | None]] = [
    ("technology", "Technology", None),
    ("software-engineering", "Software engineering", "technology"),
    ("data-analytics", "Data and analytics", "technology"),
    ("infrastructure-devops", "Infrastructure and DevOps", "technology"),
    ("quality-assurance", "Quality assurance", "technology"),
    ("design-creative", "Design and creative", None),
    ("product-project", "Product and project", None),
    ("business-operations", "Business and operations", None),
    ("finance-accounting", "Finance and accounting", "business-operations"),
    ("sales-marketing", "Sales and marketing", "business-operations"),
    ("people-hr", "People and HR", "business-operations"),
    ("healthcare-jobs", "Healthcare", None),
    ("education-jobs", "Education and training", None),
    ("hospitality", "Hospitality and tourism", None),
    ("construction", "Construction and engineering", None),
]

#: (slug, name) — mirrors the list the employer registration form already offers.
INDUSTRIES: list[tuple[str, str]] = [
    ("software-it", "Software & IT"),
    ("health-technology", "Health technology"),
    ("financial-services", "Financial services"),
    ("ecommerce", "E-commerce"),
    ("logistics", "Logistics"),
    ("manufacturing", "Manufacturing"),
    ("education", "Education"),
    ("hospitality", "Hospitality"),
    ("construction", "Construction"),
    ("media-marketing", "Media & marketing"),
    ("non-profit", "Non-profit & development"),
    ("other", "Other"),
]

#: (country, region, city, timezone) — Nepal first (SRS §1.4), then the cities
#: that appear in remote postings aimed at this market.
LOCATIONS: list[tuple[str, str, str, str]] = [
    ("NP", "Bagmati", "Kathmandu", "Asia/Kathmandu"),
    ("NP", "Bagmati", "Lalitpur", "Asia/Kathmandu"),
    ("NP", "Bagmati", "Bhaktapur", "Asia/Kathmandu"),
    ("NP", "Bagmati", "Dhulikhel", "Asia/Kathmandu"),
    ("NP", "Gandaki", "Pokhara", "Asia/Kathmandu"),
    ("NP", "Lumbini", "Butwal", "Asia/Kathmandu"),
    ("NP", "Koshi", "Biratnagar", "Asia/Kathmandu"),
    ("NP", "Madhesh", "Birgunj", "Asia/Kathmandu"),
    ("IN", "Karnataka", "Bengaluru", "Asia/Kolkata"),
    ("IN", "Delhi", "New Delhi", "Asia/Kolkata"),
    ("SG", "", "Singapore", "Asia/Singapore"),
    ("AE", "Dubai", "Dubai", "Asia/Dubai"),
    ("GB", "England", "London", "Europe/London"),
    ("US", "New York", "New York", "America/New_York"),
    ("AU", "New South Wales", "Sydney", "Australia/Sydney"),
]

#: (code, name)
LANGUAGES: list[tuple[str, str]] = [
    ("ne", "Nepali"),
    ("en", "English"),
    ("hi", "Hindi"),
    ("mai", "Maithili"),
    ("bho", "Bhojpuri"),
    ("new", "Newari"),
    ("ur", "Urdu"),
    ("zh", "Chinese"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("ar", "Arabic"),
    ("fr", "French"),
    ("de", "German"),
    ("es", "Spanish"),
]
