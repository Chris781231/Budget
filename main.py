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
app.secret_key = 'koltsegvetes_secret_key'

google_bp = make_google_blueprint(
    client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    scope=["https://www.googleapis.com/auth/userinfo.profile",
           "https://www.googleapis.com/auth/userinfo.email", "openid"]
)
app.register_blueprint(google_bp, url_prefix="/login")

login_manager = LoginManager(app)
login_manager.login_view = "login_page"

DATABASE = 'koltsegvetes.db'


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
    """Returns int wallet id or 'all' based on ?wallet= query param."""
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


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        google_id TEXT UNIQUE NOT NULL,
        name TEXT, email TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS wallets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        initial_balance REAL DEFAULT 0,
        sort_order INTEGER)''')

    c.execute("SELECT COUNT(*) FROM wallets")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO wallets (name, description, initial_balance, sort_order) VALUES (?, ?, 0, ?)", [
            ('Készpénz', '', 1),
            ('Gránit bankkártya', '', 2),
        ])

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

    c.execute('''CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    try:
        c.execute("ALTER TABLE transactions ADD COLUMN wallet_id INTEGER REFERENCES wallets(id)")
    except Exception:
        pass
    c.execute("UPDATE transactions SET wallet_id=1 WHERE wallet_id IS NULL")

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

    try:
        c.execute("ALTER TABLE transaction_items ADD COLUMN category_id INTEGER REFERENCES categories(id)")
    except Exception:
        pass

    c.execute("SELECT COUNT(*) FROM categories")
    if c.fetchone()[0] == 0:
        c.executemany("INSERT INTO categories (name, type) VALUES (?, ?)", [
            ('Fizetés', 'income'), ('Egyéb bevétel', 'income'),
            ('Lakás', 'expense'), ('Élelmiszer', 'expense'),
            ('Közlekedés', 'expense'), ('Szórakozás', 'expense'),
            ('Egészség', 'expense'), ('Egyéb kiadás', 'expense'),
        ])

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
    wallets = conn.execute("SELECT * FROM wallets ORDER BY sort_order").fetchall()
    active = get_active_wallet(wallets)

    if active == 'all':
        income = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='income'").fetchone()[0]
        expense = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='expense'").fetchone()[0]
        balance = income - expense
        recent = conn.execute('''
            SELECT t.*, c.name as category_name, w.name as wallet_name
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            LEFT JOIN wallets w ON t.wallet_id = w.id
            ORDER BY t.date DESC LIMIT 10''').fetchall()
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
            LEFT JOIN wallets w ON t.wallet_id = w.id
            WHERE t.wallet_id = ?
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
    wallets = conn.execute("SELECT * FROM wallets ORDER BY sort_order").fetchall()
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
        cursor = conn.execute(
            "INSERT INTO transactions (amount, description, category_id, type, date, wallet_id) VALUES (?, ?, ?, ?, ?, ?)",
            (amount, request.form['description'], request.form['category_id'],
             request.form['type'], request.form['date'], request.form['wallet_id']))
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
            LEFT JOIN wallets w ON t.wallet_id = w.id
            ORDER BY t.date DESC''').fetchall()
    else:
        all_transactions = conn.execute('''
            SELECT t.*, c.name as category_name, w.name as wallet_name,
                   (SELECT COUNT(*) FROM transaction_items WHERE transaction_id = t.id) as item_count
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            LEFT JOIN wallets w ON t.wallet_id = w.id
            WHERE t.wallet_id = ?
            ORDER BY t.date DESC''', (active,)).fetchall()

    categories = conn.execute("SELECT * FROM categories ORDER BY type, name").fetchall()
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
        LEFT JOIN wallets w ON t.wallet_id = w.id
        WHERE t.id = ?''', (id,)).fetchone()
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
        conn.execute(
            "UPDATE transactions SET amount=?, description=?, category_id=?, type=?, date=?, wallet_id=? WHERE id=?",
            (amount, request.form['description'], request.form['category_id'],
             request.form['type'], request.form['date'], request.form['wallet_id'], id))
        conn.execute("DELETE FROM transaction_items WHERE transaction_id=?", (id,))
        for desc, amt, cat_id in valid_items:
            conn.execute(
                "INSERT INTO transaction_items (transaction_id, description, amount, category_id) VALUES (?, ?, ?, ?)",
                (id, desc, amt, cat_id))
        conn.commit()
        conn.close()
        flash('Tranzakció módosítva!', 'success')
        return redirect(url_for('transactions'))
    t = conn.execute("SELECT * FROM transactions WHERE id=?", (id,)).fetchone()
    if not t:
        conn.close()
        flash('Tranzakció nem található.', 'danger')
        return redirect(url_for('transactions'))
    items = conn.execute('''
        SELECT ti.*, c.name as category_name
        FROM transaction_items ti
        LEFT JOIN categories c ON ti.category_id = c.id
        WHERE ti.transaction_id=? ORDER BY ti.id''', (id,)).fetchall()
    categories = conn.execute("SELECT * FROM categories ORDER BY type, name").fetchall()
    wallets_list = conn.execute("SELECT * FROM wallets ORDER BY sort_order").fetchall()
    conn.close()
    return render_template('transaction_edit.html', transaction=t, items=items,
                           categories=categories, wallets=wallets_list)


