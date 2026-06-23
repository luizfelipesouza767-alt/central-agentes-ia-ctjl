const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const url = require('url');
const { spawn } = require('child_process');
const crypto = require('crypto');

const PORT = process.env.PORT || 3000;
const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_KEY;
const HTML_FILE = path.join(__dirname, 'index.html');
const PYTHON_SCRIPT = path.join(__dirname, 'agentes_voibi.py');

if (!SUPABASE_URL || !SUPABASE_KEY) {
  console.error('ERRO: Defina as variáveis de ambiente SUPABASE_URL e SUPABASE_KEY.');
  process.exit(1);
}

const sessions = new Map();

function sha256(t) { return crypto.createHash('sha256').update(t).digest('hex'); }
function token() { return crypto.randomBytes(32).toString('hex'); }
function getSession(req) { return sessions.get((req.headers['authorization']||'').replace('Bearer ','').trim()) || null; }

function parseBody(req) {
  return new Promise(resolve => {
    let b = '';
    req.on('data', c => b += c);
    req.on('end', () => { try { resolve(JSON.parse(b||'{}')); } catch { resolve({}); } });
  });
}

function sbReq(method, table, params, body, prefer, cb) {
  if (typeof prefer === 'function') { cb = prefer; prefer = 'return=minimal'; }
  const target = url.parse(`${SUPABASE_URL}/rest/v1/${table}?${params||''}`);
  const hdrs = {
    'apikey': SUPABASE_KEY,
    'Authorization': `Bearer ${SUPABASE_KEY}`,
    'Content-Type': 'application/json',
    'Prefer': prefer || 'return=minimal'
  };
  const payload = body ? (typeof body === 'string' ? body : JSON.stringify(body)) : null;
  const r = https.request({ hostname: target.hostname, path: target.path, method, headers: hdrs }, res => {
    let d = '';
    res.on('data', c => d += c);
    res.on('end', () => cb(null, res.statusCode, d));
  });
  r.on('error', e => cb(e));
  if (payload) r.write(payload);
  r.end();
}

function j(res, code, data) {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(JSON.stringify(data));
}

http.createServer(async (req, res) => {
  const parsed = url.parse(req.url, true);
  const p = parsed.pathname;
  const params = parsed.search ? parsed.search.slice(1) : '';

  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PATCH,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type,Authorization');
  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  if ((p === '/' || p === '/index.html') && req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    fs.createReadStream(HTML_FILE).pipe(res);
    return;
  }

  if (p === '/cadastrar' && req.method === 'POST') {
    const { nome, email, senha, pode_varredura: pv, is_admin: ia } = await parseBody(req);
    if (!nome || !email || !senha) { j(res, 400, { erro: 'Nome, email e senha obrigatórios' }); return; }
    sbReq('GET', 'usuarios', 'select=id&limit=1', null, (err, st, d) => {
      const existentes = JSON.parse(d||'[]');
      const primeiro = existentes.length === 0;
      if (!primeiro) {
        const sess = getSession(req);
        if (!sess || !sess.is_admin) { j(res, 403, { erro: 'Somente administradores podem cadastrar usuários' }); return; }
      }
      const pode_varredura = primeiro ? true : (pv === true || pv === 'true');
      const is_admin = primeiro ? true : (ia === true || ia === 'true');
      const novo = { nome, email, senha: sha256(senha), pode_varredura, is_admin };
      sbReq('POST', 'usuarios', '', JSON.stringify(novo), 'return=representation', (err2, st2, d2) => {
        if (st2 >= 400) {
          const e = JSON.parse(d2||'{}');
          j(res, e.code === '23505' ? 409 : 400, { erro: e.code === '23505' ? 'Email já cadastrado' : (e.message || 'Erro ao cadastrar') });
          return;
        }
        const criado = JSON.parse(d2||'[]')[0] || {};
        if (primeiro) {
          const tk = token();
          const sess2 = { id: criado.id, nome, email, pode_varredura, is_admin };
          sessions.set(tk, sess2);
          j(res, 201, { token: tk, user: sess2 });
        } else {
          j(res, 201, { user: { id: criado.id, nome, email, pode_varredura, is_admin } });
        }
      });
    });
    return;
  }

  if (p === '/login' && req.method === 'POST') {
    const { email, senha } = await parseBody(req);
    if (!email || !senha) { j(res, 400, { erro: 'Email e senha obrigatórios' }); return; }
    const q = `email=eq.${encodeURIComponent(email)}&senha=eq.${sha256(senha)}&select=id,nome,email,pode_varredura,is_admin`;
    sbReq('GET', 'usuarios', q, null, (err, st, d) => {
      const users = JSON.parse(d||'[]');
      if (!users.length) { j(res, 401, { erro: 'Email ou senha incorretos' }); return; }
      const tk = token();
      sessions.set(tk, users[0]);
      j(res, 200, { token: tk, user: users[0] });
    });
    return;
  }

  if (p === '/me' && req.method === 'GET') {
    const sess = getSession(req);
    if (!sess) { j(res, 401, { erro: 'Não autenticado' }); return; }
    j(res, 200, sess);
    return;
  }

  if (p === '/rodar-varredura' && req.method === 'POST') {
    const sess = getSession(req);
    if (!sess) { j(res, 401, { erro: 'Não autenticado' }); return; }
    if (!sess.pode_varredura) { j(res, 403, { erro: 'Sem permissão para rodar varredura' }); return; }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    const proc = spawn('python3', [PYTHON_SCRIPT]);
    let out = '';
    proc.stdout.on('data', d => out += d);
    proc.stderr.on('data', d => out += d);
    proc.on('close', code => res.end(JSON.stringify({ ok: code === 0, output: out })));
    return;
  }

  if (p === '/usuarios' && req.method === 'GET') {
    const sess = getSession(req);
    if (!sess || !sess.is_admin) { j(res, 403, { erro: 'Sem permissão' }); return; }
    sbReq('GET', 'usuarios', 'select=id,nome,email,pode_varredura,is_admin,created_at&order=created_at.asc', null, (err, st, d) => {
      res.writeHead(st, { 'Content-Type': 'application/json' });
      res.end(d);
    });
    return;
  }

  const userPatch = p.match(/^\/usuarios\/(\d+)$/);
  if (userPatch && req.method === 'PATCH') {
    const sess = getSession(req);
    if (!sess || !sess.is_admin) { j(res, 403, { erro: 'Sem permissão' }); return; }
    const body = await parseBody(req);
    sbReq('PATCH', 'usuarios', `id=eq.${userPatch[1]}`, JSON.stringify(body), (err, st, d) => {
      res.writeHead(st || 200, { 'Content-Type': 'application/json' });
      res.end(d || '{}');
    });
    return;
  }

  const apiMatch = p.match(/^\/api\/([^/]+)$/);
  if (apiMatch) {
    const sess = getSession(req);
    if (!sess) { j(res, 401, { erro: 'Não autenticado' }); return; }
    const table = apiMatch[1];
    if (req.method === 'GET') {
      sbReq('GET', table, params, null, (err, st, d) => {
        res.writeHead(st, { 'Content-Type': 'application/json' });
        res.end(d);
      });
    } else if (req.method === 'PATCH') {
      const body = await parseBody(req);
      sbReq('PATCH', table, params, JSON.stringify(body), (err, st, d) => {
        res.writeHead(st || 200, { 'Content-Type': 'application/json' });
        res.end(d || '{}');
      });
    }
    return;
  }

  res.writeHead(404); res.end('Not found');

}).listen(PORT, () => console.log(`Central de Agentes rodando na porta ${PORT}`));
