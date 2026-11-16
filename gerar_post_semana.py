r"""
Gera as imagens do post "Achados da semana" (formato Reels/TikTok/Stories, 1080x1920)
com os melhores achados que o robô está encontrando agora.

Uso:  python gerar_post_semana.py            (escolhe 5 achados até R$ 60)
      python gerar_post_semana.py 6 50       (6 achados até R$ 50)
As imagens e a legenda ficam na pasta Divulgacao\semana-AAAA-MM-DD.
"""
import io
import logging
import sys
import textwrap
from datetime import date
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

import robo

PASTA = Path(__file__).resolve().parent
W, H = 1080, 1920
FUNDO, TEXTO, DESTAQUE, CINZA, BRANCO = "#FFF6EC", "#1F1F1F", "#E8542C", "#7A7A7A", "#FFFFFF"
CANAL = "t.me/achadosdabonentti"
FONTES = Path("C:/Windows/Fonts")


def fonte(tam, negrito=False):
    return ImageFont.truetype(str(FONTES / ("segoeuib.ttf" if negrito else "segoeui.ttf")), tam)


def centro(d, y, texto, f, cor=TEXTO):
    largura = d.textlength(texto, font=f)
    d.text(((W - largura) / 2, y), texto, font=f, fill=cor)


def capa(qtd, preco_max, desconto_max):
    img = Image.new("RGB", (W, H), FUNDO)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((90, 520, W - 90, 1400), 60, fill=BRANCO)
    centro(d, 600, "ACHADOS", fonte(150, True), DESTAQUE)
    centro(d, 770, "DA SEMANA", fonte(110, True))
    centro(d, 960, f"{qtd} promoções até {robo.reais(preco_max).replace(',00', '')}", fonte(56))
    centro(d, 1050, f"com até {desconto_max}% OFF", fonte(56, True), DESTAQUE)
    centro(d, 1220, "arrasta pro lado →", fonte(44), CINZA)
    centro(d, 1640, "Eu amo economizar 💸".replace(" 💸", ""), fonte(48, True))
    centro(d, 1710, f"links no Telegram: {CANAL}", fonte(40), CINZA)
    return img


