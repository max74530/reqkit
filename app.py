"""
ReqKit - 零用金與申購單管理系統
Python + Flask + SQLite

權限模型：
  - 一般使用者（user）：輸入姓名即可進入，僅能維護自己的資料
  - 管理員（admin）  ：進入需輸入密碼，可用全部功能
                     （會計端審核、統計分析、人員管理、匯出、備份、年度封存）

環境變數請參閱 .env.example
"""

import os
import io
import csv
import sqlite3
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, g, send_from_directory, Response
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('REQKIT_DATA_DIR', os.path.join(BASE_DIR, 'data'))
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault('REQKIT_DB_DIR', DATA_DIR)
os.environ.setdefault('REQKIT_ARCHIVE_DIR', os.path.join(DATA_DIR, 'archive'))

# SECRET_KEY：優先讀環境變數，否則沿用本機持久化檔案，避免重啟後 token 失效
def _load_secret_key():
    env_key = os.environ.get('REQKIT_SECRET_KEY')
    if env_key:
        return env_key
    key_file = os.path.join(DATA_DIR, '.secret_key')
    if os.path.exists(key_file):
        return io.open(key_file, encoding='utf-8').read().strip()
    key = secrets.token_hex(32)
    io.open(key_file, 'w', encoding='utf-8').write(key)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__, static_folder='static', static_url_path='')
app.config['SECRET_KEY'] = _load_secret_key()

# ── 路徑設定 ──────────────────────────────────────────────
DB_DIR = os.environ['REQKIT_DB_DIR']
DB_PATH = os.path.join(DB_DIR, 'reqkit.db')

# ── 預設值 ────────────────────────────────────────────────
DEFAULT_CATEGORIES = ['用餐', '車資', '飲料', '加油', '停車費', '過路費', '其他']
PAYMENT_METHODS = ['現金', '銀行轉帳']
BOOTSTRAP_ADMIN = os.environ.get('REQKIT_ADMIN_USER', 'admin')
ADMIN_DISPLAY_NAME = os.environ.get('REQKIT_ADMIN_NAME', 'Admin')
DEFAULT_ADMIN_PASSWORD = os.environ.get('REQKIT_ADMIN_PASSWORD', '')
TOKEN_TTL_HOURS = int(os.environ.get('REQKIT_TOKEN_HOURS', '12'))
PORT = int(os.environ.get('PORT', '2000'))

# ── 資料庫連線 ────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def row_to_dict(row):
    return dict(row) if row is not None else None


