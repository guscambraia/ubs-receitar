import streamlit as st
import pandas as pd
import re
import io
import json
import base64
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject, DictionaryObject
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

NOME_CAMPO_PACIENTE_PDF = "CIDADÃO"
NOME_CAMPO_PRESCRICAO_PDF = "PRESCRIÇÃO"

NOME_ARQUIVO_HISTORICO = "historico_prescricoes.json"
ID_PADRAO_HISTORICO = "1RFtMetxdO2B8MioNsSC9SMi7MiUMCDC_"

MESES_PT = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"
]

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
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

drive_service = conectar_drive()

def baixar_arquivo_drive(file_id: str) -> bytes:
    request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    concluido = False
    while not concluido:
        _, concluido = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()

def remover_acentos(texto: str) -> str:
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    sem_acento = "".join([c for c in nfkd if not unicodedata.combining(c)])
    return re.sub(r"\s+", " ", sem_acento).strip().upper()

def limpar_endereco_esus(end_bruto: str) -> str:
    """
    Converte o endereço do e-SUS:
    De: 'Rua CAPINÓPOLIS, 241. CATALÃO, Divinópolis - MG | 35501-230'
    Para: 'Rua CAPINÓPOLIS, 241, CATALÃO, Divinópolis - MG'
    """
    if not end_bruto:
        return ""
    texto = str(end_bruto).strip()
    if texto.lower() in ["", "-", "nan", "none", "nao informada", "nao informado"]:
        return ""
    # Impede que um CPF seja interpretado como endereço
    if re.match(r"^CPF\s*:", texto, flags=re.IGNORECASE):
        return ""
    # Remove o '| CEP' do final
    texto = texto.split("|")[0].strip()
    # Troca o ponto logo após o número (ex: ', 241. BAIRRO' ou ', S/N. BAIRRO') por vírgula
    texto = re.sub(r",\s*(\d+|S/N)\.\s*", r", \1, ", texto, flags=re.IGNORECASE)
    # Troca ' - ' antes do bairro por ', ' apenas se houver outro ' - UF' no final
    partes_hifen = texto.split(" - ")
    if len(partes_hifen) >= 3:
        texto = ", ".join(partes_hifen[:-1]) + " - " + partes_hifen[-1]
    return re.sub(r"\s+", " ", texto).strip(" ,.-")

@st.cache_data(ttl=3600)
def carregar_base_pacientes() -> pd.DataFrame:
    """Lê o arquivo pacientes.csv do e-SUS garantindo o alinhamento exato das colunas."""
    csv_id = st.secrets["drive"]["csv_file_id"]
    conteudo_bytes = baixar_arquivo_drive(csv_id)
    
    # Tenta UTF-8 primeiro; se houver bytes Latin1/Windows-1252 (padrão e-SUS), usa cp1252
    texto_decodificado = None
    for enc in ["utf-8-sig", "utf-8", "cp1252", "latin1"]:
        try:
            texto_decodificado = conteudo_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
            
    if texto_decodificado is None:
        texto_decodificado = conteudo_bytes.decode("latin1", errors="replace")
        
    linhas = texto_decodificado.splitlines()
    
    indice_cabecalho = 0
    for i, linha in enumerate(linhas[:40]):
        linha_norm = remover_acentos(linha)
        if ("CPF" in linha_norm and "NOME" in linha_norm) and ";" in linha:
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
    
    # Mapeia cada coluna pelo seu nome sem acento
    col_map = {remover_acentos(c): c for c in df.columns}
    
    col_cpf = col_map.get("CPF/CNS") or col_map.get("CPF")
    col_nome = col_map.get("NOME")
    col_end = col_map.get("ENDERECO")
    col_rua = col_map.get("RUA") or col_map.get("LOGRADOURO")
    col_num = col_map.get("NUMERO")
    col_comp = col_map.get("COMPLEMENTO")
    col_bairro = col_map.get("BAIRRO")
    col_cid = col_map.get("MUNICIPIO") or col_map.get("CIDADE")
    col_uf = col_map.get("UF") or col_map.get("ESTADO")
    col_nasc = col_map.get("DATA DE NASCIMENTO")
    
    df["doc_limpo"] = df[col_cpf].fillna("").astype(str).str.replace(r"\D", "", regex=True) if col_cpf else ""
    df["nome_limpo"] = df[col_nome].fillna("").astype(str).str.strip() if col_nome else ""
    df["nome_busca"] = df["nome_limpo"].apply(remover_acentos)
    df["nascimento_limpo"] = df[col_nasc].fillna("").astype(str).str.strip() if col_nasc else ""
    
    # Monta o endereço limpo para cada linha (suporta coluna única 'Endereço' ou colunas separadas)
    enderecos_finais = []
    for _, row in df.iterrows():
        end_str = ""
        if col_end and pd.notna(row.get(col_end)):
            end_str = limpar_endereco_esus(row.get(col_end, ""))
        if not end_str and col_rua:
            partes = []
            rua_v = str(row.get(col_rua, "")).strip()
            num_v = str(row.get(col_num, "")).strip() if col_num else ""
            comp_v = str(row.get(col_comp, "")).strip() if col_comp else ""
            bairro_v = str(row.get(col_bairro, "")).strip() if col_bairro else ""
            cid_v = str(row.get(col_cid, "")).strip() if col_cid else ""
            uf_v = str(row.get(col_uf, "")).strip() if col_uf else ""
            
            if rua_v and rua_v not in ["-", "nan"]:
                partes.append(f"{rua_v}, {num_v}" if (num_v and num_v not in ["-", "nan"]) else rua_v)
            if comp_v and comp_v not in ["-", "nan"]:
                partes.append(comp_v)
            if bairro_v and bairro_v not in ["-", "nan"]:
                partes.append(bairro_v)
            base_e = ", ".join(partes)
            if cid_v and cid_v not in ["-", "nan"]:
                cid_uf = f"{cid_v} - {uf_v}" if (uf_v and uf_v not in ["-", "nan"]) else cid_v
                end_str = f"{base_e}, {cid_uf}" if base_e else cid_uf
            else:
                end_str = base_e
        enderecos_finais.append(end_str)
        
    df["endereco_limpo"] = enderecos_finais
    df = df[(df["nome_limpo"] != "") | (df["doc_limpo"] != "")].copy()
    return df

