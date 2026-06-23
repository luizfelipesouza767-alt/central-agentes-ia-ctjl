import urllib.request
import urllib.error
import json
import re
import os
from datetime import datetime, timedelta

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"

def supabase_query(tabela, select, filtro=None):
    url = f"{SUPABASE_URL}/rest/v1/{tabela}?select={select}"
    if filtro:
        url += f"&{filtro}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
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
        "Prefer": "return=minimal"
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
        "Prefer": "return=minimal"
    })
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        print(f"Erro update em {tabela}: {e.read().decode()}")

def chamar_claude(prompt):
    url = "https://api.anthropic.com/v1/messages"
    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
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

def rodar_analista_comercial():
    print("Rodando Analista Comercial...")
    leads = supabase_query("leads", "nome,telefone,etapa,temperatura,lead_score,valor,data_ultima_interacao")
    contatos = supabase_query("contatos", "nome,telefone,email,empresa,origem")

    total = len(contatos)
    sem_email = sum(1 for c in contatos if not c.get("email"))
    sem_empresa = sum(1 for c in contatos if not c.get("empresa"))
    sem_origem = sum(1 for c in contatos if not c.get("origem"))
    hoje = datetime.now().strftime("%Y-%m-%d")
    amanha = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    prompt = (
        "Voce e o Analista Comercial do Instituto Juarez Leite. Analise os dados abaixo e produza um diagnostico curto e acionavel.\n\n"
        f"FUNIL DE VENDAS:\n"
        + (json.dumps(leads, ensure_ascii=False, indent=2) if leads else "Sem leads qualificados ainda.")
        + f"\n\nBASE DE CONTATOS ({total} contatos):\n"
        f"- Sem email: {sem_email} ({round(sem_email/total*100) if total else 0}%)\n"
        f"- Sem empresa: {sem_empresa} ({round(sem_empresa/total*100) if total else 0}%)\n"
        f"- Sem origem: {sem_origem} ({round(sem_origem/total*100) if total else 0}%)\n\n"
        "DISTRIBUICAO ATUAL: Lead Entrou: 56, Lead Contactado: 314, Em Negociacao: 140, Aguardando Pagamento: 2, Fechado: 14, Perdido: 8, Suporte: 139.\n\n"
        "Produza:\n1. Status do funil e represas detectadas\n2. Leads quentes parados\n"
        "3. Proxima acao concreta por gargalo\n4. Qualidade da base\n5. 3 recomendacoes do dia\n\n"
        "Ao final, gere obrigatoriamente um bloco ACOES_JSON com as tarefas concretas a criar (maximo 5, as mais urgentes).\n"
        "Formato EXATO (nao altere a estrutura):\n"
        "ACOES_JSON:\n```json\n"
        '{"tarefas": [{"tarefa": "descricao da tarefa", "responsavel": "", "prazo": "YYYY-MM-DD", "etapa_relacionada": "etapa do funil"}]}\n'
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
        "executado": False
    })
    print("Analista Comercial: concluido.")

def rodar_gestor_de_tarefas():
    print("Rodando Gestor de Tarefas...")
    tarefas = supabase_query("tarefas", "id,tarefa,responsavel,etapa_relacionada,prazo,status,contato_telefone")
    hoje = datetime.now().isoformat()
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    amanha_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    prompt = (
        "Voce e o Gestor de Tarefas do Instituto Juarez Leite. Analise as tarefas abaixo e produza sugestoes de gestao.\n\n"
        f"DATA ATUAL: {hoje}\n\n"
        f"TAREFAS:\n{json.dumps(tarefas, ensure_ascii=False, indent=2)}\n\n"
        "Produza:\n1. Tarefas ATRASADAS\n2. Tarefas VENCENDO HOJE\n3. Tarefas das PROXIMAS 48h\n"
        "4. Avaliacao de carga por responsavel e sugestao de redistribuicao\n"
        "5. Resumo diario: total atrasadas, vencendo hoje, % no prazo, 3 focos do dia\n\n"
        "Ao final, gere obrigatoriamente um bloco ACOES_JSON.\n"
        "Formato EXATO (nao altere a estrutura):\n"
        "ACOES_JSON:\n```json\n"
        '{"tarefas_atualizar": [{"id": 1, "status": "Atrasada"}], '
        '"tarefas_criar": [{"tarefa": "descricao", "responsavel": "", "prazo": "YYYY-MM-DD", "etapa_relacionada": ""}]}\n'
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
        "executado": False
    })
    print("Gestor de Tarefas: concluido.")