# ── 初始化 / 遷移 ─────────────────────────────────────────
def _table_columns(db, table):
    return {r['name'] for r in db.execute(f"PRAGMA table_info({table})")}


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            display_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS petty_cash (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            applicant_id INTEGER NOT NULL,
            activity TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT '待審核',
            remark TEXT,
            invoice_date TEXT,
            confirmed_amount REAL,
            payment_date TEXT,
            payment_method TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (applicant_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS petty_cash_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            petty_cash_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            FOREIGN KEY (petty_cash_id) REFERENCES petty_cash(id) ON DELETE CASCADE,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );

        CREATE TABLE IF NOT EXISTS requisitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            requester_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            total_price REAL NOT NULL,
            approved_by_wang INTEGER DEFAULT 0,
            is_ordered INTEGER DEFAULT 0,
            completion_date TEXT,
            other TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (requester_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS admin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_name TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        /* ── 年度明細檔（封存快照，歷史查詢用）── */
        CREATE TABLE IF NOT EXISTS archive_petty (
            id INTEGER PRIMARY KEY,
            year TEXT NOT NULL,
            date TEXT NOT NULL,
            applicant_id INTEGER NOT NULL,
            applicant_name TEXT,
            activity TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT,
            remark TEXT,
            invoice_date TEXT,
            confirmed_amount REAL,
            payment_date TEXT,
            payment_method TEXT,
            items TEXT,
            created_at TEXT,
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS archive_requisitions (
            id INTEGER PRIMARY KEY,
            year TEXT NOT NULL,
            date TEXT NOT NULL,
            requester_id INTEGER NOT NULL,
            requester_name TEXT,
            item_name TEXT NOT NULL,
            qty INTEGER,
            total_price REAL,
            approved_by_wang INTEGER,
            is_ordered INTEGER,
            completion_date TEXT,
            other TEXT,
            created_at TEXT,
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        /* ── 備份紀錄 ── */
        CREATE TABLE IF NOT EXISTS backup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year TEXT,
            month TEXT,
            kind TEXT NOT NULL,
            file_path TEXT,
            petty_count INTEGER DEFAULT 0,
            req_count INTEGER DEFAULT 0,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    ''')

    # ── 欄位遷移 ──────────────────────────────────────────
    cols = _table_columns(db, 'users')
    if 'last_login_at' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
    if 'is_deleted' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
    if 'password' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN password TEXT")

    # ── 種子資料 ──────────────────────────────────────────
    if db.execute("SELECT COUNT(*) c FROM users").fetchone()['c'] == 0:
        if not DEFAULT_ADMIN_PASSWORD:
            raise SystemExit(
                '[錯誤] 首次啟動必須設定管理員密碼\n'
                '       請設定環境變數 REQKIT_ADMIN_PASSWORD 後再執行'
            )
        db.execute(
            "INSERT INTO users (username, password, role, display_name) VALUES (?, ?, 'admin', ?)",
            (BOOTSTRAP_ADMIN, generate_password_hash(DEFAULT_ADMIN_PASSWORD), ADMIN_DISPLAY_NAME),
        )

    # 確保預設管理員存在且有密碼
    adm = db.execute("SELECT id, password FROM users WHERE username = ?",
                     (BOOTSTRAP_ADMIN,)).fetchone()
    if not adm:
        db.execute(
            "INSERT INTO users (username, password, role, display_name) VALUES (?, ?, 'admin', ?)",
            (BOOTSTRAP_ADMIN, generate_password_hash(DEFAULT_ADMIN_PASSWORD), ADMIN_DISPLAY_NAME)
        )
    elif not adm['password']:
        db.execute("UPDATE users SET password = ? WHERE id = ?",
                   (generate_password_hash(DEFAULT_ADMIN_PASSWORD), adm['id']))

    # 其他管理員若尚未設定密碼，給予預設密碼（避免無法登入）
    for r in db.execute(
            "SELECT id FROM users WHERE role = 'admin' AND is_deleted = 0 "
            "AND (password IS NULL OR password = '')"
    ).fetchall():
        db.execute("UPDATE users SET password = ? WHERE id = ?",
                   (generate_password_hash(DEFAULT_ADMIN_PASSWORD), r['id']))

    for i, cat in enumerate(DEFAULT_CATEGORIES):
        db.execute("INSERT OR IGNORE INTO categories (name, sort_order) VALUES (?, ?)", (cat, i))

    db.commit()
    db.close()


# ── 認證 ──────────────────────────────────────────────────
def current_identity():
    """從 token 解析出目前使用者，並回填到 g"""
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.startswith('Bearer ') else ''
    if not token:
        return None

    db = get_db()
    row = db.execute(
        '''SELECT u.id, u.username, u.role, u.display_name, u.is_deleted, t.expires_at
           FROM tokens t JOIN users u ON t.user_id = u.id
           WHERE t.token = ?''',
        (token,)
    ).fetchone()
    if not row or row['is_deleted']:
        return None

    if datetime.fromisoformat(row['expires_at']) < datetime.now():
        db.execute("DELETE FROM tokens WHERE token = ?", (token,))
        db.commit()
        return None

    return {'id': row['id'], 'username': row['username'],
            'role': row['role'], 'display_name': row['display_name']}


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        me = current_identity()
        if not me:
            return jsonify({'error': '請重新進入系統'}), 401
        g.me = me
        g.is_admin = (me['role'] == 'admin')
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """僅限管理員；未登入→401，非管理員→403"""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if not g.is_admin:
            return jsonify({'error': '此功能僅限管理員使用'}), 403
        return f(*args, **kwargs)
    return decorated


def log_admin(action, target=None, detail=None):
    db = get_db()
    db.execute(
        "INSERT INTO admin_log (actor_name, action, target, detail) VALUES (?, ?, ?, ?)",
        (g.me['username'], action, target, detail)
    )
    db.commit()


# ── 共用工具 ──────────────────────────────────────────────
def re_month_ok(month):
    try:
        datetime.strptime(month, '%Y-%m')
        return True
    except Exception:
        return False


def re_year_ok(year):
    try:
        if len(str(year)) != 4:
            return False
        datetime.strptime(str(year), '%Y')
        return True
    except Exception:
        return False


def archived_petty_dict(row):
    """年度明細檔的零用金資料（固定欄位，供歷史查詢）"""
    return {
        'id': row['id'],
        'date': row['date'],
        'applicant_id': row['applicant_id'],
        'applicant_name': row['applicant_name'] or '',
        'activity': row['activity'],
        'amount': row['amount'],
        'status': row['status'],
        'remark': row['remark'] or '',
        'items': (row['items'] or '').split(';') if row['items'] else [],
        'invoice_date': row['invoice_date'] or '',
        'confirmed_amount': row['confirmed_amount'] if row['confirmed_amount'] is not None else row['amount'],
        'payment_date': row['payment_date'] or '',
        'payment_method': row['payment_method'] or '',
        'archived': True,
    }


def archived_req_dict(row):
    return {
        'id': row['id'],
        'date': row['date'],
        'requester_id': row['requester_id'],
        'requester_name': row['requester_name'] or '',
        'item_name': row['item_name'],
        'qty': row['qty'],
        'total_price': row['total_price'],
        'approved_by_wang': bool(row['approved_by_wang']),
        'is_ordered': bool(row['is_ordered']),
        'completion_date': row['completion_date'] or '',
        'other': row['other'] or '',
        'archived': True,
    }


def petty_dict(row, db, is_admin):
    items = db.execute(
        '''SELECT c.name FROM petty_cash_items pci
           JOIN categories c ON pci.category_id = c.id
           WHERE pci.petty_cash_id = ? ORDER BY c.sort_order, c.id''',
        (row['id'],)
    ).fetchall()

    d = {
        'id': row['id'],
        'date': row['date'],
        'applicant_id': row['applicant_id'],
        'applicant_name': row['display_name'] or row['username'],
        'activity': row['activity'],
        'amount': row['amount'],
        'status': row['status'],
        'remark': row['remark'] or '',
        'items': [i['name'] for i in items],
        'created_at': row['created_at'],
    }
    # 會計欄位僅管理員可見
    if is_admin:
        d['invoice_date'] = row['invoice_date'] or ''
        d['confirmed_amount'] = row['confirmed_amount'] if row['confirmed_amount'] is not None else row['amount']
        d['payment_date'] = row['payment_date'] or ''
        d['payment_method'] = row['payment_method'] or ''
    return d


def req_dict(row, is_admin):
    return {
        'id': row['id'],
        'date': row['date'],
        'requester_id': row['requester_id'],
        'requester_name': row['display_name'] or row['requester_username'] or '',
        'item_name': row['item_name'],
        'qty': row['qty'],
        'total_price': row['total_price'],
        'approved_by_wang': bool(row['approved_by_wang']),
        'is_ordered': bool(row['is_ordered']),
        'completion_date': row['completion_date'] or '',
        'other': row['other'] or '',
        'created_at': row['created_at'],
    }


# ── 進入系統（姓名判定身分）──────────────────────────────
@app.route('/api/session', methods=['POST'])
def create_session():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': '請輸入姓名'}), 400
    if len(name) > 40:
        return jsonify({'error': '姓名過長'}), 400

    db = get_db()
    row = db.execute(
        "SELECT id, username, role, display_name, password FROM users "
        "WHERE username = ? AND is_deleted = 0",
        (name,)
    ).fetchone()

    if row:
        # 管理員需驗證密碼
        if row['role'] == 'admin':
            pw = data.get('password') or ''
            if not row['password']:
                return jsonify({'error': '此管理員尚未設定密碼，請聯絡系統負責人'}), 401
            if not check_password_hash(row['password'], pw):
                return jsonify({'error': '管理員密碼錯誤'}), 401
        user_id, role, display = row['id'], row['role'], row['display_name']
    else:
        # 未登記者自動加入名冊（一般使用者）
        try:
            cur = db.execute(
                "INSERT INTO users (username, role, display_name) VALUES (?, 'user', ?)",
                (name, name)
            )
            user_id = cur.lastrowid
            role, display = 'user', name
        except sqlite3.IntegrityError:
            # 已存在但被停用 → 重新啟用
            db.execute("UPDATE users SET is_deleted = 0 WHERE username = ?", (name,))
            r2 = db.execute(
                "SELECT id, role, display_name, password FROM users WHERE username = ?",
                (name,)).fetchone()
            if r2['role'] == 'admin':
                pw = data.get('password') or ''
                if not r2['password'] or not check_password_hash(r2['password'], pw):
                    return jsonify({'error': '管理員密碼錯誤'}), 401
            user_id, role, display = r2['id'], r2['role'], r2['display_name']

    now = datetime.now().isoformat()
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))

    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
        (user_id, token, (datetime.now() + timedelta(hours=TOKEN_TTL_HOURS)).isoformat())
    )
    db.commit()

    return jsonify({
        'token': token,
        'user': {'id': user_id, 'username': name, 'role': role, 'display_name': display},
    })


@app.route('/api/entry-names', methods=['GET'])
def entry_names():
    """進入畫面用的人員清單（免登入）。最近登入者排在前面。"""
    db = get_db()
    rows = db.execute(
        '''SELECT username, display_name, role, last_login_at
           FROM users WHERE is_deleted = 0
           ORDER BY (last_login_at IS NULL), last_login_at DESC, id'''
    ).fetchall()
    return jsonify([
        {
            'username': r['username'],
            'display_name': r['display_name'] or r['username'],
            'role': r['role'],
            'last_login_at': (r['last_login_at'] or '')[:16].replace('T', ' '),
        }
        for r in rows
    ])


@app.route('/api/me', methods=['GET'])
@require_auth
def api_me():
    return jsonify({
        'user': g.me,
        'is_admin': g.is_admin,
        'payment_methods': PAYMENT_METHODS,
    })


@app.route('/api/logout', methods=['POST'])
@require_auth
def api_logout():
    auth = request.headers.get('Authorization', '')
    db = get_db()
    db.execute("DELETE FROM tokens WHERE token = ?", (auth[7:],))
    db.commit()
    return jsonify({'message': '已切換身分'})


# ── 申請項目類別 ──────────────────────────────────────────
@app.route('/api/categories', methods=['GET'])
@require_auth
def list_categories():
    db = get_db()
    rows = db.execute("SELECT * FROM categories ORDER BY sort_order, id").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route('/api/categories', methods=['POST'])
@require_admin
def create_category():
    name = ((request.get_json(silent=True) or {}).get('name') or '').strip()
    if not name:
        return jsonify({'error': '名稱不可為空'}), 400
    db = get_db()
    try:
        cur = db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        db.commit()
        log_admin('新增申請項目', name)
        return jsonify({'id': cur.lastrowid, 'name': name}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': '此類別已存在'}), 409


@app.route('/api/categories/<int:cat_id>', methods=['DELETE'])
@require_admin
def delete_category(cat_id):
    db = get_db()
    used = db.execute("SELECT COUNT(*) c FROM petty_cash_items WHERE category_id = ?", (cat_id,)).fetchone()['c']
    if used > 0:
        return jsonify({'error': f'此類別已被 {used} 筆申請使用，無法刪除'}), 400
    name = db.execute("SELECT name FROM categories WHERE id = ?", (cat_id,)).fetchone()
    db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    db.commit()
    log_admin('刪除申請項目', name['name'] if name else str(cat_id))
    return jsonify({'message': '已刪除'})


# ── 零用金 ────────────────────────────────────────────────
@app.route('/api/petty', methods=['GET'])
@require_auth
def list_petty():
    month = request.args.get('month', '')
    year = request.args.get('year', '')
    db = get_db()

    # 年度明細檔（歷史唯讀）
    if year:
        if not re_year_ok(year):
            return jsonify({'error': '年份格式錯誤'}), 400
        rows = db.execute(
            "SELECT * FROM archive_petty WHERE year = ? ORDER BY date DESC, id DESC", (year,)
        ).fetchall()
        if not g.is_admin:
            rows = [r for r in rows if r['applicant_name'] == g.me['display_name']]
        return jsonify([archived_petty_dict(r) for r in rows])

    sql = '''SELECT pc.*, u.username, u.display_name
             FROM petty_cash pc JOIN users u ON pc.applicant_id = u.id'''
    args = []
    if month:
        if not re_month_ok(month):
            return jsonify({'error': '月份格式錯誤'}), 400
        sql += " WHERE pc.date LIKE ?"
        args.append(month + '%')
    if not g.is_admin:
        # 一般使用者僅看自己的資料
        sql += (" AND pc.applicant_id = ?" if ' WHERE' in sql else " WHERE pc.applicant_id = ?")
        args.append(g.me['id'])
    sql += " ORDER BY pc.date DESC, pc.id DESC"

    rows = db.execute(sql, args).fetchall()
    return jsonify([petty_dict(r, db, g.is_admin) for r in rows])


@app.route('/api/petty', methods=['POST'])
@require_auth
def create_petty():
    d = request.get_json(silent=True) or {}
    date = (d.get('date') or '').strip()
    activity = (d.get('activity') or '').strip()
    remark = (d.get('remark') or '').strip()
    items = d.get('items') or []
    try:
        amount = float(d.get('amount') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': '金額格式不正確'}), 400

    if not date or not activity:
        return jsonify({'error': '申請日期與活動內容為必填'}), 400
    if amount < 0:
        return jsonify({'error': '金額不可為負數'}), 400
    if not items:
        return jsonify({'error': '請至少勾選一項申請項目'}), 400

    db = get_db()
    cur = db.execute(
        '''INSERT INTO petty_cash (date, applicant_id, activity, amount, status, remark)
           VALUES (?, ?, ?, ?, '待審核', ?)''',
        (date, g.me['id'], activity, amount, remark)
    )
    pid = cur.lastrowid
    for cat_name in items:
        cat = db.execute("SELECT id FROM categories WHERE name = ?", (cat_name,)).fetchone()
        if cat:
            db.execute("INSERT INTO petty_cash_items (petty_cash_id, category_id) VALUES (?, ?)", (pid, cat['id']))
    db.commit()
    return jsonify({'id': pid, 'message': '已建立'}), 201


@app.route('/api/petty/<int:pid>', methods=['PUT'])
@require_auth
def update_petty(pid):
    db = get_db()
    row = db.execute("SELECT * FROM petty_cash WHERE id = ?", (pid,)).fetchone()
    if not row:
        return jsonify({'error': '找不到此筆資料'}), 404
    if not g.is_admin and row['applicant_id'] != g.me['id']:
        return jsonify({'error': '您只能修改自己建立的申請單'}), 403

    d = request.get_json(silent=True) or {}
    date = (d.get('date') or row['date']).strip()
    activity = (d.get('activity') or row['activity']).strip()
    remark = d.get('remark', row['remark'])
    remark = (remark or '').strip()
    try:
        amount = float(d.get('amount', row['amount']))
    except (TypeError, ValueError):
        return jsonify({'error': '金額格式不正確'}), 400
    if amount < 0:
        return jsonify({'error': '金額不可為負數'}), 400
    items = d.get('items')

    db.execute(
        '''UPDATE petty_cash SET date=?, activity=?, amount=?, remark=?,
           updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (date, activity, amount, remark, pid)
    )
    if items is not None:
        if not items:
            return jsonify({'error': '請至少勾選一項申請項目'}), 400
        db.execute("DELETE FROM petty_cash_items WHERE petty_cash_id = ?", (pid,))
        for cat_name in items:
            cat = db.execute("SELECT id FROM categories WHERE name = ?", (cat_name,)).fetchone()
            if cat:
                db.execute("INSERT INTO petty_cash_items (petty_cash_id, category_id) VALUES (?, ?)", (pid, cat['id']))
    db.commit()
    return jsonify({'message': '已更新'})


@app.route('/api/petty/<int:pid>', methods=['DELETE'])
@require_auth
def delete_petty(pid):
    db = get_db()
    row = db.execute("SELECT applicant_id FROM petty_cash WHERE id = ?", (pid,)).fetchone()
    if not row:
        return jsonify({'error': '找不到此筆資料'}), 404
    if not g.is_admin and row['applicant_id'] != g.me['id']:
        return jsonify({'error': '您只能刪除自己建立的申請單'}), 403
    db.execute("DELETE FROM petty_cash WHERE id = ?", (pid,))
    db.commit()
    return jsonify({'message': '已刪除'})


# ── 會計端審核（僅管理員）─────────────────────────────────
@app.route('/api/petty/<int:pid>/accounting', methods=['PUT'])
@require_admin
def update_accounting(pid):
    db = get_db()
    row = db.execute("SELECT * FROM petty_cash WHERE id = ?", (pid,)).fetchone()
    if not row:
        return jsonify({'error': '找不到此筆資料'}), 404

    d = request.get_json(silent=True) or {}
    confirmed = d.get('confirmed_amount', row['confirmed_amount'])
    try:
        confirmed = float(confirmed) if confirmed not in (None, '') else row['amount']
    except (TypeError, ValueError):
        return jsonify({'error': '確認金額格式不正確'}), 400

    status = d.get('status', row['status'])
    if status not in ('待審核', '已撥款', '已退回'):
        return jsonify({'error': '狀態值不合法'}), 400

    payment_method = d.get('payment_method', row['payment_method']) or ''
    if payment_method and payment_method not in PAYMENT_METHODS:
        return jsonify({'error': f'支付方式不合法，僅限：{"、".join(PAYMENT_METHODS)}'}), 400

    db = get_db()
    db.execute(
        '''UPDATE petty_cash SET invoice_date=?, confirmed_amount=?, payment_date=?,
           payment_method=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (d.get('invoice_date', row['invoice_date']) or '',
         confirmed,
         d.get('payment_date', row['payment_date']) or '',
         payment_method,
         status, pid)
    )
    db.commit()
    return jsonify({'message': '已更新'})


# ── 申購單 ────────────────────────────────────────────────
@app.route('/api/requisitions', methods=['GET'])
@require_auth
def list_requisitions():
    month = request.args.get('month', '')
    year = request.args.get('year', '')
    db = get_db()

    if year:
        if not re_year_ok(year):
            return jsonify({'error': '年份格式錯誤'}), 400
        rows = db.execute(
            "SELECT * FROM archive_requisitions WHERE year = ? ORDER BY date DESC, id DESC", (year,)
        ).fetchall()
        if not g.is_admin:
            rows = [r for r in rows if r['requester_name'] == g.me['display_name']]
        return jsonify([archived_req_dict(r) for r in rows])

    sql = '''SELECT r.*, u.username AS requester_username, u.display_name
             FROM requisitions r JOIN users u ON r.requester_id = u.id'''
    args = []
    if month:
        if not re_month_ok(month):
            return jsonify({'error': '月份格式錯誤'}), 400
        sql += " WHERE r.date LIKE ?"
        args.append(month + '%')
    if not g.is_admin:
        sql += (" AND r.requester_id = ?" if ' WHERE' in sql else " WHERE r.requester_id = ?")
        args.append(g.me['id'])
    sql += " ORDER BY r.date DESC, r.id DESC"

    rows = db.execute(sql, args).fetchall()
    return jsonify([req_dict(r, g.is_admin) for r in rows])


@app.route('/api/requisitions', methods=['POST'])
@require_auth
def create_requisition():
    d = request.get_json(silent=True) or {}
    date = (d.get('date') or '').strip()
    item_name = (d.get('item_name') or '').strip()
    other = (d.get('other') or '').strip()
    try:
        qty = int(d.get('qty') or 1)
        total = float(d.get('total_price') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': '數量或金額格式不正確'}), 400

    if not date or not item_name:
        return jsonify({'error': '日期與申購項目為必填'}), 400
    if qty < 1:
        return jsonify({'error': '數量至少為 1'}), 400
    if total < 0:
        return jsonify({'error': '金額不可為負數'}), 400

    db = get_db()
    cur = db.execute(
        '''INSERT INTO requisitions (date, requester_id, item_name, qty, total_price, other)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (date, g.me['id'], item_name, qty, total, other)
    )
    db.commit()
    return jsonify({'id': cur.lastrowid, 'message': '已建立'}), 201


@app.route('/api/requisitions/<int:rid>', methods=['PUT'])
@require_auth
def update_requisition(rid):
    db = get_db()
    row = db.execute("SELECT * FROM requisitions WHERE id = ?", (rid,)).fetchone()
    if not row:
        return jsonify({'error': '找不到此筆資料'}), 404

    d = request.get_json(silent=True) or {}

    # 一般使用者不可執行核准動作
    if not g.is_admin and ('approved_by_wang' in d or 'is_ordered' in d):
        return jsonify({'error': '核准與訂購狀態僅管理員可變更'}), 403
    if not g.is_admin and row['requester_id'] != g.me['id']:
        return jsonify({'error': '您只能修改自己建立的申購單'}), 403

    date = (d.get('date') or row['date']).strip()
    item_name = (d.get('item_name') or row['item_name']).strip()
    other = d.get('other', row['other'])
    other = (other or '').strip()
    try:
        qty = int(d.get('qty', row['qty']))
        total = float(d.get('total_price', row['total_price']))
    except (TypeError, ValueError):
        return jsonify({'error': '數量或金額格式不正確'}), 400
    if qty < 1:
        return jsonify({'error': '數量至少為 1'}), 400
    if total < 0:
        return jsonify({'error': '金額不可為負數'}), 400

    approved = 1 if d.get('approved_by_wang', row['approved_by_wang']) else 0
    ordered = 1 if d.get('is_ordered', row['is_ordered']) else 0
    completion = d.get('completion_date', row['completion_date']) or ''
    if ordered and not completion:
        completion = datetime.now().strftime('%Y-%m-%d')

    db.execute(
        '''UPDATE requisitions SET date=?, item_name=?, qty=?, total_price=?, other=?,
           approved_by_wang=?, is_ordered=?, completion_date=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?''',
        (date, item_name, qty, total, other, approved, ordered, completion, rid)
    )
    db.commit()
    return jsonify({'message': '已更新'})


@app.route('/api/requisitions/<int:rid>', methods=['DELETE'])
@require_auth
def delete_requisition(rid):
    db = get_db()
    row = db.execute("SELECT requester_id FROM requisitions WHERE id = ?", (rid,)).fetchone()
    if not row:
        return jsonify({'error': '找不到此筆資料'}), 404
    if not g.is_admin and row['requester_id'] != g.me['id']:
        return jsonify({'error': '您只能刪除自己建立的申購單'}), 403
    db.execute("DELETE FROM requisitions WHERE id = ?", (rid,))
    db.commit()
    return jsonify({'message': '已刪除'})


# ── 統計分析（僅管理員）───────────────────────────────────
@app.route('/api/stats/overview', methods=['GET'])
@require_admin
def stats_overview():
    month = request.args.get('month', '')
    year = request.args.get('year', '')
    db = get_db()

    if year:
        if not re_year_ok(year):
            return jsonify({'error': '年份格式錯誤'}), 400
        p = db.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM archive_petty WHERE year = ?",
            (year,)).fetchone()
        pend = db.execute(
            "SELECT COUNT(*) c FROM archive_petty WHERE year = ? AND status = '待審核'",
            (year,)).fetchone()
        r_ = db.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(total_price),0) s FROM archive_requisitions WHERE year = ?",
            (year,)).fetchone()
        rpend = db.execute(
            "SELECT COUNT(*) c FROM archive_requisitions WHERE year = ? AND approved_by_wang = 0",
            (year,)).fetchone()
        return jsonify({'month': year, 'petty_count': p['c'], 'petty_amount': p['s'],
                        'petty_pending': pend['c'], 'req_count': r_['c'],
                        'req_amount': r_['s'], 'req_pending': rpend['c']})

    where, args = "", []
    if month:
        if not re_month_ok(month):
            return jsonify({'error': '月份格式錯誤'}), 400
        where, args = "WHERE date LIKE ?", [month + '%']

    petty = db.execute(f"SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM petty_cash {where}", args).fetchone()
    pending = db.execute(f"SELECT COUNT(*) c FROM petty_cash {where}{' AND' if where else 'WHERE'} status = '待審核'", args).fetchone()
    reqs = db.execute(f"SELECT COUNT(*) c, COALESCE(SUM(total_price),0) s FROM requisitions {where}", args).fetchone()
    req_pending = db.execute(f"SELECT COUNT(*) c FROM requisitions {where}{' AND' if where else 'WHERE'} approved_by_wang = 0", args).fetchone()

    return jsonify({
        'month': month,
        'petty_count': petty['c'], 'petty_amount': petty['s'], 'petty_pending': pending['c'],
        'req_count': reqs['c'], 'req_amount': reqs['s'], 'req_pending': req_pending['c'],
    })


