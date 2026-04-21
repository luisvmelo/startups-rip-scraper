"""
consultoria_benchmark.py
========================
Sistema consultivo de risco por benchmarking contra startups falidas.

Filosofia: confiabilidade > sofisticação.
    A recomendação é forte quando VÁRIAS dimensões batem ao mesmo tempo, mesmo
    que a causa específica do fracasso não esteja documentada. Empresa morta +
    mesmo segmento + mesmo produto + mesma era + mesmo país = alerta, ainda
    que o "motivo oficial" seja desconhecido — a soma das arestas é o sinal.

Camadas de confiabilidade:
    1. Enriquecimento offline (uma vez, cacheado):
       - Causa de falha inferida por lexicon sobre `description` para as ~1000
         startups.rip que só têm status=Inactive (sem motivo documentado).
       - Expansão de categorias para "macro-segmentos" (Fintech↔Finances,
         SaaS↔Software, AI↔Generative AI, etc.) — une taxonomias desalinhadas
         entre fontes.
       - TF-IDF sobre `description` pra similaridade semântica (Payments ≈
         Fintech mesmo sem a palavra exata).
    2. Scoring multi-dimensional (7 dimensões, cada uma com peso e evidência).
    3. Bônus de convergência: se a empresa está morta E alinhou em >=4
       dimensões, adiciona +20 e marca "CLONE ESTRUTURAL".
    4. Relatório com checklist de dimensões + trechos literais que sustentam o
       match (user valida com os próprios olhos, não confia cegamente no score).

Uso:
    python consultoria_benchmark.py
    python consultoria_benchmark.py --input output/consultorias/sample_input.json
    python consultoria_benchmark.py --rebuild-enrichment   (descartar cache)

Saídas em ./output/consultorias/:
    - consultoria_<empresa>_<ts>.txt   — relatório humano
    - consultoria_<empresa>_<ts>.json  — grafo com nó do usuário + RESEMBLES
    - consultoria_<empresa>_<ts>_matches.json — ranking estruturado
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

# Forçar UTF-8 no stdout (Windows defaulta pra cp1252)
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

# Numpy (requerido por causa dos embeddings; import em modo defensivo)
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# Camada semântica (opcional mas recomendada)
try:
    from sentence_transformers import SentenceTransformer
    SEMANTIC_AVAILABLE = True and NUMPY_AVAILABLE
except ImportError:
    SEMANTIC_AVAILABLE = False

# ─── Paths ───────────────────────────────────────────────────────────────────

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(ROOT, "output")
GRAPH_PATH = os.path.join(OUTPUT_DIR, "startups_graph_multi.json")
COMPANIES_PATH = os.path.join(OUTPUT_DIR, "multi_source_companies.json")
RAW_STARTUPS_RIP_PATH = os.path.join(OUTPUT_DIR, "startups_raw.json")
ENRICHED_PATH = os.path.join(OUTPUT_DIR, "multi_source_companies_enriched.json")
SEMANTIC_CACHE_PATH = os.path.join(OUTPUT_DIR, "company_embeddings.npz")
CONSULTORIAS_DIR = os.path.join(OUTPUT_DIR, "consultorias")
os.makedirs(CONSULTORIAS_DIR, exist_ok=True)

# Modelo de embedding multilingual: trata PT e EN nativamente, ~120MB,
# baixa na 1ª vez do HuggingFace e cacheia em ~/.cache/huggingface/.
SEMANTIC_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


# ─── Alias de países (fecha buraco Brasil↔Brazil, USA↔United States, etc.) ────
COUNTRY_ALIASES = {
    "brasil": "brazil",
    "br": "brazil",
    "brazilian": "brazil",
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "u.s.a.": "united states",
    "eua": "united states",
    "estados unidos": "united states",
    "american": "united states",
    "uk": "united kingdom",
    "reino unido": "united kingdom",
    "inglaterra": "united kingdom",
    "alemanha": "germany",
    "franca": "france",
    "espanha": "spain",
    "italia": "italy",
    "holanda": "netherlands",
    "paises baixos": "netherlands",
    "suecia": "sweden",
    "suica": "switzerland",
    "india": "india",
    "china": "china",
    "japao": "japan",
    "russia": "russia",
    "mexico": "mexico",
    "argentina": "argentina",
    "chile": "chile",
    "colombia": "colombia",
    "canada": "canada",
    "australia": "australia",
}


def normalize_country(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s]", "", s)  # tira pontuação
    return COUNTRY_ALIASES.get(s, s)


# ─── Dicionário PT↔EN pra TF-IDF e macro-matching ────────────────────────────
# Mapeia termos comuns de startup-speak PT → equivalente em inglês presente
# nas descrições do dataset. Aplicado ao tokenizer ANTES da vectorização.
# Esse dicionário existe PORQUE a base é majoritariamente em inglês; sem isso,
# o user escrevendo "pagamentos" em PT não bateria com "payments" em EN.
PT_MULTIWORD = {
    "meio ambiente": "environment",
    "recursos humanos": "human resources",
    "inteligencia artificial": "artificial intelligence",
    "aprendizado de maquina": "machine learning",
    "cadeia de suprimentos": "supply chain",
    "conciliacao bancaria": "bank reconciliation",
    "financas pessoais": "personal finance",
    "realidade virtual": "virtual reality",
    "realidade aumentada": "augmented reality",
    "internet das coisas": "internet of things",
    "big data": "big data",
    "no code": "no code",
    "low code": "low code",
    "pequenas e medias empresas": "small medium business",
    "estados unidos": "united states",
}

PT_TO_EN_TERMS = {
    # Geografia
    "brasil": "brazil", "brasileiro": "brazilian", "brasileira": "brazilian",
    "brasileiros": "brazilian", "brasileiras": "brazilian",
    # Segmentos amplos
    "saude": "health", "financas": "finance", "financeiro": "financial",
    "financeira": "financial", "pagamentos": "payments", "pagamento": "payment",
    "varejo": "retail", "educacao": "education", "ensino": "education",
    "logistica": "logistics", "entretenimento": "entertainment",
    "imoveis": "real estate", "imobiliario": "real estate", "imobiliaria": "real estate",
    "seguros": "insurance", "seguro": "insurance",
    "seguranca": "security", "midia": "media", "jogos": "gaming",
    "musica": "music", "viagens": "travel", "turismo": "travel",
    "noticias": "news", "trabalho": "work", "emprego": "employment",
    "vagas": "jobs", "estagio": "internship", "estudante": "student",
    "estudantes": "students", "universitario": "university",
    "universidade": "university", "universitaria": "university",
    "escola": "school", "professor": "teacher", "aluno": "student",
    "curso": "course", "cursos": "courses", "aprendizado": "learning",
    "consultoria": "consulting", "agronegocio": "agriculture",
    "agro": "agriculture", "agricultura": "agriculture",
    "construcao": "construction", "alimento": "food", "alimentos": "food",
    "bebida": "beverage", "bebidas": "beverage", "restaurante": "restaurant",
    "restaurantes": "restaurants", "transporte": "transportation",
    "mobilidade": "mobility", "energia": "energy",
    "sustentabilidade": "sustainability", "ambiente": "environment",
    "juridico": "legal", "direito": "legal", "advogado": "lawyer",
    "advocacia": "legal",
    # Modelos / produto
    "assinatura": "subscription", "mensalidade": "subscription",
    "plataforma": "platform", "aplicativo": "app", "aplicacao": "application",
    "software": "software", "solucao": "solution", "solucoes": "solutions",
    "ferramenta": "tool", "ferramentas": "tools", "automacao": "automation",
    "gestao": "management", "gerenciamento": "management",
    # Operações / negócio
    "empresa": "company", "empresas": "companies", "negocio": "business",
    "negocios": "business", "cliente": "customer", "clientes": "customers",
    "usuario": "user", "usuarios": "users", "produto": "product",
    "produtos": "products", "servico": "service", "servicos": "services",
    "receita": "revenue", "faturamento": "revenue", "vendas": "sales",
    "venda": "sale", "crescimento": "growth",
    # Funding / time
    "fundador": "founder", "fundadores": "founders",
    "cofundador": "cofounder", "equipe": "team", "funcionario": "employee",
    "funcionarios": "employees", "investimento": "investment",
    "investidor": "investor", "investidores": "investors",
    "captacao": "fundraise", "rodada": "round", "aporte": "investment",
    # Causa-like
    "fracasso": "failure", "falencia": "bankruptcy", "fechou": "closed",
    "encerrou": "closed", "morreu": "died", "quebrou": "failed",
    "faliu": "failed", "dinheiro": "money", "caixa": "cash",
    "mercado": "market", "concorrencia": "competition",
    "concorrente": "competitor", "concorrentes": "competitors",
    # Industry-specific
    "fintech": "fintech", "biotech": "biotech",
    "pme": "sme", "pmes": "smes",
    # Finance detalhado
    "credito": "credit", "emprestimo": "loan", "emprestimos": "loans",
    "cartao": "card", "conta": "account", "banco": "bank", "bancos": "banks",
    "neobank": "neobank", "corretora": "brokerage", "bolsa": "exchange",
    "bancaria": "banking", "bancario": "banking",
    # Health detalhado
    "medico": "doctor", "medicos": "doctors", "clinica": "clinic",
    "hospital": "hospital", "paciente": "patient", "pacientes": "patients",
    "farmacia": "pharmacy", "remedio": "medicine",
    # HR / recruiting
    "recrutamento": "recruiting", "contratacao": "hiring",
    "recrutador": "recruiter", "rh": "hr",
    # E-commerce
    "loja": "store", "lojas": "stores", "compras": "shopping",
    "carrinho": "cart", "marketplace": "marketplace",
    # Tech genéricas
    "nuvem": "cloud", "dados": "data", "api": "api",
    "desenvolvedor": "developer", "desenvolvedores": "developers",
    "programador": "programmer", "programacao": "programming",
    # Marketing
    "publicidade": "advertising", "anuncio": "ad", "anuncios": "ads",
    "campanha": "campaign", "engajamento": "engagement",
    # Sustentabilidade
    "limpa": "clean", "renovavel": "renewable", "carbono": "carbon",
    # Ampliações para cobrir termos comuns faltantes
    "cobranca": "billing", "fatura": "invoice", "faturas": "invoices",
    "locatario": "tenant", "inquilino": "tenant", "proprietario": "landlord",
    "aluguel": "rental", "locacao": "rental", "fornecedor": "supplier",
    "fornecedores": "suppliers", "atacado": "wholesale", "atacadista": "wholesale",
    "entrega": "delivery", "entregas": "deliveries", "frete": "shipping",
    "estoque": "inventory", "armazem": "warehouse", "pedido": "order",
    "pedidos": "orders", "reserva": "booking", "reservas": "bookings",
    "agendamento": "scheduling", "agenda": "calendar",
    "prontuario": "records", "exame": "exam", "exames": "exams",
    "receita medica": "prescription", "consulta": "appointment",
    "moradia": "housing", "condominio": "condominium",
    "caminhao": "truck", "caminhoes": "trucks", "motorista": "driver",
    "motoristas": "drivers", "onibus": "bus", "taxi": "taxi",
    "bicicleta": "bike", "patinete": "scooter",
    "fazenda": "farm", "lavoura": "crop", "pecuaria": "livestock",
    "produtor": "producer", "produtores": "producers", "colheita": "harvest",
    "trabalhador": "worker", "trabalhadores": "workers", "vaga": "job",
    "salario": "salary", "beneficio": "benefit", "beneficios": "benefits",
    "imposto": "tax", "impostos": "taxes", "tributo": "tax",
    "poupanca": "savings", "dividida": "debt", "divida": "debt", "dividas": "debt",
    "web3": "web3", "cripto": "crypto", "criptomoeda": "cryptocurrency",
    "token": "token", "blockchain": "blockchain", "nft": "nft",
    "agente": "agent", "agentes": "agents", "chatbot": "chatbot",
    "assistente": "assistant", "assistentes": "assistants",
}


# ─── Espanhol → Inglês (conjunto focado em termos de startup) ─────────────────
ES_MULTIWORD = {
    "medio ambiente": "environment",
    "recursos humanos": "human resources",
    "inteligencia artificial": "artificial intelligence",
    "aprendizaje automatico": "machine learning",
    "cadena de suministro": "supply chain",
    "bienes raices": "real estate",
    "tarjeta de credito": "credit card",
    "comercio electronico": "ecommerce",
    "estados unidos": "united states",
}

ES_TO_EN_TERMS = {
    "salud": "health", "finanzas": "finance", "pagos": "payments",
    "pago": "payment", "seguro": "insurance", "seguros": "insurance",
    "educacion": "education", "ensenanza": "education",
    "estudiante": "student", "estudiantes": "students",
    "universidad": "university", "escuela": "school",
    "empresa": "company", "empresas": "companies", "negocio": "business",
    "negocios": "business", "cliente": "customer", "clientes": "customers",
    "usuario": "user", "usuarios": "users", "producto": "product",
    "productos": "products", "servicio": "service", "servicios": "services",
    "mercado": "market", "ventas": "sales", "venta": "sale",
    "fundador": "founder", "fundadores": "founders", "equipo": "team",
    "empleado": "employee", "empleados": "employees",
    "inversion": "investment", "inversor": "investor",
    "comida": "food", "restaurante": "restaurant",
    "transporte": "transportation", "logistica": "logistics",
    "plataforma": "platform", "aplicacion": "app", "aplicaciones": "apps",
    "programa": "software", "solucion": "solution", "herramienta": "tool",
    "gestion": "management", "automatizacion": "automation",
    "nube": "cloud", "datos": "data", "desarrollador": "developer",
    "desarrolladores": "developers", "tienda": "store", "compras": "shopping",
    "viajes": "travel", "turismo": "travel", "alojamiento": "accommodation",
    "trabajo": "work", "empleo": "employment", "reclutamiento": "recruiting",
    "contratacion": "hiring", "abogado": "lawyer", "juridico": "legal",
    "medico": "doctor", "clinica": "clinic", "paciente": "patient",
    "energia": "energy", "sostenibilidad": "sustainability",
    "agricultura": "agriculture", "mexico": "mexico",
    "espana": "spain", "argentina": "argentina",
    "fracaso": "failure", "quiebra": "bankruptcy", "cerro": "closed",
    "dinero": "money", "efectivo": "cash", "competencia": "competition",
    "alquiler": "rental", "inmobiliaria": "real estate",
    "fintech": "fintech", "comercio": "commerce",
}


# ─── Francês → Inglês (conjunto focado em termos de startup) ──────────────────
FR_MULTIWORD = {
    "intelligence artificielle": "artificial intelligence",
    "apprentissage automatique": "machine learning",
    "chaine logistique": "supply chain",
    "immobilier": "real estate",
    "carte de credit": "credit card",
    "ressources humaines": "human resources",
    "etats unis": "united states",
}

FR_TO_EN_TERMS = {
    "sante": "health", "finance": "finance", "paiement": "payment",
    "paiements": "payments", "assurance": "insurance",
    "education": "education", "enseignement": "education",
    "etudiant": "student", "etudiants": "students",
    "universite": "university", "ecole": "school",
    "entreprise": "company", "entreprises": "companies", "affaires": "business",
    "client": "customer", "clients": "customers",
    "utilisateur": "user", "utilisateurs": "users", "produit": "product",
    "produits": "products", "service": "service", "services": "services",
    "marche": "market", "ventes": "sales",
    "fondateur": "founder", "fondateurs": "founders", "equipe": "team",
    "employe": "employee", "employes": "employees",
    "investissement": "investment", "investisseur": "investor",
    "nourriture": "food", "restaurant": "restaurant",
    "transport": "transportation", "logistique": "logistics",
    "plateforme": "platform", "application": "app", "applications": "apps",
    "logiciel": "software", "outil": "tool", "outils": "tools",
    "gestion": "management", "automatisation": "automation",
    "nuage": "cloud", "donnees": "data", "developpeur": "developer",
    "magasin": "store", "voyages": "travel", "tourisme": "travel",
    "travail": "work", "emploi": "employment", "recrutement": "recruiting",
    "avocat": "lawyer", "juridique": "legal", "medecin": "doctor",
    "clinique": "clinic", "patient": "patient", "energie": "energy",
    "agriculture": "agriculture", "france": "france",
    "echec": "failure", "faillite": "bankruptcy", "ferme": "closed",
    "argent": "money", "concurrence": "competition", "location": "rental",
    "fintech": "fintech",
}


# ─── Taxonomias ──────────────────────────────────────────────────────────────

CAUSE_TAXONOMY = {
    "1":  ("Lack of Funds",          "Fluxo de caixa apertado / dificuldade em captar rodada"),
    "2":  ("No Market Need",         "Falta de demanda real / pessoas não querem isso"),
    "3":  ("Bad Market Fit",         "Mercado escolhido não comporta o produto"),
    "4":  ("Bad Business Model",     "Modelo de negócio não fecha contas / margens ruins"),
    "5":  ("Bad Marketing",          "Falha em canais de aquisição / marketing fraco"),
    "6":  ("Poor Product",           "Produto não entrega valor / qualidade / performance"),
    "7":  ("Bad Management",         "Gestão / liderança / conflitos entre founders"),
    "8":  ("Mismanagement of Funds", "Má alocação de capital / overspend / queima alta"),
    "9":  ("Competition",            "Concorrência forte / players estabelecidos"),
    "10": ("Multiple Reasons",       "Várias frentes erradas ao mesmo tempo"),
    "11": ("Lack of Focus",          "Falta de foco / escopo se espalha demais"),
    "12": ("Lack of Experience",     "Time inexperiente no domínio / sem expertise"),
    "13": ("Legal Challenges",       "Problemas legais / regulatórios"),
    "14": ("Bad Timing",             "Mercado cedo/tarde demais"),
    "15": ("Dependence on Others",   "Dependência de plataforma/parceiro único"),
}

BUSINESS_MODELS = {
    "1":  "SaaS",
    "2":  "B2B",
    "3":  "B2C",
    "4":  "B2B2C",
    "5":  "Marketplace",
    "6":  "Agency",
    "7":  "e-Commerce",
    "8":  "App",
    "9":  "Hardware",
    "10": "Subscription",
    "11": "Outro",
}


# ─── Macro-segmentos (unifica taxonomias divergentes entre fontes) ───────────
# Mesmo macro entre user e empresa = sinal de mesmo segmento, mesmo que as
# palavras-chave sejam diferentes.
CATEGORY_MACROS = {
    "finance":      ["fintech", "finance", "finances", "financial", "banking",
                     "bank", "payments", "payment", "insurance", "insurtech",
                     "lending", "loan", "credit", "investing", "investment",
                     "wealth", "trading", "crypto", "blockchain", "defi",
                     "finops", "accounting", "neobank", "regtech", "tax",
                     # PT
                     "financas", "financeiro", "financeira", "pagamento",
                     "pagamentos", "bancario", "bancaria", "credito",
                     "emprestimo", "conta digital", "corretora", "seguro",
                     "seguros", "tributos", "contabilidade", "investimentos"],
    "software":     ["saas", "software", "software & hardware", "b2b", "b2b2c",
                     "dev tools", "developer tools", "devtools", "devops",
                     "infrastructure", "api", "platform", "enterprise", "cloud",
                     "workflow", "automation", "no-code", "low-code", "no code",
                     "low code", "paas", "iaas", "middleware",
                     "plg", "product-led", "api-first", "open source",
                     "open-source", "sdk", "framework", "ide", "git",
                     # PT
                     "plataforma", "aplicativo", "aplicacao", "ferramenta",
                     "nuvem", "automacao", "codigo aberto"],
    "ai":           ["ai", "artificial intelligence", "generative ai", "genai",
                     "machine learning", "ml", "computer vision", "nlp",
                     "llm", "llms", "deep learning", "robotics", "neural",
                     "ai agent", "ai agents", "agentic", "agents",
                     "prompt engineering", "rag", "foundation model",
                     "mlops", "transformer", "chatbot", "copilot",
                     # PT
                     "inteligencia artificial", "aprendizado de maquina",
                     "visao computacional", "rede neural", "agente de ia",
                     "assistente virtual"],
    "web3":         ["web3", "crypto", "cryptocurrency", "blockchain", "defi",
                     "dao", "nft", "nfts", "token", "tokens", "smart contract",
                     "smart contracts", "l2", "rollup", "zk", "ethereum",
                     "solana", "bitcoin", "wallet", "dex",
                     # PT
                     "cripto", "criptomoeda", "contrato inteligente",
                     "carteira digital"],
    "ecommerce":    ["e-commerce", "ecommerce", "e commerce", "retail",
                     "marketplace", "shopping", "dtc", "d2c", "direct to consumer",
                     "consumer goods", "online store", "checkout", "fulfillment",
                     "drop shipping", "dropshipping", "cpg", "wholesale",
                     # PT
                     "varejo", "loja virtual", "comercio eletronico", "compras",
                     "atacado", "venda direta"],
    "social":       ["social", "social media", "social network", "community",
                     "messaging", "chat", "dating", "forum", "creator",
                     # PT
                     "rede social", "mensageria", "comunidade", "bate-papo"],
    "food":         ["food", "food & beverage", "food and beverage", "beverage",
                     "restaurants", "restaurant", "grocery", "food delivery",
                     "meal", "kitchen", "dining",
                     # PT
                     "alimentos", "alimenticio", "bebidas", "restaurante",
                     "restaurantes", "delivery de comida", "mercearia"],
    "transport":    ["transportation", "transport", "logistics", "mobility",
                     "delivery", "shipping", "automotive", "rideshare",
                     "autonomous", "fleet",
                     # PT
                     "transporte", "logistica", "mobilidade", "entrega",
                     "frete", "automotivo"],
    "marketing":    ["marketing", "advertising", "adtech", "growth", "email",
                     "seo", "content marketing", "martech", "crm", "campaign",
                     # PT
                     "publicidade", "anuncios", "campanhas", "engajamento"],
    "media":        ["media", "entertainment", "music", "video", "streaming",
                     "content", "gaming", "games", "podcast", "news", "film",
                     # PT
                     "midia", "entretenimento", "musica", "jogos",
                     "streaming de video", "noticias"],
    "education":    ["education", "edtech", "learning", "online learning",
                     "tutoring", "school", "university", "student", "course",
                     "mooc", "training",
                     # PT
                     "educacao", "ensino", "aprendizado", "escola",
                     "universidade", "curso", "cursos", "tutoria",
                     "estudantes", "estagio"],
    "health":       ["health", "healthcare", "healthtech", "medical", "medicine",
                     "biotech", "pharma", "fitness", "wellness", "mental health",
                     "telemedicine", "clinical", "patient",
                     # PT
                     "saude", "medico", "clinica", "hospital", "paciente",
                     "farmacia", "bem-estar", "telemedicina"],
    "travel":       ["travel", "hospitality", "tourism", "hotel", "flights",
                     "booking", "vacation",
                     # PT
                     "viagem", "viagens", "turismo", "hospitalidade", "hotel"],
    "productivity": ["productivity", "collaboration", "project management",
                     "note taking", "calendar", "task management",
                     "remote work", "meetings",
                     # PT
                     "produtividade", "colaboracao", "gestao de projetos",
                     "trabalho remoto"],
    "analytics":    ["analytics", "data", "bi", "business intelligence",
                     "dashboards", "metrics", "observability", "data science",
                     # PT
                     "analise de dados", "inteligencia de negocio", "metricas"],
    "real_estate":  ["real estate", "proptech", "property", "housing",
                     "construction", "rental",
                     # PT
                     "imoveis", "imobiliario", "imobiliaria", "construcao",
                     "aluguel", "locacao"],
    "hr":           ["hr", "human resources", "recruiting", "recruitment",
                     "hiring", "talent", "workforce", "employee",
                     "people ops", "payroll",
                     # PT
                     "recursos humanos", "recrutamento", "contratacao",
                     "funcionario", "folha de pagamento"],
    "security":     ["security", "cybersecurity", "infosec", "privacy",
                     "identity", "authentication", "encryption",
                     # PT
                     "seguranca", "seguranca digital", "privacidade",
                     "autenticacao", "criptografia"],
    "hardware":     ["hardware", "iot", "devices", "robotics", "electronics",
                     "wearable", "sensor",
                     # PT
                     "dispositivos", "sensores", "eletronicos"],
    "sustainability": ["sustainability", "climate", "clean energy", "cleantech",
                       "greentech", "energy", "solar", "renewable", "carbon",
                       # PT
                       "sustentabilidade", "energia limpa", "energia renovavel",
                       "meio ambiente", "carbono"],
    "legal":        ["legal", "legaltech", "law", "compliance", "contracts",
                     # PT
                     "juridico", "direito", "advocacia", "contratos"],
    "design":       ["design", "creative", "art", "ux", "ui",
                     # PT
                     "criativo", "arte"],
    "agriculture":  ["agriculture", "agritech", "agtech", "farming", "crops",
                     # PT
                     "agricultura", "agronegocio", "agro", "lavoura", "pecuaria"],
}


# ─── Lexicon de causas para classificação automática ─────────────────────────
# Para cada causa da taxonomia, palavras/expressões que sugerem essa causa
# quando aparecem na `description`, `post_mortem`, ou `one_liner`.
# Matching é substring case-insensitive sobre texto normalizado.
CAUSE_LEXICON = {
    "Lack of Funds": [
        "ran out of money", "ran out of cash", "ran out of funds",
        "couldn't raise", "could not raise", "unable to raise",
        "failed to raise", "funding dried", "no more capital", "out of runway",
        "couldn't secure funding", "failed fundraise",
    ],
    "No Market Need": [
        "no market need", "no demand", "nobody wanted", "no one wanted",
        "users didn't want", "no real need", "solution looking for a problem",
        "didn't solve a real problem", "no product-market fit",
        "no product market fit", "lacked demand",
    ],
    "Bad Market Fit": [
        "market too small", "wrong market", "niche was too small",
        "market fit", "tam too small", "couldn't find product-market fit",
    ],
    "Bad Business Model": [
        "unit economics", "couldn't monetize", "couldn't make money",
        "margins too thin", "business model didn't work", "business model failed",
        "unprofitable", "couldn't turn a profit", "unable to charge",
        "pricing problem",
    ],
    "Bad Marketing": [
        "couldn't acquire users", "couldn't acquire customers",
        "acquisition was too expensive", "cac was too high",
        "couldn't get customers", "growth stalled", "bad marketing",
        "marketing failed", "no growth",
    ],
    "Poor Product": [
        "product was bad", "poor product", "product didn't work",
        "too many bugs", "quality issues", "product wasn't ready",
        "technical problems",
    ],
    "Bad Management": [
        "cofounder left", "co-founder left", "cofounder conflict",
        "founder conflict", "management issues", "team fell apart",
        "founder dispute", "founders fought",
    ],
    "Mismanagement of Funds": [
        "spent too much", "overspent", "burn rate too high", "burned through cash",
        "burned too much", "misallocated",
    ],
    "Competition": [
        "outcompeted", "bigger competitor", "lost to", "couldn't compete",
        "competition was too strong", "dominant player", "incumbents won",
    ],
    "Lack of Focus": [
        "too many pivots", "pivoted too many", "spread too thin",
        "lost focus", "scope creep", "tried to do too much",
    ],
    "Lack of Experience": [
        "first-time founder", "first time founder", "inexperienced",
        "didn't know the industry", "no domain expertise",
    ],
    "Legal Challenges": [
        "lawsuit", "regulatory", "regulation", "legal issue", "legal challenge",
        "banned", "compliance", "sued", "regulator", "illegal",
    ],
    "Bad Timing": [
        "too early", "too late", "ahead of its time", "timing was wrong",
        "market wasn't ready",
    ],
    "Dependence on Others": [
        "api changed", "platform changed", "depended on", "blocked by",
        "shut out of", "access revoked", "api deprecated", "kicked off",
    ],
}

# Status que indicam empresa morta (alvo principal do sistema).
DEAD_STATUSES = {"inactive", "dead", "failed", "shutdown", "shut down",
                 "closed", "defunct", "operating"}  # "operating" excluído abaixo
_DEAD = {"inactive", "dead", "failed", "shutdown", "shut down", "closed", "defunct"}

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "by", "is", "was", "are", "were", "be", "been", "being", "it",
    "its", "that", "this", "these", "those", "as", "from", "into", "about",
    "their", "they", "them", "we", "our", "you", "your", "he", "she", "his",
    "her", "i", "me", "my", "who", "what", "which", "so", "not", "no", "yes",
    "will", "would", "could", "should", "can", "may", "might", "has", "have",
    "had", "do", "does", "did", "than", "then", "there", "here", "when",
    "where", "why", "how", "all", "any", "some", "every", "more", "most",
    "other", "such", "only", "own", "same", "also", "just", "very", "like",
    "de", "da", "do", "para", "por", "com", "sem", "que", "uma", "um", "os",
    "as", "na", "no", "se", "foi", "ser", "ter", "mais", "ou", "sua", "seu",
}


# ─── Utils ───────────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "anon"


def _strip_accents(s: str) -> str:
    # Acentos são chatos pro matching com dicionário PT (digitado "saúde"
    # deve bater com a chave "saude"). Solução minimalista sem unicodedata:
    # mapa direto pros caracteres mais comuns em PT.
    mapping = str.maketrans({
        "á": "a", "à": "a", "â": "a", "ã": "a", "ä": "a",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "í": "i", "ì": "i", "î": "i", "ï": "i",
        "ó": "o", "ò": "o", "ô": "o", "õ": "o", "ö": "o",
        "ú": "u", "ù": "u", "û": "u", "ü": "u",
        "ç": "c", "ñ": "n",
        "Á": "A", "À": "A", "Â": "A", "Ã": "A",
        "É": "E", "Ê": "E", "Í": "I", "Ó": "O", "Ô": "O", "Õ": "O",
        "Ú": "U", "Ü": "U", "Ç": "C",
    })
    return s.translate(mapping)


def _damerau_leq1(a: str, b: str) -> bool:
    """
    True se a e b diferem por ≤ 1 edição: substituição, inserção, deleção,
    OU transposição de caracteres adjacentes (Damerau). Transposição é
    obrigatória pra pegar typos comuns tipo 'markteplace' ↔ 'marketplace'.
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        # substituição única OU transposição adjacente
        diffs = []
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                diffs.append(i)
                if len(diffs) > 2:
                    return False
        if len(diffs) <= 1:
            return True
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
            i, j = diffs
            return a[i] == b[j] and a[j] == b[i]
        return False
    # la != lb → inserção/deleção de 1 char. Garante a é o menor.
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = 0
    skipped = False
    while i < la and j < lb:
        if a[i] != b[j]:
            if skipped:
                return False
            skipped = True
            j += 1
        else:
            i += 1
            j += 1
    return True


