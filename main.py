import os
import base64
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session
import csv
import io
import zipfile
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_dance.contrib.google import make_google_blueprint, google
from flask_dance.contrib.github import make_github_blueprint, github
from flask_dance.consumer import oauth_authorized
from flask_wtf.csrf import CSRFProtect
import sqlite3
import pyotp
import qrcode
from datetime import datetime
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

if os.environ.get("RAILWAY_ENVIRONMENT") is None:
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-insecure-key')
csrf = CSRFProtect(app)

google_bp = make_google_blueprint(
    client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    scope=["https://www.googleapis.com/auth/userinfo.profile",
           "https://www.googleapis.com/auth/userinfo.email", "openid"]
)
app.register_blueprint(google_bp, url_prefix="/login")

github_bp = make_github_blueprint(
    client_id=os.environ.get("GITHUB_OAUTH_CLIENT_ID", ""),
    client_secret=os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", ""),
    scope="read:user,user:email"
)
app.register_blueprint(github_bp, url_prefix="/login")

login_manager = LoginManager(app)
login_manager.login_view = "login_page"

DATABASE = '/data/koltsegvetes.db' if os.environ.get('RAILWAY_ENVIRONMENT') else 'koltsegvetes.db'


class User(UserMixin):
    def __init__(self, id, name, email, theme='original', is_admin=False, totp_enabled=False):
        self.id = id
        self.name = name
        self.email = email
        self.theme = theme
        self.is_admin = is_admin
        self.totp_enabled = totp_enabled


def user_from_row(row):
    return User(
        row["id"], row["name"], row["email"],
        row["theme"] if row["theme"] else 'original',
        row["email"] in ADMIN_EMAILS,
        bool(row["totp_enabled"])
    )


@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if row:
        return user_from_row(row)
    return None


@app.before_request
def enforce_2fa():
    exempt = {
        'two_factor_verify', 'logout', 'static', 'login_page', 'privacy',
        'google.login', 'google.authorized',
        'github.login', 'github.authorized',
    }
    if (current_user.is_authenticated
            and session.get('needs_2fa')
            and request.endpoint not in exempt):
        return redirect(url_for('two_factor_verify'))


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def get_active_wallet(wallets):
    w = request.args.get('wallet', 'all')
    if w == 'all':
        return 'all'
    try:
        w = int(w)
        if any(wl['id'] == w for wl in wallets):
            return w
    except (ValueError, TypeError):
        pass
    return 'all'


def wallet_balance(conn, wallet_id):
    initial = conn.execute(
        "SELECT COALESCE(initial_balance, 0) FROM wallets WHERE id=?",
        (wallet_id,)).fetchone()[0]
    income = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='income' AND wallet_id=?",
        (wallet_id,)).fetchone()[0]
    expense = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='expense' AND wallet_id=?",
        (wallet_id,)).fetchone()[0]
    t_in = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transfers WHERE to_wallet_id=?",
        (wallet_id,)).fetchone()[0]
    t_out = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transfers WHERE from_wallet_id=?",
        (wallet_id,)).fetchone()[0]
    return initial + income - expense + t_in - t_out


def round_to_5(amount):
    n = round(amount)
    r = n % 5
    return n - r if r < 3 else n + (5 - r)


ADMIN_EMAILS = [e.strip() for e in os.environ.get('ADMIN_EMAILS', 'ligeti.karoly78@gmail.com').split(',')]

PLANS = {
    'free':         {'display': 'Zsebpénz',    'monthly_scans': 3,   'lifetime': True},
    'megtakaritor': {'display': 'Megtakarító', 'monthly_scans': 30,  'lifetime': False},
    'befekteto':    {'display': 'Befektető',   'monthly_scans': 100, 'lifetime': False},
}


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Nincs hozzáférésed ehhez az oldalhoz.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def get_scan_status(conn, user_id):
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    plan_key = user['plan'] or 'free'
    plan_expires_at = user['plan_expires_at']
    is_expired = bool(plan_expires_at and plan_expires_at < datetime.today().strftime('%Y-%m-%d'))
    if is_expired and plan_key != 'free':
        plan_key = 'free'
    plan = PLANS.get(plan_key, PLANS['free'])

    current_month = datetime.today().strftime('%Y-%m')
    scans_used = user['scans_used'] or 0
    extra_scans = user['extra_scans'] or 0

    if not plan['lifetime'] and user['scans_period'] != current_month:
        conn.execute("UPDATE users SET scans_used=0, scans_period=? WHERE id=?", (current_month, user_id))
        conn.commit()
        scans_used = 0

    monthly_limit = plan['monthly_scans']
    monthly_remaining = max(0, monthly_limit - scans_used)
    total_remaining = monthly_remaining + extra_scans
    soft_threshold = monthly_limit * 0.8
    at_soft_limit = scans_used >= soft_threshold and total_remaining > 0
    at_hard_limit = total_remaining == 0
    percent_used = min(100, int(scans_used / monthly_limit * 100)) if monthly_limit else 100

    return {
        'plan_key': plan_key,
        'plan_display': plan['display'],
        'monthly_limit': monthly_limit,
        'scans_used': scans_used,
        'extra_scans': extra_scans,
        'monthly_remaining': monthly_remaining,
        'total_remaining': total_remaining,
        'at_soft_limit': at_soft_limit,
        'at_hard_limit': at_hard_limit,
        'percent_used': percent_used,
        'plan_expires_at': plan_expires_at,
        'is_expired': is_expired,
    }


def increment_scan_used(conn, user_id, status):
    current_month = datetime.today().strftime('%Y-%m')
    if status['extra_scans'] > 0 and status['monthly_remaining'] == 0:
        conn.execute("UPDATE users SET extra_scans=extra_scans-1 WHERE id=?", (user_id,))
    else:
        conn.execute("UPDATE users SET scans_used=scans_used+1, scans_period=? WHERE id=?",
                     (current_month, user_id))
    conn.commit()


