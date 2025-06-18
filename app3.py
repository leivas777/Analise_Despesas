import streamlit as st
import streamlit_authenticator as stauth
import pandas as pd
import re
from datetime import datetime
import yaml
from yaml.loader import SafeLoader
import os
import matplotlib.pyplot as plt
from supabase import create_client, Client

import unicodedata


SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def carregar_historico(username):
    resp = supabase.table("lancamentos").select("*").eq("usuario", str(username)).execute()
    df = pd.DataFrame(resp.data)

    if df.empty:
        return pd.DataFrame(columns=["Banco", "Data", "Tipo Lançamento", "Descrição", "Valor", "Tag"])

    # Renomeia corretamente as colunas vindas do Supabase para as colunas do seu DataFrame
    df = df.rename(columns={
        "banco": "Banco",
        "data": "Data",
        "tipo_lancamento": "Tipo Lançamento",
        "descricao": "Descrição",
        "valor": "Valor",
        "tag": "Tag"
    })

    # Converte a coluna Data para datetime
    df["Data"] = pd.to_datetime(df["Data"], errors="coerce")

    # Retorna as colunas na ordem desejada
    return df[["Banco", "Data", "Tipo Lançamento", "Descrição", "Valor", "Tag"]]

def salvar_lancamentos(username, df):
    for _, row in df.iterrows():
        # Pula linhas com valor NaN para evitar problemas de serialização JSON
        if pd.isna(row["Valor"]):
            continue
        # Corrige o formato da coluna Valor para salvar corretamente
        valor_str = str(row["Valor"]).replace("R$", "").replace(".", "").replace(",", ".").strip()
        try:
            valor_float = float(row["Valor"])
        except (ValueError, TypeError):
            valor_float = 0.0

        # Corrige o formato da coluna Data para salvar corretamente, tratando datas inválidas
        data_correta = pd.to_datetime(row["Data"], format='%d/%m/%Y', errors="coerce")

        # Se a data for inválida (NaT), substitui pela data atual
        if pd.isna(data_correta):
            data_correta = datetime.today().date()
        else:
            data_correta = data_correta.date()

        data = {
            "usuario": username,
            "banco": row["Banco"],
            "data": str(data_correta),
            "tipo_lancamento": row["Tipo Lançamento"],
            "descricao": row["Descrição"],
            "valor": valor_float,
            "tag": row.get("Tag", "Outros")
        }
        supabase.table("lancamentos").insert(data).execute()


CONFIG_PATH = "config.yaml"

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as file:
            return yaml.load(file, Loader=SafeLoader)
    else:
        config = {
            "credentials": { "usernames": {}},
            "cookie": {"expiry_days": 30, "key": "abcdef", "name": "app_financas"}
        }
        with open(CONFIG_PATH, "w") as file:
            yaml.dump(config, file)
        return config

def save_config(config):
    with open(CONFIG_PATH, "w") as file:
        yaml.dump(config, file)

st.set_page_config(page_title="Relatório Financeiro Multi-Banco", layout="wide")
config = load_config()

authenticator = stauth.Authenticate(
    config['credentials'],
    config['cookie']['name'],
    config['cookie']['key'],
    config['cookie']['expiry_days']
)

try:
    authenticator.login()
except Exception as e:
    st.error(e)
    