# Mantém nome antigo como alias por segurança de callers futuros
_levenshtein_leq1 = _damerau_leq1


def fuzzy_correct(token: str, vocab: set[str]) -> str:
    """
    Se `token` não está no vocabulário, tenta achar uma correção dentro de
    edit-distance 1. Só aplica em tokens ≥ 5 chars pra não colapsar palavras
    curtas homógrafas ("saas" ≠ "sass"). Retorna o próprio token se nada bater.
    """
    if len(token) < 5 or token in vocab:
        return token
    # heurística barata: só candidatos de len ±1 e mesma 1ª letra
    first = token[0]
    target_lens = {len(token) - 1, len(token), len(token) + 1}
    best = None
    for v in vocab:
        if len(v) < 4 or len(v) not in target_lens or v[0] != first:
            continue
        if _damerau_leq1(token, v):
            best = v
            break
    return best or token


def tokenize(text: str, vocab: set | None = None,
             strip_negation: bool = True) -> list[str]:
    """
    Tokenizer multilíngue (PT/ES/FR → EN). Com `vocab`, ativa correção de
    typos — tokens órfãos (não estão no vocabulário do corpus nem nos dicts
    de tradução) são submetidos a fuzzy match de edit-distance 1.

    `strip_negation=True` (default pro input do user) remove tokens dentro
    da janela de negação. Nas empresas do corpus, use False — descrições
    em 3ª pessoa raramente têm negação relevante ao produto.
    """
    if not text:
        return []
    text = _strip_accents(text).lower()

    # 0) Remove spans negados antes de qualquer substituição
    if strip_negation:
        text = _strip_negated_spans(text)

    # 1) Substitui frases multi-palavra primeiro (ordem importa).
    for phrase_dict in (PT_MULTIWORD, ES_MULTIWORD, FR_MULTIWORD):
        for src, dst in phrase_dict.items():
            text = text.replace(src, dst)

    # 2) Extrai tokens.
    raw_tokens = re.findall(r"[a-z][a-z0-9]{2,}", text)

    # 3) Traduz token a token: tenta PT → ES → FR → identity. Primeiro hit vence.
    out: list[str] = []
    translated_from_foreign: list[bool] = []
    for t in raw_tokens:
        translated = False
        en = None
        for d in (PT_TO_EN_TERMS, ES_TO_EN_TERMS, FR_TO_EN_TERMS):
            if t in d:
                en = d[t]
                translated = True
                break
        if en is None:
            en = t
        if " " in en:
            for piece in en.split():
                out.append(piece)
                translated_from_foreign.append(translated)
        else:
            out.append(en)
            translated_from_foreign.append(translated)

    # 4) Filtra stopwords/curtos, e aplica fuzzy em tokens EN órfãos.
    filtered: list[str] = []
    for t, was_translated in zip(out, translated_from_foreign):
        if t in STOPWORDS or len(t) < 3:
            continue
        # Só tenta fuzzy em tokens que NÃO vieram de tradução (senão
        # corrigimos palavras já canônicas, colapsando dimensões).
        if vocab is not None and not was_translated and t not in vocab:
            t = fuzzy_correct(t, vocab)
        filtered.append(t)
    return filtered


