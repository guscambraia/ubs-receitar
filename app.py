import streamlit as st
import pandas as pd
import re
import io
import base64
import unicodedata
from datetime import datetime
from pypdf import PdfReader, PdfWriter
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# ==========================================
# 1. CONFIGURAÇÃO DA PÁGINA E CAMPOS DO PDF
# ==========================================
st.set_page_config(
    page_title="Sistema de Prescrições",
    page_icon="🩺",
    layout="wide"
)

# Nomes exatos dos campos de formulário no seu PDF Template
NOME_CAMPO_PACIENTE_PDF = "CIDADÃO"
NOME_CAMPO_PRESCRICAO_PDF = "PRESCRIÇÃO"

# ==========================================
# 2. SISTEMA DE LOGIN (COM ST.SECRETS)
# ==========================================
if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False

if not st.session_state["autenticado"]:
    col1, col2, col3 = st.columns([1, 1.5, 1])
    with col2:
        st.title("🔒 Acesso Restrito - Unidade")
        st.write("Insira a credencial da unidade para acessar o receituário eletrônico.")
        senha_digitada = st.text_input("Senha de acesso:", type="password")
        if st.button("Entrar", type="primary", use_container_width=True):
            if senha_digitada == st.secrets["auth"]["password"]:
                st.session_state["autenticado"] = True
                st.rerun()
            else:
                st.error("Senha incorreta. Tente novamente.")
    st.stop()

# ==========================================
# 3. CONEXÃO COM O GOOGLE DRIVE PRIVADO
# ==========================================
@st.cache_resource
def conectar_drive():
    """Conecta à API do Google Drive usando a Service Account configurada no Secrets."""
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

drive_service = conectar_drive()

def baixar_arquivo_drive(file_id: str) -> bytes:
    """Baixa qualquer arquivo do Google Drive para a memória RAM (em bytes)."""
    request = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    concluido = False
    while not concluido:
        _, concluido = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()

