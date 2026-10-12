import streamlit as st
import pandas as pd
import re
import io
import base64
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

@st.cache_data(ttl=3600)
def carregar_base_pacientes() -> pd.DataFrame:
    """Baixa o pacientes.csv privado do Drive e prepara os dados em memória."""
    csv_id = st.secrets["drive"]["csv_file_id"]
    conteudo_bytes = baixar_arquivo_drive(csv_id)
    
    # Tenta ler separado por vírgula ou ponto-e-vírgula automaticamente
    try:
        df = pd.read_csv(io.BytesIO(conteudo_bytes), dtype=str, sep=None, engine="python")
    except Exception:
        df = pd.read_csv(io.BytesIO(conteudo_bytes), dtype=str)
        
    # Padroniza nomes das colunas para minúsculo e sem espaços extras
    df.columns = [col.strip().lower() for col in df.columns]
    
    # Cria coluna de CPF apenas com números para busca exata
    if "cpf" in df.columns:
        df["cpf_limpo"] = df["cpf"].fillna("").str.replace(r"\D", "", regex=True).str.zfill(11)
    return df

@st.cache_data(ttl=3600)
def carregar_pdf_template() -> bytes:
    """Baixa o PDF template privado com JavaScript embutido do Google Drive."""
    pdf_id = st.secrets["drive"]["pdf_template_id"]
    return baixar_arquivo_drive(pdf_id)

# ==========================================
# 4. FUNÇÕES DE NEGÓCIO (BUSCA, DRIVE E PDF)
# ==========================================
def formatar_cpf(cpf_num: str) -> str:
    """Formata 11 dígitos para 000.000.000-00."""
    cpf_num = re.sub(r"\D", "", str(cpf_num)).zfill(11)
    if len(cpf_num) == 11:
        return f"{cpf_num[:3]}.{cpf_num[3:6]}.{cpf_num[6:9]}-{cpf_num[9:]}"
    return cpf_num

def extrair_cpf_texto_pec(texto: str):
    """Extrai o CPF a partir do texto bruto copiado do PEC e-SUS."""
    padrao = r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"
    encontrado = re.search(padrao, texto)
    if encontrado:
        return re.sub(r"\D", "", encontrado.group()).zfill(11)
    return None

def obter_dados_paciente(cpf_limpo: str) -> dict:
    """Cruza o CPF com o CSV e retorna Nome, CPF e Endereço formatado."""
    df = carregar_base_pacientes()
    if df.empty or "cpf_limpo" not in df.columns:
        return None
        
    filtro = df[df["cpf_limpo"] == cpf_limpo]
    if filtro.empty:
        return None
        
    linha = filtro.iloc[0]
    
    nome = str(linha.get("nome", "")).strip()
    rua = str(linha.get("rua", linha.get("logradouro", ""))).strip()
    numero = str(linha.get("numero", "S/N")).strip()
    complemento = str(linha.get("complemento", "")).strip()
    bairro = str(linha.get("bairro", "")).strip()
    cidade = str(linha.get("cidade", linha.get("municipio", ""))).strip()
    estado = str(linha.get("estado", linha.get("uf", ""))).strip()
    
    # Monta: Rua, Número, Complemento, Bairro, Cidade - Estado
    partes_endereco = []
    if rua:
        partes_endereco.append(f"{rua}, {numero}" if numero and numero.lower() != "nan" else rua)
    if complemento and complemento.lower() != "nan":
        partes_endereco.append(complemento)
    if bairro and bairro.lower() != "nan":
        partes_endereco.append(bairro)
        
    endereco_base = ", ".join(partes_endereco)
    if cidade and cidade.lower() != "nan":
        cidade_uf = f"{cidade} - {estado}" if (estado and estado.lower() != "nan") else cidade
        endereco_completo = f"{endereco_base}, {cidade_uf}" if endereco_base else cidade_uf
    else:
        endereco_completo = endereco_base

    return {
        "nome": nome,
        "cpf_limpo": cpf_limpo,
        "cpf_formatado": formatar_cpf(cpf_limpo),
        "endereco": endereco_completo
    }

def listar_prescricoes_drive(cpf_limpo: str = None) -> list:
    """
    Lista os arquivos .txt de prescrições salvos na pasta privada do Drive.
    Formato do arquivo: CPF_DD-MM-YYYY_HH-MM.txt
    """
    pasta_id = st.secrets["drive"]["folder_historico_id"]
    query = f"'{pasta_id}' in parents and mimeType = 'text/plain' and trashed = false"
    if cpf_limpo:
        query += f" and name contains '{cpf_limpo}_'"
        
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
        data_arq = partes[1].replace("-", "/") if len(partes) >= 2 else ""
        hora_arq = partes[2].replace("-", ":") if len(partes) >= 3 else ""
        
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
    return conteudo_bytes.decode("utf-8")