def is_dead(status: str) -> bool:
    s = (status or "").lower().strip()
    return any(d in s for d in _DEAD)


# Buckets de outcome — usados pro benchmarking bidirecional.
# Base classifica em 4 destinos, e o sistema compara o perfil do user
# contra a distribuição dos peers por bucket.
#   dead      → morreu (inactive, deadpooled, defunct)
#   acquired  → foi adquirida (saída positiva, sobreviveu até virar alvo)
#   operating → ainda opera (seed..series_e, public) — sinal positivo
#   unknown   → unfunded / status vazio — outcome indeterminado
OPERATING_MARKERS = {"seed", "series", "public", "late stage", "ipo",
                     "operating", "active", "live"}


def outcome_bucket(c: dict) -> str:
    """Classifica a empresa em 1 dos 4 destinos possíveis. Prioridade:
    acquired → dead → operating → unknown (evita conflitos de status ambíguo)."""
    status = (c.get("status") or "").lower().strip()
    if "acquired" in status:
        return "acquired"
    if any(d in status for d in _DEAD):
        return "dead"
    if any(op in status for op in OPERATING_MARKERS):
        return "operating"
    return "unknown"


# ─── Detecção de negação (rudimentar mas cobre os casos comuns) ──────────────
# MiniLM não distingue "marketplace" de "not a marketplace" no embedding;
# e o TF-IDF trata os dois iguais. Solução: antes do tokenizer, pegamos
# trechos negados e removemos os tokens daí, pra não somar afirmação
# errada.
#
# Cobre casos: "not X", "no X", "never X", "não X", "sem X", "nunca X",
# "n't X" (contrações). Janela = próximas 3 palavras depois do negador.
NEGATION_MARKERS = {
    "not", "no", "never", "n't",
    "nao", "sem", "nunca", "ninguem",  # pt
    "nunca jamais",
    "nunca jamais",
    "ni", "nadie", "ningun",  # es
    "pas", "aucun", "jamais", "ni",  # fr
}
NEGATION_WINDOW = 3  # palavras afetadas após a marca

def _strip_negated_spans(text: str) -> str:
    """
    Remove tokens dentro de uma janela curta após marcadores de negação.
    Conservador: prefere perder sinal a afirmar o inverso.
    Ex.: "not a marketplace for food" → "food" (tira "marketplace")
    """
    words = text.split()
    if not words:
        return text
    out = []
    skip = 0
    for w in words:
        # limpa pontuação pra comparar
        bare = re.sub(r"[^a-z0-9']", "", w)
        if skip > 0:
            skip -= 1
            # mantém stopwords do conector (a, an, the) pra não afetar parsing
            if bare in {"a", "an", "the", "um", "uma", "o", "os", "as"}:
                continue
            continue  # pula este token
        if bare in NEGATION_MARKERS or bare.endswith("n't"):
            skip = NEGATION_WINDOW
            continue
        out.append(w)
    return " ".join(out)


# ─── Macro-segmentos ─────────────────────────────────────────────────────────

def categories_to_macros(categories: list[str]) -> set[str]:
    """Mapeia lista de categorias → conjunto de macros. Aceita PT e EN."""
    if not categories:
        return set()
    out = set()
    for cat in categories:
        low = _strip_accents((cat or "").lower().strip())
        if not low:
            continue
        for macro, keywords in CATEGORY_MACROS.items():
            for kw in keywords:
                if kw in low or low in kw:
                    out.add(macro)
                    break
    return out


# ─── Classificação de causa por lexicon ──────────────────────────────────────

def infer_cause(texts: list[str]) -> tuple[str, str, str]:
    """
    Retorna (cause, evidence_snippet, confidence).
    - cause: nome canônico da taxonomia ou "" se nada bateu
    - evidence: frase original onde casou (para o relatório)
    - confidence: "high" se 1+ match forte, "low" caso contrário
    """
    joined = " ".join(t for t in texts if t).lower()
    if len(joined) < 20:
        return "", "", ""

    hits: Counter = Counter()
    evidence: dict[str, str] = {}

    for cause, keywords in CAUSE_LEXICON.items():
        for kw in keywords:
            if kw in joined:
                hits[cause] += 1
                if cause not in evidence:
                    # extrai ~120 chars ao redor do match como trecho de evidência
                    idx = joined.find(kw)
                    start = max(0, idx - 40)
                    end = min(len(joined), idx + len(kw) + 60)
                    evidence[cause] = "…" + joined[start:end].strip() + "…"

    if not hits:
        return "", "", ""

    if len(hits) >= 3:
        top = "Multiple Reasons"
        ev = next(iter(evidence.values()), "")
        return top, ev, "medium"

    top_cause, top_count = hits.most_common(1)[0]
    conf = "high" if top_count >= 2 else "low"
    return top_cause, evidence.get(top_cause, ""), conf


# ─── Qualidade do input & coerência (camadas de confiabilidade) ──────────────
# Por que: cause/model auto-declarados têm peso grande no scoring, mas o user
# marca num dropdown sem necessariamente descrever o problema. Se o TEXTO
# livre não corrobora a auto-declaração, baixamos a confiança naquela
# dimensão em vez de confiar cegamente. Também: input curto → camada
# semântica fica ruidosa, então reduzimos seu peso proporcionalmente.

# Keywords que esperamos ver no one-liner/notes quando o user declara cada
# business model. Não precisa bater TODAS, mas pelo menos 1.
BUSINESS_MODEL_KEYWORDS = {
    "saas":         {"saas", "subscription", "software", "platform", "cloud",
                     "assinatura"},
    "marketplace":  {"marketplace", "match", "connect", "platform", "two-sided",
                     "vendedores", "compradores"},
    "b2b":          {"b2b", "enterprise", "business", "company", "companies",
                     "empresa", "empresas"},
    "b2c":          {"b2c", "consumer", "users", "user", "customer",
                     "usuario", "usuarios", "pessoas"},
    "b2b2c":        {"b2b2c", "platform", "enterprise", "consumer"},
    "e-commerce":   {"ecommerce", "commerce", "shop", "store", "retail", "buy",
                     "sell", "loja", "varejo"},
    "app":          {"app", "mobile", "ios", "android", "aplicativo"},
    "hardware":     {"hardware", "device", "devices", "iot", "sensor",
                     "dispositivo"},
    "agency":       {"agency", "services", "consulting", "consultoria"},
    "subscription": {"subscription", "recurring", "monthly", "assinatura",
                     "mensalidade"},
}


def assess_input_quality(user: dict, user_tokens: list[str]) -> dict:
    """
    Avalia se o input do user tem substância suficiente pra gerar
    recomendações confiáveis. Retorna flags e um fator de confiança
    semântica (0..1) que é multiplicado no peso da dimensão semântica.
    """
    unique_tokens = set(user_tokens)
    n = len(unique_tokens)
    desc_len = len((user.get("one_liner") or "") + " " + (user.get("notes") or ""))

    warnings: list[str] = []
    semantic_trust = 1.0
    short_input = False

    if n < 5:
        warnings.append(
            f"descrição muito curta ({n} termos significativos depois de tokenizar); "
            "recomendações podem ser instáveis — tente ampliar o one-liner "
            "ou adicionar observações no campo notes"
        )
        short_input = True
        semantic_trust = 0.4
    elif n < 10:
        warnings.append(
            f"descrição parcial ({n} termos); a camada semântica roda com "
            "peso reduzido pra compensar o vetor pobre"
        )
        semantic_trust = 0.7

    if desc_len < 30:
        warnings.append(
            "one-liner+notes somam menos de 30 caracteres; com texto tão curto, "
            "o ranking depende quase só de categorias/país/causa auto-declarados"
        )

    return {
        "short_input": short_input,
        "warnings": warnings,
        "semantic_trust": semantic_trust,
        "n_user_tokens": n,
    }


def coherence_check(user: dict, user_tokens_set: set[str]) -> dict:
    """
    Detecta auto-declarações que o texto livre NÃO corrobora. Não impede
    matching; sinaliza e reduz o peso local daquela dimensão.

    - business_coherent=False → W_BUSINESS vira 0 ou metade (o user marcou
      'SaaS' mas o texto descreve um marketplace, por exemplo)
    - cause_corroborated=False → W_CAUSE_SAME vira metade (cause é checkbox;
      se o texto não menciona nada relacionado àquela causa, trate como
      sinal mais fraco)
    """
    warnings: list[str] = []
    bm = (user.get("business_model") or "").strip().lower()
    expected = BUSINESS_MODEL_KEYWORDS.get(bm, set())
    business_coherent = True
    if expected and user_tokens_set:
        if not (expected & user_tokens_set):
            business_coherent = False
            warnings.append(
                f"você declarou modelo '{user.get('business_model')}' mas "
                f"nenhum dos termos esperados aparece no texto "
                f"({', '.join(sorted(expected)[:3])}…). Peso dessa dimensão reduzido."
            )

    cause = user.get("main_concern", "")
    cause_corroborated = True
    if cause and user_tokens_set:
        cause_vocab: set[str] = set()
        for kw in CAUSE_LEXICON.get(cause, []):
            for piece in kw.split():
                if piece not in STOPWORDS and len(piece) >= 3:
                    cause_vocab.add(piece)
        if cause_vocab and not (cause_vocab & user_tokens_set):
            cause_corroborated = False
            # NÃO é warning pro user — causa é legitimamente um checkbox.
            # Mas o peso interno da dimensão será atenuado se vier isolada.

    return {
        "business_coherent": business_coherent,
        "cause_corroborated": cause_corroborated,
        "warnings": warnings,
    }


# ─── TF-IDF (puro Python, sem deps) ──────────────────────────────────────────

def build_tfidf_index(companies: list[dict]) -> dict:
    """Retorna { 'idf': {term: idf}, 'vectors': {idx: {term: tfidf}} }."""
    N = len(companies)
    df: Counter = Counter()
    doc_tokens: list[list[str]] = []

    for c in companies:
        txt = " ".join([
            c.get("description") or "",
            c.get("one_liner") or "",
            c.get("post_mortem") or "",
            c.get("name") or "",
            (c.get("rich_narrative") or "")[:4000],
        ])
        tokens = tokenize(txt, strip_negation=False)
        doc_tokens.append(tokens)
        for t in set(tokens):
            df[t] += 1

    idf = {t: math.log((1 + N) / (1 + d)) + 1.0 for t, d in df.items()}
    vectors: dict[int, dict[str, float]] = {}

    for i, tokens in enumerate(doc_tokens):
        if not tokens:
            vectors[i] = {}
            continue
        tf: Counter = Counter(tokens)
        vec = {t: (count / len(tokens)) * idf.get(t, 0.0) for t, count in tf.items()}
        # L2 normalize
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors[i] = {t: v / norm for t, v in vec.items()}

    return {"idf": idf, "vectors": vectors}


def vectorize(text: str, idf: dict) -> dict[str, float]:
    tokens = tokenize(text)
    if not tokens:
        return {}
    tf: Counter = Counter(tokens)
    vec = {t: (count / len(tokens)) * idf.get(t, 0.0) for t, count in tf.items() if t in idf}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


# ─── Camada semântica (sentence-transformers, multilingual) ──────────────────
# Por que: TF-IDF é lexical ("pagamentos" ≠ "payments" por default, mesmo com
# dict PT→EN; e produtos descritos com outras palavras não casam). Embeddings
# multilingues resolvem: "app pra estudantes acharem estágio" vai ter vetor
# próximo de "platform matching students with internship opportunities".
#
# Modelo: paraphrase-multilingual-MiniLM-L12-v2
#   - 118MB download (HuggingFace cache em ~/.cache/huggingface/)
#   - suporta 50+ idiomas, inclusive português
#   - embeddings de 384 dimensões
#   - CPU: ~2-5s para encode de 1350 textos curtos
#
# Cache em disco: output/company_embeddings.npz — regenerado se invalidado.

