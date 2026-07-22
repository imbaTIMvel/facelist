"""
FACELIST — Modificador de Folha de Rosto (Identificador de Seções de Documento)
Lê PDFs de processos administrativos, identifica as seções neles contidas
(por palavras-chave) e insere a listagem dessas seções como caixa de texto
na folha de rosto (página 1) de uma cópia do documento.

Baseado na proposta de automação e seguindo o padrão visual do BolsistaDB (DAF/HBR).
"""

import os
import re
import sys
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional
import fitz  # PyMuPDF

from PySide6.QtWidgets import (
    QApplication, QWidget, QPushButton, QLabel, QFileDialog,
    QMessageBox, QVBoxLayout, QHBoxLayout, QFrame, QScrollArea,
    QCheckBox
)
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFontMetrics
from PySide6.QtCore import Qt, QThread, Signal, QRectF

def resource_path(rel):
    """Resolve um caminho relativo tanto em modo de desenvolvimento (rodando
    o .py direto) quanto empacotado pelo PyInstaller (--onefile), onde os
    arquivos de --add-data são extraídos para uma pasta temporária indicada
    por sys._MEIPASS."""
    try:
        base = sys._MEIPASS
    except Exception:
        base = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base, rel)


try:
    import pytesseract
    from PIL import Image
    OCR_DISPONIVEL = True
except ImportError:
    OCR_DISPONIVEL = False

if OCR_DISPONIVEL:
    # 1ª tentativa: Tesseract embutido no pacote (pasta "tesseract-bin",
    # incluída via --add-data no PyInstaller). Uso preferencial no .exe final.
    _TESSERACT_EMBUTIDO = resource_path(os.path.join("tesseract-bin", "tesseract.exe"))
    _TESSDATA_EMBUTIDO = resource_path(os.path.join("tesseract-bin", "tessdata"))

    # 2ª tentativa (fallback): instalação padrão do Tesseract no sistema,
    # útil durante o desenvolvimento (rodando o .py direto, sem empacotar).
    _TESSERACT_SISTEMA = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

    if os.path.isfile(_TESSERACT_EMBUTIDO):
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_EMBUTIDO
        os.environ["TESSDATA_PREFIX"] = _TESSDATA_EMBUTIDO
    elif os.path.isfile(_TESSERACT_SISTEMA):
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_SISTEMA
    # Se nenhum dos dois for encontrado, mantém o comportamento padrão do
    # pytesseract (procura "tesseract" no PATH do sistema).


# ============================================================================
# CONFIGURAÇÃO DO OCR (PÁGINAS ESCANEADAS)
# ----------------------------------------------------------------------------
# Algumas páginas de um PDF podem ter sido inseridas como imagem escaneada
# (sem texto legível/selecionável pelo próprio PDF). Nesses casos, o PyMuPDF
# retorna pouco ou nenhum texto para a página, e o OCR entra como fallback:
# a página é rasterizada em imagem e o texto é extraído via Tesseract.
#
# Requer, no ambiente de execução (fora do desenvolvimento no chat):
#   pip install pytesseract pillow
#   E o motor Tesseract instalado no sistema, com o pacote de idioma
#   português (ex.: no Windows, instalar o Tesseract-OCR e garantir que
#   "por.traineddata" esteja na pasta tessdata; no Linux/apt:
#   sudo apt install tesseract-ocr tesseract-ocr-por).
# ============================================================================
OCR_ATIVADO = True          # liga/desliga o fallback de OCR
OCR_MIN_CARACTERES = 20     # abaixo disso, a página é considerada "sem texto nativo"
OCR_DPI = 300               # resolução da rasterização (maior = mais preciso, porém mais lento)
OCR_IDIOMA = "por"          # idioma do pacote de dados do Tesseract
# ============================================================================