def remover_acentos(texto: str) -> str:
    """Remove acentos e padroniza para letras maiúsculas."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    sem_acento = "".join([c for c in nfkd if not unicodedata.combining(c)])
    return re.sub(r"\s+", " ", sem_acento).strip().upper()

@st.cache_data(ttl=3600)
def carregar_base_pacientes() -> pd.DataFrame:
    """
    Lê o arquivo pacientes.csv do e-SUS (codificação cp1252/latin1,
    separador ';' e cabeçalho iniciando na linha 18).
    """
    csv_id = st.secrets["drive"]["csv_file_id"]
    conteudo_bytes = baixar_arquivo_drive(csv_id)
    
    # Decodifica tratando o padrão Windows/e-SUS (cp1252 / latin1) ou UTF-8
    texto_decodificado = None
    for enc in ["cp1252", "latin1", "utf-8-sig", "utf-8"]:
        try:
            texto_decodificado = conteudo_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
            
    if texto_decodificado is None:
        texto_decodificado = conteudo_bytes.decode("latin1", errors="replace")
        
    linhas = texto_decodificado.splitlines()
    
    # Encontra automaticamente a linha onde começa "Nome equipe;INE equipe;...;CPF/CNS;Nome"
    indice_cabecalho = 0
    for i, linha in enumerate(linhas[:35]):
        if "CPF/CNS" in linha or ("CPF" in linha.upper() and "NOME" in linha.upper()):
            indice_cabecalho = i
            break
            
    texto_tabela = "\n".join(linhas[indice_cabecalho:])
    
    df = pd.read_csv(
        io.StringIO(texto_tabela),
        sep=";",
        dtype=str,
        on_bad_lines="skip",
        index_col=False
    )
    
    # Remove espaços extras dos nomes das colunas
    df.columns = [str(col).strip() for col in df.columns]
    
    # Cria colunas auxiliares limpas para busca rápida por CPF/CNS e por Nome
    col_cpf = "CPF/CNS" if "CPF/CNS" in df.columns else ("CPF" if "CPF" in df.columns else df.columns[4])
    col_nome = "Nome" if "Nome" in df.columns else df.columns[5]
    col_end = "Endereço" if "Endereço" in df.columns else df.columns[3]
    col_nasc = "Data de nascimento" if "Data de nascimento" in df.columns else None
    
    df["doc_limpo"] = df[col_cpf].fillna("").astype(str).str.replace(r"\D", "", regex=True)
    df["nome_limpo"] = df[col_nome].fillna("").astype(str).str.strip()
    df["nome_busca"] = df["nome_limpo"].apply(remover_acentos)
    df["endereco_bruto"] = df[col_end].fillna("").astype(str).str.strip()
    df["nascimento_limpo"] = df[col_nasc].fillna("").astype(str).str.strip() if col_nasc else ""
    
    df = df[(df["nome_limpo"] != "") | (df["doc_limpo"] != "")].copy()
    return df

@st.cache_data(ttl=3600)
def carregar_pdf_template() -> bytes:
    """Baixa o PDF template privado com JavaScript embutido do Google Drive."""
    pdf_id = st.secrets["drive"]["pdf_template_id"]
    return baixar_arquivo_drive(pdf_id)

# ==========================================
# 4. FUNÇÕES DE NEGÓCIO (BUSCA, DRIVE E PDF)
# ==========================================
def formatar_documento(doc_num: str) -> str:
    """Formata 11 dígitos para 000.000.000-00."""
    apenas_num = re.sub(r"\D", "", str(doc_num))
    if len(apenas_num) == 11:
        return f"{apenas_num[:3]}.{apenas_num[3:6]}.{apenas_num[6:9]}-{apenas_num[9:]}"
    return apenas_num if apenas_num else "Não informado"

def extrair_dados_linha(linha: pd.Series) -> dict:
    """
    Retorna Nome Completo, CPF formatado e Endereço contendo
    Rua, Número, Complemento, Bairro e Cidade - Estado (removendo o '| CEP').
    """
    nome = linha["nome_limpo"]
    doc_limpo = linha["doc_limpo"]
    end_bruto = linha["endereco_bruto"]
    
    # Exemplo no CSV: "Rua CAPINÓPOLIS, 241. CATALÃO, Divinópolis - MG | 35501-230"
    # Cortando no "|" ficamos apenas com: "Rua CAPINÓPOLIS, 241. CATALÃO, Divinópolis - MG"
    endereco_sem_cep = end_bruto.split("|")[0].strip() if end_bruto and end_bruto != "-" else "Endereço não informado"
    
    return {
        "nome": nome,
        "cpf_limpo": doc_limpo if doc_limpo else remover_acentos(nome).replace(" ", "_"),
        "cpf_formatado": formatar_documento(doc_limpo),
        "endereco": endereco_sem_cep,
        "nascimento": linha.get("nascimento_limpo", "")
    }

def buscar_paciente_no_texto_pec(texto_pec: str) -> list:
    """
    Cruza o texto bruto colado do PEC com o pacientes.csv:
    1. Tenta pelo CPF (com ou sem pontuação) ou CNS.
    2. Se não houver CPF no texto, localiza pelo Nome Completo do paciente.
    """
    df = carregar_base_pacientes()
    if df.empty:
        return []
        
    # 1. Busca por CPF (formato 000.000.000-00 ou sequência de 11 dígitos)
    cpfs_encontrados = re.findall(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", texto_pec)
    for cpf_str in cpfs_encontrados:
        num = re.sub(r"\D", "", cpf_str)
        achados = df[df["doc_limpo"] == num]
        if not achados.empty:
            return [extrair_dados_linha(row) for _, row in achados.iterrows()]
            
    # 2. Busca por CNS (15 dígitos)
    cns_encontrados = re.findall(r"\b\d{15}\b", re.sub(r"[.\-]", "", texto_pec))
    for cns_str in cns_encontrados:
        achados = df[df["doc_limpo"] == cns_str]
        if not achados.empty:
            return [extrair_dados_linha(row) for _, row in achados.iterrows()]

    # 3. Busca pelo Nome do paciente contido no texto do PEC
    texto_norm = remover_acentos(texto_pec)
    if len(texto_norm) >= 3:
        mask_nome_inteiro = df["nome_busca"].apply(lambda n: len(n) >= 5 and n in texto_norm)
        achados_nome = df[mask_nome_inteiro]
        if not achados_nome.empty:
            return [extrair_dados_linha(row) for _, row in achados_nome.iterrows()]
            
        if len(texto_norm) <= 60:
            achados_parcial = df[df["nome_busca"].str.contains(re.escape(texto_norm), na=False)]
            if not achados_parcial.empty:
                return [extrair_dados_linha(row) for _, row in achados_parcial.head(15).iterrows()]

    return []

def listar_prescricoes_drive(identificador_limpo: str = None) -> list:
    """Lista os arquivos .txt de prescrições salvos na pasta privada do Drive."""
    pasta_id = st.secrets["drive"]["folder_historico_id"]
    query = f"'{pasta_id}' in parents and mimeType = 'text/plain' and trashed = false"
    if identificador_limpo:
        query += f" and name contains '{identificador_limpo}_'"
        
    resultados = drive_service.files().list(
        q=query,
        orderBy="createdTime desc",
        pageSize=100,
        fields="files(id, name, createdTime)"
    ).execute()
    
    arquivos = resultados.get("files", [])
    lista_formatada = []
    
    for arq in arquivos:
        nome_arq = arq["name"].replace(".txt", "")
        partes = nome_arq.split("_")
        cpf_arq = partes[0] if len(partes) >= 1 else ""
        data_arq = partes[-2].replace("-", "/") if len(partes) >= 3 else ""
        hora_arq = partes[-1].replace("-", ":") if len(partes) >= 3 else ""
        
        lista_formatada.append({
            "file_id": arq["id"],
            "nome_arquivo": arq["name"],
            "cpf_limpo": cpf_arq,
            "data_hora_str": f"{data_arq} às {hora_arq}".strip(" às "),
            "createdTime": arq.get("createdTime", "")
        })
    return lista_formatada

def ler_texto_prescricao_drive(file_id: str) -> str:
    """Lê o conteúdo de um arquivo .txt de prescrição salvo no Google Drive."""
    conteudo_bytes = baixar_arquivo_drive(file_id)
    return conteudo_bytes.decode("utf-8", errors="replace")

def salvar_prescricao_drive(identificador_limpo: str, texto_prescricao: str) -> str:
    """Salva a prescrição como arquivo .txt no formato CPF_DD-MM-YYYY_HH-MM.txt."""
    pasta_id = st.secrets["drive"]["folder_historico_id"]
    agora = datetime.now()
    nome_arquivo = f"{identificador_limpo}_{agora.strftime('%d-%m-%Y_%H-%M')}.txt"
    
    metadados = {
        "name": nome_arquivo,
        "parents": [pasta_id],
        "mimeType": "text/plain"
    }
    media = MediaIoBaseUpload(
        io.BytesIO(texto_prescricao.encode("utf-8")),
        mimetype="text/plain",
        resumable=False
    )
    arquivo = drive_service.files().create(
        body=metadados,
        media_body=media,
        fields="id, name"
    ).execute()
    return arquivo.get("name")

def preencher_pdf_template(dados_paciente: dict, texto_prescricao: str, campo_pac: str, campo_presc: str) -> bytes:
    """
    Clona o PDF template preservando AcroForm e JavaScripts embutidos,
    injeta os campos CIDADÃO e PRESCRIÇÃO e aciona o NeedAppearances.
    """
    pdf_bytes = carregar_pdf_template()
    writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes))
    
    # Texto consolidado do campo CIDADÃO (Nome, CPF e Endereço)
    cabecalho_cidadao = (
        f"{dados_paciente['nome']} - CPF: {dados_paciente['cpf_formatado']}\n"
        f"{dados_paciente['endereco']}"
    )
    
    valores_campos = {
        campo_pac: cabecalho_cidadao,
        campo_presc: texto_prescricao
    }
    
    for pagina in writer.pages:
        writer.update_page_form_field_values(pagina, valores_campos)
        
    writer.set_need_appearances_writer()
    
    output_buffer = io.BytesIO()
    writer.write(output_buffer)
    output_buffer.seek(0)
    return output_buffer.read()

# ==========================================
# 5. MENU LATERAL E CONFIGURAÇÕES
# ==========================================
with st.sidebar:
    st.header("🩺 Menu Principal")
    aba_selecionada = st.radio(
        "Navegação:",
        ["📝 Emitir Prescrição", "🗂️ Histórico de Pacientes"]
    )
    
    st.divider()
    with st.expander("⚙️ Conferência de Campos e Base"):
        try:
            df_teste = carregar_base_pacientes()
            st.caption(f"Pacientes carregados no CSV: **{len(df_teste)}**")
        except Exception as e_csv:
            st.error(f"Erro no CSV: {e_csv}")
            
        try:
            pdf_temp = carregar_pdf_template()
            reader_temp = PdfReader(io.BytesIO(pdf_temp))
            campos_detectados = list((reader_temp.get_fields() or {}).keys())
            st.caption(f"Campos detectados no PDF: `{campos_detectados}`")
        except Exception:
            campos_detectados = []
            
        campo_pac_input = st.text_input("Campo Cidadão:", value=NOME_CAMPO_PACIENTE_PDF)
        campo_presc_input = st.text_input("Campo Prescrição:", value=NOME_CAMPO_PRESCRICAO_PDF)
        
    if st.button("🔄 Recarregar CSV / Template do Drive", use_container_width=True):
        st.cache_data.clear()
        st.success("Cache atualizado com sucesso!")
        
    if st.button("🚪 Sair", use_container_width=True):
        st.session_state["autenticado"] = False
        st.rerun()

# ==========================================
# 6. TELA 1: EMITIR PRESCRIÇÃO
# ==========================================
if aba_selecionada == "📝 Emitir Prescrição":
    st.title("📝 Emissão de Prescrição")
    
    texto_pec = st.text_area(
        "1. Cole aqui o texto bruto do paciente copiado do PEC (e-SUS) ou digite Nome/CPF:",
        height=110,
        placeholder="Cole o bloco de texto do prontuário contendo o CPF ou o Nome do paciente..."
    )
    
    if texto_pec:
        with st.spinner("Cruzando dados com a base de pacientes do e-SUS..."):
            candidatos = buscar_paciente_no_texto_pec(texto_pec)
            
        if not candidatos:
            st.error("❌ Nenhum paciente correspondente foi encontrado no arquivo 'pacientes.csv'.")
        else:
            if len(candidatos) > 1:
                opcoes = {f"{p['nome']} (CPF: {p['cpf_formatado']})": p for p in candidatos}
                escolha = st.selectbox("Mais de um paciente encontrado. Selecione:", list(opcoes.keys()))
                paciente = opcoes[escolha]
            else:
                paciente = candidatos[0]
                
            st.success("✅ Paciente localizado na base de dados!")
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.markdown(f"**Nome Completo:** {paciente['nome']}")
                c2.markdown(f"**CPF:** {paciente['cpf_formatado']}")
                if paciente.get("nascimento"):
                    c3.markdown(f"**Nascimento:** {paciente['nascimento']}")
                st.markdown(f"**Endereço:** {paciente['endereco']}")
            
            id_busca = paciente["cpf_limpo"]
            
            # Verifica prescrição anterior no Google Drive
            if "ultimo_id_buscado" not in st.session_state or st.session_state["ultimo_id_buscado"] != id_busca:
                with st.spinner("Verificando histórico de prescrições no Google Drive..."):
                    historico_cpf = listar_prescricoes_drive(id_busca)
                    if historico_cpf:
                        texto_previo = ler_texto_prescricao_drive(historico_cpf[0]["file_id"])
                        st.session_state["texto_prescricao_atual"] = texto_previo
                        st.session_state["data_ultima_presc"] = historico_cpf[0]["data_hora_str"]
                    else:
                        st.session_state["texto_prescricao_atual"] = ""
                        st.session_state["data_ultima_presc"] = None
                    st.session_state["ultimo_id_buscado"] = id_busca
            
            if st.session_state.get("data_ultima_presc"):
                st.info(f"🔄 Prescrição anterior encontrada (emitida em **{st.session_state['data_ultima_presc']}**). O texto foi carregado automaticamente abaixo para edição opcional.")
            else:
                st.info("✨ Primeira prescrição deste paciente no sistema. Digite os itens abaixo.")
            
            texto_editado = st.text_area(
                "2. Prescrição (edite como desejar):",
                value=st.session_state.get("texto_prescricao_atual", ""),
                height=260
            )
            
            if st.button("🖨️ Gerar Prescrição, Salvar no Drive e Imprimir", type="primary"):
                if not texto_editado.strip():
                    st.warning("O campo de prescrição não pode estar vazio.")
                else:
                    with st.spinner("Salvando histórico no Google Drive e preenchendo PDF..."):
                        nome_txt_salvo = salvar_prescricao_drive(id_busca, texto_editado)
                        
                        pdf_final_bytes = preencher_pdf_template(
                            dados_paciente=paciente,
                            texto_prescricao=texto_editado,
                            campo_pac=campo_pac_input,
                            campo_presc=campo_presc_input
                        )
                        
                        st.session_state["texto_prescricao_atual"] = texto_editado
                        
                    st.success(f"Prescrição registrada no histórico (`{nome_txt_salvo}`) e PDF gerado!")
                    
                    nome_pdf_download = f"Prescricao_{paciente['nome'].replace(' ', '_')}_{datetime.now().strftime('%d-%m-%Y')}.pdf"
                    
                    st.download_button(
                        label="⬇️ Baixar Arquivo PDF",
                        data=pdf_final_bytes,
                        file_name=nome_pdf_download,
                        mime="application/pdf",
                        type="primary"
                    )
                    
                    st.subheader("Visualização para Impressão")
                    b64_pdf = base64.b64encode(pdf_final_bytes).decode("utf-8")
                    iframe_html = f'<iframe src="data:application/pdf;base64,{b64_pdf}" width="100%" height="750" type="application/pdf"></iframe>'
                    st.markdown(iframe_html, unsafe_allow_html=True)

# ==========================================
# 7. TELA 2: HISTÓRICO E BUSCA POR PACIENTE
# ==========================================
elif aba_selecionada == "🗂️ Histórico de Pacientes":
    st.title("🗂️ Histórico de Prescrições Emitidas")
    st.write("Pesquise pelo **Nome** ou **CPF** do paciente para consultar todas as receitas já emitidas e suas respectivas datas.")
    
    termo_busca = st.text_input("🔍 Digite o Nome ou CPF do paciente:")
    
    if termo_busca:
        df_pac = carregar_base_pacientes()
        termo_limpo_num = re.sub(r"\D", "", termo_busca)
        termo_norm = remover_acentos(termo_busca)
        
        if termo_limpo_num and len(termo_limpo_num) >= 3:
            pacientes_encontrados = df_pac[df_pac["doc_limpo"].str.contains(termo_limpo_num, na=False)]
        else:
            pacientes_encontrados = df_pac[df_pac["nome_busca"].str.contains(re.escape(termo_norm), na=False)]
            
        if pacientes_encontrados.empty:
            st.warning("Nenhum paciente encontrado no CSV com esse termo.")
        else:
            for _, pac_row in pacientes_encontrados.head(20).iterrows():
                dados_pac = extrair_dados_linha(pac_row)
                id_pac = dados_pac["cpf_limpo"]
                nome_pac = dados_pac["nome"]
                
                with st.expander(f"👤 {nome_pac} — CPF: {dados_pac['cpf_formatado']}", expanded=True):
                    prescricoes_pac = listar_prescricoes_drive(id_pac)
                    
                    if not prescricoes_pac:
                        st.caption("Nenhuma prescrição emitida para este paciente até o momento.")
                    else:
                        st.markdown(f"**Total de prescrições encontradas:** {len(prescricoes_pac)}")
                        for item in prescricoes_pac:
                            col_data, col_acao = st.columns([3, 1])
                            col_data.markdown(f"📅 **Data e Hora:** {item['data_hora_str']} (`{item['nome_arquivo']}`)")
                            
                            if col_acao.button("📄 Ver / Reimprimir", key=f"ver_{item['file_id']}"):
                                texto_hist = ler_texto_prescricao_drive(item["file_id"])
                                st.text_area(
                                    f"Prescrição de {item['data_hora_str']}:",
                                    value=texto_hist,
                                    height=180,
                                    disabled=True,
                                    key=f"txt_{item['file_id']}"
                                )
                                pdf_reimpressao = preencher_pdf_template(
                                    dados_paciente=dados_pac,
                                    texto_prescricao=texto_hist,
                                    campo_pac=campo_pac_input,
                                    campo_presc=campo_presc_input
                                )
                                st.download_button(
                                    label="⬇️ Baixar PDF desta data",
                                    data=pdf_reimpressao,
                                    file_name=f"Prescricao_{nome_pac.replace(' ', '_')}_{item['nome_arquivo'].replace('.txt', '.pdf')}",
                                    mime="application/pdf",
                                    key=f"dl_{item['file_id']}"
                                )
