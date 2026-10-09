"""SQLite snapshots in the owner's Google Drive via Apps Script.

Revision-checked snapshots with separate immutable image bundles. Warm writes
use one request; unchanged reads return only a revision. Drive remains authoritative.
"""
import base64
import hashlib
import json
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
    def __init__(self, endpoint, token, cache_seconds=20):
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
        self.images = {}
        self.image_digest = None
        self.http = requests.Session()

    def _rpc(self, operation, **kwargs):
        try:
            # requests follows Google's POST -> GET ContentService redirect.
            # The secret lives in the POST body, never in the URL.
            response = self.http.post(self.endpoint, json={'token': self.token, 'op': operation, 'protocol': 2, **kwargs},
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
                'upgrade_required': 'יש לעדכן ולפרוס את Code.gs ב־Google Apps Script לפי הוראות עדכון המהירות.',
            }
            raise DriveError(messages.get(code, 'Google Drive לא השלים את הפעולה. רעננו את הרשימה ובדקו את פריסת הסקריפט.'))
        return result

    def _snapshot(self, fresh=False):
        if fresh or self.cached is None or time.monotonic() - self.cache_at >= self.cache_seconds:
            result = self._rpc('read', revision=self.cached['revision'] if self.cached else -1,
                               image_digest=self.image_digest)
            if result.get('protocol') != 2:
                self.cached = None
                raise DriveError('יש לעדכן ולפרוס את Code.gs ב־Google Apps Script לפי הוראות עדכון המהירות.')
            if not result.get('not_modified'):
                if result.get('format') == 2:
                    digest = result.get('image_digest')
                    if 'images' in result:
                        images = result['images']
                        if not isinstance(images, dict) or self._digest(images) != digest:
                            raise DriveError('קובץ התמונות ב־Drive אינו תקין. לא בוצע שינוי.')
                        self.images, self.image_digest = images, digest
                    elif digest != self.image_digest:
                        raise DriveError('חסרות תמונות בסנכרון. רעננו את העמוד.')
                self.cached = result
            self.cache_at = time.monotonic()
        return self.cached

    @staticmethod
    def _digest(images):
        payload = json.dumps(images, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _open(self, snapshot):
        con = sqlite3.connect(':memory:')
        con.row_factory = sqlite3.Row
        try:
            encoded = snapshot.get('database', '')
            if encoded:
                raw = base64.b64decode(encoded, validate=True)
                if len(raw) > MAX_BYTES or not raw.startswith(b'SQLite format 3\x00'):
                    raise ValueError('invalid database')
                con.deserialize(raw)
                if snapshot.get('format') == 2:
                    for row in con.execute("SELECT id,image FROM products WHERE image!=''").fetchall():
                        if not row['image'].startswith('@image:') or row['image'][7:] not in self.images:
                            raise ValueError('missing image')
                        con.execute('UPDATE products SET image=? WHERE id=?',
                                    (self.images[row['image'][7:]], row['id']))
                    con.commit()
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('PRAGMA trusted_schema=OFF')
            return con
        except Exception:
            con.close()
            raise DriveError('קובץ הנתונים ב־Drive אינו תקין. לא בוצע איפוס או שינוי בקובץ.') from None

    def _pack(self, con):
        """Copy before stripping photos; callers continue using ordinary image data."""
        packed = sqlite3.connect(':memory:')
        try:
            con.backup(packed)
            images = {}
            for pid, image in packed.execute("SELECT id,image FROM products WHERE image!=''").fetchall():
                key = hashlib.sha256(image.encode()).hexdigest()
                images[key] = image
                packed.execute('UPDATE products SET image=? WHERE id=?', ('@image:' + key, pid))
            packed.commit()
            packed.execute('VACUUM')  # Remove freed photo pages, not just photo values.
            raw = packed.serialize()
            if len(raw) + sum(len(v) for v in images.values()) > MAX_BYTES:
                raise DriveError('הנתונים גדולים מדי. הסירו תמונות כבדות ונסו שוב.')
            return base64.b64encode(raw).decode('ascii'), images, self._digest(images)
        finally:
            packed.close()

    def _save(self, con, snapshot):
        data, images, digest = self._pack(con)
        payload = dict(revision=snapshot['revision'], database=data, format=2,
                       image_digest=digest, request_id=uuid.uuid4().hex)
        if snapshot.get('format') != 2 or digest != snapshot.get('image_digest'):
            payload['images'] = images
        result = self._rpc('write', **payload)
        self.images, self.image_digest = images, digest
        self.cached = dict(ok=True, protocol=2, format=2, revision=result['revision'],
                           database=data, image_digest=digest)
        self.cache_at = time.monotonic()

    @contextmanager
    def conn(self):
        # Serialize writes within this server. Separate servers are protected
        # by the Apps Script revision check, not by this in-process lock.
        with self.lock:
            # Reuse even an expired snapshot for writing: CAS rejects stale writes.
            # Never blindly retry quantity increments after network failures.
            snapshot = self.cached if self.cached is not None else self._snapshot()
            con = self._open(snapshot)
            schema_before = con.execute('PRAGMA schema_version').fetchone()[0]
            changes_before = con.total_changes
            try:
                yield con
                con.commit()
                changed = con.total_changes != changes_before or con.execute('PRAGMA schema_version').fetchone()[0] != schema_before
                if changed:
                    self._save(con, snapshot)
            except Exception:
                con.rollback()
                self.cached = None
                raise
            finally:
                con.close()

    def set_bought(self, lid, iid, bought):
        # Explicit set (not toggle) is safe to reapply after a confirmed conflict.
        with self.lock:
            for attempt in range(2):
                try:
                    return super().set_bought(lid, iid, bought)
                except DriveConflict:
                    if attempt:
                        raise
                    self.cached = None

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
            with self.lock:
                snapshot = self._snapshot(fresh=True)
                if snapshot.get('database'):
                    raise DriveError('כבר קיימים נתונים ב־Drive. הייבוא בוטל כדי לא לדרוס אותם.')
                self._save(con, snapshot)
        finally:
            con.close()