@app.route('/api/stats/by-category', methods=['GET'])
@require_admin
def stats_by_category():
    month = request.args.get('month', '')
    db = get_db()
    args, where = [], "WHERE pc.status != '已退回'"
    if month:
        if not re_month_ok(month):
            return jsonify({'error': '月份格式錯誤'}), 400
        where += " AND pc.date LIKE ?"
        args.append(month + '%')

    rows = db.execute(
        f'''SELECT c.name AS name, COUNT(*) AS cnt, COALESCE(SUM(pc.amount),0) AS amount
            FROM petty_cash_items pci
            JOIN categories c ON c.id = pci.category_id
            JOIN petty_cash pc ON pc.id = pci.petty_cash_id
            {where}
            GROUP BY c.id, c.name ORDER BY cnt DESC, amount DESC''',
        args
    ).fetchall()
    return jsonify({'rows': [row_to_dict(r) for r in rows]})


@app.route('/api/stats/by-person', methods=['GET'])
@require_admin
def stats_by_person():
    month = request.args.get('month', '')
    kind = request.args.get('kind', 'petty')
    db = get_db()

    if kind == 'req':
        sql, col, tbl, uid, name = (
            "SELECT u.display_name AS name, u.username, COUNT(*) AS cnt, COALESCE(SUM(r.total_price),0) AS amount "
            "FROM requisitions r JOIN users u ON r.requester_id = u.id", 'total_price', 'requisitions',
            'requester_id', 'display_name')
    else:
        sql, col, tbl, uid, name = (
            "SELECT u.display_name AS name, u.username, COUNT(*) AS cnt, COALESCE(SUM(p.amount),0) AS amount "
            "FROM petty_cash p JOIN users u ON p.applicant_id = u.id", 'amount', 'petty_cash',
            'applicant_id', 'display_name')

    args, where = [], ""
    if month:
        if not re_month_ok(month):
            return jsonify({'error': '月份格式錯誤'}), 400
        where = f"WHERE {tbl[0]}.date LIKE ?"
        args.append(month + '%')

    rows = db.execute(f"{sql} {where} GROUP BY u.id ORDER BY amount DESC", args).fetchall()
    return jsonify({'rows': [row_to_dict(r) for r in rows], 'kind': kind})


