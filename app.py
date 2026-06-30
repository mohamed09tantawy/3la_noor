from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import pg8000.native
import os, base64
from datetime import date, timedelta
from functools import wraps
from urllib.parse import urlparse

app = Flask(__name__)
app.secret_key = "wird_tracker_v3_secret_key_2026"
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024  # 3MB max للوجو

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# قيم افتراضية تُستخدم أول مرة فقط عند إنشاء قاعدة البيانات
DEFAULT_START_DATE = date(2026, 6, 27)
DEFAULT_END_DATE   = date(2026, 7, 3)

DEFAULT_WIRDS = [
    "سنة المغرب البعدية ركعتان",
    "سنة العشاء البعدية ركعتان",
    "قيام الليل بعشر آيات",
    "خمس دقائق دعاء",
    "100 صلاة على النبي ﷺ",
    "100 استغفار",
    "ركعتا الضحى",
    "قراءة جزء من القرآن",
    "سنة الظهر القبلية 4 ركعات",
    "سنة الظهر البعدية ركعتان",
]

# مجموعة الخيارات الافتراضية (أداء / قضاء / غرامة) — تُنشأ تلقائياً أول مرة
DEFAULT_GROUP_NAME = "افتراضية (أداء/قضاء/غرامة)"
DEFAULT_STATUS_OPTIONS = [
    {"code": "ada2",    "label": "أداء",   "value": "0",  "color": "#28a745", "order_num": 0},
    {"code": "qadaa",   "label": "قضاء",   "value": "0",  "color": "#ffc107", "order_num": 1},
    {"code": "gharama", "label": "غرامة",  "value": "20", "color": "#dc3545", "order_num": 2},
]

# المراحل الدراسية المتاحة عند تسجيل ولي الأمر
SCHOOL_STAGES = [
    {"code": "prep",      "label": "إعدادي"},
    {"code": "secondary", "label": "ثانوي"},
]


# ────────────────────────────────────────────────────────────────
# قاعدة البيانات - pg8000.native
# ────────────────────────────────────────────────────────────────

def get_db():
    p = urlparse(DATABASE_URL)
    conn = pg8000.native.Connection(
        host=p.hostname,
        port=p.port or 5432,
        database=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
        ssl_context=True,
    )
    return conn

def qone(conn, sql, params=None):
    if params:
        rows = conn.run(sql, **{f"p{i+1}": v for i, v in enumerate(params)})
    else:
        rows = conn.run(sql)
    if not rows:
        return None
    cols = [c["name"] for c in conn.columns]
    return dict(zip(cols, rows[0]))

def qall(conn, sql, params=None):
    if params:
        rows = conn.run(sql, **{f"p{i+1}": v for i, v in enumerate(params)})
    else:
        rows = conn.run(sql)
    if not rows:
        return []
    cols = [c["name"] for c in conn.columns]
    return [dict(zip(cols, r)) for r in rows]

def qrun(conn, sql, params=None):
    if params:
        conn.run(sql, **{f"p{i+1}": v for i, v in enumerate(params)})
    else:
        conn.run(sql)


