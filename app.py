import os
import json
from contextlib import contextmanager
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit

# ── DB config ─────────────────────────────────────────────────────────────────
# Render sets DATABASE_URL automatically when a Postgres DB is attached.
# Locally it's unset, so SQLite is used instead.
_DATABASE_URL = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1) or None
IS_PG = bool(_DATABASE_URL)
P = '%s' if IS_PG else '?'  # SQL placeholder character

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
socketio = SocketIO(app)

_SECRET_FILE = '.secret_key'


def _secret_key():
    if v := os.environ.get('SECRET_KEY'):
        return v.encode()
    if os.path.exists(_SECRET_FILE):
        with open(_SECRET_FILE, 'rb') as f:
            return f.read()
    k = os.urandom(24)
    with open(_SECRET_FILE, 'wb') as f:
        f.write(k)
    return k


app.secret_key = _secret_key()


# ── Database helpers ──────────────────────────────────────────────────────────

@contextmanager
def get_db():
    """Yield a DB connection that commits on success, rolls back on error."""
    if IS_PG:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        import sqlite3
        conn = sqlite3.connect('pub_crawl.db')
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q(conn, sql, params=()):
    """Run a query; returns a cursor with .fetchone() / .fetchall()."""
    if IS_PG:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur
    return conn.execute(sql, params)


# ── DB initialisation ─────────────────────────────────────────────────────────