@app.route('/api/stats/monthly-trend', methods=['GET'])
@require_admin
def stats_trend():
    db = get_db()
    rows = db.execute(
        '''SELECT substr(date,1,7) AS month, COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS amount
           FROM petty_cash GROUP BY month ORDER BY month DESC LIMIT 12'''
    ).fetchall()
    return jsonify({'rows': list(reversed([row_to_dict(r) for r in rows]))})


# ── 人員管理（僅管理員）───────────────────────────────────
@app.route('/api/people', methods=['GET'])
@require_admin
def list_people():
    db = get_db()
    rows = db.execute(
        '''SELECT u.id, u.username, u.display_name, u.role, u.created_at, u.last_login_at,
                  (SELECT COUNT(*) FROM petty_cash p WHERE p.applicant_id = u.id) AS petty_count,
                  (SELECT COALESCE(SUM(amount),0) FROM petty_cash p WHERE p.applicant_id = u.id) AS petty_total,
                  (SELECT COUNT(*) FROM requisitions r WHERE r.requester_id = u.id) AS req_count,
                  (SELECT COALESCE(SUM(total_price),0) FROM requisitions r WHERE r.requester_id = u.id) AS req_total
           FROM users u
           WHERE u.is_deleted = 0
           ORDER BY CASE u.role WHEN 'admin' THEN 0 ELSE 1 END, u.id'''
    ).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route('/api/people', methods=['POST'])