# ============================================================================
# CONFIGURAÇÃO DA CAIXA DE TEXTO (LISTA DE SEÇÕES) NA FOLHA DE ROSTO
# ----------------------------------------------------------------------------
# Ajuste os valores abaixo para reposicionar, redimensionar ou reformatar a
# lista inserida na página 1 de cada PDF processado.
#
# Sistema de coordenadas do PyMuPDF: origem (0,0) no canto SUPERIOR ESQUERDO
# da página; eixo Y cresce para BAIXO. Unidade: pontos (1 pt = 1/72 pol.).
# Uma página A4 tipicamente mede 595 x 842 pt (retrato).
#
# TEXTBOX_X / TEXTBOX_Y  -> canto superior esquerdo da caixa que recebe a LISTA
# TEXTBOX_WIDTH / HEIGHT -> dimensões da caixa que recebe a LISTA
# ============================================================================
TEXTBOX_X = 110
TEXTBOX_Y = 540
TEXTBOX_WIDTH = 200
TEXTBOX_HEIGHT = 300

TEXTBOX_FONT = "helv"        # fontes base do PyMuPDF: "helv", "times" ou "cour"
TEXTBOX_FONT_SIZE = 9
TEXTBOX_COLOR = (0, 0, 0)    # RGB 0-1
TEXTBOX_ALIGN = 0            # 0 = esquerda, 1 = centro, 2 = direita
# ============================================================================


# ==============================
# DICIONÁRIO DE SEÇÕES
# ==============================

@dataclass
class SectionDef:
    key: str                          # identificador interno único
    nome_base: str                    # termo de nomenclatura padrão
    match_fn: Callable[[str], bool]   # recebe texto da página (lower, espaços normalizados)
    min_page: Optional[int] = None    # página mínima (1-indexed) para considerar ocorrência válida
    single_page: bool = False         # metadado informativo (não afeta o algoritmo)


def _tem(kw):
    return lambda t, kw=kw: kw in t


def _tem_todas(*kws):
    return lambda t, kws=kws: all(kw in t for kw in kws)


def _tem_alguma(*kws):
    return lambda t, kws=kws: any(kw in t for kw in kws)


# A ordem desta lista é a ORDEM DE PRIORIDADE de busca (conforme a proposta):
# ao encontrar mais de uma seção "candidata" na mesma página, a que aparece
# primeiro nesta lista tem prioridade e "toma" a página; a outra segue sendo
# procurada a partir da próxima página livre.
SECTION_DEFS = [
    SectionDef("folha_rosto", "Folha de Rosto",
               _tem_alguma("natureza de dispêndio", "natureza do dispêndio"), single_page=True),
    SectionDef("mapa_cotacao", "Mapa de Cotação",
               _tem("mapa de cotação")),
    SectionDef("justificativa_dispensa", "Justificativa de Dispensa",
               _tem("justificativa da dispensa")),
    SectionDef("contrato", "Contrato",
               _tem("contrato de ")),
    SectionDef("relatorio_viagem", "Relatório de Viagem",
               _tem("relatório de viagem")),
    SectionDef("folha_contagem_diarias", "Folha de Contagem de Diárias",
               _tem("quantidade de diárias disponibilizadas")),
    # "Cartões de Embarque" não tem palavra-chave própria: é inserida
    # automaticamente na listagem final (ver detectar_secoes) quando há
    # Relatório de Viagem.
    SectionDef("bilhete_eletronico", "Bilhete Eletrônico",
               _tem("bilhete eletrônico")),
    SectionDef("nota_fiscal", "Nota Fiscal",
               _tem("nota fiscal")),
    SectionDef("fatura_passagens", "Fatura das Passagens",
               _tem_todas("nº fatura", "passageiro")),
    SectionDef("nota_debito_passagens", "Nota de Débito das Passagens",
               lambda t: (("notas de débito" in t) or ("nota de débito" in t))
                          and ("passageiro" in t)),
    SectionDef("boleto", "Boleto",
               _tem_alguma("pagável", "boleto"), min_page=4),
    SectionDef("comprovante", "Comprovante",
               _tem_alguma("comprovante de pagamento", "comprovante de transferência",
                           "comprovante provisório", "comprovante pagamento"),
               min_page=4),
]


