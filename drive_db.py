"""SQLite snapshots in the owner's Google Drive via Apps Script.

The server performs revision-checked writes under a ScriptLock. Each transaction
starts from the current remote revision. Reads share a short in-process cache.
No local SQLite file is used as the source of truth in Drive mode.
"""
import base64
from contextlib import contextmanager
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid
import requests
from db import DB

MAX_BYTES = 6 * 1024 * 1024


class DriveError(ValueError):
    pass


class DriveConflict(DriveError):
    pass


class DriveDB(DB):
    def __init__(self, endpoint, token, cache_seconds=4):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or parsed.hostname != 'script.google.com'
                or not parsed.path.startswith('/macros/s/') or not parsed.path.endswith('/exec')
                or parsed.query or parsed.fragment):
            raise ValueError('יש להזין כתובת Google Apps Script שמסתיימת ב־/exec.')
        if len(token) < 32:
            raise ValueError('מפתח החיבור ל־Drive חסר או קצר מדי.')
        self.endpoint, self.token = endpoint, token
        self.url = ''  # Reuse the SQLite SQL dialect from DB.
        self.lock = threading.RLock()
        self.cache_seconds = cache_seconds
        self.cached = None
        self.cache_at = 0.

    def _rpc(self, operation, **kwargs):
        try:
            # requests follows Google's POST -> GET ContentService redirect.
            # The secret lives in the POST body, never in the URL.
            response = requests.post(self.endpoint, json={'token': self.token, 'op': operation, **kwargs},
                                     timeout=(10, 50))
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError):
            self.cached = None
            raise DriveError('החיבור ל־Google Drive לא הושלם. רעננו ובדקו את הרשימה לפני ניסיון נוסף; ייתכן שהשמירה כבר בוצעה.') from None
        if not isinstance(result, dict) or not result.get('ok'):
            self.cached = None
            code = result.get('error') if isinstance(result, dict) else None
            if code == 'conflict':
                raise DriveConflict('הרשימה השתנתה במכשיר אחר. רעננו ונסו לשמור שוב; השינוי שלכם לא נכתב.')
            messages = {
                'unauthorized': 'מפתח החיבור ל־Drive אינו תקין. בדקו את DRIVE_API_TOKEN ב־Secrets.',
                'not_initialized': 'יש להריץ initializeStorage בסקריפט Google לפני הפעלת האפליקציה.',
                'too_large': 'נתוני האפליקציה הגיעו למגבלת הגודל. יש לצמצם תמונות לפני שמירה.',
                'busy': 'מתבצעת שמירה אחרת כרגע. נסו שוב בעוד רגע.',
            }
            raise DriveError(messages.get(code, 'Google Drive לא השלים את הפעולה. רעננו את הרשימה ובדקו את פריסת הסקריפט.'))
        return result

    def _snapshot(self, fresh=False):
        if fresh or self.cached is None or time.monotonic() - self.cache_at >= self.cache_seconds:
            self.cached = self._rpc('read')
            self.cache_at = time.monotonic()
        return self.cached

    @staticmethod
    def _open(snapshot):
        con = sqlite3.connect(':memory:')
        con.row_factory = sqlite3.Row
        try:
            encoded = snapshot.get('database', '')
            if encoded:
                raw = base64.b64decode(encoded, validate=True)
                if len(raw) > MAX_BYTES or not raw.startswith(b'SQLite format 3\x00'):
                    raise ValueError('invalid database')
                con.deserialize(raw)
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('PRAGMA trusted_schema=OFF')
            return con
        except Exception:
            con.close()
            raise DriveError('קובץ הנתונים ב־Drive אינו תקין. לא בוצע איפוס או שינוי בקובץ.') from None

    @contextmanager
    def conn(self):
        # Serialize writes within this server. Separate servers are protected
        # by the Apps Script revision check, not by this in-process lock.
        with self.lock:
            snapshot = self._snapshot(fresh=True)
            con = self._open(snapshot)
            schema_before = con.execute('PRAGMA schema_version').fetchone()[0]
            try:
                yield con
                con.commit()
                changed = con.total_changes or con.execute('PRAGMA schema_version').fetchone()[0] != schema_before
                if changed:
                    raw = con.serialize()
                    if len(raw) > MAX_BYTES:
                        raise DriveError('קובץ הנתונים גדול מדי. הסירו תמונות כבדות ונסו שוב.')
                    data = base64.b64encode(raw).decode('ascii')
                    result = self._rpc('write', revision=snapshot['revision'], database=data,
                                       request_id=uuid.uuid4().hex)
                    self.cached = {'ok': True, 'revision': result['revision'], 'database': data}
                    self.cache_at = time.monotonic()
            except Exception:
                con.rollback()
                self.cached = None
                raise
            finally:
                con.close()

    def query(self, sql, args=()):
        with self.lock:
            con = self._open(self._snapshot())
            try:
                con.execute('PRAGMA query_only=ON')
                return [dict(row) for row in con.execute(sql, args).fetchall()]
            finally:
                con.close()

    def import_empty(self, raw):
        """One-time migration, refusing to replace any existing Drive database."""
        if len(raw) > MAX_BYTES:
            raise DriveError('הקובץ גדול ממגבלת 6MB.')
        data = base64.b64encode(raw).decode('ascii')
        con = self._open({'database': data})
        try:
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise DriveError('קובץ SQLite לא תקין.')
            for table in ['categories', 'products', 'lists', 'items']:
                con.execute(f'SELECT * FROM {table} LIMIT 1')
        finally:
            con.close()
        with self.lock:
            snapshot = self._snapshot(fresh=True)
            if snapshot.get('database'):
                raise DriveError('כבר קיימים נתונים ב־Drive. הייבוא בוטל כדי לא לדרוס אותם.')
            self._rpc('write', revision=snapshot['revision'], database=data, request_id=uuid.uuid4().hex)
            self.cached = None