@require_admin
def create_person():
    d = request.get_json(silent=True) or {}
    name = (d.get('username') or '').strip()
    role = d.get('role', 'user')
    display = (d.get('display_name') or name).strip()
    if not name:
        return jsonify({'error': '請輸入姓名'}), 400
    if role not in ('user', 'admin'):
        return jsonify({'error': '角色不合法'}), 400

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (username, role, display_name, is_deleted) VALUES (?, ?, ?, 0)",
            (name, role, display)
        )
        db.commit()
        log_admin('新增人員', name, f'role={role}')
        return jsonify({'id': cur.lastrowid, 'message': '已新增'}), 201
    except sqlite3.IntegrityError:
        db.execute("UPDATE users SET is_deleted = 0, role = ?, display_name = ? WHERE username = ?", (role, display, name))
        db.commit()
        log_admin('啟用人員', name, f'role={role}')
        return jsonify({'message': '該姓名已存在，已重新啟用'}), 200


@app.route('/api/people/<int:uid>', methods=['PUT'])
@require_admin
def update_person(uid):
    d = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return jsonify({'error': '找不到該人員'}), 404

    role = d.get('role', row['role'])
    if role not in ('user', 'admin'):
        return jsonify({'error': '角色不合法'}), 400
    display = (d.get('display_name') or row['display_name']).strip()
    name = (d.get('username') or row['username']).strip()

    if not name:
        return jsonify({'error': '姓名不可為空'}), 400

    # 預設管理員（admin）不可改名或降級
    if row['username'] == BOOTSTRAP_ADMIN and (name != BOOTSTRAP_ADMIN or role != 'admin'):
        return jsonify({'error': f'預設管理員 {BOOTSTRAP_ADMIN} 不可改名或降級'}), 400

    # 防止把最後一位管理員降級
    if row['role'] == 'admin' and role != 'admin':
        admin_cnt = db.execute("SELECT COUNT(*) c FROM users WHERE role='admin' AND is_deleted=0").fetchone()['c']
        if admin_cnt <= 1:
            return jsonify({'error': '系統至少需保留一位管理員'}), 400

    # 設定/更新密碼（僅對管理員有效）
    pw = d.get('password')
    if pw:
        if role != 'admin':
            return jsonify({'error': '一般使用者不需要密碼'}), 400
        if len(str(pw)) < 4:
            return jsonify({'error': '密碼至少 4 碼'}), 400
        db.execute("UPDATE users SET password = ? WHERE id = ?",
                   (generate_password_hash(str(pw)), uid))

    try:
        db.execute("UPDATE users SET username=?, role=?, display_name=? WHERE id=?", (name, role, display, uid))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({'error': '此姓名已被使用'}), 409

    log_admin('更新人員', row['username'], f'{row["role"]}→{role}')
    return jsonify({'message': '已更新'})


