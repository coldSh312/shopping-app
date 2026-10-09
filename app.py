"""עגליסט — mobile-first Hebrew family shopping lists."""
import base64
import hmac
import io
import logging
import os
from pathlib import Path
from urllib.parse import quote
import streamlit as st
from PIL import Image, ImageOps, UnidentifiedImageError
from db import DB, UNITS, clean, quantity, uid, PRODUCT_ASSETS, default_product_image

st.set_page_config(page_title='עגליסט | קניות ביחד', page_icon='🛒', layout='centered')
st.markdown('''<style>
.stApp {direction:rtl;}
.block-container {max-width:740px;padding-top:2rem;padding-bottom:3rem;}
h1,h2,h3,p, label {text-align:right;}
[data-testid="stWidgetLabel"] {text-align:right;}
[data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea {direction:rtl;text-align:right;font-size:16px;}
[data-testid="stNumberInput"] input {direction:ltr;font-size:16px;}
[data-baseweb="select"] {direction:rtl;}
.stButton button,.stDownloadButton button,.stLinkButton a {min-height:48px;border-radius:14px;font-weight:600;}
[data-testid="stCheckbox"] label {min-height:48px;align-items:center;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius:18px;}
[data-testid="stRadio"] div[role="radiogroup"] {gap:8px;flex-wrap:wrap;}
[data-testid="stRadio"] label {min-height:44px;}
[data-testid="stCode"] {direction:ltr;text-align:left;}
[class*="st-key-product-row-"] [data-testid="stHorizontalBlock"] {flex-wrap:nowrap!important;}
[class*="st-key-product-row-"] [data-testid="stVerticalBlock"] {min-width:0;}
[class*="st-key-product-row-"] [data-testid="stImage"] img {border-radius:12px;}
.brand {font-size:13px;color:#16745A;font-weight:700;letter-spacing:1px;}
@media(max-width:600px){.block-container {padding:1.2rem 1rem 3rem;}h1{font-size:2rem!important;}}
</style>''', unsafe_allow_html=True)


def config(name, default=''):
    env = os.environ.get(name)
    if env is not None:
        return env
    try:
        return str(st.secrets.get(name, default))
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return default


DATABASE_URL = config('DATABASE_URL')
STORAGE_BACKEND = config('STORAGE_BACKEND', 'local').lower()
DRIVE_URL = config('DRIVE_API_URL')
DRIVE_TOKEN = config('DRIVE_API_TOKEN')
DRIVE_MODE = STORAGE_BACKEND == 'google_drive'
if STORAGE_BACKEND not in ('local', 'google_drive'):
    st.error('STORAGE_BACKEND צריך להיות local או google_drive.')
    st.stop()
APP_PASSWORD = config('APP_PASSWORD')
APP_URL = config('APP_URL').rstrip('/')
DB_PATH = config('DB_PATH', str(Path(__file__).parent / 'data/shopping.db'))

# Optional family password. Blank or absent APP_PASSWORD permits app access.
if APP_PASSWORD and not st.session_state.get('authenticated'):
    st.title('🛒 עגליסט')
    st.write('רשימת הקניות של הבית, תמיד יחד.')
    with st.form('login'):
        password = st.text_input('סיסמת המשפחה', type='password')
        login = st.form_submit_button('כניסה', type='primary', use_container_width=True)
    if login:
        if hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
            st.session_state['authenticated'] = True
            st.rerun()
        else:
            st.error('הסיסמה אינה נכונה.')
    st.stop()


@st.cache_resource
def storage(url, path, backend, endpoint, token):
    if backend == 'google_drive':
        from drive_db import DriveDB
        database = DriveDB(endpoint, token)
    else:
        database = DB(url, path)
    # Initializes the one-time default product image migration on this version.
    database.init()
    return database


try:
    db = storage(DATABASE_URL, DB_PATH, STORAGE_BACKEND, DRIVE_URL, DRIVE_TOKEN)
except ValueError as exc:
    st.error(str(exc))
    st.stop()
