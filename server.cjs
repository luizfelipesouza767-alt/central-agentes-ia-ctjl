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

// ── Supabase em Promise ──
function sbGet(table, params) {
  return new Promise((resolve, reject) => {
    sbReq('GET', table, params, null, (err, st, d) => {
      if (err) return reject(err);
      try { resolve(JSON.parse(d || '[]')); } catch { resolve([]); }
    });
  });
}
function sbPatch(table, params, body) {
  return new Promise((resolve, reject) => {
    sbReq('PATCH', table, params, JSON.stringify(body), (err, st) => {
      if (err || st >= 400) return reject(err || new Error('PATCH status ' + st));
      resolve(true);
    });
  });
}

// Log de sistema (fire-and-forget)
function logSistema(tipo, usuario, detalhe) {
  sbReq('POST', 'logs_sistema', '', JSON.stringify({ tipo, usuario: usuario || null, detalhe: detalhe || null }), () => {});
}

// Traduz erro tecnico para linguagem simples
function erroAmigavel(linha) {
  const t = linha.replace(/^ERRO\s*/i, '').trim();
  if (/nao persistiu|não persistiu/i.test(t)) return 'O Voibi não salvou a tarefa criada (limitação atual da API do Voibi).';
  if (/sem responsável|sem responsavel/i.test(t)) return 'Tarefa sem responsável definido.';
  if (/etiqueta/i.test(t) && /existe/i.test(t)) return 'A etiqueta de revisão não existe no Voibi.';
  if (/404|not found/i.test(t)) return 'Registro não encontrado no Voibi.';
  if (/voibi/i.test(t)) return 'Não foi possível concluir uma ação no Voibi.';
  return t;
}

// ── Voibi (execucao da aprovacao direto em Node, sem Python) ──
const VOIBI_BASE = process.env.VOIBI_BASE_URL || 'https://chat.voibi.com.br';
const VOIBI_KEY = process.env.VOIBI_API_KEY_EMPRESA || '';
const VOIBI_TAG_REVISAO = process.env.VOIBI_TAG_REVISAO || 'Revisão CRM';
const VOIBI_DEFAULT_USER_ID = process.env.VOIBI_DEFAULT_USER_ID || '';

function voibiReq(method, apiPath, body) {
  return new Promise((resolve, reject) => {
    if (!VOIBI_KEY) return reject(new Error('VOIBI_API_KEY_EMPRESA ausente'));
    const target = url.parse(VOIBI_BASE + apiPath);
    const payload = body ? JSON.stringify(body) : null;
    const r = https.request({
      hostname: target.hostname, path: target.path, method,
      headers: { 'Authorization': `Bearer ${VOIBI_KEY}`, 'Content-Type': 'application/json' }
    }, res => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => {
        if (res.statusCode >= 400) return reject(new Error(`${method} ${apiPath} -> ${res.statusCode}: ${d}`));
        try { resolve(d ? JSON.parse(d) : {}); } catch { resolve({}); }
      });
    });
    r.on('error', reject);
    if (payload) r.write(payload);
    r.end();
  });
}