@app.route('/api/people/<int:uid>', methods=['DELETE'])
@require_admin
def delete_person(uid):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        return jsonify({'error': '找不到該人員'}), 404
    if row['username'] == BOOTSTRAP_ADMIN:
        return jsonify({'error': f'預設管理員 {BOOTSTRAP_ADMIN} 不可停用或刪除'}), 400
    if row['username'] == g.me['username']:
        return jsonify({'error': '不可停用自己'}), 400
    if row['role'] == 'admin':
        admin_cnt = db.execute("SELECT COUNT(*) c FROM users WHERE role='admin' AND is_deleted=0").fetchone()['c']
        if admin_cnt <= 1:
            return jsonify({'error': '系統至少需保留一位管理員'}), 400

    # 軟刪除：保留其既有單據
    db.execute("UPDATE users SET is_deleted = 1 WHERE id = ?", (uid,))
    db.execute("DELETE FROM tokens WHERE user_id = ?", (uid,))
    db.commit()
    log_admin('停用人員', row['username'])
    return jsonify({'message': f'已停用 {row["display_name"]}，其歷史單據仍保留'})


# ── 管理日誌（僅管理員）───────────────────────────────────
@app.route('/api/admin-log', methods=['GET'])
@require_admin
def admin_log():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM admin_log ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