# ==============================
# ALGORITMO DE DETECÇÃO DE SEÇÕES
# ==============================

def _ocr_pagina(page, dpi=OCR_DPI, idioma=OCR_IDIOMA):
    """Rasteriza a página (via PyMuPDF) e extrai o texto com Tesseract OCR.
    Retorna string vazia em caso de falha (OCR indisponível, erro do motor, etc.)."""
    if not OCR_DISPONIVEL:
        return ""
    try:
        zoom = dpi / 72
        matriz = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matriz, colorspace=fitz.csRGB, alpha=False)
        imagem = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return pytesseract.image_to_string(imagem, lang=idioma)
    except Exception:
        return ""


def extrair_textos_paginas(doc):
    """Retorna (textos, textos_brutos, paginas_ocr, paginas_sem_texto):
        textos           -> lista de strings (uma por página), em lower case
                             e com espaços/quebras de linha normalizados
                             (usada na detecção por palavra-chave).
        textos_brutos    -> lista de strings (uma por página) com o texto
                             original (preservando maiúsculas/minúsculas e
                             quebras de linha), usada para localizar valores
                             monetários na mesma linha de uma palavra-chave.
        paginas_ocr       -> páginas (1-indexed) em que o texto nativo do PDF
                             veio insuficiente e o OCR foi usado com sucesso.
        paginas_sem_texto -> páginas (1-indexed) que ficaram com texto
                             insuficiente mesmo após a tentativa de OCR (ou
                             sem tentativa, se o OCR estiver desativado ou
                             indisponível no ambiente)."""
    textos = []
    textos_brutos = []
    paginas_ocr = []
    paginas_sem_texto = []

    for indice, page in enumerate(doc, start=1):
        texto_original = page.get_text("text") or ""
        insuficiente = len(re.sub(r"\s+", "", texto_original)) < OCR_MIN_CARACTERES

        if insuficiente and OCR_ATIVADO:
            texto_ocr = _ocr_pagina(page)
            if len(re.sub(r"\s+", "", texto_ocr)) >= OCR_MIN_CARACTERES:
                texto_original = texto_ocr
                paginas_ocr.append(indice)
            else:
                paginas_sem_texto.append(indice)
        elif insuficiente:
            paginas_sem_texto.append(indice)

        textos_brutos.append(texto_original)

        texto_normalizado = texto_original.lower()
        texto_normalizado = re.sub(r"\s+", " ", texto_normalizado)
        textos.append(texto_normalizado)

    return textos, textos_brutos, paginas_ocr, paginas_sem_texto


# Palavras-chave que indicam o valor do comprovante, quando aparecem na
# mesma linha de um valor monetário (ex.: "VALOR DO DOCUMENTO: R$ 1.234,56").
VALOR_COMPROVANTE_KEYWORDS = (
    "valor do documento",
    "valor cobrado",
    "valor da transferência",
    "valor do pagamento",
)


def _formatar_valor_monetario(bruto):
    """Recebe um trecho de texto contendo um valor monetário (ex.:
    '1234,56', '1.234,56', '1 234,56') e retorna sempre no formato
    padronizado X.XXX,XX (string, para preservar pontos e vírgula).
    Retorna None se não for possível interpretar um valor válido."""
    m = re.search(r"(\d[\d.\s]*),(\d{2})", bruto)
    if not m:
        return None
    parte_inteira_bruta, parte_decimal = m.group(1), m.group(2)
    digitos = re.sub(r"\D", "", parte_inteira_bruta)
    if not digitos:
        return None
    grupos = []
    while digitos:
        grupos.insert(0, digitos[-3:])
        digitos = digitos[:-3]
    parte_inteira = ".".join(grupos)
    return f"{parte_inteira},{parte_decimal}"