def _first_sentence(s: str, max_chars: int = 180) -> str:
    """Primeira frase (até o 1º ponto) ou truncagem. Descrições longas
    diluem o embedding — a 1ª frase é quase sempre 'o que a empresa é'."""
    if not s:
        return ""
    s = s.strip()
    m = re.search(r"\.(?:\s|$)", s[:max_chars + 20])
    if m:
        return s[:m.start() + 1]
    return s[:max_chars]


def _build_semantic_text(c: dict) -> str:
    """
    Texto da empresa passado pro encoder. CURTO e FOCADO — cosine degrada
    rápido com texto longo (experimento: 87 chars → 0.67 ; 261 chars → 0.35
    pro mesmo par). MiniLM também limita a 128 tokens.
    """
    name = (c.get("name") or "").strip()
    one = (c.get("one_liner") or "").strip()
    desc_first = _first_sentence(c.get("description") or "", 180)
    # Se tem rich_narrative (startups.rip), usa primeira frase dela em vez da
    # description quando esta for vazia.
    if not one and not desc_first:
        desc_first = _first_sentence(c.get("rich_narrative") or "", 180)

    parts = [name]
    if one:
        parts.append(one)
    if desc_first:
        parts.append(desc_first)
    return " ".join(parts).strip()[:400]


def build_semantic_index(companies: list[dict], force: bool = False) -> dict | None:
    """
    Retorna {"vectors": np.ndarray [N, 384], "model": SentenceTransformer|None}
    ou None se a lib não estiver disponível.
    """
    if not SEMANTIC_AVAILABLE:
        print("[semantic] camada semântica DESATIVADA "
              "(pip install sentence-transformers numpy para habilitar).")
        print("           O sistema funciona sem ela, mas perde sinônimos "
              "cross-language e conceitos parecidos.")
        return None

    N = len(companies)

    # tenta cache
    if os.path.exists(SEMANTIC_CACHE_PATH) and not force:
        try:
            data = np.load(SEMANTIC_CACHE_PATH, allow_pickle=False)
            vecs = data["vectors"]
            if vecs.shape[0] == N:
                print(f"[semantic] cache hit: {vecs.shape[0]} vetores de {vecs.shape[1]} dims")
                return {"vectors": vecs, "model": None}
            print(f"[semantic] cache mismatch ({vecs.shape[0]} != {N}); rebuild")
        except Exception as e:
            print(f"[semantic] cache inválido ({e}); rebuild")

    print(f"[semantic] gerando embeddings pra {N} empresas "
          "(1ª vez: baixa modelo ~120MB; próximas: ~30s em CPU)")
    model = SentenceTransformer(SEMANTIC_MODEL_NAME)
    texts = [_build_semantic_text(c) for c in companies]

    vectors = model.encode(
        texts,
        batch_size=32,
        show_progress_bar=True,
        normalize_embeddings=True,   # L2-normaliza pra cosine = dot product
        convert_to_numpy=True,
    )

    np.savez_compressed(SEMANTIC_CACHE_PATH, vectors=vectors)
    print(f"[semantic] cache gravado em {SEMANTIC_CACHE_PATH} "
          f"({vectors.shape[0]} x {vectors.shape[1]})")
    return {"vectors": vectors, "model": model}


def semantic_encode_user(user: dict, semantic_index: dict | None):
    """Gera vetor do user. Lazy-load do modelo se só o cache veio do disco."""
    if semantic_index is None:
        return None
    model = semantic_index.get("model")
    if model is None:
        print("[semantic] carregando modelo pra encode do user…")
        model = SentenceTransformer(SEMANTIC_MODEL_NAME)
        semantic_index["model"] = model

    # Texto FOCADO: nome + one_liner + categorias (omite notes pra não
    # poluir o embedding com metacomentário do user).
    text = " ".join([
        user.get("name", ""),
        user.get("one_liner", ""),
        " ".join(user.get("categories", [])),
    ]).strip()[:400]
    if not text:
        return None

    vec = model.encode([text], normalize_embeddings=True, convert_to_numpy=True)[0]
    return vec


# ─── Enriquecimento (idempotente com cache em disco) ─────────────────────────