# ── 匯出 CSV（僅管理員）───────────────────────────────────
@app.route('/api/export/<kind>', methods=['GET'])
@require_admin
def export_csv(kind):
    month = request.args.get('month', '')
    db = get_db()

    def make_csv(header, rows_iter, filename):
        buf = io.StringIO()
        buf.write('\ufeff')
        w = csv.writer(buf)
        w.writerow(header)
        for r in rows_iter:
            w.writerow(r)
        return Response(
            buf.getvalue(),
            mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )

    args, where = [], ""
    if month:
        if not re_month_ok(month):
            return jsonify({'error': '月份格式錯誤'}), 400
        where = "WHERE date LIKE ?"
        args.append(month + '%')

    suffix = month or 'all'

    if kind == 'petty':
        rows = db.execute(
            f'''SELECT pc.id, pc.date, u.display_name, pc.activity, pc.amount, pc.status, pc.remark
                FROM petty_cash pc JOIN users u ON pc.applicant_id = u.id
                {where} ORDER BY pc.date DESC''',
            args
        ).fetchall()
        item_rows = db.execute(
            '''SELECT pci.petty_cash_id, c.name FROM petty_cash_items pci
               JOIN categories c ON c.id = pci.category_id'''
        ).fetchall()
        items_map = {}
        for ir in item_rows:
            items_map.setdefault(ir['petty_cash_id'], []).append(ir['name'])

        return make_csv(
            ['申請日期', '請領人', '活動內容簡述', '總金額', '狀態', '備註', '申請項目'],
            ([r['date'], r['display_name'], r['activity'], r['amount'], r['status'],
              r['remark'] or '', ';'.join(items_map.get(r['id'], []))] for r in rows),
            f'零用金申請_{suffix}.csv')

    if kind == 'accounting':
        rows = db.execute(
            f'''SELECT pc.date, u.display_name, pc.activity, pc.amount, pc.invoice_date,
                       pc.confirmed_amount, pc.payment_date, pc.payment_method, pc.status
                FROM petty_cash pc JOIN users u ON pc.applicant_id = u.id
                {where} ORDER BY pc.date DESC''',
            args
        ).fetchall()
        return make_csv(
            ['申請日期', '請領人', '活動內容', '申請金額', '發票取得日期', '確認金額', '支付日期', '支付方式', '狀態'],
            ([r['date'], r['display_name'], r['activity'], r['amount'], r['invoice_date'] or '',
              r['confirmed_amount'] if r['confirmed_amount'] is not None else r['amount'],
              r['payment_date'] or '', r['payment_method'] or '', r['status']] for r in rows),
            f'會計審核_{suffix}.csv')

    if kind == 'req':
        rows = db.execute(
            f'''SELECT r.date, u.display_name, r.item_name, r.qty, r.total_price,
                       r.approved_by_wang, r.is_ordered, r.completion_date, r.other
                FROM requisitions r JOIN users u ON r.requester_id = u.id
                {where} ORDER BY r.date DESC''',
            args
        ).fetchall()
        return make_csv(
            ['日期', '申購人', '申購項目', '數量', '總金額', '通過訂購', '已訂購', '完成日期', '其他'],
            ([r['date'], r['display_name'], r['item_name'], r['qty'], r['total_price'],
              '是' if r['approved_by_wang'] else '否', '是' if r['is_ordered'] else '否',
              r['completion_date'] or '', r['other'] or ''] for r in rows),
            f'申購單_{suffix}.csv')

    return jsonify({'error': '不支援的匯出類型'}), 400