except Exception:
    logging.exception('Database connection failed')
    st.error('לא ניתן להתחבר למסד הנתונים. בדקו את הגדרות החיבור ונסו שוב.')
    st.stop()


def action(function, *args, **kwargs):
    """Avoid leaking database credentials / SQL details through UI errors."""
    try:
        return True, function(*args, **kwargs)
    except ValueError as exc:
        st.error(str(exc))
    except Exception:
        logging.exception('Data operation failed')
        st.error('השינוי לא נשמר. ייתכן שהשם כבר קיים או שהחיבור נותק. רעננו ונסו שוב.')
    return False, None


def go(lid, *, edit=False):
    st.query_params['list'] = lid
    st.query_params['view'] = 'edit' if edit else 'shop'
    st.rerun()


def back_to_lists():
    st.query_params.clear()
    st.session_state['page'] = 'הרשימות שלי'
    st.rerun()


def photo(upload):
    if upload.size > 5 * 1024 * 1024:
        raise ValueError('אפשר להעלות תמונה עד 5MB.')
    try:
        with Image.open(upload) as im:
            if im.width * im.height > 25_000_000:
                raise ValueError('התמונה גדולה מדי. בחרו תמונה עד 25 מגה־פיקסל.')
            im = ImageOps.exif_transpose(im).convert('RGB')
            im.thumbnail((480, 480))
            target = io.BytesIO()
            im.save(target, format='JPEG', quality=82)
        return base64.b64encode(target.getvalue()).decode()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError('לא ניתן לפתוח את התמונה. נסו JPG או PNG.') from None


def product_form(existing=None, list_id=None):
    categories = db.categories()
    ids = [c['id'] for c in categories]
    names = {c['id']: c['name'] for c in categories}
    existing = existing or {}
    with st.form('product_' + existing.get('id', list_id or 'new'), clear_on_submit=not existing):
        name = st.text_input('שם המוצר', value=existing.get('name', ''), max_chars=100)
        cid = st.selectbox('קטגוריה', ids, format_func=names.get,
                           index=ids.index(existing['category_id']) if existing else 0)
        unit = st.selectbox('יחידת מידה', UNITS, index=UNITS.index(existing.get('unit', UNITS[0])))
        preset = st.selectbox('תמונה מוכנה — לא חובה', [''] + list(PRODUCT_ASSETS),
                              format_func=lambda x: x or 'בלי שינוי / תמונה שאעלה בעצמי')
        upload = st.file_uploader('העלאת תמונה משלך', type=['jpg', 'jpeg', 'png', 'webp'])
        st.caption('התמונות המוכנות הן להמחשה. תמונה שתעלו בעצמכם תקבל עדיפות.')
        remove = st.checkbox('הסרת התמונה הקיימת') if existing.get('image') else False
        qty = st.number_input('כמות לרשימה', min_value=0.01, max_value=100000., value=1., step=1.) if list_id else None
        submit = st.form_submit_button('שמירה והוספה לרשימה' if list_id else 'שמירת מוצר', type='primary', use_container_width=True)
    if submit:
        img = '' if remove else existing.get('image', '')
        if preset and not remove:
            img = default_product_image(preset) or img
        elif not existing and not img and not remove:
            img = default_product_image(name.strip())
        if upload:
            ok, img = action(photo, upload)
            if not ok:
                return
        ok, pid = action(db.product, name, cid, unit, img, existing.get('id'))
        if ok and list_id:
            ok, _ = action(db.add_item, list_id, pid, qty, unit)
        if ok:
            st.toast('המוצר נשמר')
            st.rerun()


def catalog():
    st.subheader('המוצרים של הבית')
    st.caption('מגדירים פעם אחת, מוסיפים לכל רשימה. תמונה עוזרת לבחור את המוצר הנכון.')
    with st.expander('＋ מוצר חדש'):
        product_form()
    search = st.text_input('חיפוש מוצר', placeholder='שם או קטגוריה…')
    products = [p for p in db.products() if search.strip().casefold() in (p['name'] + p['category']).casefold()]
    if not products:
        st.info('אין מוצרים שמתאימים לחיפוש.')
    st.caption('התמונות המוכנות הן תמונות המחשה, ללא מותגים.')
    for p in products:
        with st.container(border=True):
            with st.container(horizontal=True, wrap=False, vertical_alignment='center'):
                if p['image']:
                    st.image(base64.b64decode(p['image']), width=64)
                with st.container(width='stretch'):
                    st.subheader(p['name'])
                    st.caption(p['category'] + ' · ' + p['unit'])
            with st.expander('עריכת מוצר ותמונה'):
                product_form(p)