def _extrair_valor_comprovante(texto_pagina_bruto):
    """Procura, linha a linha, por uma das palavras-chave de valor
    (VALOR_COMPROVANTE_KEYWORDS) e, se encontrada, extrai o valor monetário
    presente na mesma linha, já formatado como X.XXX,XX. Retorna None se
    nenhuma linha combinar palavra-chave + valor."""
    for linha in texto_pagina_bruto.splitlines():
        linha_lower = linha.lower()
        if any(kw in linha_lower for kw in VALOR_COMPROVANTE_KEYWORDS):
            m = re.search(r"(\d[\d.\s]*,\d{2})", linha)
            if m:
                valor = _formatar_valor_monetario(m.group(1))
                if valor:
                    return valor
    return None


def _decidir_nome_comprovante(pagina_comprovante, encontrados, page_texts, page_texts_brutos):
    """Decide, para documentos de viagem, se o termo a listar deve ser
    'Comprovante' ou 'Comprovante de Pagamento das Diárias': extrai o valor
    do comprovante (palavras-chave de valor na mesma linha) e verifica se
    esse valor aparece na página de início da seção 'Folha de Contagem de
    Diárias' (quando esta existir)."""
    texto_bruto_comprovante = page_texts_brutos[pagina_comprovante - 1]
    valor_comprovante = _extrair_valor_comprovante(texto_bruto_comprovante)

    if valor_comprovante is None:
        return "Comprovante"

    if "folha_contagem_diarias" not in encontrados:
        return "Comprovante"

    pagina_diarias = encontrados["folha_contagem_diarias"]
    texto_diarias = page_texts[pagina_diarias - 1]  # já normalizado (lower + espaços únicos)

    if valor_comprovante in texto_diarias:
        return "Comprovante de Pagamento das Diárias"
    return "Comprovante"


def detectar_secoes(page_texts, page_texts_brutos):
    """
    (docstring igual à anterior)
    """
    n_paginas = len(page_texts)
    avisos = []
    encontrados = {}

    for pagina in range(1, n_paginas + 1):
        texto = page_texts[pagina - 1]
        for sec in SECTION_DEFS:
            if sec.key in encontrados:
                continue
            if sec.min_page is not None and pagina < sec.min_page:
                continue
            if sec.match_fn(texto):
                encontrados[sec.key] = pagina
                break

    ordenados = sorted(encontrados.items(), key=lambda kv: kv[1])
    tem_viagem = "relatorio_viagem" in encontrados

    lista_termos = []
    for key, _pagina in ordenados:
        if key == "folha_rosto":
            continue
        sec = next(s for s in SECTION_DEFS if s.key == key)
        nome = sec.nome_base
        if key == "boleto" and tem_viagem:
            nome = "Boleto das Passagens"
        elif key == "comprovante" and tem_viagem:
            nome = _decidir_nome_comprovante(_pagina, encontrados, page_texts, page_texts_brutos)
        lista_termos.append(nome)

    if tem_viagem:
        idx = lista_termos.index("Relatório de Viagem") + 1
        lista_termos.insert(idx, "Cartões de Embarque")

    if "mapa_cotacao" in encontrados:
        idx = lista_termos.index("Mapa de Cotação") + 1
        lista_termos.insert(idx, "Anexos das Cotações")

    if "folha_rosto" not in encontrados:
        encontrados["folha_rosto"] = 1
        avisos.append(
            "Folha de Rosto não identificada por palavra-chave "
            "('Natureza de Dispêndio'/'Natureza do Dispêndio' não localizada); "
            "assumida a página 1 como folha de rosto (fallback)."
        )
    if not lista_termos:
        avisos.append("Nenhuma palavra-chave encontrada no documento.")

    return lista_termos, avisos, encontrados