async function executarItemNode(id) {
  const log = [];
  const rows = await sbGet('central_aprovacao', `id=eq.${id}&select=id,agente,acoes_json,executado`);
  const item = rows[0];
  if (!item) return { ok: false, log: ['Item não encontrado'] };
  if (item.executado) return { ok: true, log: ['Item já estava executado'] };

  let acoes = {};
  try { acoes = typeof item.acoes_json === 'string' ? JSON.parse(item.acoes_json) : (item.acoes_json || {}); } catch { acoes = {}; }
  const agente = item.agente;

  async function criarTarefa(t) {
    const assigned = t.assigned_user_id || VOIBI_DEFAULT_USER_ID;
    if (!assigned) { log.push('ERRO tarefa sem responsável: ' + (t.title || t.tarefa || '')); return; }
    const payload = {
      title: t.title || t.tarefa || 'Tarefa',
      description: t.description || '',
      assigned_user_id: assigned,
      priority: t.priority || 'medium'
    };
    if (t.deal_id) payload.deal_id = t.deal_id;
    if (t.due_date || t.prazo) payload.due_date = t.due_date || t.prazo;
    const resp = await voibiReq('POST', '/api/v1/tasks', payload);
    const novoId = resp && resp.data && resp.data.id;
    // O POST /tasks do Voibi tem um bug: retorna sucesso mas as vezes a tarefa
    // nao persiste. Confirmamos buscando pelo id retornado.
    let persistiu = false;
    if (novoId) {
      try { await voibiReq('GET', '/api/v1/tasks/' + novoId); persistiu = true; } catch (e) { persistiu = false; }
    }
    if (persistiu) log.push('ok tarefa criada: ' + payload.title);
    else log.push('ERRO Voibi nao persistiu a tarefa "' + payload.title + '" (bug da API de criar tarefa; reportar ao suporte Voibi)');
  }

  try {
    if (agente === 'Analista Comercial') {
      for (const t of (acoes.tarefas || [])) await criarTarefa(t);
    } else if (agente === 'Gestor de Tarefas') {
      for (const t of (acoes.tarefas_criar || [])) await criarTarefa(t);
      for (const t of (acoes.tarefas_atualizar || [])) {
        if (!t.id) continue;
        const dados = {};
        if (t.status) dados.status = t.status;
        if (t.priority) dados.priority = t.priority;
        if (Object.keys(dados).length) {
          await voibiReq('PUT', '/api/v1/tasks/' + t.id, dados);
          log.push('ok tarefa ' + t.id + ' atualizada');
        }
      }
    } else if (agente === 'Qualidade CRM') {
      const tagsResp = await voibiReq('GET', '/api/v1/tags');
      const tag = (tagsResp.tags || []).find(x => (x.name || '').trim().toLowerCase() === VOIBI_TAG_REVISAO.trim().toLowerCase());
      if (!tag) {
        log.push('ERRO etiqueta "' + VOIBI_TAG_REVISAO + '" não existe no Voibi');
      } else {
        for (const c of (acoes.contatos_revisar || [])) {
          if (c.contact_id) {
            await voibiReq('POST', '/api/v1/contacts/tags', { contact_id: c.contact_id, tag_ids: [tag.id] });
            log.push('ok contato ' + c.contact_id + ' etiquetado');
          }
        }
      }
    } else {
      log.push('Agente desconhecido: ' + agente);
    }
  } catch (e) {
    log.push('ERRO Voibi: ' + e.message);
  }

  await sbPatch('central_aprovacao', `id=eq.${id}`, { executado: true });
  log.push('Item ' + id + ' marcado como executado.');
  return { ok: !log.some(l => l.startsWith('ERRO')), log };
}

// ── Varredura em Node (gera sugestoes sem depender de Python no painel) ──
const ANTHROPIC_KEY = process.env.ANTHROPIC_KEY || '';
const CLAUDE_MODEL = 'claude-sonnet-4-6';

function chamarClaude(prompt) {
  return new Promise((resolve, reject) => {
    if (!ANTHROPIC_KEY) return reject(new Error('ANTHROPIC_KEY ausente'));
    const payload = JSON.stringify({ model: CLAUDE_MODEL, max_tokens: 2048, messages: [{ role: 'user', content: prompt }] });
    const r = https.request({ hostname: 'api.anthropic.com', path: '/v1/messages', method: 'POST',
      headers: { 'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json' } }, res => {
      let d = ''; res.on('data', c => d += c);
      res.on('end', () => {
        try { const jd = JSON.parse(d); if (jd.content && jd.content[0]) resolve(jd.content[0].text); else reject(new Error('resposta Claude inesperada')); }
        catch (e) { reject(e); }
      });
    });
    r.on('error', reject); r.write(payload); r.end();
  });
}

function extrairAcoesJson(texto) {
  const m = texto.match(/ACOES_JSON:\s*```json\s*([\s\S]*?)\s*```/);
  if (m) { try { return JSON.parse(m[1]); } catch { return {}; } }
  return {};
}

