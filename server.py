#!/usr/bin/env python3
"""拾事独立后台：本地增量缓存 -> 证据审核 -> 原生 EventKit 队列。"""
import fcntl
import difflib
import hashlib
import json
import os
import pathlib
import queue
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from core import VERSION, SYSTEM, TZ, clean_message, candidate, eligible, validate_item, digest, declines, review_batch, TIME_PREFIX

ROOT = pathlib.Path(__file__).resolve().parent
DATA = pathlib.Path(os.environ.get('SHISHI_DATA', str(pathlib.Path.home()/'Library/Application Support/Shishi')))
PORT = int(os.environ.get('SHISHI_PORT', '8766'))
ALPHA = 'http://127.0.0.1:5031'
MODEL = os.environ.get('SHISHI_MODEL', 'deepseek-v4-pro')
ASR_SCRIPT = ROOT/'asr.py'
ASR_MODEL_DIR = pathlib.Path(os.environ.get('SHISHI_ASR_MODEL', str(pathlib.Path.home()/'Library/Application Support/Alpha/voice/models/qwen3-asr-0.6b')))
ASR_BATCH = 6
LOOKBACK_DEFAULT = 30   # 首次处理只回看最近 30 天，旧聊天可在界面里再放宽
BATCH_MESSAGES = 60     # 每次模型调用最多带上的消息数（含上下文）
VISIBLE_SESSIONS = "kind<>3 AND id NOT IN ('brandservicesessionholder','brandsessionholder')"
CONTEXT_BEFORE = 8      # 候选消息前保留的上下文条数