def setup_new_user(conn, user_id):
    conn.execute(
        "INSERT INTO wallets (user_id, name, description, initial_balance, sort_order, is_cash) VALUES (?, ?, ?, 0, ?, 1)",
        (user_id, 'Készpénz', '', 1))
    conn.execute(
        "INSERT INTO wallets (user_id, name, description, initial_balance, sort_order, is_cash) VALUES (?, ?, ?, 0, ?, 0)",
        (user_id, 'Bankszámla', '', 2))
    conn.executemany("INSERT INTO categories (user_id, name, type) VALUES (?, ?, ?)", [
        (user_id, 'Fizetés', 'income'),
        (user_id, 'Egyéb bevétel', 'income'),
        (user_id, 'Lakás', 'expense'),
        (user_id, 'Élelmiszer', 'expense'),
        (user_id, 'Közlekedés', 'expense'),
        (user_id, 'Szórakozás', 'expense'),
        (user_id, 'Egészség', 'expense'),
        (user_id, 'Egyéb kiadás', 'expense'),
    ])


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        google_id TEXT UNIQUE NOT NULL,
        name TEXT, email TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS wallets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        name TEXT NOT NULL,
        description TEXT,
        initial_balance REAL DEFAULT 0,
        sort_order INTEGER)''')

    c.execute('''CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        name TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('income', 'expense')))''')

    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount REAL NOT NULL,
        description TEXT,
        category_id INTEGER,
        type TEXT NOT NULL CHECK(type IN ('income', 'expense')),
        date TEXT NOT NULL,
        wallet_id INTEGER,
        FOREIGN KEY (category_id) REFERENCES categories(id),
        FOREIGN KEY (wallet_id) REFERENCES wallets(id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_wallet_id INTEGER NOT NULL,
        to_wallet_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        description TEXT,
        date TEXT NOT NULL,
        FOREIGN KEY (from_wallet_id) REFERENCES wallets(id),
        FOREIGN KEY (to_wallet_id) REFERENCES wallets(id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS transaction_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        FOREIGN KEY (transaction_id) REFERENCES transactions(id))''')

    c.execute('''CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        category_id INTEGER REFERENCES categories(id),
        monthly_limit REAL NOT NULL,
        UNIQUE(user_id, category_id))''')

    # Migrations
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN initial_balance REAL DEFAULT 0")
        c.execute("UPDATE wallets SET initial_balance = 0 WHERE initial_balance IS NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN sort_order INTEGER")
        c.execute("UPDATE wallets SET sort_order = id WHERE sort_order IS NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN user_id INTEGER REFERENCES users(id)")
        c.execute("UPDATE wallets SET user_id = 1 WHERE user_id IS NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE categories ADD COLUMN user_id INTEGER REFERENCES users(id)")
        c.execute("UPDATE categories SET user_id = 1 WHERE user_id IS NULL")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE transactions ADD COLUMN wallet_id INTEGER REFERENCES wallets(id)")
    except Exception:
        pass
    c.execute("UPDATE transactions SET wallet_id=1 WHERE wallet_id IS NULL")
    try:
        c.execute("ALTER TABLE transaction_items ADD COLUMN category_id INTEGER REFERENCES categories(id)")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE wallets ADD COLUMN is_cash INTEGER DEFAULT 0")
        c.execute("UPDATE wallets SET is_cash=1 WHERE name='Készpénz'")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN theme TEXT DEFAULT 'original'")
    except Exception:
        pass

    for col in [
        "plan TEXT DEFAULT 'free'",
        "plan_expires_at TEXT",
        "scans_used INTEGER DEFAULT 0",
        "scans_period TEXT",
        "extra_scans INTEGER DEFAULT 0",
        "github_id TEXT",
        "apple_id TEXT",
        "totp_secret TEXT",
        "totp_enabled INTEGER DEFAULT 0",
        "password_hash TEXT",
    ]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except Exception:
            pass

    conn.commit()
    conn.close()


# --- Privacy & Account ---

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/account')
@login_required
def account():
    conn = get_db()
    scan_status = get_scan_status(conn, current_user.id)
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (current_user.id,)).fetchone()
    has_password = bool(row and row['password_hash'])
    conn.close()
    return render_template('account.html', scan_status=scan_status, has_password=has_password)


@app.route('/account/theme', methods=['POST'])
@login_required
def update_theme():
    theme = request.form.get('theme', 'original')
    if theme not in ('original', 'blue', 'green', 'lavender'):
        theme = 'original'
    conn = get_db()
    conn.execute("UPDATE users SET theme=? WHERE id=?", (theme, current_user.id))
    conn.commit()
    conn.close()
    current_user.theme = theme
    flash('Téma sikeresen módosítva!', 'success')
    return redirect(url_for('account'))


