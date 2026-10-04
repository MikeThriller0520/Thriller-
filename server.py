#!/usr/bin/env python3
import hashlib, json, os, random, secrets, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
ROOT = Path(__file__).resolve().parent
DB = ROOT / "draw.db"
PASSWORD = "thriller"

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init():
    c = conn()
    c.executescript('''
    create table if not exists legs (id text primary key, label text, game text, need text, result text default 'pending', stat text default 'prop', line_number real);
    create table if not exists users (id integer primary key autoincrement, email text unique, password_hash text, salt text, balance real default 100, token text);
    create table if not exists tickets (id integer primary key autoincrement, price real, payout real, status text, created text default current_timestamp, user_id integer);
    create table if not exists withdrawals (id integer primary key autoincrement, user_id integer, amount real, status text default 'requested', created text default current_timestamp);
    create table if not exists notes (id integer primary key autoincrement, user_id integer, body text, created text default current_timestamp);
    create table if not exists ticket_legs (ticket_id integer, leg_id text, label text, game text, need text);
    ''')
    if c.execute('select count(*) from legs').fetchone()[0] == 0:
        c.executemany('insert into legs (id, label, game, need, stat, line_number) values (?,?,?,?,?,?)', [
            ('allen-pass','Josh Allen over 249.5 passing yards','NE-BUF','Over','passing yards',249.5),
            ('chase-rec',"Ja'Marr Chase over 74.5 receiving yards",'JAX-CIN','Over','receiving yards',74.5),
            ('saquon-rush','Saquon Barkley over 69.5 rushing yards','LAR-PHI','Over','rushing yards',69.5),
            ('mahomes-pass','Patrick Mahomes over 239.5 passing yards','KC-LV','Over','passing yards',239.5),
            ('bills-spread','Bills -7','NE-BUF','Cover','spread',-7),
            ('lions-spread','Lions -3.5','DET-CAR','Cover','spread',-3.5),
            ('niners-spread','49ers -3','DEN-SF','Cover','spread',-3),
            ('vikings-spread','Vikings -10','MIA-MIN','Cover','spread',-10)])
    c.commit(); c.close()