# ── 備份 / 還原（僅管理員）───────────────────────────────
@app.route('/api/backup', methods=['GET'])
@require_admin
def export_backup():
    db = get_db()
    def dump(q):
        return [dict(r) for r in db.execute(q).fetchall()]
    return jsonify({
        'petty_cash': dump("SELECT * FROM petty_cash"),
        'petty_cash_items': dump("SELECT * FROM petty_cash_items"),
        'requisitions': dump("SELECT * FROM requisitions"),
        'categories': dump("SELECT * FROM categories"),
        'users': dump("SELECT id, username, role, display_name, is_deleted FROM users"),
        'exported_at': datetime.now().isoformat(),
    })


@app.route('/api/backup', methods=['POST'])
@require_admin
def import_backup():
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not data.get('petty_cash'):
        return jsonify({'error': 'JSON 格式不正確或缺少必要欄位'}), 400

    try:
        db.execute("PRAGMA foreign_keys = OFF")
        for t in ('petty_cash_items', 'petty_cash', 'requisitions', 'categories'):
            db.execute(f"DELETE FROM {t}")

        for c in data.get('categories', []):
            db.execute("INSERT INTO categories (id, name, sort_order) VALUES (?,?,?)",
                       (c['id'], c['name'], c.get('sort_order', 0)))

        for p in data.get('petty_cash', []):
            db.execute(
                '''INSERT INTO petty_cash (id, date, applicant_id, activity, amount, status, remark,
                   invoice_date, confirmed_amount, payment_date, payment_method)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (p['id'], p['date'], p['applicant_id'], p['activity'], p['amount'], p['status'],
                 p.get('remark', ''), p.get('invoice_date', ''), p.get('confirmed_amount'),
                 p.get('payment_date', ''), p.get('payment_method', '')))

        for pi in data.get('petty_cash_items', []):
            db.execute("INSERT INTO petty_cash_items (petty_cash_id, category_id) VALUES (?,?)",
                       (pi['petty_cash_id'], pi['category_id']))

        for r in data.get('requisitions', []):
            db.execute(
                '''INSERT INTO requisitions (id, date, requester_id, item_name, qty, total_price,
                   approved_by_wang, is_ordered, completion_date, other)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (r['id'], r['date'], r['requester_id'], r['item_name'], r['qty'], r['total_price'],
                 r.get('approved_by_wang', 0), r.get('is_ordered', 0),
                 r.get('completion_date', ''), r.get('other', '')))

        db.execute("PRAGMA foreign_keys = ON")
        db.commit()
        log_admin('還原備份')
        return jsonify({'message': '資料已還原'})
    except Exception as e:
        db.rollback()
        return jsonify({'error': f'還原失敗: {e}'}), 400


@app.route('/api/me/password', methods=['POST'])
@require_admin
def change_own_password():
    """管理員修改自己的密碼"""
    d = request.get_json(silent=True) or {}
    old = d.get('old_password') or ''
    new = d.get('new_password') or ''
    if not new or len(str(new)) < 4:
        return jsonify({'error': '新密碼至少 4 碼'}), 400

    db = get_db()
    row = db.execute("SELECT password FROM users WHERE id = ?", (g.me['id'],)).fetchone()
    if not row or not row['password'] or not check_password_hash(row['password'], old):
        return jsonify({'error': '目前密碼不正確'}), 401

    db.execute("UPDATE users SET password = ? WHERE id = ?",
               (generate_password_hash(str(new)), g.me['id']))
    # 改密碼後使其他 token 失效
    db.execute("DELETE FROM tokens WHERE user_id = ?", (g.me['id'],))
    db.commit()
    log_admin('修改管理員密碼', g.me['username'])
    return jsonify({'message': '密碼已更新，請重新登入'})


# ── 年度封存與自動備份 ────────────────────────────────────
@app.route('/api/archive/years', methods=['GET'])
@require_admin
def archive_years():
    import archive as AR
    return jsonify(AR.list_archives(DB_PATH))


@app.route('/api/archive/<year>', methods=['POST'])
@require_admin
def do_archive(year):
    import archive as AR
    try:
        res = AR.archive_year(DB_PATH, year, note='manual')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    log_admin('年度封存', str(year), f'零用金{res["petty"]} 申購單{res["requisitions"]}')
    return jsonify(res)


@app.route('/api/backup/run', methods=['POST'])
@require_admin
def run_backup():
    import archive as AR
    res = AR.monthly_backup(DB_PATH, note='manual')
    if res.get('error'):
        return jsonify({'error': res['error']}), 500
    log_admin('手動備份', os.path.basename(res.get('db', '')))
    return jsonify(res)


@app.route('/api/backup/auto-now', methods=['POST'])
@require_admin
def run_auto_now():
    import archive as AR
    res = AR.auto_close_if_month_end(DB_PATH, force=True)
    log_admin('執行月底作業')
    return jsonify(res)


@app.route('/api/backup/log', methods=['GET'])
@require_admin
def backup_log():
    import archive as AR
    return jsonify(AR.list_backups(DB_PATH))


@app.route('/api/archive/files', methods=['GET'])
@require_admin
def archive_files():
    import archive as AR
    d = AR.ARCHIVE_DIR
    if not os.path.isdir(d):
        return jsonify([])
    out = []
    for f in os.listdir(d):
        fp = os.path.join(d, f)
        try:
            out.append({'name': f, 'size': os.path.getsize(fp), 'mtime': os.path.getmtime(fp)})
        except OSError:
            pass
    return jsonify(sorted(out, key=lambda x: x['mtime'], reverse=True))


# ── 靜態檔案 ──────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


# ── 啟動 ──────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    print("=" * 52)
    print("  ReqKit - 零用金與申購單管理系統")
    print(f"  Server : http://127.0.0.1:{PORT}  (0.0.0.0:{PORT})")
    print(f"  DB     : {DB_PATH}")
    print(f"  管理員 : {BOOTSTRAP_ADMIN}  (需密碼)")
    print("  其他姓名自動以一般使用者身分加入（免密碼）")
    print("=" * 52)
    app.run(host='0.0.0.0', port=PORT, debug=False)