from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'koltsegvetes_secret_key'

DATABASE = 'koltsegvetes.db'


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('income', 'expense'))
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            description TEXT,
            category_id INTEGER,
            type TEXT NOT NULL CHECK(type IN ('income', 'expense')),
            date TEXT NOT NULL,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        )
    ''')

    cursor.execute("SELECT COUNT(*) FROM categories")
    if cursor.fetchone()[0] == 0:
        default_categories = [
            ('Fizetés', 'income'),
            ('Egyéb bevétel', 'income'),
            ('Lakás', 'expense'),
            ('Élelmiszer', 'expense'),
            ('Közlekedés', 'expense'),
            ('Szórakozás', 'expense'),
            ('Egészség', 'expense'),
            ('Egyéb kiadás', 'expense'),
        ]
        cursor.executemany("INSERT INTO categories (name, type) VALUES (?, ?)", default_categories)

    conn.commit()
    conn.close()


@app.route('/')
def index():
    conn = get_db()

    income = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='income'").fetchone()[0]
    expense = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type='expense'").fetchone()[0]
    balance = income - expense

    transactions = conn.execute('''
        SELECT t.*, c.name as category_name
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        ORDER BY t.date DESC
        LIMIT 10
    ''').fetchall()

    conn.close()
    return render_template('index.html', balance=balance, income=income, expense=expense, transactions=transactions)


@app.route('/transactions', methods=['GET', 'POST'])
def transactions():
    conn = get_db()

    if request.method == 'POST':
        amount = request.form['amount']
        description = request.form['description']
        category_id = request.form['category_id']
        type_ = request.form['type']
        date = request.form['date']

        conn.execute(
            "INSERT INTO transactions (amount, description, category_id, type, date) VALUES (?, ?, ?, ?, ?)",
            (amount, description, category_id, type_, date)
        )
        conn.commit()
        flash('Tranzakció sikeresen hozzáadva!', 'success')
        return redirect(url_for('transactions'))

    all_transactions = conn.execute('''
        SELECT t.*, c.name as category_name
        FROM transactions t
        LEFT JOIN categories c ON t.category_id = c.id
        ORDER BY t.date DESC
    ''').fetchall()

    categories = conn.execute("SELECT * FROM categories ORDER BY type, name").fetchall()
    conn.close()

    today = datetime.today().strftime('%Y-%m-%d')
    return render_template('transactions.html', transactions=all_transactions, categories=categories, today=today)


@app.route('/transactions/delete/<int:id>')
def delete_transaction(id):
    conn = get_db()
    conn.execute("DELETE FROM transactions WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash('Tranzakció törölve!', 'info')
    return redirect(url_for('transactions'))


@app.route('/categories', methods=['GET', 'POST'])
def categories():
    conn = get_db()

    if request.method == 'POST':
        name = request.form['name']
        type_ = request.form['type']
        conn.execute("INSERT INTO categories (name, type) VALUES (?, ?)", (name, type_))
        conn.commit()
        flash('Kategória sikeresen hozzáadva!', 'success')
        return redirect(url_for('categories'))

    all_categories = conn.execute("SELECT * FROM categories ORDER BY type, name").fetchall()
    conn.close()
    return render_template('categories.html', categories=all_categories)


@app.route('/categories/delete/<int:id>')
def delete_category(id):
    conn = get_db()
    conn.execute("DELETE FROM categories WHERE id=?", (id,))
    conn.commit()
    conn.close()
    flash('Kategória törölve!', 'info')
    return redirect(url_for('categories'))


@app.route('/reports')
def reports():
    conn = get_db()

    monthly = conn.execute('''
        SELECT
            strftime('%Y-%m', date) as month,
            SUM(CASE WHEN type='income' THEN amount ELSE 0 END) as income,
            SUM(CASE WHEN type='expense' THEN amount ELSE 0 END) as expense
        FROM transactions
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    ''').fetchall()

    by_category = conn.execute('''
        SELECT c.name, SUM(t.amount) as total
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.type='expense'
        GROUP BY c.name
        ORDER BY total DESC
    ''').fetchall()

    conn.close()
    return render_template('reports.html', monthly=monthly, by_category=by_category)


if __name__ == '__main__':
    init_db()
    app.run(debug=True)
