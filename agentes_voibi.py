"""Varredura dos agentes de IA do Centro de Treinamento Juarez Leite.

Arquitetura (fase 1, so CRM):
- LE os dados vivos direto do Voibi (deals, contatos, tarefas, colunas) via API.
- ANALISA com Claude e grava a sugestao + ACOES_JSON na fila Supabase (central_aprovacao).
- EXECUTA as acoes aprovadas DENTRO do Voibi (cria tarefa, move deal, aplica etiqueta).

Supabase continua sendo so a camada de governanca: login (usuarios) e fila (central_aprovacao).
O Voibi e a fonte da verdade do CRM.

Uso:
  python3 agentes_voibi.py                  # varredura completa (executa aprovados + roda os 3 agentes)
  python3 agentes_voibi.py --executar-item N  # executa SO o item N (aprovacao instantanea pelo painel)
"""
import sys
import json
import re
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta

import voibi

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"

# Etiqueta usada pelo agente de Qualidade para sinalizar contato a revisar.
# Precisa existir no Voibi (a API nao cria etiquetas). Configuravel por env.
TAG_REVISAO = os.environ.get("VOIBI_TAG_REVISAO", "Revisão CRM")
# Usuario padrao para tarefas quando o agente nao consegue definir o responsavel.
# assigned_user_id e obrigatorio ao criar tarefa no Voibi.
DEFAULT_USER_ID = os.environ.get("VOIBI_DEFAULT_USER_ID", "")


# ── Supabase: apenas central_aprovacao ──
def supabase_query(tabela, select, filtro=None):
    url = f"{SUPABASE_URL}/rest/v1/{tabela}?select={select}"
    if filtro:
        url += f"&{filtro}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        res = urllib.request.urlopen(req)
        return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Erro {e.code} em {tabela}: {e.read().decode()}")
        return []


def supabase_insert(tabela, dados):
    url = f"{SUPABASE_URL}/rest/v1/{tabela}"
    payload = json.dumps(dados).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    })
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        print(f"Erro insert em {tabela}: {e.read().decode()}")


def supabase_update(tabela, filtro, dados):
    url = f"{SUPABASE_URL}/rest/v1/{tabela}?{filtro}"
    payload = json.dumps(dados).encode()
    req = urllib.request.Request(url, data=payload, method="PATCH", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    })
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        print(f"Erro update em {tabela}: {e.read().decode()}")


# ── Claude ──
def chamar_claude(prompt):
    url = "https://api.anthropic.com/v1/messages"
    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    try:
        res = urllib.request.urlopen(req)
        data = json.loads(res.read().decode())
        return data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        erro = e.read().decode()
        print(f"Erro Claude: {erro}")
        return f"Erro ao chamar Claude: {erro}"