function inserirSugestao(agente, sugestao, acoes) {
  return new Promise(resolve => {
    sbReq('POST', 'central_aprovacao', '', JSON.stringify({
      agente, sugestao, acoes_json: JSON.stringify(acoes), status: 'Pendente', executado: false
    }), () => resolve());
  });
}

async function contarDealsPorColuna() {
  const out = {};
  const data = await voibiReq('GET', '/api/v1/columns');
  for (const pipe of (data.pipelines || [])) {
    out[pipe.name] = {};
    for (const col of (pipe.columns || [])) {
      try { const r = await voibiReq('GET', `/api/v1/deals?column_id=${col.id}&limit=1`); out[pipe.name][col.name] = r.total || (r.pagination && r.pagination.total) || 0; }
      catch { out[pipe.name][col.name] = 0; }
    }
  }
  return out;
}

async function descobrirUsuarios() {
  const mp = {};
  const colher = rows => { for (const x of rows) {
    if (x.assigned_user_id && x.assigned_user_name) mp[x.assigned_user_name] = x.assigned_user_id;
    if (x.created_by_user_id && x.created_by_user_name) mp[x.created_by_user_name] = x.created_by_user_id;
  } };
  try { colher((await voibiReq('GET', '/api/v1/tasks?limit=100')).data || []); } catch {}
  try { colher((await voibiReq('GET', '/api/v1/deals?limit=100')).data || []); } catch {}
  return mp;
}

async function agenteAnalista() {
  const deals = (await voibiReq('GET', '/api/v1/deals?limit=50')).data || [];
  const contatos = (await voibiReq('GET', '/api/v1/contacts?limit=100')).data || [];
  const distribuicao = await contarDealsPorColuna();
  const usuarios = await descobrirUsuarios();
  const total = contatos.length;
  const semEmail = contatos.filter(c => !c.email).length;
  const semEmpresa = contatos.filter(c => !c.company).length;
  const hoje = new Date().toISOString().slice(0, 10);
  const amanha = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  const prompt =
    'Voce e o Analista Comercial do Centro de Treinamento Juarez Leite. Analise os dados VIVOS do CRM Voibi abaixo e produza um diagnostico curto e acionavel.\n\n' +
    'DISTRIBUICAO EXATA DO FUNIL (total de deals por coluna, por pipeline):\n' + JSON.stringify(distribuicao, null, 2) + '\n\n' +
    'AMOSTRA DE DEALS (50 mais recentes, com IDs reais):\n' + JSON.stringify(deals, null, 2) + '\n\n' +
    'AMOSTRA DE CONTATOS (' + total + ' lidos nesta rodada): sem email ' + semEmail + ', sem empresa ' + semEmpresa + '.\n\n' +
    'USUARIOS DISPONIVEIS (nome -> assigned_user_id) para atribuir tarefas:\n' + JSON.stringify(usuarios, null, 2) + '\n\n' +
    'Produza:\n1. Status do funil e represas detectadas\n2. Leads quentes parados (cite o deal)\n3. Proxima acao concreta por gargalo\n4. Qualidade da base\n5. 3 recomendacoes do dia\n\n' +
    'Ao final, gere obrigatoriamente um bloco ACOES_JSON com as tarefas concretas a criar no Voibi (maximo 5, as mais urgentes). Use IDs REAIS dos dados acima.\n' +
    'Regras: "assigned_user_id" deve ser um UUID da lista de usuarios (se nao tiver certeza, deixe ""). "deal_id" deve ser o id de um deal acima quando a tarefa for sobre um lead (senao ""). "priority" deve ser low, medium, high ou urgent.\n' +
    'Formato EXATO (nao altere a estrutura):\nACOES_JSON:\n```json\n' +
    '{"tarefas": [{"title": "titulo curto", "description": "detalhe", "assigned_user_id": "", "deal_id": "", "due_date": "YYYY-MM-DDTHH:MM:SS-03:00", "priority": "high"}]}\n```\n' +
    'Use a data de hoje (' + hoje + ') ou amanha (' + amanha + ') nos prazos.';
  const resp = await chamarClaude(prompt);
  await inserirSugestao('Analista Comercial', resp, extrairAcoesJson(resp));
}

