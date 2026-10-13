import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import re
import io
import json
import base64
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    NameObject,
    TextStringObject,
    DictionaryObject,
    ArrayObject,
    FloatObject,
    NumberObject,
    DecodedStreamObject,
    ByteStringObject,
)
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# ==========================================
# 1. CONFIGURAÇÃO DA PÁGINA E ESTILO VISUAL
# ==========================================
st.set_page_config(
    page_title="Sistema de Prescrições - UBS",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Lato:wght@400;700&display=swap');

.block-container {
    padding-top: 1.2rem !important;
    padding-bottom: 1rem !important;
    max-width: 98% !important;
}

div[data-testid="stTextArea"] textarea {
    font-family: 'Lato', sans-serif !important;
    font-size: 15.2px !important; 
    line-height: 1.35 !important;
    color: #000000 !important;
    background-color: #ffffff !important;
    border: 2px solid #cbd5e1 !important;
    border-radius: 6px !important;
    padding: 16px 20px !important;
    box-shadow: inset 0 1px 4px rgba(0,0,0,0.06) !important;
}
</style>
""", unsafe_allow_html=True)

NOME_CAMPO_PACIENTE_PDF = "CIDADÃO"
NOME_CAMPO_PRESCRICAO_PDF = "PRESCRIÇÃO"

NOME_ARQUIVO_HISTORICO = "historico_prescricoes.json"
ID_PADRAO_HISTORICO = "1RFtMetxdO2B8MioNsSC9SMi7MiUMCDC_"

FATOR_A4 = 11.41 / 8.0
TAMANHO_A4_PADRAO = 11.41
TAMANHO_A4_DATA_HORA = 8.56

MESES_PT = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"
]

# ==========================================
# 2. SISTEMA DE LOGIN
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
    if not texto: return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    sem_acento = "".join([c for c in nfkd if not unicodedata.combining(c)])
    return re.sub(r"\s+", " ", sem_acento).strip().upper()

def limpar_endereco_esus(end_bruto: str) -> str:
    if not end_bruto: return ""
    texto = str(end_bruto).strip()
    if texto.lower() in ["", "-", "nan", "none", "nao informada", "nao informado"]: return ""
    if re.match(r"^CPF\s*:", texto, flags=re.IGNORECASE): return ""
    texto = texto.split("|")[0].strip()
    texto = re.sub(r",\s*(\d+|S/N)\.\s*", r", \1, ", texto, flags=re.IGNORECASE)
    partes_hifen = texto.split(" - ")
    if len(partes_hifen) >= 3:
        texto = ", ".join(partes_hifen[:-1]) + " - " + partes_hifen[-1]
    return re.sub(r"\s+", " ", texto).strip(" ,.-")

@st.cache_data(ttl=3600)
def carregar_base_pacientes() -> pd.DataFrame:
    csv_id = st.secrets["drive"]["csv_file_id"]
    conteudo_bytes = baixar_arquivo_drive(csv_id)
    
    texto_decodificado = None
    for enc in ["utf-8-sig", "utf-8", "cp1252", "latin1"]:
        try:
            texto_decodificado = conteudo_bytes.decode(enc)
            break
        except UnicodeDecodeError: continue
            
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
    
    df = pd.read_csv(io.StringIO(texto_tabela), sep=";", dtype=str, on_bad_lines="skip", index_col=False)
    
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
            
            if rua_v and rua_v not in ["-", "nan"]: partes.append(f"{rua_v}, {num_v}" if (num_v and num_v not in ["-", "nan"]) else rua_v)
            if comp_v and comp_v not in ["-", "nan"]: partes.append(comp_v)
            if bairro_v and bairro_v not in ["-", "nan"]: partes.append(bairro_v)
            base_e = ", ".join(partes)
            if cid_v and cid_v not in ["-", "nan"]:
                cid_uf = f"{cid_v} - {uf_v}" if (uf_v and uf_v not in ["-", "nan"]) else cid_v
                end_str = f"{base_e}, {cid_uf}" if base_e else cid_uf
            else: end_str = base_e
        enderecos_finais.append(end_str)
        
    df["endereco_limpo"] = enderecos_finais
    df = df[(df["nome_limpo"] != "") | (df["doc_limpo"] != "")].copy()
    return df

@st.cache_data(ttl=300)
def carregar_pdf_template() -> bytes:
    pdf_id = st.secrets["drive"]["pdf_template_id"]
    return baixar_arquivo_drive(pdf_id)

# ==========================================
# 4. FUNÇÕES DE NEGÓCIO E EXTRAÇÃO AVULSA
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
    if df.empty: return []
        
    # Regex robusto para buscar CPF (com ou sem pontuação) evitando pegar números fora de contexto
    cpfs_encontrados = re.findall(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)", texto_pec)
    for cpf_str in cpfs_encontrados:
        num = re.sub(r"\D", "", cpf_str)
        achados = df[df["doc_limpo"] == num]
        if not achados.empty:
            return [extrair_dados_linha(row) for _, row in achados.iterrows()]
            
    cns_encontrados = re.findall(r"\b\d{15}\b", re.sub(r"[.\-]", "", texto_pec))
    for cns_str in cns_encontrados:
        achados = df[df["doc_limpo"] == cns_str]
        if not achados.empty:
            return [extrair_dados_linha(row) for _, row in achados.iterrows()]

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

def extrair_dados_brutos_avulsos(texto: str) -> dict:
    """Extrai os dados brutos de pacientes que não estão no CSV."""
    texto_limpo = (texto or "").strip()
    
    # 1. Tenta extrair CPF ou CNS
    cpf = ""
    cpf_match = re.search(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)", texto_limpo)
    if cpf_match:
        cpf = re.sub(r"\D", "", cpf_match.group())
    else:
        cns_match = re.search(r"\b\d{15}\b", re.sub(r"[.\-]", "", texto_limpo))
        if cns_match: cpf = cns_match.group()

    # 2. Tenta extrair Nascimento
    nascimento = ""
    nasc_match = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", texto_limpo)
    if nasc_match:
        nascimento = nasc_match.group(1)

    # 3. Tenta extrair Nome (Limpando barras, parentescos e gêneros)
    nome = ""
    linhas = texto_limpo.splitlines()
    if linhas:
        primeira_linha = linhas[0].strip()
        # Se for "Belmarques Rodrigues | Sexo Masculino", corta no "|"
        parte_nome = re.split(r"\||\-|–|Sexo|Idade|Nasceu", primeira_linha, flags=re.IGNORECASE)[0].strip()
        if len(parte_nome) > 5 and not re.search(r"\d", parte_nome) and "CPF" not in parte_nome.upper():
            nome = parte_nome

    # 4. Tenta extrair Endereço
    end = ""
    m_end = re.search(r"(?:Endereço|Endereco|Residência|Logradouro|CEP)[:\-\s]+([A-Za-zÀ-ÖØ-öø-ÿ0-9\s\,\.\-\|]{10,})(?:\n|\r|$)", texto_limpo, re.IGNORECASE)
    if m_end: end = m_end.group(1).strip()

    return {
        "nome": remover_acentos(nome),
        "cpf": cpf,
        "nascimento": nascimento,
        "endereco": end
    }

def obter_id_arquivo_historico() -> str:
    if "history_file_id" in st.secrets["drive"]: return st.secrets["drive"]["history_file_id"]
    pasta_id = st.secrets["drive"]["folder_historico_id"]
    query = f"'{pasta_id}' in parents and name = '{NOME_ARQUIVO_HISTORICO}' and trashed = false"
    res = drive_service.files().list(q=query, pageSize=1, fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    arquivos = res.get("files", [])
    if arquivos: return arquivos[0]["id"]
    return ID_PADRAO_HISTORICO

def carregar_todo_historico_drive() -> list:
    try:
        arq_id = obter_id_arquivo_historico()
        conteudo = baixar_arquivo_drive(arq_id).decode("utf-8", errors="replace").strip()
        if not conteudo: return []
        dados = json.loads(conteudo)
        return dados if isinstance(dados, list) else []
    except Exception: return []

def listar_prescricoes_drive(identificador_limpo: str = None) -> list:
    registros = carregar_todo_historico_drive()
    if identificador_limpo: registros = [r for r in registros if r.get("cpf_limpo") == identificador_limpo]
    return registros

def salvar_prescricao_drive(identificador_limpo: str, nome_paciente: str, texto_prescricao: str) -> str:
    arq_id = obter_id_arquivo_historico()
    registros = carregar_todo_historico_drive()
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    codigo_registro = f"{identificador_limpo}_{agora.strftime('%d-%m-%Y_%H-%M-%S')}"
    nome_referencia = f"{identificador_limpo}_{agora.strftime('%d-%m-%Y_%H-%M')}.txt"
    data_hora_str = agora.strftime("%d/%m/%Y às %H:%M")
    
    novo_item = {
        "file_id": codigo_registro, "nome_arquivo": nome_referencia,
        "cpf_limpo": identificador_limpo, "nome": nome_paciente,
        "data_hora_str": data_hora_str, "texto": texto_prescricao,
        "createdTime": agora.isoformat()
    }
    registros.insert(0, novo_item)
    json_bytes = json.dumps(registros, ensure_ascii=False, indent=2).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(json_bytes), mimetype="application/json", resumable=False)
    drive_service.files().update(fileId=arq_id, media_body=media, supportsAllDrives=True).execute()
    return nome_referencia

# ==========================================
# MOTOR DE MEDIÇÃO E DESENHO EM FONTE LATO
# ==========================================
@st.cache_data(ttl=3600)
def obter_larguras_fonte_lato() -> list[float]:
    try:
        pdf_bytes = carregar_pdf_template()
        reader = PdfReader(io.BytesIO(pdf_bytes))
        lato_obj = reader.trailer["/Root"]["/AcroForm"].get_object()["/DR"]["/Font"]["/Lato"].get_object()
        return [float(w) for w in lato_obj["/Widths"]]
    except Exception: return [500.0] * 256

def largura_texto_lato_pt(texto: str, font_size_interno: float, widths: list[float]) -> float:
    return (sum(widths[ch] for ch in texto.encode("cp1252", errors="replace")) / 1000.0) * font_size_interno

def quebrar_linhas_para_caixa(texto: str, largura_max_pt: float, font_size_interno: float, widths: list[float]) -> list[str]:
    linhas_finais = []
    for linha in (texto or "").splitlines():
        linha = linha.rstrip()
        if not linha:
            linhas_finais.append("")
            continue
        if largura_texto_lato_pt(linha, font_size_interno, widths) <= largura_max_pt:
            linhas_finais.append(linha)
            continue
        if "-----" in linha:
            while "-----" in linha and largura_texto_lato_pt(linha, font_size_interno, widths) > largura_max_pt:
                nova = re.sub(r"-{6,}", lambda m: m.group(0)[:-2], linha, count=1)
                if nova == linha: break
                linha = nova
            if largura_texto_lato_pt(linha, font_size_interno, widths) <= largura_max_pt:
                linhas_finais.append(linha)
                continue
        atual = ""
        for p in linha.split(" "):
            teste = f"{atual} {p}".strip() if atual else p
            if largura_texto_lato_pt(teste, font_size_interno, widths) <= largura_max_pt: atual = teste
            else:
                if atual: linhas_finais.append(atual)
                atual = p
        if atual: linhas_finais.append(atual)
    return linhas_finais

def calcular_ajuste_prescricao(texto: str, largura_caixa_pt: float = 350.0, altura_caixa_pt: float = 316.0, escala_a4: float = FATOR_A4) -> tuple[list[str], float, float]:
    widths = obter_larguras_fonte_lato()
    texto_limpo = (texto or "").strip()
    if not texto_limpo: return [""], 8.0, TAMANHO_A4_PADRAO
    tam_interno_max = TAMANHO_A4_PADRAO / escala_a4
    tam_interno_min = 4.2 / (escala_a4 / FATOR_A4)

    tam = tam_interno_max
    while tam >= tam_interno_min:
        linhas_quebradas = quebrar_linhas_para_caixa(texto_limpo, largura_caixa_pt, tam, widths)
        if len(linhas_quebradas) * (tam * 1.32) <= altura_caixa_pt:
            return linhas_quebradas, round(tam, 2), round(tam * escala_a4, 2)
        tam -= 0.10

    texto_compacto = re.sub(r"\n\s*\n+", "\n\n", texto_limpo)
    texto_compacto = re.sub(r"(^\s*(?:\d+\.|USO\s).+)\n\n+(?=\S)", r"\1\n", texto_compacto, flags=re.MULTILINE | re.IGNORECASE)
    tam = tam_interno_max
    while tam >= 3.5:
        linhas_quebradas = quebrar_linhas_para_caixa(texto_compacto, largura_caixa_pt, tam, widths)
        if len(linhas_quebradas) * (tam * 1.28) <= altura_caixa_pt:
            return linhas_quebradas, round(tam, 2), round(tam * escala_a4, 2)
        tam -= 0.10

    linhas_finais = quebrar_linhas_para_caixa(texto_compacto, largura_caixa_pt, 3.5, widths)
    return linhas_finais, 3.5, round(3.5 * escala_a4, 2)

def escapar_literal_pdf(texto: str) -> bytes:
    b = texto.encode("cp1252", errors="replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")

def construir_ap_stream_lato(linhas: list[str], largura_rect: float, altura_rect: float, font_size: float, centralizar: bool = False, multilinha: bool = True, widths: list[float] = None) -> bytes:
    if widths is None: widths = obter_larguras_fonte_lato()
    leading = font_size * 1.32
    y_inicial = altura_rect - font_size - 2.0 if multilinha else max(1.5, (altura_rect - font_size) / 2.0 + 1.0)
    comandos = [f"q\n/Tx BMC\nq\n1 1 {largura_rect - 2:.2f} {altura_rect - 2:.2f} re\nW\nn\nBT\n/Lato {font_size:.2f} Tf\n0 g".encode("ascii")]

    y_atual = y_inicial
    for idx, linha in enumerate(linhas):
        x_pos = max(2.0, (largura_rect - largura_texto_lato_pt(linha, font_size, widths)) / 2.0) if centralizar else 2.0
        if idx == 0: comandos.append(f"{x_pos:.2f} {y_atual:.2f} Td".encode("ascii"))
        else: comandos.append(f"0 {-leading:.2f} Td".encode("ascii"))
        comandos.append(b"(" + escapar_literal_pdf(linha) + b") Tj")
    comandos.append(b"ET\nQ\nEMC\nQ\n")
    return b"\n".join(comandos)

def preencher_pdf_template(dados_paciente: dict, texto_prescricao: str, campo_pac: str = NOME_CAMPO_PACIENTE_PDF, campo_presc: str = NOME_CAMPO_PRESCRICAO_PDF) -> tuple[bytes, float]:
    pdf_bytes = carregar_pdf_template()
    writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes))
    widths = obter_larguras_fonte_lato()
    pagina = writer.pages[0]
    escala_a4 = FATOR_A4 if float(pagina.mediabox.width) < 500 else 1.0

    tam_padrao_int = round(TAMANHO_A4_PADRAO / escala_a4, 2)
    tam_datahora_int = round(TAMANHO_A4_DATA_HORA / escala_a4, 2)

    linha_1 = f"{dados_paciente['nome']} - CPF: {dados_paciente['cpf_formatado']}"
    endereco_val = limpar_endereco_esus(dados_paciente.get("endereco", ""))
    texto_cidadao = f"{linha_1}\n{endereco_val}" if endereco_val else linha_1

    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    texto_data = f"Divinópolis - MG, {agora.strftime('%d')} de {MESES_PT[agora.month - 1]} de {agora.year}"
    texto_data_hora = f"{agora.strftime('%d/%m/%Y')} às {agora.strftime('%H:%M')}"

    larg_presc_box, alt_presc_box = 350.0, 316.0
    if "/Annots" in pagina:
        for annot_ref in pagina["/Annots"]:
            if annot_ref.get_object().get("/T") == campo_presc:
                r = [float(x) for x in annot_ref.get_object().get("/Rect")]
                larg_presc_box, alt_presc_box = abs(r[2] - r[0]) - 5.0, abs(r[3] - r[1]) - 5.0
                break

    linhas_presc, tam_presc_int, tam_presc_a4 = calcular_ajuste_prescricao(texto_prescricao, larg_presc_box, alt_presc_box, escala_a4)
    lato_font_ref = writer._root_object["/AcroForm"].get_object()["/DR"].get_object()["/Font"].get_object()["/Lato"]

    config_campos = {
        campo_pac: {"valor": texto_cidadao, "linhas": texto_cidadao.splitlines(), "size": tam_padrao_int, "center": False, "multiline": True},
        campo_presc: {"valor": "\n".join(linhas_presc), "linhas": linhas_presc, "size": tam_presc_int, "center": False, "multiline": True},
        "DATA": {"valor": texto_data, "linhas": [texto_data], "size": tam_padrao_int, "center": True, "multiline": False},
        "DATA_HORA": {"valor": texto_data_hora, "linhas": [texto_data_hora], "size": tam_datahora_int, "center": False, "multiline": False}
    }

    for p in writer.pages:
        if "/Annots" not in p: continue
        for annot_ref in p["/Annots"]:
            annot = annot_ref.get_object()
            nome_c = annot.get("/T")
            if nome_c not in config_campos: continue
            cfg = config_campos[nome_c]
            
            annot[NameObject("/DR")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/Lato"): lato_font_ref})})
            annot[NameObject("/DA")] = TextStringObject(f"/Lato {cfg['size']} Tf 0 g")
            annot[NameObject("/V")] = TextStringObject(cfg["valor"])
            if "/AA" in annot: del annot["/AA"]

            rect = [float(x) for x in annot.get("/Rect")]
            w_box, h_box = abs(rect[2] - rect[0]), abs(rect[3] - rect[1])
            stream_bytes = construir_ap_stream_lato(cfg["linhas"], w_box, h_box, cfg["size"], cfg["center"], cfg["multiline"], widths)

            ap_stream_obj = DecodedStreamObject.initialize_from_dictionary({
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/FormType"): NumberObject(1),
                NameObject("/BBox"): ArrayObject([FloatObject(0.0), FloatObject(0.0), FloatObject(w_box), FloatObject(h_box)]),
                NameObject("/Resources"): DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/Lato"): lato_font_ref})}),
                "__streamdata__": ByteStringObject(stream_bytes),
                NameObject("/Length"): NumberObject(0)
            })
            annot[NameObject("/AP")] = DictionaryObject({NameObject("/N"): writer._add_object(ap_stream_obj)})

    writer.set_need_appearances_writer(False)
    if "/Names" in writer._root_object and "/JavaScript" in writer._root_object["/Names"].get_object():
        del writer._root_object["/Names"].get_object()["/JavaScript"]

    output_buffer = io.BytesIO()
    writer.write(output_buffer)
    output_buffer.seek(0)
    return output_buffer.read(), tam_presc_a4

def renderizar_visualizador_e_impressao_pdf(pdf_bytes: bytes, altura_px: int = 760):
    b64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
    html_code = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
        <style>
            body {{ margin: 0; padding: 0; font-family: 'Lato', sans-serif; background: #f1f5f9; }}
            .toolbar {{ display: flex; gap: 10px; padding: 10px 14px; background: #1e293b; color: white; align-items: center; justify-content: space-between; border-radius: 8px 8px 0 0; }}
            .btn-print {{ background: #16a34a; color: white; border: none; padding: 9px 18px; font-size: 14px; font-weight: bold; border-radius: 6px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }}
            .btn-print:hover {{ background: #15803d; }}
            .btn-open {{ background: #3b82f6; color: white; border: none; padding: 9px 14px; font-size: 13px; font-weight: bold; border-radius: 6px; cursor: pointer; }}
            #canvas-container {{ display: flex; flex-direction: column; align-items: center; padding: 14px; overflow-y: auto; max-height: {altura_px - 65}px; }}
            canvas {{ box-shadow: 0 4px 14px rgba(0,0,0,0.18); border-radius: 4px; background: white; max-width: 100%; height: auto !important; }}
        </style>
    </head>
    <body>
        <div class="toolbar">
            <span>📄 <b>Visualização A4 (Fonte Lato)</b></span>
            <div>
                <button class="btn-print" onclick="imprimirDireto()">🖨️ IMPRIMIR DIRETO AGORA</button>
                <button class="btn-open" onclick="abrirNovaAba()">↗️ Abrir em Nova Aba</button>
            </div>
        </div>
        <div id="canvas-container"><canvas id="pdf-canvas"></canvas></div>
        <iframe id="print-iframe" style="display:none;"></iframe>
        <script>
            const b64Data = "{b64_pdf}";
            const byteCharacters = atob(b64Data);
            const byteArray = new Uint8Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) byteArray[i] = byteCharacters.charCodeAt(i);
            const blobUrl = URL.createObjectURL(new Blob([byteArray], {{ type: 'application/pdf' }}));
            const printFrame = document.getElementById('print-iframe');
            printFrame.src = blobUrl;

            function imprimirDireto() {{
                try {{ printFrame.contentWindow.focus(); printFrame.contentWindow.print(); }} 
                catch (e) {{ const win = window.open(blobUrl, '_blank'); if (win) win.focus(); }}
            }}
            function abrirNovaAba() {{ window.open(blobUrl, '_blank'); }}

            const pdfjsLib = window['pdfjs-dist/build/pdf'];
            pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
            pdfjsLib.getDocument({{ data: byteArray }}).promise.then(function(pdf) {{
                pdf.getPage(1).then(function(page) {{
                    const viewport = page.getViewport({{ scale: 2.0 }});
                    const canvas = document.getElementById('pdf-canvas');
                    canvas.height = viewport.height; canvas.width = viewport.width;
                    page.render({{ canvasContext: canvas.getContext('2d'), viewport: viewport }});
                }});
            }});
        </script>
    </body>
    </html>
    """
    components.html(html_code, height=altura_px, scrolling=False)

# ==========================================
# 5. MENU LATERAL
# ==========================================
with st.sidebar:
    st.header("🩺 Menu Principal")
    aba_selecionada = st.radio("Navegação:", ["📝 Emitir Prescrição", "🗂️ Histórico de Pacientes"])
    st.divider()
    with st.expander("⚙️ Conferência da Base"):
        try: st.caption(f"Pacientes no CSV: **{len(carregar_base_pacientes())}**")
        except Exception as e_csv: st.error(f"Erro no CSV: {e_csv}")
        campo_pac_input = st.text_input("Campo Cidadão:", value=NOME_CAMPO_PACIENTE_PDF)
        campo_presc_input = st.text_input("Campo Prescrição:", value=NOME_CAMPO_PRESCRICAO_PDF)

    if st.button("🔄 Recarregar CSV / Template do Drive", use_container_width=True):
        st.cache_data.clear()
        st.success("Cache atualizado!")
    if st.button("🚪 Sair", use_container_width=True):
        st.session_state["autenticado"] = False
        st.rerun()

# ==========================================
# 6. TELA 1: EMITIR PRESCRIÇÃO
# ==========================================
if aba_selecionada == "📝 Emitir Prescrição":
    col_input_pec, col_dados_pac = st.columns([1, 1.3], gap="medium")
    
    with col_input_pec:
        texto_pec = st.text_area(
            "📋 1. Cole o texto do PEC (e-SUS) ou digite Nome / CPF do paciente:",
            height=95,
            placeholder="Cole aqui os dados do prontuário..."
        )

    candidatos = buscar_paciente_no_texto_pec(texto_pec) if texto_pec else []

    if texto_pec:
        with col_dados_pac:
            paciente = None
            
            # --- LÓGICA DE CADASTRO AVULSO ---
            if not candidatos:
                st.warning("⚠️ Paciente não encontrado no CSV. **Cadastro Avulso:**")
                dados_ext = extrair_dados_brutos_avulsos(texto_pec)
                
                c_nome, c_cpf, c_nasc = st.columns([2, 1, 1])
                nome_avulso = c_nome.text_input("Nome Completo (Obrigatório):", value=dados_ext["nome"], key="nome_avulso")
                cpf_avulso = c_cpf.text_input("CPF (Obrigatório):", value=formatar_documento(dados_ext["cpf"]) if dados_ext["cpf"] else "", key="cpf_avulso")
                nasc_avulso = c_nasc.text_input("Nascimento:", value=dados_ext["nascimento"], key="nasc_avulso", placeholder="DD/MM/AAAA")
                
                if nome_avulso.strip() and cpf_avulso.strip():
                    paciente = {
                        "nome": remover_acentos(nome_avulso.strip()),
                        "cpf_limpo": re.sub(r"\D", "", cpf_avulso),
                        "cpf_formatado": formatar_documento(cpf_avulso),
                        "endereco": dados_ext["endereco"],
                        "nascimento": nasc_avulso.strip()
                    }
                else:
                    st.info("Preencha o Nome e o CPF acima para liberar a emissão da receita.")
            
            # --- ENCONTRADO NA BASE DE DADOS NORMAL ---
            else:
                if len(candidatos) > 1:
                    opcoes = {f"{p['nome']} — CPF: {p['cpf_formatado']}": p for p in candidatos}
                    escolha = st.selectbox("👥 Pacientes encontrados. Selecione:", list(opcoes.keys()))
                    paciente = opcoes[escolha]
                else:
                    paciente = candidatos[0]

            # --- RENDERIZA O PACIENTE (AVULSO OU DO CSV) ---
            if paciente:
                id_busca = paciente["cpf_limpo"]

                if st.session_state.get("paciente_ativo_id") != id_busca:
                    historico_cpf = listar_prescricoes_drive(id_busca)
                    if historico_cpf:
                        st.session_state[f"editor_presc_{id_busca}"] = historico_cpf[0].get("texto", "")
                        st.session_state["data_ultima_presc"] = historico_cpf[0].get("data_hora_str", "")
                    else:
                        st.session_state[f"editor_presc_{id_busca}"] = ""
                        st.session_state["data_ultima_presc"] = None
                    st.session_state["paciente_ativo_id"] = id_busca

                with st.container(border=True):
                    st.markdown(f"**Cidadão:** `{paciente['nome']} - CPF: {paciente['cpf_formatado']}`")
                    
                    if not paciente.get("endereco"):
                        st.caption("⚠️ Endereço não preenchido. A linha do endereço ficará vazia na receita se você não informar abaixo.")
                        
                    endereco_editavel = st.text_input(
                        "📍 Endereço (2ª linha do campo CIDADÃO):",
                        value=paciente["endereco"],
                        key=f"end_{id_busca}",
                        label_visibility="collapsed",
                        placeholder="Endereço desejável (Rua, Número, Bairro, Cidade - MG)"
                    )
                    paciente["endereco"] = endereco_editavel.strip()

        # ==================================
        # EXIBE A TELA DE PRESCRIÇÃO E PDF
        # ==================================
        if paciente:
            st.divider()
            col_editor, col_preview = st.columns([1.05, 0.95], gap="large")

            with col_editor:
                if st.session_state.get("data_ultima_presc"):
                    st.caption(f"🔄 Histórico carregado automaticamente da última receita (**{st.session_state['data_ultima_presc']}**)")
                else:
                    st.caption("✨ Iniciando prescrição em branco")

                texto_editado = st.text_area(
                    "💊 2. Campo PRESCRIÇÃO (Proporção real da folha A4 em fonte Lato):",
                    key=f"editor_presc_{id_busca}",
                    height=620
                )

                linhas_calc, _, tam_lato_a4 = calcular_ajuste_prescricao(texto_editado)
                cor_badge = "green" if tam_lato_a4 >= 11.4 else "orange"
                st.markdown(
                    f"🔤 Fonte **Lato** aplicada automaticamente no PDF: "
                    f":{cor_badge}[**{str(tam_lato_a4).replace('.', ',')} pt**] *(Máximo: 11,41 pt)* — "
                    f"Total de linhas no quadro: **{len(linhas_calc)}**"
                )

                col_btn_salvar, col_btn_dl = st.columns([1.3, 1])
                with col_btn_salvar:
                    salvar_clicado = st.button("💾 Salvar no Histórico do Google Drive", type="primary", use_container_width=True)

            pdf_preview_bytes, _ = preencher_pdf_template(paciente, texto_editado, campo_pac_input, campo_presc_input)

            if salvar_clicado:
                if not texto_editado.strip():
                    st.warning("Digite a prescrição antes de salvar no histórico.")
                else:
                    with st.spinner("Gravando prescrição no histórico..."):
                        nome_salvo = salvar_prescricao_drive(id_busca, paciente["nome"], "\n".join(linhas_calc))
                        st.session_state["data_ultima_presc"] = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y às %H:%M")
                    st.success(f"✅ Prescrição salva no Google Drive (`{nome_salvo}`)!")

            with col_btn_dl:
                agora_local = datetime.now(ZoneInfo("America/Sao_Paulo"))
                nome_pdf_dl = f"Prescricao_{paciente['nome'].replace(' ', '_')}_{agora_local.strftime('%d-%m-%Y')}.pdf"
                st.download_button(label="⬇️ Baixar PDF", data=pdf_preview_bytes, file_name=nome_pdf_dl, mime="application/pdf", use_container_width=True)

            with col_preview:
                renderizar_visualizador_e_impressao_pdf(pdf_preview_bytes, altura_px=740)

# ==========================================
# 7. TELA 2: HISTÓRICO E BUSCA POR PACIENTE
# ==========================================
elif aba_selecionada == "🗂️ Histórico de Pacientes":
    st.title("🗂️ Histórico de Prescrições Emitidas")
    termo_busca = st.text_input("🔍 Digite o Nome ou CPF do paciente:")
    
    if termo_busca:
        df_pac = carregar_base_pacientes()
        termo_limpo_num = re.sub(r"\D", "", termo_busca)
        termo_norm = remover_acentos(termo_busca)
        
        pacientes_encontrados = df_pac[df_pac["doc_limpo"].str.contains(termo_limpo_num, na=False)] if termo_limpo_num and len(termo_limpo_num) >= 3 else df_pac[df_pac["nome_busca"].str.contains(re.escape(termo_norm), na=False)]
            
        if pacientes_encontrados.empty:
            st.warning("Nenhum paciente encontrado no CSV com esse termo.")
        else:
            for _, pac_row in pacientes_encontrados.head(20).iterrows():
                dados_pac = extrair_dados_linha(pac_row)
                id_pac, nome_pac = dados_pac["cpf_limpo"], dados_pac["nome"]
                
                with st.expander(f"👤 {nome_pac} — CPF: {dados_pac['cpf_formatado']}", expanded=True):
                    st.caption(f"📍 **Endereço:** {dados_pac['endereco'] if dados_pac['endereco'] else 'Não informado no CSV'}")
                    prescricoes_pac = listar_prescricoes_drive(id_pac)
                    
                    if not prescricoes_pac: st.caption("Nenhuma prescrição emitida para este paciente até o momento.")
                    else:
                        for item in prescricoes_pac:
                            col_data, col_acao = st.columns([3, 1])
                            col_data.markdown(f"📅 **Data e Hora:** {item['data_hora_str']} (`{item['nome_arquivo']}`)")
                            if col_acao.button("📄 Visualizar / Imprimir", key=f"ver_{item['file_id']}"):
                                pdf_reimp, _ = preencher_pdf_template(dados_pac, item.get("texto", ""), campo_pac_input, campo_presc_input)
                                renderizar_visualizador_e_impressao_pdf(pdf_reimp, altura_px=680)
