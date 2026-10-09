"""Fast shopping view with explicit pending state; all Drive I/O stays off the UI thread.

The queue is process memory, not durable storage. Only an acknowledged Drive
write is labelled saved. No Streamlit APIs are called from worker threads.
"""
import logging
import threading
import time


class ShoppingSync:
    def __init__(self, db, refresh_seconds=20):
        self.db = db
        self.refresh_seconds = refresh_seconds
        self.lock = threading.Lock()
        self.states = {}
        self.queue = {}
        self.refreshes = set()
        self.sequence = 0
        self.running = False
        self.idle = threading.Event()
        self.idle.set()

    def _state(self, lid):
        return self.states.setdefault(lid, dict(rows=None, pending={}, errors={},
                                                sync_error='', checked_at=0., refreshing=False,
                                                saved=False, verify=False))

    def _start(self):
        if not self.running:
            self.running = True
            self.idle.clear()
            threading.Thread(target=self._work, name='shopping-drive-sync', daemon=True).start()

    def view(self, lid):
        """A memory-only read, even while Google is slow or offline."""
        with self.lock:
            state = self._state(lid)
            if not state['refreshing'] and time.monotonic() - state['checked_at'] >= self.refresh_seconds:
                state['refreshing'] = True
                self.refreshes.add(lid)
                self._start()
            rows = [dict(row) for row in state['rows'] or []]
            for row in rows:
                change = state['pending'].get(row['id'])
                if change:
                    row['bought'] = int(change[1])
            return dict(rows=rows, loading=state['rows'] is None, pending=len(state['pending']),
                        error=next(iter(state['errors'].values()), ''),
                        sync_error=state['sync_error'], saved=state['saved'])

    def set_bought(self, lid, iid, bought):
        with self.lock:
            state = self._state(lid)
            if not any(row['id'] == iid for row in state['rows'] or []):
                raise ValueError('המוצר כבר אינו ברשימה. רעננו את הרשימה.')
            self.sequence += 1
            change = (self.sequence, bool(bought))
            state['pending'][iid] = change
            state['errors'].pop(iid, None)
            self.queue[(lid, iid)] = change
            self._start()

    def refresh(self, lid, *, verify=True):
        with self.lock:
            state = self._state(lid)
            state['refreshing'] = True
            state['verify'] = state['verify'] or verify
            self.refreshes.add(lid)
            self._start()

    def _work(self):
        while True:
            with self.lock:
                if self.queue:
                    batch, self.queue = self.queue, {}
                    lid = None
                elif self.refreshes:
                    lid = self.refreshes.pop()
                    verify = self._state(lid)['verify']
                    self._state(lid)['verify'] = False
                    batch = None
                else:
                    self.running = False
                    self.idle.set()
                    return
            # Never hold the view lock while accessing Drive or the database lock.
            if batch is not None:
                error = ''
                try:
                    self.db.set_bought_batch([(key[0], key[1], change[1]) for key, change in batch.items()])
                except Exception as exc:
                    logging.exception('Background shopping save failed')
                    error = str(exc) if isinstance(exc, ValueError) else 'לא התקבל אישור שמירה. רעננו ובדקו מה נשמר.'
                with self.lock:
                    for (list_id, iid), change in batch.items():
                        state = self._state(list_id)
                        if not error:
                            for row in state['rows'] or []:
                                if row['id'] == iid:
                                    row['bought'] = int(change[1])
                            state['saved'] = True
                        if state['pending'].get(iid) == change:
                            del state['pending'][iid]
                            if error:
                                state['errors'][iid] = error
                continue
            try:
                rows = self.db.refresh_items(lid) if verify else self.db.items(lid)
                error = ''
            except Exception as exc:
                logging.exception('Background shopping refresh failed')
                rows = None
                error = str(exc) if isinstance(exc, ValueError) else 'לא ניתן לבדוק עדכונים כרגע.'
            with self.lock:
                state = self._state(lid)
                if rows is not None:
                    state['rows'] = [dict(row) for row in rows]
                    # Never silently dismiss a failed user's action. A requested
                    # verification resolves it by showing authoritative values.
                    if verify:
                        state['errors'].clear()
                state['sync_error'] = error
                state['checked_at'] = time.monotonic()
                state['refreshing'] = lid in self.refreshes