def atomic(path, value):
    temp = path.with_name(path.name+'.'+secrets.token_hex(4)+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temp.chmod(0o600)
    os.replace(temp, path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def credentials():
    out = {}
    path = pathlib.Path.home()/'.config/wvbar/secrets.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k,v = line.split('=',1)
                out[k.strip()] = v.strip().strip('\"').strip("'")
    return out


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,name TEXT,kind INTEGER,last_ts INTEGER,selected INTEGER DEFAULT 0,offset INTEGER DEFAULT 0,synced_ts INTEGER DEFAULT 0,error TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS messages(sid TEXT,mid TEXT,ts INTEGER,body TEXT,hash TEXT,processed INTEGER DEFAULT 0,PRIMARY KEY(sid,mid));
            CREATE INDEX IF NOT EXISTS message_pending ON messages(processed,ts);
            CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,sid TEXT,body TEXT,status TEXT,reminder_id TEXT DEFAULT '',revision INTEGER DEFAULT 1,updated REAL);
            CREATE TABLE IF NOT EXISTS batches(hash TEXT PRIMARY KEY,result TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY,tokens INTEGER DEFAULT 0,calls INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,item_id TEXT,revision INTEGER,payload TEXT,status TEXT DEFAULT 'pending',error TEXT DEFAULT '');
            ''')
            try:
                db.execute('ALTER TABLE messages ADD COLUMN skipped INTEGER DEFAULT 0')
            except sqlite3.OperationalError:
                pass
            for column in ("seq INTEGER DEFAULT 0","voice_url TEXT DEFAULT ''","transcribed INTEGER DEFAULT 0"):
                try:
                    db.execute('ALTER TABLE messages ADD COLUMN '+column)
                except sqlite3.OperationalError:
                    pass
            try:
                db.execute("ALTER TABLE sessions ADD COLUMN avatar TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            for column in ("pinned INTEGER DEFAULT 0","folded INTEGER DEFAULT 0","muted INTEGER DEFAULT 0","sort_ts INTEGER DEFAULT 0"):
                try:
                    db.execute('ALTER TABLE sessions ADD COLUMN '+column)
                except sqlite3.OperationalError:
                    pass

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(str(self.path), timeout=30)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def setting(self, key, default=None):
        with self.db() as db:
            r = db.execute('SELECT v FROM settings WHERE k=?',(key,)).fetchone()
            return json.loads(r[0]) if r else default

    def set_setting(self, key, val):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(val)))

    def query(self, sql, args=()):
        with self.db() as db:
            return [dict(r) for r in db.execute(sql,args)]

    def execute(self, sql, args=()):
        with self.db() as db:
            db.execute(sql,args)

    def items(self, sid=None):
        rows = self.query('SELECT * FROM items'+(' WHERE sid=?' if sid else '')+' ORDER BY updated DESC', (sid,) if sid else ())
        return [dict(json.loads(r['body']), id=r['id'],sid=r['sid'],status=r['status'],reminder_id=r['reminder_id'],revision=r['revision']) for r in rows]

    def put_item(self, sid, item):
        target = item.get('target_id')
        existing = self.items(sid)
        match = next((x for x in existing if x['id'] == target), None)
        if item['action'] != 'create' and not match:
            item['auto'] = False
            item['review_reasons'].append('找不到被变更的原事项')
        if item['action'] == 'dismiss' and match and match['status'] == 'review':
            if any(e['direction']=='2' and declines(e['text'] if 'text' in e else e['quote']) for e in item['evidence']):
                self.execute("UPDATE items SET status='dismissed',body=?,revision=revision+1,updated=? WHERE id=?",(json.dumps(item,ensure_ascii=False),time.time(),match['id']))
            return
        if item['action'] == 'confirm' and match:
            if match['status'] != 'review':
                return
            if item['due'] != match.get('due') and item['due'] is not None:
                item['auto'] = False
                item['review_reasons'].append('确认时日期发生变化，需要核对')
            item['due'] = item['due'] or match.get('due')
            if match.get('due'):
                from core import parse_due
                if parse_due(match['due']) <= datetime.now(TZ):
                    item['auto'] = False
                    item['review_reasons'].append('原事项时间已过')
            # 证据里需包含原事项，不能将一次同意应用到无关待办。
            oldrefs = {e['id'] for e in match['evidence']}
            if not oldrefs.intersection(e['id'] for e in item['evidence']):
                item['auto'] = False
                item['review_reasons'].append('确认未引用原事项')
            iid = match['id']
        else:
            # Same chat, action, normalized title and date gives stable semantic key.
            key = ''.join(c for c in item['title'].lower() if c.isalnum())
            iid = digest([sid,key,item.get('due'),item['action'],target])[:32]
            same = next((x for x in existing if x['id']==iid),None)
            if same:
                if same['status']=='review' and item['auto']:
                    # 模型仍叫它 create 时，也更新同一条待查看，不丢失新答复。
                    item['target_id']=same['id']
                    item['action']='confirm'
                else:
                    return
            # 语义去重：同一条消息可能拆出多个不同安排（周五/周六课程），
            # 只有日期相同且标题相近时才当作重复，避免把正常拆分误判。
            def similar_title(left, right):
                def strip_time(value):
                    value=str(value or '')
                    match=TIME_PREFIX.match(value)
                    return value[match.end():] if match else value
                a=''.join(ch for ch in strip_time(left).lower() if ch.isalnum())
                b=''.join(ch for ch in strip_time(right).lower() if ch.isalnum())
                if not a or not b:return False
                return a==b or a in b or b in a or difflib.SequenceMatcher(None,a,b).ratio()>=0.6
            for x in existing:
                if x['status'] not in ('dismissed','cancelled') and x['action']=='create' and item['action']=='create':
                    if x.get('due')==item.get('due') and similar_title(x.get('title'),item.get('title')) and \
                       {e['id'] for e in x['evidence']}.intersection(e['id'] for e in item['evidence']):
                        item['auto'] = False
                        item['review_reasons'].append('与已有事项证据重叠，请核对是否重复')
                        break
        status = 'ready' if item['auto'] else 'review'
        with self.db() as db:
            db.execute('''INSERT INTO items(id,sid,body,status,updated) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET body=excluded.body,status=excluded.status,revision=items.revision+1,updated=excluded.updated''',
                (iid,sid,json.dumps(item,ensure_ascii=False),status,time.time()))


class Engine:
    def __init__(self, data=DATA):
        self.data = pathlib.Path(data)
        self.data.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.data.chmod(0o700)
        self.ipc = self.data/'bridge'
        self.ipc.mkdir(exist_ok=True,mode=0o700)
        for name in ('requests','responses'):
            (self.ipc/name).mkdir(exist_ok=True,mode=0o700)
        self.store = Store(self.data/'shishi.sqlite3')
        self.store.execute('UPDATE sessions SET selected=0 WHERE NOT ('+VISIBLE_SESSIONS+')')
        self.avatars = self.data/'avatars'
        self.avatars.mkdir(exist_ok=True,mode=0o700)
        self.keys = credentials()
        self.csrf = secrets.token_urlsafe(32)
        self.state = {'connected':False,'sync':'正在连接微信接口','analysis':'等待选择聊天','error':'','fatal':False,'account_ok':False}
        self.dirty = queue.Queue()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.account = ''
        if self.store.setting('new_only',None) is None:
            # 默认只记录新消息：从生效当天 0 点起，不再回捞旧聊天。
            self.store.set_setting('new_only',True)
            self.store.set_setting('since_date',datetime.now(TZ).date().isoformat())
        self.failures = 0
        self.retry_at = 0
        self.warnings = 0
        self.heartbeat = {'sync':0,'analysis':0,'bridge':0,'asr':0}
        self.thread_error = ''
        self.last_menu_sig = ''
        self.asr_enabled = ASR_SCRIPT.exists() and (ASR_MODEL_DIR/'encoder.int8.onnx').exists()
        self.asr_error = ''

    def alpha(self, path):
        req = urllib.request.Request(ALPHA+path,headers={'Authorization':'Bearer '+self.keys.get('ALPHA_TOKEN','')})
        with urllib.request.urlopen(req,timeout=35) as r:
            return json.load(r)

    def discover(self):
        account = self.alpha('/api/v1/accounts')
        selected = account.get('selected_account_id') or account.get('selectedAccountId')
        if not selected:
            raise ValueError('请先在 Alpha 选择微信账户')
        saved = self.store.setting('account')
        if saved and saved != selected:
            self.state['account_ok'] = False
            raise ValueError('微信账户已切换：已暂停，防止不同账号的事项混在一起')
        self.store.set_setting('account',selected)
        self.account = selected
        self.state['account_ok'] = True
        offset = 0
        seen = set()
        while True:
            r = self.alpha('/api/v1/sessions?limit=200&offset='+str(offset))
            rows = r.get('items') or []
            with self.store.db() as db:
                for s in rows:
                    if s['id'] in seen:
                        raise ValueError('会话分页重复，读取不完整')
                    seen.add(s['id'])
                    db.execute('''INSERT INTO sessions(id,name,kind,last_ts,avatar,pinned,folded,muted,sort_ts)
                        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id)
                        DO UPDATE SET name=excluded.name,kind=excluded.kind,last_ts=excluded.last_ts,
                        avatar=CASE WHEN excluded.avatar<>'' THEN excluded.avatar ELSE sessions.avatar END,
                        pinned=excluded.pinned,folded=excluded.folded,muted=excluded.muted,sort_ts=excluded.sort_ts''',
                        (s['id'],s.get('display_name') or s['id'],s.get('kind',0),s.get('last_activity_unix_ms') or 0,
                         str(s.get('avatar_uri') or '')[:2000],1 if s.get('is_pinned') else 0,
                         1 if s.get('is_folded') else 0,1 if s.get('is_muted') else 0,
                         int(s.get('sort_timestamp') or s.get('last_activity_unix_ms') or 0)))
            if not r.get('hasMore'):
                break
            nxt = r.get('nextOffset')
            if not isinstance(nxt,int) or nxt <= offset or not rows:
                raise ValueError('会话分页未前进')
            offset = nxt
        self.state['sync'] = '已读取 %s 个会话，请勾选需要分析的聊天' % len(seen)

    def sync_session(self, session):
        sid = session['id']
        offset = max(0,session['offset']-30)
        # 页面级保存，可恢复中断；小幅重叠捕获尾部变动。
        while not self.stop.is_set():
            if not self.store.query('SELECT id FROM sessions WHERE id=? AND selected=1',(sid,)):
                return
            r = self.alpha('/api/v1/messages?'+urllib.parse.urlencode({'talker':sid,'limit':500,'offset':offset,'media':'false'}))
            rows = r.get('messages') or []
            with self.store.db() as db:
                for index,raw in enumerate(rows):
                    m = clean_message(raw)
                    h = digest(m)
                    db.execute('''INSERT INTO messages(sid,mid,ts,body,hash,processed,seq) VALUES(?,?,?,?,?,0,?) ON CONFLICT(sid,mid)
                        DO UPDATE SET ts=excluded.ts,body=excluded.body,hash=excluded.hash,seq=COALESCE(excluded.seq,messages.seq),processed=0 WHERE messages.hash<>excluded.hash''',
                        (sid,m['id'],m['ts'],json.dumps(m,ensure_ascii=False),h,offset+index))
                nxt = r.get('nextOffset') if r.get('hasMore') else offset+len(rows)
                if not isinstance(nxt,int) or (r.get('hasMore') and (nxt<=offset or not rows)):
                    raise ValueError('消息分页未前进')
                db.execute("UPDATE sessions SET offset=?,error='' WHERE id=?",(nxt,sid))
            self.state['sync'] = '正在读取所选聊天：已读 %s 条' % nxt
            if not r.get('hasMore'):
                self.store.execute('UPDATE sessions SET synced_ts=? WHERE id=?',(session['last_ts'],sid))
                self.wake.set()
                return
            offset = nxt

    def sync_loop(self):
        last = 0
        while not self.stop.is_set():
            self.heartbeat['sync']=time.time()
            try:
                if time.time()-last>60:
                    self.discover()
                    last = time.time()
                sessions = self.store.query('SELECT * FROM sessions WHERE selected=1 ORDER BY last_ts DESC')
                dirty = set()
                while not self.dirty.empty():
                    dirty.add(self.dirty.get_nowait())
                for s in sessions:
                    if s['id'] in dirty or s['synced_ts'] != s['last_ts'] or not s['offset']:
                        try:
                            self.sync_session(s)
                        except Exception as exc:
                            self.store.execute('UPDATE sessions SET error=? WHERE id=?',(self.safe_error(exc),s['id']))
                if sessions:
                    self.state['sync'] = '所选聊天同步完成；新消息自动增量读取'
            except Exception as exc:
                self.state['sync'] = self.safe_error(exc)
            self.stop.wait(3)

    @staticmethod
    def safe_error(exc):
        if isinstance(exc,urllib.error.HTTPError):
            return {402:'API 额度不足，已暂停分析；充值后点击重试',401:'API 密钥失效或未配置',403:'API 权限不足',429:'API 限流，请稍后重试'}.get(exc.code,'接口返回 HTTP '+str(exc.code))
        if isinstance(exc,ValueError):
            return str(exc)[:180]
        if isinstance(exc,(urllib.error.URLError,TimeoutError)):
            return '网络连接失败，稍后自动重试'
        return '连接或处理失败：'+type(exc).__name__

    @staticmethod
    def fatal_error(exc,message):
        """只有额度、密钥、权限、账户切换这类问题才需要一直暂停。"""
        if isinstance(exc,urllib.error.HTTPError):
            return exc.code in (401,402,403)
        return any(k in message for k in ('预算','密钥','账户已切换','权限'))

    def warn(self,message):
        self.warnings=(self.warnings+1)%1000000
        self.state['notice']=str(message)[:180]
        self.state['notice_count']=self.warnings

    def lookback_days(self):
        days=self.store.setting('lookback_days',LOOKBACK_DEFAULT)
        try:days=float(days)
        except (TypeError,ValueError):days=LOOKBACK_DEFAULT
        return 0 if days<=0 else int(days)

    def lookback_cutoff(self):
        days=self.lookback_days()
        return 0 if not days else int((time.time()-days*86400)*1000)

    def since_cutoff(self):
        """仅记录新消息时，按用户设定的起始日期过滤（当天 0 点，Asia/Shanghai）。"""
        if not self.store.setting('new_only',False):return 0
        value=self.store.setting('since_date','')
        try:
            day=datetime.strptime(str(value),'%Y-%m-%d')
        except (TypeError,ValueError):
            day=datetime.now(TZ)
        day=day.replace(hour=0,minute=0,second=0,microsecond=0,tzinfo=TZ)
        return int(day.timestamp()*1000)

    def effective_cutoff(self):
        """两个日期判断取更严格的一个：回看范围 + 仅记录新消息。"""
        cutoffs=[x for x in (self.lookback_cutoff(),self.since_cutoff()) if x]
        return max(cutoffs) if cutoffs else 0

    def reset_skipped(self,new_cutoff,old_cutoff):
        """把日期判断放宽后，重新纳入此前只标记跳过、从未分析过的消息。"""
        if not old_cutoff:return
        if new_cutoff and new_cutoff>=old_cutoff:return
        low=0 if not new_cutoff else new_cutoff
        self.store.execute('UPDATE messages SET processed=0,skipped=0 WHERE sid IN '
            '(SELECT id FROM sessions WHERE selected=1) AND skipped=1 AND ts>=? AND ts<?',(low,old_cutoff))

    def mark_processed(self,sid,pairs,skipped=0):
        if not pairs:return
        if skipped:
            sql='UPDATE messages SET processed=1,skipped=1 WHERE sid=? AND mid=? AND hash=?'
        else:
            sql='UPDATE messages SET processed=1 WHERE sid=? AND mid=? AND hash=?'
        with self.store.db() as db:
            db.executemany(sql,[(sid,mid,h) for mid,h in pairs])

    def fetch_avatar(self,uri):
        """头像只在有会话头像地址时按需下载，并缓存到本地目录。"""
        if not isinstance(uri,str) or not uri:return None
        try:
            if uri.startswith('http://') or uri.startswith('https://'):
                req=urllib.request.Request(uri,headers={'User-Agent':'Shishi/1.1'})
                with urllib.request.urlopen(req,timeout=15) as r:
                    data=r.read(3*1024*1024)
            else:
                # 部分账户给的是不透明媒体 id，需要走 Alpha 的媒体接口。
                q=urllib.parse.quote(uri,safe='')
                req=urllib.request.Request(ALPHA+'/api/v1/media/'+q,
                    headers={'Authorization':'Bearer '+self.keys.get('ALPHA_TOKEN','')})
                with urllib.request.urlopen(req,timeout=15) as r:
                    data=r.read(3*1024*1024)
            return data if data else None
        except Exception:
            return None

    def avatar(self,sid):
        rows=self.store.query('SELECT avatar FROM sessions WHERE id=?',(sid,))
        if not rows or not rows[0]['avatar']:return None
        uri=rows[0]['avatar']
        name=hashlib.sha256(sid.encode()).hexdigest()[:16]+'.img'
        path=self.avatars/name
        data=None
        if path.exists():
            try:
                if path.stat().st_size>0:data=path.read_bytes()
            except OSError:
                data=None
        if data is None:
            data=self.fetch_avatar(uri)
            if not data:return None
            temp=path.with_name(path.name+'.'+secrets.token_hex(3)+'.tmp')
            temp.write_bytes(data);temp.chmod(0o600);os.replace(temp,path)
        mime='image/png'
        if data[:3]==b'\xff\xd8\xff':mime='image/jpeg'
        elif data[:4]==b'GIF8':mime='image/gif'
        elif data[:4]==b'RIFF' and data[8:12]==b'WEBP':mime='image/webp'
        return data,mime

    def push_loop(self):
        while not self.stop.is_set():
            if not self.account:
                self.stop.wait(3)
                continue
            try:
                req=urllib.request.Request(ALPHA+'/api/v1/push/messages?accountId='+urllib.parse.quote(self.account),headers={'Authorization':'Bearer '+self.keys.get('ALPHA_TOKEN',''),'Accept':'text/event-stream'})
                with urllib.request.urlopen(req,timeout=90) as r:
                    self.state['connected']=True
                    event, lines = '',[]
                    for raw in r:
                        line=raw.decode('utf-8','replace').strip()
                        if line.startswith('event:'): event=line[6:].strip()
                        elif line.startswith('data:'): lines.append(line[5:].strip())
                        elif not line:
                            if event=='message.new' and lines:
                                try:
                                    d=json.loads('\n'.join(lines))
                                    sid=d.get('sessionId') or d.get('session_id') or d.get('talker')
                                    if sid:self.dirty.put(sid)
                                except ValueError:pass
                            event,lines='',[]
                        if self.stop.is_set():return
            except Exception:
                self.state['connected']=False
                self.stop.wait(5)

    def usage(self):
        day = datetime.now(TZ).date().isoformat()
        r = self.store.query('SELECT * FROM usage WHERE day=?',(day,))
        return r[0] if r else {'day':day,'tokens':0,'calls':0}

    def extract(self, messages, fresh, existing, session):
        payload = {'now':datetime.now(TZ).date().isoformat(),'timezone':'Asia/Shanghai','group':session['kind']==2,
            'chat':str(session.get('name') or '')[:40],
            'messages':[{'id':m['id'],'time':datetime.fromtimestamp(m['ts']/1000,TZ).isoformat(),'who':'me' if m['direction']=='2' else 'other','text':m['text']} for m in messages if eligible(m)],
            'fresh_ids':fresh,'existing':[{'id':x['id'],'title':x['title'],'due':x.get('due'),'status':x['status']} for x in existing[:25]]}
        body = json.dumps(payload,ensure_ascii=False)
        key = digest([VERSION,MODEL,body])
        cached=self.store.query('SELECT result FROM batches WHERE hash=?',(key,))
        if cached:return json.loads(cached[0]['result'])
        # 保守预留：UTF-8字节数作为输入 token 上界，加输出上限，不并发模型调用。
        reserve = len((SYSTEM+body).encode())+2200
        budget = self.store.setting('daily_budget',30000)
        usage = self.usage()
        if usage['tokens']+reserve>budget:
            raise ValueError('今日 token 预算不足，已保留未处理消息；可调整预算或明日继续')
        self.store.execute('INSERT INTO usage VALUES(?,?,1) ON CONFLICT(day) DO UPDATE SET tokens=tokens+excluded.tokens,calls=calls+1',(usage['day'],reserve))
        req = urllib.request.Request('https://api.deepseek.com/chat/completions',data=json.dumps({
            'model':MODEL,'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':body}],
            'temperature':.1,'thinking':{'type':'disabled'},'max_tokens':2200,
            'response_format':{'type':'json_object'}
        }).encode(),headers={'Authorization':'Bearer '+self.keys.get('DEEPSEEK_API_KEY',''),'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=120) as r:
                result=json.load(r)
        except Exception:
            # 调用失败时退回预留额度，避免一次网络错误把当日预算算掉。
            self.store.execute('UPDATE usage SET tokens=tokens-? WHERE day=?',(reserve,usage['day']))
            raise
        used=result.get('usage',{}).get('total_tokens')
        if isinstance(used,int):
            self.store.execute('UPDATE usage SET tokens=tokens+? WHERE day=?',(used-reserve,usage['day']))
        choices=result.get('choices') or []
        if not choices:raise ValueError('模型未返回结果，保留待处理批次')
        choice=choices[0]
        if choice.get('finish_reason')!='stop':raise ValueError('模型输出不完整，保留待处理批次')
        try:result=json.loads(choice['message']['content'])
        except (ValueError,KeyError,TypeError):
            raise ValueError('模型返回的内容无法解析，保留待处理批次')
        if not isinstance(result.get('items'),list) or len(result['items'])>20:
            raise ValueError('模型返回格式不符合约定')
        self.store.execute('INSERT OR REPLACE INTO batches VALUES(?,?,?)',(key,json.dumps(result,ensure_ascii=False),time.time()))
        return result

    def analyze_once(self):
        if not self.state['account_ok'] or self.store.setting('paused',False):return False
        cutoff=self.effective_cutoff()
        sessions=self.store.query('''SELECT s.* FROM sessions s WHERE selected=1 AND synced_ts=last_ts AND EXISTS
            (SELECT 1 FROM messages m WHERE m.sid=s.id AND m.processed=0) ORDER BY s.last_ts DESC''')
        for s in sessions:
            rows=self.store.query('SELECT * FROM messages WHERE sid=? ORDER BY ts,mid',(s['id'],))
            if not rows:continue
            pending={r['mid'] for r in rows if not r['processed']}
            if not pending:continue
            # 回看范围外的旧消息只标记跳过，不送模型，避免翻出过期安排。
            stale=[(r['mid'],r['hash']) for r in rows if r['mid'] in pending and cutoff and r['ts']<cutoff]
            if stale:
                self.mark_processed(s['id'],stale,skipped=1)
                pending.difference_update(mid for mid,_ in stale)
            if not pending:continue
            order=[(r,json.loads(r['body'])) for r in rows]
            # 尚未转写的语音先交给本地 ASR，分析线程不能把它当空消息直接消化掉。
            blocked={r['mid'] for r,m in order if r['mid'] in pending and self.asr_enabled and r['transcribed']==0 and m.get('kind')=='voice'}
            existing=[x for x in self.store.items(s['id']) if x['status'] not in ('dismissed','cancelled','completed')]
            # 待查看事项对应的邀请之后，我的回复即使不含关键词（如“那不行”“那好吧”）也要送模型判断。
            review_ts=[max((e['ts'] for e in x['evidence'] if e.get('ts')),default=0) for x in existing if x['status']=='review']
            def reply_after_review(m):
                if m['direction']!='2':return False
                return any(0 < m['ts']-rts <= 48*3600*1000 for rts in review_ts)
            # 从最早的待处理候选项（或待查看事项之后的回复）开始。
            candidates=[i for i,(r,m) in enumerate(order) if r['mid'] in pending and r['mid'] not in blocked and (candidate(m) or reply_after_review(m))]
            if not candidates:
                self.mark_processed(s['id'],[(row['mid'],row['hash']) for row,_ in order if row['mid'] in pending and row['mid'] not in blocked])
                continue
            start=candidates[0]
            begin=max(0,start-CONTEXT_BEFORE)
            early=[(r['mid'],r['hash']) for i,(r,m) in enumerate(order) if i<begin and r['mid'] in pending and r['mid'] not in blocked]
            if early:
                self.mark_processed(s['id'],early)
                pending.difference_update(mid for mid,_ in early)
            window=order[begin:min(len(order),begin+BATCH_MESSAGES)]
            msgs=[m for _,m in window]
            fresh=[r['mid'] for r,m in window if r['mid'] in pending and r['mid'] not in blocked]
            if not fresh:continue
            # 把待查看事项的原始证据也带上，模型才能把一句“好的”对应回原安排。
            refs={e['id'] for x in existing[:10] for e in x['evidence'] if x['status']=='review'}
            extra=[m for m in [json.loads(r['body']) for r in rows] if m['id'] in refs and m['id'] not in {m['id'] for m in msgs}][-10:]
            msgs=extra+msgs
            # Max text keeps request cheap; truncated text never qualifies for automatic insertion.
            for m in msgs:
                if len(m['text'])>700:m['text']=m['text'][:700];m['truncated']=True
            self.state['analysis']='正在识别安排与回复（本批 %d 条新消息）'%len(fresh)
            result=self.extract(msgs,fresh,existing,s)
            valid=review_batch(result['items'],msgs,fresh,existing,group=s['kind']==2,on_skip=self.warn)
            # 先全批验证，再落库；取消选择后丢弃在途结果。
            if not self.store.query('SELECT id FROM sessions WHERE id=? AND selected=1',(s['id'],)):return True
            for item in valid:self.store.put_item(s['id'],item)
            self.mark_processed(s['id'],[(r['mid'],r['hash']) for r,m in window if r['mid'] in pending and r['mid'] not in blocked])
            self.state['analysis']='识别完成，新消息到达后继续'
            return True
        self.state['analysis']='所选聊天待处理文字已检查'
        return False

    def analysis_loop(self):
        while not self.stop.is_set():
            self.heartbeat['analysis']=time.time()
            try:
                if self.state.get('fatal'):
                    if '预算' in self.state.get('error','') and self.state.get('error_day')!=self.usage()['day']:
                        self.state['fatal']=False;self.state['error']=''
                    else:
                        self.stop.wait(3);continue
                if time.time()<self.retry_at:
                    self.stop.wait(min(5,max(1,self.retry_at-time.time())));continue
                if self.analyze_once():
                    self.failures=0;self.retry_at=0
                    self.state['error']='';self.state.pop('fatal',None)
                    continue
            except Exception as exc:
                # 临时失败自动退避重试，不把整条分析链路永久卡死。
                message=self.safe_error(exc)
                self.state['error']=message
                self.state['error_day']=self.usage()['day']
                if self.fatal_error(exc,message):
                    self.state['fatal']=True
                    self.state['analysis']='已暂停：'+message
                else:
                    self.failures+=1
                    delay=min(120,5*self.failures)
                    self.retry_at=time.time()+delay
                    self.state['fatal']=False
                    self.state['analysis']='遇到临时问题，%d 秒后自动重试'%delay
            self.wake.wait(3);self.wake.clear()

    def command(self, op, args=None, iid='',revision=0):
        cid=digest([op,iid,revision,args])[:32] if iid else secrets.token_hex(16)
        payload=dict(args or {},id=cid,op=op)
        self.store.execute('INSERT OR IGNORE INTO commands(id,item_id,revision,payload) VALUES(?,?,?,?)',(cid,iid,revision,json.dumps(payload,ensure_ascii=False)))
        return cid

    def bridge_loop(self):
        while not self.stop.is_set():
            self.heartbeat['bridge']=time.time()
            try:
                native=read_json(self.ipc/'status.json',{})
                online=time.time()-native.get('updated',0)<12
                if online and native.get('authorized') and native.get('calendar_id') and self.state['account_ok']:
                    for item in self.store.items():
                        if item['status']!='ready':continue
                        if not self.store.query('SELECT id FROM sessions WHERE id=? AND selected=1',(item['sid'],)):continue
                        session=self.store.query('SELECT name FROM sessions WHERE id=?',(item['sid'],))[0]
                        target=next((x for x in self.store.items(item['sid']) if x['id']==item['target_id']),None)
                        if item['action'] in ('cancel','complete','reschedule','reopen') and (not target or not target['reminder_id']):
                            self.store.execute("UPDATE items SET status='review' WHERE id=?",(item['id'],));continue
                        notes='来自微信 · '+session['name']+'\n'+'\n'.join(datetime.fromtimestamp(e['ts']/1000,TZ).strftime('%Y-%m-%d %H:%M')+' '+('我' if e['direction']=='2' else '对方')+'：'+e['quote'] for e in item['evidence'])
                        self.command('save',{'item_key':target['id'] if target and item['action'] in ('cancel','complete','reschedule','reopen') else item['id'],
                            'title':item['title'],'due':item.get('due'),'notes':notes,'action':item['action'],
                            'reminder_id':target['reminder_id'] if target else item['reminder_id']},item['id'],item['revision'])
                        self.store.execute("UPDATE items SET status='queued' WHERE id=?",(item['id'],))
                for c in self.store.query("SELECT * FROM commands WHERE status='pending'"):
                    response=self.ipc/'responses'/(c['id']+'.json')
                    if response.exists():
                        r=read_json(response,{})
                        if r.get('ok'):
                            with self.store.db() as db:
                                db.execute("UPDATE commands SET status='done' WHERE id=?",(c['id'],))
                                if c['item_id']:
                                    action=json.loads(c['payload']).get('action')
                                    status={'cancel':'cancelled','complete':'completed','reopen':'synced'}.get(action,'synced')
                                    db.execute('UPDATE items SET status=?,reminder_id=? WHERE id=? AND revision=?',(status,r.get('reminder_id',''),c['item_id'],c['revision']))
                                    item=db.execute('SELECT body FROM items WHERE id=?',(c['item_id'],)).fetchone()
                                    if item and action in ('cancel','complete','reschedule','reopen'):
                                        b=json.loads(item[0]);target=b.get('target_id')
                                        if target:
                                            if action=='reschedule':
                                                old=db.execute('SELECT body FROM items WHERE id=?',(target,)).fetchone()
                                                if old:
                                                    updated=json.loads(old[0]);updated['due']=b.get('due');updated['title']=b['title']
                                                    db.execute('UPDATE items SET body=? WHERE id=?',(json.dumps(updated,ensure_ascii=False),target))
                                            else:db.execute('UPDATE items SET status=? WHERE id=?',(status,target))
                        else:
                            self.store.execute("UPDATE commands SET status='error',error=? WHERE id=?",(r.get('error','提醒事项写入失败'),c['id']))
                            if c['item_id']:self.store.execute("UPDATE items SET status='sync_error' WHERE id=?",(c['item_id'],))
                        (self.ipc/'requests'/(c['id']+'.json')).unlink(missing_ok=True)
                        response.unlink(missing_ok=True)
                    elif not (self.ipc/'requests'/(c['id']+'.json')).exists():
                        # 待确认期间取消勾选，不发送尚未提交的写入。
                        if c['item_id'] and not self.store.query('SELECT i.id FROM items i JOIN sessions s ON s.id=i.sid WHERE i.id=? AND s.selected=1',(c['item_id'],)):continue
                        atomic(self.ipc/'requests'/(c['id']+'.json'),json.loads(c['payload']))
                # 系统内勾选完成后回流本地状态；不覆盖用户刚刚在客户端做的完成/恢复操作。
                for reminder in native.get('items',[]):
                    if not reminder.get('completed'):continue
                    key=reminder.get('item_key')
                    rows=self.store.query('SELECT body FROM items WHERE id=?',(key,))
                    if not rows:continue
                    try:manual=float(json.loads(rows[0]['body']).get('manual_at') or 0)
                    except (ValueError,TypeError):manual=0
                    if time.time()-manual<120:continue
                    self.store.execute("UPDATE items SET status='completed' WHERE id=? AND status='synced'",(key,))
                self.write_menu()
            except Exception as exc:
                self.state['bridge_error']=self.safe_error(exc)
            self.stop.wait(1)

    def alpha_bytes(self, path):
        url=path if path.startswith('http') else ALPHA+path
        req=urllib.request.Request(url,headers={'Authorization':'Bearer '+self.keys.get('ALPHA_TOKEN','')})
        with urllib.request.urlopen(req,timeout=90) as r:
            return r.read()

    def media_url(self, row):
        """按 seq 单条取回语音消息的媒体地址；Alpha 整页 media=true 容易 422。"""
        seq=int(row['seq'] or 0)
        for off in (seq,seq-1,seq+1,seq-2,seq+2):
            if off<0:continue
            try:
                r=self.alpha('/api/v1/messages?'+urllib.parse.urlencode(
                    {'talker':row['sid'],'limit':1,'offset':off,'media':'true'}))
            except Exception:
                continue
            for raw in r.get('messages') or []:
                mid=str(raw.get('platform_message_id') or raw.get('id') or '')
                if mid!=str(row['mid']):continue
                resources=((raw.get('media') or {}).get('resources') or [])
                for item in resources:
                    mime=(item.get('mimeType') or '').lower()
                    if 'silk' in mime or 'audio' in mime or 'voice' in mime:
                        return item.get('url')
                if resources:return resources[0].get('url')
        return None

    def mark_voice_failed(self, row):
        self.store.execute('UPDATE messages SET transcribed=-1 WHERE sid=? AND mid=?',(row['sid'],row['mid']))

    def transcribe_voices(self, rows):
        """本地 Qwen3-ASR 在独立进程里解码，完成后释放内存；转写文本再交给分析。"""
        if not self.asr_enabled or not rows:return 0
        tmp=pathlib.Path(tempfile.mkdtemp(prefix='shishi-asr-'))
        work={}
        try:
            for index,row in enumerate(rows):
                try:
                    url=row['voice_url'] or self.media_url(row)
                    if not url:
                        self.mark_voice_failed(row);continue
                    sep='&' if '?' in url else '?'
                    data=self.alpha_bytes(url+sep+'format=wav')
                    path=tmp/(str(index)+'.wav');path.write_bytes(data)
                    work[str(path)]=dict(row,voice_url=url)
                except Exception:
                    self.mark_voice_failed(row)
            if not work:return 0
            env=dict(os.environ);env['SHISHI_ASR_MODEL']=str(ASR_MODEL_DIR)
            proc=subprocess.run([sys.executable,str(ASR_SCRIPT),*work.keys()],capture_output=True,timeout=240,env=env)
            try:
                texts=json.loads(proc.stdout.decode('utf-8','replace') or '{}')
            except ValueError:
                texts={}
            done=0
            for path,row in work.items():
                text=str(texts.get(path) or '').strip()
                if not text:
                    self.mark_voice_failed(row);continue
                try:body=json.loads(row['body'])
                except ValueError:body={'id':row['mid'],'ts':row['ts'],'direction':'0','kind':'voice','truncated':False}
                body['text']=text;body['transcribed']=True;body['truncated']=False
                self.store.execute('UPDATE messages SET body=?,transcribed=1,voice_url=?,processed=0,skipped=0 WHERE sid=? AND mid=? AND hash=?',
                    (json.dumps(body,ensure_ascii=False),row['voice_url'],row['sid'],row['mid'],row['hash']))
                done+=1
            if done:self.wake.set()
            return done
        finally:
            shutil.rmtree(tmp,ignore_errors=True)

    def asr_once(self):
        if not self.asr_enabled:return False
        cutoff=self.effective_cutoff()
        rows=self.store.query('''SELECT m.* FROM messages m JOIN sessions s ON s.id=m.sid
            WHERE s.selected=1 AND m.transcribed=0 AND json_extract(m.body,'$.kind')='voice' AND m.ts>=?
            ORDER BY m.ts DESC LIMIT ?''',(cutoff,ASR_BATCH))
        if not rows:return False
        return self.transcribe_voices(rows)>0

    def asr_loop(self):
        while not self.stop.is_set():
            self.heartbeat['asr']=time.time()
            try:
                if self.asr_enabled and self.state['account_ok'] and not self.store.setting('paused',False):
                    self.asr_once()
            except Exception as exc:
                self.asr_error=self.safe_error(exc)
            self.stop.wait(4)

    def menu_snapshot(self):
        """菜单栏只列待查看项目，与仪表盘待查看范围一致。"""
        cutoff=self.effective_cutoff()
        selected={r['id'] for r in self.store.query('SELECT id FROM sessions WHERE selected=1')}
        rows=[]
        for x in self.store.items():
            if x['sid'] not in selected:continue
            if x['status'] not in ('review','sync_error'):continue
            if cutoff and x['status'] in ('review','sync_error') and all((e.get('ts') or 0)<cutoff for e in (x['evidence'] or [])):continue
            rows.append(x)
        rank={'ready':0,'queued':0,'synced':0,'review':1,'sync_error':1}
        rows.sort(key=lambda x:(rank.get(x['status'],2),-(x.get('updated') or 0)))
        review=sum(1 for x in rows if x['status'] in ('review','sync_error'))
        return {'updated':time.time(),'pending':len(rows),'review':review,'active':len(rows)-review,
            'items':[{'id':x['id'],'title':x['title'],'status':x['status']} for x in rows[:10]]}

    def write_menu(self):
        payload=self.menu_snapshot()
        core=json.dumps({'pending':payload['pending'],'review':payload['review'],'active':payload['active'],'items':payload['items']},
            ensure_ascii=False,sort_keys=True)
        sig=hashlib.sha256(core.encode()).hexdigest()
        if sig==self.last_menu_sig:return
        self.last_menu_sig=sig
        atomic(self.ipc/'menu.json',payload)

    def snapshot(self):
        # 公众号不进入聊天选择列表，直接从快照里排除（数据库仍保留，便于排查）。
        sessions=self.store.query('SELECT * FROM sessions WHERE '+VISIBLE_SESSIONS+' ORDER BY selected DESC,last_ts DESC')
        counts=self.store.query('SELECT count(*) n,sum(processed=0) pending,sum(processed=1 AND skipped=1) skipped FROM messages m JOIN sessions s ON s.id=m.sid WHERE s.selected=1')[0]
        unsupported=self.store.query("SELECT count(*) n FROM messages m JOIN sessions s ON s.id=m.sid WHERE s.selected=1 AND json_extract(body,'$.kind')<>'text' AND m.transcribed<1")[0]['n']
        transcribed=self.store.query("SELECT count(*) n FROM messages m JOIN sessions s ON s.id=m.sid WHERE s.selected=1 AND m.transcribed=1")[0]['n']
        cutoff=self.effective_cutoff()
        older=self.store.query('SELECT count(*) n FROM messages m JOIN sessions s ON s.id=m.sid WHERE s.selected=1 AND m.ts<?',(cutoff,))[0]['n'] if cutoff else 0
        native=read_json(self.ipc/'status.json',{})
        native['online']=time.time()-native.get('updated',0)<12
        return {'version':VERSION,'state':self.state.copy(),'sessions':sessions,'items':self.store.items(),
            'counts':dict(counts,media=unsupported,transcribed=transcribed),'usage':self.usage(),'budget':self.store.setting('daily_budget',30000),
            'paused':self.store.setting('paused',False),'native':native,
            'lookback':self.lookback_days(),'older':older,
            'asr':{'enabled':self.asr_enabled,'error':self.asr_error},
            'new_only':bool(self.store.setting('new_only',True)),'cutoff':cutoff,
            'since_date':self.store.setting('since_date',datetime.now(TZ).date().isoformat()),
            'bridge_errors':self.store.query("SELECT error FROM commands WHERE status='error' ORDER BY rowid DESC LIMIT 3")}

    def action(self, path, d):
        if path=='/api/select':
            ids=d.get('ids')
            if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids):raise ValueError('会话列表格式错误')
            with self.store.db() as db:
                db.execute('UPDATE sessions SET selected=0')
                # 公众号不再出现在选择列表里，即使传入也不参与分析。
                db.executemany('UPDATE sessions SET selected=1 WHERE id=? AND '+VISIBLE_SESSIONS,[(i,) for i in ids])
            for sid in ids:self.dirty.put(sid)
            self.wake.set()
        elif path=='/api/item':
            rows=self.store.query('SELECT * FROM items WHERE id=?',(d.get('id'),))
            if not rows:raise ValueError('事项不存在')
            item=rows[0];op=d.get('action')
            if op in ('complete','reopen'):
                # 已同步/已完成的事项可以直接勾选或取消勾选，交由原生桥接改写提醒。
                if item['status'] not in ('synced','completed'):raise ValueError('只有已同步或已完成的事项可以这样标记')
                body=json.loads(item['body'])
                body['action']='complete' if op=='complete' else 'reopen'
                body['target_id']=item['id']
                body['manual_at']=time.time()
                self.store.execute("UPDATE items SET body=?,status='ready',revision=revision+1 WHERE id=?",
                    (json.dumps(body,ensure_ascii=False),item['id']))
            elif item['status'] not in ('review','sync_error'):raise ValueError('此事项当前不能操作')
            elif op=='dismiss':
                self.store.execute("UPDATE items SET status='dismissed' WHERE id=?",(item['id'],))
            elif op=='approve':
                body=json.loads(item['body'])
                if 'title' in d:
                    if not isinstance(d['title'],str) or not 2<=len(d['title'].strip())<=120:raise ValueError('请填写 2–120 字标题')
                    body['title']=d['title'].strip()
                if 'due' in d:
                    from core import parse_due
                    parse_due(d['due']);body['due']=d['due'] or None
                self.store.execute("UPDATE items SET body=?,status='ready',revision=revision+1 WHERE id=?",(json.dumps(body,ensure_ascii=False),item['id']))
            else:raise ValueError('未知操作')
        elif path=='/api/native':
            op=d.get('op')
            if op not in ('authorize','choose','refresh'):raise ValueError('未知原生操作')
            self.command(op,{'calendar_id':str(d.get('calendar_id',''))})
        elif path=='/api/settings':
            old_cutoff=self.effective_cutoff()
            if 'paused' in d:self.store.set_setting('paused',bool(d['paused']))
            if 'budget' in d:
                budget=int(d['budget'])
                if not 5000<=budget<=1000000:raise ValueError('每日预算范围 5,000–1,000,000 token')
                self.store.set_setting('daily_budget',budget)
            if 'lookback_days' in d:
                try:days=int(d['lookback_days'])
                except (TypeError,ValueError):raise ValueError('回看范围无效')
                if days not in (0,7,30,90,365):raise ValueError('回看范围无效')
                self.store.set_setting('lookback_days',days)
            if 'new_only' in d:
                if not isinstance(d['new_only'],bool):raise ValueError('仅记录新消息参数无效')
                self.store.set_setting('new_only',d['new_only'])
                if d['new_only'] and not self.store.setting('since_date',''):
                    self.store.set_setting('since_date',datetime.now(TZ).date().isoformat())
            if 'since_date' in d:
                value=str(d.get('since_date') or '').strip()
                try:day=datetime.strptime(value,'%Y-%m-%d').date()
                except ValueError:raise ValueError('起始日期格式应为 2026-10-21')
                today=datetime.now(TZ).date()
                if day>today:raise ValueError('起始日期不能晚于今天')
                if day.year<2020:raise ValueError('起始日期太早')
                self.store.set_setting('since_date',day.isoformat())
            new_cutoff=self.effective_cutoff()
            if new_cutoff!=old_cutoff:
                # 日期判断放宽后，把此前跳过、从未分析过的消息重新纳入分析。
                self.reset_skipped(new_cutoff,old_cutoff)
                self.keys=credentials();self.state['error']='';self.state.pop('fatal',None)
                self.retry_at=0;self.failures=0
            if d.get('retry'):
                self.keys=credentials();self.state['error']='';self.state.pop('fatal',None)
                self.retry_at=0;self.failures=0
            self.wake.set()
        else:raise ValueError('未知接口')
        return {'ok':True}

    def start(self):
        def guard(fn):
            def run():
                try:fn()
                except Exception as exc:self.thread_error=type(exc).__name__+': '+str(exc)[:200]
            return run
        for fn in (self.sync_loop,self.push_loop,self.analysis_loop,self.bridge_loop,self.asr_loop):
            threading.Thread(target=guard(fn),name=fn.__name__,daemon=True).start()

    def debug(self):
        return {'version':VERSION,'state':self.state.copy(),'heartbeat':self.heartbeat,'now':time.time(),
            'failures':self.failures,'retry_at':self.retry_at,'thread_error':self.thread_error,
            'threads':[{'name':t.name,'alive':t.is_alive()} for t in threading.enumerate()],
            'lookback':self.lookback_days(),'account':self.account}


def handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,body,ctype='application/json',cache='no-store'):
            if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type',ctype+('; charset=utf-8' if ctype.startswith('text/') or ctype=='application/json' else ''))
            self.send_header('Cache-Control',cache);self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def trusted(self):
            return self.headers.get('Host') in ('127.0.0.1:'+str(PORT),'localhost:'+str(PORT))
        def do_GET(self):
            if not self.trusted():return self.reply(403,{'error':'非法来源'})
            parsed=urllib.parse.urlparse(self.path)
            if parsed.path=='/api/avatar':
                sid=urllib.parse.parse_qs(parsed.query).get('sid',[''])[0]
                got=engine.avatar(sid) if sid else None
                if not got:return self.reply(404,b'','image/png')
                data,mime=got
                return self.reply(200,data,mime,'public, max-age=86400')
            if parsed.path=='/api/state':return self.reply(200,engine.snapshot())
            if parsed.path=='/api/health':return self.reply(200,{'ok':True,'app':'shishi','version':VERSION})
            if parsed.path=='/api/debug':return self.reply(200,engine.debug())
            if self.path=='/':
                return self.reply(200,(ROOT/'index.html').read_text().replace('__CSRF__',engine.csrf).encode(),'text/html')
            self.reply(404,{'error':'未找到'})
        def do_POST(self):
            if not self.trusted() or self.headers.get('X-Shishi-Token')!=engine.csrf:return self.reply(403,{'error':'请求校验失败'})
            origin=self.headers.get('Origin')
            if origin and origin not in ('http://127.0.0.1:'+str(PORT),'http://localhost:'+str(PORT)):return self.reply(403,{'error':'非法来源'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<100000:raise ValueError('请求过大或为空')
                d=json.loads(self.rfile.read(size))
                if not isinstance(d,dict):raise ValueError('请求格式错误')
                self.reply(200,engine.action(self.path,d))
            except (ValueError,TypeError) as exc:self.reply(400,{'error':str(exc)})
            except Exception:self.reply(500,{'error':'处理失败，请重试'})
    return Handler


if __name__=='__main__':
    os.umask(0o077)
    DATA.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=open(DATA/'server.lock','a+')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('拾事后台已在运行')
    pidfile=DATA/'server.pid'
    pidfile.write_text(str(os.getpid()))
    engine=Engine()
    http=ThreadingHTTPServer(('127.0.0.1',PORT),handler(engine))
    engine.start()
    print('拾事已启动 http://127.0.0.1:'+str(PORT),flush=True)
    try:http.serve_forever()
    finally:
        engine.stop.set();http.server_close()
        pidfile.unlink(missing_ok=True)
