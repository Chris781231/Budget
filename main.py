import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_dance.contrib.google import make_google_blueprint, google
from flask_dance.consumer import oauth_authorized
import sqlite3
from datetime import datetime
from werkzeug.middleware.proxy_fix import ProxyFix

if os.environ.get("RAILWAY_ENVIRONMENT") is None:
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-insecure-key')

google_bp = make_google_blueprint(
    client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    scope=["https://www.googleapis.com/auth/userinfo.profile",
           "https://www.googleapis.com/auth/userinfo.email", "openid"]
)
app.register_blueprint(google_bp, url_prefix="/login")

login_manager = LoginManager(app)
login_manager.login_view = "login_page"

DATABASE = '/data/koltsegvetes.db' if os.environ.get('RAILWAY_ENVIRONMENT') else 'koltsegvetes.db'


class User(UserMixin):
    def __init__(self, id, name, email):
        self.id = id
        self.name = name
        self.email = email


@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if row:
        return User(row["id"], row["name"], row["email"])
    return None


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


def setup_new_user(conn, user_id):
    conn.execute(
        "INSERT INTO wallets (user_id, name, description, initial_balance, sort_order) VALUES (?, ?, ?, 0, ?)",
        (user_id, 'Készpénz', '', 1))
    conn.execute(
        "INSERT INTO wallets (user_id, name, description, initial_balance, sort_order) VALUES (?, ?, ?, 0, ?)",
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

    conn.commit()
    conn.close()


# --- Auth ---

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
        conn.execute("INSERT INTO users (google_id, name, email) VALUES (?, ?, ?)", (google_id, name, email))
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
        setup_new_user(conn, row["id"])
        conn.commit()
    conn.close()
    login_user(User(row["id"], row["name"], row["email"]))
    flash(f"Üdvözöllek, {name}!", "success")
    return False


@app.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Sikeresen kijelentkeztél.", "info")
    return redirect(url_for("login_page"))


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
    conn.close()
    return render_template('index.html',
        balance=balance, income=income, expense=expense,
        transactions=recent, wallets=wallets, active_wallet=active,
        wallet_balances=wallet_balances)


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
        if not conn.execute("SELECT id FROM wallets WHERE id=? AND user_id=?", (wallet_id, uid)).fetchone():
            conn.close()
            flash('Érvénytelen tárca.', 'danger')
            return redirect(url_for('transactions'))
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

    if active == 'all':
        all_transactions = conn.execute('''
            SELECT t.*, c.name as category_name, w.name as wallet_name,
                   (SELECT COUNT(*) FROM transaction_items WHERE transaction_id = t.id) as item_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            JOIN wallets w ON t.wallet_id = w.id
            WHERE w.user_id=?
            ORDER BY t.date DESC''', (uid,)).fetchall()
    else:
        all_transactions = conn.execute('''
            SELECT t.*, c.name as category_name, w.name as wallet_name,
                   (SELECT COUNT(*) FROM transaction_items WHERE transaction_id = t.id) as item_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            JOIN wallets w ON t.wallet_id = w.id
            WHERE t.wallet_id=? AND w.user_id=?
            ORDER BY t.date DESC''', (active, uid)).fetchall()

    categories = conn.execute("SELECT * FROM categories WHERE user_id=? ORDER BY type, name", (uid,)).fetchall()
    conn.close()
    today = datetime.today().strftime('%Y-%m-%d')
    return render_template('transactions.html',
        transactions=all_transactions, categories=categories, today=today,
        wallets=wallets, active_wallet=active)


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
        if not conn.execute("SELECT id FROM wallets WHERE id=? AND user_id=?", (wallet_id, uid)).fetchone():
            conn.close()
            flash('Érvénytelen tárca.', 'danger')
            return redirect(url_for('transactions'))
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
    conn.close()
    return render_template('transaction_edit.html', transaction=t, items=items,
                           categories=categories, wallets=wallets_list)


@app.route('/transactions/delete/<int:id>')
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
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM wallets WHERE user_id=?", (uid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO wallets (user_id, name, description, initial_balance, sort_order) VALUES (?, ?, ?, ?, ?)",
            (uid, request.form['name'], request.form.get('description', ''), initial, max_order + 1))
        conn.commit()
        flash('Tárca hozzáadva!', 'success')
        conn.close()
        return redirect(url_for('wallets'))
    all_wallets = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall()
    wallet_list = [
        {'id': w['id'], 'name': w['name'], 'description': w['description'],
         'balance': wallet_balance(conn, w['id'])}
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
        conn.execute("UPDATE wallets SET name=?, description=?, initial_balance=? WHERE id=? AND user_id=?",
                     (request.form['name'], request.form.get('description', ''), initial, id, uid))
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


@app.route('/wallets/delete/<int:id>')
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


@app.route('/transfers/delete/<int:id>')
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


@app.route('/categories/delete/<int:id>')
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
    wallets = conn.execute("SELECT * FROM wallets WHERE user_id=? ORDER BY sort_order", (uid,)).fetchall()
    categories = conn.execute(
        "SELECT * FROM categories WHERE user_id=? AND type='expense' ORDER BY name", (uid,)).fetchall()
    conn.close()
    today = datetime.today().strftime('%Y-%m-%d')

    if request.method == 'POST':
        files = request.files.getlist('receipt_images')
        if not files or all(f.filename == '' for f in files):
            flash('Legalább egy képet tölts fel!', 'danger')
            return render_template('scan_receipt.html', wallets=wallets, categories=categories, today=today)

        images = []
        for f in files:
            if f.filename:
                images.append((f.read(), f.content_type or 'image/jpeg'))

        try:
            from receipt_scanner import scan_receipt_images
            result = scan_receipt_images(images)
        except Exception as e:
            flash(f'Hiba a blokk beolvasásakor: {e}', 'danger')
            return render_template('scan_receipt.html', wallets=wallets, categories=categories, today=today)

        items = result.get('tetelek', [])
        vegosszeg = result.get('vegosszeg', 0) or 0
        items_sum = sum(i.get('amount', 0) for i in items)
        mismatch = abs(items_sum - vegosszeg) > 1 if vegosszeg else False
        selected_wallet = request.form.get('wallet_id')

        return render_template('scan_receipt.html',
            wallets=wallets, categories=categories, today=today,
            scan_result=result, items=items, vegosszeg=vegosszeg,
            items_sum=items_sum, mismatch=mismatch,
            selected_wallet=int(selected_wallet) if selected_wallet else None)

    return render_template('scan_receipt.html', wallets=wallets, categories=categories, today=today)


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