def extrair_acoes_json(texto):
    match = re.search(r'ACOES_JSON:\s*```json\s*(.*?)\s*```', texto, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            print("Aviso: ACOES_JSON mal formatado, ignorando.")
    return {}


# ── Agentes ──
def rodar_analista_comercial():
    print("Rodando Analista Comercial...")
    deals = voibi.listar_deals(limit=50).get("data", [])
    contatos = voibi.listar_contatos(limit=100).get("data", [])
    distribuicao = voibi.contar_deals_por_coluna()
    usuarios = voibi.descobrir_usuarios()

    total = len(contatos)
    sem_email = sum(1 for c in contatos if not c.get("email"))
    sem_empresa = sum(1 for c in contatos if not c.get("company"))
    hoje = datetime.now().strftime("%Y-%m-%d")
    amanha = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    prompt = (
        "Voce e o Analista Comercial do Centro de Treinamento Juarez Leite. "
        "Analise os dados VIVOS do CRM Voibi abaixo e produza um diagnostico curto e acionavel.\n\n"
        f"DISTRIBUICAO EXATA DO FUNIL (total de deals por coluna, por pipeline):\n{json.dumps(distribuicao, ensure_ascii=False, indent=2)}\n\n"
        f"AMOSTRA DE DEALS (50 mais recentes, com IDs reais):\n{json.dumps(deals, ensure_ascii=False, indent=2)}\n\n"
        f"AMOSTRA DE CONTATOS ({total} lidos nesta rodada): sem email {sem_email}, sem empresa {sem_empresa}.\n\n"
        f"USUARIOS DISPONIVEIS (nome -> assigned_user_id) para atribuir tarefas:\n{json.dumps(usuarios, ensure_ascii=False, indent=2)}\n\n"
        "Produza:\n1. Status do funil e represas detectadas\n2. Leads quentes parados (cite o deal)\n"
        "3. Proxima acao concreta por gargalo\n4. Qualidade da base\n5. 3 recomendacoes do dia\n\n"
        "Ao final, gere obrigatoriamente um bloco ACOES_JSON com as tarefas concretas a criar no Voibi "
        "(maximo 5, as mais urgentes). Use IDs REAIS dos dados acima.\n"
        "Regras: 'assigned_user_id' deve ser um UUID da lista de usuarios (se nao tiver certeza, deixe \"\"). "
        "'deal_id' deve ser o id de um deal acima quando a tarefa for sobre um lead (senao \"\"). "
        "'priority' deve ser low, medium, high ou urgent.\n"
        "Formato EXATO (nao altere a estrutura):\n"
        "ACOES_JSON:\n```json\n"
        '{"tarefas": [{"title": "titulo curto", "description": "detalhe", "assigned_user_id": "", '
        '"deal_id": "", "due_date": "YYYY-MM-DDTHH:MM:SS-03:00", "priority": "high"}]}\n'
        "```\n"
        f"Use a data de hoje ({hoje}) ou amanha ({amanha}) nos prazos."
    )

    resposta = chamar_claude(prompt)
    acoes = extrair_acoes_json(resposta)
    supabase_insert("central_aprovacao", {
        "agente": "Analista Comercial",
        "sugestao": resposta,
        "acoes_json": json.dumps(acoes, ensure_ascii=False),
        "status": "Pendente",
        "executado": False,
    })
    print("Analista Comercial: concluido.")


def rodar_gestor_de_tarefas():
    print("Rodando Gestor de Tarefas...")
    tarefas = voibi.listar_tarefas(limit=100).get("data", [])
    usuarios = voibi.descobrir_usuarios()
    hoje = datetime.now().isoformat()
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    amanha_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    prompt = (
        "Voce e o Gestor de Tarefas do Centro de Treinamento Juarez Leite. "
        "Analise as tarefas VIVAS do Voibi abaixo e produza sugestoes de gestao.\n\n"
        f"DATA ATUAL: {hoje}\n\n"
        f"TAREFAS (com IDs reais):\n{json.dumps(tarefas, ensure_ascii=False, indent=2)}\n\n"
        f"USUARIOS (nome -> assigned_user_id):\n{json.dumps(usuarios, ensure_ascii=False, indent=2)}\n\n"
        "Produza:\n1. Tarefas ATRASADAS (due_date no passado e status diferente de completed)\n"
        "2. Tarefas VENCENDO HOJE\n3. Tarefas das PROXIMAS 48h\n"
        "4. Avaliacao de carga por responsavel e sugestao de redistribuicao\n"
        "5. Resumo diario: total atrasadas, vencendo hoje, % no prazo, 3 focos do dia\n\n"
        "Ao final, gere obrigatoriamente um bloco ACOES_JSON.\n"
        "Regras: em 'tarefas_atualizar', 'id' e o UUID real da tarefa e 'status' so pode ser "
        "pending, in_progress, completed ou cancelled; 'priority' so pode ser low, medium, high, urgent. "
        "Em 'tarefas_criar', siga o mesmo formato de tarefa (title, description, assigned_user_id, deal_id, due_date, priority).\n"
        "Formato EXATO (nao altere a estrutura):\n"
        "ACOES_JSON:\n```json\n"
        '{"tarefas_atualizar": [{"id": "uuid-task", "status": "in_progress", "priority": "urgent"}], '
        '"tarefas_criar": [{"title": "titulo", "description": "", "assigned_user_id": "", "deal_id": "", '
        '"due_date": "YYYY-MM-DDTHH:MM:SS-03:00", "priority": "medium"}]}\n'
        "```\n"
        f"Use datas reais ({hoje_str}, {amanha_str}). Se nao houver itens, envie listas vazias."
    )

    resposta = chamar_claude(prompt)
    acoes = extrair_acoes_json(resposta)
    supabase_insert("central_aprovacao", {
        "agente": "Gestor de Tarefas",
        "sugestao": resposta,
        "acoes_json": json.dumps(acoes, ensure_ascii=False),
        "status": "Pendente",
        "executado": False,
    })
    print("Gestor de Tarefas: concluido.")


def rodar_qualidade_crm():
    print("Rodando Qualidade do CRM...")
    contatos = voibi.listar_contatos(limit=100).get("data", [])
    deals = voibi.listar_deals(limit=100).get("data", [])

    prompt = (
        "Voce e o agente de Qualidade do CRM do Centro de Treinamento Juarez Leite. "
        "Analise os dados VIVOS do Voibi e produza um relatorio de higiene.\n\n"
        f"CONTATOS (ate 100, com IDs reais):\n{json.dumps(contatos, ensure_ascii=False, indent=2)}\n\n"
        f"DEALS (para checar negocios sem valor):\n{json.dumps(deals, ensure_ascii=False, indent=2)}\n\n"
        "Produza:\n1. Contatos incompletos (faltando nome, email ou empresa)\n"
        "2. Negocios sem valor preenchido\n3. Duplicados suspeitos com sugestao de qual manter\n"
        "4. Percentual de completude\n\n"
        f"Ao final, gere obrigatoriamente um bloco ACOES_JSON com os contatos a sinalizar com a "
        f"etiqueta '{TAG_REVISAO}' (maximo 20 casos criticos). Use o 'contact_id' REAL (UUID) de cada contato acima.\n"
        "Formato EXATO (nao altere a estrutura):\n"
        "ACOES_JSON:\n```json\n"
        '{"contatos_revisar": [{"contact_id": "uuid-do-contato", "problema": "descricao do problema"}]}\n'
        "```\n"
        "Se nao houver contatos criticos, envie lista vazia."
    )

    resposta = chamar_claude(prompt)
    acoes = extrair_acoes_json(resposta)
    supabase_insert("central_aprovacao", {
        "agente": "Qualidade CRM",
        "sugestao": resposta,
        "acoes_json": json.dumps(acoes, ensure_ascii=False),
        "status": "Pendente",
        "executado": False,
    })
    print("Qualidade CRM: concluido.")


# ── Execucao das acoes aprovadas (escreve no Voibi) ──
def _criar_tarefa_voibi(t):
    """Cria uma tarefa no Voibi. Retorna (ok, mensagem)."""
    assigned = t.get("assigned_user_id") or DEFAULT_USER_ID
    if not assigned:
        return False, f"tarefa '{t.get('title','')}' sem assigned_user_id (defina VOIBI_DEFAULT_USER_ID)"
    payload = {
        "title": t.get("title") or t.get("tarefa") or "Tarefa",
        "description": t.get("description", ""),
        "assigned_user_id": assigned,
        "priority": t.get("priority", "medium"),
    }
    if t.get("deal_id"):
        payload["deal_id"] = t["deal_id"]
    if t.get("due_date") or t.get("prazo"):
        payload["due_date"] = t.get("due_date") or t.get("prazo")
    voibi.criar_tarefa(payload)
    return True, f"tarefa criada: {payload['title']}"


def _executar_um(item):
    """Executa as acoes de um item da central_aprovacao no Voibi.
    Retorna lista de linhas de log."""
    log = []
    agente = item.get("agente")
    item_id = item.get("id")
    acoes_raw = item.get("acoes_json") or "{}"
    try:
        acoes = json.loads(acoes_raw) if isinstance(acoes_raw, str) else acoes_raw
    except json.JSONDecodeError:
        log.append(f"ID {item_id}: acoes_json invalido, nada a executar.")
        supabase_update("central_aprovacao", f"id=eq.{item_id}", {"executado": True})
        return log

    log.append(f"Executando ID {item_id} ({agente}) no Voibi...")

    try:
        if agente == "Analista Comercial":
            for t in acoes.get("tarefas", []):
                ok, msg = _criar_tarefa_voibi(t)
                log.append(("  ok " if ok else "  ERRO ") + msg)

        elif agente == "Gestor de Tarefas":
            for t in acoes.get("tarefas_criar", []):
                ok, msg = _criar_tarefa_voibi(t)
                log.append(("  ok " if ok else "  ERRO ") + msg)
            for t in acoes.get("tarefas_atualizar", []):
                tid = t.get("id")
                if not tid:
                    continue
                dados = {}
                if t.get("status"):
                    dados["status"] = t["status"]
                if t.get("priority"):
                    dados["priority"] = t["priority"]
                if dados:
                    voibi.atualizar_tarefa(tid, dados)
                    log.append(f"  ok tarefa {tid} atualizada: {dados}")

        elif agente == "Qualidade CRM":
            tag_id = voibi.achar_tag_id(TAG_REVISAO)
            if not tag_id:
                log.append(f"  ERRO etiqueta '{TAG_REVISAO}' nao existe no Voibi. Crie-a em Configuracoes e rode de novo.")
            else:
                for c in acoes.get("contatos_revisar", []):
                    cid = c.get("contact_id")
                    if cid:
                        voibi.vincular_tags(cid, [tag_id])
                        log.append(f"  ok contato {cid} etiquetado '{TAG_REVISAO}'")
        else:
            log.append(f"  Agente desconhecido: {agente}")
    except voibi.VoibiError as e:
        log.append(f"  ERRO Voibi: {e}")

    supabase_update("central_aprovacao", f"id=eq.{item_id}", {"executado": True})
    log.append(f"  ID {item_id} marcado como executado.")
    return log


def executar_aprovados():
    print("Verificando aprovacoes pendentes de execucao...")
    aprovados = supabase_query(
        "central_aprovacao",
        "id,agente,acoes_json",
        "status=eq.Aprovado&executado=eq.false",
    )
    if not aprovados:
        print("Nenhuma aprovacao pendente de execucao.")
        return
    for item in aprovados:
        for linha in _executar_um(item):
            print(linha)
    print("Execucao de aprovados concluida.")


def executar_item(item_id):
    """Executa SO um item (chamado pelo painel ao aprovar = execucao instantanea)."""
    rows = supabase_query(
        "central_aprovacao",
        "id,agente,acoes_json,status,executado",
        f"id=eq.{item_id}",
    )
    if not rows:
        print(f"Item {item_id} nao encontrado.")
        return
    item = rows[0]
    if item.get("executado"):
        print(f"Item {item_id} ja foi executado.")
        return
    for linha in _executar_um(item):
        print(linha)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--executar-item":
        executar_item(sys.argv[2])
    else:
        print(f"=== Iniciando varredura: {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")
        supabase_insert("logs_sistema", {
            "tipo": "varredura_auto",
            "usuario": "Sistema (agendado)",
            "detalhe": "Varredura automática iniciada"
        })
        executar_aprovados()
        rodar_analista_comercial()
        rodar_gestor_de_tarefas()
        rodar_qualidade_crm()
        print("=== Varredura concluida ===")