def grade(c):
    for ticket in c.execute("select * from tickets where status='open'"):
        results=[r['result'] for r in c.execute('select l.result from ticket_legs tl join legs l on l.id=tl.leg_id where tl.ticket_id=?',(ticket['id'],))]
        if not results or any(r=='pending' for r in results): continue
        status='refund' if any(r=='push' for r in results) else 'won' if all(r=='hit' for r in results) else 'lost'
        if status=='won':
            c.execute('update users set balance=balance+? where id=?',(ticket['payout'],ticket['user_id']))
        elif status=='refund':
            c.execute('update users set balance=balance+? where id=?',(ticket['price'],ticket['user_id']))
        c.execute('update tickets set status=? where id=?',(status,ticket['id']))

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type='application/json'):
        data=body if isinstance(body,bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.end_headers(); self.wfile.write(data)
    def _read(self):
        n=int(self.headers.get('Content-Length',0))
        return json.loads(self.rfile.read(n).decode() or '{}') if n else {}
    def _user(self):
        token=self.headers.get('Authorization','').replace('Bearer ','').strip()
        if not token: return None
        c=conn(); row=c.execute('select * from users where token=?',(token,)).fetchone(); c.close(); return row
    def _hash(self, password, salt):
        return hashlib.sha256(f'{salt}:{password}'.encode()).hexdigest()
    def _file(self, path):
        rel='index.html' if path in ('','/') else path.lstrip('/')
        file_path=(ROOT/rel).resolve()
        if not str(file_path).startswith(str(ROOT)) or not file_path.is_file():
            return self._send(404, {'error':'not found'})
        kind={'.html':'text/html; charset=utf-8','.svg':'image/svg+xml'}.get(file_path.suffix,'application/octet-stream')
        self._send(200, file_path.read_bytes(), kind)
    def _tickets(self, user_id):
        c=conn(); out=[]
        for ticket in c.execute('select * from tickets where user_id=? order by id desc',(user_id,)):
            item=dict(ticket)
            item['legs']=[dict(r) for r in c.execute('select tl.label, tl.game, tl.need, l.result from ticket_legs tl join legs l on l.id=tl.leg_id where tl.ticket_id=?',(ticket['id'],))]
            out.append(item)
        c.close(); return out
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/tickets':
            user=self._user()
            if not user: return self._send(401, {'error':'log in'})
            return self._send(200, {'tickets':self._tickets(user['id']),'balance':user['balance'],'email':user['email'],'notes':[]})
        if path=='/api/admin':
            c=conn(); legs=[dict(r) for r in c.execute('select * from legs order by label')]; c.close()
            return self._send(200, {'legs':legs})
        if path=='/api/report':
            c=conn()
            players=[]
            for user in c.execute('select id, email, balance from users order by id desc'):
                row=dict(user)
                row['tickets']=[dict(t) for t in c.execute('select id, price, payout, status, created from tickets where user_id=? order by id desc limit 8',(user['id'],))]
                players.append(row)
            balances=dict(c.execute('select count(*) as players, coalesce(sum(balance),0) as balances from users').fetchone())
            totals=dict(c.execute("select count(*) as tickets, coalesce(sum(price),0) as staked, coalesce(sum(case when status='won' then payout else 0 end),0) as paid, coalesce(sum(case when status='open' then price else 0 end),0) as open_stake from tickets").fetchone())
            c.close()
            return self._send(200, {'players':players,'balances':balances,'totals':totals,'day':{'wins':0,'losses':0,'ratio':None},'withdrawals':[]})
        return self._file(path)
    def do_POST(self):
        path=urlparse(self.path).path
        payload=self._read()
        if path=='/api/signup': return self._signup(payload)
        if path=='/api/login': return self._login(payload)
        if path=='/api/draw': return self._draw(payload)
        if path=='/api/discard': return self._discard(payload)
        if path=='/api/withdraw': return self._withdraw(payload)
        if path=='/api/results':
            if payload.get('password')!=PASSWORD: return self._send(401, {'error':'bad password'})
            return self._result(payload)
        if path=='/api/legs':
            if payload.get('password')!=PASSWORD: return self._send(401, {'error':'bad password'})
            return self._save_leg(payload)
        if path=='/api/credit':
            if payload.get('password')!=PASSWORD: return self._send(401, {'error':'bad password'})
            c=conn(); c.execute('update users set balance=balance+? where id=?',(float(payload.get('amount',0)), payload.get('user_id'))); c.commit(); c.close()
            return self._send(200, {'ok':True})
        self._send(404, {'error':'not found'})
    def _signup(self, payload):
        email=(payload.get('email') or '').strip().lower(); password=payload.get('password') or ''
        if '@' not in email or len(password)<4: return self._send(400, {'error':'email and a 4 character password'})
        salt, token=secrets.token_hex(8), secrets.token_hex(16)
        c=conn()
        try:
            c.execute('insert into users (email, password_hash, salt, token, balance) values (?,?,?,?,100)',(email,self._hash(password,salt),salt,token)); c.commit()
        except sqlite3.IntegrityError:
            c.close(); return self._send(409, {'error':'that email already has an account'})
        bal=c.execute('select balance from users where email=?',(email,)).fetchone()['balance']; c.close()
        return self._send(200, {'token':token,'email':email,'balance':bal})
    def _login(self, payload):
        email=(payload.get('email') or '').strip().lower(); password=payload.get('password') or ''
        c=conn(); user=c.execute('select * from users where email=?',(email,)).fetchone()
        if not user or user['password_hash']!=self._hash(password,user['salt']):
            c.close(); return self._send(401, {'error':'wrong email or password'})
        token=secrets.token_hex(16); c.execute('update users set token=? where id=?',(token,user['id'])); c.commit(); c.close()
        return self._send(200, {'token':token,'email':email,'balance':user['balance']})
    def _draw(self, payload):
        user=self._user()
        if not user: return self._send(401, {'error':'log in'})
        price=float(payload.get('price',0))
        if price not in (1,5,10,20,50): return self._send(400, {'error':'price must be 1, 5, 10, 20, or 50'})
        c=conn(); fresh=c.execute('select * from users where id=?',(user['id'],)).fetchone()
        if fresh['balance']<price:
            c.close(); return self._send(400, {'error':'not enough balance'})
        c.execute('update users set balance=balance-? where id=?',(price,user['id']))
        bag=list(c.execute('select * from legs')); random.shuffle(bag); used,picked=set(),[]
        for leg in bag:
            if leg['game'] in used: continue
            used.add(leg['game']); picked.append(leg)
            if len(picked)==4: break
        cur=c.execute("insert into tickets (price, payout, status, user_id) values (?,?, 'open', ?)",(price,price*12,user['id']))
        for leg in picked:
            c.execute('insert into ticket_legs (ticket_id, leg_id, label, game, need) values (?,?,?,?,?)',(cur.lastrowid,leg['id'],leg['label'],leg['game'],leg['need']))
        c.commit(); balance=c.execute('select balance from users where id=?',(user['id'],)).fetchone()['balance']
        ticket=next(t for t in self._tickets(user['id']) if t['id']==cur.lastrowid); c.close()
        return self._send(200, {'ticket':ticket,'balance':balance})
    def _discard(self, payload):
        user=self._user()
        if not user: return self._send(401, {'error':'log in'})
        c=conn(); c.execute("update tickets set status='discarded' where id=? and user_id=? and status='open'",(payload.get('id'),user['id'])); c.commit(); c.close()
        return self._send(200, {'ok':True})
    def _withdraw(self, payload):
        user=self._user()
        if not user: return self._send(401, {'error':'log in'})
        amount=float(payload.get('amount',0))
        if amount<10 or amount>user['balance']: return self._send(400, {'error':'request 10 or more, and no more than the balance'})
        c=conn(); c.execute("insert into withdrawals (user_id, amount, status) values (?,?, 'requested')",(user['id'],amount)); c.commit(); c.close()
        return self._send(200, {'ok':True,'status':'requested'})
    def _result(self, payload):
        result=payload.get('result')
        if result not in ('pending','hit','miss','push'): return self._send(400, {'error':'bad result'})
        c=conn(); c.execute('update legs set result=? where id=?',(result,payload.get('leg_id'))); grade(c); c.commit()
        legs=[dict(r) for r in c.execute('select * from legs order by label')]; c.close()
        return self._send(200, {'legs':legs})
    def _save_leg(self, payload):
        player=(payload.get('player') or '').strip(); stat=(payload.get('stat') or '').strip(); game=(payload.get('game') or player or 'NFL').strip()
        number=float(payload.get('number')); side=(payload.get('side') or 'Over').strip()
        label=f'{player} {side.lower()} {number:g} {stat}'; leg_id=f'{player}-{stat}-{number}'.lower().replace(' ','-')
        c=conn()
        c.execute('insert into legs (id,label,game,need,stat,line_number,result) values (?,?,?,?,?,?,\'pending\') on conflict(id) do update set label=excluded.label, game=excluded.game, need=excluded.need, stat=excluded.stat, line_number=excluded.line_number',(leg_id,label,game,side,stat,number))
        c.commit(); legs=[dict(r) for r in c.execute('select * from legs order by label')]; c.close()
        return self._send(200, {'legs':legs})
    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args))

if __name__=='__main__':
    init(); port=int(os.environ.get('PORT','8787')); print(f'thriller draw on :{port}')
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