def categories_page():
    st.subheader('הסדר שלכם בסופר')
    st.caption('המספר קובע איזו קטגוריה תופיע קודם ברשימת הקניות.')
    with st.form('new_category', clear_on_submit=True):
        name = st.text_input('קטגוריה חדשה', max_chars=100)
        if st.form_submit_button('הוספת קטגוריה', use_container_width=True):
            ok, title = action(clean, name)
            if ok:
                ok, _ = action(db.write, 'INSERT INTO categories VALUES (?,?,?)', (uid(), title, len(db.categories())))
                if ok:
                    st.rerun()
    categories = db.categories()
    for i, c in enumerate(categories):
        with st.expander(f"{i + 1}. {c['name']}"):
            with st.form('cat_' + c['id']):
                name = st.text_input('שם הקטגוריה', c['name'])
                if st.form_submit_button('שמירת שם', use_container_width=True):
                    ok, title = action(clean, name)
                    if ok:
                        ok, _ = action(db.write, 'UPDATE categories SET name=? WHERE id=?', (title, c['id']))
                        if ok:
                            st.rerun()
            for direction, label, disabled in [(-1, '↑ הזזה למעלה', i == 0), (1, '↓ הזזה למטה', i == len(categories)-1)]:
                if st.button(label, key=f'move_{c["id"]}_{direction}', disabled=disabled, use_container_width=True):
                    ok, _ = action(db.reorder, c['id'], direction)
                    if ok:
                        st.rerun()


def home():
    st.subheader('מה קונים הפעם?')
    with st.expander('＋ יצירת רשימה', expanded=not db.lists()):
        with st.form('new_list', clear_on_submit=True):
            name = st.text_input('שם הרשימה', placeholder='למשל: קניות לסוף השבוע', max_chars=100)
            all_lists = db.lists()
            lookup = {x['id']: x['name'] for x in all_lists}
            source = st.selectbox('התחלה מרשימה קודמת', [''] + list(lookup), format_func=lambda x: lookup.get(x, 'רשימה ריקה'))
            if st.form_submit_button('יצירת רשימה', type='primary', use_container_width=True):
                ok, lid = action(db.create_list, name, source or None)
                if ok:
                    go(lid, edit=True)
    for row in db.lists():
        with st.container(border=True):
            st.subheader(row['name'])
            st.caption(f"{int(row['done'])} מתוך {row['total']} מוצרים בעגלה")
            st.progress(float(row['done'] / row['total']) if row['total'] else 0.)
            if st.button('פתיחת הרשימה ←', key='open_' + row['id'], use_container_width=True):
                go(row['id'])


def change_bought(lid, iid, key, previous):
    ok, _ = action(db.set_bought, lid, iid, bool(st.session_state[key]))
    if not ok:
        st.session_state[key] = previous