def init_db():
    with get_db() as conn:
        if IS_PG:
            for stmt in [
                '''CREATE TABLE IF NOT EXISTS players (
                       id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE)''',
                '''CREATE TABLE IF NOT EXISTS pubs (
                       id SERIAL PRIMARY KEY, order_num INTEGER NOT NULL UNIQUE,
                       name TEXT NOT NULL, address TEXT DEFAULT '',
                       lat REAL NOT NULL, lng REAL NOT NULL, par INTEGER NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS scores (
                       id SERIAL PRIMARY KEY, player_id INTEGER NOT NULL,
                       pub_id INTEGER NOT NULL, sips INTEGER NOT NULL,
                       UNIQUE(player_id, pub_id))''',
                '''CREATE TABLE IF NOT EXISTS crawl_state (
                       id INTEGER PRIMARY KEY DEFAULT 1,
                       current_pub_order INTEGER DEFAULT 1,
                       join_code TEXT DEFAULT 'crawl2024',
                       admin_password TEXT DEFAULT 'admin123')''',
                'INSERT INTO crawl_state (id) VALUES (1) ON CONFLICT DO NOTHING',
            ]:
                q(conn, stmt)
        else:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS pubs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_num INTEGER NOT NULL UNIQUE,
                    name TEXT NOT NULL, address TEXT DEFAULT '',
                    lat REAL NOT NULL, lng REAL NOT NULL, par INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER NOT NULL,
                    pub_id INTEGER NOT NULL, sips INTEGER NOT NULL,
                    UNIQUE(player_id, pub_id));
                CREATE TABLE IF NOT EXISTS crawl_state (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    current_pub_order INTEGER DEFAULT 1,
                    join_code TEXT DEFAULT 'crawl2024',
                    admin_password TEXT DEFAULT 'admin123');
                INSERT OR IGNORE INTO crawl_state (id) VALUES (1);
            ''')


def load_pubs_from_json():
    if not os.path.exists('pubs.json'):
        return
    with get_db() as conn:
        count = q(conn, 'SELECT COUNT(*) AS c FROM pubs').fetchone()['c']
        if count > 0:
            return
        with open('pubs.json') as f:
            pubs = json.load(f)
        for pub in pubs:
            q(conn,
              f'INSERT INTO pubs (order_num, name, address, lat, lng, par) VALUES ({P},{P},{P},{P},{P},{P})',
              (pub['order'], pub['name'], pub.get('address', ''), pub['lat'], pub['lng'], pub['par']))


def get_leaderboard():
    with get_db() as conn:
        rows = q(conn, '''
            SELECT p.name,
                   COALESCE(SUM(s.sips - pu.par), 0) AS score,
                   COUNT(s.id) AS pubs_completed
            FROM players p
            LEFT JOIN scores s ON p.id = s.player_id
            LEFT JOIN pubs pu ON s.pub_id = pu.id
            GROUP BY p.id, p.name
            ORDER BY score ASC, pubs_completed DESC
        ''').fetchall()
        return [dict(r) for r in rows]


# Run on startup (works for both `python app.py` and gunicorn importing the module)
init_db()
load_pubs_from_json()


# ── Auth decorator ────────────────────────────────────────────────────────────

def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'player_id' not in session:
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'player_id' in session:
        return redirect(url_for('score'))
    return render_template('join.html')


@app.route('/join', methods=['POST'])
def join():
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip()
    if not name:
        return render_template('join.html', error='Please enter your name.')

    with get_db() as conn:
        state = q(conn, 'SELECT join_code FROM crawl_state WHERE id = 1').fetchone()
        if code != state['join_code']:
            return render_template('join.html', error='Wrong join code — ask the organiser!')

        player = q(conn, f'SELECT id FROM players WHERE name = {P}', (name,)).fetchone()
        if player:
            player_id = player['id']
        else:
            if IS_PG:
                player_id = q(conn, f'INSERT INTO players (name) VALUES ({P}) RETURNING id', (name,)).fetchone()['id']
            else:
                player_id = q(conn, f'INSERT INTO players (name) VALUES ({P})', (name,)).lastrowid

    session['player_id'] = player_id
    session['player_name'] = name
    return redirect(url_for('score'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/score')
@require_login
def score():
    with get_db() as conn:
        state = q(conn, 'SELECT current_pub_order FROM crawl_state WHERE id = 1').fetchone()
        pub = q(conn, f'SELECT * FROM pubs WHERE order_num = {P}', (state['current_pub_order'],)).fetchone()
        total_pubs = q(conn, 'SELECT COUNT(*) AS c FROM pubs').fetchone()['c']
        existing = None
        if pub:
            existing = q(conn,
                         f'SELECT sips FROM scores WHERE player_id = {P} AND pub_id = {P}',
                         (session['player_id'], pub['id'])).fetchone()

    return render_template('score.html',
                           pub=dict(pub) if pub else None,
                           existing=dict(existing) if existing else None,
                           total_pubs=total_pubs,
                           current_pub_order=state['current_pub_order'])


@app.route('/submit_score', methods=['POST'])
@require_login
def submit_score():
    sips = request.form.get('sips', type=int)
    pub_id = request.form.get('pub_id', type=int)

    if sips is None or sips < 0 or pub_id is None:
        return redirect(url_for('score'))

    with get_db() as conn:
        state = q(conn, 'SELECT current_pub_order FROM crawl_state WHERE id = 1').fetchone()
        current_pub = q(conn, f'SELECT id FROM pubs WHERE order_num = {P}', (state['current_pub_order'],)).fetchone()
        if not current_pub or current_pub['id'] != pub_id:
            return redirect(url_for('score'))

        q(conn, f'''
            INSERT INTO scores (player_id, pub_id, sips) VALUES ({P},{P},{P})
            ON CONFLICT(player_id, pub_id) DO UPDATE SET sips = excluded.sips
        ''', (session['player_id'], pub_id, sips))

    socketio.emit('leaderboard_update', {'leaderboard': get_leaderboard()})
    return redirect(url_for('leaderboard'))


@app.route('/leaderboard')
@require_login
def leaderboard():
    return render_template('leaderboard.html', leaderboard=get_leaderboard())


@app.route('/map')
@require_login
def map_view():
    with get_db() as conn:
        state = q(conn, 'SELECT current_pub_order FROM crawl_state WHERE id = 1').fetchone()
        current_pub = q(conn, f'SELECT * FROM pubs WHERE order_num = {P}', (state['current_pub_order'],)).fetchone()
        next_pub = q(conn, f'SELECT * FROM pubs WHERE order_num = {P}', (state['current_pub_order'] + 1,)).fetchone()

    return render_template('map.html',
                           current_pub=dict(current_pub) if current_pub else None,
                           next_pub=dict(next_pub) if next_pub else None)


@app.route('/admin', methods=['GET', 'POST'])
def admin():
    error = None
    success = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'login':
            with get_db() as conn:
                state = q(conn, 'SELECT admin_password FROM crawl_state WHERE id = 1').fetchone()
            if request.form.get('password') == state['admin_password']:
                session['is_admin'] = True
            else:
                error = 'Wrong admin password.'

        elif not session.get('is_admin'):
            return redirect(url_for('admin'))

        elif action == 'next_pub':
            with get_db() as conn:
                order = q(conn, 'SELECT current_pub_order FROM crawl_state WHERE id = 1').fetchone()['current_pub_order']
                total = q(conn, 'SELECT COUNT(*) AS c FROM pubs').fetchone()['c']
                if order < total:
                    q(conn, 'UPDATE crawl_state SET current_pub_order = current_pub_order + 1 WHERE id = 1')
                    success = 'Advanced to next pub!'
                    socketio.emit('pub_changed', {})
                else:
                    error = 'Already at the last pub!'

        elif action == 'prev_pub':
            with get_db() as conn:
                order = q(conn, 'SELECT current_pub_order FROM crawl_state WHERE id = 1').fetchone()['current_pub_order']
                if order > 1:
                    q(conn, 'UPDATE crawl_state SET current_pub_order = current_pub_order - 1 WHERE id = 1')
                    success = 'Back to previous pub.'
                    socketio.emit('pub_changed', {})
                else:
                    error = 'Already at the first pub!'

        elif action == 'update_join_code':
            new_code = request.form.get('join_code', '').strip()
            if new_code:
                with get_db() as conn:
                    q(conn, f'UPDATE crawl_state SET join_code = {P} WHERE id = 1', (new_code,))
                success = f'Join code updated to: {new_code}'

        elif action == 'update_admin_password':
            new_pw = request.form.get('admin_password', '').strip()
            if new_pw:
                with get_db() as conn:
                    q(conn, f'UPDATE crawl_state SET admin_password = {P} WHERE id = 1', (new_pw,))
                success = 'Admin password updated.'

        elif action == 'reset_scores':
            with get_db() as conn:
                q(conn, 'DELETE FROM scores')
                q(conn, 'UPDATE crawl_state SET current_pub_order = 1 WHERE id = 1')
            socketio.emit('leaderboard_update', {'leaderboard': []})
            socketio.emit('pub_changed', {})
            success = 'All scores cleared and crawl reset to pub 1.'

    if not session.get('is_admin'):
        return render_template('admin.html', logged_in=False, error=error)

    with get_db() as conn:
        state = dict(q(conn, 'SELECT * FROM crawl_state WHERE id = 1').fetchone())
        current_pub = q(conn, f'SELECT * FROM pubs WHERE order_num = {P}', (state['current_pub_order'],)).fetchone()
        total_pubs = q(conn, 'SELECT COUNT(*) AS c FROM pubs').fetchone()['c']

    return render_template('admin.html',
                           logged_in=True,
                           state=state,
                           current_pub=dict(current_pub) if current_pub else None,
                           total_pubs=total_pubs,
                           leaderboard=get_leaderboard(),
                           error=error,
                           success=success)


# ── Sockets ───────────────────────────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    emit('leaderboard_update', {'leaderboard': get_leaderboard()})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print('\n  Pub Crawl app running.')
    if not IS_PG:
        print(f'  Local: http://localhost:{port}')
        print('  On your network: http://<your-ip>:5000\n')
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