def slide(o, n, total):
    img = Image.new("RGB", (W, H), FUNDO)
    d = ImageDraw.Draw(img)
    centro(d, 70, f"{n}/{total}", fonte(40), CINZA)
    # foto do produto
    d.rounded_rectangle((70, 150, W - 70, 1170), 50, fill=BRANCO)
    try:
        foto = Image.open(io.BytesIO(requests.get(o.imagem, headers={"User-Agent": robo.NAVEGADOR}, timeout=30).content))
        foto = ImageOps.contain(foto.convert("RGB"), (W - 200, 960))
        img.paste(foto, ((W - foto.width) // 2, 180 + (960 - foto.height) // 2))
    except Exception:
        centro(d, 620, "(sem foto)", fonte(50), CINZA)
    # selo de desconto
    d.ellipse((W - 330, 100, W - 70, 360), fill=DESTAQUE)
    txt = f"-{o.desconto}%"
    f = fonte(84, True)
    d.text((W - 200 - d.textlength(txt, font=f) / 2, 175), txt, font=f, fill=BRANCO)
    # nome
    y = 1220
    for linha in textwrap.wrap(o.nome, 30)[:3]:
        centro(d, y, linha, fonte(54, True))
        y += 70
    # precos
    y += 30
    de = f"De {robo.reais(o.preco_antigo)}" if o.preco_antigo else ""
    if de:
        f = fonte(52)
        x = (W - d.textlength(de, font=f)) / 2
        d.text((x, y), de, font=f, fill=CINZA)
        d.line((x, y + 38, x + d.textlength(de, font=f), y + 38), fill=CINZA, width=4)
        y += 80
    centro(d, y, f"Por {robo.reais(o.preco)}", fonte(110, True), DESTAQUE)
    loja = robo.NOMES_LOJAS.get(o.loja, o.loja.title())
    oficial = next((e.split(": ", 1)[1] for e in o.extras if e.startswith("✅ Loja oficial")), None)
    if oficial:
        loja = f"{loja} · loja oficial {oficial}"[:42]
    centro(d, y + 160, loja, fonte(44), CINZA)
    centro(d, 1800, f"link no canal: {CANAL}", fonte(40), CINZA)
    return img


def final():
    img = Image.new("RGB", (W, H), DESTAQUE)
    d = ImageDraw.Draw(img)
    centro(d, 620, "Quer os links?", fonte(110, True), BRANCO)
    centro(d, 800, "Entra no meu canal", fonte(70), BRANCO)
    centro(d, 890, "do Telegram 👇".replace(" 👇", ":"), fonte(70), BRANCO)
    d.rounded_rectangle((110, 1040, W - 110, 1200), 80, fill=BRANCO)
    centro(d, 1072, CANAL, fonte(64, True), DESTAQUE)
    centro(d, 1350, "Eu amo economizar e posto", fonte(52), BRANCO)
    centro(d, 1420, "as melhores promoções todo dia.", fonte(52), BRANCO)
    centro(d, 1780, "#publi  links de afiliado", fonte(38), BRANCO)
    return img


def main():
    qtd = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    preco_max = float(sys.argv[2]) if len(sys.argv) > 2 else 100
    robo.log.addHandler(logging.NullHandler())
    cfg = robo.carregar_config()
    bloq = [p.lower() for p in cfg.get("filtros", {}).get("palavras_bloqueadas", [])]
    ofertas = robo.buscar_shopee_feed(cfg) + robo.buscar_renner_promocoes(cfg)
    ofertas = [o for o in ofertas if o.imagem and o.preco <= preco_max and 30 <= o.desconto <= 80
               and not any(p in o.nome.lower() for p in bloq)]
    ofertas.sort(key=lambda o: (-o.ordem, o.preco))  # populares e bem avaliados primeiro
    # Variedade: alterna as categorias (beleza, roupas, calçados, Renner...), 1 por vez,
    # sempre pegando o melhor de cada uma, sem repetir produto parecido.
    filas = {}
    for o in ofertas:
        filas.setdefault(o.categoria or o.loja, []).append(o)
    escolhidas, inicios = [], set()
    while len(escolhidas) < qtd and any(filas.values()):
        for fila in filas.values():
            while fila:
                o = fila.pop(0)
                inicio = " ".join(o.nome.lower().split()[:2])
                if inicio not in inicios:
                    escolhidas.append(o)
                    inicios.add(inicio)
                    break
            if len(escolhidas) == qtd:
                break
    if not escolhidas:
        print("Nenhum achado encontrado agora.")
        return

    pasta = PASTA / "Divulgacao" / f"semana-{date.today():%Y-%m-%d}"
    pasta.mkdir(parents=True, exist_ok=True)
    for antigo in pasta.glob("*.jpg"):  # se gerar de novo no mesmo dia, substitui
        antigo.unlink()
    maior = max(o.desconto for o in escolhidas)
    capa(len(escolhidas), preco_max, maior).save(pasta / "01-capa.jpg", quality=92)
    for i, o in enumerate(escolhidas, 1):
        slide(o, i, len(escolhidas)).save(pasta / f"{i + 1:02d}-achado.jpg", quality=92)
    final().save(pasta / f"{len(escolhidas) + 2:02d}-final.jpg", quality=92)

    linhas = [f"💸 Achados da semana: {len(escolhidas)} promoções até {robo.reais(preco_max).replace(',00', '')}!", ""]
    for i, o in enumerate(escolhidas, 1):
        loja = robo.NOMES_LOJAS.get(o.loja, o.loja)
        linhas.append(f"{i}. {o.nome[:60]} - {robo.reais(o.preco)} (-{o.desconto}%) na {loja}")
    linhas += ["", f"Os links estão no meu canal do Telegram: {CANAL} (link na bio)",
               "Eu amo economizar e posto as melhores promoções todo dia. 💸", "",
               "#publi #achadinhos #promoção #economia #achadosdasemana #shopee #renner"]
    (pasta / "legenda.txt").write_text("\n".join(linhas), encoding="utf-8")
    print(f"Pronto! {len(escolhidas) + 2} imagens e a legenda em: {pasta}")
    for o in escolhidas:
        print(f" - {o.nome[:60]} | {robo.reais(o.preco)} | -{o.desconto}%")


if __name__ == "__main__":
    main()