def item_card(row, lid, *, editing=False):
    """One compact image/text row; edit controls exist only on the editor page."""
    with st.container(border=True, key='product-row-' + row['id']):
        with st.container(horizontal=True, wrap=False, vertical_alignment='center', gap='small'):
            if row['image']:
                st.image(base64.b64decode(row['image']), width=64)
            with st.container(width='stretch'):
                label = f"{row['name']} · {row['qty']:g} {row['unit']}"
                if editing:
                    st.write(label)
                else:
                    key = f"check_{row['id']}_{row['bought']}"
                    st.checkbox(label, value=bool(row['bought']), key=key, width='stretch',
                                on_change=change_bought,
                                args=(lid, row['id'], key, bool(row['bought'])))
        if not editing:
            return
        with st.expander('עריכת כמות, הערה והסרה'):
            with st.form('edit_' + row['id']):
                qty = st.number_input('כמות', min_value=0.01, max_value=100000., value=float(row['qty']), step=1.)
                unit = st.selectbox('יחידה', UNITS, index=UNITS.index(row['unit']))
                note = st.text_input('הערה', row['note'], max_chars=300)
                if st.form_submit_button('שמירת השינויים', use_container_width=True):
                    ok, _ = action(db.write, 'UPDATE items SET qty=?,unit=?,note=? WHERE id=? AND list_id=?',
                                   (quantity(qty), unit, note.strip(), row['id'], lid))
                    if ok:
                        st.rerun()
            if st.button('הסרת המוצר מהרשימה', key='del_' + row['id'], use_container_width=True):
                ok, _ = action(db.write, 'DELETE FROM items WHERE id=? AND list_id=?', (row['id'], lid))
                if ok:
                    st.session_state['undo'] = row
                    st.rerun()


@st.fragment(run_every='20s')
def shopping_items(lid):
    try:
        rows = db.items(lid)
    except ValueError as exc:
        st.error(str(exc))
        return
    pending = [r for r in rows if not r['bought']]
    done = [r for r in rows if r['bought']]
    st.progress(len(done) / len(rows) if rows else 0., text=f'{len(done)} בעגלה · {len(pending)} נשארו')
    if not rows:
        st.info('הרשימה ריקה. לחצו על עריכה כדי להוסיף מוצרים.')
    elif not pending:
        st.success('הכול בעגלה. קנייה נעימה! 🎉')
    previous = None
    for row in pending:
        if row['category'] != previous:
            st.subheader(row['category'])
            previous = row['category']
        item_card(row, lid)
    if done:
        with st.expander(f'כבר בעגלה ({len(done)})', expanded=False):
            for row in done:
                item_card(row, lid)