def inserir_lista_folha_rosto(doc, lista_termos):
    """
    Insere a lista de seções identificadas na página 1 (folha de rosto) do
    documento. Retorna True se o texto coube no espaço definido em
    TEXTBOX_WIDTH/TEXTBOX_HEIGHT, False se foi cortado por falta de espaço.
    """
    page = doc[0]

    if lista_termos:
        texto_lista = "\n".join(f"- {termo}" for termo in lista_termos)
    else:
        texto_lista = "(nenhuma seção identificada)"

    rect = fitz.Rect(
        TEXTBOX_X,
        TEXTBOX_Y,
        TEXTBOX_X + TEXTBOX_WIDTH,
        TEXTBOX_Y + TEXTBOX_HEIGHT,
    )
    espaco_restante = page.insert_textbox(
        rect,
        texto_lista,
        fontsize=TEXTBOX_FONT_SIZE,
        fontname=TEXTBOX_FONT,
        color=TEXTBOX_COLOR,
        align=TEXTBOX_ALIGN,
    )
    return espaco_restante >= 0


# ==============================
# PROCESSAMENTO DE ARQUIVOS
# ==============================

def encontrar_pdfs(pasta_entrada):
    """Localiza recursivamente todos os PDFs em pasta_entrada, ignorando
    qualquer subpasta 'output' já existente (de uma execução anterior)."""
    pasta_saida_norm = os.path.normpath(os.path.join(pasta_entrada, "output"))
    pdfs = []
    for raiz, _dirs, arquivos in os.walk(pasta_entrada):
        raiz_norm = os.path.normpath(raiz)
        if raiz_norm == pasta_saida_norm or raiz_norm.startswith(pasta_saida_norm + os.sep):
            continue
        for nome in arquivos:
            if nome.lower().endswith(".pdf"):
                pdfs.append(os.path.join(raiz, nome))
    return pdfs


def processar_pdf(path_entrada, path_saida):
    """
    Processa um único PDF: extrai texto, detecta seções, insere a lista na
    folha de rosto e salva a cópia modificada em path_saida.
    Retorna (sucesso, n_termos, avisos).
    """
    try:
        doc = fitz.open(path_entrada)
    except Exception as e:
        return False, 0, [f"Documento não legível/corrompido ({e})."]

    if doc.page_count == 0:
        doc.close()
        return False, 0, ["Documento sem páginas."]

    try:
        page_texts, page_texts_brutos, paginas_ocr, paginas_sem_texto = extrair_textos_paginas(doc)
        lista_termos, avisos, _encontrados = detectar_secoes(page_texts, page_texts_brutos)

        if paginas_ocr:
            paginas_str = ", ".join(str(p) for p in paginas_ocr)
            avisos.append(f"OCR aplicado (texto nativo insuficiente) nas páginas: {paginas_str}.")

        if paginas_sem_texto:
            paginas_str = ", ".join(str(p) for p in paginas_sem_texto)
            if OCR_ATIVADO and not OCR_DISPONIVEL:
                avisos.append(
                    f"Páginas sem texto nativo suficiente ({paginas_str}); OCR está ativado, "
                    "mas pytesseract/Pillow não estão instalados neste ambiente."
                )
            elif OCR_ATIVADO:
                avisos.append(
                    f"Páginas sem texto suficiente mesmo após OCR ({paginas_str}); "
                    "verifique a qualidade da digitalização ou o pacote de idioma do Tesseract."
                )
            else:
                avisos.append(f"Páginas sem texto nativo suficiente ({paginas_str}); OCR está desativado.")

        coube = inserir_lista_folha_rosto(doc, lista_termos)
        if not coube:
            avisos.append("Lista de seções excedeu o espaço definido na caixa de texto (texto pode ter sido cortado).")

        os.makedirs(os.path.dirname(path_saida), exist_ok=True)
        doc.save(path_saida)
        doc.close()
        return True, len(lista_termos), avisos
    except Exception as e:
        doc.close()
        return False, 0, [f"Erro durante processamento: {e}"]