async function agenteGestor() {
  const tarefas = (await voibiReq('GET', '/api/v1/tasks?limit=100')).data || [];
  const usuarios = await descobrirUsuarios();
  const agora = new Date().toISOString();
  const hoje = agora.slice(0, 10);
  const amanha = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  const prompt =
    'Voce e o Gestor de Tarefas do Centro de Treinamento Juarez Leite. Analise as tarefas VIVAS do Voibi abaixo e produza sugestoes de gestao.\n\n' +
    'DATA ATUAL: ' + agora + '\n\n' +
    'TAREFAS (com IDs reais):\n' + JSON.stringify(tarefas, null, 2) + '\n\n' +
    'USUARIOS (nome -> assigned_user_id):\n' + JSON.stringify(usuarios, null, 2) + '\n\n' +
    'Produza:\n1. Tarefas ATRASADAS (due_date no passado e status diferente de completed)\n2. Tarefas VENCENDO HOJE\n3. Tarefas das PROXIMAS 48h\n4. Avaliacao de carga por responsavel e sugestao de redistribuicao\n5. Resumo diario: total atrasadas, vencendo hoje, % no prazo, 3 focos do dia\n\n' +
    'Ao final, gere obrigatoriamente um bloco ACOES_JSON.\n' +
    'Regras: em "tarefas_atualizar", "id" e o UUID real da tarefa e "status" so pode ser pending, in_progress, completed ou cancelled; "priority" so pode ser low, medium, high, urgent. Em "tarefas_criar", siga o mesmo formato de tarefa (title, description, assigned_user_id, deal_id, due_date, priority).\n' +
    'Formato EXATO (nao altere a estrutura):\nACOES_JSON:\n```json\n' +
    '{"tarefas_atualizar": [{"id": "uuid-task", "status": "in_progress", "priority": "urgent"}], "tarefas_criar": [{"title": "titulo", "description": "", "assigned_user_id": "", "deal_id": "", "due_date": "YYYY-MM-DDTHH:MM:SS-03:00", "priority": "medium"}]}\n```\n' +
    'Use datas reais (' + hoje + ', ' + amanha + '). Se nao houver itens, envie listas vazias.';
  const resp = await chamarClaude(prompt);
  await inserirSugestao('Gestor de Tarefas', resp, extrairAcoesJson(resp));
}

async function agenteQualidade() {
  const contatos = (await voibiReq('GET', '/api/v1/contacts?limit=100')).data || [];
  const deals = (await voibiReq('GET', '/api/v1/deals?limit=50')).data || [];
  const prompt =
    'Voce e o agente de Qualidade do CRM do Centro de Treinamento Juarez Leite. Analise os dados VIVOS do Voibi e produza um relatorio de higiene.\n\n' +
    'CONTATOS (ate 100, com IDs reais):\n' + JSON.stringify(contatos, null, 2) + '\n\n' +
    'DEALS (para checar negocios sem valor):\n' + JSON.stringify(deals, null, 2) + '\n\n' +
    'Produza:\n1. Contatos incompletos (faltando nome, email ou empresa)\n2. Negocios sem valor preenchido\n3. Duplicados suspeitos com sugestao de qual manter\n4. Percentual de completude\n\n' +
    'Ao final, gere obrigatoriamente um bloco ACOES_JSON com os contatos a sinalizar com a etiqueta "' + VOIBI_TAG_REVISAO + '" (maximo 20 casos criticos). Use o "contact_id" REAL (UUID) de cada contato acima.\n' +
    'Formato EXATO (nao altere a estrutura):\nACOES_JSON:\n```json\n' +
    '{"contatos_revisar": [{"contact_id": "uuid-do-contato", "problema": "descricao do problema"}]}\n```\n' +
    'Se nao houver contatos criticos, envie lista vazia.';
  const resp = await chamarClaude(prompt);
  await inserirSugestao('Qualidade CRM', resp, extrairAcoesJson(resp));
}