def init_db():
    conn = get_db()

    conn.run("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            plain_password TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'parent',
            parent_name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            child_name TEXT DEFAULT '',
            school_stage TEXT DEFAULT ''
        )
    """)

    conn.run("""
        CREATE TABLE IF NOT EXISTS status_groups (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            order_num INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.run("""
        CREATE TABLE IF NOT EXISTS wirds (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            order_num INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            group_id INTEGER
        )
    """)

    conn.run("""
        CREATE TABLE IF NOT EXISTS records (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            wird_id INTEGER NOT NULL,
            record_date TEXT NOT NULL,
            status_code TEXT NOT NULL,
            UNIQUE(user_id, wird_id, record_date)
        )
    """)

    conn.run("""
        CREATE TABLE IF NOT EXISTS status_options (
            id SERIAL PRIMARY KEY,
            group_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            label TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '0',
            color TEXT NOT NULL DEFAULT '#888888',
            order_num INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(group_id, code)
        )
    """)

    # ترقية: لو الجداول كانت موجودة قبل كده بهيكل قديم (wird_id مباشرة، أو code بمفرده unique)
    try:
        conn.run("ALTER TABLE wirds ADD COLUMN IF NOT EXISTS group_id INTEGER")
    except Exception:
        pass
    try:
        conn.run("ALTER TABLE status_options ADD COLUMN IF NOT EXISTS group_id INTEGER")
    except Exception:
        pass
    try:
        conn.run("ALTER TABLE users ADD COLUMN IF NOT EXISTS school_stage TEXT DEFAULT ''")
    except Exception:
        pass

    # إزالة أي قيود uniqueness قديمة على status_options كانت من نسخ سابقة (code لوحده، أو wird_id+code)
    old_constraints = qall(conn, """
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'status_options'::regclass AND contype = 'u'
    """)
    for c in old_constraints:
        cname = c["conname"]
        if cname != "status_options_group_id_code_key":
            try:
                conn.run(f'ALTER TABLE status_options DROP CONSTRAINT "{cname}"')
            except Exception:
                pass

    # تأكد من وجود القيد الصحيح (group_id, code)
    has_correct = qone(conn, """
        SELECT COUNT(*) as cnt FROM pg_constraint
        WHERE conrelid = 'status_options'::regclass
          AND contype = 'u' AND conname = 'status_options_group_id_code_key'
    """)
    if not has_correct or has_correct["cnt"] == 0:
        try:
            conn.run("ALTER TABLE status_options ADD CONSTRAINT status_options_group_id_code_key UNIQUE (group_id, code)")
        except Exception:
            pass

    conn.run("""
        CREATE TABLE IF NOT EXISTS site_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            site_name TEXT NOT NULL DEFAULT 'متابعة الأوراد',
            logo_data TEXT DEFAULT '',
            welcome_message TEXT DEFAULT 'نشكركم على متابعتكم ومتابعة أبنائكم في أداء الأوراد اليومية',
            start_date TEXT DEFAULT '',
            end_date TEXT DEFAULT '',
            CHECK (id = 1)
        )
    """)

    for col, coltype in [
        ("start_date", "TEXT DEFAULT ''"),
        ("end_date", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.run(f"ALTER TABLE site_settings ADD COLUMN IF NOT EXISTS {col} {coltype}")
        except Exception:
            pass

    # ترقية: أعمدة جديدة لو الجدول كان موجود من قبل بدون الأعمدة دي
    for col, coltype in [
        ("parent_name", "TEXT DEFAULT ''"),
        ("phone", "TEXT DEFAULT ''"),
        ("child_name", "TEXT DEFAULT ''"),
        ("plain_password", "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            conn.run(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {coltype}")
        except Exception:
            pass

    try:
        conn.run("ALTER TABLE records ADD COLUMN IF NOT EXISTS status_code TEXT")
    except Exception:
        pass

    # الأوراد الافتراضية
    r = qone(conn, "SELECT COUNT(*) as cnt FROM wirds")
    if r and r["cnt"] == 0:
        for i, w in enumerate(DEFAULT_WIRDS):
            qrun(conn, "INSERT INTO wirds (name, order_num) VALUES (:p1, :p2)", (w, i))

    # مجموعة الخيارات الافتراضية — تُنشأ مرة واحدة فقط
    default_group = qone(conn, "SELECT id FROM status_groups WHERE name=:p1", (DEFAULT_GROUP_NAME,))
    if not default_group:
        qrun(conn, "INSERT INTO status_groups (name, order_num) VALUES (:p1, 0)", (DEFAULT_GROUP_NAME,))
        default_group = qone(conn, "SELECT id FROM status_groups WHERE name=:p1", (DEFAULT_GROUP_NAME,))
    default_group_id = default_group["id"]

    r = qone(conn, "SELECT COUNT(*) as cnt FROM status_options WHERE group_id=:p1", (default_group_id,))
    if r and r["cnt"] == 0:
        for opt in DEFAULT_STATUS_OPTIONS:
            qrun(conn, """
                INSERT INTO status_options (group_id, code, label, value, color, order_num)
                VALUES (:p1, :p2, :p3, :p4, :p5, :p6)
                ON CONFLICT (group_id, code) DO NOTHING
            """, (default_group_id, opt["code"], opt["label"], opt["value"], opt["color"], opt["order_num"]))

    # أي ورد من غير مجموعة (سواء جديد أو من ترقية قديمة) يتربط بالمجموعة الافتراضية
    qrun(conn, "UPDATE wirds SET group_id=:p1 WHERE group_id IS NULL", (default_group_id,))

    # إعدادات الموقع
    r = qone(conn, "SELECT COUNT(*) as cnt FROM site_settings")
    if r and r["cnt"] == 0:
        qrun(conn, """
            INSERT INTO site_settings (id, site_name, start_date, end_date)
            VALUES (1, 'متابعة الأوراد', :p1, :p2)
        """, (DEFAULT_START_DATE.isoformat(), DEFAULT_END_DATE.isoformat()))
    else:
        # لو الصف موجود بس التواريخ فاضية (ترقية من نسخة قديمة) املأها بالافتراضي
        s = qone(conn, "SELECT start_date, end_date FROM site_settings WHERE id=1")
        if s and (not s.get("start_date") or not s.get("end_date")):
            qrun(conn, """
                UPDATE site_settings SET start_date=:p1, end_date=:p2 WHERE id=1
            """, (DEFAULT_START_DATE.isoformat(), DEFAULT_END_DATE.isoformat()))

    # حساب الـ owner
    r = qone(conn, "SELECT COUNT(*) as cnt FROM users WHERE role='owner'")
    if r and r["cnt"] == 0:
        qrun(conn,
            "INSERT INTO users (username, password, plain_password, role) VALUES (:p1,:p2,:p3,:p4)",
            ("owner", generate_password_hash("owner123"), "owner123", "owner")
        )

    conn.close()


# ────────────────────────────────────────────────────────────────
# Decorators
# ────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if session.get("role") not in roles:
                flash("مش عندك صلاحية تدخل هنا", "error")
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated
    return decorator


# ────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────

def get_period_days(start_d, end_d):
    days = []
    current = start_d
    while current <= end_d:
        days.append(current)
        current += timedelta(days=1)
    return days

def get_site_settings(conn):
    s = qone(conn, "SELECT * FROM site_settings WHERE id=1")
    if not s:
        s = {"site_name": "متابعة الأوراد", "logo_data": "", "welcome_message": "",
             "start_date": DEFAULT_START_DATE.isoformat(), "end_date": DEFAULT_END_DATE.isoformat()}
    if not s.get("start_date"):
        s["start_date"] = DEFAULT_START_DATE.isoformat()
    if not s.get("end_date"):
        s["end_date"] = DEFAULT_END_DATE.isoformat()
    return s

def get_period_dates(settings):
    """يحول التواريخ المخزّنة كنص إلى date objects، مع رجوع آمن للقيم الافتراضية"""
    try:
        sd = date.fromisoformat(settings["start_date"])
    except Exception:
        sd = DEFAULT_START_DATE
    try:
        ed = date.fromisoformat(settings["end_date"])
    except Exception:
        ed = DEFAULT_END_DATE
    if ed < sd:
        ed = sd
    return sd, ed

def get_status_groups(conn):
    return qall(conn, "SELECT * FROM status_groups WHERE active=1 ORDER BY order_num")

def get_group_options(conn, group_id):
    return qall(conn, "SELECT * FROM status_options WHERE active=1 AND group_id=:p1 ORDER BY order_num", (group_id,))

def get_wirds_with_options(conn):
    """يرجع كل الأوراد، كل ورد معاه خياراته الخاصة (حسب المجموعة المربوط بيها)"""
    wirds = qall(conn, "SELECT * FROM wirds WHERE active=1 ORDER BY order_num")
    groups_cache = {}
    for w in wirds:
        gid = w.get("group_id")
        if gid not in groups_cache:
            groups_cache[gid] = qall(conn,
                "SELECT * FROM status_options WHERE active=1 AND group_id=:p1 ORDER BY order_num", (gid,))
        w["options"] = groups_cache[gid]
    return wirds

def inject_globals():
    """يُستخدم لحقن إعدادات الموقع في كل الصفحات تلقائياً"""
    conn = get_db()
    settings = get_site_settings(conn)
    conn.close()
    return settings

app.jinja_env.globals.update(get_site_settings=lambda: inject_globals())


# ────────────────────────────────────────────────────────────────
# Routes - عامة
# ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" in session:
        role = session.get("role")
        if role == "owner":   return redirect(url_for("owner_dashboard"))
        elif role == "admin": return redirect(url_for("admin_dashboard"))
        else:                 return redirect(url_for("parent_dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    settings = get_site_settings(conn)
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = qone(conn, "SELECT * FROM users WHERE username=:p1", (username,))
        conn.close()
        if user and check_password_hash(user["password"], password):
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["role"]     = user["role"]
            session["parent_name"] = user.get("parent_name") or ""
            session["child_name"]  = user.get("child_name") or ""
            return redirect(url_for("index"))
        flash("اسم المستخدم أو كلمة السر غلط", "error")
        return render_template("login.html", settings=settings)
    conn.close()
    return render_template("login.html", settings=settings)


@app.route("/register", methods=["GET", "POST"])
def register():
    conn = get_db()
    settings = get_site_settings(conn)

    if request.method == "POST":
        parent_name  = request.form.get("parent_name", "").strip()
        phone        = request.form.get("phone", "").strip()
        child_name   = request.form.get("child_name", "").strip()
        school_stage = request.form.get("school_stage", "").strip()
        username     = request.form.get("username", "").strip()
        password     = request.form.get("password", "")
        password2    = request.form.get("password2", "")

        valid_stages = {s["code"] for s in SCHOOL_STAGES}

        if not all([parent_name, phone, child_name, username, password]) or school_stage not in valid_stages:
            flash("من فضلك املأ كل الحقول واختار المرحلة الدراسية", "error")
        elif password != password2:
            flash("كلمة السر غير متطابقة", "error")
        elif len(password) < 4:
            flash("كلمة السر لازم تكون 4 حروف على الأقل", "error")
        else:
            try:
                qrun(conn, """
                    INSERT INTO users (username, password, plain_password, role, parent_name, phone, child_name, school_stage)
                    VALUES (:p1, :p2, :p3, 'parent', :p4, :p5, :p6, :p7)
                """, (username, generate_password_hash(password), password, parent_name, phone, child_name, school_stage))
                flash("تم إنشاء الحساب بنجاح! يمكنك تسجيل الدخول الآن ✅", "success")
                conn.close()
                return redirect(url_for("login"))
            except Exception:
                flash("اسم المستخدم ده مستخدم بالفعل، اختار اسم تاني", "error")

    conn.close()
    return render_template("register.html", settings=settings, school_stages=SCHOOL_STAGES)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ────────────────────────────────────────────────────────────────
# صفحة ولي الأمر (parent)
# ────────────────────────────────────────────────────────────────

@app.route("/parent", methods=["GET", "POST"])
@login_required
@role_required("parent")
def parent_dashboard():
    today = date.today()

    conn     = get_db()
    settings = get_site_settings(conn)
    START_DATE, END_DATE = get_period_dates(settings)
    period_days = get_period_days(START_DATE, END_DATE)

    selected_str = request.args.get("date", "")
    try:
        selected_date = date.fromisoformat(selected_str)
        if not (START_DATE <= selected_date <= END_DATE):
            raise ValueError
    except ValueError:
        selected_date = max(START_DATE, min(today, END_DATE))

    wirds = get_wirds_with_options(conn)
    # كل أكواد الحالة الموجودة في أي مجموعة (لاستخدامها في الإحصائيات وألوانها)
    all_options = qall(conn, "SELECT * FROM status_options WHERE active=1 ORDER BY order_num")
    opt_by_code = {}
    for o in all_options:
        if o["code"] not in opt_by_code:
            opt_by_code[o["code"]] = o

    if request.method == "POST":
        action = request.form.get("action", "save_wirds")

        if action == "change_password":
            old_pw  = request.form.get("old_password", "")
            new_pw  = request.form.get("new_password", "")
            new_pw2 = request.form.get("new_password2", "")
            user = qone(conn, "SELECT * FROM users WHERE id=:p1", (session["user_id"],))
            if not check_password_hash(user["password"], old_pw):
                flash("كلمة السر القديمة غلط ❌", "error")
            elif new_pw != new_pw2:
                flash("كلمة السر الجديدة مش متطابقة ❌", "error")
            elif len(new_pw) < 4:
                flash("كلمة السر لازم تكون 4 حروف على الأقل", "error")
            else:
                qrun(conn,
                    "UPDATE users SET password=:p1, plain_password=:p2 WHERE id=:p3",
                    (generate_password_hash(new_pw), new_pw, session["user_id"])
                )
                flash("تم تغيير كلمة السر بنجاح ✅", "success")

        else:
            rec_date_str = request.form.get("record_date", selected_date.isoformat())
            try:
                rec_date = date.fromisoformat(rec_date_str)
                if not (START_DATE <= rec_date <= END_DATE):
                    raise ValueError
            except ValueError:
                rec_date = selected_date

            for wird in wirds:
                valid_codes = {o["code"] for o in wird["options"]}
                status = request.form.get(f"wird_{wird['id']}")
                if status in valid_codes:
                    qrun(conn, """
                        INSERT INTO records (user_id, wird_id, record_date, status_code)
                        VALUES (:p1, :p2, :p3, :p4)
                        ON CONFLICT (user_id, wird_id, record_date)
                        DO UPDATE SET status_code=EXCLUDED.status_code
                    """, (session["user_id"], wird["id"], rec_date.isoformat(), status))
            flash(f"تم حفظ أوراد {rec_date.strftime('%d/%m')} ✅", "success")
            conn.close()
            return redirect(url_for("parent_dashboard", date=rec_date.isoformat()))

    records_rows = qall(conn,
        "SELECT wird_id, status_code FROM records WHERE user_id=:p1 AND record_date=:p2",
        (session["user_id"], selected_date.isoformat()))
    records_selected = {r["wird_id"]: r["status_code"] for r in records_rows}

    stats = []
    for d in period_days:
        day_rows = qall(conn,
            "SELECT wird_id, status_code FROM records WHERE user_id=:p1 AND record_date=:p2",
            (session["user_id"], d.isoformat()))
        day_rec = {r["wird_id"]: r["status_code"] for r in day_rows}
        counts = {}
        for v in day_rec.values():
            counts[v] = counts.get(v, 0) + 1
        # عدد الأوراد اللي اتسجلت بحالة تعتبر "أداء" (أول خيار في مجموعتها) — لتقدير نسبة الإنجاز
        done_as_first = 0
        for w in wirds:
            if w["options"]:
                first_code = w["options"][0]["code"]
                if day_rec.get(w["id"]) == first_code:
                    done_as_first += 1
        stats.append({
            "date": d, "records": day_rec, "counts": counts,
            "done_count": len(day_rec), "ada2_like": done_as_first,
            "missing": len(wirds) - len(day_rec),
        })

    conn.close()
    return render_template("parent_dashboard.html",
                           settings=settings, wirds=wirds, opt_by_code=opt_by_code,
                           records_selected=records_selected,
                           selected_date=selected_date, today=today,
                           stats=stats, START_DATE=START_DATE, END_DATE=END_DATE)


# ────────────────────────────────────────────────────────────────
# صفحة الأدمن
# ────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
@role_required("admin")
def admin_dashboard():
    conn        = get_db()
    settings    = get_site_settings(conn)
    START_DATE, END_DATE = get_period_dates(settings)
    users       = qall(conn, "SELECT * FROM users WHERE role='parent'")
    wirds       = get_wirds_with_options(conn)
    period_days = get_period_days(START_DATE, END_DATE)

    all_options = qall(conn, "SELECT * FROM status_options WHERE active=1 ORDER BY order_num")
    opt_by_code = {}
    for o in all_options:
        if o["code"] not in opt_by_code:
            opt_by_code[o["code"]] = o
    stage_label = {s["code"]: s["label"] for s in SCHOOL_STAGES}

    report = []
    for user in users:
        user_data = {
            "username": user["username"],
            "parent_name": user.get("parent_name") or user["username"],
            "child_name": user.get("child_name") or "—",
            "school_stage_label": stage_label.get(user.get("school_stage"), "—"),
            "days": [],
        }
        totals = {}
        total_missing = 0
        total_done = 0
        for d in period_days:
            rows = qall(conn,
                "SELECT wird_id, status_code FROM records WHERE user_id=:p1 AND record_date=:p2",
                (user["id"], d.isoformat()))
            day_rec = {r["wird_id"]: r["status_code"] for r in rows}
            counts = {}
            for v in day_rec.values():
                counts[v] = counts.get(v, 0) + 1
            missing = len(wirds) - len(day_rec)
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v
            total_missing += missing
            total_done += len(day_rec)
            user_data["days"].append({"date": d, "records": day_rec, "counts": counts, "missing": missing})

        total_wirds = len(wirds) * len(period_days)
        user_data["totals"] = totals
        user_data["total_missing"] = total_missing
        user_data["completion_pct"] = round(total_done / total_wirds * 100) if total_wirds else 0
        report.append(user_data)

    daily_reports = []
    for d in period_days:
        day_data = {"date": d, "users": [], "totals": {}, "total_missing": 0}
        for user in users:
            rows = qall(conn,
                "SELECT wird_id, status_code FROM records WHERE user_id=:p1 AND record_date=:p2",
                (user["id"], d.isoformat()))
            day_rec = {r["wird_id"]: r["status_code"] for r in rows}
            counts = {}
            for v in day_rec.values():
                counts[v] = counts.get(v, 0) + 1
            missing = len(wirds) - len(day_rec)
            day_data["users"].append({
                "username": user["username"],
                "parent_name": user.get("parent_name") or user["username"],
                "child_name": user.get("child_name") or "—",
                "school_stage_label": stage_label.get(user.get("school_stage"), "—"),
                "records": day_rec, "counts": counts, "missing": missing,
            })
            for k, v in counts.items():
                day_data["totals"][k] = day_data["totals"].get(k, 0) + v
            day_data["total_missing"] += missing
        daily_reports.append(day_data)

    conn.close()
    return render_template("admin_dashboard.html",
                           settings=settings, opt_by_code=opt_by_code,
                           report=report, daily_reports=daily_reports,
                           wirds=wirds, START_DATE=START_DATE, END_DATE=END_DATE)


# ────────────────────────────────────────────────────────────────
# صفحة صاحب النظام (owner)
# ────────────────────────────────────────────────────────────────

@app.route("/owner", methods=["GET", "POST"])
@login_required
@role_required("owner")
def owner_dashboard():
    conn = get_db()

    if request.method == "POST":
        action = request.form.get("action")

        # ── إدارة الموقع (الاسم واللوجو) ──
        if action == "update_site":
            site_name = request.form.get("site_name", "").strip()
            welcome   = request.form.get("welcome_message", "").strip()
            logo_file = request.files.get("logo_file")

            updates = {}
            if site_name:
                updates["site_name"] = site_name
            if welcome:
                updates["welcome_message"] = welcome

            if logo_file and logo_file.filename:
                ext = logo_file.filename.rsplit(".", 1)[-1].lower()
                if ext in ("png", "jpg", "jpeg", "svg", "webp"):
                    raw = logo_file.read()
                    mime = {"png": "image/png", "jpg": "image/jpeg",
                            "jpeg": "image/jpeg", "svg": "image/svg+xml",
                            "webp": "image/webp"}[ext]
                    b64 = base64.b64encode(raw).decode("ascii")
                    updates["logo_data"] = f"data:{mime};base64,{b64}"
                else:
                    flash("امتداد الصورة غير مدعوم، استخدم PNG أو JPG أو SVG", "error")

            if updates:
                set_clause = ", ".join(f"{k}=:p{i+1}" for i, k in enumerate(updates.keys()))
                qrun(conn, f"UPDATE site_settings SET {set_clause} WHERE id=1", tuple(updates.values()))
                flash("تم تحديث إعدادات الموقع ✅", "success")

        elif action == "remove_logo":
            qrun(conn, "UPDATE site_settings SET logo_data='' WHERE id=1")
            flash("تم حذف اللوجو", "success")

        elif action == "update_period":
            new_start = request.form.get("start_date", "").strip()
            new_end   = request.form.get("end_date", "").strip()
            try:
                sd = date.fromisoformat(new_start)
                ed = date.fromisoformat(new_end)
                if ed < sd:
                    flash("تاريخ النهاية لازم يكون بعد أو يساوي تاريخ البداية", "error")
                elif (ed - sd).days > 90:
                    flash("الفترة طويلة جداً، الحد الأقصى 90 يوم", "error")
                else:
                    qrun(conn, "UPDATE site_settings SET start_date=:p1, end_date=:p2 WHERE id=1",
                         (sd.isoformat(), ed.isoformat()))
                    flash(f"تم تحديث الفترة الزمنية: {sd.strftime('%d/%m/%Y')} – {ed.strftime('%d/%m/%Y')} ✅", "success")
            except ValueError:
                flash("التاريخ غير صحيح", "error")

        # ── إدارة المستخدمين ──
        elif action == "add_user":
            uname  = request.form.get("username", "").strip()
            pw     = request.form.get("password", "")
            role   = request.form.get("role", "parent")
            pname  = request.form.get("parent_name", "").strip()
            phone  = request.form.get("phone", "").strip()
            cname  = request.form.get("child_name", "").strip()
            stage  = request.form.get("school_stage", "").strip()
            if uname and pw and role in ("parent", "admin"):
                try:
                    qrun(conn, """
                        INSERT INTO users (username, password, plain_password, role, parent_name, phone, child_name, school_stage)
                        VALUES (:p1,:p2,:p3,:p4,:p5,:p6,:p7,:p8)
                    """, (uname, generate_password_hash(pw), pw, role, pname, phone, cname, stage))
                    flash(f"تم إضافة '{uname}' ✅", "success")
                except Exception:
                    flash("الاسم ده موجود بالفعل", "error")

        elif action == "delete_user":
            uid = request.form.get("user_id")
            qrun(conn, "DELETE FROM users WHERE id=:p1 AND role != 'owner'", (uid,))
            flash("تم حذف المستخدم", "success")

        elif action == "reset_password":
            uid    = request.form.get("user_id")
            new_pw = request.form.get("new_password", "").strip()
            if new_pw and len(new_pw) >= 4:
                qrun(conn,
                    "UPDATE users SET password=:p1, plain_password=:p2 WHERE id=:p3 AND role != 'owner'",
                    (generate_password_hash(new_pw), new_pw, uid)
                )
                flash("تم تغيير كلمة السر ✅", "success")
            else:
                flash("كلمة السر لازم تكون 4 حروف على الأقل", "error")

        # ── إدارة الأوراد ──
        elif action == "add_wird":
            wname = request.form.get("wird_name", "").strip()
            gid   = request.form.get("group_id", "").strip()
            if wname and gid:
                r  = qone(conn, "SELECT MAX(order_num) as mx FROM wirds")
                mx = r["mx"] if r and r["mx"] is not None else 0
                qrun(conn, "INSERT INTO wirds (name, order_num, group_id) VALUES (:p1,:p2,:p3)", (wname, mx + 1, gid))
                flash("تم إضافة الورد ✅", "success")
            else:
                flash("اختار اسم الورد ومجموعة الخيارات", "error")

        elif action == "delete_wird":
            wid = request.form.get("wird_id")
            qrun(conn, "UPDATE wirds SET active=0 WHERE id=:p1", (wid,))
            flash("تم حذف الورد", "success")

        elif action == "edit_wird":
            wid   = request.form.get("wird_id")
            wname = request.form.get("wird_name", "").strip()
            gid   = request.form.get("group_id", "").strip()
            if wid and wname and gid:
                qrun(conn, "UPDATE wirds SET name=:p1, group_id=:p2 WHERE id=:p3", (wname, gid, wid))
                flash("تم تعديل الورد ✅", "success")

        # ── إدارة مجموعات الخيارات ──
        elif action == "add_group":
            gname = request.form.get("group_name", "").strip()
            if gname:
                r  = qone(conn, "SELECT MAX(order_num) as mx FROM status_groups")
                mx = r["mx"] if r and r["mx"] is not None else 0
                qrun(conn, "INSERT INTO status_groups (name, order_num) VALUES (:p1,:p2)", (gname, mx + 1))
                flash(f"تم إنشاء مجموعة '{gname}' ✅ — دلوقتي ضيف لها خيارات", "success")

        elif action == "rename_group":
            gid   = request.form.get("group_id")
            gname = request.form.get("group_name", "").strip()
            if gid and gname:
                qrun(conn, "UPDATE status_groups SET name=:p1 WHERE id=:p2", (gname, gid))
                flash("تم تعديل اسم المجموعة ✅", "success")

        elif action == "delete_group":
            gid = request.form.get("group_id")
            in_use = qone(conn, "SELECT COUNT(*) as cnt FROM wirds WHERE group_id=:p1 AND active=1", (gid,))
            total_groups = qone(conn, "SELECT COUNT(*) as cnt FROM status_groups WHERE active=1")
            if in_use and in_use["cnt"] > 0:
                flash("مينفعش تحذف مجموعة مستخدمة في أوراد — غيّر مجموعة الأوراد دي الأول", "error")
            elif total_groups and total_groups["cnt"] <= 1:
                flash("لازم تفضل مجموعة واحدة على الأقل", "error")
            else:
                qrun(conn, "UPDATE status_groups SET active=0 WHERE id=:p1", (gid,))
                qrun(conn, "UPDATE status_options SET active=0 WHERE group_id=:p1", (gid,))
                flash("تم حذف المجموعة", "success")

        # ── إدارة خيارات داخل مجموعة (أداء/قضاء/غرامة وغيرها) ──
        elif action == "add_status_option":
            gid   = request.form.get("group_id", "").strip()
            label = request.form.get("status_label", "").strip()
            value = request.form.get("status_value", "0").strip()
            color = request.form.get("status_color", "#888888").strip()
            if label and gid:
                code = "opt_" + str(abs(hash(label + gid)) % 1000000)
                r  = qone(conn, "SELECT MAX(order_num) as mx FROM status_options WHERE group_id=:p1", (gid,))
                mx = r["mx"] if r and r["mx"] is not None else 0
                try:
                    qrun(conn, """
                        INSERT INTO status_options (group_id, code, label, value, color, order_num)
                        VALUES (:p1,:p2,:p3,:p4,:p5,:p6)
                    """, (gid, code, label, value, color, mx + 1))
                    flash("تم إضافة الخيار ✅", "success")
                except Exception:
                    flash("حصل خطأ، حاول تاني", "error")

        elif action == "edit_status_option":
            oid   = request.form.get("option_id")
            label = request.form.get("status_label", "").strip()
            value = request.form.get("status_value", "0").strip()
            color = request.form.get("status_color", "#888888").strip()
            if oid and label:
                qrun(conn, """
                    UPDATE status_options SET label=:p1, value=:p2, color=:p3 WHERE id=:p4
                """, (label, value, color, oid))
                flash("تم تعديل الخيار ✅", "success")

        elif action == "delete_status_option":
            oid = request.form.get("option_id")
            opt = qone(conn, "SELECT group_id FROM status_options WHERE id=:p1", (oid,))
            if opt:
                cnt = qone(conn, "SELECT COUNT(*) as cnt FROM status_options WHERE active=1 AND group_id=:p1",
                           (opt["group_id"],))
                if cnt and cnt["cnt"] > 1:
                    qrun(conn, "UPDATE status_options SET active=0 WHERE id=:p1", (oid,))
                    flash("تم حذف الخيار", "success")
                else:
                    flash("لازم يفضل خيار واحد على الأقل في كل مجموعة", "error")

        conn.close()
        return redirect(url_for("owner_dashboard"))

    settings = get_site_settings(conn)
    groups   = get_status_groups(conn)
    for g in groups:
        g["options"] = get_group_options(conn, g["id"])
    users    = qall(conn, "SELECT * FROM users WHERE role != 'owner' ORDER BY role, username")
    wirds    = qall(conn, "SELECT w.*, g.name as group_name FROM wirds w "
                          "LEFT JOIN status_groups g ON w.group_id = g.id "
                          "WHERE w.active=1 ORDER BY w.order_num")
    conn.close()
    return render_template("owner_dashboard.html",
                           settings=settings, groups=groups, users=users, wirds=wirds,
                           school_stages=SCHOOL_STAGES)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