def processar_pasta(pasta_entrada):
    """Orquestra o processamento de todos os PDFs de pasta_entrada, grava o
    error_logs.txt e retorna um resumo (dict) da execução."""
    pasta_saida = os.path.join(pasta_entrada, "output")
    os.makedirs(pasta_saida, exist_ok=True)

    pdfs = encontrar_pdfs(pasta_entrada)

    n_modificados = 0
    n_termos_total = 0
    linhas_log = []

    for path_pdf in pdfs:
        rel = os.path.relpath(path_pdf, pasta_entrada)
        path_saida = os.path.join(pasta_saida, rel)

        sucesso, n_termos, avisos = processar_pdf(path_pdf, path_saida)

        if sucesso:
            n_modificados += 1
            n_termos_total += n_termos

        for aviso in avisos:
            linhas_log.append(f"[{rel}] {aviso}")

    path_log = os.path.join(pasta_saida, "error_logs.txt")
    with open(path_log, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas_log) if linhas_log else "Nenhum erro ou exceção registrado.")

    return {
        "n_documentos_encontrados": len(pdfs),
        "n_modificados": n_modificados,
        "n_termos_total": n_termos_total,
        "n_erros": len(linhas_log),
        "pasta_saida": pasta_saida,
    }


# ==============================
# WORKER (thread de processamento)
# ==============================

class WorkerThread(QThread):
    concluido = Signal(dict)
    erro = Signal(str)

    def __init__(self, pasta_entrada):
        super().__init__()
        self.pasta_entrada = pasta_entrada

    def run(self):
        try:
            resultado = processar_pasta(self.pasta_entrada)
            self.concluido.emit(resultado)
        except Exception as e:
            self.erro.emit(str(e))


# ==============================
# INTERFACE GRÁFICA (PySide6)
# ==============================

BUTTON_STYLE = """
QPushButton {
    background-color: #f9b02e;
    color: black;
    border: none;
    border-radius: 8px;
    padding: 10px;
    font-weight: bold;
    font-size: 14px;
}
QPushButton:hover { background-color: #ffd166; }
QPushButton:pressed { background-color: #e69500; }
QPushButton:disabled { background-color: #666666; color: #aaaaaa; }
"""

REMOVE_STYLE = """
QPushButton {
    background-color: #444444;
    color: #cccccc;
    border: none;
    border-radius: 8px;
    padding: 10px;
    font-size: 12px;
}
QPushButton:hover { background-color: #666666; }
"""


