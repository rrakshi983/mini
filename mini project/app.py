import json
import math
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("DATABASE_PATH", ROOT / "healthcare.db"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", ROOT / "private_uploads"))
UPLOAD_LIMIT = 5 * 1024 * 1024
PHOTO_DIR = Path(os.environ.get("PHOTO_DIR", ROOT / "doctor_photos"))

AVAILABILITY = {"Available", "Busy", "On Break", "Unavailable"}
APPOINTMENT_STATES = {
    "pending", "confirmed", "accepted", "rejected", "cancelled", "rescheduled", "completed"
}
ACTIVE_APPOINTMENT_STATES = ("pending", "confirmed", "accepted", "rescheduled")
BED_TYPES = ("General", "Private", "ICU")
ADMISSION_STATES = {"Pending", "Approved", "Rejected", "Discharged", "Cancelled"}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

SPECIALISTS = {
    "cardiologist", "dermatologist", "neurologist", "orthopedist",
    "pediatrician", "gynecologist", "general physician", "ophthalmologist",
    "endocrinologist", "pulmonologist", "ent specialist", "nephrologist",
    "gastroenterologist",
}

app = Flask(__name__)
cookie_secure = os.environ.get("COOKIE_SECURE", "0") == "1"
cookie_samesite = os.environ.get("COOKIE_SAMESITE", "None" if cookie_secure else "Lax")
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=UPLOAD_LIMIT,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=cookie_samesite,
    SESSION_COOKIE_SECURE=cookie_secure,
    SESSION_COOKIE_PARTITIONED=cookie_secure and cookie_samesite == "None",
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK(role IN ('patient','hospital','admin')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    age INTEGER,
    gender TEXT NOT NULL DEFAULT '',
    blood_group TEXT NOT NULL DEFAULT '',
    emergency_contact TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hospitals (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER UNIQUE REFERENCES users(id),
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    phone TEXT NOT NULL DEFAULT '',
    photo_url TEXT NOT NULL DEFAULT '',
    specialties TEXT NOT NULL DEFAULT '[]',
    facilities TEXT NOT NULL DEFAULT '[]',
    emergency_services INTEGER NOT NULL DEFAULT 0,
    opd_available INTEGER NOT NULL DEFAULT 1,
    ipd_available INTEGER NOT NULL DEFAULT 1,
    opd_timings TEXT NOT NULL DEFAULT '',
    verified INTEGER NOT NULL DEFAULT 0,
    is_sample INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'hospital dashboard',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doctors (
    id INTEGER PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qualification TEXT NOT NULL DEFAULT '',
    specialty TEXT NOT NULL,
    department TEXT NOT NULL DEFAULT '',
    experience_years INTEGER NOT NULL DEFAULT 0,
    consultation_fee INTEGER NOT NULL DEFAULT 0,
    opd_available INTEGER NOT NULL DEFAULT 1,
    languages TEXT NOT NULL DEFAULT '[]',
    consultation_timings TEXT NOT NULL DEFAULT '',
    photo_url TEXT NOT NULL DEFAULT '',
    is_sample INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doctor_availability (
    doctor_id INTEGER PRIMARY KEY REFERENCES doctors(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'Unavailable',
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS diseases (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    category TEXT NOT NULL DEFAULT '',
    recommended_specialist TEXT NOT NULL,
    department TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    guidance_note TEXT NOT NULL DEFAULT '',
    is_emergency_risk INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS doctor_specializations (
    id INTEGER PRIMARY KEY,
    doctor_id INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    specialization TEXT NOT NULL,
    department TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(doctor_id, specialization)
);
CREATE TABLE IF NOT EXISTS hospital_doctors (
    id INTEGER PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    doctor_id INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    department TEXT NOT NULL DEFAULT '',
    consultation_fee INTEGER NOT NULL DEFAULT 0,
    opd_available INTEGER NOT NULL DEFAULT 1,
    availability_status TEXT NOT NULL DEFAULT 'Unavailable'
        CHECK(availability_status IN ('Available','Busy','On Break','Unavailable')),
    is_active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(hospital_id, doctor_id)
);
CREATE TABLE IF NOT EXISTS doctor_schedules (
    id INTEGER PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    doctor_id INTEGER NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    day_of_week INTEGER NOT NULL CHECK(day_of_week BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    slot_duration_minutes INTEGER NOT NULL DEFAULT 30 CHECK(slot_duration_minutes BETWEEN 10 AND 120),
    max_slots INTEGER NOT NULL DEFAULT 8 CHECK(max_slots BETWEEN 1 AND 60),
    is_available INTEGER NOT NULL DEFAULT 1,
    specific_date TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(hospital_id, doctor_id, day_of_week, start_time, specific_date)
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    appointment_code TEXT,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
    doctor_id INTEGER NOT NULL REFERENCES doctors(id),
    disease_name TEXT NOT NULL DEFAULT '',
    department TEXT NOT NULL DEFAULT '',
    appointment_type TEXT NOT NULL DEFAULT 'OPD',
    consultation_fee INTEGER NOT NULL DEFAULT 0,
    appointment_date TEXT NOT NULL,
    appointment_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    reminder_sent_at TEXT
);
CREATE TABLE IF NOT EXISTS ipd_beds (
    id INTEGER PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    bed_type TEXT NOT NULL CHECK(bed_type IN ('General','Private','ICU')),
    total_beds INTEGER NOT NULL DEFAULT 0 CHECK(total_beds >= 0),
    available_beds INTEGER NOT NULL DEFAULT 0 CHECK(available_beds >= 0 AND available_beds <= total_beds),
    daily_charge INTEGER NOT NULL DEFAULT 0 CHECK(daily_charge >= 0),
    is_sample INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    updated_by INTEGER REFERENCES users(id),
    UNIQUE(hospital_id, bed_type)
);
CREATE TABLE IF NOT EXISTS admission_requests (
    id INTEGER PRIMARY KEY,
    request_code TEXT UNIQUE,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    doctor_id INTEGER REFERENCES doctors(id) ON DELETE SET NULL,
    bed_type TEXT NOT NULL CHECK(bed_type IN ('General','Private','ICU')),
    disease_or_reason TEXT NOT NULL,
    preferred_date TEXT NOT NULL,
    patient_phone TEXT NOT NULL DEFAULT '',
    emergency_contact TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Pending'
        CHECK(status IN ('Pending','Approved','Rejected','Discharged','Cancelled')),
    admin_note TEXT NOT NULL DEFAULT '',
    reviewed_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symptoms (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    symptoms_json TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    severity INTEGER NOT NULL,
    specialist TEXT NOT NULL,
    emergency INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS medical_history (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    condition TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    allergies TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prescriptions (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS favourites (
    patient_id INTEGER NOT NULL REFERENCES users(id),
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(patient_id, hospital_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    comment TEXT NOT NULL DEFAULT '',
    verified_visit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    message TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hospital_verification (
    id INTEGER PRIMARY KEY,
    hospital_id INTEGER NOT NULL REFERENCES hospitals(id) ON DELETE CASCADE,
    admin_id INTEGER REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES users(id),
    hospital_id INTEGER REFERENCES hospitals(id),
    doctor_id INTEGER REFERENCES doctors(id),
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    event TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appointments_patient ON appointments(patient_id, appointment_date);
CREATE INDEX IF NOT EXISTS idx_appointments_hospital ON appointments(hospital_id, appointment_date);
CREATE INDEX IF NOT EXISTS idx_doctors_hospital ON doctors(hospital_id);
CREATE INDEX IF NOT EXISTS idx_hospital_doctors_hospital ON hospital_doctors(hospital_id);
CREATE INDEX IF NOT EXISTS idx_hospital_doctors_doctor ON hospital_doctors(doctor_id);
CREATE INDEX IF NOT EXISTS idx_schedules_lookup ON doctor_schedules(hospital_id, doctor_id, day_of_week);
CREATE INDEX IF NOT EXISTS idx_ipd_beds_hospital ON ipd_beds(hospital_id);
CREATE INDEX IF NOT EXISTS idx_admission_patient ON admission_requests(patient_id, created_at);
CREATE INDEX IF NOT EXISTS idx_admission_hospital ON admission_requests(hospital_id, status);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_verified_feedback_per_patient
    ON feedback(patient_id, hospital_id) WHERE verified_visit=1;
"""

DEMO_ACCOUNTS = [
    ("patient@example.com", "Patient123!", "Asha Patient", "patient"),
    ("hospital@example.com", "Hospital123!", "Demo Hospital Staff", "hospital"),
    ("admin@example.com", "Admin123!", "Platform Admin", "admin"),
]

DISEASE_CATALOG = [
    (
        "Heart Disease", "Cardiovascular", "Cardiologist", "Cardiology",
        "heart,cardiac,chest pain,hypertension,blood pressure,bp,palpitations,arrhythmia,coronary,angina,cholesterol",
        "A cardiologist evaluates heart and cardiovascular concerns. Seek immediate emergency care for sudden chest pain or shortness of breath.",
        1,
    ),
    (
        "Skin Disease", "Dermatological", "Dermatologist", "Dermatology",
        "skin,rash,acne,eczema,psoriasis,itching,fungal,allergy,hair loss,dermatitis,vitiligo",
        "A dermatologist assesses skin, hair, and nail conditions.",
        0,
    ),
    (
        "Eye Problems", "Ophthalmic", "Ophthalmologist", "Ophthalmology",
        "eye,vision,cataract,glaucoma,blurred vision,red eye,conjunctivitis,spectacles,retina,eye pain",
        "An ophthalmologist examines eye health, vision changes, and ocular conditions.",
        0,
    ),
    (
        "Bone & Joint Problems", "Musculoskeletal", "Orthopedist", "Orthopedics",
        "bone,joint,arthritis,fracture,knee pain,back pain,spine,ligament,shoulder pain,sprain,orthopedic",
        "An orthopedic specialist treats bone, joint, spine, and musculoskeletal injuries or pain.",
        0,
    ),
    (
        "Brain & Nervous System Problems", "Neurological", "Neurologist", "Neurology",
        "brain,nerve,nervous system,migraine,headache,seizure,epilepsy,stroke,numbness,dizziness,parkinson,neuropathy",
        "A neurologist evaluates brain, spinal cord, and peripheral nerve conditions. Sudden weakness, slurred speech, or seizures need emergency care.",
        1,
    ),
    (
        "Diabetes", "Endocrine & Metabolic", "Endocrinologist", "Endocrinology & Diabetology",
        "diabetes,blood sugar,insulin,diabetologist,thyroid,pcos,hormone,endocrine,hba1c,glucose",
        "An endocrinologist or diabetologist specializes in diabetes, thyroid, and hormonal disorders.",
        0,
    ),
    (
        "Asthma & Breathing Problems", "Respiratory", "Pulmonologist", "Pulmonology",
        "asthma,lungs,breathing,copd,bronchitis,chronic cough,wheezing,pneumonia,tuberculosis,respiratory",
        "A pulmonologist evaluates lung and airway conditions. Severe breathing difficulty requires urgent emergency attention.",
        1,
    ),
    (
        "Child Health & Pediatric Care", "Pediatrics", "Pediatrician", "Pediatrics",
        "child,baby,infant,pediatric,vaccination,child fever,growth,newborn,kids",
        "A pediatrician provides preventive and clinical care for infants, children, and adolescents.",
        0,
    ),
    (
        "Pregnancy & Women's Health", "Obstetrics & Gynecology", "Gynecologist", "Obstetrics & Gynecology",
        "pregnancy,maternity,prenatal,gynecology,menstrual,pcos,women health,antenatal",
        "A gynecologist or obstetrician provides prenatal, reproductive, and women's healthcare.",
        0,
    ),
    (
        "Ear, Nose & Throat (ENT) Problems", "ENT", "ENT Specialist", "ENT",
        "ear,nose,throat,sinus,sinusitis,tonsils,hearing,vertigo,snoring,ent,sore throat",
        "An ENT specialist evaluates ear, sinus, throat, and hearing-related concerns.",
        0,
    ),
    (
        "Kidney & Urinary Problems", "Renal", "Nephrologist", "Nephrology",
        "kidney,renal,kidney stone,uti,dialysis,creatinine,urinary,swelling",
        "A nephrologist evaluates kidney function and renal conditions.",
        0,
    ),
    (
        "Digestive & Stomach Problems", "Gastrointestinal", "Gastroenterologist", "Gastroenterology",
        "stomach,gastric,acidity,ulcer,liver,jaundice,digestion,constipation,ibs,abdomen",
        "A gastroenterologist diagnoses and manages digestive tract and liver conditions.",
        0,
    ),
    (
        "Fever & General Health Concerns", "General Medicine", "General Physician", "General Medicine",
        "fever,cold,cough,flu,viral,fatigue,body ache,weakness,checkup,general,infection",
        "A general physician is a reliable first consultation for common symptoms and primary care.",
        0,
    ),
]

SPECIALTY_TO_DEPARTMENT = {
    "cardiologist": "Cardiology",
    "dermatologist": "Dermatology",
    "ophthalmologist": "Ophthalmology",
    "orthopedist": "Orthopedics",
    "neurologist": "Neurology",
    "endocrinologist": "Endocrinology & Diabetology",
    "diabetologist": "Endocrinology & Diabetology",
    "pulmonologist": "Pulmonology",
    "pediatrician": "Pediatrics",
    "gynecologist": "Obstetrics & Gynecology",
    "ent specialist": "ENT",
    "nephrologist": "Nephrology",
    "gastroenterologist": "Gastroenterology",
    "general physician": "General Medicine",
}

EMERGENCY_TERMS = {
    "chest pain", "difficulty breathing", "shortness of breath", "severe bleeding",
    "unconscious", "seizure", "face drooping", "suicidal thoughts",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def department_for_specialty(specialty):
    key = str(specialty or "").strip().lower()
    if key in SPECIALTY_TO_DEPARTMENT:
        return SPECIALTY_TO_DEPARTMENT[key]
    for name, dept in SPECIALTY_TO_DEPARTMENT.items():
        if name in key or key in name:
            return dept
    return str(specialty or "General Medicine").strip() or "General Medicine"


def generate_code(prefix, target_date=None):
    stamp = (target_date or date.today()).strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{secrets.token_hex(3).upper()}"


def connect_db():
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")
    return db


def ensure_columns(db, table, additions):
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for col, ddl in additions:
        if col not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    db = connect_db()
    try:
        db.executescript(SCHEMA)

        # Additive, non-destructive column migrations for existing databases
        ensure_columns(db, "users", [
            ("phone", "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns(db, "hospitals", [
            ("opd_available", "INTEGER NOT NULL DEFAULT 1"),
            ("ipd_available", "INTEGER NOT NULL DEFAULT 1"),
            ("opd_timings", "TEXT NOT NULL DEFAULT ''"),
        ])
        ensure_columns(db, "doctors", [
            ("department", "TEXT NOT NULL DEFAULT ''"),
            ("consultation_fee", "INTEGER NOT NULL DEFAULT 0"),
            ("opd_available", "INTEGER NOT NULL DEFAULT 1"),
        ])
        ensure_columns(db, "appointments", [
            ("appointment_code", "TEXT"),
            ("disease_name", "TEXT NOT NULL DEFAULT ''"),
            ("department", "TEXT NOT NULL DEFAULT ''"),
            ("appointment_type", "TEXT NOT NULL DEFAULT 'OPD'"),
            ("consultation_fee", "INTEGER NOT NULL DEFAULT 0"),
        ])

        # Ensure active slot unique index covers 'confirmed' as well as 'pending','accepted','rescheduled'
        idx_row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_unique_active_appointment_slot'"
        ).fetchone()
        if not idx_row or "'confirmed'" not in (idx_row["sql"] or ""):
            db.execute("DROP INDEX IF EXISTS idx_unique_active_appointment_slot")
            db.execute(
                """CREATE UNIQUE INDEX idx_unique_active_appointment_slot
                   ON appointments(doctor_id, appointment_date, appointment_time)
                   WHERE status IN ('pending','confirmed','accepted','rescheduled')"""
            )

        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_code ON appointments(appointment_code) WHERE appointment_code IS NOT NULL"
        )

        # Backfill appointment_code for any legacy appointments
        for row in db.execute("SELECT id FROM appointments WHERE appointment_code IS NULL OR appointment_code=''").fetchall():
            db.execute(
                "UPDATE appointments SET appointment_code=? WHERE id=?",
                (f"CF-LEGACY-{row['id']:04d}", row["id"]),
            )

        # Seed demo user accounts (idempotent)
        for email, password, name, role in DEMO_ACCOUNTS:
            db.execute(
                "INSERT OR IGNORE INTO users(email,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                (email, generate_password_hash(password), name, role, now_iso()),
            )

        # Ensure every patient user has a linked record in patients table
        for row in db.execute("SELECT id FROM users WHERE role='patient'").fetchall():
            db.execute(
                "INSERT OR IGNORE INTO patients(user_id, updated_at) VALUES(?,?)",
                (row["id"], now_iso()),
            )

        # Seed disease-to-specialist knowledge catalog (idempotent)
        for item in DISEASE_CATALOG:
            db.execute(
                """INSERT OR IGNORE INTO diseases
                   (name, category, recommended_specialist, department, keywords, guidance_note, is_emergency_risk)
                   VALUES(?,?,?,?,?,?,?)""",
                item,
            )

        # Existing Academic Demo Hospital (preserved)
        hospital_user = db.execute("SELECT id FROM users WHERE email=?", ("hospital@example.com",)).fetchone()
        db.execute(
            """INSERT OR IGNORE INTO hospitals(owner_id,name,address,area,city,latitude,longitude,phone,
               specialties,facilities,emergency_services,opd_available,ipd_available,opd_timings,
               verified,is_sample,source,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                hospital_user["id"], "Academic Demo Hospital · SAMPLE ONLY",
                "Demo listing — not a real healthcare facility", "Academic sample", "Bengaluru",
                12.9716, 77.5946, "", json.dumps(["General Physician", "Dermatologist", "Cardiologist"]),
                json.dumps(["OPD Clinic (Demo)", "IPD Ward (Demo)", "Academic sample record"]),
                0, 1, 1, "Mon–Sat, 09:00 AM – 05:00 PM (Demo)",
                0, 1, "academic sample seed", now_iso(),
            ),
        )
        demo_hospital = db.execute(
            "SELECT id FROM hospitals WHERE owner_id=?", (hospital_user["id"],)
        ).fetchone()
        if demo_hospital:
            db.execute(
                """INSERT OR IGNORE INTO doctors(hospital_id,name,qualification,specialty,department,
                   experience_years,consultation_fee,opd_available,languages,consultation_timings,photo_url,is_sample,updated_at)
                   SELECT ?,?,?,?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS
                   (SELECT 1 FROM doctors WHERE hospital_id=? AND name=?)""",
                (
                    demo_hospital["id"], "Demo Provider · SAMPLE ONLY", "Academic sample",
                    "General Physician", "General Medicine", 0, 300, 1, json.dumps(["English", "Kannada"]),
                    "Set timings in the hospital dashboard", "", 1, now_iso(),
                    demo_hospital["id"], "Demo Provider · SAMPLE ONLY",
                ),
            )
            demo_doc = db.execute(
                "SELECT id FROM doctors WHERE hospital_id=? AND name=?",
                (demo_hospital["id"], "Demo Provider · SAMPLE ONLY"),
            ).fetchone()
            if demo_doc:
                db.execute(
                    "INSERT OR IGNORE INTO doctor_availability(doctor_id,status,updated_at) VALUES(?,?,?)",
                    (demo_doc["id"], "Unavailable", now_iso()),
                )
            # Sample IPD beds for Academic Demo Hospital
            for bed_type, total, avail, charge in [("General", 25, 10, 600), ("Private", 10, 4, 2200), ("ICU", 6, 2, 5500)]:
                db.execute(
                    """INSERT OR IGNORE INTO ipd_beds(hospital_id,bed_type,total_beds,available_beds,daily_charge,is_sample,updated_at)
                       VALUES(?,?,?,?,?,1,?)""",
                    (demo_hospital["id"], bed_type, total, avail, charge, now_iso()),
                )

        # Clearly labeled DEMO DATA showcase hospital in Hassan for testing OPD, IPD, Disease Search & Slots
        hassan_demo_name = "Hassan District Hospital · DEMO DATA"
        hassan_demo = db.execute("SELECT id FROM hospitals WHERE name=?", (hassan_demo_name,)).fetchone()
        if not hassan_demo:
            cur = db.execute(
                """INSERT INTO hospitals(owner_id,name,address,area,city,latitude,longitude,phone,
                   specialties,facilities,emergency_services,opd_available,ipd_available,opd_timings,
                   verified,is_sample,source,created_at)
                   VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    hassan_demo_name,
                    "Hospital Road, Chamarajapura, Hassan, Karnataka (Demo sample record)",
                    "Chamarajapura", "Hassan", 13.0068, 76.0996, "+91 8172 000000 (Demo)",
                    json.dumps([
                        "Cardiologist", "Dermatologist", "Ophthalmologist",
                        "Orthopedist", "Neurologist", "Endocrinologist", "General Physician"
                    ]),
                    json.dumps(["24x7 Emergency (Demo)", "OPD Block (Demo)", "IPD General/Private/ICU (Demo)", "Diagnostic Lab (Demo)"]),
                    1, 1, 1, "Mon–Sat, 09:00 AM – 05:30 PM (Demo)",
                    1, 1, "demo seed data", now_iso(),
                ),
            )
            hassan_demo_id = cur.lastrowid
        else:
            hassan_demo_id = hassan_demo["id"]

        # Seed clearly labeled demo specialists & schedules at Hassan District Hospital · DEMO DATA
        demo_specialists = [
            ("Dr. Ananya Rao (Demo)", "MBBS, MD, DM (Cardiology)", "Cardiologist", "Cardiology", 12, 500,
             ["English", "Kannada", "Hindi"], "Mon–Sat · 09:00–12:30, 14:30–17:00", "Available",
             [(d, "09:00", "12:30", 30, 7) for d in range(6)] + [(d, "14:30", "17:00", 30, 5) for d in range(6)]),
            ("Dr. Karthik Shetty (Demo)", "MBBS, MD (Dermatology)", "Dermatologist", "Dermatology", 8, 400,
             ["English", "Kannada"], "Mon–Sat · 10:00–13:00, 15:00–17:30", "Available",
             [(d, "10:00", "13:00", 30, 6) for d in range(6)] + [(d, "15:00", "17:30", 30, 5) for d in range(6)]),
            ("Dr. Meera Nair (Demo)", "MBBS, MS (Ophthalmology)", "Ophthalmologist", "Ophthalmology", 10, 450,
             ["English", "Kannada", "Malayalam"], "Mon–Fri · 09:30–13:00", "Available",
             [(d, "09:30", "13:00", 30, 7) for d in range(5)]),
            ("Dr. Rajesh Kulkarni (Demo)", "MBBS, MS (Orthopedics)", "Orthopedist", "Orthopedics", 14, 550,
             ["English", "Kannada", "Hindi"], "Mon–Sat · 09:00–12:00, 16:00–19:00", "Available",
             [(d, "09:00", "12:00", 30, 6) for d in range(6)] + [(d, "16:00", "19:00", 30, 6) for d in range(6)]),
            ("Dr. Sneha Patil (Demo)", "MBBS, DM (Neurology)", "Neurologist", "Neurology", 9, 650,
             ["English", "Kannada"], "Mon–Fri · 10:00–13:30", "Available",
             [(d, "10:00", "13:30", 30, 7) for d in range(5)]),
            ("Dr. Vikram Gowda (Demo)", "MBBS, MD, DM (Endocrinology)", "Endocrinologist", "Endocrinology & Diabetology", 11, 500,
             ["English", "Kannada"], "Mon–Sat · 09:00–12:00, 14:30–17:30", "Available",
             [(d, "09:00", "12:00", 30, 6) for d in range(6)] + [(d, "14:30", "17:30", 30, 6) for d in range(6)]),
            ("Dr. Priya Deshmukh (Demo)", "MBBS, MD (General Medicine)", "General Physician", "General Medicine", 7, 300,
             ["English", "Kannada", "Hindi"], "Daily · 09:00–13:00, 16:00–20:00", "Available",
             [(d, "09:00", "13:00", 30, 8) for d in range(7)] + [(d, "16:00", "20:00", 30, 8) for d in range(7)]),
            ("Dr. Arun Kumar (Demo)", "MBBS, MD (Cardiology)", "Cardiologist", "Cardiology", 15, 600,
             ["English", "Kannada"], "Currently on leave (Unavailable)", "Unavailable",
             [(d, "10:00", "12:00", 30, 4) for d in range(5)]),
        ]

        for name, qual, spec, dept, yrs, fee, langs, timings, status, schedules in demo_specialists:
            existing_doc = db.execute(
                "SELECT id FROM doctors WHERE hospital_id=? AND name=?", (hassan_demo_id, name)
            ).fetchone()
            if not existing_doc:
                d_cur = db.execute(
                    """INSERT INTO doctors(hospital_id,name,qualification,specialty,department,
                       experience_years,consultation_fee,opd_available,languages,consultation_timings,
                       photo_url,is_sample,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                    (hassan_demo_id, name, qual, spec, dept, yrs, fee, 1,
                     json.dumps(langs), timings, "", now_iso()),
                )
                doc_id = d_cur.lastrowid
                db.execute(
                    "INSERT OR IGNORE INTO doctor_availability(doctor_id,status,updated_at) VALUES(?,?,?)",
                    (doc_id, status, now_iso()),
                )
                db.execute(
                    """INSERT OR IGNORE INTO doctor_specializations(doctor_id,specialization,department,created_at)
                       VALUES(?,?,?,?)""",
                    (doc_id, spec, dept, now_iso()),
                )
                db.execute(
                    """INSERT OR IGNORE INTO hospital_doctors
                       (hospital_id,doctor_id,department,consultation_fee,opd_available,availability_status,is_active,updated_at)
                       VALUES(?,?,?,?,?,?,1,?)""",
                    (hassan_demo_id, doc_id, dept, fee, 1, status, now_iso()),
                )
                for dow, st, et, dur, mslots in schedules:
                    db.execute(
                        """INSERT OR IGNORE INTO doctor_schedules
                           (hospital_id,doctor_id,day_of_week,start_time,end_time,slot_duration_minutes,
                            max_slots,is_available,specific_date,updated_at)
                           VALUES(?,?,?,?,?,?,?,1,'',?)""",
                        (hassan_demo_id, doc_id, dow, st, et, dur, mslots, now_iso()),
                    )

        # Sample IPD beds for Hassan District Hospital · DEMO DATA (clearly tagged is_sample=1)
        for bed_type, total, avail, charge in [("General", 40, 14, 800), ("Private", 15, 5, 2500), ("ICU", 10, 3, 6500)]:
            db.execute(
                """INSERT OR IGNORE INTO ipd_beds(hospital_id,bed_type,total_beds,available_beds,daily_charge,is_sample,updated_at)
                   VALUES(?,?,?,?,?,1,?)""",
                (hassan_demo_id, bed_type, total, avail, charge, now_iso()),
            )

        # Sync ALL existing doctors into doctor_specializations and hospital_doctors (preserving existing records)
        existing_doctors = db.execute(
            """SELECT d.*, COALESCE(a.status, 'Unavailable') AS avail_status
               FROM doctors d LEFT JOIN doctor_availability a ON a.doctor_id=d.id"""
        ).fetchall()
        for doc in existing_doctors:
            dept = doc["department"] or department_for_specialty(doc["specialty"])
            if not doc["department"]:
                db.execute("UPDATE doctors SET department=? WHERE id=?", (dept, doc["id"]))
            db.execute(
                """INSERT OR IGNORE INTO doctor_specializations(doctor_id,specialization,department,created_at)
                   VALUES(?,?,?,?)""",
                (doc["id"], doc["specialty"], dept, now_iso()),
            )
            db.execute(
                """INSERT OR IGNORE INTO hospital_doctors
                   (hospital_id,doctor_id,department,consultation_fee,opd_available,availability_status,is_active,updated_at)
                   VALUES(?,?,?,?,?,?,1,?)""",
                (doc["hospital_id"], doc["id"], dept, doc["consultation_fee"] or 0,
                 doc["opd_available"] if doc["opd_available"] is not None else 1,
                 doc["avail_status"] if doc["avail_status"] in AVAILABILITY else "Unavailable",
                 now_iso()),
            )

        db.commit()
    finally:
        db.close()


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def error(message, status=400):
    return jsonify({"error": message}), status


def audit(event):
    if session.get("user_id"):
        get_db().execute(
            "INSERT INTO activity_log(user_id,event,created_at) VALUES(?,?,?)",
            (session["user_id"], event[:160], now_iso()),
        )
        get_db().commit()


def notify(user_id, message):
    get_db().execute(
        "INSERT INTO notifications(user_id,message,created_at) VALUES(?,?,?)",
        (user_id, message[:300], now_iso()),
    )


def notify_admins(db, message):
    for row in db.execute("SELECT id FROM users WHERE role='admin' AND active=1").fetchall():
        notify(row["id"], message)


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return error("Sign in to continue.", 401)
        user = get_db().execute(
            "SELECT id,email,full_name,phone,role,active FROM users WHERE id=?", (session["user_id"],)
        ).fetchone()
        if not user or not user["active"]:
            session.clear()
            return error("This account is unavailable. Contact an administrator.", 403)
        g.user = user
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            token = request.headers.get("X-CSRF-Token", "")
            if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
                return error("Your session token expired. Refresh and try again.", 403)
        return fn(*args, **kwargs)
    return wrapped


def roles(*allowed):
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in allowed:
                return error("You do not have permission to do that.", 403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def body_json():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def valid_coords(lat, lon):
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    try:
        lat, lon = float(lat), float(lon)
        if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return lat, lon
    except (TypeError, ValueError):
        return None


def safe_hospital(row):
    data = dict(row)
    data["specialties"] = json.loads(data.get("specialties") or "[]")
    data["facilities"] = json.loads(data.get("facilities") or "[]")
    data["is_sample"] = bool(data.get("is_sample"))
    data["verified"] = bool(data.get("verified"))
    data["emergency_services"] = bool(data.get("emergency_services"))
    data["opd_available"] = bool(data.get("opd_available", 1))
    data["ipd_available"] = bool(data.get("ipd_available", 1))
    return data


def safe_doctor(row):
    data = dict(row)
    data["languages"] = json.loads(data.get("languages") or "[]")
    data["is_sample"] = bool(data.get("is_sample"))
    data["opd_available"] = bool(data.get("opd_available", 1))
    data["hospital_is_sample"] = bool(data.get("hospital_is_sample", 0))
    data["hospital_verified"] = bool(data.get("hospital_verified", 0))
    data["consultation_fee"] = int(data.get("consultation_fee") or 0)
    data["department"] = data.get("department") or department_for_specialty(data.get("specialty"))
    return data


def valid_photo_url(value):
    return not value or bool(
        re.fullmatch(r"/api/doctor-photos/[a-f0-9]{32}\.(?:png|jpe?g)", value)
    )


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def http_json(url, method="GET", data=None, timeout=8):
    headers = {"User-Agent": "NearbyCareAcademic/1.0 (local healthcare project)"}
    payload = None
    if data is not None:
        payload = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def geocode(query):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query[:180], "format": "jsonv2", "limit": 1}
    )
    result = http_json(url)
    return result[0] if result else None


def specialist_for(symptoms):
    text = " ".join(symptoms).lower()
    rules = [
        (("child health concern",), "Pediatrician",
         "A pediatrician is a suitable first point of contact for a child's health concern."),
        (("pregnancy concern",), "Gynecologist",
         "A gynecologist can assess pregnancy-related or reproductive-health concerns."),
        (("chest pain", "shortness of breath", "palpitations"), "Cardiologist",
         "Heart or breathing-related symptoms are commonly assessed by a cardiologist."),
        (("rash", "itching", "skin changes"), "Dermatologist",
         "Skin-related symptoms are commonly assessed by a dermatologist."),
        (("headache", "dizziness", "numbness", "seizure"), "Neurologist",
         "Neurological symptoms are commonly assessed by a neurologist."),
        (("joint pain", "back pain", "bone pain", "injury"), "Orthopedist",
         "Bone, joint, and injury symptoms are commonly assessed by an orthopedist."),
        (("fever", "cough", "sore throat", "stomach pain", "fatigue"), "General Physician",
         "A general physician can assess common symptoms and advise on next steps."),
    ]
    for terms, specialty, explanation in rules:
        if any(term in text for term in terms):
            return specialty, explanation
    return "General Physician", "A general physician is a reasonable starting point for the symptoms selected."


def get_doctor_specializations_map(db):
    rows = db.execute("SELECT doctor_id, specialization FROM doctor_specializations ORDER BY id").fetchall()
    mapping = {}
    for r in rows:
        mapping.setdefault(r["doctor_id"], []).append(r["specialization"])
    return mapping


def get_doctors(db, hospital_id=None):
    query = """SELECT d.id, hd.hospital_id, d.name, d.qualification, d.specialty,
                      COALESCE(NULLIF(hd.department, ''), NULLIF(d.department, ''), d.specialty) AS department,
                      d.experience_years,
                      CASE WHEN hd.consultation_fee > 0 THEN hd.consultation_fee ELSE d.consultation_fee END AS consultation_fee,
                      hd.opd_available,
                      d.languages, d.consultation_timings, d.photo_url, d.is_sample, d.updated_at,
                      COALESCE(hd.availability_status, a.status, 'Unavailable') AS availability,
                      COALESCE(hd.updated_at, a.updated_at) AS availability_updated_at,
                      h.name AS hospital_name, h.city AS hospital_city, h.area AS hospital_area,
                      h.address AS hospital_address, h.is_sample AS hospital_is_sample,
                      h.verified AS hospital_verified, h.opd_available AS hospital_opd_available,
                      h.ipd_available AS hospital_ipd_available
               FROM hospital_doctors hd
               JOIN doctors d ON d.id = hd.doctor_id
               JOIN hospitals h ON h.id = hd.hospital_id
               LEFT JOIN doctor_availability a ON a.doctor_id = d.id
               WHERE hd.is_active = 1"""
    params = []
    if hospital_id is not None:
        query += " AND hd.hospital_id = ?"
        params.append(hospital_id)
    query += " ORDER BY d.name, h.name"
    spec_map = get_doctor_specializations_map(db)
    results = []
    for row in db.execute(query, params).fetchall():
        doc = safe_doctor(row)
        doc["specializations"] = spec_map.get(doc["id"], [doc["specialty"]])
        results.append(doc)
    return results


def parse_hhmm(value):
    dt = datetime.strptime(str(value).strip(), "%H:%M")
    return dt.hour * 60 + dt.minute


def format_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def compute_doctor_date_slots(db, hospital_id, doctor_id, target_date):
    """
    Strictly verifies whether a specific doctor is actually available at a specific
    hospital on a specific date, and computes real remaining appointment slots.
    Never returns is_available=True if unavailable, unscheduled, or out of slots.
    """
    assignment = db.execute(
        """SELECT hd.*, d.name AS doctor_name, d.specialty, d.consultation_timings, d.is_sample AS doctor_is_sample,
                  COALESCE(hd.availability_status, a.status, 'Unavailable') AS effective_status,
                  h.name AS hospital_name, h.verified AS hospital_verified, h.is_sample AS hospital_is_sample,
                  h.opd_available AS hospital_opd_available
           FROM hospital_doctors hd
           JOIN doctors d ON d.id = hd.doctor_id
           JOIN hospitals h ON h.id = hd.hospital_id
           LEFT JOIN doctor_availability a ON a.doctor_id = d.id
           WHERE hd.hospital_id = ? AND hd.doctor_id = ? AND hd.is_active = 1""",
        (hospital_id, doctor_id),
    ).fetchone()

    if not assignment:
        return {
            "found": False,
            "is_available": False,
            "has_schedules": False,
            "status": "Unassigned",
            "reason": "This doctor is not assigned to the selected hospital.",
            "slots": [],
            "available_slots": [],
            "booked_slots": [],
            "total_slots": 0,
            "remaining_slots": 0,
            "schedule_windows": [],
        }

    if not assignment["hospital_verified"] and not assignment["hospital_is_sample"]:
        return {
            "found": True,
            "is_available": False,
            "has_schedules": False,
            "status": "Hospital Unverified",
            "reason": "This hospital profile is awaiting verification and cannot accept bookings.",
            "slots": [],
            "available_slots": [],
            "booked_slots": [],
            "total_slots": 0,
            "remaining_slots": 0,
            "schedule_windows": [],
        }

    if not assignment["hospital_opd_available"] or not assignment["opd_available"]:
        return {
            "found": True,
            "is_available": False,
            "has_schedules": False,
            "status": "OPD Closed",
            "reason": "OPD service is currently disabled for this doctor at the selected hospital.",
            "slots": [],
            "available_slots": [],
            "booked_slots": [],
            "total_slots": 0,
            "remaining_slots": 0,
            "schedule_windows": [],
        }

    eff_status = assignment["effective_status"]
    if eff_status != "Available":
        return {
            "found": True,
            "is_available": False,
            "has_schedules": False,
            "status": eff_status,
            "reason": f"Doctor is currently marked {eff_status} at {assignment['hospital_name']}.",
            "slots": [],
            "available_slots": [],
            "booked_slots": [],
            "total_slots": 0,
            "remaining_slots": 0,
            "schedule_windows": [],
        }

    if target_date < date.today():
        return {
            "found": True,
            "is_available": False,
            "has_schedules": True,
            "status": "Past Date",
            "reason": "Appointments cannot be booked for past dates.",
            "slots": [],
            "available_slots": [],
            "booked_slots": [],
            "total_slots": 0,
            "remaining_slots": 0,
            "schedule_windows": [],
        }

    iso_date = target_date.isoformat()
    dow = target_date.weekday()

    # Check if doctor has any schedules configured at this hospital
    any_sched_count = db.execute(
        "SELECT COUNT(*) FROM doctor_schedules WHERE hospital_id=? AND doctor_id=?",
        (hospital_id, doctor_id),
    ).fetchone()[0]

    # Check specific date overrides first
    overrides = db.execute(
        """SELECT * FROM doctor_schedules
           WHERE hospital_id=? AND doctor_id=? AND specific_date=?
           ORDER BY start_time""",
        (hospital_id, doctor_id, iso_date),
    ).fetchall()

    if overrides:
        if any(not row["is_available"] for row in overrides) and not any(row["is_available"] for row in overrides):
            return {
                "found": True,
                "is_available": False,
                "has_schedules": True,
                "status": "Unavailable on Date",
                "reason": f"Doctor is marked unavailable on {iso_date} at {assignment['hospital_name']}.",
                "slots": [],
                "available_slots": [],
                "booked_slots": [],
                "total_slots": 0,
                "remaining_slots": 0,
                "schedule_windows": [],
            }
        active_windows = [row for row in overrides if row["is_available"]]
    else:
        active_windows = db.execute(
            """SELECT * FROM doctor_schedules
               WHERE hospital_id=? AND doctor_id=? AND day_of_week=? AND specific_date='' AND is_available=1
               ORDER BY start_time""",
            (hospital_id, doctor_id, dow),
        ).fetchall()

    if not active_windows:
        if any_sched_count == 0:
            reason = (
                f"No OPD schedule slots have been configured yet for {assignment['doctor_name']} "
                f"at {assignment['hospital_name']}."
            )
        else:
            reason = (
                f"{assignment['doctor_name']} has no OPD schedule at {assignment['hospital_name']} "
                f"on {WEEKDAY_NAMES[dow]} ({iso_date})."
            )
        return {
            "found": True,
            "is_available": False,
            "has_schedules": any_sched_count > 0,
            "status": "No Schedule",
            "reason": reason,
            "slots": [],
            "available_slots": [],
            "booked_slots": [],
            "total_slots": 0,
            "remaining_slots": 0,
            "schedule_windows": [],
        }

    # Fetch booked times for this doctor on this date across all hospitals (prevent double booking)
    booked_rows = db.execute(
        """SELECT appointment_time FROM appointments
           WHERE doctor_id=? AND appointment_date=?
             AND status IN ('pending','confirmed','accepted','rescheduled')""",
        (doctor_id, iso_date),
    ).fetchall()
    booked_set = {row["appointment_time"] for row in booked_rows}

    now_hhmm = datetime.now().strftime("%H:%M")
    is_today = target_date == date.today()

    seen_times = set()
    slots = []
    windows_info = []
    for win in active_windows:
        windows_info.append({
            "id": win["id"],
            "start_time": win["start_time"],
            "end_time": win["end_time"],
            "slot_duration_minutes": win["slot_duration_minutes"],
            "max_slots": win["max_slots"],
        })
        try:
            start_min = parse_hhmm(win["start_time"])
            end_min = parse_hhmm(win["end_time"])
        except ValueError:
            continue
        step = max(10, int(win["slot_duration_minutes"] or 30))
        limit = max(1, int(win["max_slots"] or 8))
        count = 0
        cursor = start_min
        while cursor < end_min and count < limit:
            hhmm = format_hhmm(cursor)
            if hhmm not in seen_times:
                seen_times.add(hhmm)
                is_past = is_today and hhmm <= now_hhmm
                is_booked = hhmm in booked_set
                slots.append({
                    "time": hhmm,
                    "booked": is_booked,
                    "past": is_past,
                    "available": (not is_booked) and (not is_past),
                })
                count += 1
            cursor += step

    slots.sort(key=lambda s: s["time"])
    available_slots = [s["time"] for s in slots if s["available"]]
    booked_slots = [s["time"] for s in slots if s["booked"]]

    if not available_slots:
        reason = (
            "All OPD appointment slots are booked or have already passed for this date."
            if slots else "No valid time slots configured for this schedule."
        )
        return {
            "found": True,
            "is_available": False,
            "has_schedules": True,
            "status": "Fully Booked" if booked_slots else "No Remaining Slots",
            "reason": reason,
            "slots": slots,
            "available_slots": [],
            "booked_slots": booked_slots,
            "total_slots": len(slots),
            "remaining_slots": 0,
            "schedule_windows": windows_info,
        }

    return {
        "found": True,
        "is_available": True,
        "has_schedules": True,
        "status": "Available",
        "reason": f"{len(available_slots)} of {len(slots)} OPD slots available on {iso_date}.",
        "slots": slots,
        "available_slots": available_slots,
        "booked_slots": booked_slots,
        "total_slots": len(slots),
        "remaining_slots": len(available_slots),
        "schedule_windows": windows_info,
    }


def get_hospital_beds_map(db):
    rows = db.execute(
        "SELECT * FROM ipd_beds ORDER BY hospital_id, CASE bed_type WHEN 'General' THEN 1 WHEN 'Private' THEN 2 ELSE 3 END"
    ).fetchall()
    mapping = {}
    for row in rows:
        item = dict(row)
        item["is_sample"] = bool(item["is_sample"])
        mapping.setdefault(row["hospital_id"], []).append(item)
    return mapping


def nearby_osm(lat, lon):
    query = f"""[out:json][timeout:8];
      (node["amenity"~"^(hospital|clinic)$"](around:15000,{lat},{lon});
       way["amenity"~"^(hospital|clinic)$"](around:15000,{lat},{lon});
       relation["amenity"~"^(hospital|clinic)$"](around:15000,{lat},{lon}););
      out center tags;"""
    url = "https://overpass-api.de/api/interpreter"
    result = http_json(url, "POST", {"data": query}, timeout=12)
    hospitals = []
    for item in result.get("elements", [])[:60]:
        tags = item.get("tags", {})
        coords = item.get("center", item)
        if not coords.get("lat") or not coords.get("lon") or not tags.get("name"):
            continue
        hospitals.append({
            "id": "osm-" + str(item["id"]),
            "name": tags["name"][:140],
            "address": ", ".join(filter(None, [
                tags.get("addr:street"), tags.get("addr:suburb"),
                tags.get("addr:city"), tags.get("addr:postcode"),
            ])) or "Address not listed in OpenStreetMap",
            "area": tags.get("addr:suburb", ""),
            "city": tags.get("addr:city", ""),
            "latitude": coords["lat"],
            "longitude": coords["lon"],
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "specialties": [],
            "facilities": [],
            "emergency_services": tags.get("emergency") == "yes",
            "opd_available": False,
            "ipd_available": False,
            "verified": False,
            "is_sample": False,
            "source": "OpenStreetMap community data",
            "rating": None,
            "rating_count": 0,
            "doctors": [],
            "beds": [],
            "osm_url": "https://www.openstreetmap.org/" + item["type"] + "/" + str(item["id"]),
        })
    return hospitals


def nearby_nominatim(lat, lon):
    radius = 0.18
    viewbox = f"{lon - radius},{lat + radius},{lon + radius},{lat - radius}"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": "hospital",
        "format": "jsonv2",
        "limit": 30,
        "viewbox": viewbox,
        "bounded": 1,
        "addressdetails": 1,
        "extratags": 1,
    })
    result = http_json(url)
    hospitals = []
    for item in result if isinstance(result, list) else []:
        category, kind = item.get("category") or item.get("class"), item.get("type")
        if category != "amenity" or kind not in {"hospital", "clinic"} or not item.get("name"):
            continue
        extratags = item.get("extratags") or {}
        latitude, longitude = valid_coords(item.get("lat"), item.get("lon")) or (None, None)
        if latitude is None or haversine_km(lat, lon, latitude, longitude) > 15:
            continue
        osm_type = item.get("osm_type", "")
        osm_url = f"https://www.openstreetmap.org/{osm_type}/{item.get('osm_id')}" if osm_type else ""
        hospitals.append({
            "id": f"osm-{osm_type}-{item.get('osm_id')}",
            "name": item["name"][:140],
            "address": item.get("display_name", "Address not listed in OpenStreetMap"),
            "area": "",
            "city": "",
            "latitude": latitude,
            "longitude": longitude,
            "phone": extratags.get("phone", ""),
            "specialties": [],
            "facilities": [],
            "emergency_services": extratags.get("emergency") == "yes",
            "opd_available": False,
            "ipd_available": False,
            "verified": False,
            "is_sample": False,
            "source": "OpenStreetMap Nominatim community data",
            "rating": None,
            "rating_count": 0,
            "doctors": [],
            "beds": [],
            "osm_url": osm_url,
        })
    return hospitals


def rank_hospitals(hospitals, lat, lon, specialty, emergency):
    for hospital in hospitals:
        distance = haversine_km(lat, lon, float(hospital["latitude"]), float(hospital["longitude"]))
        hospital["distance_km"] = round(distance, 1)
        doctors = hospital.get("doctors", [])
        available = [d for d in doctors if d.get("availability") == "Available"]
        matching = bool(specialty) and (
            any(
                specialty.lower() in str(value).lower() or str(value).lower() in specialty.lower()
                for value in hospital.get("specialties", [])
            )
            or any(specialty.lower() in d["specialty"].lower() for d in doctors)
        )
        distance_score = max(0, 30 * (1 - min(distance, 30) / 30))
        score = 25 * matching + distance_score + 20 * bool(available)
        score += 10 * bool(hospital.get("emergency_services") and emergency)
        score += 10 * bool(hospital.get("verified"))
        if hospital.get("rating") is not None and hospital.get("rating_count"):
            score += 10 * float(hospital["rating"]) / 5
        hospital["match_score"] = round(min(score, 100))
        reasons = []
        if matching:
            reasons.append("lists the suggested specialty")
        if available:
            reasons.append("has a provider marked available in its staff-updated directory")
        reasons.append(f"{distance:.1f} km straight-line distance")
        if emergency and hospital.get("emergency_services"):
            reasons.append("lists emergency services")
        if hospital.get("verified"):
            reasons.append("hospital profile is verified")
        if hospital.get("rating") is not None and hospital.get("rating_count"):
            reasons.append(
                f"{hospital['rating']}/5 from {hospital['rating_count']} visit-verified ratings"
            )
        hospital["recommendation_reasons"] = reasons
    return sorted(hospitals, key=lambda h: (-h["match_score"], h["distance_km"], h["name"].lower()))


def discover_hospitals(db, coords, specialty=None, emergency=False):
    lat, lon = coords
    beds_map = get_hospital_beds_map(db)
    local = [safe_hospital(row) for row in db.execute(
        "SELECT * FROM hospitals WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    ).fetchall() if haversine_km(lat, lon, row["latitude"], row["longitude"]) <= 15]
    for hospital in local:
        hospital["doctors"] = get_doctors(db, hospital["id"])
        hospital["beds"] = beds_map.get(hospital["id"], [])
        feedback = db.execute(
            """SELECT ROUND(AVG(rating),1) AS rating,COUNT(*) AS rating_count FROM feedback
               WHERE hospital_id=? AND verified_visit=1""", (hospital["id"],)
        ).fetchone()
        hospital["rating"] = feedback["rating"]
        hospital["rating_count"] = feedback["rating_count"]
    warning = None
    try:
        osm = nearby_osm(lat, lon)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        osm = []
        app.logger.warning("OpenStreetMap Overpass lookup failed: %s", exc)
    if not osm:
        try:
            osm = nearby_nominatim(lat, lon)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
            warning = "OpenStreetMap discovery is temporarily unavailable; showing saved platform listings only."
            app.logger.warning("OpenStreetMap Nominatim fallback failed: %s", exc)
    return rank_hospitals(local + osm, lat, lon, specialty, emergency), warning


@app.errorhandler(413)
def too_large(_error):
    return error("Upload exceeds the 5 MB limit.", 413)


@app.get("/")
def home():
    return send_file(ROOT / "index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "database": "sqlite"})


# ---------------------------------------------------------------------------
# Authentication & Session Routes (Preserved)
# ---------------------------------------------------------------------------

@app.post("/api/auth/register")
def register():
    data = body_json()
    name, email, password = (
        str(data.get("full_name", "")).strip(),
        str(data.get("email", "")).strip().lower(),
        data.get("password", ""),
    )
    phone = str(data.get("phone", "")).strip()
    if not 2 <= len(name) <= 100 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return error("Enter a valid name and email address.")
    if not re.fullmatch(r"[+()\d .-]{7,20}", phone) or sum(char.isdigit() for char in phone) < 7:
        return error("Enter a valid phone number.")
    if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
        return error("Password must contain 10–128 characters.")
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users(email,password_hash,full_name,phone,role,created_at) VALUES(?,?,?,?,?,?)",
            (email, generate_password_hash(password), name, phone, "patient", now_iso()),
        )
        db.execute(
            "INSERT OR IGNORE INTO patients(user_id, updated_at) VALUES(?,?)",
            (cur.lastrowid, now_iso()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return error("An account with that email already exists.", 409)
    session.clear()
    session["user_id"] = cur.lastrowid
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify({
        "user": {"id": cur.lastrowid, "email": email, "full_name": name, "phone": phone, "role": "patient"},
        "csrf_token": session["csrf_token"],
    }), 201


@app.post("/api/auth/login")
def login():
    data = body_json()
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    expected_role = data.get("expected_role")
    if not isinstance(expected_role, str) or expected_role not in {"patient", "hospital", "admin"}:
        return error("Choose a valid account type.")
    row = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if (not row or not row["active"] or row["role"] != expected_role
            or not isinstance(password, str) or not check_password_hash(row["password_hash"], password)):
        return error("Email or password is incorrect.", 401)
    session.clear()
    session["user_id"] = row["id"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    audit("Sign in")
    return jsonify({
        "user": {
            "id": row["id"], "email": row["email"], "full_name": row["full_name"],
            "phone": row["phone"], "role": row["role"],
        },
        "csrf_token": session["csrf_token"],
    })


@app.get("/api/me")
@login_required
def me():
    return jsonify({"user": dict(g.user), "csrf_token": session["csrf_token"]})


@app.post("/api/auth/logout")
@login_required
def logout():
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Location, Nearby Discovery & Symptom Recommendations (Preserved)
# ---------------------------------------------------------------------------

@app.get("/api/location/search")
@login_required
def location_search():
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return error("Enter a city or area name.")
    try:
        place = geocode(query)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return error("Location search is temporarily unavailable; try GPS or another search.", 502)
    if not place:
        return error("No matching location found.")
    return jsonify({
        "latitude": float(place["lat"]), "longitude": float(place["lon"]),
        "display_name": place.get("display_name", query),
        "source": "OpenStreetMap Nominatim",
    })


@app.post("/api/location/reverse")
@login_required
def reverse_location():
    data = body_json()
    coords = valid_coords(data.get("latitude"), data.get("longitude"))
    if not coords:
        return error("Valid latitude and longitude are required.")
    lat, lon = coords
    try:
        result = http_json("https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
            {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 16}
        ))
        address = result.get("address", {})
        area = address.get("suburb") or address.get("neighbourhood") or address.get("village") or ""
        city = address.get("city") or address.get("town") or address.get("county") or ""
        display = ", ".join(filter(None, [area, city])) or result.get("display_name", "")
        return jsonify({"latitude": lat, "longitude": lon, "area": area, "city": city,
                        "display_name": display, "source": "OpenStreetMap Nominatim"})
    except (urllib.error.URLError, TimeoutError, ValueError):
        return jsonify({"latitude": lat, "longitude": lon, "area": "", "city": "",
                        "display_name": f"{lat:.4f}, {lon:.4f} (place name unavailable)",
                        "source": "Browser GPS coordinates"})


@app.post("/api/recommendations")
@roles("patient")
def recommendations():
    data = body_json()
    symptoms = data.get("symptoms")
    coords = valid_coords(data.get("latitude"), data.get("longitude"))
    try:
        duration = int(data.get("duration_days"))
        severity = int(data.get("severity"))
    except (TypeError, ValueError):
        return error("Enter a valid symptom duration and severity.")
    allowed_symptoms = {
        "chest pain", "difficulty breathing", "shortness of breath", "severe bleeding",
        "unconscious", "seizure", "face drooping", "suicidal thoughts",
        "child health concern", "pregnancy concern",
        "fever", "cough", "sore throat", "stomach pain", "fatigue", "rash",
        "itching", "skin changes", "headache", "dizziness", "numbness",
        "joint pain", "back pain", "bone pain", "injury", "palpitations",
    }
    if not isinstance(symptoms, list) or not symptoms or any(
        not isinstance(symptom, str) or symptom not in allowed_symptoms for symptom in symptoms
    ):
        return error("Select at least one symptom from the list.")
    if not coords or not 1 <= duration <= 3650 or not 1 <= severity <= 5:
        return error("Location, duration (1–3650 days) and severity (1–5) are required.")
    specialty, explanation = specialist_for(symptoms)
    emergency = any(s in EMERGENCY_TERMS for s in symptoms)
    db = get_db()
    db.execute(
        """INSERT INTO symptoms(patient_id,symptoms_json,duration_days,severity,specialist,emergency,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (g.user["id"], json.dumps(symptoms), duration, severity, specialty, int(emergency), now_iso()),
    )
    db.commit()
    ranked, external_error = discover_hospitals(db, coords, specialty, emergency)
    return jsonify({
        "specialist": specialty, "explanation": explanation, "emergency": emergency,
        "emergency_message": (
            "A selected symptom may indicate an emergency. This tool cannot assess urgency. "
            "Call your local emergency number or go to the nearest emergency department now; do not wait for online recommendations."
        ) if emergency else "",
        "score_explanation": "Match score (0–100): specialty match 25, distance up to 25, staff-reported available provider 20, emergency services when relevant 10, verified profile 10, and visit-verified patient ratings up to 10. Straight-line distance is not driving distance; missing data earns no points.",
        "hospitals": ranked, "warning": external_error,
    })


@app.get("/api/hospitals/nearby")
@roles("patient")
def nearby_hospitals():
    coords = valid_coords(request.args.get("latitude"), request.args.get("longitude"))
    if not coords:
        return error("Valid latitude and longitude are required.")
    hospitals, warning = discover_hospitals(get_db(), coords)
    return jsonify({
        "specialist": None,
        "explanation": "No symptom information was shared. Results are ranked by distance and available verified listing data only.",
        "emergency": False,
        "score_explanation": "No specialist was selected; specialty matching is excluded. Score uses straight-line distance, staff-reported availability, listed emergency services, profile verification and visit-verified patient ratings. Unknown information earns no points.",
        "hospitals": hospitals,
        "warning": warning,
    })


@app.get("/api/hospitals/directory")
@roles("patient", "hospital", "admin")
def hospitals_directory():
    db = get_db()
    rows = db.execute("SELECT * FROM hospitals ORDER BY verified DESC, is_sample ASC, name ASC").fetchall()
    beds_map = get_hospital_beds_map(db)
    all_docs = get_doctors(db)
    docs_by_hosp = {}
    for doc in all_docs:
        docs_by_hosp.setdefault(doc["hospital_id"], []).append(doc)
    items = []
    for row in rows:
        h = safe_hospital(row)
        h["doctors"] = docs_by_hosp.get(h["id"], [])
        h["beds"] = beds_map.get(h["id"], [])
        h["total_available_beds"] = sum(b["available_beds"] for b in h["beds"])
        h["available_doctors_count"] = sum(1 for d in h["doctors"] if d["availability"] == "Available" and d["opd_available"])
        items.append(h)
    return jsonify({"hospitals": items})


# ---------------------------------------------------------------------------
# Disease-Based Doctor Recommendation (Requirement 2)
# ---------------------------------------------------------------------------

def match_disease_records(db, query_text):
    q = str(query_text or "").strip().lower()
    all_diseases = [dict(r) for r in db.execute("SELECT * FROM diseases ORDER BY name").fetchall()]
    if not q:
        return all_diseases
    scored = []
    tokens = [tok for tok in re.split(r"[\s,;/&+-]+", q) if len(tok) >= 2]
    for d in all_diseases:
        name_l = d["name"].lower()
        spec_l = d["recommended_specialist"].lower()
        dept_l = d["department"].lower()
        cat_l = d["category"].lower()
        kw_list = [k.strip().lower() for k in d["keywords"].split(",") if k.strip()]
        score = 0
        if q == name_l or q == spec_l or q == dept_l:
            score += 100
        elif q in name_l or name_l in q:
            score += 75
        if any(q == kw for kw in kw_list):
            score += 90
        elif any(q in kw or kw in q for kw in kw_list):
            score += 65
        if q in spec_l or q in dept_l or q in cat_l:
            score += 55
        for tok in tokens:
            if tok in name_l or tok in spec_l or tok in dept_l:
                score += 20
            elif any(tok in kw for kw in kw_list):
                score += 15
        if score > 0:
            scored.append((score, d))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [pair[1] for pair in scored]


@app.get("/api/diseases")
@roles("patient", "hospital", "admin")
def list_diseases():
    q = request.args.get("q", "").strip()
    matches = match_disease_records(get_db(), q) if q else [
        dict(r) for r in get_db().execute("SELECT * FROM diseases ORDER BY name").fetchall()
    ]
    return jsonify({"diseases": matches})


@app.get("/api/diseases/recommend")
@roles("patient", "hospital", "admin")
def recommend_by_disease():
    db = get_db()
    q = request.args.get("q", "").strip()
    city = request.args.get("city", "").strip().lower()
    hospital_id = request.args.get("hospital_id", "").strip()
    date_str = request.args.get("date", "").strip()
    if len(q) < 2:
        return error("Enter a disease or health concern (at least 2 characters).")

    target_date = None
    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            return error("Enter a valid date in YYYY-MM-DD format.")

    matched_diseases = match_disease_records(db, q)
    if matched_diseases:
        primary = matched_diseases[0]
        target_specialists = list(dict.fromkeys([d["recommended_specialist"] for d in matched_diseases[:3]]))
        target_departments = list(dict.fromkeys([d["department"] for d in matched_diseases[:3]]))
    else:
        primary = {
            "name": q.title(),
            "category": "General Consultation",
            "recommended_specialist": "General Physician",
            "department": "General Medicine",
            "guidance_note": "No exact disease rule matched; a General Physician is suggested as an initial consultation point.",
            "is_emergency_risk": 0,
        }
        target_specialists = ["General Physician"]
        target_departments = ["General Medicine"]

    all_docs = get_doctors(db)
    recommended_doctors = []
    for doc in all_docs:
        if hospital_id and hospital_id.isdigit() and doc["hospital_id"] != int(hospital_id):
            continue
        if city and city not in f"{doc.get('hospital_city', '')} {doc.get('hospital_area', '')} {doc.get('hospital_address', '')}".lower():
            continue
        doc_specs = [doc["specialty"].lower(), doc["department"].lower()] + [s.lower() for s in doc.get("specializations", [])]
        matched_spec = any(
            ts.lower() in ds or ds in ts.lower()
            for ts in target_specialists + target_departments
            for ds in doc_specs if ds
        )
        if not matched_spec:
            continue

        slot_info = None
        if target_date:
            slot_info = compute_doctor_date_slots(db, doc["hospital_id"], doc["id"], target_date)

        recommended_doctors.append({
            **doc,
            "disease_name": primary["name"],
            "recommended_specialist": primary["recommended_specialist"],
            "recommended_department": primary["department"],
            "date_checked": target_date.isoformat() if target_date else None,
            "date_available": slot_info["is_available"] if slot_info else None,
            "remaining_slots": slot_info["remaining_slots"] if slot_info else None,
            "availability_reason": slot_info["reason"] if slot_info else None,
        })

    recommended_doctors.sort(key=lambda d: (
        0 if (d["date_available"] if target_date else d["availability"] == "Available") else 1,
        0 if d["opd_available"] else 1,
        d["consultation_fee"],
        d["name"].lower(),
    ))

    return jsonify({
        "query": q,
        "disease": primary,
        "matched_diseases": matched_diseases[:5],
        "recommended_specialist": primary["recommended_specialist"],
        "recommended_department": primary["department"],
        "emergency_warning": bool(primary.get("is_emergency_risk")),
        "disclaimer": (
            "Informational guidance only — not a confirmed medical diagnosis. "
            "Always consult a licensed doctor for diagnosis and treatment. "
            "For urgent or severe symptoms, contact emergency services immediately."
        ),
        "doctors": recommended_doctors,
    })


# ---------------------------------------------------------------------------
# OPD Services & Hospital-Specific Doctor Availability (Requirements 1 & 3)
# ---------------------------------------------------------------------------

@app.get("/api/opd/services")
@roles("patient", "hospital", "admin")
def opd_services():
    db = get_db()
    hospital_id = request.args.get("hospital_id", "").strip()
    city = request.args.get("city", "").strip().lower()
    disease = request.args.get("disease", "").strip()
    specialization = request.args.get("specialization", "").strip().lower()
    doctor_name = request.args.get("doctor_name", "").strip().lower()
    available_only = request.args.get("available_only", "0") == "1"
    date_str = request.args.get("date", "").strip() or date.today().isoformat()

    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        return error("Choose a valid date (YYYY-MM-DD).")

    disease_specialists = []
    matched_disease = None
    if disease:
        matches = match_disease_records(db, disease)
        if matches:
            matched_disease = matches[0]
            disease_specialists = [m["recommended_specialist"].lower() for m in matches[:2]] + [
                m["department"].lower() for m in matches[:2]
            ]

    docs = get_doctors(db, int(hospital_id) if hospital_id.isdigit() else None)
    items = []
    for doc in docs:
        if city and city not in f"{doc.get('hospital_city', '')} {doc.get('hospital_area', '')} {doc.get('hospital_name', '')}".lower():
            continue
        if doctor_name and doctor_name not in doc["name"].lower():
            continue
        spec_haystack = " ".join([doc["specialty"], doc["department"]] + doc.get("specializations", [])).lower()
        if specialization and specialization not in spec_haystack:
            continue
        if disease:
            if disease_specialists:
                if not any(ds in spec_haystack for ds in disease_specialists):
                    continue
            elif disease.lower() not in spec_haystack:
                continue

        slot_info = compute_doctor_date_slots(db, doc["hospital_id"], doc["id"], target_date)
        if available_only and not slot_info["is_available"]:
            continue

        items.append({
            **doc,
            "date": target_date.isoformat(),
            "weekday": WEEKDAY_NAMES[target_date.weekday()],
            "is_available_on_date": slot_info["is_available"],
            "date_status": slot_info["status"],
            "availability_reason": slot_info["reason"],
            "remaining_slots": slot_info["remaining_slots"],
            "total_slots": slot_info["total_slots"],
            "available_slots": slot_info["available_slots"],
            "slots": slot_info["slots"],
        })

    items.sort(key=lambda d: (0 if d["is_available_on_date"] else 1, -d["remaining_slots"], d["hospital_name"], d["name"]))
    return jsonify({
        "date": target_date.isoformat(),
        "matched_disease": matched_disease,
        "services": items,
    })


@app.get("/api/availability/check")
@roles("patient", "hospital", "admin")
def check_hospital_doctor_availability():
    """
    Hospital-specific doctor availability check:
    Checks a particular hospital, particular doctor (or disease/department filter), and particular date.
    Never marks a doctor available if unavailable, unscheduled on that date, or out of slots.
    """
    db = get_db()
    hospital_id_raw = request.args.get("hospital_id", "").strip()
    doctor_id_raw = request.args.get("doctor_id", "").strip()
    disease = request.args.get("disease", "").strip()
    department = request.args.get("department", "").strip().lower()
    date_str = request.args.get("date", "").strip()

    if not hospital_id_raw.isdigit():
        return error("Select a hospital to check availability.")
    if not date_str:
        return error("Select a date to check availability.")
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        return error("Choose a valid date in YYYY-MM-DD format.")

    hospital_id = int(hospital_id_raw)
    hospital_row = db.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not hospital_row:
        return error("Hospital not found.", 404)
    hospital = safe_hospital(hospital_row)

    matched_disease = None
    disease_terms = []
    if disease:
        matches = match_disease_records(db, disease)
        if matches:
            matched_disease = matches[0]
            disease_terms = [
                matched_disease["recommended_specialist"].lower(),
                matched_disease["department"].lower(),
            ]

    docs = get_doctors(db, hospital_id)
    if doctor_id_raw.isdigit():
        target_doc_id = int(doctor_id_raw)
        docs = [d for d in docs if d["id"] == target_doc_id]
        if not docs:
            return jsonify({
                "hospital": hospital,
                "date": target_date.isoformat(),
                "weekday": WEEKDAY_NAMES[target_date.weekday()],
                "matched_disease": matched_disease,
                "results": [],
                "message": "The selected doctor is not assigned to this hospital.",
            })
    else:
        if department:
            docs = [
                d for d in docs
                if department in f"{d['department']} {d['specialty']} {' '.join(d.get('specializations', []))}".lower()
            ]
        if disease:
            filtered = []
            for d in docs:
                hay = f"{d['department']} {d['specialty']} {' '.join(d.get('specializations', []))}".lower()
                if disease_terms:
                    if any(t in hay for t in disease_terms):
                        filtered.append(d)
                elif disease.lower() in hay:
                    filtered.append(d)
            docs = filtered

    results = []
    for d in docs:
        slot_info = compute_doctor_date_slots(db, hospital_id, d["id"], target_date)
        results.append({
            **d,
            "date": target_date.isoformat(),
            "weekday": WEEKDAY_NAMES[target_date.weekday()],
            "is_available": slot_info["is_available"],
            "date_status": slot_info["status"],
            "reason": slot_info["reason"],
            "slots": slot_info["slots"],
            "available_slots": slot_info["available_slots"],
            "booked_slots": slot_info["booked_slots"],
            "total_slots": slot_info["total_slots"],
            "remaining_slots": slot_info["remaining_slots"],
            "schedule_windows": slot_info["schedule_windows"],
        })

    return jsonify({
        "hospital": hospital,
        "date": target_date.isoformat(),
        "weekday": WEEKDAY_NAMES[target_date.weekday()],
        "matched_disease": matched_disease,
        "results": results,
    })


# ---------------------------------------------------------------------------
# IPD Services — Beds & Admission Requests (Requirement 1)
# ---------------------------------------------------------------------------

@app.get("/api/ipd/beds")
@roles("patient", "hospital", "admin")
def list_ipd_beds():
    db = get_db()
    hospital_id = request.args.get("hospital_id", "").strip()
    city = request.args.get("city", "").strip().lower()
    bed_type = request.args.get("bed_type", "").strip()
    available_only = request.args.get("available_only", "0") == "1"

    query = "SELECT * FROM hospitals WHERE 1=1"
    params = []
    if hospital_id.isdigit():
        query += " AND id=?"
        params.append(int(hospital_id))
    query += " ORDER BY verified DESC, is_sample ASC, name ASC"
    hospitals = [safe_hospital(r) for r in db.execute(query, params).fetchall()]
    beds_map = get_hospital_beds_map(db)

    items = []
    for h in hospitals:
        if city and city not in f"{h['city']} {h['area']} {h['address']} {h['name']}".lower():
            continue
        beds = beds_map.get(h["id"], [])
        if bed_type in BED_TYPES:
            beds = [b for b in beds if b["bed_type"] == bed_type]
        if available_only and not any(b["available_beds"] > 0 for b in beds):
            continue
        by_type = {b["bed_type"]: b for b in beds}
        items.append({
            "hospital_id": h["id"],
            "hospital_name": h["name"],
            "city": h["city"],
            "area": h["area"],
            "address": h["address"],
            "phone": h["phone"],
            "verified": h["verified"],
            "is_sample": h["is_sample"],
            "ipd_available": h["ipd_available"],
            "emergency_services": h["emergency_services"],
            "beds": beds,
            "summary": {
                "General": by_type.get("General"),
                "Private": by_type.get("Private"),
                "ICU": by_type.get("ICU"),
                "total_beds": sum(b["total_beds"] for b in beds),
                "available_beds": sum(b["available_beds"] for b in beds),
            },
        })

    return jsonify({"hospitals": items})


@app.post("/api/ipd/admissions")
@roles("patient")
def create_admission_request():
    data = body_json()
    try:
        hospital_id = int(data.get("hospital_id"))
    except (TypeError, ValueError):
        return error("Select a valid hospital for admission.")
    bed_type = str(data.get("bed_type", "")).strip()
    if bed_type not in BED_TYPES:
        return error("Select a valid bed type: General, Private, or ICU.")
    reason = str(data.get("disease_or_reason", "")).strip()
    if not 3 <= len(reason) <= 300:
        return error("Enter the disease or admission reason (3–300 characters).")
    try:
        pref_date = date.fromisoformat(str(data.get("preferred_date", "")))
    except ValueError:
        return error("Choose a valid preferred admission date.")
    if pref_date < date.today() or pref_date > date.today() + timedelta(days=90):
        return error("Preferred admission date must be between today and the next 90 days.")

    doctor_id = data.get("doctor_id")
    if doctor_id not in (None, ""):
        try:
            doctor_id = int(doctor_id)
        except (TypeError, ValueError):
            return error("Invalid doctor selection.")
    else:
        doctor_id = None

    db = get_db()
    hospital = db.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not hospital:
        return error("Hospital not found.", 404)
    if not hospital["verified"] and not hospital["is_sample"]:
        return error("This hospital is awaiting verification and cannot accept IPD admission requests.")
    if not hospital["ipd_available"]:
        return error("This hospital does not currently list IPD admission services.")

    bed_row = db.execute(
        "SELECT * FROM ipd_beds WHERE hospital_id=? AND bed_type=?", (hospital_id, bed_type)
    ).fetchone()
    if not bed_row or bed_row["available_beds"] <= 0:
        return error(f"No {bed_type} beds are currently available at {hospital['name']}.", 409)

    if doctor_id is not None:
        if not db.execute(
            "SELECT 1 FROM hospital_doctors WHERE hospital_id=? AND doctor_id=? AND is_active=1",
            (hospital_id, doctor_id),
        ).fetchone():
            return error("Selected doctor is not assigned to this hospital.", 400)

    request_code = generate_code("ADM", pref_date)
    now = now_iso()
    phone = str(data.get("patient_phone", "")).strip()[:30] or g.user["phone"]
    emergency_contact = str(data.get("emergency_contact", "")).strip()[:60]
    notes = str(data.get("notes", "")).strip()[:600]

    cur = db.execute(
        """INSERT INTO admission_requests
           (request_code, patient_id, hospital_id, doctor_id, bed_type, disease_or_reason,
            preferred_date, patient_phone, emergency_contact, notes, status, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,'Pending',?,?)""",
        (request_code, g.user["id"], hospital_id, doctor_id, bed_type, reason,
         pref_date.isoformat(), phone, emergency_contact, notes, now, now),
    )
    if hospital["owner_id"]:
        notify(hospital["owner_id"], f"New IPD {bed_type} admission request ({request_code}) from {g.user['full_name']}.")
    notify_admins(db, f"New IPD admission request {request_code} for {hospital['name']} ({bed_type}).")
    notify(g.user["id"], f"Admission request {request_code} submitted to {hospital['name']} ({bed_type} bed).")
    db.commit()
    audit(f"IPD admission requested ({request_code})")
    return jsonify({
        "id": cur.lastrowid,
        "request_code": request_code,
        "status": "Pending",
        "message": f"Admission request {request_code} submitted and pending hospital/admin review.",
    }), 201


@app.get("/api/ipd/admissions")
@roles("patient", "hospital", "admin")
def list_admission_requests():
    db = get_db()
    base = """SELECT r.*, u.full_name AS patient_name, u.email AS patient_email,
                     h.name AS hospital_name, h.city AS hospital_city, h.is_sample AS hospital_is_sample,
                     d.name AS doctor_name, d.specialty AS doctor_specialty
              FROM admission_requests r
              JOIN users u ON u.id = r.patient_id
              JOIN hospitals h ON h.id = r.hospital_id
              LEFT JOIN doctors d ON d.id = r.doctor_id"""
    if g.user["role"] == "patient":
        rows = db.execute(base + " WHERE r.patient_id=? ORDER BY r.id DESC", (g.user["id"],)).fetchall()
    elif g.user["role"] == "hospital":
        hospital = owned_hospital()
        if not hospital:
            return jsonify({"admissions": []})
        rows = db.execute(base + " WHERE r.hospital_id=? ORDER BY r.id DESC", (hospital["id"],)).fetchall()
    else:
        rows = db.execute(base + " ORDER BY r.id DESC LIMIT 200").fetchall()
    return jsonify({"admissions": [dict(r) for r in rows]})


@app.patch("/api/ipd/admissions/<int:request_id>")
@roles("patient", "hospital", "admin")
def update_admission_request(request_id):
    data = body_json()
    new_status = str(data.get("status", "")).strip()
    admin_note = str(data.get("admin_note", "")).strip()[:500]
    db = get_db()
    req_row = db.execute(
        """SELECT r.*, h.name AS hospital_name, h.owner_id
           FROM admission_requests r JOIN hospitals h ON h.id=r.hospital_id
           WHERE r.id=?""",
        (request_id,),
    ).fetchone()
    if not req_row:
        return error("Admission request not found.", 404)

    if g.user["role"] == "patient":
        if req_row["patient_id"] != g.user["id"]:
            return error("You do not have permission to update this request.", 403)
        if new_status != "Cancelled" or req_row["status"] != "Pending":
            return error("Patients can only cancel their own Pending admission requests.")
    else:
        if g.user["role"] == "hospital":
            hosp = owned_hospital()
            if not hosp or hosp["id"] != req_row["hospital_id"]:
                return error("You do not have permission to manage this hospital's admissions.", 403)
        if new_status not in {"Approved", "Rejected", "Discharged", "Cancelled"}:
            return error("Choose Approved, Rejected, Discharged, or Cancelled.")

    old_status = req_row["status"]
    if old_status == new_status:
        return jsonify({"ok": True, "status": new_status})

    # Atomic bed count updates when approving or discharging/cancelling an approved bed
    if new_status == "Approved":
        if old_status != "Pending":
            return error("Only Pending admission requests can be approved.")
        cur = db.execute(
            """UPDATE ipd_beds SET available_beds = available_beds - 1, updated_at=?, updated_by=?
               WHERE hospital_id=? AND bed_type=? AND available_beds > 0""",
            (now_iso(), g.user["id"], req_row["hospital_id"], req_row["bed_type"]),
        )
        if cur.rowcount == 0:
            return error(f"Cannot approve: no available {req_row['bed_type']} beds left at {req_row['hospital_name']}.", 409)
    elif old_status == "Approved" and new_status in {"Discharged", "Cancelled"}:
        db.execute(
            """UPDATE ipd_beds
               SET available_beds = MIN(total_beds, available_beds + 1), updated_at=?, updated_by=?
               WHERE hospital_id=? AND bed_type=?""",
            (now_iso(), g.user["id"], req_row["hospital_id"], req_row["bed_type"]),
        )
    elif old_status in {"Rejected", "Discharged", "Cancelled"}:
        return error(f"Admission request is already {old_status} and cannot be changed.")

    db.execute(
        """UPDATE admission_requests SET status=?, admin_note=?, reviewed_by=?, updated_at=? WHERE id=?""",
        (new_status, admin_note or req_row["admin_note"], g.user["id"], now_iso(), request_id),
    )
    notify(
        req_row["patient_id"],
        f"IPD admission request {req_row['request_code']} at {req_row['hospital_name']} is now {new_status}."
        + (f" Note: {admin_note}" if admin_note else ""),
    )
    db.commit()
    audit(f"IPD admission {req_row['request_code']} marked {new_status}")
    return jsonify({"ok": True, "status": new_status, "request_code": req_row["request_code"]})


# ---------------------------------------------------------------------------
# Smart Search & Multi-Faceted Filters (Requirement 6)
# ---------------------------------------------------------------------------

@app.get("/api/search")
@roles("patient", "hospital", "admin")
def smart_search():
    db = get_db()
    hospital_name = request.args.get("hospital_name", "").strip().lower()
    location = request.args.get("location", "").strip().lower()
    disease = request.args.get("disease", "").strip()
    doctor_name = request.args.get("doctor_name", "").strip().lower()
    specialization = request.args.get("specialization", "").strip().lower()
    opd_only = request.args.get("opd_only", "0") == "1"
    ipd_only = request.args.get("ipd_only", "0") == "1"
    bed_type = request.args.get("bed_type", "").strip()
    date_str = request.args.get("date", "").strip()

    target_date = None
    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            return error("Enter a valid appointment date (YYYY-MM-DD).")

    matched_disease = None
    disease_terms = []
    if disease:
        matches = match_disease_records(db, disease)
        if matches:
            matched_disease = matches[0]
            disease_terms = [
                matched_disease["recommended_specialist"].lower(),
                matched_disease["department"].lower(),
            ]

    all_hospitals = [safe_hospital(r) for r in db.execute(
        "SELECT * FROM hospitals ORDER BY verified DESC, is_sample ASC, name ASC"
    ).fetchall()]
    all_docs = get_doctors(db)
    beds_map = get_hospital_beds_map(db)

    docs_by_hosp = {}
    for doc in all_docs:
        docs_by_hosp.setdefault(doc["hospital_id"], []).append(doc)

    filtered_doctors = []
    for doc in all_docs:
        if hospital_name and hospital_name not in doc["hospital_name"].lower():
            continue
        loc_hay = f"{doc.get('hospital_city', '')} {doc.get('hospital_area', '')} {doc.get('hospital_address', '')}".lower()
        if location and location not in loc_hay:
            continue
        if doctor_name and doctor_name not in doc["name"].lower():
            continue
        spec_hay = f"{doc['specialty']} {doc['department']} {' '.join(doc.get('specializations', []))}".lower()
        if specialization and specialization not in spec_hay:
            continue
        if disease:
            if disease_terms:
                if not any(t in spec_hay for t in disease_terms):
                    continue
            elif disease.lower() not in spec_hay:
                continue
        if opd_only and not (doc["opd_available"] and doc["availability"] == "Available"):
            continue
        hosp_beds = beds_map.get(doc["hospital_id"], [])
        if ipd_only and not any(b["available_beds"] > 0 for b in hosp_beds):
            continue
        if bed_type in BED_TYPES and not any(b["bed_type"] == bed_type and b["available_beds"] > 0 for b in hosp_beds):
            continue

        slot_info = compute_doctor_date_slots(db, doc["hospital_id"], doc["id"], target_date) if target_date else None
        if target_date and opd_only and not slot_info["is_available"]:
            continue

        filtered_doctors.append({
            **doc,
            "date": target_date.isoformat() if target_date else None,
            "is_available_on_date": slot_info["is_available"] if slot_info else None,
            "remaining_slots": slot_info["remaining_slots"] if slot_info else None,
            "availability_reason": slot_info["reason"] if slot_info else None,
        })

    filtered_hospitals = []
    matching_doctor_hosp_ids = {d["hospital_id"] for d in filtered_doctors}
    has_doctor_criteria = bool(doctor_name or specialization or disease)

    for h in all_hospitals:
        if hospital_name and hospital_name not in h["name"].lower():
            continue
        loc_hay = f"{h['city']} {h['area']} {h['address']}".lower()
        if location and location not in loc_hay:
            continue
        h_beds = beds_map.get(h["id"], [])
        h_docs = docs_by_hosp.get(h["id"], [])
        if opd_only and not (h["opd_available"] and any(d["availability"] == "Available" and d["opd_available"] for d in h_docs)):
            continue
        if ipd_only and not any(b["available_beds"] > 0 for b in h_beds):
            continue
        if bed_type in BED_TYPES and not any(b["bed_type"] == bed_type and b["available_beds"] > 0 for b in h_beds):
            continue
        if has_doctor_criteria:
            spec_hay = " ".join(h["specialties"]).lower()
            spec_match = (
                (h["id"] in matching_doctor_hosp_ids)
                or (specialization and specialization in spec_hay)
                or (disease and disease_terms and any(t in spec_hay for t in disease_terms))
            )
            if not spec_match:
                continue

        filtered_hospitals.append({
            **h,
            "doctors": h_docs,
            "beds": h_beds,
            "total_available_beds": sum(b["available_beds"] for b in h_beds),
            "available_doctors_count": sum(1 for d in h_docs if d["availability"] == "Available" and d["opd_available"]),
        })

    return jsonify({
        "matched_disease": matched_disease,
        "date": target_date.isoformat() if target_date else None,
        "hospitals": filtered_hospitals,
        "doctors": filtered_doctors,
    })


# ---------------------------------------------------------------------------
# Doctors Polling & Appointment Booking System (Requirement 4 + Existing)
# ---------------------------------------------------------------------------

@app.get("/api/doctors")
@roles("patient", "hospital", "admin")
def doctors():
    hospital_id = request.args.get("hospital_id")
    if hospital_id and not str(hospital_id).isdigit():
        return error("Invalid hospital ID.")
    return jsonify({
        "doctors": get_doctors(get_db(), int(hospital_id) if hospital_id else None),
        "updated_at": now_iso(),
    })


@app.get("/api/doctors/updates")
@roles("patient", "hospital", "admin")
def doctor_updates():
    since = request.args.get("since", "")
    if len(since) > 40:
        return error("Invalid update timestamp.")
    rows = get_db().execute(
        """SELECT d.id, hd.hospital_id, d.name, d.specialty,
                  COALESCE(hd.availability_status, a.status, 'Unavailable') AS status,
                  COALESCE(hd.updated_at, a.updated_at) AS updated_at,
                  h.name AS hospital_name
           FROM hospital_doctors hd
           JOIN doctors d ON d.id = hd.doctor_id
           LEFT JOIN doctor_availability a ON a.doctor_id = d.id
           JOIN hospitals h ON h.id = hd.hospital_id
           WHERE COALESCE(hd.updated_at, a.updated_at) > ?
           ORDER BY COALESCE(hd.updated_at, a.updated_at) DESC LIMIT 200""",
        (since,),
    ).fetchall()
    return jsonify({"updates": [dict(row) for row in rows], "server_time": now_iso()})


@app.post("/api/appointments")
@roles("patient")
def book_appointment():
    data = body_json()
    try:
        hospital_id, doctor_id = int(data.get("hospital_id")), int(data.get("doctor_id"))
        appt_date = date.fromisoformat(str(data.get("date", "")))
        appt_time = datetime.strptime(str(data.get("time", "")), "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError):
        return error("Choose a valid hospital, doctor, date and time.")
    if appt_date < date.today() or appt_date > date.today() + timedelta(days=180):
        return error("Choose a date between today and the next 180 days.")
    if appt_date == date.today() and appt_time <= datetime.now().strftime("%H:%M"):
        return error("Choose a future time for today's appointment.")

    db = get_db()
    doctor = db.execute(
        """SELECT d.id, hd.hospital_id, d.name, d.specialty,
                  COALESCE(NULLIF(hd.department, ''), NULLIF(d.department, ''), d.specialty) AS department,
                  CASE WHEN hd.consultation_fee > 0 THEN hd.consultation_fee ELSE d.consultation_fee END AS consultation_fee,
                  hd.opd_available,
                  COALESCE(hd.availability_status, a.status, 'Unavailable') AS availability,
                  h.name AS hospital_name, h.verified AS hospital_verified,
                  h.is_sample AS hospital_is_sample, h.owner_id
           FROM hospital_doctors hd
           JOIN doctors d ON d.id = hd.doctor_id
           JOIN hospitals h ON h.id = hd.hospital_id
           LEFT JOIN doctor_availability a ON a.doctor_id = d.id
           WHERE hd.doctor_id=? AND hd.hospital_id=? AND hd.is_active=1""",
        (doctor_id, hospital_id),
    ).fetchone()
    if not doctor:
        return error("That doctor is not listed at the selected hospital.", 404)
    if not doctor["hospital_verified"] and not doctor["hospital_is_sample"]:
        return error("This hospital profile is awaiting verification and cannot accept platform bookings.")
    if not doctor["opd_available"]:
        return error("OPD booking is currently closed for this doctor at the selected hospital.")
    if doctor["availability"] != "Available":
        return error("This doctor is not currently marked Available at the selected hospital.")

    # Strict hospital-specific date & slot validation
    slot_check = compute_doctor_date_slots(db, hospital_id, doctor_id, appt_date)
    if slot_check["has_schedules"]:
        if not slot_check["is_available"]:
            return error(slot_check["reason"], 409)
        if appt_time not in slot_check["available_slots"]:
            if appt_time in slot_check["booked_slots"]:
                return error("That time slot has already been booked. Choose another available slot.", 409)
            return error("The chosen time is outside this doctor's available OPD slots for the selected date.", 400)

    disease_name = str(data.get("disease_name", "")).strip()[:120]
    department = str(data.get("department", "")).strip()[:100] or doctor["department"]
    appointment_type = "OPD"
    appointment_code = generate_code("CF", appt_date)

    try:
        cur = db.execute(
            """INSERT INTO appointments
               (appointment_code, patient_id, hospital_id, doctor_id, disease_name, department,
                appointment_type, consultation_fee, appointment_date, appointment_time, status, note, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                appointment_code, g.user["id"], hospital_id, doctor_id, disease_name, department,
                appointment_type, int(doctor["consultation_fee"] or 0), appt_date.isoformat(),
                appt_time, "pending", str(data.get("note", ""))[:500], now_iso(),
            ),
        )
        if doctor["owner_id"]:
            notify(
                doctor["owner_id"],
                f"New OPD appointment {appointment_code} from {g.user['full_name']} with {doctor['name']} on {appt_date.isoformat()} at {appt_time}.",
            )
        notify(
            g.user["id"],
            f"Appointment {appointment_code} requested with {doctor['name']} at {doctor['hospital_name']} on {appt_date.isoformat()} at {appt_time}.",
        )
        db.commit()
    except sqlite3.IntegrityError:
        return error("That time slot has already been booked. Choose another time.", 409)

    audit(f"Appointment requested ({appointment_code})")
    return jsonify({
        "id": cur.lastrowid,
        "appointment_code": appointment_code,
        "status": "pending",
        "appointment": {
            "id": cur.lastrowid,
            "appointment_code": appointment_code,
            "hospital_id": hospital_id,
            "hospital_name": doctor["hospital_name"],
            "doctor_id": doctor_id,
            "doctor_name": doctor["name"],
            "specialty": doctor["specialty"],
            "department": department,
            "disease_name": disease_name,
            "consultation_fee": int(doctor["consultation_fee"] or 0),
            "appointment_date": appt_date.isoformat(),
            "appointment_time": appt_time,
            "status": "pending",
        },
        "message": f"Appointment {appointment_code} booked (Status: Pending confirmation).",
    }), 201


@app.get("/api/appointments")
@roles("patient")
def patient_appointments():
    db = get_db()
    rows = db.execute(
        """SELECT a.*, d.name AS doctor_name, d.specialty, d.consultation_timings,
                  h.name AS hospital_name, h.address AS hospital_address, h.city AS hospital_city,
                  h.is_sample AS hospital_is_sample
           FROM appointments a
           JOIN doctors d ON d.id = a.doctor_id
           JOIN hospitals h ON h.id = a.hospital_id
           WHERE a.patient_id=?
           ORDER BY a.appointment_date DESC, a.appointment_time DESC, a.id DESC""",
        (g.user["id"],),
    ).fetchall()
    reminder_cutoff = datetime.now() + timedelta(hours=24)
    for row in rows:
        if row["status"] not in {"confirmed", "accepted", "rescheduled"} or row["reminder_sent_at"]:
            continue
        try:
            appointment_at = datetime.fromisoformat(f"{row['appointment_date']}T{row['appointment_time']}")
        except ValueError:
            continue
        if datetime.now() <= appointment_at <= reminder_cutoff:
            db.execute(
                "UPDATE appointments SET reminder_sent_at=? WHERE id=? AND reminder_sent_at IS NULL",
                (now_iso(), row["id"]),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                notify(
                    g.user["id"],
                    f"Reminder: appointment {row['appointment_code']} with {row['doctor_name']} at {row['hospital_name']} is within 24 hours.",
                )
    db.commit()
    return jsonify({"appointments": [dict(row) for row in rows]})


@app.get("/api/appointments/<int:appointment_id>")
@roles("patient", "hospital", "admin")
def appointment_detail(appointment_id):
    db = get_db()
    row = db.execute(
        """SELECT a.*, u.full_name AS patient_name, u.email AS patient_email, u.phone AS patient_phone,
                  d.name AS doctor_name, d.specialty, d.qualification, d.consultation_timings,
                  h.name AS hospital_name, h.address AS hospital_address, h.city AS hospital_city,
                  h.phone AS hospital_phone, h.owner_id
           FROM appointments a
           JOIN users u ON u.id = a.patient_id
           JOIN doctors d ON d.id = a.doctor_id
           JOIN hospitals h ON h.id = a.hospital_id
           WHERE a.id=?""",
        (appointment_id,),
    ).fetchone()
    if not row:
        return error("Appointment not found.", 404)
    if g.user["role"] == "patient" and row["patient_id"] != g.user["id"]:
        return error("You do not have permission to view this appointment.", 403)
    if g.user["role"] == "hospital" and row["owner_id"] != g.user["id"]:
        return error("You do not have permission to view this appointment.", 403)
    result = dict(row)
    result.pop("owner_id", None)
    return jsonify({"appointment": result})


@app.patch("/api/appointments/<int:appointment_id>")
@roles("patient")
def cancel_appointment(appointment_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM appointments WHERE id=? AND patient_id=?",
        (appointment_id, g.user["id"]),
    ).fetchone()
    if not row:
        return error("Appointment not found.", 404)
    if row["status"] not in set(ACTIVE_APPOINTMENT_STATES):
        return error("This appointment can no longer be cancelled.")
    db.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appointment_id,))
    owner = db.execute("SELECT owner_id FROM hospitals WHERE id=?", (row["hospital_id"],)).fetchone()
    if owner and owner["owner_id"]:
        notify(owner["owner_id"], f"Appointment {row['appointment_code'] or f'#{appointment_id}'} was cancelled by the patient.")
    db.commit()
    audit(f"Appointment cancelled ({row['appointment_code'] or appointment_id})")
    return jsonify({"ok": True, "status": "cancelled", "appointment_code": row["appointment_code"]})


# ---------------------------------------------------------------------------
# Patient Profile, Medical History, Prescriptions, Favourites, Feedback, Reports (Preserved)
# ---------------------------------------------------------------------------

@app.get("/api/profile")
@roles("patient")
def profile():
    db = get_db()
    patient_meta = db.execute("SELECT * FROM patients WHERE user_id=?", (g.user["id"],)).fetchone()
    history = db.execute(
        "SELECT * FROM medical_history WHERE patient_id=? ORDER BY id DESC", (g.user["id"],)
    ).fetchall()
    favs = db.execute(
        """SELECT h.* FROM favourites f JOIN hospitals h ON h.id=f.hospital_id
           WHERE f.patient_id=? ORDER BY f.created_at DESC""",
        (g.user["id"],),
    ).fetchall()
    prescriptions = db.execute(
        "SELECT id,original_name,uploaded_at FROM prescriptions WHERE patient_id=? ORDER BY id DESC",
        (g.user["id"],),
    ).fetchall()
    notifications = db.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30", (g.user["id"],)
    ).fetchall()
    admissions = db.execute(
        """SELECT r.*, h.name AS hospital_name FROM admission_requests r
           JOIN hospitals h ON h.id=r.hospital_id
           WHERE r.patient_id=? ORDER BY r.id DESC LIMIT 20""",
        (g.user["id"],),
    ).fetchall()
    return jsonify({
        "history": [dict(r) for r in history],
        "favourites": [safe_hospital(r) for r in favs],
        "prescriptions": [dict(r) for r in prescriptions],
        "notifications": [dict(r) for r in notifications],
        "admissions": [dict(r) for r in admissions],
        "patient": dict(patient_meta) if patient_meta else {},
        "account": {"full_name": g.user["full_name"], "email": g.user["email"], "phone": g.user["phone"]},
    })


@app.put("/api/profile")
@roles("patient")
def update_patient_profile():
    data = body_json()
    full_name = str(data.get("full_name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    phone = str(data.get("phone", g.user["phone"])).strip()[:20]
    if not 2 <= len(full_name) <= 100 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return error("Enter a valid name and email address.")
    db = get_db()
    if data.get("new_password"):
        user = db.execute("SELECT password_hash FROM users WHERE id=?", (g.user["id"],)).fetchone()
        current = data.get("current_password", "")
        new_password = data["new_password"]
        if not isinstance(current, str) or not check_password_hash(user["password_hash"], current):
            return error("Current password is incorrect.")
        if not isinstance(new_password, str) or not 10 <= len(new_password) <= 128:
            return error("New password must contain 10–128 characters.")
        password_hash = generate_password_hash(new_password)
        password_sql = ",password_hash=?"
        password_params = (password_hash,)
    else:
        password_sql, password_params = "", ()
    try:
        db.execute(
            f"UPDATE users SET full_name=?,email=?,phone=?{password_sql} WHERE id=?",
            (full_name, email, phone) + password_params + (g.user["id"],),
        )
        age = data.get("age")
        if age in ("", None):
            age_val = None
        else:
            try:
                age_val = int(age)
                if not 0 <= age_val <= 130:
                    return error("Enter a valid age (0–130).")
            except (TypeError, ValueError):
                return error("Age must be a number.")
        db.execute(
            """INSERT INTO patients(user_id, age, gender, blood_group, emergency_contact, address, updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 age=excluded.age, gender=excluded.gender, blood_group=excluded.blood_group,
                 emergency_contact=excluded.emergency_contact, address=excluded.address, updated_at=excluded.updated_at""",
            (
                g.user["id"], age_val, str(data.get("gender", ""))[:30],
                str(data.get("blood_group", ""))[:10], str(data.get("emergency_contact", ""))[:60],
                str(data.get("address", ""))[:240], now_iso(),
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return error("An account with that email already exists.", 409)
    audit("Patient profile updated")
    return jsonify({"user": {"id": g.user["id"], "full_name": full_name, "email": email, "phone": phone}})


@app.post("/api/medical-history")
@roles("patient")
def add_medical_history():
    data = body_json()
    condition = str(data.get("condition", "")).strip()
    if not 2 <= len(condition) <= 120:
        return error("Condition must contain 2–120 characters.")
    cur = get_db().execute(
        """INSERT INTO medical_history(patient_id,condition,details,allergies,created_at)
           VALUES(?,?,?,?,?)""",
        (g.user["id"], condition, str(data.get("details", ""))[:1000],
         str(data.get("allergies", ""))[:500], now_iso()),
    )
    get_db().commit()
    return jsonify({"id": cur.lastrowid}), 201


@app.post("/api/prescriptions")
@roles("patient")
def upload_prescription():
    file = request.files.get("file")
    if not file or not file.filename:
        return error("Choose a prescription file.")
    name = secure_filename(file.filename)
    extension = Path(name).suffix.lower()
    if extension not in {".pdf", ".png", ".jpg", ".jpeg"}:
        return error("Only PDF, PNG or JPEG files are allowed.")
    content = file.read(UPLOAD_LIMIT + 1)
    if not content or len(content) > UPLOAD_LIMIT:
        return error("File is empty or exceeds the 5 MB limit.")
    stored = secrets.token_urlsafe(24) + extension
    path = (UPLOAD_DIR / stored).resolve()
    if not str(path).startswith(str(UPLOAD_DIR.resolve()) + os.sep):
        return error("Invalid upload path.", 400)
    with path.open("wb") as target:
        target.write(content)
    cur = get_db().execute(
        "INSERT INTO prescriptions(patient_id,original_name,stored_name,uploaded_at) VALUES(?,?,?,?)",
        (g.user["id"], name[:180], stored, now_iso()),
    )
    get_db().commit()
    return jsonify({"id": cur.lastrowid, "original_name": name}), 201


@app.post("/api/hospital/doctors/<int:doctor_id>/photo")
@roles("hospital")
def upload_doctor_photo(doctor_id):
    hospital = owned_hospital()
    doctor = get_db().execute(
        "SELECT * FROM doctors WHERE id=? AND hospital_id=?",
        (doctor_id, hospital["id"] if hospital else -1),
    ).fetchone()
    if not doctor:
        return error("Doctor not found in your hospital.", 404)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return error("Choose a PNG or JPEG doctor photograph.")
    extension = Path(secure_filename(upload.filename)).suffix.lower()
    if extension not in {".png", ".jpg", ".jpeg"}:
        return error("Doctor photographs must be PNG or JPEG.")
    content = upload.read(UPLOAD_LIMIT + 1)
    valid_image = (
        (extension == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"))
    )
    if not content or len(content) > UPLOAD_LIMIT or not valid_image:
        return error("The selected file is empty, too large, or is not a valid PNG/JPEG image.")
    filename = secrets.token_hex(16) + extension
    (PHOTO_DIR / filename).write_bytes(content)
    old_url = doctor["photo_url"]
    photo_url = f"/api/doctor-photos/{filename}"
    get_db().execute("UPDATE doctors SET photo_url=?,updated_at=? WHERE id=?",
                     (photo_url, now_iso(), doctor_id))
    get_db().commit()
    if old_url and old_url.startswith("/api/doctor-photos/"):
        old_name = old_url.rsplit("/", 1)[-1]
        if re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpe?g)", old_name):
            old_path = PHOTO_DIR / old_name
            if old_path.is_file():
                old_path.unlink()
    return jsonify({"photo_url": photo_url})


@app.post("/api/hospital/photo")
@roles("hospital")
def upload_hospital_photo():
    hospital = owned_hospital()
    if not hospital:
        return error("This account has no hospital profile.", 404)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return error("Choose a PNG or JPEG hospital photograph.")
    extension = Path(secure_filename(upload.filename)).suffix.lower()
    if extension not in {".png", ".jpg", ".jpeg"}:
        return error("Hospital photographs must be PNG or JPEG.")
    content = upload.read(UPLOAD_LIMIT + 1)
    valid_image = (
        (extension == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"))
    )
    if not content or len(content) > UPLOAD_LIMIT or not valid_image:
        return error("The selected file is empty, too large, or is not a valid PNG/JPEG image.")
    filename = secrets.token_hex(16) + extension
    (PHOTO_DIR / filename).write_bytes(content)
    old_url = hospital["photo_url"]
    photo_url = f"/api/hospital-photos/{filename}"
    get_db().execute("UPDATE hospitals SET photo_url=? WHERE id=?", (photo_url, hospital["id"]))
    get_db().commit()
    if old_url and old_url.startswith("/api/hospital-photos/"):
        old_name = old_url.rsplit("/", 1)[-1]
        if re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpe?g)", old_name):
            old_path = PHOTO_DIR / old_name
            if old_path.is_file():
                old_path.unlink()
    return jsonify({"photo_url": photo_url})


@app.get("/api/hospital-photos/<filename>")
def hospital_photo(filename):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpe?g)", filename):
        return error("Photo not found.", 404)
    path = (PHOTO_DIR / filename).resolve()
    if not str(path).startswith(str(PHOTO_DIR.resolve()) + os.sep) or not path.is_file():
        return error("Photo not found.", 404)
    response = send_file(path, conditional=True, max_age=3600)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/api/doctor-photos/<filename>")
def doctor_photo(filename):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpe?g)", filename):
        return error("Photo not found.", 404)
    path = (PHOTO_DIR / filename).resolve()
    if not str(path).startswith(str(PHOTO_DIR.resolve()) + os.sep) or not path.is_file():
        return error("Photo not found.", 404)
    response = send_file(path, conditional=True, max_age=3600)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/api/prescriptions/<int:prescription_id>")
@login_required
def download_prescription(prescription_id):
    db = get_db()
    row = db.execute("SELECT * FROM prescriptions WHERE id=?", (prescription_id,)).fetchone()
    if not row:
        return error("Prescription not found.", 404)
    if g.user["role"] not in {"admin"} and row["patient_id"] != g.user["id"]:
        return error("You do not have permission to access this file.", 403)
    path = (UPLOAD_DIR / row["stored_name"]).resolve()
    if not str(path).startswith(str(UPLOAD_DIR.resolve()) + os.sep) or not path.is_file():
        return error("Prescription file is missing.", 404)
    return send_file(path, as_attachment=True, download_name=row["original_name"])


@app.post("/api/favourites/<int:hospital_id>")
@roles("patient")
def favourite(hospital_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM hospitals WHERE id=?", (hospital_id,)).fetchone():
        return error("Saved hospital not found.", 404)
    db.execute("INSERT OR IGNORE INTO favourites(patient_id,hospital_id,created_at) VALUES(?,?,?)",
               (g.user["id"], hospital_id, now_iso()))
    db.commit()
    return jsonify({"saved": True})


@app.delete("/api/favourites/<int:hospital_id>")
@roles("patient")
def unfavourite(hospital_id):
    get_db().execute("DELETE FROM favourites WHERE patient_id=? AND hospital_id=?",
                     (g.user["id"], hospital_id))
    get_db().commit()
    return jsonify({"saved": False})


@app.post("/api/feedback")
@roles("patient")
def submit_feedback():
    data = body_json()
    try:
        hospital_id, rating = int(data.get("hospital_id")), int(data.get("rating"))
    except (TypeError, ValueError):
        return error("Choose a hospital and a 1–5 rating.")
    db = get_db()
    hospital = db.execute("SELECT id FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not hospital or not 1 <= rating <= 5:
        return error("Choose a listed hospital and a rating from 1 to 5.")
    today = date.today().isoformat()
    current_time = datetime.now().strftime("%H:%M")
    verified = bool(db.execute(
        """SELECT 1 FROM appointments WHERE patient_id=? AND hospital_id=?
           AND status IN ('confirmed','accepted','rescheduled','completed')
           AND (appointment_date<? OR (appointment_date=? AND appointment_time<=?)) LIMIT 1""",
        (g.user["id"], hospital_id, today, today, current_time),
    ).fetchone())
    try:
        db.execute(
            """INSERT INTO feedback(patient_id,hospital_id,rating,comment,verified_visit,created_at)
               VALUES(?,?,?,?,?,?)""",
            (g.user["id"], hospital_id, rating, str(data.get("comment", ""))[:1000],
             int(verified), now_iso()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return error("You have already submitted visit-verified feedback for this hospital.", 409)
    return jsonify({"ok": True, "verified_visit": verified}), 201


@app.post("/api/reports")
@roles("patient")
def report_information():
    data = body_json()
    message = str(data.get("message", "")).strip()
    if not 5 <= len(message) <= 1000:
        return error("Report details must contain 5–1000 characters.")
    hospital_id = data.get("hospital_id")
    try:
        hospital_id = int(hospital_id) if hospital_id else None
    except (ValueError, TypeError):
        return error("Invalid hospital.")
    cur = get_db().execute(
        """INSERT INTO reports(patient_id,hospital_id,message,created_at) VALUES(?,?,?,?)""",
        (g.user["id"], hospital_id, message, now_iso()),
    )
    get_db().commit()
    return jsonify({"id": cur.lastrowid, "message": "Report sent to the platform team."}), 201


# ---------------------------------------------------------------------------
# Hospital Staff Workspace (Preserved + Upgraded with OPD Schedules & IPD Beds)
# ---------------------------------------------------------------------------

def owned_hospital():
    return get_db().execute("SELECT * FROM hospitals WHERE owner_id=?", (g.user["id"],)).fetchone()


def save_schedule_record(db, hospital_id, doctor_id, data):
    specific_date = str(data.get("specific_date", "")).strip()
    if specific_date:
        try:
            d_obj = date.fromisoformat(specific_date)
            dow = d_obj.weekday()
        except ValueError:
            return None, "Specific date must be in YYYY-MM-DD format."
    else:
        try:
            dow = int(data.get("day_of_week"))
            if not 0 <= dow <= 6:
                raise ValueError()
        except (TypeError, ValueError):
            return None, "Choose a valid weekday (0=Monday to 6=Sunday)."

    is_available = 1 if data.get("is_available", True) else 0
    start_time = str(data.get("start_time", "09:00")).strip()
    end_time = str(data.get("end_time", "13:00")).strip()
    try:
        s_min = parse_hhmm(start_time)
        e_min = parse_hhmm(end_time)
        start_time = format_hhmm(s_min)
        end_time = format_hhmm(e_min)
        if is_available and e_min <= s_min:
            return None, "End time must be after start time."
        duration = int(data.get("slot_duration_minutes", 30))
        max_slots = int(data.get("max_slots", 8))
        if not 10 <= duration <= 120 or not 1 <= max_slots <= 60:
            return None, "Slot duration must be 10–120 minutes and max slots 1–60."
    except (TypeError, ValueError):
        return None, "Enter valid schedule start/end times (HH:MM), duration and max slots."

    db.execute(
        """INSERT INTO doctor_schedules
           (hospital_id, doctor_id, day_of_week, start_time, end_time, slot_duration_minutes,
            max_slots, is_available, specific_date, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(hospital_id, doctor_id, day_of_week, start_time, specific_date)
           DO UPDATE SET end_time=excluded.end_time,
                         slot_duration_minutes=excluded.slot_duration_minutes,
                         max_slots=excluded.max_slots,
                         is_available=excluded.is_available,
                         updated_at=excluded.updated_at""",
        (hospital_id, doctor_id, dow, start_time, end_time, duration, max_slots, is_available, specific_date, now_iso()),
    )
    return True, None


def upsert_ipd_bed(db, hospital_id, bed_type, total_beds, available_beds, daily_charge, is_sample, user_id):
    if bed_type not in BED_TYPES:
        return "Bed type must be General, Private, or ICU."
    try:
        total_beds = int(total_beds)
        available_beds = int(available_beds)
        daily_charge = int(daily_charge)
    except (TypeError, ValueError):
        return "Bed counts and daily charge must be whole numbers."
    if total_beds < 0 or available_beds < 0 or available_beds > total_beds or daily_charge < 0:
        return "Available beds cannot exceed total beds, and values cannot be negative."
    db.execute(
        """INSERT INTO ipd_beds(hospital_id, bed_type, total_beds, available_beds, daily_charge, is_sample, updated_at, updated_by)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(hospital_id, bed_type) DO UPDATE SET
             total_beds=excluded.total_beds,
             available_beds=excluded.available_beds,
             daily_charge=excluded.daily_charge,
             updated_at=excluded.updated_at,
             updated_by=excluded.updated_by""",
        (hospital_id, bed_type, total_beds, available_beds, daily_charge, int(bool(is_sample)), now_iso(), user_id),
    )
    return None


@app.get("/api/hospital/dashboard")
@roles("hospital")
def hospital_dashboard():
    db = get_db()
    hospital = owned_hospital()
    if not hospital:
        return error("This account has no hospital profile.", 404)
    doctors_list = get_doctors(db, hospital["id"])
    appointments = db.execute(
        """SELECT a.*, u.full_name AS patient_name, u.phone AS patient_phone,
                  d.name AS doctor_name, d.specialty
           FROM appointments a
           JOIN users u ON u.id = a.patient_id
           JOIN doctors d ON d.id = a.doctor_id
           WHERE a.hospital_id=?
           ORDER BY a.appointment_date DESC, a.appointment_time DESC LIMIT 150""",
        (hospital["id"],),
    ).fetchall()
    schedules = db.execute(
        """SELECT s.*, d.name AS doctor_name FROM doctor_schedules s
           JOIN doctors d ON d.id = s.doctor_id
           WHERE s.hospital_id=? ORDER BY d.name, s.specific_date, s.day_of_week, s.start_time""",
        (hospital["id"],),
    ).fetchall()
    beds = db.execute(
        "SELECT * FROM ipd_beds WHERE hospital_id=? ORDER BY CASE bed_type WHEN 'General' THEN 1 WHEN 'Private' THEN 2 ELSE 3 END",
        (hospital["id"],),
    ).fetchall()
    admissions = db.execute(
        """SELECT r.*, u.full_name AS patient_name, u.email AS patient_email, d.name AS doctor_name
           FROM admission_requests r
           JOIN users u ON u.id = r.patient_id
           LEFT JOIN doctors d ON d.id = r.doctor_id
           WHERE r.hospital_id=? ORDER BY r.id DESC LIMIT 100""",
        (hospital["id"],),
    ).fetchall()
    counts = {status: sum(1 for d in doctors_list if d.get("availability") == status) for status in AVAILABILITY}
    today_count = db.execute(
        "SELECT COUNT(*) FROM appointments WHERE hospital_id=? AND appointment_date=?",
        (hospital["id"], date.today().isoformat()),
    ).fetchone()[0]
    return jsonify({
        "hospital": safe_hospital(hospital),
        "doctors": doctors_list,
        "schedules": [dict(s) for s in schedules],
        "beds": [dict(b) for b in beds],
        "admissions": [dict(a) for a in admissions],
        "appointments": [dict(a) for a in appointments],
        "stats": {
            "total_doctors": len(doctors_list),
            "availability": counts,
            "today_appointments": today_count,
            "available_beds": sum(b["available_beds"] for b in beds),
            "pending_admissions": sum(1 for a in admissions if a["status"] == "Pending"),
        },
    })


@app.put("/api/hospital/profile")
@roles("hospital")
def update_hospital():
    data, hospital = body_json(), owned_hospital()
    if not hospital:
        return error("This account has no hospital profile.", 404)
    name = str(data.get("name", "")).strip()
    has_lat = data.get("latitude") not in (None, "")
    has_lon = data.get("longitude") not in (None, "")
    if has_lat != has_lon:
        return error("Provide both latitude and longitude, or leave both unchanged.")
    coords = valid_coords(data.get("latitude"), data.get("longitude")) if has_lat else None
    if has_lat and not coords:
        return error("Enter valid latitude and longitude coordinates.")
    if not 2 <= len(name) <= 140:
        return error("Hospital name must contain 2–140 characters.")
    try:
        specialties = data.get("specialties", [])
        facilities = data.get("facilities", [])
        if not isinstance(specialties, list) or not isinstance(facilities, list):
            raise ValueError()
        specialties = [str(x)[:80] for x in specialties[:30]]
        facilities = [str(x)[:100] for x in facilities[:30]]
    except ValueError:
        return error("Specialties and facilities must be lists.")
    db = get_db()
    db.execute(
        """UPDATE hospitals SET name=?,address=?,area=?,city=?,latitude=?,longitude=?,phone=?,
           specialties=?,facilities=?,emergency_services=?,opd_available=?,ipd_available=?,opd_timings=?
           WHERE id=?""",
        (
            name, str(data.get("address", ""))[:300], str(data.get("area", ""))[:100],
            str(data.get("city", ""))[:100], coords[0] if coords else hospital["latitude"],
            coords[1] if coords else hospital["longitude"], str(data.get("phone", ""))[:30],
            json.dumps(specialties), json.dumps(facilities),
            int(bool(data.get("emergency_services"))),
            int(bool(data.get("opd_available", hospital["opd_available"]))),
            int(bool(data.get("ipd_available", hospital["ipd_available"]))),
            str(data.get("opd_timings", hospital["opd_timings"] or ""))[:160],
            hospital["id"],
        ),
    )
    db.commit()
    audit("Hospital profile updated")
    return jsonify({"ok": True})


@app.post("/api/hospital/doctors")
@roles("hospital")
def create_doctor():
    data, hospital = body_json(), owned_hospital()
    if not hospital:
        return error("This account has no hospital profile.", 404)
    name, specialty = str(data.get("name", "")).strip(), str(data.get("specialty", "")).strip()
    try:
        years = int(data.get("experience_years", 0))
        fee = int(data.get("consultation_fee", 0) or 0)
    except (ValueError, TypeError):
        return error("Experience and consultation fee must be whole numbers.")
    languages = data.get("languages", [])
    if not 2 <= len(name) <= 120 or len(specialty) < 2 or not 0 <= years <= 80 or fee < 0 or not isinstance(languages, list):
        return error("Enter valid doctor details, fee and language list.")
    photo_url = str(data.get("photo_url", ""))[:500]
    if not valid_photo_url(photo_url):
        return error("Doctor photos must be uploaded through the hospital dashboard.")
    department = str(data.get("department", "")).strip()[:80] or department_for_specialty(specialty)
    availability = data.get("availability", "Unavailable")
    if availability not in AVAILABILITY:
        availability = "Unavailable"
    opd_available = 1 if data.get("opd_available", True) else 0
    db = get_db()
    now = now_iso()
    cur = db.execute(
        """INSERT INTO doctors(hospital_id,name,qualification,specialty,department,experience_years,
           consultation_fee,opd_available,languages,consultation_timings,photo_url,is_sample,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)""",
        (
            hospital["id"], name, str(data.get("qualification", ""))[:160], specialty[:80], department,
            years, fee, opd_available, json.dumps([str(x)[:40] for x in languages[:10]]),
            str(data.get("consultation_timings", ""))[:200], photo_url, now,
        ),
    )
    doc_id = cur.lastrowid
    db.execute(
        "INSERT INTO doctor_availability(doctor_id,status,updated_at,updated_by) VALUES(?,?,?,?)",
        (doc_id, availability, now, g.user["id"]),
    )
    db.execute(
        "INSERT OR IGNORE INTO doctor_specializations(doctor_id,specialization,department,created_at) VALUES(?,?,?,?)",
        (doc_id, specialty[:80], department, now),
    )
    db.execute(
        """INSERT OR IGNORE INTO hospital_doctors
           (hospital_id,doctor_id,department,consultation_fee,opd_available,availability_status,is_active,updated_at)
           VALUES(?,?,?,?,?,?,1,?)""",
        (hospital["id"], doc_id, department, fee, opd_available, availability, now),
    )
    db.commit()
    return jsonify({"id": doc_id, "availability": availability}), 201


@app.put("/api/hospital/doctors/<int:doctor_id>")
@roles("hospital")
def update_doctor(doctor_id):
    data, db, hospital = body_json(), get_db(), owned_hospital()
    if not hospital:
        return error("This account has no hospital profile.", 404)
    assignment = db.execute(
        """SELECT d.*, hd.department AS hd_dept, hd.consultation_fee AS hd_fee,
                  hd.opd_available AS hd_opd, hd.availability_status AS hd_avail
           FROM hospital_doctors hd JOIN doctors d ON d.id = hd.doctor_id
           WHERE hd.doctor_id=? AND hd.hospital_id=? AND hd.is_active=1""",
        (doctor_id, hospital["id"]),
    ).fetchone()
    if not assignment:
        return error("Doctor not found in your hospital.", 404)
    status = data.get("availability")
    if status is not None and status not in AVAILABILITY:
        return error("Choose Available, Busy, On Break or Unavailable.")
    languages = data.get("languages", json.loads(assignment["languages"] or "[]"))
    if not isinstance(languages, list):
        return error("Languages must be a list.")
    fields = {
        "name": str(data.get("name", assignment["name"])).strip()[:120],
        "qualification": str(data.get("qualification", assignment["qualification"]))[:160],
        "specialty": str(data.get("specialty", assignment["specialty"])).strip()[:80],
        "department": str(data.get("department", assignment["hd_dept"] or assignment["department"] or "")).strip()[:80],
        "consultation_timings": str(data.get("consultation_timings", assignment["consultation_timings"]))[:200],
        "photo_url": str(data.get("photo_url", assignment["photo_url"]))[:500],
        "languages": json.dumps([str(x)[:40] for x in languages[:10]]),
    }
    if not fields["department"]:
        fields["department"] = department_for_specialty(fields["specialty"])
    try:
        years = int(data.get("experience_years", assignment["experience_years"]))
        fee = int(data.get("consultation_fee", assignment["hd_fee"] or assignment["consultation_fee"] or 0))
    except (ValueError, TypeError):
        return error("Experience and consultation fee must be whole numbers.")
    if len(fields["name"]) < 2 or len(fields["specialty"]) < 2 or not 0 <= years <= 80 or fee < 0:
        return error("Enter valid doctor details.")
    if not valid_photo_url(fields["photo_url"]):
        return error("Doctor photos must be uploaded through the hospital dashboard.")
    opd_avail = int(bool(data.get("opd_available", assignment["hd_opd"])))
    now = now_iso()

    db.execute(
        """UPDATE doctors SET name=?,qualification=?,specialty=?,department=?,experience_years=?,
           consultation_fee=?,opd_available=?,languages=?,consultation_timings=?,photo_url=?,updated_at=?
           WHERE id=?""",
        (
            fields["name"], fields["qualification"], fields["specialty"], fields["department"],
            years, fee, opd_avail, fields["languages"], fields["consultation_timings"],
            fields["photo_url"], now, doctor_id,
        ),
    )
    eff_status = status if status is not None else assignment["hd_avail"]
    db.execute(
        """UPDATE hospital_doctors
           SET department=?, consultation_fee=?, opd_available=?, availability_status=?, updated_at=?
           WHERE hospital_id=? AND doctor_id=?""",
        (fields["department"], fee, opd_avail, eff_status, now, hospital["id"], doctor_id),
    )
    db.execute(
        "INSERT OR IGNORE INTO doctor_specializations(doctor_id,specialization,department,created_at) VALUES(?,?,?,?)",
        (doctor_id, fields["specialty"], fields["department"], now),
    )
    if status is not None:
        db.execute(
            """INSERT INTO doctor_availability(doctor_id,status,updated_at,updated_by) VALUES(?,?,?,?)
               ON CONFLICT(doctor_id) DO UPDATE SET status=excluded.status,
               updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
            (doctor_id, status, now, g.user["id"]),
        )
        db.execute(
            """INSERT INTO notifications(user_id,message,created_at)
               SELECT DISTINCT patient_id,?,? FROM appointments
               WHERE doctor_id=? AND hospital_id=? AND status IN ('pending','confirmed','accepted','rescheduled')""",
            (f"Availability updated for {fields['name']}: {status}.", now, doctor_id, hospital["id"]),
        )
    db.commit()
    audit("Doctor profile or availability updated")
    return jsonify({"ok": True, "availability": eff_status})


@app.delete("/api/hospital/doctors/<int:doctor_id>")
@roles("hospital")
def delete_doctor(doctor_id):
    hospital = owned_hospital()
    db = get_db()
    if not hospital or not db.execute(
        "SELECT 1 FROM hospital_doctors WHERE doctor_id=? AND hospital_id=?", (doctor_id, hospital["id"])
    ).fetchone():
        return error("Doctor not found in your hospital.", 404)
    if db.execute(
        """SELECT 1 FROM appointments
           WHERE doctor_id=? AND hospital_id=? AND status IN ('pending','confirmed','accepted','rescheduled')""",
        (doctor_id, hospital["id"]),
    ).fetchone():
        return error("Doctor has active appointment requests; resolve them before removing the doctor.", 409)
    db.execute("DELETE FROM hospital_doctors WHERE doctor_id=? AND hospital_id=?", (doctor_id, hospital["id"]))
    remaining = db.execute("SELECT COUNT(*) FROM hospital_doctors WHERE doctor_id=?", (doctor_id,)).fetchone()[0]
    if remaining == 0:
        db.execute("DELETE FROM doctors WHERE id=?", (doctor_id,))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/hospital/doctors/<int:doctor_id>/schedules")
@roles("hospital")
def hospital_upsert_schedule(doctor_id):
    hospital = owned_hospital()
    db = get_db()
    if not hospital or not db.execute(
        "SELECT 1 FROM hospital_doctors WHERE hospital_id=? AND doctor_id=? AND is_active=1",
        (hospital["id"], doctor_id),
    ).fetchone():
        return error("Doctor not found in your hospital.", 404)
    ok, err_msg = save_schedule_record(db, hospital["id"], doctor_id, body_json())
    if err_msg:
        return error(err_msg)
    db.commit()
    audit("Hospital updated doctor OPD schedule")
    return jsonify({"ok": True}), 201


@app.delete("/api/hospital/schedules/<int:schedule_id>")
@roles("hospital")
def hospital_delete_schedule(schedule_id):
    hospital = owned_hospital()
    if not hospital:
        return error("Hospital profile not found.", 404)
    cur = get_db().execute(
        "DELETE FROM doctor_schedules WHERE id=? AND hospital_id=?", (schedule_id, hospital["id"])
    )
    get_db().commit()
    if not cur.rowcount:
        return error("Schedule not found.", 404)
    return jsonify({"ok": True})


@app.put("/api/hospital/beds")
@roles("hospital")
def hospital_update_beds():
    hospital = owned_hospital()
    if not hospital:
        return error("Hospital profile not found.", 404)
    data = body_json()
    beds = data.get("beds") if isinstance(data.get("beds"), list) else [data]
    db = get_db()
    for item in beds:
        if not isinstance(item, dict):
            return error("Invalid bed payload.")
        err_msg = upsert_ipd_bed(
            db, hospital["id"], str(item.get("bed_type", "")).strip(),
            item.get("total_beds", 0), item.get("available_beds", 0),
            item.get("daily_charge", 0), hospital["is_sample"], g.user["id"],
        )
        if err_msg:
            db.rollback()
            return error(err_msg)
    db.commit()
    audit("Hospital IPD beds updated")
    return jsonify({"ok": True})


@app.patch("/api/hospital/appointments/<int:appointment_id>")
@roles("hospital")
def update_appointment(appointment_id):
    data, db, hospital = body_json(), get_db(), owned_hospital()
    row = db.execute("SELECT * FROM appointments WHERE id=? AND hospital_id=?",
                     (appointment_id, hospital["id"] if hospital else -1)).fetchone()
    if not row:
        return error("Appointment not found.", 404)
    if row["status"] not in set(ACTIVE_APPOINTMENT_STATES):
        return error("This appointment is no longer active.")
    status = str(data.get("status", "")).strip().lower()
    if status not in {"confirmed", "accepted", "rejected", "rescheduled", "completed", "cancelled"}:
        return error("Choose confirmed, accepted, completed, rejected, cancelled, or rescheduled.")
    target_date, target_time = row["appointment_date"], row["appointment_time"]
    if status == "rescheduled":
        try:
            target_date = date.fromisoformat(str(data.get("date", ""))).isoformat()
            target_time = datetime.strptime(str(data.get("time", "")), "%H:%M").strftime("%H:%M")
            if date.fromisoformat(target_date) < date.today():
                return error("Rescheduled appointments must be in the future.")
            if target_date == date.today().isoformat() and target_time <= datetime.now().strftime("%H:%M"):
                return error("Choose a future time for today's rescheduled appointment.")
        except ValueError:
            return error("Choose a valid reschedule date and time.")
    try:
        db.execute(
            "UPDATE appointments SET status=?,appointment_date=?,appointment_time=? WHERE id=?",
            (status, target_date, target_time, appointment_id),
        )
    except sqlite3.IntegrityError:
        return error("That time slot is already booked. Choose another.", 409)
    notify(row["patient_id"], f"Appointment {row['appointment_code'] or f'#{appointment_id}'} was marked {status} by the hospital.")
    db.commit()
    return jsonify({"ok": True, "status": status, "date": target_date, "time": target_time})


@app.get("/api/notifications")
@login_required
def notifications():
    rows = get_db().execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (g.user["id"],)
    ).fetchall()
    return jsonify({"notifications": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Admin Dashboard & Complete Management Suite (Requirement 5 + Existing)
# ---------------------------------------------------------------------------

@app.get("/api/admin/dashboard")
@roles("admin")
def admin_dashboard():
    db = get_db()
    counts = {
        "patients": db.execute("SELECT COUNT(*) FROM users WHERE role='patient'").fetchone()[0],
        "hospital_accounts": db.execute("SELECT COUNT(*) FROM users WHERE role='hospital'").fetchone()[0],
        "hospitals": db.execute("SELECT COUNT(*) FROM hospitals").fetchone()[0],
        "unverified": db.execute("SELECT COUNT(*) FROM hospitals WHERE verified=0").fetchone()[0],
        "doctors": db.execute("SELECT COUNT(*) FROM doctors").fetchone()[0],
        "appointments": db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0],
        "pending_appointments": db.execute("SELECT COUNT(*) FROM appointments WHERE status='pending'").fetchone()[0],
        "accepted_appointments": db.execute("SELECT COUNT(*) FROM appointments WHERE status IN ('accepted','confirmed')").fetchone()[0],
        "completed_appointments": db.execute("SELECT COUNT(*) FROM appointments WHERE status='completed'").fetchone()[0],
        "rescheduled_appointments": db.execute("SELECT COUNT(*) FROM appointments WHERE status='rescheduled'").fetchone()[0],
        "available_ipd_beds": db.execute("SELECT COALESCE(SUM(available_beds), 0) FROM ipd_beds").fetchone()[0],
        "pending_admissions": db.execute("SELECT COUNT(*) FROM admission_requests WHERE status='Pending'").fetchone()[0],
        "open_reports": db.execute("SELECT COUNT(*) FROM reports WHERE status='open'").fetchone()[0],
    }
    hospitals = db.execute(
        """SELECT h.*, u.email AS owner_email, u.full_name AS owner_name,
                  (SELECT status FROM hospital_verification v WHERE v.hospital_id=h.id ORDER BY v.id DESC LIMIT 1) AS verification_status
           FROM hospitals h LEFT JOIN users u ON u.id=h.owner_id
           ORDER BY h.verified DESC, h.is_sample ASC, h.id DESC"""
    ).fetchall()
    users = db.execute(
        "SELECT id,email,full_name,phone,role,active,created_at FROM users ORDER BY id DESC LIMIT 150"
    ).fetchall()
    reports = db.execute(
        """SELECT r.*, u.full_name AS patient_name, h.name AS hospital_name
           FROM reports r JOIN users u ON u.id=r.patient_id
           LEFT JOIN hospitals h ON h.id=r.hospital_id
           ORDER BY r.id DESC LIMIT 100"""
    ).fetchall()
    activity = db.execute(
        """SELECT a.*, u.full_name FROM activity_log a LEFT JOIN users u ON u.id=a.user_id
           ORDER BY a.id DESC LIMIT 40"""
    ).fetchall()
    doctors_list = get_doctors(db)
    appointments = db.execute(
        """SELECT a.*, u.full_name AS patient_name, u.email AS patient_email, u.phone AS patient_phone,
                  d.name AS doctor_name, d.specialty, h.name AS hospital_name
           FROM appointments a
           JOIN users u ON u.id = a.patient_id
           JOIN doctors d ON d.id = a.doctor_id
           JOIN hospitals h ON h.id = a.hospital_id
           ORDER BY a.appointment_date DESC, a.appointment_time DESC, a.id DESC LIMIT 200"""
    ).fetchall()
    ipd_beds = db.execute(
        """SELECT b.*, h.name AS hospital_name, h.city AS hospital_city
           FROM ipd_beds b JOIN hospitals h ON h.id = b.hospital_id
           ORDER BY h.name, CASE b.bed_type WHEN 'General' THEN 1 WHEN 'Private' THEN 2 ELSE 3 END"""
    ).fetchall()
    admissions = db.execute(
        """SELECT r.*, u.full_name AS patient_name, u.email AS patient_email,
                  h.name AS hospital_name, d.name AS doctor_name
           FROM admission_requests r
           JOIN users u ON u.id = r.patient_id
           JOIN hospitals h ON h.id = r.hospital_id
           LEFT JOIN doctors d ON d.id = r.doctor_id
           ORDER BY r.id DESC LIMIT 150"""
    ).fetchall()
    schedules = db.execute(
        """SELECT s.*, d.name AS doctor_name, h.name AS hospital_name
           FROM doctor_schedules s
           JOIN doctors d ON d.id = s.doctor_id
           JOIN hospitals h ON h.id = s.hospital_id
           ORDER BY h.name, d.name, s.specific_date, s.day_of_week, s.start_time"""
    ).fetchall()
    diseases = [dict(r) for r in db.execute("SELECT * FROM diseases ORDER BY name").fetchall()]

    return jsonify({
        "stats": counts,
        "hospitals": [
            safe_hospital(h) | {
                "owner_email": h["owner_email"],
                "owner_name": h["owner_name"],
                "verification_status": h["verification_status"],
            }
            for h in hospitals
        ],
        "doctors": doctors_list,
        "schedules": [dict(s) for s in schedules],
        "appointments": [dict(a) for a in appointments],
        "ipd_beds": [dict(b) for b in ipd_beds],
        "admissions": [dict(a) for a in admissions],
        "diseases": diseases,
        "users": [dict(u) for u in users],
        "reports": [dict(r) for r in reports],
        "activity": [dict(a) for a in activity],
    })


@app.post("/api/admin/hospitals")
@roles("admin")
def admin_create_hospital():
    data = body_json()
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    staff = str(data.get("staff_name", "")).strip()
    password = data.get("password", "")
    if len(name) < 2:
        return error("Enter a valid hospital name.")

    db = get_db()
    owner_id = None
    if email or staff or password:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(staff) < 2:
            return error("Enter a valid staff name and email address.")
        if not isinstance(password, str) or len(password) < 10:
            return error("Temporary password must contain at least 10 characters.")

    specialties = data.get("specialties", [])
    facilities = data.get("facilities", [])
    if isinstance(specialties, str):
        specialties = [x.strip() for x in specialties.split(",") if x.strip()]
    if isinstance(facilities, str):
        facilities = [x.strip() for x in facilities.split(",") if x.strip()]
    if not isinstance(specialties, list) or not isinstance(facilities, list):
        return error("Specialties and facilities must be lists.")

    verified = 1 if data.get("verified") is True else 0
    try:
        if email:
            cur = db.execute(
                "INSERT INTO users(email,password_hash,full_name,role,created_at) VALUES(?,?,?,?,?)",
                (email, generate_password_hash(password), staff, "hospital", now_iso()),
            )
            owner_id = cur.lastrowid
        coords = valid_coords(data.get("latitude"), data.get("longitude"))
        hospital_cur = db.execute(
            """INSERT INTO hospitals(owner_id,name,address,area,city,latitude,longitude,phone,
               specialties,facilities,emergency_services,opd_available,ipd_available,opd_timings,
               verified,is_sample,source,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
            (
                owner_id, name[:140], str(data.get("address", ""))[:300],
                str(data.get("area", ""))[:100], str(data.get("city", ""))[:100],
                coords[0] if coords else None, coords[1] if coords else None,
                str(data.get("phone", ""))[:30],
                json.dumps([str(x)[:80] for x in specialties[:30]]),
                json.dumps([str(x)[:100] for x in facilities[:30]]),
                int(bool(data.get("emergency_services", False))),
                int(bool(data.get("opd_available", True))),
                int(bool(data.get("ipd_available", True))),
                str(data.get("opd_timings", ""))[:160],
                verified, "admin registered", now_iso(),
            ),
        )
        db.execute(
            "INSERT INTO hospital_verification(hospital_id,admin_id,status,updated_at) VALUES(?,?,?,?)",
            (hospital_cur.lastrowid, g.user["id"], "verified" if verified else "pending", now_iso()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return error("An account with that email already exists.", 409)
    audit(f"Admin added hospital: {name[:60]}")
    return jsonify({
        "hospital_id": hospital_cur.lastrowid,
        "message": "Hospital added successfully." if verified else "Hospital account created and awaiting verification.",
    }), 201


@app.put("/api/admin/hospitals/<int:hospital_id>")
@roles("admin")
def admin_update_hospital(hospital_id):
    data = body_json()
    db = get_db()
    hosp = db.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not hosp:
        return error("Hospital not found.", 404)
    name = str(data.get("name", hosp["name"])).strip()
    if not 2 <= len(name) <= 140:
        return error("Hospital name must contain 2–140 characters.")

    has_lat = data.get("latitude") not in (None, "")
    has_lon = data.get("longitude") not in (None, "")
    if has_lat != has_lon:
        return error("Provide both latitude and longitude, or leave both empty.")
    coords = valid_coords(data.get("latitude"), data.get("longitude")) if has_lat else None
    if has_lat and not coords:
        return error("Enter valid latitude and longitude.")

    specialties = data.get("specialties", json.loads(hosp["specialties"] or "[]"))
    facilities = data.get("facilities", json.loads(hosp["facilities"] or "[]"))
    if isinstance(specialties, str):
        specialties = [x.strip() for x in specialties.split(",") if x.strip()]
    if isinstance(facilities, str):
        facilities = [x.strip() for x in facilities.split(",") if x.strip()]

    db.execute(
        """UPDATE hospitals SET name=?,address=?,area=?,city=?,latitude=?,longitude=?,phone=?,
           specialties=?,facilities=?,emergency_services=?,opd_available=?,ipd_available=?,
           opd_timings=?,verified=? WHERE id=?""",
        (
            name, str(data.get("address", hosp["address"]))[:300],
            str(data.get("area", hosp["area"]))[:100],
            str(data.get("city", hosp["city"]))[:100],
            coords[0] if coords else hosp["latitude"],
            coords[1] if coords else hosp["longitude"],
            str(data.get("phone", hosp["phone"]))[:30],
            json.dumps([str(x)[:80] for x in specialties[:30]]),
            json.dumps([str(x)[:100] for x in facilities[:30]]),
            int(bool(data.get("emergency_services", hosp["emergency_services"]))),
            int(bool(data.get("opd_available", hosp["opd_available"]))),
            int(bool(data.get("ipd_available", hosp["ipd_available"]))),
            str(data.get("opd_timings", hosp["opd_timings"] or ""))[:160],
            int(bool(data.get("verified", hosp["verified"]))),
            hospital_id,
        ),
    )
    db.commit()
    audit(f"Admin updated hospital #{hospital_id}")
    return jsonify({"ok": True})


@app.patch("/api/admin/hospitals/<int:hospital_id>")
@roles("admin")
def verify_hospital(hospital_id):
    data = body_json()
    verified = data.get("verified")
    if not isinstance(verified, bool):
        return error("Set verified to true or false.")
    db = get_db()
    hospital = db.execute("SELECT id FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not hospital:
        return error("Hospital not found.", 404)
    status = "verified" if verified else "pending"
    db.execute("UPDATE hospitals SET verified=? WHERE id=?", (int(verified), hospital_id))
    db.execute(
        """INSERT INTO hospital_verification(hospital_id,admin_id,status,note,updated_at)
           VALUES(?,?,?,?,?)""",
        (hospital_id, g.user["id"], status, str(data.get("note", ""))[:500], now_iso()),
    )
    owner = db.execute("SELECT owner_id FROM hospitals WHERE id=?", (hospital_id,)).fetchone()["owner_id"]
    if owner:
        notify(owner, "Your hospital profile was verified." if verified else "Your hospital verification is pending.")
    db.commit()
    return jsonify({"verified": verified})


@app.post("/api/admin/doctors")
@roles("admin")
def admin_create_doctor():
    data = body_json()
    try:
        hospital_id = int(data.get("hospital_id"))
        years = int(data.get("experience_years", 0) or 0)
        fee = int(data.get("consultation_fee", 0) or 0)
    except (TypeError, ValueError):
        return error("Choose a valid hospital, experience years, and consultation fee.")
    name = str(data.get("name", "")).strip()
    specialty = str(data.get("specialty", "")).strip()
    if len(name) < 2 or len(specialty) < 2 or not 0 <= years <= 80 or fee < 0:
        return error("Enter valid doctor name, specialty, experience and consultation fee.")
    availability = str(data.get("availability", "Unavailable")).strip()
    if availability not in AVAILABILITY:
        return error("Choose Available, Busy, On Break, or Unavailable.")

    db = get_db()
    hosp = db.execute("SELECT id, is_sample FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not hosp:
        return error("Hospital not found.", 404)

    languages = data.get("languages", ["English"])
    if isinstance(languages, str):
        languages = [x.strip() for x in languages.split(",") if x.strip()]
    specializations = data.get("specializations", [specialty])
    if isinstance(specializations, str):
        specializations = [x.strip() for x in specializations.split(",") if x.strip()]
    if specialty not in specializations:
        specializations = [specialty] + specializations

    department = str(data.get("department", "")).strip()[:80] or department_for_specialty(specialty)
    opd_available = 1 if data.get("opd_available", True) else 0
    now = now_iso()

    cur = db.execute(
        """INSERT INTO doctors(hospital_id,name,qualification,specialty,department,experience_years,
           consultation_fee,opd_available,languages,consultation_timings,photo_url,is_sample,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,'',?,?)""",
        (
            hospital_id, name[:120], str(data.get("qualification", ""))[:160], specialty[:80], department,
            years, fee, opd_available, json.dumps([str(x)[:40] for x in languages[:10]]),
            str(data.get("consultation_timings", ""))[:200], int(hosp["is_sample"]), now,
        ),
    )
    doc_id = cur.lastrowid
    db.execute(
        "INSERT INTO doctor_availability(doctor_id,status,updated_at,updated_by) VALUES(?,?,?,?)",
        (doc_id, availability, now, g.user["id"]),
    )
    for sp in specializations[:10]:
        db.execute(
            "INSERT OR IGNORE INTO doctor_specializations(doctor_id,specialization,department,created_at) VALUES(?,?,?,?)",
            (doc_id, str(sp)[:80], department, now),
        )
    db.execute(
        """INSERT INTO hospital_doctors
           (hospital_id,doctor_id,department,consultation_fee,opd_available,availability_status,is_active,updated_at)
           VALUES(?,?,?,?,?,?,1,?)""",
        (hospital_id, doc_id, department, fee, opd_available, availability, now),
    )
    db.commit()
    audit(f"Admin added doctor: {name[:60]}")
    return jsonify({"id": doc_id, "hospital_id": hospital_id, "availability": availability}), 201


@app.put("/api/admin/doctors/<int:doctor_id>")
@roles("admin")
def admin_update_doctor(doctor_id):
    data = body_json()
    db = get_db()
    doc = db.execute("SELECT * FROM doctors WHERE id=?", (doctor_id,)).fetchone()
    if not doc:
        return error("Doctor not found.", 404)

    target_hospital_id = data.get("hospital_id", doc["hospital_id"])
    try:
        target_hospital_id = int(target_hospital_id)
        years = int(data.get("experience_years", doc["experience_years"]))
        fee = int(data.get("consultation_fee", doc["consultation_fee"] or 0))
    except (TypeError, ValueError):
        return error("Invalid hospital ID, experience years, or fee.")

    if not db.execute("SELECT 1 FROM hospitals WHERE id=?", (target_hospital_id,)).fetchone():
        return error("Target hospital not found.", 404)

    name = str(data.get("name", doc["name"])).strip()[:120]
    specialty = str(data.get("specialty", doc["specialty"])).strip()[:80]
    department = str(data.get("department", doc["department"] or "")).strip()[:80] or department_for_specialty(specialty)
    qualification = str(data.get("qualification", doc["qualification"]))[:160]
    timings = str(data.get("consultation_timings", doc["consultation_timings"]))[:200]
    opd_avail = int(bool(data.get("opd_available", doc["opd_available"])))

    status = data.get("availability")
    if status is not None and status not in AVAILABILITY:
        return error("Choose Available, Busy, On Break, or Unavailable.")

    languages = data.get("languages", json.loads(doc["languages"] or "[]"))
    if isinstance(languages, str):
        languages = [x.strip() for x in languages.split(",") if x.strip()]

    now = now_iso()
    db.execute(
        """UPDATE doctors SET name=?,qualification=?,specialty=?,department=?,experience_years=?,
           consultation_fee=?,opd_available=?,languages=?,consultation_timings=?,updated_at=?
           WHERE id=?""",
        (name, qualification, specialty, department, years, fee, opd_avail,
         json.dumps([str(x)[:40] for x in languages[:10]]), timings, now, doctor_id),
    )

    # Update or create hospital_doctors assignment
    current_status = status
    if current_status is None:
        existing_hd = db.execute(
            "SELECT availability_status FROM hospital_doctors WHERE hospital_id=? AND doctor_id=?",
            (target_hospital_id, doctor_id),
        ).fetchone()
        current_status = existing_hd["availability_status"] if existing_hd else "Unavailable"

    db.execute(
        """INSERT INTO hospital_doctors
           (hospital_id,doctor_id,department,consultation_fee,opd_available,availability_status,is_active,updated_at)
           VALUES(?,?,?,?,?,?,1,?)
           ON CONFLICT(hospital_id,doctor_id) DO UPDATE SET
             department=excluded.department,
             consultation_fee=excluded.consultation_fee,
             opd_available=excluded.opd_available,
             availability_status=excluded.availability_status,
             is_active=1,
             updated_at=excluded.updated_at""",
        (target_hospital_id, doctor_id, department, fee, opd_avail, current_status, now),
    )

    if status is not None:
        db.execute(
            """INSERT INTO doctor_availability(doctor_id,status,updated_at,updated_by) VALUES(?,?,?,?)
               ON CONFLICT(doctor_id) DO UPDATE SET status=excluded.status,
               updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
            (doctor_id, status, now, g.user["id"]),
        )

    if "specializations" in data:
        specs = data["specializations"]
        if isinstance(specs, str):
            specs = [x.strip() for x in specs.split(",") if x.strip()]
        if isinstance(specs, list) and specs:
            db.execute("DELETE FROM doctor_specializations WHERE doctor_id=?", (doctor_id,))
            for sp in specs[:10]:
                db.execute(
                    "INSERT OR IGNORE INTO doctor_specializations(doctor_id,specialization,department,created_at) VALUES(?,?,?,?)",
                    (doctor_id, str(sp)[:80], department, now),
                )
    else:
        db.execute(
            "INSERT OR IGNORE INTO doctor_specializations(doctor_id,specialization,department,created_at) VALUES(?,?,?,?)",
            (doctor_id, specialty, department, now),
        )

    db.commit()
    audit(f"Admin updated doctor #{doctor_id}")
    return jsonify({"ok": True, "availability": current_status})


@app.post("/api/admin/hospital-doctors")
@roles("admin")
def admin_assign_doctor_to_hospital():
    data = body_json()
    try:
        hospital_id = int(data.get("hospital_id"))
        doctor_id = int(data.get("doctor_id"))
        fee = int(data.get("consultation_fee", 0) or 0)
    except (TypeError, ValueError):
        return error("Select valid hospital, doctor, and consultation fee.")
    availability = str(data.get("availability", "Available")).strip()
    if availability not in AVAILABILITY:
        return error("Choose Available, Busy, On Break, or Unavailable.")

    db = get_db()
    hosp = db.execute("SELECT id FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    doc = db.execute("SELECT * FROM doctors WHERE id=?", (doctor_id,)).fetchone()
    if not hosp or not doc:
        return error("Hospital or doctor not found.", 404)
    department = str(data.get("department", "")).strip()[:80] or doc["department"] or department_for_specialty(doc["specialty"])
    opd_avail = 1 if data.get("opd_available", True) else 0
    now = now_iso()

    db.execute(
        """INSERT INTO hospital_doctors
           (hospital_id,doctor_id,department,consultation_fee,opd_available,availability_status,is_active,updated_at)
           VALUES(?,?,?,?,?,?,1,?)
           ON CONFLICT(hospital_id,doctor_id) DO UPDATE SET
             department=excluded.department,
             consultation_fee=excluded.consultation_fee,
             opd_available=excluded.opd_available,
             availability_status=excluded.availability_status,
             is_active=1,
             updated_at=excluded.updated_at""",
        (hospital_id, doctor_id, department, fee or doc["consultation_fee"], opd_avail, availability, now),
    )
    db.commit()
    audit(f"Admin assigned doctor #{doctor_id} to hospital #{hospital_id}")
    return jsonify({"ok": True}), 201


@app.post("/api/admin/doctors/<int:doctor_id>/schedules")
@roles("admin")
def admin_upsert_schedule(doctor_id):
    data = body_json()
    try:
        hospital_id = int(data.get("hospital_id"))
    except (TypeError, ValueError):
        return error("Choose a valid hospital for this schedule.")
    db = get_db()
    if not db.execute(
        "SELECT 1 FROM hospital_doctors WHERE hospital_id=? AND doctor_id=? AND is_active=1",
        (hospital_id, doctor_id),
    ).fetchone():
        return error("Assign this doctor to the selected hospital before adding an OPD schedule.", 400)
    ok, err_msg = save_schedule_record(db, hospital_id, doctor_id, data)
    if err_msg:
        return error(err_msg)
    db.commit()
    audit(f"Admin updated OPD schedule for doctor #{doctor_id}")
    return jsonify({"ok": True}), 201


@app.delete("/api/admin/schedules/<int:schedule_id>")
@roles("admin")
def admin_delete_schedule(schedule_id):
    cur = get_db().execute("DELETE FROM doctor_schedules WHERE id=?", (schedule_id,))
    get_db().commit()
    if not cur.rowcount:
        return error("Schedule not found.", 404)
    return jsonify({"ok": True})


@app.patch("/api/admin/appointments/<int:appointment_id>")
@roles("admin")
def admin_update_appointment(appointment_id):
    data = body_json()
    status = str(data.get("status", "")).strip().lower()
    if status not in APPOINTMENT_STATES:
        return error("Choose a valid appointment status (pending, confirmed, completed, cancelled, rejected, rescheduled).")
    db = get_db()
    row = db.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    if not row:
        return error("Appointment not found.", 404)
    try:
        db.execute("UPDATE appointments SET status=? WHERE id=?", (status, appointment_id))
    except sqlite3.IntegrityError:
        return error("Cannot activate this appointment because that doctor time slot is already booked.", 409)
    notify(row["patient_id"], f"Appointment {row['appointment_code'] or f'#{appointment_id}'} status updated to {status}.")
    db.commit()
    audit(f"Admin updated appointment #{appointment_id} to {status}")
    return jsonify({"ok": True, "status": status})


@app.put("/api/admin/ipd/beds")
@roles("admin")
def admin_update_ipd_beds():
    data = body_json()
    try:
        hospital_id = int(data.get("hospital_id"))
    except (TypeError, ValueError):
        return error("Select a valid hospital.")
    db = get_db()
    hosp = db.execute("SELECT id, is_sample FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not hosp:
        return error("Hospital not found.", 404)
    beds = data.get("beds") if isinstance(data.get("beds"), list) else [data]
    for item in beds:
        if not isinstance(item, dict):
            return error("Invalid bed payload.")
        err_msg = upsert_ipd_bed(
            db, hospital_id, str(item.get("bed_type", "")).strip(),
            item.get("total_beds", 0), item.get("available_beds", 0),
            item.get("daily_charge", 0), hosp["is_sample"], g.user["id"],
        )
        if err_msg:
            db.rollback()
            return error(err_msg)
    db.commit()
    audit(f"Admin updated IPD beds for hospital #{hospital_id}")
    return jsonify({"ok": True})


@app.post("/api/admin/diseases")
@roles("admin")
def admin_add_disease():
    data = body_json()
    name = str(data.get("name", "")).strip()
    specialist = str(data.get("recommended_specialist", "")).strip()
    department = str(data.get("department", "")).strip() or department_for_specialty(specialist)
    if len(name) < 2 or len(specialist) < 2:
        return error("Enter a valid disease name and recommended specialist.")
    db = get_db()
    db.execute(
        """INSERT INTO diseases(name, category, recommended_specialist, department, keywords, guidance_note, is_emergency_risk)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             category=excluded.category,
             recommended_specialist=excluded.recommended_specialist,
             department=excluded.department,
             keywords=excluded.keywords,
             guidance_note=excluded.guidance_note,
             is_emergency_risk=excluded.is_emergency_risk""",
        (
            name[:100], str(data.get("category", "General"))[:80], specialist[:80], department[:80],
            str(data.get("keywords", ""))[:300],
            str(data.get("guidance_note", "Consult a qualified specialist for medical evaluation."))[:300],
            int(bool(data.get("is_emergency_risk", False))),
        ),
    )
    db.commit()
    return jsonify({"ok": True}), 201


@app.patch("/api/admin/users/<int:user_id>")
@roles("admin")
def set_user_active(user_id):
    active = body_json().get("active")
    if not isinstance(active, bool):
        return error("Set active to true or false.")
    if user_id == g.user["id"]:
        return error("You cannot disable your own admin account.")
    cur = get_db().execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
    get_db().commit()
    if not cur.rowcount:
        return error("Account not found.", 404)
    return jsonify({"active": active})


@app.patch("/api/admin/reports/<int:report_id>")
@roles("admin")
def resolve_report(report_id):
    status = body_json().get("status")
    if status not in {"open", "resolved"}:
        return error("Choose open or resolved.")
    cur = get_db().execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))
    get_db().commit()
    if not cur.rowcount:
        return error("Report not found.", 404)
    return jsonify({"status": status})


@app.get("/api/route")
@roles("patient")
def route():
    start = valid_coords(request.args.get("start_lat"), request.args.get("start_lon"))
    end = valid_coords(request.args.get("end_lat"), request.args.get("end_lon"))
    profile = request.args.get("profile", "driving")
    if not start or not end:
        return error("Valid origin and destination coordinates are required.")
    if profile != "driving":
        return error("The configured public OSRM service provides driving routes only. Use Google Maps for other travel modes.", 422)
    coordinates = f"{start[1]},{start[0]};{end[1]},{end[0]}"
    url = f"https://router.project-osrm.org/route/v1/driving/{coordinates}?" + urllib.parse.urlencode(
        {"overview": "full", "geometries": "geojson", "steps": "false"}
    )
    try:
        result = http_json(url, timeout=12)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return error("Road routing service is temporarily unavailable. Google Maps navigation is still available.", 502)
    if result.get("code") != "Ok" or not result.get("routes"):
        return error("No driving route was found for these locations. Try Google Maps navigation.", 404)
    route_data = result["routes"][0]
    return jsonify({
        "distance_km": round(route_data["distance"] / 1000, 1),
        "duration_minutes": round(route_data["duration"] / 60),
        "geometry": route_data["geometry"],
        "source": "OSRM public routing service",
    })


@app.cli.command("init-db")
def init_db_command():
    init_db()
    print(f"SQLite database initialized: {DATABASE}")


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