def rodar_qualidade_crm():
    print("Rodando Qualidade do CRM...")
    leads = supabase_query("leads", "nome,telefone,etapa,valor,origem")
    contatos = supabase_query("contatos", "nome,telefone,email,empresa,origem")

    prompt = (
        "Voce e o agente de Qualidade do CRM do Instituto Juarez Leite. Analise os dados abaixo e produza um relatorio de higiene.\n\n"
        f"LEADS:\n{json.dumps(leads, ensure_ascii=False, indent=2)}\n\n"
        f"CONTATOS (primeiros 50):\n{json.dumps(contatos[:50], ensure_ascii=False, indent=2)}\n\n"
        "Produza:\n1. Contatos incompletos (faltando telefone, nome, email ou empresa)\n"
        "2. Negocios sem valor preenchido\n3. Leads sem origem\n"
        "4. Duplicados suspeitos com sugestao de qual manter\n"
        "5. Cards de Suporte no funil comercial para separar\n"
        "6. Percentual de completude\n\n"
        "Ao final, gere obrigatoriamente um bloco ACOES_JSON com os contatos a marcar para revisao (maximo 20 casos criticos).\n"
        "Formato EXATO (nao altere a estrutura):\n"
        "ACOES_JSON:\n```json\n"
        '{"contatos_revisar": [{"telefone": "numero", "problema": "descricao do problema"}]}\n'
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
        "executado": False
    })
    print("Qualidade CRM: concluido.")

def executar_aprovados():
    print("Verificando aprovacoes pendentes de execucao...")
    aprovados = supabase_query(
        "central_aprovacao",
        "id,agente,acoes_json",
        "status=eq.Aprovado&executado=eq.false"
    )

    if not aprovados:
        print("Nenhuma aprovacao pendente de execucao.")
        return

    for item in aprovados:
        agente = item.get("agente")
        item_id = item.get("id")
        acoes_raw = item.get("acoes_json") or "{}"

        try:
            acoes = json.loads(acoes_raw) if isinstance(acoes_raw, str) else acoes_raw
        except json.JSONDecodeError:
            print(f"ID {item_id}: acoes_json invalido, marcando como executado.")
            supabase_update("central_aprovacao", f"id=eq.{item_id}", {"executado": True})
            continue

        print(f"Executando: ID {item_id} | {agente}")

        if agente == "Analista Comercial":
            tarefas = acoes.get("tarefas", [])
            for t in tarefas:
                supabase_insert("tarefas", {
                    "tarefa": t.get("tarefa", ""),
                    "responsavel": t.get("responsavel", ""),
                    "etapa_relacionada": t.get("etapa_relacionada", ""),
                    "prazo": t.get("prazo") or None,
                    "status": "Pendente",
                    "contato_telefone": t.get("contato_telefone", "")
                })
            print(f"  {len(tarefas)} tarefa(s) criada(s) no Supabase.")

        elif agente == "Gestor de Tarefas":
            criar = acoes.get("tarefas_criar", [])
            for t in criar:
                supabase_insert("tarefas", {
                    "tarefa": t.get("tarefa", ""),
                    "responsavel": t.get("responsavel", ""),
                    "etapa_relacionada": t.get("etapa_relacionada", ""),
                    "prazo": t.get("prazo") or None,
                    "status": "Pendente",
                    "contato_telefone": ""
                })
            atualizar = acoes.get("tarefas_atualizar", [])
            for t in atualizar:
                if t.get("id"):
                    supabase_update("tarefas", f"id=eq.{t['id']}", {"status": t.get("status", "Atrasada")})
            print(f"  {len(criar)} tarefa(s) criada(s), {len(atualizar)} atualizada(s).")

        elif agente == "Qualidade CRM":
            contatos_revisar = acoes.get("contatos_revisar", [])
            for c in contatos_revisar:
                tel = c.get("telefone", "")
                problema = c.get("problema", "")
                if tel:
                    supabase_update("contatos", f"telefone=eq.{tel}", {
                        "revisao_pendente": True,
                        "problema_crm": problema
                    })
            print(f"  {len(contatos_revisar)} contato(s) marcado(s) para revisao.")

        supabase_update("central_aprovacao", f"id=eq.{item_id}", {"executado": True})
        print(f"  ID {item_id} marcado como executado.")

    print("Execucao de aprovados concluida.")

if __name__ == "__main__":
    print(f"=== Iniciando varredura: {datetime.now().strftime('%d/%m/%Y %H:%M')} ===")
    executar_aprovados()
    rodar_analista_comercial()
    rodar_gestor_de_tarefas()
    rodar_qualidade_crm()
    print("=== Varredura concluida ===")