async function rodarVarreduraNode() {
  try { await agenteAnalista(); } catch (e) { console.error('Analista falhou:', e.message); }
  try { await agenteGestor(); } catch (e) { console.error('Gestor falhou:', e.message); }
  try { await agenteQualidade(); } catch (e) { console.error('Qualidade falhou:', e.message); }
  console.log('Varredura (Node) concluida.');
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
    // Roda a varredura em Node (sem Python), em segundo plano, e responde na hora.
    rodarVarreduraNode().catch(e => console.error('varredura node falhou:', e.message));
    logSistema('varredura_manual', sess.nome, 'Iniciou varredura manual');
    j(res, 202, { ok: true, started: true, message: 'Varredura iniciada. Aguarde 1 a 2 minutos e atualize.' });
    return;
  }

  if (p === '/aprovar' && req.method === 'POST') {
    const sess = getSession(req);
    if (!sess) { j(res, 401, { erro: 'Não autenticado' }); return; }
    const { id } = await parseBody(req);
    if (!id || !/^\d+$/.test(String(id))) { j(res, 400, { erro: 'id inválido' }); return; }
    try {
      await sbPatch('central_aprovacao', `id=eq.${id}`, { status: 'Aprovado', aprovado_por: sess.nome, decidido_em: new Date().toISOString() });
      const resultado = await executarItemNode(id);
      const erros = [...new Set(resultado.log.filter(l => l.startsWith('ERRO')).map(erroAmigavel))];
      await sbPatch('central_aprovacao', `id=eq.${id}`, { erro_execucao: erros.length ? erros.join(' ') : null });
      logSistema('aprovacao', sess.nome, `Aprovou sugestão #${id}`);
      j(res, 200, { ok: resultado.ok, output: resultado.log.join('\n') });
    } catch (e) {
      j(res, 200, { ok: false, output: 'Erro: ' + e.message });
    }
    return;
  }

  if (p === '/rejeitar' && req.method === 'POST') {
    const sess = getSession(req);
    if (!sess) { j(res, 401, { erro: 'Não autenticado' }); return; }
    const { id, motivo } = await parseBody(req);
    if (!id || !/^\d+$/.test(String(id))) { j(res, 400, { erro: 'id inválido' }); return; }
    if (!motivo || !String(motivo).trim()) { j(res, 400, { erro: 'Motivo obrigatório' }); return; }
    try {
      await sbPatch('central_aprovacao', `id=eq.${id}`, { status: 'Rejeitado', rejeitado_por: sess.nome, motivo_rejeicao: String(motivo).trim(), decidido_em: new Date().toISOString() });
      logSistema('rejeicao', sess.nome, `Rejeitou sugestão #${id}`);
      j(res, 200, { ok: true });
    } catch (e) {
      j(res, 200, { ok: false, erro: e.message });
    }
    return;
  }

  if (p === '/reabrir' && req.method === 'POST') {
    const sess = getSession(req);
    if (!sess) { j(res, 401, { erro: 'Não autenticado' }); return; }
    const { id } = await parseBody(req);
    if (!id || !/^\d+$/.test(String(id))) { j(res, 400, { erro: 'id inválido' }); return; }
    try {
      await sbPatch('central_aprovacao', `id=eq.${id}`, { status: 'Pendente', rejeitado_por: null, motivo_rejeicao: null, decidido_em: null });
      logSistema('reabertura', sess.nome, `Desfez a rejeição da sugestão #${id}`);
      j(res, 200, { ok: true });
    } catch (e) {
      j(res, 200, { ok: false, erro: e.message });
    }
    return;
  }

  if (p === '/logs' && req.method === 'GET') {
    const sess = getSession(req);
    if (!sess) { j(res, 401, { erro: 'Não autenticado' }); return; }
    sbReq('GET', 'logs_sistema', 'select=*&order=id.desc&limit=100', null, (err, st, d) => {
      res.writeHead(st || 200, { 'Content-Type': 'application/json' });
      res.end(d || '[]');
    });
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