@app.route('/transactions/delete/<int:id>')
@login_required
def delete_transaction(id):
    conn = get_db()
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
    if request.method == 'POST':
        initial = request.form.get('initial_balance', '').strip()
        initial = float(initial) if initial else 0.0
        max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM wallets").fetchone()[0]
        conn.execute(
            "INSERT INTO wallets (name, description, initial_balance, sort_order) VALUES (?, ?, ?, ?)",
            (request.form['name'], request.form.get('description', ''), initial, max_order + 1))
        conn.commit()
        flash('Tárca hozzáadva!', 'success')
        conn.close()
        return redirect(url_for('wallets'))
    all_wallets = conn.execute("SELECT * FROM wallets ORDER BY sort_order").fetchall()
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
    if request.method == 'POST':
        initial = request.form.get('initial_balance', '').strip()
        initial = float(initial) if initial else 0.0
        conn.execute("UPDATE wallets SET name=?, description=?, initial_balance=? WHERE id=?",
                     (request.form['name'], request.form.get('description', ''), initial, id))
        conn.commit()
        conn.close()
        flash('Tárca módosítva!', 'success')
        return redirect(url_for('wallets'))
    w = conn.execute("SELECT * FROM wallets WHERE id=?", (id,)).fetchone()
    conn.close()
    if not w:
        flash('Tárca nem található.', 'danger')
        return redirect(url_for('wallets'))
    return render_template('wallet_edit.html', wallet=w)


@app.route('/wallets/delete/<int:id>')
@login_required
def delete_wallet(id):
    conn = get_db()
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
    current = conn.execute("SELECT * FROM wallets WHERE id=?", (id,)).fetchone()
    if current:
        cur_order = current['sort_order']
        if direction == 'up':
            neighbor = conn.execute(
                "SELECT * FROM wallets WHERE sort_order < ? ORDER BY sort_order DESC LIMIT 1",
                (cur_order,)).fetchone()
        else:
            neighbor = conn.execute(
                "SELECT * FROM wallets WHERE sort_order > ? ORDER BY sort_order ASC LIMIT 1",
                (cur_order,)).fetchone()
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
    all_wallets = conn.execute("SELECT * FROM wallets ORDER BY sort_order").fetchall()
    if request.method == 'POST':
        from_id = int(request.form['from_wallet_id'])
        to_id = int(request.form['to_wallet_id'])
        if from_id == to_id:
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
        ORDER BY tr.date DESC''').fetchall()
    today = datetime.today().strftime('%Y-%m-%d')
    conn.close()
    return render_template('transfers.html', transfers=all_transfers, wallets=all_wallets, today=today)


@app.route('/transfers/delete/<int:id>')
@login_required
def delete_transfer(id):
    conn = get_db()
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
    if request.method == 'POST':
        conn.execute("INSERT INTO categories (name, type) VALUES (?, ?)",
                     (request.form['name'], request.form['type']))
        conn.commit()
        flash('Kategória sikeresen hozzáadva!', 'success')
        conn.close()
        return redirect(url_for('categories'))
    all_categories = conn.execute("SELECT * FROM categories ORDER BY type, name").fetchall()
    conn.close()
    return render_template('categories.html', categories=all_categories)


@app.route('/categories/delete/<int:id>')
@login_required
def delete_category(id):
    conn = get_db()
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
    wallets = conn.execute("SELECT * FROM wallets ORDER BY sort_order").fetchall()
    active = get_active_wallet(wallets)

    if active == 'all':
        monthly = conn.execute('''
            SELECT strftime('%Y-%m', date) as month,
                SUM(CASE WHEN type='income' THEN amount ELSE 0 END) as income,
                SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) as expense
            FROM transactions
            GROUP BY month ORDER BY month DESC LIMIT 12''').fetchall()
        by_category = conn.execute('''
            SELECT name, SUM(total) as total FROM (
                SELECT c.name, SUM(ti.amount) as total
                FROM transaction_items ti
                JOIN categories c ON ti.category_id = c.id
                JOIN transactions t ON ti.transaction_id = t.id
                WHERE t.type='expense'
                GROUP BY c.name
                UNION ALL
                SELECT c.name, SUM(t.amount) as total
                FROM transactions t JOIN categories c ON t.category_id = c.id
                WHERE t.type='expense'
                AND t.id NOT IN (
                    SELECT DISTINCT transaction_id FROM transaction_items WHERE category_id IS NOT NULL)
                GROUP BY c.name
            ) GROUP BY name ORDER BY total DESC''').fetchall()
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


init_db()

if __name__ == '__main__':
    app.run(debug=True)
