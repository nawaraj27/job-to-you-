import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.dev')
django.setup()

from django.db import connection

cursor = connection.cursor()

# Check existing tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [row[0] for row in cursor.fetchall()]
print("Tables:", tables)

# Add kind column to taxonomy_skill if it doesn't exist
if 'taxonomy_skill' in tables:
    cursor.execute("PRAGMA table_info(taxonomy_skill)")
    columns = [row[1] for row in cursor.fetchall()]
    print("taxonomy_skill columns:", columns)
    
    if 'kind' not in columns:
        print("Adding 'kind' column to taxonomy_skill...")
        cursor.execute("ALTER TABLE taxonomy_skill ADD COLUMN kind VARCHAR(24) DEFAULT 'unknown'")
        print("Column added")
    else:
        print("'kind' column already exists")

# Add ProvenancedModel columns to taxonomy_skillalias if they don't exist
if 'taxonomy_skillalias' in tables:
    cursor.execute("PRAGMA table_info(taxonomy_skillalias)")
    columns = [row[1] for row in cursor.fetchall()]
    print("taxonomy_skillalias columns:", columns)
    
    columns_to_add = {
        'source': "VARCHAR(16) DEFAULT 'curated'",
        'confidence': "DECIMAL(3,2)",
        'created_by_id': "INTEGER",
        'created_reason': "VARCHAR(200)",
        'batch': "VARCHAR(64)"
    }
    
    for col, col_def in columns_to_add.items():
        if col not in columns:
            print(f"Adding '{col}' column to taxonomy_skillalias...")
            cursor.execute(f"ALTER TABLE taxonomy_skillalias ADD COLUMN {col} {col_def}")
            print(f"Column '{col}' added")
        else:
            print(f"Column '{col}' already exists")

# Add ProvenancedModel columns to taxonomy_skillrelation if they don't exist
if 'taxonomy_skillrelation' in tables:
    cursor.execute("PRAGMA table_info(taxonomy_skillrelation)")
    columns = [row[1] for row in cursor.fetchall()]
    print("taxonomy_skillrelation columns:", columns)
    
    columns_to_add = {
        'source': "VARCHAR(16) DEFAULT 'curated'",
        'confidence': "DECIMAL(3,2)",
        'created_by_id': "INTEGER",
        'created_reason': "VARCHAR(200)",
        'batch': "VARCHAR(64)"
    }
    
    for col, col_def in columns_to_add.items():
        if col not in columns:
            print(f"Adding '{col}' column to taxonomy_skillrelation...")
            cursor.execute(f"ALTER TABLE taxonomy_skillrelation ADD COLUMN {col} {col_def}")
            print(f"Column '{col}' added")
        else:
            print(f"Column '{col}' already exists")