def _flatten_text(val) -> str:
    """Achata dict/list/str numa string pra busca textual."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return " ".join(_flatten_text(v) for v in val)
    if isinstance(val, dict):
        return " ".join(_flatten_text(v) for v in val.values())
    return str(val)


def _load_startups_rip_text_by_name() -> dict[str, str]:
    """
    Monta { normalized_name → texto narrativo bruto } para cada empresa do
    startups.rip. Texto inclui overview, post_mortem, founding_story,
    what_they_built, market_position, business_model, timeline, traction,
    e os valores de all_sections_raw — tudo que possa conter menção a motivo
    de falha.
    """
    if not os.path.exists(RAW_STARTUPS_RIP_PATH):
        return {}
    with open(RAW_STARTUPS_RIP_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # Heurística: se o texto contém "does not have a completed analysis" ou
    # "Use one of your 5 monthly Pro research" é paywall — descartamos.
    PAYWALL_MARKERS = [
        "does not have a completed analysis",
        "monthly pro research",
        "commission the",
    ]

    out: dict[str, str] = {}
    for c in raw:
        name = c.get("name", "")
        # remove " (Winter 2012)" etc.
        clean_name = re.sub(r"\s*\(.*?\)\s*$", "", name)
        norm = normalize(clean_name) or normalize(name)
        if not norm:
            continue

        chunks = []
        for key in ("overview", "post_mortem", "founding_story", "what_they_built",
                    "market_position", "business_model", "timeline", "traction",
                    "key_lessons"):
            v = _flatten_text(c.get(key))
            if v:
                chunks.append(v)

        sections_txt = _flatten_text(c.get("all_sections_raw"))
        if sections_txt:
            low = sections_txt.lower()
            if not any(m in low for m in PAYWALL_MARKERS):
                chunks.append(sections_txt)

        full = " ".join(chunks).strip()
        if len(full) > 80:
            # indexa nas duas formas pra evitar miss no join
            out[norm] = full
            full_norm = normalize(name)
            if full_norm and full_norm != norm:
                out[full_norm] = full
    return out


# ─── Inferência de padrão de falha (sem narrative explícito) ────────────────
#
# Quando outcome == dead mas não temos post_mortem / failure_cause, ainda dá pra
# inferir um "padrão" a partir de sinais estruturais: tempo de vida, densidade
# da cohort que morreu junto, presença de capital vs morte jovem, dispersão de
# categorias, tamanho do founding team. Não é a causa; é o formato da falha.

_LIFESPAN_BUCKETS = [
    (0, 2, "early(<2y)", "early_death",
     "Padrão compatível com falha de PMF ou caixa esgotado antes da tração."),
    (2, 5, "mid(2-5y)", "scale_failure",
     "Atingiu operação mas não conseguiu escalar sustentavelmente; faixa típica de "
     "CAC acima do LTV ou segunda rodada que não veio."),
    (5, 10, "mature(5-10y)", "market_disruption",
     "Durou o suficiente pra construir tração real; falha nessa faixa costuma indicar "
     "comoditização, disrupção de mercado ou incapacidade de evoluir o produto."),
    (10, 999, "late(>10y)", "long_tail",
     "Empresa madura que foi definhando; padrão consistente com decadência lenta ou "
     "obsolescência tecnológica."),
]


def _build_cohort_index(companies: list[dict]) -> dict:
    """
    Índice: (macro, decade_founded, country_lower) -> [shutdown_years…] de quem morreu.
    Usado pra detectar cohort collapse (vários peers morrendo em janela apertada).
    """
    idx: dict[tuple, list[int]] = {}
    for c in companies:
        if c.get("outcome") != "dead":
            continue
        sy_raw = c.get("shutdown_year") or ""
        fy_raw = c.get("founded_year") or ""
        try:
            sy = int(str(sy_raw)[:4])
            fy = int(str(fy_raw)[:4])
        except (ValueError, TypeError):
            continue
        if sy < fy or sy < 1900 or sy > 2100:
            continue
        decade = (fy // 10) * 10
        country = (c.get("country") or "").strip().lower()
        macros = c.get("category_macros") or ["unknown"]
        for m in macros[:3]:
            idx.setdefault((m, decade, country), []).append(sy)
    return idx


def infer_failure_pattern(c: dict, cohort_idx: dict) -> Optional[dict]:
    """
    Deriva um 'formato' da falha baseado em sinais estruturais. Retorna None se
    a empresa não morreu ou se não há dados suficientes. Nunca sobrescreve
    failure_cause documentado — coexiste como sinal complementar.
    """
    if c.get("outcome") not in ("dead", "acquired"):
        return None

    labels: list[str] = []
    lifespan: Optional[int] = None
    bucket = "unknown"
    bucket_narrative = ""
    try:
        fy = int(str(c.get("founded_year", ""))[:4])
        sy = int(str(c.get("shutdown_year", ""))[:4])
        if sy >= fy and sy >= 1900:
            lifespan = sy - fy
    except (ValueError, TypeError):
        pass

    if lifespan is not None:
        for lo, hi, label_b, label_l, narr in _LIFESPAN_BUCKETS:
            if lo <= lifespan < hi:
                bucket = label_b
                labels.append(label_l)
                bucket_narrative = narr
                break

    investors = c.get("investors") or []
    funding = (c.get("total_funding") or "").strip()
    has_capital = bool(investors) or bool(funding)
    if has_capital and "early_death" in labels:
        labels.append("capital_paradox")

    cats = c.get("categories") or []
    if len(cats) >= 4:
        labels.append("scope_drift")

    founders = c.get("founders") or []
    if len(founders) == 1 and ("early_death" in labels or "scale_failure" in labels):
        labels.append("solo_founder")

    if c.get("outcome") == "acquired" and lifespan is not None and lifespan < 3:
        labels.append("acqui_hire")

    peers_co_dying = 0
    cohort_size = 0
    try:
        fy_i = int(str(c.get("founded_year", ""))[:4])
        sy_i = int(str(c.get("shutdown_year", ""))[:4])
        decade = (fy_i // 10) * 10
        country = (c.get("country") or "").strip().lower()
        for m in (c.get("category_macros") or [])[:3]:
            peers = cohort_idx.get((m, decade, country), [])
            nearby = sum(1 for y in peers if abs(y - sy_i) <= 2)
            if nearby > peers_co_dying:
                peers_co_dying = nearby
                cohort_size = len(peers)
    except (ValueError, TypeError):
        pass
    if peers_co_dying >= 10:
        labels.append("cohort_collapse")

    parts: list[str] = []
    if lifespan is not None:
        parts.append(f"Fechou {lifespan} ano{'s' if lifespan != 1 else ''} após fundação ({bucket}).")
    if bucket_narrative:
        parts.append(bucket_narrative)
    if "capital_paradox" in labels:
        parts.append(
            "Tinha capital/investidores declarados — problema aparente foi produto/mercado, não dinheiro."
        )
    if "cohort_collapse" in labels:
        parts.append(
            f"{peers_co_dying} peers da cohort (mesmo macro-segmento/país/década) morreram em ±2 anos — sinal de evento macro, não de execução individual."
        )
    if "scope_drift" in labels:
        parts.append(
            f"Atuava em {len(cats)} categorias diferentes — sinal de foco difuso ou múltiplos pivots."
        )
    if "solo_founder" in labels:
        parts.append(
            "Founder solo — aumenta risco operacional e amplia o custo de cada erro nessa fase."
        )
    if "acqui_hire" in labels:
        parts.append(
            "Aquisição em menos de 3 anos de fundação — padrão de acqui-hire mais que exit de sucesso."
        )

    narrative = " ".join(parts) if parts else ""

    if not labels:
        return None

    if len(labels) >= 3 and lifespan is not None:
        confidence = "high"
    elif labels and lifespan is not None:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "lifespan_years": lifespan,
        "lifespan_bucket": bucket,
        "pattern_labels": labels,
        "narrative": narrative,
        "confidence": confidence,
        "peers_co_dying": peers_co_dying,
        "cohort_size": cohort_size,
    }


def enrich_companies(force: bool = False) -> list[dict]:
    """
    Lê multi_source_companies.json, infere causa/macros, persiste em
    multi_source_companies_enriched.json. Retorna a lista enriquecida.

    Para empresas do startups.rip que têm narrativa no startups_raw.json,
    puxa overview/post_mortem/sections e aplica o lexicon de causa.
    """
    if os.path.exists(ENRICHED_PATH) and not force:
        with open(ENRICHED_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    print("[enrich] gerando enriquecimento (leva ~2s)…")
    with open(COMPANIES_PATH, "r", encoding="utf-8") as f:
        companies = json.load(f)

    # índice de texto narrativo por nome normalizado (startups.rip)
    text_index = _load_startups_rip_text_by_name()
    print(f"[enrich] {len(text_index)} empresas do startups.rip com narrativa útil")

    dead_count = 0
    inferred = 0
    multi = 0
    pulled_narrative = 0

    for c in companies:
        c["category_macros"] = sorted(categories_to_macros(c.get("categories", [])))
        c["outcome"] = outcome_bucket(c)
        c["failure_cause_inferred"] = False
        c["failure_cause_evidence"] = ""
        c["failure_cause_confidence"] = ""
        c["rich_narrative"] = ""  # consolidado p/ TF-IDF e relatório

        # tenta puxar narrativa rica do startups_raw (match por nome normalizado
        # com as duas variantes: nome completo, e nome sem "(Winter 2012)")
        if "startups.rip" in c.get("sources", []):
            raw_name = c.get("name", "")
            name_full_norm = normalize(raw_name)
            name_clean_norm = normalize(re.sub(r"\s*\(.*?\)\s*$", "", raw_name))
            narrative = text_index.get(name_clean_norm) or text_index.get(name_full_norm)
            if narrative:
                c["rich_narrative"] = narrative[:20000]
                pulled_narrative += 1

        if c.get("failure_cause"):
            c["failure_cause_confidence"] = "documented"
            continue

        if not is_dead(c.get("status", "")):
            continue
        dead_count += 1

        cause, ev, conf = infer_cause([
            c.get("description", ""),
            c.get("one_liner", ""),
            c.get("post_mortem", ""),
            c.get("rich_narrative", ""),
        ])
        if cause:
            c["failure_cause"] = cause
            c["failure_cause_inferred"] = True
            c["failure_cause_evidence"] = ev
            c["failure_cause_confidence"] = conf
            inferred += 1
            if cause == "Multiple Reasons":
                multi += 1

    # 2ª passada: padrão de falha inferido por sinais estruturais (não textuais).
    # Roda em TODAS as dead/acquired, mesmo quando já há failure_cause — os dois
    # são sinais complementares (um é "o que aconteceu segundo texto", o outro é
    # "o formato observado na estrutura").
    cohort_idx = _build_cohort_index(companies)
    pattern_filled = 0
    pattern_high_conf = 0
    pattern_labels_hist: Counter[str] = Counter()
    for c in companies:
        pat = infer_failure_pattern(c, cohort_idx)
        if pat:
            c["inferred_failure_pattern"] = pat
            pattern_filled += 1
            if pat["confidence"] == "high":
                pattern_high_conf += 1
            for lbl in pat["pattern_labels"]:
                pattern_labels_hist[lbl] += 1

    with open(ENRICHED_PATH, "w", encoding="utf-8") as f:
        json.dump(companies, f, indent=2, ensure_ascii=False)

    documented = sum(1 for c in companies if c.get("failure_cause_confidence") == "documented")
    outcomes = Counter(c.get("outcome", "unknown") for c in companies)
    print(f"[enrich] total={len(companies)}  "
          f"outcomes={dict(outcomes)}  "
          f"narrativa_rica={pulled_narrative}")
    print(f"[enrich] cause_documented={documented}  cause_inferred={inferred}  "
          f"(multi-reason={multi})")
    print(f"[enrich] pattern_filled={pattern_filled}  high_conf={pattern_high_conf}  "
          f"top_labels={pattern_labels_hist.most_common(6)}")
    return companies


# ─── Perguntas (CLI) ─────────────────────────────────────────────────────────

def prompt(msg: str, default: str = "") -> str:
    s = input(f"{msg} ").strip()
    return s or default


def select(msg: str, options: dict, allow_skip: bool = False) -> str:
    print(msg)
    for k, v in options.items():
        label = v[1] if isinstance(v, tuple) else v
        print(f"  {k}. {label}")
    if allow_skip:
        print("  (ENTER p/ pular)")
    while True:
        raw = input("> ").strip()
        if not raw and allow_skip:
            return ""
        val = options.get(raw)
        if val is None:
            print("   opção inválida, tente de novo")
            continue
        return val[0] if isinstance(val, tuple) else val


def multi_select(msg: str, options: dict) -> list:
    print(msg, "(separe por vírgulas, ou ENTER p/ nenhuma)")
    for k, v in options.items():
        label = v[1] if isinstance(v, tuple) else v
        print(f"  {k}. {label}")
    raw = input("> ").strip()
    if not raw:
        return []
    out = []
    for k in raw.split(","):
        val = options.get(k.strip())
        if val:
            out.append(val[0] if isinstance(val, tuple) else val)
    return out


def ask_user_interactive(known_categories: list) -> dict:
    print("=" * 70)
    print(" CONSULTORIA DE RISCO — benchmarking contra 1350 empresas")
    print("=" * 70)
    print("Responda as perguntas. ENTER pula campos opcionais.\n")

    user = {}
    user["name"] = prompt("[1/10] Nome da sua empresa:")
    user["one_liner"] = prompt("[2/10] Em uma frase, o que ela faz:")

    print("\n[3/10] Segmento/indústria principal.")
    print("    Categorias mais comuns no banco:", ", ".join(known_categories[:15]))
    cats_raw = prompt("       Digite 1 ou mais (separados por vírgula):")
    user["categories"] = [c.strip() for c in cats_raw.split(",") if c.strip()]

    user["business_model"] = select("\n[4/10] Modelo de negócio principal:",
                                    BUSINESS_MODELS, allow_skip=True)
    user["country"]      = prompt("\n[5/10] País em que opera (ex: Brazil, United States):")
    user["founded_year"] = prompt("[6/10] Ano de fundação (ex: 2022):")
    user["team_size"]    = prompt("[7/10] Tamanho do time atual:")
    user["stage"]        = prompt("[8/10] Estágio (Pre-seed / Seed / Série A / Bootstrap / Revenue):")

    print("\n[9/10] >>> PRINCIPAL PREOCUPAÇÃO / RISCO de hoje")
    user["main_concern"] = select("Escolha o que mais pesa AGORA:", CAUSE_TAXONOMY)

    print("\n[10/10] Riscos secundários (opcional)")
    user["secondary_concerns"] = multi_select("Selecione os que também te preocupam:",
                                              CAUSE_TAXONOMY)
    user["notes"] = prompt("\nQualquer observação livre (opcional):")
    return user


def ask_user_from_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        user = json.load(f)
    user.setdefault("categories", [])
    user.setdefault("secondary_concerns", [])
    return user


# ─── Scoring (multi-dimensional com evidências e convergência) ───────────────

# Pesos — cada dimensão contribui no máximo seu peso.
W_MACRO         = 16    # macro-segmento
W_CATEGORY      = 10    # categoria literal (bônus por cima do macro)
W_BUSINESS      = 8     # modelo de negócio
W_PRODUCT_SIM   = 15    # TF-IDF cosine  (0..1) * W (lexical)
W_SEMANTIC      = 24    # embedding cosine (0..1) * W (semântico, cross-lang)
W_CAUSE_SAME    = 28    # causa idêntica (documentada)
W_CAUSE_INFER   = 16    # causa idêntica (inferida) — mais fraco porque é guess
W_CAUSE_SECOND  = 8     # risco secundário do user casou
W_COUNTRY       = 7
W_ERA           = 5

# Convergência: se empresa morta + >= N dimensões com valor >= threshold, bônus.
CONVERGENCE_THRESHOLD = 0.3
CONVERGENCE_MIN_DIMS  = 4
CONVERGENCE_BONUS     = 20

MIN_SCORE_TO_LIST = 18


def score_company(user: dict, c: dict,
                  tfidf_vec_user: dict, tfidf_vec_c: dict,
                  semantic_vec_user=None, semantic_vec_c=None,
                  semantic_trust: float = 1.0,
                  business_coherent: bool = True,
                  cause_corroborated: bool = True,
                  ) -> tuple[float, list[dict], dict]:
    """
    Retorna (score_total, dimensões_detalhadas, evidence_bundle).

    Fatores de confiança (multiplicadores aplicados aos pesos):
    - semantic_trust: 0..1; reduz W_SEMANTIC quando o input do user é curto
      (vetor de embedding pobre → sinal menos confiável)
    - business_coherent: False zera o peso da dimensão "modelo" pra não
      pontuar quando o user marcou um modelo que o texto livre não corrobora
    - cause_corroborated: False aplica metade de peso na dimensão "causa"
      SE ela vier isolada (sem convergência com outras). Causa é checkbox,
      não deve dominar a recomendação sozinha.
    """
    dims: list[dict] = []

    # D1 — macro-segmento
    # Cache: user_macros/user_cats_n não mudam entre chamadas pro mesmo user.
    # Stash no dict pra evitar recomputar em cada uma das ~110k iterações.
    user_macros = user.get("_macros")
    if user_macros is None:
        user_macros = categories_to_macros(user.get("categories", []))
        user["_macros"] = user_macros
    comp_macros = set(c.get("category_macros", []))
    if user_macros and comp_macros:
        overlap = user_macros & comp_macros
        if overlap:
            val = len(overlap) / len(user_macros)
            dims.append({
                "name": "macro-segmento",
                "value": val,
                "weight": W_MACRO,
                "points": W_MACRO * val,
                "reason": f"macros em comum: {', '.join(sorted(overlap))}",
            })

    # D2 — categoria literal (bônus adicional pra match exato)
    user_cats_n = user.get("_cats_n")
    if user_cats_n is None:
        user_cats_n = {normalize(x) for x in user.get("categories", [])}
        user["_cats_n"] = user_cats_n
    comp_cats_n = {normalize(x) for x in c.get("categories", [])}
    shared = user_cats_n & comp_cats_n
    if shared and user_cats_n:
        val = len(shared) / len(user_cats_n)
        pretty = [cat for cat in c.get("categories", []) if normalize(cat) in shared]
        dims.append({
            "name": "categoria literal",
            "value": val,
            "weight": W_CATEGORY,
            "points": W_CATEGORY * val,
            "reason": f"categoria idêntica: {', '.join(pretty)}",
        })

    # D3 — modelo de negócio (usa categorias da empresa como proxy)
    um_n = normalize(user.get("business_model", ""))
    if um_n and um_n in comp_cats_n:
        # Se o user declarou modelo mas o texto não corrobora, zera o ponto.
        # Não pontuar é mais honesto do que pontuar cego.
        w_business_effective = W_BUSINESS if business_coherent else 0.0
        if w_business_effective > 0:
            dims.append({
                "name": "modelo de negócio",
                "value": 1.0,
                "weight": w_business_effective,
                "points": w_business_effective,
                "reason": f"mesmo modelo: {user['business_model']}",
            })

    # D4 — similaridade de produto (TF-IDF sobre description+one_liner)
    if tfidf_vec_user and tfidf_vec_c:
        sim = cosine(tfidf_vec_user, tfidf_vec_c)
        if sim >= 0.08:  # ruído abaixo disso
            # pega top termos em comum pra explicar
            shared_terms = sorted(
                set(tfidf_vec_user) & set(tfidf_vec_c),
                key=lambda t: -(tfidf_vec_user[t] * tfidf_vec_c[t])
            )[:5]
            dims.append({
                "name": "produto (lexical)",
                "value": min(sim, 1.0),
                "weight": W_PRODUCT_SIM,
                "points": W_PRODUCT_SIM * min(sim, 1.0),
                "reason": f"palavras em comum {sim*100:.0f}%"
                          + (f" (termos: {', '.join(shared_terms)})" if shared_terms else ""),
            })

    # D4b — similaridade SEMÂNTICA (sentence-transformers multilingual)
    # Captura conceitos parecidos mesmo quando as palavras são diferentes
    # (ou em idiomas diferentes). "app pra estudantes" ≈ "platform for students".
    # Thresholds calibrados empiricamente com o próprio dataset:
    #   - textos não-relacionados: ~0.05-0.20
    #   - relacionados por contexto (ambos são "startup SaaS"): ~0.20-0.35
    #   - mesmo produto/problema em idiomas diferentes: ~0.50-0.75
    # semantic_trust < 1.0 reduz o peso quando input do user é curto.
    if semantic_vec_user is not None and semantic_vec_c is not None:
        sem = float(np.dot(semantic_vec_user, semantic_vec_c))
        if sem >= 0.22:
            # 0.22 → 0 ;  0.70 → 1.0  (bastante generoso pra não perder sinal forte)
            rescaled = min(max((sem - 0.22) / 0.48, 0.0), 1.0)
            effective_weight = W_SEMANTIC * semantic_trust
            reason = f"significado parecido (cosine={sem:.2f}, multilingual)"
            if semantic_trust < 1.0:
                reason += f" · confiança do vetor reduzida (trust={semantic_trust:.1f})"
            dims.append({
                "name": "produto (semântico)",
                "value": rescaled,
                "weight": effective_weight,
                "points": effective_weight * rescaled,
                "reason": reason,
            })

    # D5 — causa principal
    # Se a causa NÃO é corroborada pelo texto livre do user, a dimensão fica
    # ATENUADA. Ela ainda pode pontuar (é informação auto-declarada válida),
    # mas não pode dominar a recomendação sozinha.
    user_cause_n = normalize(user.get("main_concern", ""))
    comp_cause   = c.get("failure_cause", "") or ""
    comp_cause_n = normalize(comp_cause)
    if user_cause_n and comp_cause_n and user_cause_n == comp_cause_n:
        is_inferred = c.get("failure_cause_inferred", False)
        weight = W_CAUSE_INFER if is_inferred else W_CAUSE_SAME
        tag = "inferida do texto" if is_inferred else "documentada"
        quote = c.get("failure_cause_evidence", "") if is_inferred else ""
        reason = f"morreu por '{comp_cause}' ({tag})"
        # Atenuação: causa não-corroborada pelo texto libre vira 60% do peso.
        # Ainda é forte, mas deixa espaço pra outras dimensões puxarem o score.
        if not cause_corroborated:
            weight = weight * 0.6
            reason += " · causa auto-declarada sem corroboração no texto (peso reduzido)"
        dims.append({
            "name": "causa de falha",
            "value": 1.0,
            "weight": weight,
            "points": weight,
            "reason": reason,
            "quote": quote,
        })

    # D6 — riscos secundários do user
    for sc in user.get("secondary_concerns", []):
        if normalize(sc) == comp_cause_n and comp_cause_n:
            dims.append({
                "name": "risco secundário",
                "value": 1.0,
                "weight": W_CAUSE_SECOND,
                "points": W_CAUSE_SECOND,
                "reason": f"seu risco secundário '{sc}' bate com a causa",
            })
            break

    # D7 — país (normalizado pra tratar Brasil↔Brazil, USA↔United States, etc.)
    uc = normalize_country(user.get("country", ""))
    cc = normalize_country(c.get("country", ""))
    if uc and cc and (uc == cc or uc in cc or cc in uc):
        dims.append({
            "name": "país",
            "value": 1.0,
            "weight": W_COUNTRY,
            "points": W_COUNTRY,
            "reason": f"mesmo país: {c.get('country')}",
        })

    # D8 — era de fundação (decay contínuo, não binário)
    # 0 anos = 1.0 ; 1 = 0.875 ; 2 = 0.646 ; 3 = 0.35 ; 4+ = 0
    # Faz sentido: fintech 2015 ≠ fintech 2023 (mercado/tech/regulação
    # diferentes), mas ±1 ano ainda é "mesma coorte".
    try:
        uy = int(user.get("founded_year", "") or 0)
        cy = int(c.get("founded_year", "") or 0)
        if uy and cy:
            diff = abs(uy - cy)
            if diff <= 3:
                era_decay = max(0.0, 1.0 - (diff / 4.0) ** 1.5)
                if era_decay > 0:
                    dims.append({
                        "name": "era",
                        "value": era_decay,
                        "weight": W_ERA,
                        "points": W_ERA * era_decay,
                        "reason": f"fundada {cy} (distância {diff} {'ano' if diff == 1 else 'anos'})",
                    })
    except (TypeError, ValueError):
        pass

    # Score base
    score = sum(d["points"] for d in dims)

    # Convergência: empresa morta + >=N dimensões fortes → bônus
    strong_dims = sum(1 for d in dims if d["value"] >= CONVERGENCE_THRESHOLD)
    dead = is_dead(c.get("status", ""))
    convergence = dead and strong_dims >= CONVERGENCE_MIN_DIMS
    if convergence:
        score += CONVERGENCE_BONUS

    bundle = {
        "strong_dims": strong_dims,
        "total_dims": len(dims),
        "is_dead": dead,
        "convergence": convergence,
        "status": c.get("status", ""),
    }
    return score, dims, bundle


def compute_match_confidence(score: float, dims: list[dict], bundle: dict,
                             quality: dict) -> str:
    """
    Piso de confiança por match. Resposta:
      - 'alta' → convergência OU score alto + múltiplas dims fortes
      - 'média' → score médio + pelo menos 2 dims
      - 'baixa' → score baixo, dim única, ou input pobre
    Deixa explícito pro user quando NÃO confiar no ranking.
    """
    if quality.get("short_input"):
        return "baixa (input curto — ranking pode não refletir realidade)"

    # Se só a dimensão "causa" disparou alto (sem produto/macro corroborando),
    # é só checkbox — não é padrão estrutural. Confiança baixa.
    strong = [d for d in dims if d["value"] >= CONVERGENCE_THRESHOLD]
    names = {d["name"] for d in strong}
    if strong and names.issubset({"causa de falha", "risco secundário"}):
        return "baixa (só causa auto-declarada bateu — sem convergência estrutural)"

    if bundle.get("convergence"):
        return "alta"
    if score >= 55 and bundle.get("strong_dims", 0) >= 3:
        return "alta"
    if score >= 35 and bundle.get("strong_dims", 0) >= 2:
        return "média"
    return "baixa"


# ─── Re-rank por perfil (geo / estágio / funding / decade) ──────────────────
# Porque o embedding semântico domina o score, dois founders com o mesmo
# "one-liner" caem no mesmo top-K mesmo que um seja Series B US com 200 funcs
# e o outro seja pre-seed LatAm com 3. Essa camada re-ordena os top-K já
# pescados pelo `rank()` aplicando similaridade de atributos estruturais.
# É pós-processamento cheap: só roda no top_k, não refaz a busca global.

_HEADCOUNT_BUCKETS = [
    (1, 1, 0), (2, 5, 1), (6, 10, 2), (11, 25, 3),
    (26, 50, 4), (51, 100, 5), (101, 250, 6),
    (251, 500, 7), (501, 1000, 8), (1001, 10000, 9),
]


def _bucket_headcount(s) -> int | None:
    """Normaliza string tipo '1-10', '50', '100-250' pra bucket ordinal."""
    if not s:
        return None
    txt = str(s).strip().lower().replace(" ", "")
    nums = [int(n) for n in re.findall(r"\d+", txt)]
    if not nums:
        return None
    midpoint = nums[0] if len(nums) == 1 else (nums[0] + nums[-1]) // 2
    for lo, hi, idx in _HEADCOUNT_BUCKETS:
        if lo <= midpoint <= hi:
            return idx
    return _HEADCOUNT_BUCKETS[-1][2] if midpoint > _HEADCOUNT_BUCKETS[-1][1] else None


def _first_int(s) -> int | None:
    if not s:
        return None
    m = re.search(r"\d{4}", str(s))
    if m:
        return int(m.group(0))
    m = re.search(r"\d+", str(s))
    return int(m.group(0)) if m else None


# Mapeia stages text → ordinal cru. Usado pra distância entre user.stage
# e perfil aproximado da empresa (via funding, idade, headcount).
_STAGE_ORDER = {
    "idea": 0, "ideia": 0, "concept": 0,
    "pre-seed": 1, "preseed": 1, "pre seed": 1, "prototipo": 1, "prototype": 1,
    "seed": 2, "mvp": 2,
    "early revenue": 3, "early": 3, "post-seed": 3,
    "series a": 4, "serie a": 4,
    "series b": 5, "serie b": 5,
    "series c": 6, "serie c": 6, "growth": 6,
    "series d+": 7, "late stage": 7, "late-stage": 7,
    "ipo": 8, "public": 8, "pre-ipo": 8,
}


def _stage_ordinal(s) -> int | None:
    if not s:
        return None
    k = str(s).strip().lower()
    if k in _STAGE_ORDER:
        return _STAGE_ORDER[k]
    for token, v in _STAGE_ORDER.items():
        if token in k:
            return v
    return None


def _company_implicit_stage(c: dict) -> int | None:
    """Sem label explícita, infere do headcount como proxy (melhor que nada)."""
    hb = _bucket_headcount(c.get("headcount"))
    if hb is None:
        return None
    # heurística grosseira: headcount bucket → stage ord
    return min(8, max(0, hb - 1))


def profile_similarity(user: dict, c: dict) -> tuple[float, dict]:
    """
    Retorna (sim ∈ [0,1], breakdown). Só conta dimensões com dado dos dois lados.
    Peso default distribui entre country, decade, headcount, stage e model.
    """
    parts: dict[str, tuple[float, float]] = {}  # key -> (weight, score)

    # country
    uc = normalize_country(user.get("country", ""))
    cc = normalize_country(c.get("country", ""))
    if uc and cc:
        parts["country"] = (0.35, 1.0 if uc == cc else 0.0)

    # decade
    uy = _first_int(user.get("founded_year"))
    cy = _first_int(c.get("founded_year"))
    if uy and cy:
        gap = abs(uy - cy)
        # 0-2 anos = 1.0; cada 5 anos perde ~0.25
        decade_score = max(0.0, 1.0 - (gap / 20.0))
        parts["decade"] = (0.20, decade_score)

    # headcount bucket
    ub = _bucket_headcount(user.get("team_size") or user.get("headcount"))
    cb = _bucket_headcount(c.get("headcount"))
    if ub is not None and cb is not None:
        gap = abs(ub - cb)
        hc_score = max(0.0, 1.0 - (gap / 5.0))
        parts["headcount"] = (0.20, hc_score)

    # stage
    us = _stage_ordinal(user.get("stage"))
    cs = _stage_ordinal(c.get("stage")) or _company_implicit_stage(c)
    if us is not None and cs is not None:
        gap = abs(us - cs)
        st_score = max(0.0, 1.0 - (gap / 4.0))
        parts["stage"] = (0.15, st_score)

    # business model
    ubm = (user.get("business_model") or "").strip().lower()
    cbm = (c.get("business_model") or "").strip().lower()
    if ubm and cbm:
        parts["business_model"] = (0.10, 1.0 if ubm == cbm else 0.0)

    if not parts:
        return 0.5, {"dims": {}, "coverage": 0}  # neutro: sem info pra penalizar

    total_w = sum(w for w, _ in parts.values())
    score = sum(w * s for w, s in parts.values()) / total_w
    return score, {
        "dims": {k: round(s, 2) for k, (_, s) in parts.items()},
        "coverage": len(parts),
    }


def rerank_by_profile(user: dict, ranked: list, top_k: int = 50,
                      blend: float = 0.4) -> list:
    """
    Re-ordena os top_k aplicando multiplicador baseado em profile_similarity.
    final = score * (1 - blend + blend * profile_sim)

    blend=0.4: perfil pior-caso reduz score em 40%, perfil ótimo mantém.
    Abaixo do top_k a lista fica como estava (cauda longa raramente importa).
    """
    if not ranked:
        return ranked
    head = ranked[:top_k]
    tail = ranked[top_k:]
    adjusted = []
    for s, c, dims, bundle in head:
        psim, breakdown = profile_similarity(user, c)
        mult = (1 - blend) + blend * psim
        new_score = s * mult
        bundle["profile_match"] = {
            "score": round(psim, 3),
            "dims": breakdown["dims"],
            "coverage": breakdown["coverage"],
            "mult": round(mult, 3),
        }
        adjusted.append((new_score, c, dims, bundle))
    adjusted.sort(key=lambda x: -x[0])
    return adjusted + tail


# ─── Entity extraction do texto livre ───────────────────────────────────────
# O user escreve "levantamos $2M Série A em 2023 com 12 funcs" e os campos
# structurados ficam vazios. Esse extractor preenche slots vazios com regex
# simples antes do rank rodar.

_MONEY_RE = re.compile(
    r"""(?ix)
    (?:us\$|u\$s|r\$|\$|€|£|usd|brl|eur|gbp)\s*
    (\d+(?:[.,]\d+)?)
    \s*(k|mil|m|mm|milhão|milhões|milhao|milhoes|million|millions|bi|b|bn|bilhão|bilhões|billion)?
    """,
    re.VERBOSE,
)

_ROUND_RE = re.compile(
    r"\b(pre[-\s]?seed|seed|series\s?[a-f]|série\s?[a-f]|serie\s?[a-f]|"
    r"ipo|anjo|angel|bootstrap(?:ped)?|revenue[-\s]?based)\b",
    re.IGNORECASE,
)
_YEAR_RE_FREE = re.compile(r"\b(19\d{2}|20[0-3]\d)\b")
_HEAD_RE = re.compile(
    r"(?i)\b(\d{1,4})\s*(?:funcion[aá]rios?|employees?|people|pessoas|funcs?|devs?|staff|colab(?:s|oradores)?)\b"
)


def _money_to_number(match: re.Match) -> float | None:
    try:
        num = float(match.group(1).replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None
    unit = (match.group(2) or "").lower()
    if unit in ("k", "mil"):
        num *= 1e3
    elif unit in ("m", "mm", "milhão", "milhões", "milhao", "milhoes", "million", "millions"):
        num *= 1e6
    elif unit in ("bi", "b", "bn", "bilhão", "bilhões", "billion"):
        num *= 1e9
    return num


def extract_entities(text: str) -> dict:
    """Pulls explicit funding/year/round/headcount from free text."""
    if not text:
        return {}
    out: dict = {}

    money_match = _MONEY_RE.search(text)
    if money_match:
        val = _money_to_number(money_match)
        if val:
            out["total_funding_extracted"] = val
            out["total_funding_raw"] = money_match.group(0).strip()

    round_match = _ROUND_RE.search(text)
    if round_match:
        out["stage_extracted"] = round_match.group(1).strip().lower()

    year_matches = _YEAR_RE_FREE.findall(text)
    if year_matches:
        years = sorted(set(int(y) for y in year_matches))
        out["year_extracted"] = years[0]
        if len(years) > 1:
            out["years_mentioned"] = years

    head_match = _HEAD_RE.search(text)
    if head_match:
        try:
            out["headcount_extracted"] = int(head_match.group(1))
        except ValueError:
            pass

    return out


def enrich_user_from_text(user: dict) -> dict:
    """Preenche SLOTS VAZIOS do user com entidades extraídas do texto livre.
    Nunca sobrescreve campo já informado pelo user (opt-in implícito)."""
    free_text = " ".join(filter(None, [user.get("one_liner"), user.get("notes")]))
    ent = extract_entities(free_text)
    if not ent:
        return {"applied": {}, "extracted": {}}

    applied: dict = {}
    if "stage_extracted" in ent and not user.get("stage"):
        user["stage"] = ent["stage_extracted"]
        applied["stage"] = ent["stage_extracted"]
    if "year_extracted" in ent and not user.get("founded_year"):
        user["founded_year"] = str(ent["year_extracted"])
        applied["founded_year"] = str(ent["year_extracted"])
    if "headcount_extracted" in ent and not (user.get("team_size") or user.get("headcount")):
        user["team_size"] = str(ent["headcount_extracted"])
        applied["team_size"] = str(ent["headcount_extracted"])
    if "total_funding_extracted" in ent and not user.get("total_funding"):
        user["total_funding"] = ent.get("total_funding_raw", "")
        applied["total_funding"] = ent.get("total_funding_raw", "")

    return {"applied": applied, "extracted": ent}


def rank(user: dict, companies: list, tfidf_index: dict,
         semantic_index: dict | None) -> tuple[list, dict]:
    """
    Retorna (lista_de_matches_ordenada, meta).
    meta inclui warnings de qualidade, fatores de confiança aplicados,
    e contadores agregados que ajudam o format_report a comunicar
    honestamente o que é sólido e o que é frágil.
    """
    # extrai entidades do texto livre (money, year, round, headcount) e
    # preenche slots vazios do user ANTES de rank rodar
    entity_result = enrich_user_from_text(user)

    idf_keys = set(tfidf_index["idf"].keys())
    user_text = " ".join([
        user.get("one_liner", ""),
        user.get("notes", ""),
        " ".join(user.get("categories", [])),
    ])
    # tokens do user COM fuzzy correction baseado no vocabulário global.
    # Se o user escreveu "markteplace" (typo), vira "marketplace".
    user_tokens = tokenize(user_text, vocab=idf_keys)

    # vetor TF-IDF do user a partir dos tokens já corrigidos
    user_vec: dict[str, float] = {}
    if user_tokens:
        tf: Counter = Counter(user_tokens)
        vec = {t: (count / len(user_tokens)) * tfidf_index["idf"].get(t, 0.0)
               for t, count in tf.items() if t in idf_keys}
        n = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        user_vec = {t: v / n for t, v in vec.items()}

    # qualidade + coerência do input
    quality = assess_input_quality(user, user_tokens)
    coherence = coherence_check(user, set(user_tokens))

    # Tamanho do segmento do user: se for minúsculo no banco, a camada
    # semântica é estatisticamente frágil (pega false positives de perfis
    # vagamente parecidos). Aplicamos cap multiplicador adicional.
    user_macros = categories_to_macros(user.get("categories", []))
    segment_size = 0
    if user_macros:
        for c in companies:
            if set(c.get("category_macros", [])) & user_macros:
                segment_size += 1
    sparse_segment_warning = None
    segment_trust_cap = 1.0
    if user_macros and segment_size < 10:
        segment_trust_cap = 0.5
        sparse_segment_warning = (
            f"seu macro-segmento tem apenas {segment_size} empresas no banco; "
            "camada semântica opera com peso reduzido pra evitar false positives "
            "e nenhum match recebe confiança 'alta' sozinho"
        )
    elif user_macros and segment_size < 20:
        segment_trust_cap = 0.75
        sparse_segment_warning = (
            f"segmento pequeno ({segment_size} peers); recomendações são "
            "estatisticamente frágeis — leia os matches como hipóteses, "
            "não como padrão estabelecido"
        )
    effective_semantic_trust = quality["semantic_trust"] * segment_trust_cap

    semantic_user = semantic_encode_user(user, semantic_index)
    semantic_vecs = semantic_index["vectors"] if semantic_index else None

    scored = []
    for i, c in enumerate(companies):
        if not c.get("name"):
            continue
        c_vec = tfidf_index["vectors"].get(i, {})
        sem_c = semantic_vecs[i] if semantic_vecs is not None else None
        s, dims, bundle = score_company(
            user, c, user_vec, c_vec,
            semantic_vec_user=semantic_user, semantic_vec_c=sem_c,
            semantic_trust=effective_semantic_trust,
            business_coherent=coherence["business_coherent"],
            cause_corroborated=coherence["cause_corroborated"],
        )
        if s >= MIN_SCORE_TO_LIST:
            conf = compute_match_confidence(s, dims, bundle, quality)
            # Cap duro: segmento raro nunca dá confiança 'alta' sozinho.
            if segment_trust_cap < 1.0 and conf.startswith("alta"):
                conf = f"média (cap por segmento esparso; {segment_size} peers)"
            bundle["confidence"] = conf
            scored.append((s, c, dims, bundle))
    scored.sort(key=lambda x: -x[0])

    # Re-rank dos top_k por similaridade de perfil (country/decade/headcount/
    # stage/business_model). Evita que Series B US domine top-10 de founder
    # pre-seed LatAm só porque o embedding semântico casou a descrição.
    scored = rerank_by_profile(user, scored, top_k=50, blend=0.4)

    high = sum(1 for _, _, _, b in scored if b.get("confidence", "").startswith("alta"))
    medium = sum(1 for _, _, _, b in scored if b.get("confidence", "").startswith("média"))
    low = sum(1 for _, _, _, b in scored if b.get("confidence", "").startswith("baixa"))
    warnings = list(quality["warnings"]) + list(coherence["warnings"])
    if sparse_segment_warning:
        warnings.append(sparse_segment_warning)
    meta = {
        "warnings": warnings,
        "quality": quality,
        "coherence": coherence,
        "confidence_counts": {"alta": high, "média": medium, "baixa": low},
        "user_tokens": user_tokens,
        "segment_size": segment_size,
        "segment_trust_cap": segment_trust_cap,
        "entities_extracted": entity_result.get("extracted", {}),
        "entities_applied": entity_result.get("applied", {}),
        "rerank": {"method": "profile_similarity", "top_k": 50, "blend": 0.4},
    }
    return scored, meta


# ─── Diagnóstico bidirecional (benchmarking de caminho) ──────────────────────
# Por que: o sistema original só olhava pra mortas. Mas o corpus tem 763
# adquiridas + 33 operando — sinal de SOBREVIVÊNCIA. Compara-se então o
# perfil do user com a DISTRIBUIÇÃO DE OUTCOMES dos peers: se os mais
# parecidos com você morreram em proporção maior que a média do segmento,
# é sinal negativo; se sobreviveram em proporção maior, sinal positivo.
#
# Análise contrastiva (TF-IDF delta entre buckets) mostra os termos
# que distinguem sobreviventes de falidas — input concreto para
# consultoria ("o que as adquiridas tinham que as mortas não tinham?").

def _contrastive_terms(pos_idxs: list[int], neg_idxs: list[int],
                       tfidf_index: dict, n: int = 8,
                       stopword_terms: set | None = None
                       ) -> list[tuple[str, float, int]]:
    """
    Termos mais característicos do grupo `pos` vs `neg`, medidos pelo delta
    de peso médio TF-IDF. Filtra termos com baixa cobertura (menos de X
    docs do grupo pos) pra evitar ruído de termos únicos.

    Retorna [(term, delta_peso, cobertura), ...] top N.
    """
    if not pos_idxs or not neg_idxs:
        return []
    pos_agg: Counter = Counter()
    neg_agg: Counter = Counter()
    for i in pos_idxs:
        for t, v in tfidf_index["vectors"].get(i, {}).items():
            pos_agg[t] += v
    for i in neg_idxs:
        for t, v in tfidf_index["vectors"].get(i, {}).items():
            neg_agg[t] += v
    pos_n = {t: v / len(pos_idxs) for t, v in pos_agg.items()}
    neg_n = {t: v / len(neg_idxs) for t, v in neg_agg.items()}
    stopword_terms = stopword_terms or set()
    min_support = max(3, len(pos_idxs) // 12)
    deltas = []
    for t in set(pos_n):
        if t in stopword_terms or len(t) < 3:
            continue
        support = sum(1 for i in pos_idxs if t in tfidf_index["vectors"].get(i, {}))
        if support < min_support:
            continue
        d = pos_n[t] - neg_n.get(t, 0.0)
        if d > 0:
            deltas.append((t, d, support))
    deltas.sort(key=lambda x: -x[1])
    return deltas[:n]


def _path_signal(top_outcomes: Counter, seg_outcomes: Counter) -> dict:
    """
    Compara a distribuição de outcomes nos top-N matches do user contra
    o baseline do segmento. Se o top-N tem MAIS mortas que o esperado,
    o 'caminho' do user é estatisticamente pior que a média do segmento.

    Se o segmento é pequeno (<20 peers), emite direction='INCERTO' mesmo
    com delta grande — tamanho de amostra insuficiente pra concluir.
    """
    top_total = sum(top_outcomes.values())
    seg_total = sum(seg_outcomes.values())
    if top_total == 0:
        return {"direction": "SEM_MATCHES",
                "reason": "nenhum match forte o bastante pra comparar"}
    if seg_total == 0:
        return {"direction": "SEM_SEGMENTO",
                "reason": "seu segmento não tem peers no banco — impossível comparar"}

    top_dead = top_outcomes.get("dead", 0) / top_total
    seg_dead = seg_outcomes.get("dead", 0) / seg_total
    top_surv = (top_outcomes.get("acquired", 0) + top_outcomes.get("operating", 0)) / top_total
    seg_surv = (seg_outcomes.get("acquired", 0) + seg_outcomes.get("operating", 0)) / seg_total
    delta = top_dead - seg_dead

    # Segmento pequeno → não confiar
    if seg_total < 20:
        direction = "INCERTO"
        reason = (f"segmento muito pequeno no banco ({seg_total} peers); "
                  f"lei dos pequenos números, conclusão estatística frágil. "
                  f"Seus top-N: {top_outcomes.get('dead',0)} mortas, "
                  f"{top_outcomes.get('acquired',0)} adquiridas, "
                  f"{top_outcomes.get('operating',0)} operando")
    elif delta >= 0.20:
        direction = "NEGATIVO"
        reason = (f"seu perfil está mais próximo de empresas que morreram: "
                  f"{top_dead*100:.0f}% dos top-N faleceram, contra "
                  f"{seg_dead*100:.0f}% do segmento geral "
                  f"(você está {delta*100:+.0f}pp PIOR que a média do segmento)")
    elif delta <= -0.15:
        direction = "POSITIVO"
        reason = (f"seu perfil está mais próximo de sobreviventes: "
                  f"{top_surv*100:.0f}% dos top-N foram adquiridas ou operam, "
                  f"contra {seg_surv*100:.0f}% do segmento geral "
                  f"(você está {abs(delta)*100:+.0f}pp MELHOR que a média)")
    else:
        direction = "NEUTRO"
        reason = (f"sua distribuição bate com a média do segmento "
                  f"({top_dead*100:.0f}% mortas no top-N vs "
                  f"{seg_dead*100:.0f}% no segmento)")

    return {
        "direction": direction,
        "top_dead_pct": top_dead,
        "seg_dead_pct": seg_dead,
        "top_surv_pct": top_surv,
        "seg_surv_pct": seg_surv,
        "delta_dead_pct": delta,
        "reason": reason,
    }


def diagnose_path(user: dict, companies: list, ranked: list,
                  tfidf_index: dict, top_n: int = 10) -> dict:
    """
    Diagnóstico consultivo: posiciona o user entre peers vivos E mortos.
    Retorna payload que o relatório consome pra emitir veredito de caminho
    + termos que distinguem sobreviventes de falidas do seu segmento.
    """
    user_macros = categories_to_macros(user.get("categories", []))
    # peers = tudo que compartilha macro-segmento
    segment_idxs = [
        i for i, c in enumerate(companies)
        if user_macros and (set(c.get("category_macros", [])) & user_macros)
    ]
    seg_outcomes: Counter = Counter(companies[i].get("outcome", "unknown")
                                    for i in segment_idxs)

    top_cos = [c for _s, c, _d, _b in ranked[:top_n]]
    top_outcomes: Counter = Counter(c.get("outcome", "unknown") for c in top_cos)

    signal = _path_signal(top_outcomes, seg_outcomes)

    # Contrastivo: o que as sobreviventes têm que as mortas não têm (e vice-versa)
    survivor_idxs = [i for i in segment_idxs
                     if companies[i].get("outcome") in ("acquired", "operating")]
    dead_idxs = [i for i in segment_idxs if companies[i].get("outcome") == "dead"]

    survivor_terms = _contrastive_terms(survivor_idxs, dead_idxs, tfidf_index,
                                         n=10, stopword_terms=STOPWORDS)
    dead_terms = _contrastive_terms(dead_idxs, survivor_idxs, tfidf_index,
                                     n=10, stopword_terms=STOPWORDS)

    return {
        "segment_size": len(segment_idxs),
        "seg_outcomes": dict(seg_outcomes),
        "top_outcomes": dict(top_outcomes),
        "signal": signal,
        "survivor_terms": survivor_terms,
        "dead_terms": dead_terms,
        "n_survivors_in_segment": len(survivor_idxs),
        "n_dead_in_segment": len(dead_idxs),
    }


# ─── Estatísticas de segmento ────────────────────────────────────────────────

def segment_stats(user: dict, companies: list) -> dict:
    user_macros = categories_to_macros(user.get("categories", []))
    in_segment = []
    for c in companies:
        comp_macros = set(c.get("category_macros", []))
        if user_macros and (user_macros & comp_macros):
            in_segment.append(c)

    causes_doc = Counter(c["failure_cause"] for c in in_segment
                         if c.get("failure_cause") and not c.get("failure_cause_inferred"))
    causes_inf = Counter(c["failure_cause"] for c in in_segment
                         if c.get("failure_cause_inferred"))
    countries = Counter(c["country"] for c in in_segment if c.get("country"))
    outcomes = Counter(c.get("outcome", "unknown") for c in in_segment)
    dead_count = outcomes.get("dead", 0)
    acquired_count = outcomes.get("acquired", 0)
    operating_count = outcomes.get("operating", 0)

    user_cause = user.get("main_concern", "")
    matching_cause = [
        c for c in in_segment
        if normalize(c.get("failure_cause", "")) == normalize(user_cause)
    ]

    global_causes = Counter(c["failure_cause"] for c in companies if c.get("failure_cause"))
    global_outcomes = Counter(c.get("outcome", "unknown") for c in companies)

    return {
        "segment_total": len(in_segment),
        "segment_dead": dead_count,
        "segment_acquired": acquired_count,
        "segment_operating": operating_count,
        "segment_outcomes": dict(outcomes),
        "causes_documented_in_segment": causes_doc.most_common(8),
        "causes_inferred_in_segment": causes_inf.most_common(8),
        "countries_in_segment": countries.most_common(6),
        "segment_matching_cause_count": len(matching_cause),
        "user_macros": sorted(user_macros),
        "global_total_with_cause": sum(global_causes.values()),
        "global_top_causes": global_causes.most_common(8),
        "global_outcomes": dict(global_outcomes),
    }


# ─── Relatório ───────────────────────────────────────────────────────────────

def verdict(top_score: float, has_clone: bool, meta: dict | None = None,
            diagnosis: dict | None = None) -> str:
    # Piso de confiança: input curto não gera alerta forte.
    short_input = bool(meta and meta.get("quality", {}).get("short_input"))
    if short_input:
        return ("[CONFIABILIDADE BAIXA] seu input foi curto demais pro sistema formar "
                "uma recomendação sólida. Os matches abaixo são melhores que aleatório "
                "mas PODEM NÃO REPRESENTAR sua realidade. Responda o formulário com "
                "mais detalhes (one-liner completo + notas) e rode de novo.")

    # Veredito bidirecional: usa o direction do diagnóstico de caminho
    # quando disponível. Isso é a diferença CRÍTICA — não basta "você está
    # parecida com mortas"; importa saber se está MAIS parecida com mortas
    # do que o baseline do segmento.
    if diagnosis and diagnosis.get("signal"):
        sig = diagnosis["signal"]
        d = sig.get("direction", "")
        if d == "NEGATIVO":
            prefix = "[CAMINHO EM ALERTA]"
            if has_clone:
                prefix = "[CAMINHO EM ALERTA — CLONE ESTRUTURAL]"
            return f"{prefix} {sig.get('reason','')}. Estude o bloco 'o que diferenciou as sobreviventes' abaixo."
        if d == "POSITIVO":
            return (f"[CAMINHO SAUDÁVEL] {sig.get('reason','')}. Siga atento aos riscos "
                    "listados mas o padrão estatístico é favorável.")
        if d == "NEUTRO":
            return (f"[CAMINHO MÉDIO] {sig.get('reason','')}. Você está dentro da "
                    "média do segmento — nem melhor, nem pior que a base — "
                    "use os peers como espelho operacional.")
        if d == "INCERTO":
            return f"[CAMINHO INCERTO] {sig.get('reason','')}."
        if d == "SEM_MATCHES":
            return "[SEM MATCH FORTE] a base não encontrou paralelos claros com o que você descreveu."
        if d == "SEM_SEGMENTO":
            return ("[SEGMENTO NÃO MAPEADO] seu macro-segmento não existe no banco — "
                    "a consultoria fica limitada à similaridade textual. Considere "
                    "revisar as categorias escolhidas.")

    # Fallback (sem diagnóstico disponível): lógica antiga por score
    if has_clone or top_score >= 70:
        return "[ALERTA ALTO] encontrei empresas mortas com convergência estrutural forte com a sua."
    if top_score >= 50:
        return "[ATENÇÃO] sobreposição relevante com padrões de falha."
    if top_score >= 30:
        return "[SINAL FRACO] existem paralelos, mas as evidências são parciais."
    return "[SEM MATCH FORTE] a base não encontrou paralelos claros."


def format_match_block(rank_n: int, score: float, c: dict, dims: list, bundle: dict) -> list[str]:
    L = []
    outcome = c.get("outcome", "unknown")
    if bundle["convergence"]:
        tag = "  [CLONE ESTRUTURAL — morta]"
    elif outcome == "dead":
        tag = "  [morta]"
    elif outcome == "acquired":
        tag = "  [adquirida ✓]"
    elif outcome == "operating":
        tag = "  [operando ✓]"
    else:
        tag = ""

    conf = bundle.get("confidence", "")
    conf_str = f"  ·  confiança: {conf}" if conf else ""

    L.append("")
    L.append(f"  #{rank_n}  {c.get('name','?')}  — similaridade {score:.0f}/100{tag}{conf_str}")

    if c.get("description"):
        L.append(f"      \"{c['description'][:180]}\"")

    meta = []
    if c.get("founded_year"):  meta.append(f"fundada {c['founded_year']}")
    if c.get("shutdown_year"): meta.append(f"encerrada {c['shutdown_year']}")
    if c.get("country"):       meta.append(c["country"])
    if c.get("total_funding"): meta.append(f"levantou {c['total_funding']}")
    if meta:
        L.append(f"      [{' | '.join(meta)}]")

    if c.get("failure_cause"):
        conf = c.get("failure_cause_confidence", "")
        tag_c = f" ({conf})" if conf and conf != "documented" else ""
        L.append(f"      Motivo: {c['failure_cause']}{tag_c}")

    # dimensões que dispararam — checklist visual
    L.append(f"      Dimensões casadas ({bundle['total_dims']}):")
    for d in dims:
        bar = "█" * int(d["value"] * 8) + "·" * (8 - int(d["value"] * 8))
        L.append(f"        [{bar}] {d['name']:18s}  +{d['points']:4.1f}  — {d['reason']}")
        if d.get("quote"):
            L.append(f"           ↳ trecho: {d['quote'][:160]}")

    if bundle["convergence"]:
        L.append(f"      >>> Esta empresa MORREU e bateu em {bundle['strong_dims']} dimensões com você. "
                 f"Esse é o tipo de caso que o sistema mais quer te mostrar.")

    if c.get("links"):
        L.append(f"      Fonte: {c['links'][0]}")

    return L


def _fmt_outcome_line(outcomes: dict, total: int, label: str) -> str:
    d = outcomes.get("dead", 0)
    a = outcomes.get("acquired", 0)
    o = outcomes.get("operating", 0)
    u = outcomes.get("unknown", 0)
    if total == 0:
        return f"  {label}: (nenhum)"
    def pct(n): return f"{100*n/total:.0f}%"
    return (f"  {label}: "
            f"morreram {d} ({pct(d)}) · "
            f"adquiridas {a} ({pct(a)}) · "
            f"operando {o} ({pct(o)}) · "
            f"incerto {u} ({pct(u)})")


def format_report(user: dict, ranked: list, stats: dict,
                  meta: dict | None = None,
                  diagnosis: dict | None = None) -> str:
    L = []
    L.append("=" * 72)
    L.append(f" CONSULTORIA DE RISCO: {user.get('name','?')}")
    L.append(f" Gerada em: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    L.append("=" * 72)

    # Alertas de confiabilidade ANTES dos dados — se o input é fraco ou tem
    # incoerência, o user precisa saber disso antes de ler o ranking.
    if meta:
        warnings = meta.get("warnings", [])
        if warnings:
            L.append("")
            L.append("-- ALERTAS DE CONFIABILIDADE --")
            for w in warnings:
                L.append(f"  ! {w}")
        cc = meta.get("confidence_counts", {})
        if cc and any(cc.values()):
            L.append("")
            L.append(f"  Distribuição dos matches: "
                     f"alta={cc.get('alta',0)}  média={cc.get('média',0)}  "
                     f"baixa={cc.get('baixa',0)}")

    L.append("")
    L.append(f" Segmentos:    {', '.join(user.get('categories',[])) or '-'}")
    if stats["user_macros"]:
        L.append(f" Macros:       {', '.join(stats['user_macros'])}")
    L.append(f" Modelo:       {user.get('business_model','-') or '-'}")
    L.append(f" País:         {user.get('country','-') or '-'}")
    L.append(f" Fundação:     {user.get('founded_year','-') or '-'}    Estágio: {user.get('stage','-') or '-'}")
    L.append(f" Time:         {user.get('team_size','-') or '-'}")
    L.append(f" Risco-chave:  {user.get('main_concern','-') or '-'}")
    if user.get("secondary_concerns"):
        L.append(f" Risco 2ndo:   {', '.join(user['secondary_concerns'])}")
    if user.get("notes"):
        L.append(f" Observações:  {user['notes'][:200]}")
    L.append("")

    # --- Segmento ---
    L.append("-- DADOS DO SEU SEGMENTO (macro) --")
    total = stats["segment_total"]
    dead = stats["segment_dead"]
    if total == 0:
        L.append("  Nenhuma empresa do banco mapeia pros seus macro-segmentos.")
        L.append("  Dica: tente termos mais amplos (SaaS, Fintech, Marketplace, Health, Ed, Food).")
    else:
        acq = stats.get("segment_acquired", 0)
        op  = stats.get("segment_operating", 0)
        L.append(f"  {total} empresas mapeiam pro seu macro-segmento.")
        L.append(f"    Outcomes: {dead} mortas · {acq} adquiridas · {op} operando · "
                 f"{total-dead-acq-op} incertas")
        if stats["causes_documented_in_segment"]:
            L.append("  Causas DOCUMENTADAS de falha no segmento:")
            for cause, n in stats["causes_documented_in_segment"]:
                pct = 100.0 * n / max(1, total)
                flag = " <-- SUA PREOCUPAÇÃO" if normalize(cause) == normalize(user.get("main_concern","")) else ""
                L.append(f"     {n:>3d} ({pct:5.1f}%)  {cause}{flag}")
        if stats["causes_inferred_in_segment"]:
            L.append("  Causas INFERIDAS (classificadas por texto) no segmento:")
            for cause, n in stats["causes_inferred_in_segment"]:
                flag = " <-- SUA PREOCUPAÇÃO" if normalize(cause) == normalize(user.get("main_concern","")) else ""
                L.append(f"     {n:>3d}  {cause}{flag}")
        if stats["countries_in_segment"]:
            top_countries = ", ".join(f"{c} ({n})" for c, n in stats["countries_in_segment"])
            L.append(f"  Países no segmento: {top_countries}")

    mc = user.get("main_concern", "")
    if mc:
        L.append("")
        L.append(f"  No seu segmento, {stats['segment_matching_cause_count']} empresa(s) "
                 f"(documentadas ou inferidas) morreram por '{mc}'.")

    L.append("")
    L.append("-- BASELINE GLOBAL (todas as empresas com causa, documentada+inferida) --")
    for cause, n in stats["global_top_causes"]:
        pct = 100.0 * n / max(1, stats["global_total_with_cause"])
        L.append(f"     {n:>3d} ({pct:5.1f}%)  {cause}")
    L.append("")

    # --- DIAGNÓSTICO DE CAMINHO (benchmarking bidirecional) ---
    if diagnosis and diagnosis.get("signal"):
        sig = diagnosis["signal"]
        seg_size = diagnosis.get("segment_size", 0)
        seg_outs = diagnosis.get("seg_outcomes", {}) or {}
        top_outs = diagnosis.get("top_outcomes", {}) or {}
        top_total = sum(top_outs.values())

        L.append("-- DIAGNÓSTICO DE CAMINHO (benchmarking bidirecional) --")
        L.append(f"  Direção detectada: {sig.get('direction','?')}")
        L.append(f"  {sig.get('reason','')}")
        L.append("")
        L.append(_fmt_outcome_line(seg_outs, seg_size, f"Segmento geral (n={seg_size})"))
        L.append(_fmt_outcome_line(top_outs, top_total,
                                   f"Seus top matches (n={top_total})"))
        L.append("")

        # Termos contrastivos — insight acionável
        surv = diagnosis.get("survivor_terms", []) or []
        dead = diagnosis.get("dead_terms", []) or []
        if surv and diagnosis.get("n_survivors_in_segment", 0) >= 5:
            L.append("  O QUE APARECE MAIS NAS SOBREVIVENTES DO SEU SEGMENTO:")
            terms_str = ", ".join(f"{t} ({sup})" for t, _d, sup in surv[:10])
            L.append(f"    {terms_str}")
            L.append("    (termos com maior peso TF-IDF em adquiridas/operando vs mortas)")
        if dead and diagnosis.get("n_dead_in_segment", 0) >= 5:
            L.append("")
            L.append("  O QUE APARECE MAIS NAS FALIDAS DO SEU SEGMENTO:")
            terms_str = ", ".join(f"{t} ({sup})" for t, _d, sup in dead[:10])
            L.append(f"    {terms_str}")
            L.append("    (idem, lado inverso — pistas de padrões que precederam falência)")
        L.append("")

    # --- Top matches ---
    has_clone = any(b["convergence"] for _, _, _, b in ranked[:10])
    L.append("-- TOP EMPRESAS COM TRAJETÓRIA PARECIDA À SUA --")
    if not ranked:
        L.append(f"  Nenhum match acima do score mínimo ({MIN_SCORE_TO_LIST}).")
        L.append("  Dica: refine segmentos, modelo ou risco principal.")
    for i, (score, c, dims, bundle) in enumerate(ranked[:10], 1):
        L.extend(format_match_block(i, score, c, dims, bundle))

    L.append("")
    L.append("-- VEREDITO --")
    top_score = ranked[0][0] if ranked else 0.0
    L.append(f"  {verdict(top_score, has_clone, meta, diagnosis)}")
    if ranked:
        top_causes = Counter()
        for s, c, _, _ in ranked[:15]:
            if c.get("failure_cause"):
                top_causes[c["failure_cause"]] += 1
        if top_causes:
            L.append("  Padrões mais frequentes nos seus matches:")
            for cause, n in top_causes.most_common(4):
                L.append(f"     - {cause}  ({n} matches)")
    L.append("=" * 72)
    L.append(f"  Base: {stats['global_total_with_cause']} empresas com causa classificada; "
             "match usa macro-segmento, TF-IDF de descrição, causa (documentada/inferida),")
    L.append("  modelo, país, era. Causas inferidas têm peso menor (são heurística, não fato).")
    L.append("=" * 72)
    return "\n".join(L)


# ─── Integração no grafo ─────────────────────────────────────────────────────

def write_user_into_graph(user: dict, ranked: list, user_id: str, graph_path: str) -> int:
    with open(GRAPH_PATH, "r", encoding="utf-8") as f:
        G = json.load(f)

    name_to_id = {}
    for n in G["nodes"]:
        if n.get("type") == "company":
            name_to_id[normalize(n.get("name", ""))] = n["id"]

    G["nodes"].append({
        "id": user_id,
        "type": "user_company",
        "name": user.get("name", ""),
        "one_liner": user.get("one_liner", ""),
        "categories": ", ".join(user.get("categories", [])),
        "business_model": user.get("business_model", ""),
        "country": user.get("country", ""),
        "founded_year": user.get("founded_year", ""),
        "team_size": user.get("team_size", ""),
        "stage": user.get("stage", ""),
        "main_concern": user.get("main_concern", ""),
        "secondary_concerns": ", ".join(user.get("secondary_concerns", [])),
        "notes": user.get("notes", ""),
        "consulted_at": datetime.now().isoformat(),
    })

    edge_count = 0
    for score, c, dims, bundle in ranked[:15]:
        tgt = name_to_id.get(normalize(c.get("name", "")))
        if not tgt:
            continue
        # Relação outcome-aware: grafo conta qual TIPO de similaridade é.
        outcome = c.get("outcome", "unknown")
        if bundle["convergence"]:
            relation = "RESEMBLES_DEAD_CLONE"
        elif outcome == "dead":
            relation = "RESEMBLES_DEAD"
        elif outcome == "acquired":
            relation = "RESEMBLES_ACQUIRED"   # sinal positivo: você se parece com uma saída
        elif outcome == "operating":
            relation = "RESEMBLES_OPERATING"  # sinal positivo: você se parece com sobrevivente
        else:
            relation = "RESEMBLES"
        dim_names = [d["name"] for d in dims]
        G["edges"].append({
            "source": user_id,
            "target": tgt,
            "relation": relation,
            "score": round(float(score), 1),
            "matched_dims": ", ".join(dim_names),
            "target_outcome": outcome,
            "is_dead_target": bundle["is_dead"],
            "confidence": bundle.get("confidence", ""),
        })
        edge_count += 1

    if user.get("main_concern"):
        risk_id = f"RISK_PATTERN:{user['main_concern']}"
        if not any(n["id"] == risk_id for n in G["nodes"]):
            G["nodes"].append({"id": risk_id, "type": "risk_pattern",
                               "name": user["main_concern"]})
        G["edges"].append({"source": user_id, "target": risk_id,
                           "relation": "CONCERNED_ABOUT"})

    with open(graph_path, "w", encoding="utf-8") as f:
        json.dump(G, f, indent=2, ensure_ascii=False)
    return edge_count


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="JSON com respostas pré-preenchidas")
    ap.add_argument("--top", type=int, default=10, help="quantos matches listar")
    ap.add_argument("--rebuild-enrichment", action="store_true",
                    help="regenera o cache de enriquecimento")
    ap.add_argument("--rebuild-embeddings", action="store_true",
                    help="regenera o cache de embeddings semânticos")
    ap.add_argument("--no-semantic", action="store_true",
                    help="desativa camada semântica (fallback só TF-IDF)")
    ap.add_argument("--email", default="",
                    help="e-mail do usuário para follow-up (opcional)")
    ap.add_argument("--consent-lgpd", action="store_true",
                    help="usuário deu consentimento LGPD para armazenamento")
    ap.add_argument("--consent-followup", action="store_true",
                    help="usuário aceita receber follow-ups por e-mail")
    ap.add_argument("--no-persist", action="store_true",
                    help="não persiste a empresa do usuário no banco de vértices")
    args = ap.parse_args()

    companies = enrich_companies(force=args.rebuild_enrichment)

    print("[tfidf] construindo índice…")
    tfidf_index = build_tfidf_index(companies)

    if args.no_semantic:
        semantic_index = None
        print("[semantic] desativada via --no-semantic")
    else:
        semantic_index = build_semantic_index(companies, force=args.rebuild_embeddings)

    # dica de categorias
    known_cats = Counter()
    for c in companies:
        for cat in c.get("categories", []):
            known_cats[cat] += 1
    known_category_list = [k for k, _ in known_cats.most_common(30)]

    if args.input:
        user = ask_user_from_file(args.input)
        print(f"(modo não-interativo — lido de {args.input})")
    else:
        user = ask_user_interactive(known_category_list)

    ranked, meta = rank(user, companies, tfidf_index, semantic_index)
    stats = segment_stats(user, companies)
    diagnosis = diagnose_path(user, companies, ranked, tfidf_index, top_n=args.top)
    report = format_report(user, ranked, stats, meta=meta, diagnosis=diagnosis)

    print()
    print(report)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"consultoria_{slugify(user.get('name','anon'))[:40]}_{ts}"
    report_path = os.path.join(CONSULTORIAS_DIR, base + ".txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    matches_path = os.path.join(CONSULTORIAS_DIR, base + "_matches.json")
    with open(matches_path, "w", encoding="utf-8") as f:
        json.dump({
            "user": user,
            "stats": stats,
            "meta": {
                "warnings": meta.get("warnings", []),
                "quality": meta.get("quality", {}),
                "coherence": meta.get("coherence", {}),
                "confidence_counts": meta.get("confidence_counts", {}),
                "user_tokens_preview": (meta.get("user_tokens", []) or [])[:30],
                "segment_size": meta.get("segment_size", 0),
            },
            "diagnosis": {
                "segment_size": diagnosis.get("segment_size", 0),
                "seg_outcomes": diagnosis.get("seg_outcomes", {}),
                "top_outcomes": diagnosis.get("top_outcomes", {}),
                "signal": diagnosis.get("signal", {}),
                "survivor_terms": diagnosis.get("survivor_terms", [])[:10],
                "dead_terms": diagnosis.get("dead_terms", [])[:10],
            },
            "top_matches": [
                {
                    "rank": i + 1,
                    "score": round(s, 1),
                    "name": c.get("name"),
                    "status": c.get("status"),
                    "failure_cause": c.get("failure_cause"),
                    "failure_cause_confidence": c.get("failure_cause_confidence"),
                    "failure_cause_evidence": c.get("failure_cause_evidence"),
                    "country": c.get("country"),
                    "founded_year": c.get("founded_year"),
                    "shutdown_year": c.get("shutdown_year"),
                    "sources": c.get("sources"),
                    "links": c.get("links", [])[:3],
                    "dimensions": [
                        {k: v for k, v in d.items() if k != "quote"}
                        for d in dims
                    ],
                    "convergence": bundle["convergence"],
                    "is_dead": bundle["is_dead"],
                    "strong_dims": bundle["strong_dims"],
                    "confidence": bundle.get("confidence", ""),
                }
                for i, (s, c, dims, bundle) in enumerate(ranked[:25])
            ],
        }, f, indent=2, ensure_ascii=False)

    graph_path = os.path.join(CONSULTORIAS_DIR, base + ".json")
    user_id = f"USER_COMPANY:{slugify(user.get('name','anon'))[:80]}"
    edges_added = write_user_into_graph(user, ranked, user_id, graph_path)

    print()
    print(f"Relatório:            {report_path}")
    print(f"Matches estruturados: {matches_path}")
    print(f"Grafo (c/ nó user + {edges_added} arestas): {graph_path}")

    # ─── Persistência da empresa do usuário no banco de vértices ──────────
    if not args.no_persist:
        try:
            from user_persistence import persist_user_company
            consultation_result = {
                "top": [
                    {
                        "name": c.get("name"),
                        "norm": c.get("norm"),
                        "score": round(s, 1),
                        "outcome": c.get("outcome"),
                        "convergence": bundle["convergence"],
                    }
                    for (s, c, _dims, bundle) in ranked[: args.top]
                ],
                "verdict": verdict(
                    ranked[0][0] if ranked else 0,
                    any(b["convergence"] for _, _, _, b in ranked[: args.top]),
                    meta,
                    diagnosis,
                )
                if ranked
                else "",
                "warnings": meta.get("warnings", []),
                "diagnosis": {
                    "signal": diagnosis.get("signal", {}).get("direction"),
                    "segment_size": diagnosis.get("segment_size"),
                    "seg_outcomes": diagnosis.get("seg_outcomes"),
                    "top_outcomes": diagnosis.get("top_outcomes"),
                },
            }
            uc = persist_user_company(
                user_input=user,
                consultation_result=consultation_result,
                email=args.email,
                consent_lgpd=args.consent_lgpd,
                consent_followup=args.consent_followup,
            )
            print(
                f"user_company_id:      {uc['user_company_id']} "
                f"({len(uc['snapshots'])} snapshot(s))"
            )
            if args.consent_followup and args.email:
                print("Follow-ups agendados: 90d, 180d, 365d, 730d")
        except Exception as e:
            print(f"[warn] persistência falhou: {e}")


if __name__ == "__main__":
    main()