@st.cache_data(ttl=3600)
def carregar_pdf_template() -> bytes:
    pdf_id = st.secrets["drive"]["pdf_template_id"]
    return baixar_arquivo_drive(pdf_id)

# ==========================================
# 4. FUNÇÕES DE NEGÓCIO (BUSCA, DRIVE E PDF)
# ==========================================
def formatar_documento(doc_num: str) -> str:
    apenas_num = re.sub(r"\D", "", str(doc_num))
    if len(apenas_num) == 11:
        return f"{apenas_num[:3]}.{apenas_num[3:6]}.{apenas_num[6:9]}-{apenas_num[9:]}"
    return apenas_num if apenas_num else "Não informado"

def extrair_dados_linha(linha: pd.Series) -> dict:
    nome = str(linha.get("nome_limpo", "")).strip()
    doc_limpo = str(linha.get("doc_limpo", "")).strip()
    endereco = str(linha.get("endereco_limpo", "")).strip()
    
    return {
        "nome": nome,
        "cpf_limpo": doc_limpo if doc_limpo else remover_acentos(nome).replace(" ", "_"),
        "cpf_formatado": formatar_documento(doc_limpo),
        "endereco": endereco,
        "nascimento": str(linha.get("nascimento_limpo", "")).strip()
    }

def buscar_paciente_no_texto_pec(texto_pec: str) -> list:
    df = carregar_base_pacientes()
    if df.empty:
        return []
        
    # 1. Busca pelo CPF (com ou sem pontuação)
    cpfs_encontrados = re.findall(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", texto_pec)
    for cpf_str in cpfs_encontrados:
        num = re.sub(r"\D", "", cpf_str)
        achados = df[df["doc_limpo"] == num]
        if not achados.empty:
            return [extrair_dados_linha(row) for _, row in achados.iterrows()]
            
    # 2. Busca pelo CNS (15 dígitos)
    cns_encontrados = re.findall(r"\b\d{15}\b", re.sub(r"[.\-]", "", texto_pec))
    for cns_str in cns_encontrados:
        achados = df[df["doc_limpo"] == cns_str]
        if not achados.empty:
            return [extrair_dados_linha(row) for _, row in achados.iterrows()]

    # 3. Busca pelo Nome Completo
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

def obter_id_arquivo_historico() -> str:
    if "history_file_id" in st.secrets["drive"]:
        return st.secrets["drive"]["history_file_id"]
        
    pasta_id = st.secrets["drive"]["folder_historico_id"]
    query = f"'{pasta_id}' in parents and name = '{NOME_ARQUIVO_HISTORICO}' and trashed = false"
    res = drive_service.files().list(
        q=query,
        pageSize=1,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    arquivos = res.get("files", [])
    if arquivos:
        return arquivos[0]["id"]
    return ID_PADRAO_HISTORICO

def carregar_todo_historico_drive() -> list:
    try:
        arq_id = obter_id_arquivo_historico()
        conteudo = baixar_arquivo_drive(arq_id).decode("utf-8", errors="replace").strip()
        if not conteudo:
            return []
        dados = json.loads(conteudo)
        return dados if isinstance(dados, list) else []
    except Exception:
        return []

def listar_prescricoes_drive(identificador_limpo: str = None) -> list:
    registros = carregar_todo_historico_drive()
    if identificador_limpo:
        registros = [r for r in registros if r.get("cpf_limpo") == identificador_limpo]
    return registros

def salvar_prescricao_drive(identificador_limpo: str, nome_paciente: str, texto_prescricao: str) -> str:
    arq_id = obter_id_arquivo_historico()
    registros = carregar_todo_historico_drive()
    
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    codigo_registro = f"{identificador_limpo}_{agora.strftime('%d-%m-%Y_%H-%M-%S')}"
    nome_referencia = f"{identificador_limpo}_{agora.strftime('%d-%m-%Y_%H-%M')}.txt"
    data_hora_str = agora.strftime("%d/%m/%Y às %H:%M")
    
    novo_item = {
        "file_id": codigo_registro,
        "nome_arquivo": nome_referencia,
        "cpf_limpo": identificador_limpo,
        "nome": nome_paciente,
        "data_hora_str": data_hora_str,
        "texto": texto_prescricao,
        "createdTime": agora.isoformat()
    }
    
    registros.insert(0, novo_item)
    
    json_bytes = json.dumps(registros, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(json_bytes), mimetype="application/json", resumable=False)
    
    drive_service.files().update(
        fileId=arq_id,
        media_body=media,
        supportsAllDrives=True
    ).execute()
    
    return nome_referencia

def calcular_tamanho_fonte_prescricao(texto: str) -> float:
    """
    Replica e aprimora o JavaScript 'formatarPrescricao()' do template.pdf:
    Fonte Lato com tamanho máximo 8, reduzindo progressivamente conforme
    a quantidade de caracteres e o total de linhas da prescrição.
    """
    texto_limpo = (texto or "").strip()
    total_caracteres = len(texto_limpo)
    
    # Calcula o total de linhas visuais (considerando quebras de linha e linhas longas > 75 cols)
    linhas = texto_limpo.splitlines()
    total_linhas_visuais = sum(max(1, (len(l) // 75) + 1) for l in linhas)
    
    # 1. Pela regra original de caracteres do JavaScript embutido
    if total_caracteres > 1300:
        tam_char = 6.0
    elif total_caracteres > 1050:
        tam_char = 6.5
    elif total_caracteres > 850:
        tam_char = 7.0
    elif total_caracteres > 650:
        tam_char = 7.5
    else:
        tam_char = 8.0
        
    # 2. Pela altura da caixa PRESCRIÇÃO (322 pt de altura no template.pdf)
    if total_linhas_visuais > 42:
        tam_linha = 5.5
    elif total_linhas_visuais > 36:
        tam_linha = 6.0
    elif total_linhas_visuais > 32:
        tam_linha = 6.5
    elif total_linhas_visuais > 28:
        tam_linha = 7.0
    elif total_linhas_visuais > 24:
        tam_linha = 7.5
    else:
        tam_linha = 8.0
        
    return min(tam_char, tam_linha)

def preencher_pdf_template(dados_paciente: dict, texto_prescricao: str, campo_pac: str, campo_presc: str) -> bytes:
    """
    Preenche os campos CIDADÃO, PRESCRIÇÃO, DATA e DATA_HORA no template.pdf,
    mantendo a fonte Lato (máx. 8 no CIDADÃO e dinâmica de 8 a 5.5 na PRESCRIÇÃO).
    """
    pdf_bytes = carregar_pdf_template()
    writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes))
    
    # Formatação exata solicitada para o campo CIDADÃO:
    # Linha 1: João da Silva - CPF: 000.000.000-00
    # Linha 2: Rua das Flores, 123, Centro, Cidade - MG
    linha_1 = f"{dados_paciente['nome']} - CPF: {dados_paciente['cpf_formatado']}"
    endereco_val = limpar_endereco_esus(dados_paciente.get("endereco", ""))
    cabecalho_cidadao = f"{linha_1}\n{endereco_val}" if endereco_val else linha_1
    
    # Calcula o tamanho ideal da fonte Lato (máx 8) para a prescrição
    tamanho_lato = calcular_tamanho_fonte_prescricao(texto_prescricao)
    tamanho_str = str(int(tamanho_lato)) if tamanho_lato == int(tamanho_lato) else str(tamanho_lato)
    
    # Data e hora atuais no fuso horário de Minas Gerais (igual ao script AoAbrir do PDF)
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    texto_data = f"Divinópolis - MG, {agora.strftime('%d')} de {MESES_PT[agora.month - 1]} de {agora.year}"
    texto_data_hora = f"{agora.strftime('%d/%m/%Y')} às {agora.strftime('%H:%M')}"
    
    valores_campos = {
        campo_pac: cabecalho_cidadao,
        campo_presc: texto_prescricao,
        "DATA": texto_data,
        "DATA_HORA": texto_data_hora
    }
    
    for pagina in writer.pages:
        writer.update_page_form_field_values(pagina, valores_campos)
        
        # Ajusta diretamente a propriedade /DA (Default Appearance) do campo PRESCRIÇÃO e CIDADÃO
        # para garantir que a fonte Lato com tamanho dinâmico seja aplicada em qualquer navegador
        if "/Annots" in pagina:
            for annot_ref in pagina["/Annots"]:
                annot = annot_ref.get_object()
                nome_campo = annot.get("/T")
                if nome_campo == campo_presc:
                    annot[NameObject("/DA")] = TextStringObject(f"/Lato {tamanho_str} Tf 0 g")
                elif nome_campo == campo_pac:
                    annot[NameObject("/DA")] = TextStringObject("/Lato 8 Tf 0 g")
                    
    # Também adiciona chamada ao formatarPrescricao() ao abrir o documento no Adobe Reader
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
            
            # Permite visualizar e, se desejar, complementar o endereço caso esteja em branco no CSV
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.markdown(f"**Nome Completo:** {paciente['nome']}")
                c2.markdown(f"**CPF:** {paciente['cpf_formatado']}")
                if paciente.get("nascimento"):
                    c3.markdown(f"**Nascimento:** {paciente['nascimento']}")
                
                endereco_editavel = st.text_input(
                    "📍 Endereço (extraído do CSV para sair na 2ª linha do campo CIDADÃO):",
                    value=paciente["endereco"],
                    placeholder="Rua, Número, Complemento, Bairro, Cidade - MG"
                )
                paciente["endereco"] = endereco_editavel.strip()
                
                st.caption(
                    f"**Prévia no campo CIDADÃO do PDF:**  \n"
                    f"`{paciente['nome']} - CPF: {paciente['cpf_formatado']}`  \n"
                    f"`{paciente['endereco'] if paciente['endereco'] else '(Sem endereço cadastrado no CSV)'}`"
                )
            
            id_busca = paciente["cpf_limpo"]
            
            if "ultimo_id_buscado" not in st.session_state or st.session_state["ultimo_id_buscado"] != id_busca:
                with st.spinner("Verificando histórico de prescrições no Google Drive..."):
                    historico_cpf = listar_prescricoes_drive(id_busca)
                    if historico_cpf:
                        st.session_state["texto_prescricao_atual"] = historico_cpf[0].get("texto", "")
                        st.session_state["data_ultima_presc"] = historico_cpf[0].get("data_hora_str", "")
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
            
            tam_previsto = calcular_tamanho_fonte_prescricao(texto_editado)
            st.caption(f"🔤 Tamanho automático da fonte **Lato** para esta prescrição: **{tam_previsto} pt** (Máx: 8 pt)")
            
            if st.button("🖨️ Gerar Prescrição, Salvar no Drive e Imprimir", type="primary"):
                if not texto_editado.strip():
                    st.warning("O campo de prescrição não pode estar vazio.")
                else:
                    with st.spinner("Salvando histórico no Google Drive e preenchendo PDF..."):
                        try:
                            nome_txt_salvo = salvar_prescricao_drive(id_busca, paciente["nome"], texto_editado)
                            st.success(f"✅ Prescrição registrada no histórico (`{nome_txt_salvo}`) e PDF gerado!")
                        except Exception as erro_drive:
                            st.warning(f"⚠️ O PDF foi gerado, mas houve um aviso ao atualizar o histórico no Drive: {erro_drive}")
                        
                        pdf_final_bytes = preencher_pdf_template(
                            dados_paciente=paciente,
                            texto_prescricao=texto_editado,
                            campo_pac=campo_pac_input,
                            campo_presc=campo_presc_input
                        )
                        
                        st.session_state["texto_prescricao_atual"] = texto_editado
                        
                    agora_local = datetime.now(ZoneInfo("America/Sao_Paulo"))
                    nome_pdf_download = f"Prescricao_{paciente['nome'].replace(' ', '_')}_{agora_local.strftime('%d-%m-%Y')}.pdf"
                    
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
                    st.caption(f"📍 **Endereço:** {dados_pac['endereco'] if dados_pac['endereco'] else 'Não informado no CSV'}")
                    prescricoes_pac = listar_prescricoes_drive(id_pac)
                    
                    if not prescricoes_pac:
                        st.caption("Nenhuma prescrição emitida para este paciente até o momento.")
                    else:
                        st.markdown(f"**Total de prescrições encontradas:** {len(prescricoes_pac)}")
                        for item in prescricoes_pac:
                            col_data, col_acao = st.columns([3, 1])
                            col_data.markdown(f"📅 **Data e Hora:** {item['data_hora_str']} (`{item['nome_arquivo']}`)")
                            
                            if col_acao.button("📄 Ver / Reimprimir", key=f"ver_{item['file_id']}"):
                                texto_hist = item.get("texto", "")
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