class ToggleSwitch(QCheckBox):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setFixedHeight(28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track_rect = QRectF(0, 4, 44, 20)
        painter.setBrush(QColor("#f9b02e") if self.isChecked() else QColor("#3a3a3a"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(track_rect, 10, 10)
        knob_x = 24 if self.isChecked() else 2
        painter.setBrush(QColor("white"))
        painter.drawEllipse(QRectF(knob_x, 6, 16, 16))
        painter.setPen(QColor("#dddddd"))
        painter.drawText(54, 19, self.text())


class PathLabel(QLabel):
    """QLabel que mostra o caminho selecionado sempre em uma única linha,
    truncando com reticências (...) no meio quando não couber na largura
    disponível, em vez de quebrar o texto em várias linhas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._caminho_completo = "-"
        self.setWordWrap(False)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def setCaminho(self, caminho):
        self._caminho_completo = caminho if caminho else "-"
        self.setToolTip(self._caminho_completo)
        self._atualizar_texto_elidido()

    def _atualizar_texto_elidido(self):
        metrics = QFontMetrics(self.font())
        largura_disponivel = max(self.width() - 16, 20)  # margem interna aproximada
        texto_elidido = metrics.elidedText(
            self._caminho_completo, Qt.TextElideMode.ElideMiddle, largura_disponivel
        )
        super().setText(texto_elidido)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._atualizar_texto_elidido()


def criar_janela():
    app = QApplication(sys.argv)

    window = QWidget()
    window.setWindowTitle("FaceList")
    window.setWindowIcon(QIcon(resource_path("facelist.ico")))
    window.resize(853, 480)
    window.setMinimumSize(853, 480)
    window.setStyleSheet("""
        QWidget { background-color: #1e1e1e; font-family: "Segoe UI"; color: white; }
        QScrollArea { border: none; background: transparent; }
        QScrollBar:vertical { background: #2b2b2b; width: 10px; border-radius: 5px; }
        QScrollBar::handle:vertical { background: #f9b02e; border-radius: 5px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        QCheckBox { color: #dddddd; font-size: 12px; spacing: 12px; background: transparent; }
        QCheckBox::indicator { width: 42px; height: 22px; border-radius: 11px;
                               background-color: #3a3a3a; border: 1px solid #555555; }
        QCheckBox::indicator:checked { background-color: #f9b02e; border: 1px solid #f9b02e; }
    """)

    # Fundo bg_hbr.png (mesmo recurso do BolsistaDB/SmartPC)
    bg_label = QLabel(window)
    bg_pixmap = QPixmap(resource_path("bg_hbr.png"))
    bg_label.setPixmap(bg_pixmap)
    bg_label.setScaledContents(True)

    scroll = QScrollArea(window)
    scroll.setWidgetResizable(True)
    scroll.setStyleSheet("background: transparent;")

    container = QWidget()
    container.setStyleSheet("background: transparent;")
    scroll.setWidget(container)

    card = QFrame()
    card.setObjectName("card")
    card.setStyleSheet("""
        #card {
            background-color: rgba(30,30,30,220);
            border-radius: 18px;
            border: 1px solid rgba(255,255,255,25);
        }
    """)

    # Estado
    state = {
        "pasta_entrada": None,
        "worker": None,
    }

    label_pasta = PathLabel()
    label_pasta.setCaminho("-")
    label_pasta.setStyleSheet("""
        background-color: #2b2b2b;
        border: 1px solid #3a3a3a;
        border-radius: 8px;
        padding: 8px;
        color: #cccccc;
        font-size: 11px;
    """)
    label_pasta.setFixedHeight(28)

    btn_executar = QPushButton("Executar")
    btn_executar.setFixedSize(150, 44)
    btn_executar.setStyleSheet(BUTTON_STYLE)
    btn_executar.setEnabled(False)

    status_label = QLabel("")
    status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    status_label.setStyleSheet("color: #aaaaaa; font-size: 11px; background: transparent;")

    def atualizar_status():
        btn_executar.setEnabled(bool(state["pasta_entrada"]))

    def criar_bloco(titulo, callback_sel, callback_rem, label_widget):
        bloco = QFrame()
        bloco.setStyleSheet("""
            QFrame { background-color: rgba(255,255,255,8); border-radius: 12px; }
        """)
        layout = QVBoxLayout(bloco)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        titulo_lbl = QLabel(titulo)
        titulo_lbl.setStyleSheet("color: white; font-size: 13px; font-weight: bold; background: transparent;")
        layout.addWidget(titulo_lbl)

        btn_row = QHBoxLayout()
        btn_sel = QPushButton("Selecionar")
        btn_sel.setFixedSize(110, 32)
        btn_sel.setStyleSheet(BUTTON_STYLE)
        btn_sel.clicked.connect(callback_sel)

        btn_rem = QPushButton("Remover")
        btn_rem.setFixedSize(110, 32)
        btn_rem.setStyleSheet(REMOVE_STYLE)
        btn_rem.clicked.connect(callback_rem)

        btn_row.addWidget(btn_sel)
        btn_row.addWidget(btn_rem)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        layout.addWidget(label_widget)
        return bloco

    def sel_pasta():
        d = QFileDialog.getExistingDirectory(window, "Selecionar pasta de entrada (docs_folder)")
        if d:
            state["pasta_entrada"] = d
            label_pasta.setCaminho(d)
        atualizar_status()

    def rem_pasta():
        state["pasta_entrada"] = None
        label_pasta.setCaminho("-")
        atualizar_status()

    def ao_concluir(resultado):
        btn_executar.setEnabled(True)
        status_label.setText("")

        msg = (
            f"Documentos encontrados: {resultado['n_documentos_encontrados']}\n"
            f"Documentos modificados: {resultado['n_modificados']}\n"
            f"Termos de nomenclatura listados (total): {resultado['n_termos_total']}\n"
            f"Erros/exceções encontrados: {resultado['n_erros']}\n\n"
            f"Saída salva em:\n{resultado['pasta_saida']}"
        )
        QMessageBox.information(window, "Processamento concluído", msg)

        if abrir_checkbox.isChecked():
            try:
                os.startfile(resultado["pasta_saida"])
            except Exception:
                try:
                    subprocess.call(["open", resultado["pasta_saida"]])
                except Exception:
                    pass

    def ao_erro(msg):
        btn_executar.setEnabled(True)
        status_label.setText("")
        QMessageBox.critical(window, "Erro no processamento", msg)

    def executar():
        if not state["pasta_entrada"]:
            return
        btn_executar.setEnabled(False)
        status_label.setText("Processando…")
        worker = WorkerThread(state["pasta_entrada"])
        worker.concluido.connect(ao_concluir)
        worker.erro.connect(ao_erro)
        state["worker"] = worker
        worker.start()

    btn_executar.clicked.connect(executar)

    # --- Layout do card ---
    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(28, 28, 28, 28)
    card_layout.setSpacing(14)

    titulo = QLabel("FaceList")
    titulo.setAlignment(Qt.AlignmentFlag.AlignCenter)
    titulo.setStyleSheet("""
        font-family: "Bahnschrift Condensed";
        font-size: 38px;
        font-weight: bold;
        color: white;
        padding-bottom: 2px;
        background: transparent;
    """)
    card_layout.addWidget(titulo)

    card_layout.addWidget(criar_bloco("Escolher pasta de entrada", sel_pasta, rem_pasta, label_pasta))

    abrir_checkbox = ToggleSwitch("Abrir pasta de saída quando concluído")
    card_layout.addWidget(abrir_checkbox)

    exec_row = QHBoxLayout()
    exec_row.addStretch()
    exec_row.addWidget(btn_executar)
    exec_row.addStretch()
    card_layout.addLayout(exec_row)
    card_layout.addWidget(status_label)

    # --- Layout principal ---
    main_layout = QVBoxLayout(container)
    main_layout.addStretch()
    main_layout.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
    main_layout.addStretch()
    main_layout.setContentsMargins(30, 30, 30, 30)

    github = QLabel()
    github.setAlignment(Qt.AlignmentFlag.AlignCenter)
    github.setText(
        '<a href = "https://github.com/imbaTIMvel/facelist">'
        'FaceList v0.1.0 - GitHub'
        '</a>'
    )
    github.setOpenExternalLinks(True)
    github.setStyleSheet("""
        QLabel {
            background-color: transparent;
            color: rgba(255,255,255,120);
            font-size: 11px;
        }
        QLabel:hover {
            color: #f9b02e;
        }
        """)
    main_layout.addWidget(github)

    footer = QLabel("Desenvolvido por: Diretoria Administrativa Financeira — DAF")
    footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
    footer.setStyleSheet("""
        QLabel {
            background-color: transparent;
            color: rgba(255,255,255,120);
            font-size: 10px;
            padding-bottom: 4px;
        }
    """)
    main_layout.addWidget(footer)

    window_layout = QVBoxLayout(window)
    window_layout.setContentsMargins(0, 0, 0, 0)
    window_layout.addWidget(scroll)

    def resize_event(event):
        bg_label.resize(window.size())
        scroll.resize(window.size())
        card.setMaximumWidth(560)

    window.resizeEvent = resize_event

    window.show()
    sys.exit(app.exec())


# ==============================
# ENTRY POINT
# ==============================

if __name__ == "__main__":
    criar_janela()