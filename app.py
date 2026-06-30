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

START_DATE = date(2026, 6, 27)
END_DATE   = date(2026, 7, 3)

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

# الخيارات الافتراضية (أداء / قضاء / غرامة) — قابلة للتعديل بالكامل من لوحة الـ owner
DEFAULT_STATUS_OPTIONS = [
    {"code": "ada2",    "label": "أداء",   "value": "0",  "color": "#28a745", "order_num": 0},
    {"code": "qadaa",   "label": "قضاء",   "value": "0",  "color": "#ffc107", "order_num": 1},
    {"code": "gharama", "label": "غرامة",  "value": "20", "color": "#dc3545", "order_num": 2},
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
            child_name TEXT DEFAULT ''
        )
    """)

    conn.run("""
        CREATE TABLE IF NOT EXISTS wirds (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            order_num INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
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
            code TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '0',
            color TEXT NOT NULL DEFAULT '#888888',
            order_num INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.run("""
        CREATE TABLE IF NOT EXISTS site_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            site_name TEXT NOT NULL DEFAULT 'متابعة الأوراد',
            logo_data TEXT DEFAULT '',
            welcome_message TEXT DEFAULT 'نشكركم على متابعتكم ومتابعة أبنائكم في أداء الأوراد اليومية',
            CHECK (id = 1)
        )
    """)

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

    # خيارات الحالة الافتراضية
    r = qone(conn, "SELECT COUNT(*) as cnt FROM status_options")
    if r and r["cnt"] == 0:
        for opt in DEFAULT_STATUS_OPTIONS:
            qrun(conn, """
                INSERT INTO status_options (code, label, value, color, order_num)
                VALUES (:p1, :p2, :p3, :p4, :p5)
            """, (opt["code"], opt["label"], opt["value"], opt["color"], opt["order_num"]))

    # إعدادات الموقع
    r = qone(conn, "SELECT COUNT(*) as cnt FROM site_settings")
    if r and r["cnt"] == 0:
        qrun(conn, "INSERT INTO site_settings (id, site_name) VALUES (1, 'متابعة الأوراد')")

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

def get_period_days():
    days = []
    current = START_DATE
    while current <= END_DATE:
        days.append(current)
        current += timedelta(days=1)
    return days

def get_site_settings(conn):
    s = qone(conn, "SELECT * FROM site_settings WHERE id=1")
    if not s:
        s = {"site_name": "متابعة الأوراد", "logo_data": "", "welcome_message": ""}
    return s

def get_status_options(conn):
    return qall(conn, "SELECT * FROM status_options WHERE active=1 ORDER BY order_num")

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
        parent_name = request.form.get("parent_name", "").strip()
        phone       = request.form.get("phone", "").strip()
        child_name  = request.form.get("child_name", "").strip()
        username    = request.form.get("username", "").strip()
        password    = request.form.get("password", "")
        password2   = request.form.get("password2", "")

        if not all([parent_name, phone, child_name, username, password]):
            flash("من فضلك املأ كل الحقول", "error")
        elif password != password2:
            flash("كلمة السر غير متطابقة", "error")
        elif len(password) < 4:
            flash("كلمة السر لازم تكون 4 حروف على الأقل", "error")
        else:
            try:
                qrun(conn, """
                    INSERT INTO users (username, password, plain_password, role, parent_name, phone, child_name)
                    VALUES (:p1, :p2, :p3, 'parent', :p4, :p5, :p6)
                """, (username, generate_password_hash(password), password, parent_name, phone, child_name))
                flash("تم إنشاء الحساب بنجاح! يمكنك تسجيل الدخول الآن ✅", "success")
                conn.close()
                return redirect(url_for("login"))
            except Exception:
                flash("اسم المستخدم ده مستخدم بالفعل، اختار اسم تاني", "error")

    conn.close()
    return render_template("register.html", settings=settings)


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
    today       = date.today()
    period_days = get_period_days()

    conn     = get_db()
    settings = get_site_settings(conn)
    options  = get_status_options(conn)

    selected_str = request.args.get("date", "")
    try:
        selected_date = date.fromisoformat(selected_str)
        if not (START_DATE <= selected_date <= END_DATE):
            raise ValueError
    except ValueError:
        selected_date = max(START_DATE, min(today, END_DATE))

    wirds = qall(conn, "SELECT * FROM wirds WHERE active=1 ORDER BY order_num")

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

            valid_codes = {o["code"] for o in options}
            for wird in wirds:
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

    opt_by_code = {o["code"]: o for o in options}

    stats = []
    for d in period_days:
        day_rows = qall(conn,
            "SELECT wird_id, status_code FROM records WHERE user_id=:p1 AND record_date=:p2",
            (session["user_id"], d.isoformat()))
        day_rec = {r["wird_id"]: r["status_code"] for r in day_rows}
        counts = {o["code"]: 0 for o in options}
        for v in day_rec.values():
            if v in counts:
                counts[v] += 1
        stats.append({
            "date": d, "records": day_rec, "counts": counts,
            "missing": len(wirds) - len(day_rec),
        })

    conn.close()
    return render_template("parent_dashboard.html",
                           settings=settings, wirds=wirds, options=options,
                           opt_by_code=opt_by_code,
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
    options     = get_status_options(conn)
    users       = qall(conn, "SELECT * FROM users WHERE role='parent'")
    wirds       = qall(conn, "SELECT * FROM wirds WHERE active=1 ORDER BY order_num")
    period_days = get_period_days()

    report = []
    for user in users:
        user_data = {
            "username": user["username"],
            "parent_name": user.get("parent_name") or user["username"],
            "child_name": user.get("child_name") or "—",
            "days": [],
        }
        totals = {o["code"]: 0 for o in options}
        total_missing = 0
        for d in period_days:
            rows = qall(conn,
                "SELECT wird_id, status_code FROM records WHERE user_id=:p1 AND record_date=:p2",
                (user["id"], d.isoformat()))
            day_rec = {r["wird_id"]: r["status_code"] for r in rows}
            counts = {o["code"]: 0 for o in options}
            for v in day_rec.values():
                if v in counts:
                    counts[v] += 1
            missing = len(wirds) - len(day_rec)
            for k in totals:
                totals[k] += counts[k]
            total_missing += missing
            user_data["days"].append({"date": d, "records": day_rec, "counts": counts, "missing": missing})

        total_wirds = len(wirds) * len(period_days)
        ada2_total = totals.get("ada2", 0)
        user_data["totals"] = totals
        user_data["total_missing"] = total_missing
        user_data["completion_pct"] = round(ada2_total / total_wirds * 100) if total_wirds else 0
        report.append(user_data)

    daily_reports = []
    for d in period_days:
        day_data = {"date": d, "users": [], "totals": {o["code"]: 0 for o in options}, "total_missing": 0}
        for user in users:
            rows = qall(conn,
                "SELECT wird_id, status_code FROM records WHERE user_id=:p1 AND record_date=:p2",
                (user["id"], d.isoformat()))
            day_rec = {r["wird_id"]: r["status_code"] for r in rows}
            counts = {o["code"]: 0 for o in options}
            for v in day_rec.values():
                if v in counts:
                    counts[v] += 1
            missing = len(wirds) - len(day_rec)
            day_data["users"].append({
                "username": user["username"],
                "parent_name": user.get("parent_name") or user["username"],
                "child_name": user.get("child_name") or "—",
                "records": day_rec, "counts": counts, "missing": missing,
            })
            for k in day_data["totals"]:
                day_data["totals"][k] += counts[k]
            day_data["total_missing"] += missing
        daily_reports.append(day_data)

    conn.close()
    return render_template("admin_dashboard.html",
                           settings=settings, options=options,
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

        # ── إدارة المستخدمين ──
        elif action == "add_user":
            uname = request.form.get("username", "").strip()
            pw    = request.form.get("password", "")
            role  = request.form.get("role", "parent")
            pname = request.form.get("parent_name", "").strip()
            phone = request.form.get("phone", "").strip()
            cname = request.form.get("child_name", "").strip()
            if uname and pw and role in ("parent", "admin"):
                try:
                    qrun(conn, """
                        INSERT INTO users (username, password, plain_password, role, parent_name, phone, child_name)
                        VALUES (:p1,:p2,:p3,:p4,:p5,:p6,:p7)
                    """, (uname, generate_password_hash(pw), pw, role, pname, phone, cname))
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
            if wname:
                r  = qone(conn, "SELECT MAX(order_num) as mx FROM wirds")
                mx = r["mx"] if r and r["mx"] is not None else 0
                qrun(conn, "INSERT INTO wirds (name, order_num) VALUES (:p1,:p2)", (wname, mx + 1))
                flash("تم إضافة الورد ✅", "success")

        elif action == "delete_wird":
            wid = request.form.get("wird_id")
            qrun(conn, "UPDATE wirds SET active=0 WHERE id=:p1", (wid,))
            flash("تم حذف الورد", "success")

        elif action == "edit_wird":
            wid   = request.form.get("wird_id")
            wname = request.form.get("wird_name", "").strip()
            if wid and wname:
                qrun(conn, "UPDATE wirds SET name=:p1 WHERE id=:p2", (wname, wid))
                flash("تم تعديل الورد ✅", "success")

        # ── إدارة خيارات الحالة (أداء/قضاء/غرامة) ──
        elif action == "add_status_option":
            label = request.form.get("status_label", "").strip()
            value = request.form.get("status_value", "0").strip()
            color = request.form.get("status_color", "#888888").strip()
            if label:
                code = "opt_" + str(abs(hash(label)) % 100000)
                r  = qone(conn, "SELECT MAX(order_num) as mx FROM status_options")
                mx = r["mx"] if r and r["mx"] is not None else 0
                try:
                    qrun(conn, """
                        INSERT INTO status_options (code, label, value, color, order_num)
                        VALUES (:p1,:p2,:p3,:p4,:p5)
                    """, (code, label, value, color, mx + 1))
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
            cnt = qone(conn, "SELECT COUNT(*) as cnt FROM status_options WHERE active=1")
            if cnt and cnt["cnt"] > 1:
                qrun(conn, "UPDATE status_options SET active=0 WHERE id=:p1", (oid,))
                flash("تم حذف الخيار", "success")
            else:
                flash("لازم يفضل خيار واحد على الأقل", "error")

        conn.close()
        return redirect(url_for("owner_dashboard"))

    settings = get_site_settings(conn)
    options  = qall(conn, "SELECT * FROM status_options WHERE active=1 ORDER BY order_num")
    users    = qall(conn, "SELECT * FROM users WHERE role != 'owner' ORDER BY role, username")
    wirds    = qall(conn, "SELECT * FROM wirds WHERE active=1 ORDER BY order_num")
    conn.close()
    return render_template("owner_dashboard.html",
                           settings=settings, options=options, users=users, wirds=wirds)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
