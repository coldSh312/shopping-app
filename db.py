"""Transactional storage; identical operations for SQLite and PostgreSQL."""
from contextlib import contextmanager
from pathlib import Path
import math
import sqlite3
import uuid

UNITS = ['יח׳', 'ק״ג', 'גרם', 'ליטר', 'מ״ל', 'חבילה', 'בקבוק', 'קופסה']
DEFAULTS = [('🥬 ירקות', [('מלפפונים', 'ק״ג'), ('עגבניות', 'ק״ג')]),
            ('🍎 פירות', [('תפוחים', 'ק״ג'), ('בננות', 'ק״ג')]),
            ('🥛 מוצרי חלב', [('חלב', 'ליטר'), ('קוטג׳', 'יח׳'), ('גבינה לבנה', 'יח׳')]),
            ('🍞 לחם ומאפים', [('לחם', 'יח׳')]),
            ('🥚 ביצים', [('ביצים', 'חבילה')]),
            ('🥫 מזווה', [('אורז', 'ק״ג'), ('פסטה', 'חבילה')]),
            ('🧽 ניקיון ובית', []), ('🛒 אחר', [])]


def uid():
    return uuid.uuid4().hex


def clean(value, limit=100):
    value = str(value).strip()
    if not value or len(value) > limit:
        raise ValueError(f'יש להזין טקסט באורך 1–{limit} תווים.')
    return value


def quantity(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 100000:
        raise ValueError('הכמות חייבת להיות גדולה מאפס ועד 100,000.')
    return value


class DB:
    def __init__(self, url='', path='data/shopping.db'):
        self.url, self.path = url, path
        if not url:
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def conn(self):
        if self.url:
            import psycopg
            from psycopg.rows import dict_row
            con = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10)
        else:
            con = sqlite3.connect(self.path, timeout=15)
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA foreign_keys=ON')
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def run(self, con, sql, args=()):
        return con.execute(sql.replace('?', '%s') if self.url else sql, args)

    def query(self, sql, args=()):
        with self.conn() as con:
            return [dict(x) for x in self.run(con, sql, args).fetchall()]

    def write(self, sql, args=()):
        with self.conn() as con:
            return self.run(con, sql, args).rowcount

    def init(self):
        with self.conn() as con:
            # Serialize first-time initialization across PostgreSQL workers.
            if self.url:
                self.run(con, 'SELECT pg_advisory_xact_lock(8345021)')
            else:
                con.execute('BEGIN IMMEDIATE')
            statements = [
                'CREATE TABLE IF NOT EXISTS categories (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, position INTEGER NOT NULL)',
                '''CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                   category_id TEXT NOT NULL REFERENCES categories(id), unit TEXT NOT NULL, image TEXT NOT NULL DEFAULT '')''',
                '''CREATE TABLE IF NOT EXISTS lists (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
                '''CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
                   product_id TEXT NOT NULL REFERENCES products(id), qty REAL NOT NULL CHECK(qty>0),
                   unit TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', bought INTEGER NOT NULL DEFAULT 0 CHECK(bought IN (0,1)),
                   UNIQUE(list_id, product_id))''',
                'CREATE INDEX IF NOT EXISTS idx_items_list ON items(list_id)']
            for sql in statements:
                self.run(con, sql)
            if not self.run(con, 'SELECT id FROM categories LIMIT 1').fetchone():
                for pos, (name, products) in enumerate(DEFAULTS):
                    cid = uid()
                    self.run(con, 'INSERT INTO categories VALUES (?,?,?)', (cid, name, pos))
                    for title, unit in products:
                        self.run(con, 'INSERT INTO products VALUES (?,?,?,?,?)', (uid(), title, cid, unit, ''))

    def categories(self):
        return self.query('SELECT * FROM categories ORDER BY position,name')

    def products(self):
        return self.query('''SELECT p.*, c.name AS category, c.position FROM products p
                          JOIN categories c ON p.category_id=c.id ORDER BY c.position,p.name''')

    def lists(self):
        return self.query('''SELECT l.*, COUNT(i.id) AS total, COALESCE(SUM(i.bought),0) AS done
            FROM lists l LEFT JOIN items i ON l.id=i.list_id GROUP BY l.id,l.name,l.created_at
            ORDER BY l.created_at DESC,l.id''')

    def create_list(self, name, source=None):
        lid = uid()
        with self.conn() as con:
            self.run(con, 'INSERT INTO lists(id,name) VALUES (?,?)', (lid, clean(name)))
            if source:
                rows = self.run(con, 'SELECT * FROM items WHERE list_id=?', (source,)).fetchall()
                for r in rows:
                    self.run(con, 'INSERT INTO items VALUES (?,?,?,?,?,?,0)',
                             (uid(), lid, r['product_id'], r['qty'], r['unit'], r['note']))
        return lid

    def items(self, lid):
        return self.query('''SELECT i.*, p.name, p.image, c.name AS category,c.id AS category_id,c.position
            FROM items i JOIN products p ON i.product_id=p.id JOIN categories c ON p.category_id=c.id
            WHERE list_id=? ORDER BY i.bought,c.position,p.name''', (lid,))

    def add_item(self, lid, pid, qty, unit, note=''):
        # Atomic increment avoids lost quantities when two shoppers add together.
        return self.write('''INSERT INTO items(id,list_id,product_id,qty,unit,note,bought) VALUES (?,?,?,?,?,?,0)
            ON CONFLICT(list_id,product_id) DO UPDATE SET qty=items.qty+excluded.qty,
            bought=0, note=CASE WHEN excluded.note='' THEN items.note ELSE excluded.note END
            WHERE items.unit=excluded.unit AND items.qty+excluded.qty<=100000''',
            (uid(), lid, pid, quantity(qty), clean(unit, 20), str(note).strip()[:300]))

    def set_bought(self, lid, iid, bought):
        self.write('UPDATE items SET bought=? WHERE id=? AND list_id=?', (int(bought), iid, lid))

    def product(self, name, cid, unit, image='', pid=None):
        name = clean(name)
        if unit not in UNITS:
            raise ValueError('יחידת מידה לא מוכרת.')
        pid = pid or uid()
        self.write('''INSERT INTO products VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,category_id=excluded.category_id,unit=excluded.unit,image=excluded.image''',
            (pid, name, cid, unit, image))
        return pid

    def reorder(self, cid, direction):
        with self.conn() as con:
            if self.url:
                self.run(con, 'SELECT pg_advisory_xact_lock(8345022)')
            else:
                con.execute('BEGIN IMMEDIATE')
            rows = self.run(con, 'SELECT * FROM categories ORDER BY position,name').fetchall()
            ids = [r['id'] for r in rows]
            i = ids.index(cid)
            j = i + direction
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
                for pos, ident in enumerate(ids):
                    self.run(con, 'UPDATE categories SET position=? WHERE id=?', (pos, ident))
