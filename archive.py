"""
ReqKit - 年度封存與自動備份模組

功能：
  1. monthly_backup()   每月自動備份（SQLite 安全快照 + JSON）
  2. archive_year()     把某年度資料封存成「年度明細檔」（主表保留）
  3. auto_close_if_month_end()  每月最後一天自動執行上面兩項
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta

ARCHIVE_DIR = os.environ.get(
    'REQKIT_ARCHIVE_DIR',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'archive')
)
KEEP_BACKUPS = 24  # 自動備份保留份數


def _month_end(d):
    """回傳該日期所在月份的���後一天"""
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def year_ok(year):
    try:
        if len(str(year)) != 4:
            return False
        datetime.strptime(str(year), '%Y')
        return True
    except Exception:
        return False


def ensure_dir():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    return ARCHIVE_DIR


def prune(prefix, keep=KEEP_BACKUPS):
    """只保留最近 keep 個以 prefix 開頭的檔案"""
    try:
        files = sorted(f for f in os.listdir(ARCHIVE_DIR) if f.startswith(prefix))
        for f in files[:-keep]:
            os.remove(os.path.join(ARCHIVE_DIR, f))
    except Exception:
        pass


def monthly_backup(db_path, note='auto'):
    """每月自動備份：SQLite 線上安全快照 + JSON 匯出"""
    ensure_dir()
    ts = datetime.now()
    stamp = ts.strftime('%Y%m%d_%H%M')

    # 1. SQLite 安全快照（用 backup API，避免直接複製造成檔案不一致）
    db_copy = os.path.join(ARCHIVE_DIR, f'reqkit_{stamp}.db')
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(db_copy)
        src.backup(dst)
        dst.close()
        src.close()
    except Exception as e:
        return {'error': f'備份失敗: {e}'}

    # 2. JSON 匯出
    json_copy = os.path.join(ARCHIVE_DIR, f'ReqKit_備份_{stamp}.json')
    counts = {'petty': 0, 'req': 0}
    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        data = {
            'exported_at': ts.isoformat(),
            'note': note,
            'petty_cash': [dict(r) for r in db.execute("SELECT * FROM petty_cash")],
            'petty_cash_items': [dict(r) for r in db.execute("SELECT * FROM petty_cash_items")],
            'requisitions': [dict(r) for r in db.execute("SELECT * FROM requisitions")],
            'categories': [dict(r) for r in db.execute("SELECT * FROM categories")],
            'users': [dict(r) for r in db.execute(
                "SELECT username, role, display_name, is_deleted FROM users")],
        }
        counts['petty'] = len(data['petty_cash'])
        counts['req'] = len(data['requisitions'])
        with open(json_copy, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        db.close()
    except Exception as e:
        return {'error': f'JSON 匯出失敗: {e}', 'db': db_copy}

    prune('reqkit_')
    prune('ReqKit_備份')

    return {'db': db_copy, 'json': json_copy, **counts, 'note': note}


def archive_year(db_path, year, note='manual'):
    """
    把指定年度資料封存為「年度明細檔」。
    主表資料保留（不刪除），因此仍可正常編輯；重複封存會覆寫該年度檔。
    """
    if not year_ok(year):
        raise ValueError('年份格式錯誤（需 4 位數字）')

    year = str(year)
    ensure_dir()
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    petty = db.execute(
        """SELECT pc.*, u.display_name, u.username FROM petty_cash pc
           JOIN users u ON pc.applicant_id = u.id WHERE pc.date LIKE ?""",
        (year + '-%',)
    ).fetchall()
    reqs = db.execute(
        """SELECT r.*, u.display_name, u.username FROM requisitions r
           JOIN users u ON r.requester_id = u.id WHERE r.date LIKE ?""",
        (year + '-%',)
    ).fetchall()

    # 1) 寫出 JSON 年度明細檔（可離線保存/下載）
    json_path = os.path.join(ARCHIVE_DIR, f'ReqKit_年度明細_{year}.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            'year': year,
            'generated_at': datetime.now().isoformat(),
            'note': note,
            'petty': [dict(r) for r in petty],
            'requisitions': [dict(r) for r in reqs],
        }, f, ensure_ascii=False, indent=2)

    # 2) 寫入年度表（供查詢介面快速檢索）
    for t in ('archive_petty', 'archive_requisitions'):
        db.execute(f'DELETE FROM {t} WHERE year = ?', (year,))

    for r in petty:
        items = [i['name'] for i in db.execute(
            """SELECT c.name FROM petty_cash_items pci
               JOIN categories c ON c.id = pci.category_id
               WHERE pci.petty_cash_id = ?""", (r['id'],)
        )]
        db.execute(
            """INSERT INTO archive_petty
               (id, year, date, applicant_id, applicant_name, activity, amount, status,
                remark, invoice_date, confirmed_amount, payment_date, payment_method,
                items, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r['id'], year, r['date'], r['applicant_id'],
             r['display_name'] or r['username'], r['activity'], r['amount'],
             r['status'], r['remark'] or '', r['invoice_date'] or '',
             r['confirmed_amount'], r['payment_date'] or '',
             r['payment_method'] or '', ';'.join(items), r['created_at'])
        )

    for r in reqs:
        db.execute(
            """INSERT INTO archive_requisitions
               (id, year, date, requester_id, requester_name, item_name, qty, total_price,
                approved_by_wang, is_ordered, completion_date, other, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r['id'], year, r['date'], r['requester_id'],
             r['display_name'] or r['username'], r['item_name'], r['qty'],
             r['total_price'], r['approved_by_wang'], r['is_ordered'],
             r['completion_date'] or '', r['other'] or '', r['created_at'])
        )

    db.execute(
        """INSERT INTO backup_log (year, month, kind, file_path, petty_count, req_count, note)
           VALUES (?,?,?,?,?,?,?)""",
        (year, datetime.now().strftime('%Y-%m'), 'year-archive', json_path,
         len(petty), len(reqs), note)
    )
    db.commit()
    db.close()

    return {'year': year, 'petty': len(petty), 'requisitions': len(reqs), 'file': json_path}


def auto_close_if_month_end(db_path, force=False):
    """每日檢查：今天若為當月最後一天，則自動備份 + 封存本年度"""
    today = datetime.now().date()
    is_month_end = (today == _month_end(today))
    if not (force or is_month_end):
        return {'skipped': True, 'today': str(today), 'is_month_end': False}

    b = monthly_backup(db_path, note='auto-month-end')
    if b.get('error'):
        return {'error': b['error']}
    a = archive_year(db_path, datetime.now().strftime('%Y'), note='auto-month-end')
    return {'ran': True, 'is_month_end': is_month_end, 'backup': b, 'archive': a}


def list_archives(db_path):
    """列出已封存的年度檔（含筆數）"""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT a.year,
                  (SELECT COUNT(*) FROM archive_petty p WHERE p.year = a.year) AS petty_count,
                  (SELECT COUNT(*) FROM archive_requisitions r WHERE r.year = a.year) AS req_count,
                  (SELECT MAX(archived_at) FROM archive_petty p WHERE p.year = a.year) AS petty_at,
                  (SELECT MAX(archived_at) FROM archive_requisitions r WHERE r.year = a.year) AS req_at
           FROM (SELECT DISTINCT year FROM archive_petty
                 UNION SELECT DISTINCT year FROM archive_requisitions) a
           ORDER BY a.year DESC"""
    ).fetchall()
    db.close()

    out = []
    for r in rows:
        files = []
        if os.path.isdir(ARCHIVE_DIR):
            files = [f for f in os.listdir(ARCHIVE_DIR)
                     if f.startswith(f'ReqKit_年度明細_{r["year"]}')]
        out.append({
            'year': r['year'],
            'petty_count': r['petty_count'],
            'req_count': r['req_count'],
            'archived_at': (r['petty_at'] or r['req_at'] or ''),
            'file': (f'ReqKit_年度明細_{r["year"]}.json' in files),
        })
    return out


def list_backups(db_path, limit=30):
    """最近的備份/封存紀錄"""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT * FROM backup_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]