def list_page(lid):
    lists = db.query('SELECT * FROM lists WHERE id=?', (lid,))
    if not lists:
        st.warning('הרשימה לא נמצאה. ייתכן שנמחקה.')
        if st.button('לכל הרשימות', use_container_width=True):
            back_to_lists()
        return
    current = lists[0]
    editing = st.query_params.get('view') == 'edit'
    with st.container(horizontal=True, wrap=False, horizontal_alignment='distribute'):
        if st.button('→ הרשימות שלי'):
            back_to_lists()
        if editing:
            if st.button('סיימתי · לקניות', type='primary'):
                go(lid)
        elif st.button('עריכה', key='edit-list'):
            go(lid, edit=True)
    st.subheader(current['name'])
    if not editing:
        shopping_items(lid)
        return
    st.caption('הכינו את הרשימה כאן. כשתסיימו, עברו למסך הקניות.')
    undo = st.session_state.get('undo')
    if undo and undo['list_id'] == lid:
        if st.button(f"ביטול הסרה: {undo['name']}", use_container_width=True):
            # Preserve original state; do not overwrite another shopper's re-addition.
            ok, _ = action(db.write, '''INSERT INTO items VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(list_id,product_id) DO NOTHING''',
                tuple(undo[k] for k in ['id','list_id','product_id','qty','unit','note','bought']))
            if ok:
                del st.session_state['undo']
                st.rerun()
    with st.expander('＋ הוספת מוצרים', expanded=not db.items(lid)):
        products = db.products()
        lookup = {p['id']: p for p in products}
        with st.form('add_to_' + lid, clear_on_submit=True):
            pid = st.selectbox('חיפוש ובחירת מוצר', list(lookup), index=None,
                              placeholder='הקלידו שם מוצר…',
                              format_func=lambda x: lookup[x]['name'] + ' · ' + lookup[x]['unit'])
            qty = st.number_input('כמות', min_value=0.01, max_value=100000., value=1., step=1.)
            st.caption('יחידת המידה מופיעה ליד שם המוצר. אפשר לשנות אותה אחרי ההוספה.')
            note = st.text_input('הערה — לא חובה', placeholder='למשל: רק מהמותג שבתמונה', max_chars=300)
            add = st.form_submit_button('הוספה לרשימה', type='primary', use_container_width=True)
        if add:
            if not pid:
                st.warning('בחרו מוצר להוספה.')
            else:
                ok, changed = action(db.add_item, lid, pid, qty, lookup[pid]['unit'], note)
                if ok and not changed:
                    st.warning('לא ניתן לחבר את הכמות: בדקו את יחידת המידה והכמות של המוצר ברשימה.')
                elif ok:
                    st.toast('נוסף לרשימה')
                    st.rerun()
        with st.expander('לא מצאתם? הגדירו מוצר חדש'):
            product_form(list_id=lid)
    with st.expander('שיתוף ואפשרויות רשימה'):
        st.caption('כל מי שיש לו גישה לאפליקציה יכול לצפות ולערוך את כל הרשימות והקטלוג.')
        if APP_URL:
            link = f'{APP_URL}/?list={quote(lid)}'
            st.code(link, language=None)
            st.link_button('שיתוף קישור ב־WhatsApp', 'https://wa.me/?text=' + quote(current['name'] + '\n' + link), use_container_width=True)
        else:
            st.info('אפשר להעתיק את כתובת העמוד מהדפדפן. להפעלת כפתור WhatsApp הגדירו APP_URL לפי ההוראות.')
        rows = db.items(lid)
        content = current['name'] + '\n\n' + '\n'.join(
            f"{'✓' if r['bought'] else '□'} {r['name']} — {r['qty']:g} {r['unit']}" + (f" ({r['note']})" if r['note'] else '') for r in rows)
        st.download_button('הורדת הרשימה כטקסט', content.encode('utf-8-sig'), file_name='shopping-list.txt', mime='text/plain', use_container_width=True)
        with st.form('rename_list'):
            name = st.text_input('שם הרשימה', current['name'], max_chars=100)
            if st.form_submit_button('שמירת שם', use_container_width=True):
                ok, name = action(clean, name)
                if ok:
                    ok, _ = action(db.write, 'UPDATE lists SET name=? WHERE id=?', (name, lid))
                    if ok:
                        st.rerun()
        if st.button('שכפול לקנייה חדשה', use_container_width=True):
            ok, new_id = action(db.create_list, current['name'][:85] + ' — עותק', lid)
            if ok:
                go(new_id, edit=True)
        confirm = st.checkbox('אני מאשר/ת למחוק את הרשימה הזאת', key='confirm_' + lid)
        if st.button('מחיקת הרשימה', disabled=not confirm, use_container_width=True):
            ok, _ = action(db.write, 'DELETE FROM lists WHERE id=?', (lid,))
            if ok:
                back_to_lists()
    rows = db.items(lid)
    if rows:
        st.subheader('המוצרים ברשימה')
        for row in rows:
            item_card(row, lid, editing=True)


# An open list is a separate page: no brand header, catalogue tabs or storage text.
list_id = st.query_params.get('list')
try:
    if list_id:
        list_page(list_id)
    else:
        st.markdown('<div class="brand">קניות ביחד · פחות לשכוח</div>', unsafe_allow_html=True)
        st.title('🛒 עגליסט')
        page = st.radio('ניווט', ['הרשימות שלי', 'מוצרים', 'קטגוריות'], horizontal=True,
                        key='page', label_visibility='collapsed')
        if page == 'מוצרים':
            catalog()
        elif page == 'קטגוריות':
            categories_page()
        else:
            home()
        with st.expander('הגדרות ומידע'):
            if DRIVE_MODE:
                st.caption('☁️ הנתונים והתמונות נשמרים ב־Google Drive המשפחתי')
            elif not DATABASE_URL:
                st.caption('מצב מקומי · הנתונים נשמרים במחשב שמריץ את האפליקציה.')
            if APP_PASSWORD and st.button('יציאה'):
                st.session_state.clear()
                st.rerun()
except ValueError as exc:
    st.error(str(exc))
except Exception:
    logging.exception('Page failed')
    st.error('לא ניתן לטעון את הנתונים כרגע. רעננו את העמוד ונסו שוב.')
