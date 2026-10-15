"""Runtime-owned, revocable Job MCP connection grants and OAuth state."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
from pydantic import AnyHttpUrl

from mn_api.path_utils import resolve_mn_home


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def valid_server_url(value, job_id):
    if any(ord(char) <= 32 for char in value):
        raise ValueError('Use an MCP URL without whitespace or control characters.')
    value = str(AnyHttpUrl(value))
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or parsed.path != f'/api/v1/jobs/{job_id}/mcp'
        or (parsed.scheme != 'https' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'})):
        raise ValueError('Use an HTTPS MCP URL or a local loopback URL for this Job.')
    return value.rstrip('/')


class ConnectionStore:
    def __init__(self, path=None):
        self.path = Path(path or resolve_mn_home() / 'mcp_connections' / 'connections.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS policies (job_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, generation INTEGER NOT NULL, key_hash TEXT, server_url TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS grants (key_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, job_id TEXT, generation INTEGER, expires REAL NOT NULL, data TEXT NOT NULL)')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def policy(self, job_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM policies WHERE job_id=?', (job_id,)).fetchone()
        return dict(row) if row else {'job_id': job_id, 'enabled': False, 'generation': 0, 'key_hash': None, 'server_url': ''}

    def configure(self, job_id, enabled, server_url):
        server_url = valid_server_url(server_url, job_id)
        key = secrets.token_urlsafe(32) if enabled else None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO policies VALUES (?,?,1,?,?) ON CONFLICT(job_id) DO UPDATE SET enabled=excluded.enabled,generation=generation+1,key_hash=excluded.key_hash,server_url=excluded.server_url',
                       (job_id, int(enabled), digest(key) if key else None, server_url))
            db.execute("DELETE FROM grants WHERE job_id=? AND kind!='internal'", (job_id,))
        return key

    def issue(self, kind, *, job_id=None, generation=0, lifetime=3600, data=None):
        key = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM grants WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO grants VALUES (?,?,?,?,?,?)', (digest(key), kind, job_id, generation, time.time()+lifetime, json.dumps(data or {})))
        return key

    def grant(self, key, kind=None, *, consume=False):
        if not isinstance(key, str) or not 16 <= len(key) <= 256:
            return None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM grants WHERE key_hash=? AND expires>?', (digest(key), time.time())).fetchone()
            if row is None or kind is not None and row['kind'] != kind:
                return None
            record = dict(row)
            record['data'] = json.loads(record['data'])
            if consume:
                db.execute('DELETE FROM grants WHERE key_hash=?', (digest(key),))
        if record['job_id'] and record['kind'] != 'internal':
            policy = self.policy(record['job_id'])
            if not policy['enabled'] or policy['generation'] != record['generation']:
                return None
        return record

    def revoke(self, key):
        with self.db() as db:
            db.execute('DELETE FROM grants WHERE key_hash=?', (digest(key),))

    def authorize(self, job_id, key):
        return self.authority(job_id, key) is not None

    def authority(self, job_id, key):
        grant = self.grant(key)
        if grant and grant['kind'] in {'internal', 'access'} and grant['job_id'] == job_id:
            if grant['kind'] == 'internal':
                return 'internal'
            if 'co_worker:supervise' in grant['data'].get('scopes', []):
                return 'external'
        policy = self.policy(job_id)
        if key and policy['enabled'] and policy['key_hash'] and secrets.compare_digest(digest(key), policy['key_hash']):
            return 'external'
        return None

    def put(self, key, kind, *, job_id=None, generation=0, lifetime=3600, data=None):
        with self.db() as db:
            db.execute('DELETE FROM grants WHERE expires<?', (time.time(),))
            db.execute('INSERT OR REPLACE INTO grants VALUES (?,?,?,?,?,?)', (digest(key), kind, job_id, generation, time.time()+lifetime, json.dumps(data or {})))

    def policy_for_resource(self, resource):
        with self.db() as db:
            row = db.execute('SELECT * FROM policies WHERE server_url=? AND enabled=1', (resource,)).fetchone()
        return dict(row) if row else None
