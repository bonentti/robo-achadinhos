"""Gera o "cartão" de uma oferta: foto do produto + desconto + preço + loja (1080x1080, bom para WhatsApp)."""
import io
import textwrap
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

W = H = 1080
FUNDO, TEXTO, DESTAQUE, CINZA, BRANCO = "#FFF6EC", "#1F1F1F", "#E8542C", "#7A7A7A", "#FFFFFF"
NAVEGADOR = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"

# Fontes: Windows (computador) ou DejaVu (servidor Linux do GitHub)
_FONTES = {
    False: ["C:/Windows/Fonts/segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    True: ["C:/Windows/Fonts/segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}


def fonte(tam: int, negrito: bool = False):
    for caminho in _FONTES[negrito]:
        if Path(caminho).exists():
            return ImageFont.truetype(caminho, tam)
    return ImageFont.load_default(size=tam)


def _centro(d, y, texto, f, cor=TEXTO):
    d.text(((W - d.textlength(texto, font=f)) / 2, y), texto, font=f, fill=cor)


def reais(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def gerar_cartao(nome: str, preco: float, preco_antigo: float | None, loja: str,
                 imagem_url: str | None, cupom: str | None = None, rodape: str = "") -> bytes:
    img = Image.new("RGB", (W, H), FUNDO)
    d = ImageDraw.Draw(img)

    # Foto do produto
    d.rounded_rectangle((40, 40, W - 40, 640), 40, fill=BRANCO)
    if imagem_url:
        try:
            foto = Image.open(io.BytesIO(requests.get(imagem_url, headers={"User-Agent": NAVEGADOR}, timeout=30).content))
            foto = ImageOps.contain(foto.convert("RGB"), (W - 140, 560))
            img.paste(foto, ((W - foto.width) // 2, 60 + (560 - foto.height) // 2))
        except Exception:
            _centro(d, 320, "(sem foto)", fonte(44), CINZA)

    # Selo de desconto
    desconto = round((1 - preco / preco_antigo) * 100) if preco_antigo and preco_antigo > preco else 0
    if desconto:
        d.ellipse((W - 270, 10, W - 20, 260), fill=DESTAQUE)
        txt, f = f"-{desconto}%", fonte(78, True)
        d.text((W - 145 - d.textlength(txt, font=f) / 2, 90), txt, font=f, fill=BRANCO)

    # Nome
    y = 665
    for linha in textwrap.wrap(nome, 34)[:2]:
        _centro(d, y, linha, fonte(44, True))
        y += 56

    # Preços
    y = max(y + 10, 790)
    if desconto:
        de, f = f"De {reais(preco_antigo)}", fonte(42)
        x = (W - d.textlength(de, font=f)) / 2
        d.text((x, y), de, font=f, fill=CINZA)
        d.line((x, y + 28, x + d.textlength(de, font=f), y + 28), fill=CINZA, width=3)
        y += 55
    _centro(d, y, f"Por {reais(preco)}", fonte(88, True), DESTAQUE)
    y += 110

    # Loja / cupom
    linha_loja = f"na {loja}" + (f"  •  cupom {cupom}" if cupom else "")
    _centro(d, y, linha_loja, fonte(40, bool(cupom)), TEXTO if cupom else CINZA)
    if rodape:
        _centro(d, H - 55, rodape, fonte(30), CINZA)

    saida = io.BytesIO()
    img.save(saida, "JPEG", quality=90)
    return saida.getvalue()
