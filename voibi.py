"""Cliente da API do ChatVoibi (https://chat.voibi.com.br).

Centraliza as chamadas REST usadas pelos agentes. Duas chaves, conforme o manual:
- API Key da Empresa: kanban, contatos, etiquetas, tarefas (leitura e escrita).
- API Key da Conexao: envio de mensagem/midia e status de conversa (fase 2).

Defina as variaveis de ambiente:
  VOIBI_API_KEY_EMPRESA, VOIBI_API_KEY_CONEXAO, VOIBI_BASE_URL (opcional)
"""
import os
import json
import urllib.request
import urllib.error
from urllib.parse import urlencode

VOIBI_BASE = os.environ.get("VOIBI_BASE_URL", "https://chat.voibi.com.br")
VOIBI_KEY_EMPRESA = os.environ.get("VOIBI_API_KEY_EMPRESA", "")
VOIBI_KEY_CONEXAO = os.environ.get("VOIBI_API_KEY_CONEXAO", "")


class VoibiError(Exception):
    pass


def _req(method, path, body=None, key=None, query=None):
    if key is None:
        key = VOIBI_KEY_EMPRESA
    if not key:
        raise VoibiError(
            f"API Key do Voibi ausente para {method} {path}. "
            "Defina VOIBI_API_KEY_EMPRESA (kanban/contatos/etiquetas/tarefas) "
            "ou VOIBI_API_KEY_CONEXAO (envio)."
        )
    url = f"{VOIBI_BASE}{path}"
    if query:
        limpos = {k: v for k, v in query.items() if v is not None}
        if limpos:
            url += "?" + urlencode(limpos)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        res = urllib.request.urlopen(req)
        raw = res.read().decode()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode()
        raise VoibiError(f"{method} {path} -> HTTP {e.code}: {detalhe}")
    except urllib.error.URLError as e:
        raise VoibiError(f"{method} {path} -> falha de conexao: {e.reason}")


# ── Kanban ──
def listar_colunas():
    return _req("GET", "/api/v1/columns")

def listar_pipelines():
    return _req("GET", "/api/v1/pipelines")

def listar_deals(**query):
    return _req("GET", "/api/v1/deals", query=query)

def obter_deal(deal_id):
    return _req("GET", f"/api/v1/deals/{deal_id}")

def criar_deal(payload):
    return _req("POST", "/api/v1/deals/create", body=payload)

def mover_deal(deal_id, column_id):
    return _req("POST", "/api/v1/deals/move", body={"deal_id": deal_id, "column_id": column_id})

def atualizar_deal(deal_id, dados):
    return _req("PUT", f"/api/v1/deals/{deal_id}", body=dados)


# ── Contatos ──
def listar_contatos(**query):
    return _req("GET", "/api/v1/contacts", query=query)

def obter_contato(contact_id):
    return _req("GET", f"/api/v1/contacts/{contact_id}")


# ── Etiquetas ──
def listar_tags():
    return _req("GET", "/api/v1/tags")

def vincular_tags(contact_id, tag_ids):
    return _req("POST", "/api/v1/contacts/tags", body={"contact_id": contact_id, "tag_ids": tag_ids})


# ── Tarefas ──
def listar_tarefas(**query):
    return _req("GET", "/api/v1/tasks", query=query)

def criar_tarefa(payload):
    return _req("POST", "/api/v1/tasks", body=payload)

def atualizar_tarefa(task_id, dados):
    return _req("PUT", f"/api/v1/tasks/{task_id}", body=dados)


# ── Auxiliares ──
def achar_tag_id(nome):
    """Retorna o UUID da etiqueta cujo nome bate (case-insensitive), ou None."""
    data = listar_tags()
    for t in data.get("tags", []):
        if (t.get("name") or "").strip().lower() == nome.strip().lower():
            return t.get("id")
    return None


def _coletar_usuarios(registros, mp):
    """Garimpa usuarios (nome -> uuid) de uma lista de deals/tarefas.
    A API real devolve campos achatados (assigned_user_id, assigned_user_name,
    created_by_user_id, created_by_user_name). Tambem cobre o formato aninhado
    do manual (assigned_user: {id, name}) caso a API mude."""
    for x in registros:
        if x.get("assigned_user_id") and x.get("assigned_user_name"):
            mp[x["assigned_user_name"]] = x["assigned_user_id"]
        if x.get("created_by_user_id") and x.get("created_by_user_name"):
            mp[x["created_by_user_name"]] = x["created_by_user_id"]
        u = x.get("assigned_user")
        if isinstance(u, dict) and u.get("id"):
            mp[u.get("name", "?")] = u["id"]
    return mp


def descobrir_usuarios():
    """Monta o mapa nome -> uuid a partir de tarefas e deals (nao ha endpoint de usuarios)."""
    mp = {}
    try:
        _coletar_usuarios(listar_tarefas(limit=100).get("data", []), mp)
    except VoibiError:
        pass
    try:
        _coletar_usuarios(listar_deals(limit=100).get("data", []), mp)
    except VoibiError:
        pass
    return mp


def contar_deals_por_coluna():
    """Distribuicao EXATA do funil: {pipeline: {coluna: total}}.
    Usa o total da paginacao por coluna (nao baixa todos os deals)."""
    out = {}
    data = listar_colunas()
    for pipe in data.get("pipelines", []):
        pnome = pipe.get("name", "?")
        out[pnome] = {}
        for col in pipe.get("columns", []):
            try:
                r = listar_deals(column_id=col["id"], limit=1)
                total = r.get("total") or (r.get("pagination") or {}).get("total") or 0
            except VoibiError:
                total = 0
            out[pnome][col.get("name", "?")] = total
    return out