def salvar_prescricao_drive(cpf_limpo: str, texto_prescricao: str) -> str:
    """Salva a prescrição como arquivo .txt no formato CPF_DD-MM-YYYY_HH-MM.txt."""
    pasta_id = st.secrets["drive"]["folder_historico_id"]
    agora = datetime.now()
    nome_arquivo = f"{cpf_limpo}_{agora.strftime('%d-%m-%Y_%H-%M')}.txt"
    
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
    
    # Atualiza os campos em todas as páginas do template (caso tenha mais de 1 via)
    for pagina in writer.pages:
        writer.update_page_form_field_values(pagina, valores_campos)
        
    # Mantém a renderização dinâmica e os JavaScripts do PDF ativos
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
    with st.expander("⚙️ Conferência de Campos do PDF"):
        try:
            pdf_temp = carregar_pdf_template()
            reader_temp = PdfReader(io.BytesIO(pdf_temp))
            campos_detectados = list((reader_temp.get_fields() or {}).keys())
            st.caption(f"Campos detectados no PDF: {campos_detectados}")
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
        "1. Cole aqui o texto bruto do paciente copiado do PEC (e-SUS):",
        height=110,
        placeholder="Cole o bloco de texto do prontuário contendo o CPF do paciente..."
    )
    
    if texto_pec:
        cpf_extraido = extrair_cpf_texto_pec(texto_pec)
        
        if not cpf_extraido:
            st.warning("⚠️ Nenhum CPF válido foi identificado no texto colado.")
        else:
            with st.spinner("Cruzando dados com a base de pacientes..."):
                paciente = obter_dados_paciente(cpf_extraido)
                
            if not paciente:
                st.error(f"❌ O CPF {formatar_cpf(cpf_extraido)} foi extraído, mas não consta no arquivo 'pacientes.csv'.")
            else:
                st.success("✅ Paciente localizado na base de dados!")
                with st.container(border=True):
                    c1, c2 = st.columns([2, 1])
                    c1.markdown(f"**Nome Completo:** {paciente['nome']}")
                    c2.markdown(f"**CPF:** {paciente['cpf_formatado']}")
                    st.markdown(f"**Endereço:** {paciente['endereco']}")
                
                # Busca se já existe prescrição prévia no Google Drive
                if "ultimo_cpf_buscado" not in st.session_state or st.session_state["ultimo_cpf_buscado"] != cpf_extraido:
                    with st.spinner("Verificando histórico de prescrições no Google Drive..."):
                        historico_cpf = listar_prescricoes_drive(cpf_extraido)
                        if historico_cpf:
                            texto_previo = ler_texto_prescricao_drive(historico_cpf[0]["file_id"])
                            st.session_state["texto_prescricao_atual"] = texto_previo
                            st.session_state["data_ultima_presc"] = historico_cpf[0]["data_hora_str"]
                        else:
                            st.session_state["texto_prescricao_atual"] = ""
                            st.session_state["data_ultima_presc"] = None
                        st.session_state["ultimo_cpf_buscado"] = cpf_extraido
                
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
                            # 1. Salva arquivo .txt no Drive (CPF_DD-MM-YYYY_HH-MM.txt)
                            nome_txt_salvo = salvar_prescricao_drive(paciente["cpf_limpo"], texto_editado)
                            
                            # 2. Preenche os campos CIDADÃO e PRESCRIÇÃO mantendo JavaScript
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
        
        if termo_limpo_num and len(termo_limpo_num) >= 3:
            pacientes_encontrados = df_pac[df_pac["cpf_limpo"].str.contains(termo_limpo_num, na=False)]
        else:
            pacientes_encontrados = df_pac[df_pac["nome"].str.contains(termo_busca, case=False, na=False)]
            
        if pacientes_encontrados.empty:
            st.warning("Nenhum paciente encontrado no CSV com esse termo.")
        else:
            for _, pac_row in pacientes_encontrados.iterrows():
                cpf_pac = pac_row["cpf_limpo"]
                nome_pac = pac_row.get("nome", "Sem Nome")
                
                with st.expander(f"👤 {nome_pac} — CPF: {formatar_cpf(cpf_pac)}", expanded=True):
                    prescricoes_pac = listar_prescricoes_drive(cpf_pac)
                    
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
                                dados_completos = obter_dados_paciente(cpf_pac)
                                if dados_completos:
                                    pdf_reimpressao = preencher_pdf_template(
                                        dados_paciente=dados_completos,
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