# Create audit_auditlog table if it doesn't exist
if 'audit_auditlog' not in tables:
    print("Creating audit_auditlog table...")
    cursor.execute("""
        CREATE TABLE audit_auditlog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER,
            on_behalf_of_id INTEGER,
            verb VARCHAR(100) NOT NULL,
            target_ct_id INTEGER,
            target_id VARCHAR(64),
            target_repr VARCHAR(255),
            company_id INTEGER,
            ip VARCHAR(39),
            request_id VARCHAR(64),
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("audit_auditlog table created")
else:
    print("audit_auditlog table already exists")

# Create missing tables that were faked in migrations
missing_tables = {
    'resumes_resumetemplate': """
        CREATE TABLE resumes_resumetemplate (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug VARCHAR(80) UNIQUE NOT NULL,
            name VARCHAR(120) NOT NULL,
            layout VARCHAR(20) NOT NULL,
            density VARCHAR(20) NOT NULL,
            is_default BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    'resumes_resume': """
        CREATE TABLE resumes_resume (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_profile_id INTEGER NOT NULL,
            title VARCHAR(120),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    'candidates_candidateprofile': """
        CREATE TABLE candidates_candidateprofile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            headline VARCHAR(120),
            summary TEXT,
            location_id INTEGER,
            salary_min INTEGER,
            salary_max INTEGER,
            salary_currency VARCHAR(3),
            salary_period VARCHAR(20),
            is_open_to_work BOOLEAN DEFAULT 1,
            is_open_to_remote BOOLEAN DEFAULT 1,
            is_open_to_relocation BOOLEAN DEFAULT 0,
            preferred_location_ids TEXT,
            job_category_ids TEXT,
            industry_ids TEXT,
            experience_months INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    'companies_company': """
        CREATE TABLE companies_company (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug VARCHAR(80) UNIQUE NOT NULL,
            name VARCHAR(120) NOT NULL,
            legal_name VARCHAR(120),
            description TEXT,
            website VARCHAR(200),
            founded_year INTEGER,
            industry_id INTEGER,
            size VARCHAR(20),
            logo VARCHAR(200),
            banner VARCHAR(200),
            location_id INTEGER,
            is_verified BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    'jobs_job': """
        CREATE TABLE jobs_job (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            title VARCHAR(120) NOT NULL,
            description TEXT NOT NULL,
            location_id INTEGER,
            job_category_id INTEGER,
            industry_id INTEGER,
            employment_type VARCHAR(20),
            experience_level VARCHAR(20),
            salary_min INTEGER,
            salary_max INTEGER,
            salary_currency VARCHAR(3),
            salary_period VARCHAR(20),
            is_remote BOOLEAN DEFAULT 0,
            is_active BOOLEAN DEFAULT 1,
            is_paused BOOLEAN DEFAULT 0,
            published_at TIMESTAMP,
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    'interviews_interviewtemplate': """
        CREATE TABLE interviews_interviewtemplate (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            kind VARCHAR(16) NOT NULL,
            name VARCHAR(120) NOT NULL,
            slug VARCHAR(140),
            config TEXT,
            question_pools TEXT,
            rubric_version INTEGER DEFAULT 1,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    'billing_plan': """
        CREATE TABLE billing_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code VARCHAR(40) UNIQUE NOT NULL,
            name VARCHAR(60) NOT NULL,
            audience VARCHAR(10) NOT NULL,
            summary VARCHAR(200),
            price DECIMAL(10,2) DEFAULT 0,
            currency VARCHAR(3) DEFAULT 'NPR',
            interval VARCHAR(8) DEFAULT 'month',
            is_quoted BOOLEAN DEFAULT 0,
            limits TEXT,
            is_default BOOLEAN DEFAULT 0,
            is_active BOOLEAN DEFAULT 1,
            "order" INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    'applications_pipelinestage': """
        CREATE TABLE applications_pipelinestage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            key VARCHAR(40) NOT NULL,
            label VARCHAR(60) NOT NULL,
            "order" INTEGER DEFAULT 0,
            kind VARCHAR(16) DEFAULT 'open',
            is_visible_to_candidate BOOLEAN DEFAULT 1,
            candidate_label VARCHAR(60),
            reveals_contact BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    'notifications_outboxevent': """
        CREATE TABLE notifications_outboxevent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(100) NOT NULL,
            context TEXT,
            recipient_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
}

for table_name, create_sql in missing_tables.items():
    if table_name not in tables:
        print(f"Creating {table_name} table...")
        cursor.execute(create_sql)
        print(f"{table_name} table created")
    else:
        print(f"{table_name} table already exists")

# Recreate interviews_interviewtemplate with correct schema
if 'interviews_interviewtemplate' in tables:
    cursor.execute("PRAGMA table_info(interviews_interviewtemplate)")
    columns = [row[1] for row in cursor.fetchall()]
    print("interviews_interviewtemplate columns:", columns)
    
    if 'company_id' not in columns or 'kind' not in columns:
        print("Recreating interviews_interviewtemplate table with correct schema...")
        cursor.execute("DROP TABLE interviews_interviewtemplate")
        cursor.execute("""
            CREATE TABLE interviews_interviewtemplate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER,
                kind VARCHAR(16) NOT NULL,
                name VARCHAR(120) NOT NULL,
                slug VARCHAR(140),
                config TEXT,
                question_pools TEXT,
                rubric_version INTEGER DEFAULT 1,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("interviews_interviewtemplate table recreated with correct schema")
    else:
        print("Table schema already correct")

# Recreate billing_plan with correct schema
if 'billing_plan' in tables:
    cursor.execute("PRAGMA table_info(billing_plan)")
    columns = [row[1] for row in cursor.fetchall()]
    print("billing_plan columns:", columns)
    
    if 'code' not in columns or 'audience' not in columns:
        print("Recreating billing_plan table with correct schema...")
        cursor.execute("DROP TABLE billing_plan")
        cursor.execute("""
            CREATE TABLE billing_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code VARCHAR(40) UNIQUE NOT NULL,
                name VARCHAR(60) NOT NULL,
                audience VARCHAR(10) NOT NULL,
                summary VARCHAR(200),
                price DECIMAL(10,2) DEFAULT 0,
                currency VARCHAR(3) DEFAULT 'NPR',
                interval VARCHAR(8) DEFAULT 'month',
                is_quoted BOOLEAN DEFAULT 0,
                limits TEXT,
                is_default BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                "order" INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("billing_plan table recreated with correct schema")
    else:
        print("Table schema already correct")

# Recreate applications_pipelinestage with correct schema
if 'applications_pipelinestage' in tables:
    cursor.execute("PRAGMA table_info(applications_pipelinestage)")
    columns = [row[1] for row in cursor.fetchall()]
    print("applications_pipelinestage columns:", columns)
    
    if 'key' not in columns or 'label' not in columns:
        print("Recreating applications_pipelinestage table with correct schema...")
        cursor.execute("DROP TABLE applications_pipelinestage")
        cursor.execute("""
            CREATE TABLE applications_pipelinestage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER,
                key VARCHAR(40) NOT NULL,
                label VARCHAR(60) NOT NULL,
                "order" INTEGER DEFAULT 0,
                kind VARCHAR(16) DEFAULT 'open',
                is_visible_to_candidate BOOLEAN DEFAULT 1,
                candidate_label VARCHAR(60),
                reveals_contact BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("applications_pipelinestage table recreated with correct schema")
    else:
        print("Table schema already correct")

# Add missing slug column to resumes_resumetemplate
if 'resumes_resumetemplate' in tables:
    cursor.execute("PRAGMA table_info(resumes_resumetemplate)")
    columns = [row[1] for row in cursor.fetchall()]
    print("resumes_resumetemplate columns:", columns)
    
    if 'slug' not in columns or 'family' not in columns or 'order' not in columns:
        print("Recreating resumes_resumetemplate table with correct schema...")
        cursor.execute("DROP TABLE resumes_resumetemplate")
        cursor.execute("""
            CREATE TABLE resumes_resumetemplate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug VARCHAR(60) UNIQUE NOT NULL,
                name VARCHAR(80) NOT NULL,
                family VARCHAR(40),
                layout VARCHAR(16) DEFAULT 'classic',
                accent VARCHAR(20),
                font VARCHAR(40),
                density VARCHAR(8) DEFAULT 'normal',
                tier VARCHAR(8) DEFAULT 'free',
                suits VARCHAR(200),
                notes TEXT,
                is_active BOOLEAN DEFAULT 1,
                preview_id INTEGER,
                "order" INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("resumes_resumetemplate table recreated with correct schema")
    else:
        print("Table schema already correct")