@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    conn = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    current_month = datetime.today().strftime('%Y-%m')
    today_str = datetime.today().strftime('%Y-%m-%d')
    user_list = []
    for u in users:
        plan_key = u['plan'] or 'free'
        plan_expires_at = u['plan_expires_at']
        is_expired = bool(plan_expires_at and plan_expires_at < today_str)
        if is_expired and plan_key != 'free':
            plan_key = 'free'
        plan = PLANS.get(plan_key, PLANS['free'])
        scans_used = u['scans_used'] or 0
        if not plan['lifetime'] and u['scans_period'] != current_month:
            scans_used = 0
        monthly_limit = plan['monthly_scans']
        user_list.append({
            'id': u['id'],
            'name': u['name'],
            'email': u['email'],
            'plan': plan_key,
            'plan_display': plan['display'],
            'monthly_limit': monthly_limit,
            'scans_used': scans_used,
            'extra_scans': u['extra_scans'] or 0,
            'plan_expires_at': u['plan_expires_at'] or '',
            'is_expired': is_expired,
            'pct': min(100, scans_used * 100 // monthly_limit) if monthly_limit else 100,
        })
    stats = {
        'total': len(user_list),
        'free': sum(1 for u in user_list if u['plan'] == 'free'),
        'megtakaritor': sum(1 for u in user_list if u['plan'] == 'megtakaritor'),
        'befekteto': sum(1 for u in user_list if u['plan'] == 'befekteto'),
        'total_scans': sum(u['scans_used'] for u in user_list),
    }
    conn.close()
    return render_template('admin.html', users=user_list, stats=stats, plans=PLANS)


@app.route('/admin/users/<int:uid>/plan', methods=['POST'])
@login_required
@admin_required
def admin_set_plan(uid):
    plan = request.form.get('plan', 'free')
    if plan not in PLANS:
        flash('Érvénytelen csomag.', 'danger')
        return redirect(url_for('admin_panel'))
    expires_at = request.form.get('expires_at', '').strip() or None
    conn = get_db()
    conn.execute(
        "UPDATE users SET plan=?, plan_expires_at=?, scans_used=0, scans_period=? WHERE id=?",
        (plan, expires_at, datetime.today().strftime('%Y-%m'), uid))
    conn.commit()
    conn.close()
    flash('Csomag sikeresen módosítva!', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/users/<int:uid>/add-scans', methods=['POST'])
@login_required
@admin_required
def admin_add_scans(uid):
    try:
        amount = int(request.form.get('amount', 0))
    except (ValueError, TypeError):
        amount = 0
    if amount not in (10, 30):
        flash('Érvénytelen mennyiség.', 'danger')
        return redirect(url_for('admin_panel'))
    conn = get_db()
    conn.execute("UPDATE users SET extra_scans=extra_scans+? WHERE id=?", (amount, uid))
    conn.commit()
    conn.close()
    flash(f'{amount} extra beolvasás hozzáadva!', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/account/delete', methods=['POST'])
@login_required
def delete_account():
    uid = current_user.id
    conn = get_db()
    conn.execute('''DELETE FROM transaction_items WHERE transaction_id IN (
        SELECT t.id FROM transactions t JOIN wallets w ON t.wallet_id = w.id WHERE w.user_id = ?)''', (uid,))
    conn.execute('''DELETE FROM transactions WHERE wallet_id IN (
        SELECT id FROM wallets WHERE user_id = ?)''', (uid,))
    conn.execute('''DELETE FROM transfers WHERE from_wallet_id IN (SELECT id FROM wallets WHERE user_id = ?)
        OR to_wallet_id IN (SELECT id FROM wallets WHERE user_id = ?)''', (uid, uid))
    conn.execute("DELETE FROM wallets WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM categories WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    logout_user()
    flash("Fiókod és minden adatod véglegesen törlve.", "info")
    return redirect(url_for('login_page'))


@app.route('/account/export')
@login_required
def export_account():
    uid = current_user.id
    conn = get_db()

    def make_csv(headers, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        w.writerows(rows)
        return buf.getvalue().encode('utf-8-sig')

    transactions = conn.execute('''
        SELECT t.date, t.type, t.amount, t.description, c.name, w.name
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        JOIN wallets w ON t.wallet_id = w.id
        WHERE w.user_id = ? ORDER BY t.date DESC''', (uid,)).fetchall()

    transfers = conn.execute('''
        SELECT tr.date, fw.name, tw.name, tr.amount, tr.description
        FROM transfers tr
        JOIN wallets fw ON tr.from_wallet_id = fw.id
        JOIN wallets tw ON tr.to_wallet_id = tw.id
        WHERE fw.user_id = ? ORDER BY tr.date DESC''', (uid,)).fetchall()

    wallets = conn.execute(
        "SELECT name, description, initial_balance FROM wallets WHERE user_id = ? ORDER BY sort_order",
        (uid,)).fetchall()

    categories = conn.execute(
        "SELECT name, type FROM categories WHERE user_id = ? ORDER BY type, name",
        (uid,)).fetchall()

    conn.close()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('tranzakciok.csv', make_csv(
            ['Dátum', 'Típus', 'Összeg (Ft)', 'Leírás', 'Kategória', 'Tárca'],
            transactions))
        zf.writestr('atutalasok.csv', make_csv(
            ['Dátum', 'Forrás tárca', 'Cél tárca', 'Összeg (Ft)', 'Megjegyzés'],
            transfers))
        zf.writestr('tarcak.csv', make_csv(
            ['Név', 'Megjegyzés', 'Induló egyenleg (Ft)'],
            wallets))
        zf.writestr('kategoriak.csv', make_csv(
            ['Név', 'Típus'],
            categories))

    zip_buf.seek(0)
    return send_file(zip_buf, mimetype='application/zip',
                     as_attachment=True, download_name='koltsegvetes_export.zip')


# --- Auth ---

def _finish_oauth_login(conn, row, name):
    user = user_from_row(row)
    login_user(user)
    if user.totp_enabled:
        session['needs_2fa'] = True
    else:
        session.pop('needs_2fa', None)
    conn.close()
    flash(f"Üdvözöllek, {name}!", "success")


@oauth_authorized.connect_via(google_bp)
def google_logged_in(blueprint, token):
    if not token:
        flash("Nem sikerült bejelentkezni Google-fiókkal.", "danger")
        return False
    resp = blueprint.session.get("/oauth2/v2/userinfo")
    if not resp.ok:
        flash("Nem sikerült lekérni a Google-fiók adatait.", "danger")
        return False
    info = resp.json()
    google_id = info["id"]
    name = info.get("name", "")
    email = info.get("email", "")
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row:
            conn.execute("UPDATE users SET google_id=? WHERE id=?", (google_id, row["id"]))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    if row is None:
        conn.execute("INSERT INTO users (google_id, name, email) VALUES (?, ?, ?)", (google_id, name, email))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
        setup_new_user(conn, row["id"])
        conn.commit()
    _finish_oauth_login(conn, row, name)
    return False


@oauth_authorized.connect_via(github_bp)
def github_logged_in(blueprint, token):
    if not token:
        flash("Nem sikerült bejelentkezni GitHub-fiókkal.", "danger")
        return False
    resp = blueprint.session.get("/user")
    if not resp.ok:
        flash("Nem sikerült lekérni a GitHub-fiók adatait.", "danger")
        return False
    info = resp.json()
    github_id = str(info["id"])
    name = info.get("name") or info.get("login", "")
    email = info.get("email") or ""
    if not email:
        emails_resp = blueprint.session.get("/user/emails")
        if emails_resp.ok:
            primary = next((e for e in emails_resp.json()
                            if e.get("primary") and e.get("verified")), None)
            if primary:
                email = primary["email"]
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE github_id=?", (github_id,)).fetchone()
    if row is None and email:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row:
            conn.execute("UPDATE users SET github_id=? WHERE id=?", (github_id, row["id"]))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    if row is None:
        conn.execute("INSERT INTO users (github_id, name, email) VALUES (?, ?, ?)", (github_id, name, email))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE github_id=?", (github_id,)).fetchone()
        setup_new_user(conn, row["id"])
        conn.commit()
    _finish_oauth_login(conn, row, name)
    return False


@app.route('/register', methods=['POST'])
def register():
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    confirm = request.form.get('confirm_password', '')
    session['auth_tab'] = 'register'
    if not name or not email or not password:
        flash('Minden mező kitöltése kötelező.', 'danger')
        return redirect(url_for('login_page'))
    if len(password) < 8:
        flash('A jelszónak legalább 8 karakter hosszúnak kell lennie.', 'danger')
        return redirect(url_for('login_page'))
    if password != confirm:
        flash('A két jelszó nem egyezik.', 'danger')
        return redirect(url_for('login_page'))
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        flash('Ez az email cím már regisztrált. Jelentkezz be Google, GitHub vagy email+jelszó kombinációval.', 'danger')
        return redirect(url_for('login_page'))
    conn.execute("INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                 (name, email, generate_password_hash(password)))
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    setup_new_user(conn, row["id"])
    conn.commit()
    session.pop('auth_tab', None)
    login_user(user_from_row(row))
    session.pop('needs_2fa', None)
    conn.close()
    flash(f'Üdvözlünk, {name}! A fiókod sikeresen létrejött.', 'success')
    return redirect(url_for('index'))


@app.route('/login/email', methods=['POST'])
def login_email():
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '')
    session['auth_tab'] = 'login'
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not row or not row['password_hash'] or not check_password_hash(row['password_hash'], password):
        flash('Hibás email cím vagy jelszó.', 'danger')
        return redirect(url_for('login_page'))
    user = user_from_row(row)
    login_user(user)
    session.pop('auth_tab', None)
    if user.totp_enabled:
        session['needs_2fa'] = True
    else:
        session.pop('needs_2fa', None)
    flash(f'Üdvözöllek, {row["name"]}!', 'success')
    return redirect(url_for('index'))


@app.route('/account/password', methods=['POST'])
@login_required
def set_password():
    new_password = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')
    if len(new_password) < 8:
        flash('A jelszónak legalább 8 karakter hosszúnak kell lennie.', 'danger')
        return redirect(url_for('account'))
    if new_password != confirm:
        flash('A két jelszó nem egyezik.', 'danger')
        return redirect(url_for('account'))
    conn = get_db()
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (current_user.id,)).fetchone()
    if row['password_hash']:
        current_password = request.form.get('current_password', '')
        if not check_password_hash(row['password_hash'], current_password):
            flash('Hibás jelenlegi jelszó.', 'danger')
            conn.close()
            return redirect(url_for('account'))
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (generate_password_hash(new_password), current_user.id))
    conn.commit()
    conn.close()
    flash('Jelszó sikeresen beállítva!', 'success')
    return redirect(url_for('account'))


@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return render_template('landing.html')


@app.route('/logout')
@login_required
def logout():
    session.pop('needs_2fa', None)
    logout_user()
    flash("Sikeresen kijelentkeztél.", "info")
    return redirect(url_for("login_page"))


# --- 2FA ---

@app.route('/2fa/verify', methods=['GET', 'POST'])
@login_required
def two_factor_verify():
    if not session.get('needs_2fa'):
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        code = request.form.get('code', '').strip().replace(' ', '')
        conn = get_db()
        row = conn.execute("SELECT totp_secret FROM users WHERE id=?", (current_user.id,)).fetchone()
        conn.close()
        if row and row['totp_secret'] and pyotp.TOTP(row['totp_secret']).verify(code, valid_window=1):
            session.pop('needs_2fa', None)
            return redirect(url_for('index'))
        error = "Hibás kód. Próbáld újra."
    return render_template('2fa_verify.html', error=error)


@app.route('/2fa/setup', methods=['GET', 'POST'])
@login_required
def two_factor_setup():
    if request.method == 'POST':
        code = request.form.get('code', '').strip().replace(' ', '')
        secret = session.get('totp_secret_pending')
        if secret and pyotp.TOTP(secret).verify(code, valid_window=1):
            conn = get_db()
            conn.execute("UPDATE users SET totp_secret=?, totp_enabled=1 WHERE id=?",
                         (secret, current_user.id))
            conn.commit()
            conn.close()
            session.pop('totp_secret_pending', None)
            flash("Kétlépéses hitelesítés sikeresen aktiválva!", "success")
            return redirect(url_for('account'))
        flash("Hibás kód. Próbáld újra.", "danger")

    secret = session.get('totp_secret_pending') or pyotp.random_base32()
    session['totp_secret_pending'] = secret
    uri = pyotp.TOTP(secret).provisioning_uri(current_user.email, issuer_name="Költségvetés")
    qr_img = qrcode.make(uri)
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return render_template('2fa_setup.html', qr_b64=qr_b64, secret=secret)


@app.route('/2fa/disable', methods=['POST'])
@login_required
def two_factor_disable():
    code = request.form.get('code', '').strip().replace(' ', '')
    conn = get_db()
    row = conn.execute("SELECT totp_secret FROM users WHERE id=?", (current_user.id,)).fetchone()
    if row and row['totp_secret'] and pyotp.TOTP(row['totp_secret']).verify(code, valid_window=1):
        conn.execute("UPDATE users SET totp_secret=NULL, totp_enabled=0 WHERE id=?", (current_user.id,))
        conn.commit()
        flash("Kétlépéses hitelesítés kikapcsolva.", "info")
    else:
        flash("Hibás kód — a 2FA nem lett kikapcsolva.", "danger")
    conn.close()
    return redirect(url_for('account'))


# --- Index ---

@app.route('/')
@login_required
def index():
    conn = get_db()
    uid = current_user.id
    wallets = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall()
    active = get_active_wallet(wallets)

    if active == 'all':
        income = conn.execute('''
            SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
            JOIN wallets w ON t.wallet_id=w.id
            WHERE t.type='income' AND w.user_id=?''', (uid,)).fetchone()[0]
        expense = conn.execute('''
            SELECT COALESCE(SUM(t.amount), 0) FROM transactions t
            JOIN wallets w ON t.wallet_id=w.id
            WHERE t.type='expense' AND w.user_id=?''', (uid,)).fetchone()[0]
        balance = income - expense
        recent = conn.execute('''
            SELECT t.*, c.name as category_name, w.name as wallet_name
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            JOIN wallets w ON t.wallet_id = w.id
            WHERE w.user_id=?
            ORDER BY t.date DESC LIMIT 10''', (uid,)).fetchall()
    else:
        income = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='income' AND wallet_id=?",
            (active,)).fetchone()[0]
        expense = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='expense' AND wallet_id=?",
            (active,)).fetchone()[0]
        balance = wallet_balance(conn, active)
        recent = conn.execute('''
            SELECT t.*, c.name as category_name, w.name as wallet_name
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            JOIN wallets w ON t.wallet_id = w.id
            WHERE t.wallet_id=?
            ORDER BY t.date DESC LIMIT 10''', (active,)).fetchall()

    wallet_balances = [
        {'id': w['id'], 'name': w['name'], 'balance': wallet_balance(conn, w['id'])}
        for w in wallets
    ]
    current_month = datetime.today().strftime('%Y-%m')
    budget_warnings = conn.execute('''
        SELECT c.name, b.monthly_limit, COALESCE(SUM(t.amount), 0) as spent
        FROM budgets b
        JOIN categories c ON b.category_id = c.id
        LEFT JOIN transactions t ON t.category_id = b.category_id
            AND t.type='expense' AND strftime('%Y-%m', t.date)=?
            AND t.wallet_id IN (SELECT id FROM wallets WHERE user_id=?)
        WHERE b.user_id=?
        GROUP BY b.id HAVING spent >= b.monthly_limit * 0.7
        ORDER BY (spent / b.monthly_limit) DESC''',
        (current_month, uid, uid)).fetchall()
    conn.close()
    return render_template('index.html',
        balance=balance, income=income, expense=expense,
        transactions=recent, wallets=wallets, active_wallet=active,
        wallet_balances=wallet_balances, budget_warnings=budget_warnings)


# --- Transactions ---

@app.route('/transactions', methods=['GET', 'POST'])
@login_required
def transactions():
    conn = get_db()
    uid = current_user.id
    wallets = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall()
    active = get_active_wallet(wallets)

    if request.method == 'POST':
        items_desc = request.form.getlist('item_description')
        items_amount = request.form.getlist('item_amount')
        items_cat = request.form.getlist('item_category_id')
        valid_items = []
        for desc, amt, cat in zip(items_desc, items_amount, items_cat):
            desc = desc.strip()
            try:
                amt = float(amt)
            except (ValueError, TypeError):
                continue
            if desc and amt > 0:
                valid_items.append((desc, amt, int(cat) if cat else None))
        amount = float(request.form['amount'])
        if valid_items:
            amount = sum(a for _, a, _ in valid_items)
        wallet_id = request.form['wallet_id']
        cat_id = request.form['category_id']
        wallet_row = conn.execute("SELECT id, is_cash FROM wallets WHERE id=? AND user_id=?", (wallet_id, uid)).fetchone()
        if not wallet_row:
            conn.close()
            flash('Érvénytelen tárca.', 'danger')
            return redirect(url_for('transactions'))
        if wallet_row['is_cash']:
            amount = round_to_5(amount)
        if cat_id and not conn.execute("SELECT id FROM categories WHERE id=? AND user_id=?", (cat_id, uid)).fetchone():
            conn.close()
            flash('Érvénytelen kategória.', 'danger')
            return redirect(url_for('transactions'))
        cursor = conn.execute(
            "INSERT INTO transactions (amount, description, category_id, type, date, wallet_id) VALUES (?, ?, ?, ?, ?, ?)",
            (amount, request.form['description'], cat_id,
             request.form['type'], request.form['date'], wallet_id))
        tx_id = cursor.lastrowid
        for desc, amt, cat_id in valid_items:
            conn.execute(
                "INSERT INTO transaction_items (transaction_id, description, amount, category_id) VALUES (?, ?, ?, ?)",
                (tx_id, desc, amt, cat_id))
        conn.commit()
        flash('Tranzakció sikeresen hozzáadva!', 'success')
        return redirect(url_for('transactions', wallet=request.form['wallet_id']))

    q = request.args.get('q', '').strip()
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    filter_type = request.args.get('filter_type', '')
    filter_cat = request.args.get('category_id', '')

    per_page = 25
    page = max(1, int(request.args.get('page', 1) or 1))

    where = ' FROM transactions t LEFT JOIN categories c ON t.category_id = c.id JOIN wallets w ON t.wallet_id = w.id WHERE w.user_id=?'
    params = [uid]
    if active != 'all':
        where += ' AND t.wallet_id=?'
        params.append(active)
    if q:
        where += ' AND t.description LIKE ?'
        params.append(f'%{q}%')
    if date_from:
        where += ' AND t.date >= ?'
        params.append(date_from)
    if date_to:
        where += ' AND t.date <= ?'
        params.append(date_to)
    if filter_type in ('income', 'expense'):
        where += ' AND t.type=?'
        params.append(filter_type)
    if filter_cat:
        where += ' AND t.category_id=?'
        params.append(filter_cat)

    total = conn.execute('SELECT COUNT(*)' + where, params).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    sql = ('SELECT t.*, c.name as category_name, w.name as wallet_name,'
           ' (SELECT COUNT(*) FROM transaction_items WHERE transaction_id = t.id) as item_count'
           + where + ' ORDER BY t.date DESC LIMIT ? OFFSET ?')
    all_transactions = conn.execute(sql, params + [per_page, (page - 1) * per_page]).fetchall()
    is_filtered = any([q, date_from, date_to, filter_type, filter_cat])

    categories = conn.execute("SELECT * FROM categories WHERE user_id=? ORDER BY type, name", (uid,)).fetchall()
    cash_wallet_ids = [w['id'] for w in wallets if w['is_cash']]
    conn.close()
    today = datetime.today().strftime('%Y-%m-%d')
    return render_template('transactions.html',
        transactions=all_transactions, categories=categories, today=today,
        wallets=wallets, active_wallet=active,
        q=q, date_from=date_from, date_to=date_to,
        filter_type=filter_type, filter_cat=filter_cat, is_filtered=is_filtered,
        page=page, total_pages=total_pages, total=total,
        cash_wallet_ids=cash_wallet_ids)


@app.route('/transactions/<int:id>')
@login_required
def transaction_detail(id):
    conn = get_db()
    t = conn.execute('''
        SELECT t.*, c.name as category_name, w.name as wallet_name
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        JOIN wallets w ON t.wallet_id = w.id
        WHERE t.id=? AND w.user_id=?''', (id, current_user.id)).fetchone()
    if not t:
        conn.close()
        flash('Tranzakció nem található.', 'danger')
        return redirect(url_for('transactions'))
    items = conn.execute('''
        SELECT ti.*, c.name as category_name
        FROM transaction_items ti
        LEFT JOIN categories c ON ti.category_id = c.id
        WHERE ti.transaction_id=? ORDER BY ti.id''', (id,)).fetchall()
    conn.close()
    return render_template('transaction_detail.html', transaction=t, items=items)


@app.route('/transactions/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_transaction(id):
    conn = get_db()
    uid = current_user.id
    if request.method == 'POST':
        t = conn.execute('''
            SELECT t.id FROM transactions t JOIN wallets w ON t.wallet_id=w.id
            WHERE t.id=? AND w.user_id=?''', (id, uid)).fetchone()
        if not t:
            conn.close()
            flash('Tranzakció nem található.', 'danger')
            return redirect(url_for('transactions'))
        items_desc = request.form.getlist('item_description')
        items_amount = request.form.getlist('item_amount')
        items_cat = request.form.getlist('item_category_id')
        valid_items = []
        for desc, amt, cat in zip(items_desc, items_amount, items_cat):
            desc = desc.strip()
            try:
                amt = float(amt)
            except (ValueError, TypeError):
                continue
            if desc and amt > 0:
                valid_items.append((desc, amt, int(cat) if cat else None))
        amount = float(request.form['amount'])
        if valid_items:
            amount = sum(a for _, a, _ in valid_items)
        wallet_id = request.form['wallet_id']
        cat_id = request.form['category_id']
        wallet_row = conn.execute("SELECT id, is_cash FROM wallets WHERE id=? AND user_id=?", (wallet_id, uid)).fetchone()
        if not wallet_row:
            conn.close()
            flash('Érvénytelen tárca.', 'danger')
            return redirect(url_for('transactions'))
        if wallet_row['is_cash']:
            amount = round_to_5(amount)
        if cat_id and not conn.execute("SELECT id FROM categories WHERE id=? AND user_id=?", (cat_id, uid)).fetchone():
            conn.close()
            flash('Érvénytelen kategória.', 'danger')
            return redirect(url_for('transactions'))
        conn.execute(
            "UPDATE transactions SET amount=?, description=?, category_id=?, type=?, date=?, wallet_id=? WHERE id=?",
            (amount, request.form['description'], cat_id,
             request.form['type'], request.form['date'], wallet_id, id))
        conn.execute("DELETE FROM transaction_items WHERE transaction_id=?", (id,))
        for desc, amt, cat_id in valid_items:
            conn.execute(
                "INSERT INTO transaction_items (transaction_id, description, amount, category_id) VALUES (?, ?, ?, ?)",
                (id, desc, amt, cat_id))
        conn.commit()
        conn.close()
        flash('Tranzakció módosítva!', 'success')
        return redirect(url_for('transactions'))
    t = conn.execute('''
        SELECT t.* FROM transactions t JOIN wallets w ON t.wallet_id=w.id
        WHERE t.id=? AND w.user_id=?''', (id, uid)).fetchone()
    if not t:
        conn.close()
        flash('Tranzakció nem található.', 'danger')
        return redirect(url_for('transactions'))
    items = conn.execute('''
        SELECT ti.*, c.name as category_name
        FROM transaction_items ti
        LEFT JOIN categories c ON ti.category_id = c.id
        WHERE ti.transaction_id=? ORDER BY ti.id''', (id,)).fetchall()
    categories = conn.execute("SELECT * FROM categories WHERE user_id=? ORDER BY type, name", (uid,)).fetchall()
    wallets_list = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall()
    cash_wallet_ids = [w['id'] for w in wallets_list if w['is_cash']]
    conn.close()
    return render_template('transaction_edit.html', transaction=t, items=items,
                           categories=categories, wallets=wallets_list,
                           cash_wallet_ids=cash_wallet_ids)


@app.route('/transactions/delete/<int:id>', methods=['POST'])
@login_required
def delete_transaction(id):
    conn = get_db()
    t = conn.execute('''
        SELECT t.id FROM transactions t JOIN wallets w ON t.wallet_id=w.id
        WHERE t.id=? AND w.user_id=?''', (id, current_user.id)).fetchone()
    if not t:
        conn.close()
        flash('Tranzakció nem található.', 'danger')
        return redirect(url_for('transactions'))
    conn.execute("DELETE FROM transaction_items WHERE transaction_id=?", (id,))
    conn.execute("DELETE FROM transactions WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash('Tranzakció törölve!', 'info')
    return redirect(url_for('transactions'))


# --- Wallets ---

@app.route('/wallets', methods=['GET', 'POST'])
@login_required
def wallets():
    conn = get_db()
    uid = current_user.id
    if request.method == 'POST':
        initial = request.form.get('initial_balance', '').strip()
        initial = float(initial) if initial else 0.0
        is_cash = 1 if request.form.get('is_cash') else 0
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM wallets WHERE user_id=?", (uid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO wallets (user_id, name, description, initial_balance, sort_order, is_cash) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, request.form['name'], request.form.get('description', ''), initial, max_order + 1, is_cash))
        conn.commit()
        flash('Tárca hozzáadva!', 'success')
        conn.close()
        return redirect(url_for('wallets'))
    all_wallets = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall()
    wallet_list = [
        {'id': w['id'], 'name': w['name'], 'description': w['description'],
         'balance': wallet_balance(conn, w['id']), 'is_cash': w['is_cash']}
        for w in all_wallets
    ]
    conn.close()
    return render_template('wallets.html', wallets=wallet_list)


@app.route('/wallets/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_wallet(id):
    conn = get_db()
    uid = current_user.id
    if request.method == 'POST':
        initial = request.form.get('initial_balance', '').strip()
        initial = float(initial) if initial else 0.0
        is_cash = 1 if request.form.get('is_cash') else 0
        conn.execute("UPDATE wallets SET name=?, description=?, initial_balance=?, is_cash=? WHERE id=? AND user_id=?",
                     (request.form['name'], request.form.get('description', ''), initial, is_cash, id, uid))
        conn.commit()
        conn.close()
        flash('Tárca módosítva!', 'success')
        return redirect(url_for('wallets'))
    w = conn.execute("SELECT * FROM wallets WHERE id=? AND user_id=?", (id, uid)).fetchone()
    conn.close()
    if not w:
        flash('Tárca nem található.', 'danger')
        return redirect(url_for('wallets'))
    return render_template('wallet_edit.html', wallet=w)


@app.route('/wallets/delete/<int:id>', methods=['POST'])
@login_required
def delete_wallet(id):
    conn = get_db()
    uid = current_user.id
    w = conn.execute("SELECT id FROM wallets WHERE id=? AND user_id=?", (id, uid)).fetchone()
    if not w:
        flash('Tárca nem található.', 'danger')
        conn.close()
        return redirect(url_for('wallets'))
    tx_count = conn.execute("SELECT COUNT(*) FROM transactions WHERE wallet_id=?", (id,)).fetchone()[0]
    tr_count = conn.execute(
        "SELECT COUNT(*) FROM transfers WHERE from_wallet_id=? OR to_wallet_id=?", (id, id)).fetchone()[0]
    if tx_count > 0 or tr_count > 0:
        flash('Nem törölhető: a tárcához tranzakciók vagy átutalások tartoznak.', 'danger')
        conn.close()
        return redirect(url_for('wallets'))
    conn.execute("DELETE FROM wallets WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash('Tárca törölve!', 'info')
    return redirect(url_for('wallets'))


@app.route('/wallets/move/<int:id>/<direction>')
@login_required
def move_wallet(id, direction):
    conn = get_db()
    uid = current_user.id
    current_w = conn.execute("SELECT * FROM wallets WHERE id=? AND user_id=?", (id, uid)).fetchone()
    if current_w:
        cur_order = current_w['sort_order']
        if direction == 'up':
            neighbor = conn.execute(
                "SELECT * FROM wallets WHERE sort_order<? AND user_id=? ORDER BY sort_order DESC LIMIT 1",
                (cur_order, uid)).fetchone()
        else:
            neighbor = conn.execute(
                "SELECT * FROM wallets WHERE sort_order>? AND user_id=? ORDER BY sort_order ASC LIMIT 1",
                (cur_order, uid)).fetchone()
        if neighbor:
            conn.execute("UPDATE wallets SET sort_order=? WHERE id=?", (neighbor['sort_order'], id))
            conn.execute("UPDATE wallets SET sort_order=? WHERE id=?", (cur_order, neighbor['id']))
            conn.commit()
    conn.close()
    return redirect(url_for('wallets'))


# --- Transfers ---

@app.route('/transfers', methods=['GET', 'POST'])
@login_required
def transfers():
    conn = get_db()
    uid = current_user.id
    all_wallets = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall()
    if request.method == 'POST':
        from_id = int(request.form['from_wallet_id'])
        to_id = int(request.form['to_wallet_id'])
        if not conn.execute("SELECT id FROM wallets WHERE id=? AND user_id=?", (from_id, uid)).fetchone() or \
           not conn.execute("SELECT id FROM wallets WHERE id=? AND user_id=?", (to_id, uid)).fetchone():
            flash('Érvénytelen tárca.', 'danger')
        elif from_id == to_id:
            flash('A forrás és a cél tárca nem lehet ugyanaz.', 'danger')
        else:
            conn.execute(
                "INSERT INTO transfers (from_wallet_id, to_wallet_id, amount, description, date) VALUES (?, ?, ?, ?, ?)",
                (from_id, to_id, request.form['amount'],
                 request.form.get('description', ''), request.form['date']))
            conn.commit()
            flash('Átutalás rögzítve!', 'success')
        conn.close()
        return redirect(url_for('transfers'))
    all_transfers = conn.execute('''
        SELECT tr.*, fw.name as from_name, tw.name as to_name
        FROM transfers tr
        JOIN wallets fw ON tr.from_wallet_id = fw.id
        JOIN wallets tw ON tr.to_wallet_id = tw.id
        WHERE fw.user_id=?
        ORDER BY tr.date DESC''', (uid,)).fetchall()
    today = datetime.today().strftime('%Y-%m-%d')
    conn.close()
    return render_template('transfers.html', transfers=all_transfers, wallets=all_wallets, today=today)


@app.route('/transfers/delete/<int:id>', methods=['POST'])
@login_required
def delete_transfer(id):
    conn = get_db()
    uid = current_user.id
    tr = conn.execute('''
        SELECT tr.id FROM transfers tr JOIN wallets fw ON tr.from_wallet_id=fw.id
        WHERE tr.id=? AND fw.user_id=?''', (id, uid)).fetchone()
    if not tr:
        conn.close()
        flash('Átutalás nem található.', 'danger')
        return redirect(url_for('transfers'))
    conn.execute("DELETE FROM transfers WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash('Átutalás törölve!', 'info')
    return redirect(url_for('transfers'))


# --- Categories ---

@app.route('/categories', methods=['GET', 'POST'])
@login_required
def categories():
    conn = get_db()
    uid = current_user.id
    if request.method == 'POST':
        conn.execute("INSERT INTO categories (user_id, name, type) VALUES (?, ?, ?)",
                     (uid, request.form['name'], request.form['type']))
        conn.commit()
        flash('Kategória sikeresen hozzáadva!', 'success')
        conn.close()
        return redirect(url_for('categories'))
    all_categories = conn.execute(
        "SELECT * FROM categories WHERE user_id=? ORDER BY type, name", (uid,)).fetchall()
    conn.close()
    return render_template('categories.html', categories=all_categories)


@app.route('/categories/delete/<int:id>', methods=['POST'])
@login_required
def delete_category(id):
    conn = get_db()
    uid = current_user.id
    cat = conn.execute("SELECT id FROM categories WHERE id=? AND user_id=?", (id, uid)).fetchone()
    if not cat:
        conn.close()
        flash('Kategória nem található.', 'danger')
        return redirect(url_for('categories'))
    conn.execute("DELETE FROM categories WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash('Kategória törölve!', 'info')
    return redirect(url_for('categories'))


# --- Budgets ---

@app.route('/budgets', methods=['GET', 'POST'])
@login_required
def budgets():
    conn = get_db()
    uid = current_user.id
    if request.method == 'POST':
        cat_id = request.form['category_id']
        limit = float(request.form['monthly_limit'])
        if not conn.execute("SELECT id FROM categories WHERE id=? AND user_id=?", (cat_id, uid)).fetchone():
            conn.close()
            flash('Érvénytelen kategória.', 'danger')
            return redirect(url_for('budgets'))
        existing = conn.execute("SELECT id FROM budgets WHERE user_id=? AND category_id=?", (uid, cat_id)).fetchone()
        if existing:
            conn.execute("UPDATE budgets SET monthly_limit=? WHERE id=?", (limit, existing['id']))
        else:
            conn.execute("INSERT INTO budgets (user_id, category_id, monthly_limit) VALUES (?, ?, ?)", (uid, cat_id, limit))
        conn.commit()
        conn.close()
        flash('Keret mentve!', 'success')
        return redirect(url_for('budgets'))

    current_month = datetime.today().strftime('%Y-%m')
    budget_list = conn.execute('''
        SELECT b.id, b.monthly_limit, b.category_id, c.name as cat_name,
               COALESCE(SUM(t.amount), 0) as spent
        FROM budgets b
        JOIN categories c ON b.category_id = c.id
        LEFT JOIN transactions t ON t.category_id = b.category_id
            AND t.type='expense' AND strftime('%Y-%m', t.date)=?
            AND t.wallet_id IN (SELECT id FROM wallets WHERE user_id=?)
        WHERE b.user_id=?
        GROUP BY b.id ORDER BY c.name''', (current_month, uid, uid)).fetchall()
    free_cats = conn.execute('''
        SELECT * FROM categories WHERE user_id=? AND type='expense'
        AND id NOT IN (SELECT category_id FROM budgets WHERE user_id=?)
        ORDER BY name''', (uid, uid)).fetchall()
    conn.close()
    return render_template('budgets.html', budget_list=budget_list,
                           free_cats=free_cats, current_month=current_month)


@app.route('/budgets/delete/<int:id>', methods=['POST'])
@login_required
def delete_budget(id):
    conn = get_db()
    conn.execute("DELETE FROM budgets WHERE id=? AND user_id=?", (id, current_user.id))
    conn.commit()
    conn.close()
    flash('Keret törölve!', 'info')
    return redirect(url_for('budgets'))


# --- Reports ---

@app.route('/reports')
@login_required
def reports():
    conn = get_db()
    uid = current_user.id
    wallets = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall()
    active = get_active_wallet(wallets)

    if active == 'all':
        monthly = conn.execute('''
            SELECT strftime('%Y-%m', t.date) as month,
                SUM(CASE WHEN t.type='income' THEN t.amount ELSE 0 END) as income,
                SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END) as expense
            FROM transactions t JOIN wallets w ON t.wallet_id=w.id
            WHERE w.user_id=?
            GROUP BY month ORDER BY month DESC LIMIT 12''', (uid,)).fetchall()
        by_category = conn.execute('''
            SELECT name, SUM(total) as total FROM (
                SELECT c.name, SUM(ti.amount) as total
                FROM transaction_items ti
                JOIN categories c ON ti.category_id = c.id
                JOIN transactions t ON ti.transaction_id = t.id
                JOIN wallets w ON t.wallet_id=w.id
                WHERE t.type='expense' AND w.user_id=?
                GROUP BY c.name
                UNION ALL
                SELECT c.name, SUM(t.amount) as total
                FROM transactions t
                JOIN categories c ON t.category_id = c.id
                JOIN wallets w ON t.wallet_id=w.id
                WHERE t.type='expense' AND w.user_id=?
                AND t.id NOT IN (
                    SELECT DISTINCT transaction_id FROM transaction_items WHERE category_id IS NOT NULL)
                GROUP BY c.name
            ) GROUP BY name ORDER BY total DESC''', (uid, uid)).fetchall()
    else:
        monthly = conn.execute('''
            SELECT strftime('%Y-%m', date) as month,
                SUM(CASE WHEN type='income' THEN amount ELSE 0 END) as income,
                SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) as expense
            FROM transactions WHERE wallet_id=?
            GROUP BY month ORDER BY month DESC LIMIT 12''', (active,)).fetchall()
        by_category = conn.execute('''
            SELECT name, SUM(total) as total FROM (
                SELECT c.name, SUM(ti.amount) as total
                FROM transaction_items ti
                JOIN categories c ON ti.category_id = c.id
                JOIN transactions t ON ti.transaction_id = t.id
                WHERE t.type='expense' AND t.wallet_id=?
                GROUP BY c.name
                UNION ALL
                SELECT c.name, SUM(t.amount) as total
                FROM transactions t JOIN categories c ON t.category_id = c.id
                WHERE t.type='expense' AND t.wallet_id=?
                AND t.id NOT IN (
                    SELECT DISTINCT transaction_id FROM transaction_items WHERE category_id IS NOT NULL)
                GROUP BY c.name
            ) GROUP BY name ORDER BY total DESC''', (active, active)).fetchall()

    conn.close()
    return render_template('reports.html', monthly=monthly, by_category=by_category,
                           wallets=wallets, active_wallet=active)


# --- Receipt Scanner ---

@app.route('/scan-receipt', methods=['GET', 'POST'])
@login_required
def scan_receipt():
    conn = get_db()
    uid = current_user.id
    scan_status = get_scan_status(conn, uid)
    wallets = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall()
    categories = conn.execute(
        "SELECT * FROM categories WHERE user_id=? AND type='expense' ORDER BY name", (uid,)).fetchall()
    today = datetime.today().strftime('%Y-%m-%d')

    if request.method == 'POST':
        if scan_status['at_hard_limit']:
            conn.close()
            return render_template('scan_receipt.html', wallets=wallets, categories=categories,
                                   today=today, scan_status=scan_status)

        files = request.files.getlist('receipt_images')
        if not files or all(f.filename == '' for f in files):
            flash('Legalább egy képet tölts fel!', 'danger')
            conn.close()
            return render_template('scan_receipt.html', wallets=wallets, categories=categories,
                                   today=today, scan_status=scan_status)

        images = []
        for f in files:
            if f.filename:
                images.append((f.read(), f.content_type or 'image/jpeg'))

        try:
            from receipt_scanner import scan_receipt_images
            result = scan_receipt_images(images)
        except Exception as e:
            flash(f'Hiba a blokk beolvasásakor: {e}', 'danger')
            conn.close()
            return render_template('scan_receipt.html', wallets=wallets, categories=categories,
                                   today=today, scan_status=scan_status)

        increment_scan_used(conn, uid, scan_status)
        scan_status = get_scan_status(conn, uid)

        items = result.get('tetelek', [])
        vegosszeg = result.get('vegosszeg', 0) or 0
        items_sum = sum(i.get('amount', 0) for i in items)
        mismatch = abs(items_sum - vegosszeg) > 1 if vegosszeg else False
        selected_wallet = request.form.get('wallet_id')
        conn.close()
        return render_template('scan_receipt.html',
            wallets=wallets, categories=categories, today=today,
            scan_result=result, items=items, vegosszeg=vegosszeg,
            items_sum=items_sum, mismatch=mismatch,
            selected_wallet=int(selected_wallet) if selected_wallet else None,
            scan_status=scan_status)

    conn.close()
    return render_template('scan_receipt.html', wallets=wallets, categories=categories,
                           today=today, scan_status=scan_status)


@app.route('/scan-receipt/save', methods=['POST'])
@login_required
def save_receipt():
    conn = get_db()
    uid = current_user.id
    wallet_id = int(request.form['wallet_id'])
    w = conn.execute("SELECT id FROM wallets WHERE id=? AND user_id=?", (wallet_id, uid)).fetchone()
    if not w:
        conn.close()
        flash('Érvénytelen tárca.', 'danger')
        return redirect(url_for('scan_receipt'))

    items_desc = request.form.getlist('item_description')
    items_amount = request.form.getlist('item_amount')
    items_cat = request.form.getlist('item_category_id')

    valid_items = []
    for desc, amt, cat in zip(items_desc, items_amount, items_cat):
        desc = desc.strip()
        try:
            amt = float(amt)
        except (ValueError, TypeError):
            continue
        if desc and amt > 0:
            valid_items.append((desc, amt, int(cat) if cat else None))

    total = sum(a for _, a, _ in valid_items) if valid_items else 0
    cat_id = request.form.get('category_id') or None

    cursor = conn.execute(
        "INSERT INTO transactions (amount, description, category_id, type, date, wallet_id) VALUES (?, ?, ?, 'expense', ?, ?)",
        (total, request.form.get('description', ''), cat_id, request.form['date'], wallet_id))
    tx_id = cursor.lastrowid

    for desc, amt, cat_id in valid_items:
        conn.execute(
            "INSERT INTO transaction_items (transaction_id, description, amount, category_id) VALUES (?, ?, ?, ?)",
            (tx_id, desc, amt, cat_id))

    conn.commit()
    conn.close()
    flash('Blokk sikeresen mentve!', 'success')
    return redirect(url_for('transactions'))


init_db()

if __name__ == '__main__':
    app.run(debug=True)