if st.session_state.get('authentication_status'):

    st.write(f'Bem-Vindo(a) *{st.session_state.get("name")}*')
    authenticator.logout()
    
    st.title("Relatório Financeiro Multi-Banco - Extratos OFX com Categorias e Gráficos")

    HISTORICO_PATH = "historico_lancamentos.csv"
    CATEGORIA_USER_PATH = "categorias_personalizadas.csv"

    def normalizar_descricao(descricao):
            """
            Normaliza descrições de extrato removendo datas, horários, o termo 'memo', e padronizando para manter
            apenas a parte relevante (tipo e beneficiário).
            Exemplos:
            "pix  enviado  0205 1020 ipva sefaz rsmemo" -> "pix enviado ipva sefaz rs"
            "compra com cartao 1505 1930 supermercado memo" -> "compra com cartao supermercado"
            """
            
            if pd.isna(descricao):
                return ""
            desc = str(descricao).lower().strip()
            # Remove acentos
            desc = unicodedata.normalize('NFKD', desc)
            desc = "".join([c for c in desc if not unicodedata.combining(c)])
            # Remove caracteres especiais, exceto espaço
            desc = re.sub(r'[^a-z0-9\s]', '', desc)
            # Remove datas e horários (ex: 0205, 1020, 1505, 1930, 20240501, etc)
            desc = re.sub(r'\b\d{4,8}\b', ' ', desc)   # blocos de 4 a 8 dígitos
            # Remove padrões de horas/minutos (ex: 10:20, 19:30, 10h20)
            desc = re.sub(r'\b\d{1,2}[:h]\d{2}\b', ' ', desc)
            # Remove o termo "memo" isolado ou junto a outros termos
            desc = re.sub(r'\bmemo\b', ' ', desc)
            # Remove ocorrências de "rsmemo", "memo", "rs memo", etc
            desc = re.sub(r'\brs\s*memo\b', ' ', desc)
            # Remove múltiplos espaços
            desc = re.sub(r'\s+', ' ', desc)
            # Remove hífens isolados
            desc = re.sub(r'\s*-\s*', ' ', desc)
            # Remove espaços no início e fim
            desc = desc.strip()
            # Se sobrar datas/hora, remove de novo
            desc = re.sub(r'\b\d{4,8}\b', '', desc)
            # Remove múltiplos espaços novamente
            desc = re.sub(r'\s+', ' ', desc)
            return desc

        # Função para limpar descrições do C6, removendo "memo" e "rs memo" após normalizar
    def limpar_memo_c6(desc):
        desc = normalizar_descricao(desc)
        # Remove todas as ocorrências de "rs memo" e "memo" (case-insensitive)
        desc = re.sub(r'\brs\s*memo\b', ' ', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\bmemo\b', ' ', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\s+', ' ', desc)
        return desc.strip().title()

    def extrair_tipo_e_descricao(descricao):
        """
        Extrai o tipo de lançamento e a descrição de um campo de descrição de extrato, de forma resiliente para bancos diferentes.
        - Se o campo estiver vazio ou só tiver hífens/espaços, retorna ("Outros", "").
        - Se não houver hífen, ou não houver partes válidas após split, retorna ("Outros", "").
        - Limpa datas/horas do início da descrição.
        - Aplica normalização e iniciais maiúsculas no resultado.
        """
        
        if pd.isna(descricao):
            return "Outros", ""
        desc = str(descricao).strip()
        # Remove tags e marcadores comuns de OFX/MEMO
        desc = re.sub(r'</?MEMO>', ' ', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\bmemo\b', ' ', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\s+', ' ', desc).strip()
        # Se o campo for vazio ou só hífens ou só espaços, retorna Outros
        if not desc or re.fullmatch(r'-+\s*', desc) or re.fullmatch(r'\s*-+\s*', desc):
            return "Outros", ""
        # Procura todos os hífens (pode estar rodeado de espaços)
        hifens = list(re.finditer(r'\s*-\s*', desc))
        tipo = ""
        descricao_restante = ""
        if len(hifens) >= 2:
            # Dois ou mais hífens: tipo é até o segundo hífen, descrição é o resto
            tipo = desc[:hifens[1].start()].replace('-', ' ').strip()
            descricao_restante = desc[hifens[1].end():].strip()
        elif len(hifens) == 1:
            # Apenas um hífen: tipo é antes do hífen, descrição é depois
            tipo = desc[:hifens[0].start()].strip()
            descricao_restante = desc[hifens[0].end():].strip()
        else:
            # Não há hífen: usa a primeira palavra como tipo, o resto como descrição
            partes = desc.split()
            # Se não há partes válidas, retorna Outros
            if not partes:
                return "Outros", ""
            # Se só há uma palavra, mas ela é só hífen, retorna Outros
            if len(partes) == 1:
                if re.fullmatch(r'-+', partes[0]):
                    return "Outros", ""
                tipo = partes[0]
                descricao_restante = ""
            else:
                tipo = partes[0]
                descricao_restante = " ".join(partes[1:])
        # Se tipo for vazio ou só hífens, retorna Outros
        if not tipo or re.fullmatch(r'-+', tipo):
            return "Outros", ""
        # Limpa datas/horas do início da descrição
        if descricao_restante:
            # Remove datas e horas do início (ex: 0205, 1020, 20240501, 10:20, 10h20, etc)
            descricao_restante = re.sub(r"^(\d{4,8}\s*)+", "", descricao_restante)
            descricao_restante = re.sub(r"^(\d{1,2}[:h]\d{2}\s*)+", "", descricao_restante)
            descricao_restante = re.sub(r'^\s+', '', descricao_restante)
        # Normaliza tipo e descrição
        tipo = normalizar_descricao(tipo)
        descricao_restante = normalizar_descricao(descricao_restante)
        # Se após normalizar o tipo ficou vazio ou só hífens, retorna Outros
        if not tipo or re.fullmatch(r'-+', tipo):
            return "Outros", ""
        # Inicial maiúscula em cada palavra
        tipo_fmt = tipo.title()
        descricao_fmt = descricao_restante.title()
        return tipo_fmt, descricao_fmt

    def carregar_regras_usuario():
        if os.path.exists(CATEGORIA_USER_PATH):
            df_regras = pd.read_csv(CATEGORIA_USER_PATH)
            # Normaliza as descrições ao carregar
            df_regras['descricao'] = df_regras['descricao'].apply(normalizar_descricao)
            return dict(zip(df_regras['descricao'], df_regras['tag']))
        else:
            return {}

    def salvar_regras_usuario(df):
        # Extrai pares únicos descrição-tag do dataframe e salva
        df_regras = df[['Tipo Lançamento', 'Descrição', 'Tag']].drop_duplicates()
        df_regras['descricao'] = (df_regras['Tipo Lançamento'].fillna('') + " " + df_regras['Descrição'].fillna('')).str.strip()
        df_regras['descricao'] = df_regras['descricao'].apply(normalizar_descricao)
        df_regras = df_regras[['descricao', 'Tag']]
        df_regras.columns = ['descricao', 'tag']
        df_regras.to_csv(CATEGORIA_USER_PATH, index=False)

    CATEGORIAS = {
        "aluguel": "Aluguel",
        "imovel": "Aluguel",
        "supermercado": "Mercado",
        "mercado": "Mercado",
        "padaria": "Mercado",
        "farmacia": "Saúde",
        "droga": "Saúde",
        "hospital": "Saúde",
        "consultorio": "Saúde",
        "saude": "Saúde",
        "bb rende fácil": "Investimento Automático",
        "bb rf ref di": "Investimento Automático",
        "rende facil": "Investimento Automático",
        "depósito online taa": "Depósito Caixa Eletrônico",
        "atm": "Depósito Caixa Eletrônico",
        "samantha treib": "Pensão",
        "rge": "Energia Elétrica",
        "ebanx": "Lazer",
        "pix - recebido": "PIX Recebimento",
        "comercial zaffari": "Mercado",
        "telecom": "Internet",
        "pet": "Pet-Shop",
        "impostos": "Impostos",
        "posto": "Veículo",
        "seguros": "Seguros",
        "seguro": "Seguros",
        "tarifa": "Tarifa",
        "não identificado": "Não identificado",
        "compras": "Compras",
        "desenvolvimento h": "Saúde",
        "regis gruber leivas": "Mesada",
        "colegio maua": "Educacao",
        "transferência entre contas": "Transferência Pessoal",
        "empréstimo": "Empréstimo",
        "vestuário": "Vestuário",
        "borba imoveis": "Aluguel",
        "manutencao": "Manutenção"
    }

    BANCOS = {
        "Banco do Brasil": "BB",
        "C6": "C6"
    }

    def categorizar(descricao, regras_usuario):
        desc_norm = normalizar_descricao(descricao)
        # Primeiro verifica regras personalizadas (normalizadas)
        for chave_desc, categoria in regras_usuario.items():
            if chave_desc == desc_norm:
                return categoria
        # Depois no dicionário fixo
        for palavra, categoria in CATEGORIAS.items():
            if palavra in desc_norm:
                return categoria
        return "Outros"

    def simple_ofx_to_df(uploaded_file, banco, regras_usuario):
        content = uploaded_file.read().decode("latin1")
        trans_blocks = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", content, re.DOTALL)
        transactions = []
        banco_nome = str(banco).strip().lower()
        for block in trans_blocks:
            date = re.search(r"<DTPOSTED>(\d+)", block)
            amount = re.search(r"<TRNAMT>(-?\d+\.\d+)", block)
            # Extração padrão de MEMO
            desc = re.search(r"<MEMO>(.*)", block)
            descricao_completa = desc.group(1).strip() if desc else ""
            # Lógica especial para C6
            if "c6" in banco_nome:
                # Extrai também o tipo de transação
                trntype_match = re.search(r"<TRNTYPE>([A-Z]+)", block)
                trntype = trntype_match.group(1).upper() if trntype_match else ""
                memo = descricao_completa.lower()
                memo_norm = normalizar_descricao(memo)
                tipo_lanc = ""
                descricao = ""
                # Lógica para C6
                if trntype == "DEBIT":
                    if "boleto" in memo:
                        tipo_lanc = "Pagamento De Boleto"
                        descricao = "Boleto"
                    elif "fatura" in memo:
                        tipo_lanc = "Pagamento Fatura Cartão"
                        descricao = "Fatura Cartão"
                    elif "pix" in memo or "enviado" in memo:
                        tipo_lanc = "Pix Enviado"
                        descricao = limpar_memo_c6(memo)
                    else:
                        tipo_lanc = "Compra Com Cartão"
                        descricao = limpar_memo_c6(memo)
                elif trntype == "CREDIT":
                    if "pix" in memo or "recebido" in memo:
                        tipo_lanc = "Pix Recebido"
                        descricao = limpar_memo_c6(memo)
                    else:
                        tipo_lanc = "Crédito"
                        descricao = limpar_memo_c6(memo)
                else:
                    # fallback para padrão
                    tipo_lanc, descricao = extrair_tipo_e_descricao(descricao_completa)
                # Garante maiúsculas e normalização
                tipo_lanc = normalizar_descricao(tipo_lanc).title()
                descricao = limpar_memo_c6(descricao)
            else:
                tipo_lanc, descricao = extrair_tipo_e_descricao(descricao_completa)
            tag = categorizar(tipo_lanc + " " + descricao, regras_usuario)
            transactions.append({
                "Banco": banco,
                "Data": datetime.strptime(date.group(1)[:8], "%Y%m%d") if date else None,
                "Tipo Lançamento": tipo_lanc,
                "Descrição": descricao,
                "Valor": float(amount.group(1)) if amount else 0.0,
                "Tag": tag
            })
        return pd.DataFrame(transactions)

    historico = carregar_historico(st.session_state.get("username"))
    

    regras_usuario = carregar_regras_usuario()

    uploaded_file = st.file_uploader("Selecione o arquivo OFX", type=["ofx"])
    banco = st.selectbox(
        "Selecione o banco:",
        help="Escolha um banco dentre os dispníveis",
        options=BANCOS
        )
    visualizar_btn = st.button("Revisar Lançamentos")

    if visualizar_btn and uploaded_file and banco:
        df = simple_ofx_to_df(uploaded_file, banco, regras_usuario)
        st.session_state["df_novo_extrato"] = df
        st.session_state["df_novo_extrato_raw"] = df.copy()
        st.subheader("Lançamentos deste extrato")
        # A edição do DataFrame será feita abaixo, fora deste bloco, junto ao controle de salvamento
    # CONTROLE DE SALVAMENTO DE LANÇAMENTOS DO EXTRATO - AGORA FORA DO IF DE REVISÃO
    if "df_novo_extrato" in st.session_state:
        df = st.session_state["df_novo_extrato"]
        df_raw = st.session_state["df_novo_extrato_raw"]
        # Sincroniza a coluna "Valor" do df com os valores numéricos corretos de df_raw antes de exibir para edição
        df["Valor"] = df_raw["Valor"]
        # Função para formatar valor para exibição
        def formatar_valor(x):
            try:
                # Garante que o valor é float antes de formatar
                x_float = float(x)
                return f"R$ {x_float:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            except:
                return x

        df["Valor"] = pd.to_numeric(df["Valor"], errors="coerce")
        df["Valor"] = df["Valor"].apply(formatar_valor)
        # Formata a coluna Data para DD-MM-YYYY de maneira mais segura
        df["Data"] = pd.to_datetime(df["Data"], dayfirst=True, errors="coerce")
        df["Data"] = df["Data"].apply(lambda x: x.strftime("%d/%m/%Y") if pd.notnull(x) else "")
        # Mantém edição se usuário alterar tags/tipos
        all_tags = list(set(historico["Tag"].dropna().tolist() + list(CATEGORIAS.values()) + ["Outros"]))
        all_tipos = list(set(historico["Tipo Lançamento"].dropna().tolist()))
        df = st.data_editor(
            df,
            column_config={
                "Tag": st.column_config.SelectboxColumn(
                    "Tag",
                    help="Categoria da despesa/receita",
                    options=all_tags,
                    required=True,
                ),
                "Tipo Lançamento": st.column_config.TextColumn(
                    "Tipo Lançamento",
                    help="Tipo do lançamento extraído da descrição",
                    disabled=False,
                ),
            },
            num_rows="dynamic",
            key="novo_extrato_editor"
        )
        st.session_state["df_novo_extrato"] = df

        # Controle do fluxo de salvamento
        if "salvar_novo_extrato" not in st.session_state:
            st.session_state["salvar_novo_extrato"] = False

        if not st.session_state["salvar_novo_extrato"]:
            if st.button("Salvar lançamentos deste extrato no histórico"):
                st.session_state["salvar_novo_extrato"] = True

        if st.session_state["salvar_novo_extrato"]:
            st.warning("Tem certeza que deseja salvar estes lançamentos no histórico? Esta ação não pode ser desfeita.")
            col1, col2 = st.columns(2)
            if col1.button("Confirmar salvamento", key="confirma_salva"):
                # Atualiza os campos editáveis no df_raw antes de salvar
                df_raw["Tag"] = df["Tag"]
                df_raw["Tipo Lançamento"] = df["Tipo Lançamento"]
                salvar_lancamentos(st.session_state.get("username"), df_raw)
                historico = carregar_historico(st.session_state.get("username"))
                salvar_regras_usuario(historico)
                st.success("Lançamentos salvos no histórico! Atualize a página para visualizar o consolidado.")
                st.session_state["salvar_novo_extrato"] = False
                del st.session_state["df_novo_extrato"]
                if "df_novo_extrato_raw" in st.session_state:
                    del st.session_state["df_novo_extrato_raw"]
            if col2.button("Cancelar salvamento", key="cancela_salva"):
                st.session_state["salvar_novo_extrato"] = False
                if "df_novo_extrato" in st.session_state:
                    del st.session_state["df_novo_extrato"]
                if "df_novo_extrato_raw" in st.session_state:
                    del st.session_state["df_novo_extrato_raw"]
    st.header("Histórico Consolidado de Lançamentos")
    if not historico.empty:
        editar_hist = st.checkbox("Editar histórico de lançamentos")
        if editar_hist:
            all_tags = list(set(historico["Tag"].dropna().tolist() + list(CATEGORIAS.values()) + ["Outros"]))
            all_tipos = list(set(historico["Tipo Lançamento"].dropna().tolist()))
            historico_edit = st.data_editor(
                historico,
                column_config={
                    "Tag": st.column_config.SelectboxColumn(
                        "Tag",
                        help="Categoria da despesa/receita",
                        options=all_tags,
                        required=True,
                    ),
                    "Tipo Lançamento": st.column_config.TextColumn(
                        "Tipo Lançamento",
                        help="Tipo do lançamento extraído da descrição",
                        disabled=False,
                    ),
                },
                num_rows="dynamic",
                key="editor_hist"
            )
            if st.button("Salvar histórico editado"):
                st.session_state['salvar_historico_editado'] = True

            if st.session_state.get('salvar_historico_editado', False):
                st.warning("Tem certeza que deseja sobrescrever o histórico salvo?")
                col1, col2 = st.columns(2)
                if col1.button("Confirmar edição", key="confirma_hist"):
                    salvar_lancamentos(st.session_state.get("username"), historico_edit)
                    # Atualizar regras personalizadas com base no histórico editado
                    salvar_regras_usuario(historico_edit)
                    st.success("Histórico atualizado com sucesso!")
                    st.session_state['salvar_historico_editado'] = False
                if col2.button("Cancelar edição", key="cancela_hist"):
                    st.session_state['salvar_historico_editado'] = False
        else:
            historico_display = historico.copy()
            # Usa a função formatar_valor para exibir no formato brasileiro
            def formatar_valor(x):
                try:
                    x_float = float(x)
                    return f"R$ {x_float:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                except:
                    return x
            historico_display["Valor"] = historico_display["Valor"].apply(formatar_valor)
            historico_display["Data"] = pd.to_datetime(historico_display["Data"], errors="coerce").dt.strftime("%d/%m/%Y")
            st.dataframe(historico_display)

        # -------- FILTROS E RELATÓRIOS ----------
        st.subheader("Filtros do Histórico")
        bancos = historico["Banco"].unique().tolist()
        banco_filtro = st.multiselect("Filtrar por Banco", bancos, default=bancos)
        tags = historico["Tag"].unique().tolist()
        tag_filtro = st.multiselect("Filtrar por Tag", tags, default=tags)
        tipos = historico["Tipo Lançamento"].unique().tolist()
        tipo_filtro = st.multiselect("Filtrar por Tipo Lançamento", tipos, default=tipos)
        from datetime import datetime

        data_min = historico["Data"].min()
        data_max = historico["Data"].max()

        if pd.isnull(data_min) or data_min is pd.NaT:
            data_min = datetime.today()
        if pd.isnull(data_max) or data_max is pd.NaT:
            data_max = datetime.today()

        data_inicio = st.date_input("Data inicial", data_min.date())
        data_fim = st.date_input("Data final", data_max.date())
        texto_filtro = st.text_input("Buscar por palavra na descrição")

        historico_filtrado = historico[
            (historico["Banco"].isin(banco_filtro)) &
            (historico["Tag"].isin(tag_filtro)) &
            (historico["Tipo Lançamento"].isin(tipo_filtro)) &
            (historico["Data"] >= pd.Timestamp(data_inicio)) &
            (historico["Data"] <= pd.Timestamp(data_fim))
        ]
        if texto_filtro:
            historico_filtrado = historico_filtrado[
                historico_filtrado["Descrição"].str.contains(texto_filtro, case=False, na=False)
            ]
            

        historico_filtrado_display = historico_filtrado.copy()
        historico_filtrado_display["Valor"] = historico_filtrado_display["Valor"].apply(formatar_valor)
        historico_filtrado_display["Data"] = pd.to_datetime(historico_filtrado_display["Data"], errors="coerce").dt.strftime("%d/%m/%Y")
        st.dataframe(historico_filtrado_display)

        relatorio_base = historico_filtrado[historico_filtrado["Tag"] != "Investimento Automático"]

        receitas = relatorio_base[relatorio_base['Valor'] > 0]['Valor'].sum()
        gastos = relatorio_base[relatorio_base['Valor'] < 0]['Valor'].sum()
        saldo = relatorio_base['Valor'].sum()

        st.write(f"**Total de Receitas (exceto Investimentos):** R$ {receitas:,.2f}")
        st.write(f"**Total de Gastos (exceto Investimentos):** R$ {gastos:,.2f}")
        st.write(f"**Saldo (exceto Investimentos):** R$ {saldo:,.2f}")

        st.subheader("Top 5 Maiores Gastos")
        top_gastos = relatorio_base[relatorio_base['Valor'] < 0].sort_values(by="Valor").head(5)
        top_gastos["Valor"] = top_gastos["Valor"].apply(formatar_valor)
        top_gastos["Data"] = pd.to_datetime(top_gastos["Data"], errors="coerce").dt.strftime("%d/%m/%Y")
        st.dataframe(top_gastos)

        st.subheader("Top 5 Maiores Receitas")
        top_receitas = relatorio_base[relatorio_base['Valor'] > 0].sort_values(by="Valor", ascending=False).head(5)
        top_receitas["Valor"] = top_receitas["Valor"].apply(formatar_valor)
        top_receitas["Data"] = pd.to_datetime(top_receitas["Data"], errors="coerce").dt.strftime("%d/%m/%Y")
        st.dataframe(top_receitas)

        st.subheader("Resumo de Investimento Automático (BB Rende Fácil)")
        investimentos = historico_filtrado[historico_filtrado["Tag"] == "Investimento Automático"]
        if not investimentos.empty:
            investimentos_display = investimentos.copy()
            investimentos_display["Valor"] = investimentos_display["Valor"].apply(formatar_valor)
            investimentos_display["Data"] = pd.to_datetime(investimentos_display["Data"], errors="coerce").dt.strftime("%d/%m/%Y")
            st.dataframe(investimentos_display)
            st.write(
                f"Total movimentado (não entra no saldo geral): R$ {investimentos['Valor'].sum():,.2f}"
            )

        # ---------- GRÁFICOS ----------
        st.header("Visualização Gráfica")

        # 1. Gráfico de barras das despesas por categoria (Tags)
        st.subheader("Despesas por Categoria (Tag)")
        df_gastos = historico_filtrado[(historico_filtrado["Valor"] < 0) & (historico_filtrado["Tag"] != "Investimento Automático")]
        if not df_gastos.empty:
            cat_gastos = df_gastos.groupby("Tag")["Valor"].sum().sort_values()
            st.bar_chart(cat_gastos.abs())
        else:
            st.info("Sem despesas no filtro atual.")

        # 2. Gráfico de pizza das despesas por categoria
        st.subheader("Distribuição das Despesas por Categoria")
        if not df_gastos.empty:
            fig, ax = plt.subplots()
            ax.pie(cat_gastos.abs(), labels=cat_gastos.index, autopct='%1.1f%%', startangle=90)
            ax.axis('equal')
            st.pyplot(fig)
        else:
            st.info("Sem despesas no filtro atual.")

        # 3. Linha do tempo do saldo acumulado
        st.subheader("Evolução do Saldo Acumulado")
        df_tempo = historico_filtrado.copy()
        df_tempo = df_tempo.sort_values("Data")
        df_tempo["Saldo Acumulado"] = df_tempo["Valor"].cumsum()
        if not df_tempo.empty:
            st.line_chart(df_tempo.set_index("Data")["Saldo Acumulado"])
        else:
            st.info("Sem dados no filtro atual.")

        # 4. Linha do tempo dos gastos e receitas mensais
        st.subheader("Receitas e Despesas por Mês")
        df_mes = historico_filtrado.copy()
        df_mes["AnoMes"] = df_mes["Data"].dt.to_period("M").astype(str)
        gastos_mes = df_mes[df_mes["Valor"] < 0].groupby("AnoMes")["Valor"].sum()
        receitas_mes = df_mes[df_mes["Valor"] > 0].groupby("AnoMes")["Valor"].sum()
        df_bar = pd.DataFrame({
            "Despesas": gastos_mes,
            "Receitas": receitas_mes
        }).fillna(0)
        if not df_bar.empty:
            st.bar_chart(df_bar)
        else:
            st.info("Sem dados no filtro atual.")
            
        # --- Gráfico de linhas: Evolução mensal de receitas e despesas ---
        st.subheader("Evolução Mensal de Receitas e Despesas (Linhas)")

        if not df_bar.empty:
            fig, ax = plt.subplots()
            ax.plot(df_bar.index, df_bar['Receitas'], marker='o', label='Receitas', color='green')
            ax.plot(df_bar.index, df_bar['Despesas'].abs(), marker='o', label='Despesas', color='red')
            ax.set_xlabel("Mês/Ano")
            ax.set_ylabel("Valor")
            ax.set_title("Entradas e Saídas Mensais")
            ax.legend()
            plt.xticks(rotation=45)
            st.pyplot(fig)
        else:
            st.info("Sem dados no filtro atual.")
elif st.session_state.get('authentication_status') is False:
    st.error('Username/password is incorrect')
elif st.session_state.get('authentication_status') is None:
    st.sidebar.title("Cadastro de Novo Usuário")
    with st.sidebar.form("cadastro_form"):
        nome_novo = st.text_input("Nome completo")
        usuario_novo = st.text_input("Novo usuário (login)")
        senha_nova = st.text_input("Nova senha", type="password")
        senha_conf = st.text_input("Confirme a senha", type="password")
        submit_cadastro = st.form_submit_button("Cadastrar")

    if submit_cadastro:
        config = load_config()
        if not nome_novo or not usuario_novo or not senha_nova:
            st.sidebar.error("Preencha todos os campos!")
        elif senha_nova != senha_conf:
            st.sidebar.error("As senhas não coincidem.")
        elif usuario_novo in config['credentials']['usernames']:
            st.sidebar.error("Usuário já existe!")
        else:
            hash_pw = stauth.Hasher().hash(senha_nova)
            config['credentials']['usernames'][usuario_novo] = {
                'name': nome_novo,
                'password': hash_pw
            }
            save_config(config)
            st.sidebar.success("Usuário cadastrado! Recarregando para efetivar o login...")
            st.rerun()


    





