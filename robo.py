"""
Robo de Achadinhos
==================
Procura ofertas (Shopee, Renner/Awin, Mercado Livre, Shein), monta a mensagem
com o SEU link de afiliado e publica no seu canal do Telegram.

Comandos (rode no terminal, dentro da pasta do robo):
  python robo.py                  -> roda para sempre, verificando de tempos em tempos
  python robo.py teste            -> faz UMA verificacao e so MOSTRA as mensagens (nao posta)
  python robo.py uma-vez          -> faz UMA verificacao e posta de verdade
  python robo.py testar-telegram  -> manda uma mensagem de teste no canal
  python robo.py link-renner URL  -> gera o seu link de afiliado (Awin) para um produto da Renner
  python robo.py postar --loja shein --nome "Vestido" --preco 59,90 --de 99,90 --link URL [--imagem URL]
                                  -> posta uma oferta que voce achou manualmente
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import io
import json
import logging
import math
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

PASTA = Path(__file__).resolve().parent
ARQ_CONFIG = PASTA / "config.toml"
ARQ_ESTADO = PASTA / "estado.json"
ARQ_LOG = PASTA / "robo.log"

NAVEGADOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

NOMES_LOJAS = {
    "shopee": "Shopee",
    "renner": "Renner",
    "mercadolivre": "Mercado Livre",
    "shein": "Shein",
    "hardyn": "Hardyn",
    "kaisan": "Kaisan",
    "amazon": "Amazon",
}

log = logging.getLogger("robo")


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def configurar_log() -> None:
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%d/%m %H:%M:%S")
    log.setLevel(logging.INFO)
    tela = logging.StreamHandler(sys.stdout)
    tela.setFormatter(fmt)
    arquivo = logging.FileHandler(ARQ_LOG, encoding="utf-8")
    arquivo.setFormatter(fmt)
    log.addHandler(tela)
    log.addHandler(arquivo)


def preenchido(valor) -> bool:
    """False para campos vazios ou que ainda estao com o texto de exemplo."""
    return bool(valor) and "COLOQUE" not in str(valor).upper()


def parse_preco(valor) -> float | None:
    """Aceita 59.9, "59,90", "1.234,56", "R$ 1.234,56", "1234.56"."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = re.sub(r"[^\d,.]", "", str(valor))
    if not texto:
        return None
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):  # 1.234,56
            texto = texto.replace(".", "").replace(",", ".")
        else:  # 1,234.56
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", ".")
    elif texto.count(".") > 1 or (
        isinstance(valor, str) and re.fullmatch(r"\d{1,3}\.\d{3}", texto)
    ):  # "1.234.567" ou "2.499" (milhar no formato brasileiro)
        texto = texto.replace(".", "")
    try:
        return float(texto)
    except ValueError:
        return None


def reais(valor: float) -> str:
    s = f"{valor:,.2f}"  # 1,234.56
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------------- #
# Oferta e mensagem
# --------------------------------------------------------------------------- #

@dataclass
class Oferta:
    loja: str
    nome: str
    preco: float
    link: str
    preco_antigo: float | None = None
    imagem: str | None = None
    chave: str = ""  # identificador unico para nao repetir posts
    extras: list[str] = field(default_factory=list)
    chamada: str | None = None  # frase de impacto no topo (ex.: "O PREÇO CAIU!")
    cupom: str | None = None
    prioridade: float | None = None  # quanto maior, antes posta (padrao: o desconto)
    categoria: str = ""
    favorita: str | None = None  # marca favorita (maquiagem/beleza)

    @property
    def grupo(self) -> str:
        """Vez no revezamento: cada loja, as marcas favoritas como um grupo proprio,
        e a Shopee dividida por categoria (para o canal nao ficar so com beleza)."""
        if self.favorita:
            return "favoritas"
        if self.loja == "shopee" and self.categoria:
            return f"shopee-{self.categoria}"
        return self.loja

    @property
    def desconto(self) -> int:
        if self.preco_antigo and self.preco_antigo > self.preco:
            return round((1 - self.preco / self.preco_antigo) * 100)
        return 0

    @property
    def ordem(self) -> float:
        return self.prioridade if self.prioridade is not None else self.desconto


def montar_mensagem(oferta: Oferta, cfg: dict) -> str:
    msg_cfg = cfg.get("mensagem", {})
    e = html.escape
    nome = oferta.nome.strip()
    if len(nome) > 120:
        nome = nome[:117] + "..."
    # Chamada no topo: os canais grandes abrem quase todo post com uma frase de impacto.
    chamada = oferta.chamada
    if not chamada and msg_cfg.get("usar_chamadas", True):
        if oferta.desconto >= 1:
            chamada = f"🔥 {oferta.desconto}% OFF"
    linhas = []
    if chamada:
        linhas += [f"<b>{e(chamada)}</b>", ""]
    linhas += [f"🛍️ {e(nome)}", ""]
    if oferta.desconto >= 1:
        linhas.append(f"De <s>{reais(oferta.preco_antigo)}</s>")
        linhas.append(f"💰 <b>Por {reais(oferta.preco)}</b>  (-{oferta.desconto}%)")
    else:
        linhas.append(f"💰 <b>{reais(oferta.preco)}</b>")
    if oferta.cupom:
        linhas.append(f"🎟️ Cupom: <code>{e(oferta.cupom)}</code>")
    for extra in oferta.extras:
        linhas.append(e(extra))
    linhas.append(f"🏬 {NOMES_LOJAS.get(oferta.loja, oferta.loja.title())}")
    linhas.append("")
    linhas.append(f'🛒 Comprar aqui: {e(oferta.link)}')  # link visivel: vai junto ao copiar para o WhatsApp
    rodape = msg_cfg.get("rodape", "")
    if rodape:
        linhas += ["", e(rodape)]
    return "\n".join(linhas)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

class Telegram:
    def __init__(self, cfg: dict, modo_teste: bool):
        tg = cfg.get("telegram", {})
        self.token = tg.get("token_do_bot", "")
        self.canal = str(tg.get("canal", ""))
        self.modo_teste = modo_teste
        if not modo_teste and not (preenchido(self.token) and preenchido(self.canal)):
            raise SystemExit(
                "Falta configurar o Telegram (token_do_bot e canal) no config.toml. "
                "Veja o LEIA-ME."
            )

    def _api(self, metodo: str, dados: dict, arquivos: dict | None = None) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{metodo}"
        r = requests.post(url, data=dados, files=arquivos, timeout=60)
        resposta = r.json()
        if not resposta.get("ok"):
            raise RuntimeError(resposta.get("description", r.text))
        return resposta

    def enviar_privado(self, chat_id: str, texto: str) -> None:
        """Mensagem de texto puro no privado (para copiar e colar no WhatsApp)."""
        for i in range(0, len(texto), 4000):  # limite do Telegram por mensagem
            self._api("sendMessage", {"chat_id": chat_id, "text": texto[i:i + 4000],
                                      "disable_web_page_preview": "true"})

    def enviar_foto_privado(self, chat_id: str, foto: bytes, legenda: str) -> None:
        """Foto + legenda em texto puro (os * e ~ ficam para o WhatsApp formatar)."""
        self._api("sendPhoto", {"chat_id": chat_id, "caption": legenda[:1024]},
                  {"photo": ("achado.jpg", foto)})

    def enviar(self, texto: str, imagem: str | None = None) -> None:
        if self.modo_teste:
            print("\n" + "=" * 60)
            if imagem:
                print(f"[foto: {imagem}]")
            print(texto)
            print("=" * 60)
            return
        if imagem and len(texto) <= 1024:
            dados = {"chat_id": self.canal, "caption": texto, "parse_mode": "HTML"}
            try:
                # Algumas lojas (ex.: Renner) nao deixam o Telegram baixar a foto,
                # entao o robo baixa a foto e envia o arquivo.
                foto = requests.get(imagem, headers={"User-Agent": NAVEGADOR}, timeout=30)
                foto.raise_for_status()
                self._api("sendPhoto", dados, {"photo": ("foto.jpg", foto.content)})
                return
            except Exception as erro:
                try:
                    self._api("sendPhoto", {**dados, "photo": imagem})
                    return
                except Exception:
                    log.info(f"  (nao consegui enviar a foto: {erro}; enviando so texto)")
        self._api("sendMessage", {
            "chat_id": self.canal, "text": texto, "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        })


# --------------------------------------------------------------------------- #
# Fonte 1: Shopee (API oficial de afiliados)
# --------------------------------------------------------------------------- #

SHOPEE_URL = "https://open-api.affiliate.shopee.com.br/graphql"


def shopee_consulta(app_id: str, segredo: str, consulta: str) -> dict:
    corpo = json.dumps({"query": consulta}, separators=(",", ":"))
    ts = str(int(time.time()))
    assinatura = hashlib.sha256((app_id + ts + corpo + segredo).encode()).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={app_id}, Timestamp={ts}, Signature={assinatura}",
    }
    r = requests.post(SHOPEE_URL, data=corpo.encode(), headers=headers, timeout=30)
    dados = r.json()
    if dados.get("errors"):
        raise RuntimeError(dados["errors"][0].get("message", str(dados["errors"])))
    return dados["data"]


def buscar_shopee(cfg: dict) -> list[Oferta]:
    sp = cfg.get("shopee", {})
    if not sp.get("ativo", False):
        return []
    app_id, segredo = str(sp.get("app_id", "")), str(sp.get("segredo", ""))
    if not (preenchido(app_id) and preenchido(segredo)):
        log.info("Shopee: falta app_id/segredo no config.toml — pulando.")
        return []

    desconto_min = sp.get("desconto_minimo", 30)
    nota_min = sp.get("nota_minima", 4.5)
    vendas_min = sp.get("vendas_minimas", 50)
    preco_max = sp.get("preco_maximo", 0)
    ofertas: list[Oferta] = []

    for palavra in sp.get("palavras_chave", []):
        consulta = (
            "{ productOfferV2(keyword: %s, sortType: 2, page: 1, limit: %d) { nodes {"
            " itemId productName priceMin priceDiscountRate imageUrl offerLink"
            " sales ratingStar shopName } } }"
        ) % (json.dumps(palavra, ensure_ascii=False), int(sp.get("resultados_por_busca", 30)))
        try:
            nos = shopee_consulta(app_id, segredo, consulta)["productOfferV2"]["nodes"]
        except Exception as erro:
            log.info(f"Shopee: erro ao buscar '{palavra}': {erro}")
            continue

        aprovados = 0
        for p in nos:
            preco = parse_preco(p.get("priceMin"))
            desconto = int(p.get("priceDiscountRate") or 0)
            nota = float(p.get("ratingStar") or 0)
            vendas = int(p.get("sales") or 0)
            if not preco or desconto < desconto_min or nota < nota_min or vendas < vendas_min:
                continue
            if preco_max and preco > preco_max:
                continue
            antigo = round(preco / (1 - desconto / 100), 2) if 0 < desconto < 100 else None
            extras = [f"⭐ {nota:.1f}  |  {vendas:,} vendidos".replace(",", ".")]
            ofertas.append(Oferta(
                loja="shopee", nome=p["productName"], preco=preco, preco_antigo=antigo,
                link=p["offerLink"], imagem=p.get("imageUrl"),
                chave=f"shopee:{p['itemId']}", extras=extras,
            ))
            aprovados += 1
        log.info(f"Shopee '{palavra}': {len(nos)} produtos, {aprovados} passaram nos filtros")
    return ofertas


ARQ_FEED_SHOPEE = PASTA / "shopee_feed.csv"


def produto_favorito(cfg: dict, titulo: str, loja: str) -> str | None:
    """Produto especifico que a Nicole ama (ex.: oleo de banho Nivea na loja oficial)."""
    t, lj = titulo.lower(), loja.lower()
    for item in cfg.get("produtos_favoritos", {}).get("itens", []):
        termos = [x.lower() for x in item.get("termos", [])]
        if termos and all(x in t for x in termos) and item.get("loja", "").lower() in lj:
            return item.get("nome") or " ".join(item.get("termos", []))
    return None


def marca_favorita(cfg: dict, texto: str) -> str | None:
    """Devolve a marca favorita citada no texto (nome do produto/loja), se houver."""
    t = " " + re.sub(r"[^a-z0-9à-ú]+", " ", texto.lower()) + " "
    for marca in cfg.get("marcas_favoritas", {}).get("marcas", []):
        m = " " + re.sub(r"[^a-z0-9à-ú]+", " ", marca.lower()).strip() + " "
        if m.strip() and m in t:
            return marca
    return None


_MARCAS_CACHE: dict = {}


def marca_conhecida(cfg: dict, titulo: str, loja: str) -> str | None:
    """Marca reconhecida no nome da loja ou do produto (ignora 'compativel com X', 'tipo X')."""
    chave = id(cfg)
    if chave not in _MARCAS_CACHE:
        mc = cfg.get("marcas_conhecidas", {})
        nomes = {m for lista in mc.values() if isinstance(lista, list) for m in lista}
        nomes |= set(cfg.get("marcas_favoritas", {}).get("marcas", []))
        norm = lambda s: re.sub(r"[^a-z0-9à-ú+!]+", " ", s.lower()).strip()
        _MARCAS_CACHE[chave] = sorted(((m, norm(m)) for m in nomes if norm(m)), key=lambda x: -len(x[1]))
    norm = lambda s: " " + re.sub(r"[^a-z0-9à-ú+!]+", " ", s.lower()) + " "
    lj, tt = norm(loja), norm(titulo)
    for marca, m in _MARCAS_CACHE[chave]:
        if f" {m} " in lj:
            return marca
    for marca, m in _MARCAS_CACHE[chave]:
        if f" {m} " in tt and not re.search(
                rf"(compat[ií]vel com|para|tipo|estilo|similar a?|inspirad[oa] (?:em|no|na)|r[eé]plica)\s+(?:o |a )?{re.escape(m)}\b",
                tt):
            return marca
    return None


def loja_confiavel(cfg: dict, titulo: str, loja: str) -> bool:
    """Produto de marca so entra se vendido pela loja da propria marca ou por uma grande
    varejista conhecida ([shopee] lojas_multimarcas). Revendedora qualquer fica de fora."""
    sp = cfg.get("shopee", {})
    if not sp.get("exigir_loja_da_marca", False):
        return True
    if marca_conhecida(cfg, "", loja) or marca_favorita(cfg, loja):
        return True  # a marca esta no nome da loja (ex.: "Moleca Oficial")
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return any(norm(v) in norm(loja) for v in sp.get("lojas_multimarcas", []))


def link_shopee_afiliado(link: str, id_afiliado: str, sub_id: str = "telegram") -> str:
    """Os links do feed nao levam o ID de afiliada; sem ele nao ha comissao."""
    if "affiliate_id=" in link:
        return link
    if "an_redir" not in link:
        link = "https://shope.ee/an_redir?origin_link=" + quote(link, safe="")
    return f"{link}&affiliate_id={id_afiliado}&sub_id={sub_id}"


def link_amazon(cfg: dict, url: str) -> str | None:
    """Link limpo do produto com a SUA tag de associada (tira a tag de quem divulgou antes)."""
    tag = str(cfg.get("amazon", {}).get("tag", ""))
    asin = re.search(r"/(?:dp|gp/product|gp/aw/d|d)/([A-Z0-9]{10})", url)
    if not (preenchido(tag) and asin):
        return None
    return f"https://www.amazon.com.br/dp/{asin.group(1)}?tag={tag}"


def converter_link(cfg: dict, url: str, sub_id: str = "telegram") -> str | None:
    """Troca um link de qualquer divulgador pelo link de afiliada da Nicole.
    Devolve None quando a loja nao e parceira (o link de outra pessoa nunca e usado)."""
    url = html.unescape(url.strip())
    if not url.lower().startswith("http"):
        url = "https://" + url
    final = url
    if re.search(r"amzn\.to|a\.co/|link\.amazon|amzlinks|shope\.ee|s\.shopee|tidd\.ly|bit\.ly", url):
        try:  # link encurtado: descobre para onde ele leva
            r = requests.get(url, headers={"User-Agent": NAVEGADOR}, timeout=20, allow_redirects=True)
            final = r.url
            if "origin_link=" in final:
                final = unquote((parse_qs(urlparse(final).query).get("origin_link") or [final])[0])
        except Exception as erro:
            log.info(f"  nao consegui abrir o link encurtado {url}: {erro}")
            return None
    host = urlparse(final).netloc.lower()
    if "amazon." in host:
        return link_amazon(cfg, final)
    if "shopee." in host:
        if "origin_link=" in final:
            final = unquote((parse_qs(urlparse(final).query).get("origin_link") or [final])[0])
        limpo = final.split("?")[0]
        return link_shopee_afiliado(limpo, str(cfg.get("shopee", {}).get("id_afiliado", "")), sub_id)
    if "lojasrenner.com.br" in host:
        return link_awin(cfg, final.split("?")[0])
    return None


def buscar_shopee_feed(cfg: dict) -> list[Oferta]:
    """Feed de produtos do painel de afiliados (Criativo > Feed de produto), atualizado 1x por dia."""
    sp = cfg.get("shopee", {})
    urls = [u for u in sp.get("urls_do_feed", []) if preenchido(u)]
    id_af = str(sp.get("id_afiliado", ""))
    if not sp.get("usar_feed", False) or not urls or not preenchido(id_af):
        return []
    categorias = set(sp.get("categorias_feed", ["Women Clothes", "Women Shoes", "Women Bags", "Beauty"]))
    cafes = cfg.get("cafes", {})
    cat_cafe = cafes.get("categoria", "Food & Beverages") if cafes.get("ativo") else None
    if cat_cafe:
        categorias.add(cat_cafe)  # so entram os cafes das marcas vigiadas (filtro abaixo)
    # O feed de lojas oficiais e enorme (~200 MB): baixa 1x por dia e guarda so as
    # categorias do canal num arquivo pequeno.
    if not ARQ_FEED_SHOPEE.exists() or time.time() - ARQ_FEED_SHOPEE.stat().st_mtime > 24 * 3600:
        csv.field_size_limit(10**8)
        temp = PASTA / "shopee_feed_baixando.tmp"
        guardados, cabecalho = [], None
        for url in urls:
            try:
                with requests.get(url, timeout=900, stream=True) as r, open(temp, "wb") as f:
                    r.raise_for_status()
                    for bloco in r.iter_content(1 << 20):
                        f.write(bloco)
                with open(temp, "rb") as f:
                    gz = f.read(2) == b"\x1f\x8b"
                aberto = gzip.open(temp, "rt", encoding="utf-8-sig", errors="replace", newline="") if gz \
                    else open(temp, encoding="utf-8-sig", errors="replace", newline="")
                with aberto as f:
                    leitor = csv.DictReader(f)
                    cabecalho = cabecalho or [c for c in leitor.fieldnames if c not in ("description", "global_item_attributes")]
                    for linha in leitor:
                        if linha.get("global_category1") in categorias:
                            guardados.append({c: linha.get(c, "") for c in cabecalho})
            except Exception as erro:
                log.info(f"Shopee (feed): erro ao baixar o feed: {erro}")
        temp.unlink(missing_ok=True)
        if guardados:
            with open(ARQ_FEED_SHOPEE, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cabecalho)
                w.writeheader()
                w.writerows(guardados)
    if not ARQ_FEED_SHOPEE.exists():
        return []

    desconto_min = sp.get("desconto_minimo", 30)
    nota_min = sp.get("nota_minima", 4.7)
    nota_loja_min = sp.get("nota_minima_loja", 4.7)
    curtidas_min = sp.get("curtidas_minimas", 500)
    preco_max = sp.get("preco_maximo", 0)
    excluir = [p.lower() for p in sp.get("excluir_palavras", ["masculin", "infantil", "kids"])]
    lojas_bloqueadas = [p.lower() for p in sp.get("lojas_bloqueadas", [])]
    sub_bloqueadas = set(sp.get("subcategorias_bloqueadas", []))
    so_marcas = sp.get("so_marcas_conhecidas", False)
    d_marca = sp.get("desconto_minimo_marcas", desconto_min)
    c_marca = sp.get("curtidas_minimas_marcas", curtidas_min)
    preco_min = sp.get("preco_minimo", 0)
    ofertas, total = [], 0
    with open(ARQ_FEED_SHOPEE, encoding="utf-8", newline="") as f:
        for linha in csv.DictReader(f):
            total += 1
            if linha.get("global_category1") not in categorias:
                continue
            if linha.get("global_category2") in sub_bloqueadas:
                continue  # ex.: moveis, ferramentas, jardim, cuidados masculinos
            if linha.get("cb_option", "Non-Cross border") != "Non-Cross border":  # vendedor no Brasil
                continue
            if linha.get("condition", "New") != "New":
                continue
            titulo = linha.get("title", "")
            if any(p in titulo.lower() for p in excluir):
                continue
            preco = parse_preco(linha.get("sale_price"))
            antigo = parse_preco(linha.get("price"))
            try:
                desconto = int(float(linha.get("discount_percentage") or 0))
                nota = float(linha.get("item_rating") or 0)
                nota_loja = float(linha.get("shop_rating") or 5)
                curtidas = int(float(linha.get("like") or 0))
            except ValueError:
                continue
            # Marcas favoritas: entram com desconto menor e sem exigir curtidas, e vao para o topo da fila
            n_min = nota_min
            texto_busca = titulo + " " + (linha.get("shop_name") or "")
            produto_fav = produto_favorito(cfg, titulo, linha.get("shop_name") or "")
            if cat_cafe and linha.get("global_category1") == cat_cafe:
                # Cafes: so as marcas vigiadas e so produtos de cafe (evita, ex., "Sofa Orfeu")
                favorita = marca_favorita({"marcas_favoritas": {"marcas": cafes.get("marcas", [])}}, texto_busca)
                if not favorita or not re.search(r"caf[eé]|c[aá]psul|espresso|cappuccino", titulo, re.I):
                    continue
                d_min, c_min = cafes.get("desconto_minimo", 10), 0
            elif produto_fav:  # produto que a Nicole ama: prioridade maxima, regras mais leves
                favorita = produto_fav
                d_min, c_min = cfg.get("produtos_favoritos", {}).get("desconto_minimo", 20), 0
            else:
                favorita = marca_favorita(cfg, texto_busca)
                if not loja_confiavel(cfg, titulo, linha.get("shop_name") or ""):
                    continue  # marca vendida por revendedora (ex.: "Moleca" na loja DIGOS ONLINE)
                if favorita:
                    d_min, c_min = cfg.get("marcas_favoritas", {}).get("desconto_minimo", 15), 0
                else:
                    marca = marca_conhecida(cfg, titulo, linha.get("shop_name") or "")
                    if so_marcas and not marca:
                        continue  # so marcas que as pessoas reconhecem
                    d_min, c_min = (d_marca, c_marca) if marca else (desconto_min, curtidas_min)
                    if marca:
                        n_min = sp.get("nota_minima_marcas", nota_min)
            if preco_min and preco and preco < preco_min:
                continue
            if (not preco or desconto < d_min or nota < n_min or nota_loja < min(nota_loja_min, n_min)
                    or curtidas < c_min or (preco_max and preco > preco_max)):
                continue
            link = linha.get("product_short link") or linha.get("product_link")
            if not link:
                continue
            loja_nome = (linha.get("shop_name") or "").strip()
            if any(b in loja_nome.lower() for b in lojas_bloqueadas):
                continue
            extras = [f"⭐ {nota:.1f}" + (f"  |  ❤️ {curtidas:,} curtidas".replace(",", ".") if curtidas else "")]
            if loja_nome:
                extras.append(f"✅ Loja oficial: {loja_nome}")
            o = Oferta(
                loja="shopee", nome=titulo or "Oferta", preco=preco,
                preco_antigo=antigo if antigo and antigo > preco else None,
                link=link_shopee_afiliado(link, id_af), imagem=linha.get("image_link") or None,
                chave=f"shopee:{linha.get('itemid')}", extras=extras,
                categoria=linha.get("global_category1", ""),
            )
            # Prioriza produtos populares (curtidas) com bom desconto, e nao so o maior %
            o.prioridade = desconto + 10 * math.log10(max(curtidas, 1)) + (500 if produto_fav else 0)
            o.favorita = favorita
            ofertas.append(o)
    log.info(f"Shopee (lojas oficiais): {total} produtos do tema, {len(ofertas)} passaram nos filtros")
    return ofertas


# --------------------------------------------------------------------------- #
# Fonte 2: Renner pela Awin (link automatico + feed de produtos opcional)
# --------------------------------------------------------------------------- #

def link_awin(cfg: dict, url_produto: str) -> str | None:
    aw = cfg.get("awin", {})
    pub, loja = aw.get("id_publisher", ""), aw.get("id_anunciante_renner", "")
    if not (preenchido(pub) and preenchido(loja)):
        return None
    return (
        f"https://www.awin1.com/cread.php?awinmid={loja}&awinaffid={pub}"
        f"&ued={quote(url_produto, safe='')}"
    )


def link_rastreado(cfg: dict, link: str, canal: str, num: int | None = None) -> str:
    """Etiqueta o link com o canal e o numero do post, para os relatorios de afiliado
    mostrarem cliques/vendas de cada post: Shopee (sub_id), Awin (clickref) e Amazon (tag propria)."""
    etiqueta = f"{canal}-{num}" if num else canal
    if "affiliate_id=" in link:  # Shopee: sub_id = canal-numero (aparece como Sub_id1/Sub_id2 no relatorio)
        return re.sub(r"sub_id=[^&]*", f"sub_id={etiqueta}", link) if "sub_id=" in link else f"{link}&sub_id={etiqueta}"
    if "awin1.com/cread.php" in link:
        link = re.sub(r"&clickref=[^&]*", "", link)
        return link.replace("&ued=", f"&clickref={etiqueta}&ued=", 1)
    if "amazon.com.br" in link:
        tag = str(cfg.get("amazon", {}).get(f"tag_{canal}", ""))
        if preenchido(tag):
            return re.sub(r"tag=[^&]*", f"tag={tag}", link)
    return link


def registrar_post(cfg: dict, estado: dict, canal: str, nome: str, loja: str, link: str,
                   preco: float | None = None, preco_antigo: float | None = None,
                   categoria: str = "", cupom: str | None = None) -> int:
    """Da um numero ao post e guarda o que ele era (para o painel cruzar com os relatorios de venda)."""
    num = int(estado.get("seq_posts", 0)) + 1
    estado["seq_posts"] = num
    asin = re.search(r"/dp/([A-Z0-9]{10})", link)
    estado.setdefault("posts_rastreados", {})[str(num)] = {
        "em": datetime.now().isoformat(timespec="seconds"), "canal": canal, "loja": loja,
        "nome": nome[:120], "marca": marca_conhecida(cfg, nome, loja) or "", "categoria": categoria,
        "preco": preco, "de": preco_antigo, "cupom": cupom or "", "asin": asin.group(1) if asin else "",
    }
    return num


def buscar_feed_awin(cfg: dict) -> list[Oferta]:
    aw = cfg.get("awin", {})
    url = aw.get("url_do_feed", "")
    if not aw.get("usar_feed", False) or not preenchido(url):
        return []
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        conteudo = r.content
        if conteudo[:2] == b"\x1f\x8b":
            conteudo = gzip.decompress(conteudo)
        texto = conteudo.decode("utf-8-sig", errors="replace")
    except Exception as erro:
        log.info(f"Renner (feed Awin): erro ao baixar: {erro}")
        return []

    palavras = [p.lower() for p in aw.get("palavras_chave", [])]
    desconto_min = aw.get("desconto_minimo", 30)
    preco_max = aw.get("preco_maximo", 0)
    ofertas = []
    linhas = csv.DictReader(io.StringIO(texto))
    total = 0
    for linha in linhas:
        total += 1
        nome = linha.get("product_name") or ""
        if palavras and not any(p in nome.lower() for p in palavras):
            continue
        preco = parse_preco(linha.get("search_price") or linha.get("store_price"))
        antigo = parse_preco(linha.get("rrp_price") or linha.get("product_price_old"))
        link = linha.get("aw_deep_link")
        if not (preco and antigo and link) or antigo <= preco:
            continue
        desconto = (1 - preco / antigo) * 100
        if desconto < desconto_min or (preco_max and preco > preco_max):
            continue
        ofertas.append(Oferta(
            loja="renner", nome=nome, preco=preco, preco_antigo=antigo, link=link,
            imagem=linha.get("merchant_image_url") or linha.get("aw_image_url"),
            chave=f"renner:{linha.get('aw_product_id') or link}",
        ))
    log.info(f"Renner (feed Awin): {total} produtos no feed, {len(ofertas)} passaram nos filtros")
    return ofertas


RENNER = "https://www.lojasrenner.com.br"


def _renner_registros(url: str) -> list[dict]:
    """Le os produtos de uma pagina de lista/categoria da Renner (dados que a propria pagina traz)."""
    r = requests.get(url, headers={"User-Agent": NAVEGADOR, "Accept-Language": "pt-BR"}, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return []
    achados: list[dict] = []

    def andar(o):
        if isinstance(o, dict):
            a = o.get("attributes")
            if isinstance(a, dict) and "prop.product.displayName" in a:
                achados.append(a)
            for v in o.values():
                andar(v)
        elif isinstance(o, list):
            for v in o:
                andar(v)

    andar(json.loads(m.group(1)))
    return achados


def cupom_renner(cfg: dict, nome: str, marca: str, hierarquia: str) -> tuple[str | None, list[str]]:
    """Cupom do programa Favoritos Renner (vem por e-mail da Awin). Some sozinho depois de 'ate'."""
    rc = cfg.get("renner_cupom", {})
    cupom = rc.get("cupom", "")
    ate = rc.get("ate", "")
    if not preenchido(cupom) or (ate and datetime.now().strftime("%Y-%m-%d") > ate):
        return None, []
    texto = f"{nome} {marca} {hierarquia}".lower()
    if any(x.lower() in texto for x in rc.get("exceto", [])):
        return None, []
    extras = [rc["regra"]] if rc.get("regra") else []
    for c in rc.get("categorias", []):
        if any(t.lower() in texto for t in c.get("termos", [])):
            extras.append(f"➕ Junte com o cupom {c['cupom']}: {c['texto']}")
            break
    return cupom, extras


def buscar_renner_promocoes(cfg: dict) -> list[Oferta]:
    """Percorre as listas de promocao da Renner e pega pecas femininas com desconto alto."""
    rc = cfg.get("renner_auto", {})
    if not rc.get("ativo", False):
        return []
    if not link_awin(cfg, RENNER):
        log.info("Renner automatica: falta configurar a secao [awin] — pulando.")
        return []
    desconto_min = rc.get("desconto_minimo", 50)
    preco_max = rc.get("preco_maximo", 0)
    permitidas = [c.lower() for c in rc.get("categorias", ["feminino", "bolsas-e-calcados", "perfumaria-e-cosmeticos"])]

    paginas = list(rc.get("paginas_extras", []))
    try:  # as listas de promocao mudam: pega as que estao na pagina inicial agora
        home = requests.get(RENNER + "/", headers={"User-Agent": NAVEGADOR}, timeout=30).text
        paginas += sorted(set(re.findall(r'/lista/-/N-[0-9a-z]+', home)))
    except Exception as erro:
        log.info(f"Renner automatica: nao consegui abrir a pagina inicial: {erro}")

    vistos, ofertas, total = set(), [], 0
    for pagina in dict.fromkeys(paginas):
        url = pagina if pagina.startswith("http") else RENNER + pagina
        try:
            registros = _renner_registros(url)
        except Exception as erro:
            log.info(f"Renner automatica: erro em {pagina}: {erro}")
            continue
        total += len(registros)
        for a in registros:
            g = lambda k: (a.get(k) or [""])[0]
            pid = g("prop.product.repositoryId") or g("record.id")
            if pid in vistos:
                continue
            vistos.add(pid)
            hierarquia = g("dim.product.categoryHierarchy").lower()
            if "masculin" in hierarquia or "infantil" in hierarquia or not any(hierarquia.startswith(c) for c in permitidas):
                continue
            if g("dim.product.stockStatus") and g("dim.product.stockStatus") != "INSTOCK":
                continue
            preco, antigo = parse_preco(g("prop.sku.activePrice")), parse_preco(g("prop.sku.listPrice"))
            if not (preco and antigo) or antigo <= preco:
                continue
            desconto = (1 - preco / antigo) * 100
            marca = g("prop.product.brand.name")
            favorita = marca_favorita(cfg, marca + " " + g("prop.product.displayName"))
            d_min = cfg.get("marcas_favoritas", {}).get("desconto_minimo", 15) if favorita else desconto_min
            if desconto < d_min or (preco_max and preco > preco_max):
                continue
            caminho = g("prop.product.url")
            imagem = g("prop.product.smallImageUrl").replace("/small/", "/zoom/")
            if imagem.startswith("//"):
                imagem = "https:" + imagem
            nome = g("prop.product.displayName")
            if favorita and marca and marca.lower() not in nome.lower():
                nome = f"{marca} — {nome}"  # deixa a marca visivel no post
            cupom, extras = cupom_renner(cfg, nome, marca, hierarquia)
            ofertas.append(Oferta(
                loja="renner", nome=nome, preco=preco, preco_antigo=antigo,
                link=link_awin(cfg, RENNER + caminho), imagem=imagem or None, chave=f"renner:{pid}",
                categoria=hierarquia.split("|")[0],
                favorita=favorita, cupom=cupom, extras=extras,
            ))
        time.sleep(1)
    log.info(f"Renner automatica: {len(paginas)} listas, {total} pecas lidas, {len(ofertas)} passaram nos filtros")
    return ofertas


def buscar_hardyn(cfg: dict) -> list[Oferta]:
    """Outlet da Hardyn (parceria por cupom): le a pagina publica de outlet."""
    hc = cfg.get("hardyn", {})
    if not hc.get("ativo", False):
        return []
    base = "https://www.hardyn.com.br"
    desconto_min = hc.get("desconto_minimo", 30)
    preco_max = hc.get("preco_maximo", 0)
    ofertas, total = [], 0
    for pagina in hc.get("paginas", ["/hotsite/outlet"]):
        try:
            r = requests.get(base + pagina, headers={"User-Agent": NAVEGADOR, "Accept-Language": "pt-BR"}, timeout=30)
            r.raise_for_status()
        except Exception as erro:
            log.info(f"Hardyn: erro ao abrir {pagina}: {erro}")
            continue
        for card in re.split(r'<li data-id-produto=', r.text)[1:]:
            total += 1
            nome = re.search(r'data-nome="([^"]+)"', card)
            link = re.search(r'class=linkProduto href="([^"]+)"', card)
            de = re.search(r'class="de p-price">\s*R\$\s*([\d.,]+)', card)
            por = re.search(r'class="boleto p-price"[^>]*>\s*R\$\s*([\d.,]+)', card)
            img = re.search(r'<img src="([^"]+)"', card)
            if not (nome and link and por):
                continue
            preco, antigo = parse_preco(por.group(1)), parse_preco(de.group(1)) if de else None
            if not preco or not antigo or antigo <= preco:
                continue
            desconto = (1 - preco / antigo) * 100
            if desconto < desconto_min or (preco_max and preco > preco_max):
                continue
            pid = re.search(r"/produto/(\d+)/", link.group(1))
            ofertas.append(Oferta(
                loja="hardyn", nome=html.unescape(nome.group(1)), preco=preco, preco_antigo=antigo,
                link=base + link.group(1), imagem=img.group(1) if img else None,
                chave=f"hardyn:{pid.group(1) if pid else link.group(1)}", categoria="fitness",
                cupom=hc.get("cupom") or cfg.get("cupons", {}).get("hardyn") or None,
            ))
    log.info(f"Hardyn (outlet): {total} produtos lidos, {len(ofertas)} passaram nos filtros")
    return ofertas


# --------------------------------------------------------------------------- #
# Fonte 3: lista de produtos vigiados (Mercado Livre, Renner, Shein...)
# --------------------------------------------------------------------------- #

def ler_pagina_produto(url: str) -> dict:
    """Le nome, preco e foto de uma pagina de produto (dados estruturados da propria loja)."""
    r = requests.get(url, headers={
        "User-Agent": NAVEGADOR, "Accept-Language": "pt-BR,pt;q=0.9",
    }, timeout=30)
    r.raise_for_status()
    pagina = r.text
    info: dict = {}

    # 1) JSON-LD (schema.org Product)
    for bloco in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        pagina, flags=re.S | re.I,
    ):
        try:
            dados = json.loads(bloco.strip())
        except Exception:
            continue
        candidatos = dados if isinstance(dados, list) else [dados]
        for item in list(candidatos):
            if isinstance(item, dict) and "@graph" in item:
                candidatos += item["@graph"]
        for item in candidatos:
            if not isinstance(item, dict) or "Product" not in str(item.get("@type")):
                continue
            ofertas = item.get("offers") or {}
            if isinstance(ofertas, list):
                ofertas = ofertas[0] if ofertas else {}
            preco = parse_preco(ofertas.get("price") or ofertas.get("lowPrice"))
            if preco:
                info["preco"] = preco
                info["nome"] = item.get("name")
                img = item.get("image")
                if isinstance(img, list):
                    img = img[0] if img else None
                if isinstance(img, dict):
                    img = img.get("url")
                info["imagem"] = img
                break
        if "preco" in info:
            break

    # 2) Meta tags (reserva)
    def meta(*nomes):
        for n in nomes:
            m = re.search(
                rf'<meta[^>]+(?:property|name|itemprop)=["\']{re.escape(n)}["\'][^>]+content=["\']([^"\']+)',
                pagina, flags=re.I,
            )
            if m:
                return html.unescape(m.group(1))
        return None

    if "preco" not in info:
        p = parse_preco(meta("product:price:amount", "og:price:amount", "price"))
        if p:
            info["preco"] = p
    info["nome"] = info.get("nome") or meta("og:title")
    info["imagem"] = info.get("imagem") or meta("og:image")
    # Preco antigo ("De"), quando a loja mostra (ex.: lojas Magento como a Kaisan)
    m = re.search(r'old-price.{0,400}?R\$\s*(?:&nbsp;)?\s*([\d.]+,\d{2})', pagina, flags=re.S)
    if m and info.get("preco"):
        antigo = parse_preco(m.group(1))
        if antigo and antigo > info["preco"]:
            info["preco_antigo"] = antigo
    if info["imagem"] and info["imagem"].startswith("//"):
        info["imagem"] = "https:" + info["imagem"]
    return info


_ML_TOKEN: dict = {}


def ler_mercadolivre(cfg: dict, url: str) -> dict:
    """O Mercado Livre bloqueia leitura de paginas por robos; usa a API oficial."""
    ml = cfg.get("mercadolivre", {})
    cid, segredo = str(ml.get("client_id", "")), str(ml.get("client_secret", ""))
    if not (preenchido(cid) and preenchido(segredo)):
        raise RuntimeError("preencha client_id e client_secret na secao [mercadolivre]")
    if not _ML_TOKEN or _ML_TOKEN["expira"] < time.time():
        r = requests.post("https://api.mercadolibre.com/oauth/token", data={
            "grant_type": "client_credentials", "client_id": cid, "client_secret": segredo,
        }, timeout=30)
        dados = r.json()
        if "access_token" not in dados:
            raise RuntimeError(f"Mercado Livre recusou as credenciais: {dados.get('message', dados)}")
        _ML_TOKEN.update(token=dados["access_token"], expira=time.time() + dados.get("expires_in", 3600) - 120)
    auth = {"Authorization": f"Bearer {_ML_TOKEN['token']}"}

    catalogo = re.search(r"/p/(MLB\d+)", url)
    if catalogo:  # pagina de catalogo (.../p/MLB123)
        d = requests.get(f"https://api.mercadolibre.com/products/{catalogo.group(1)}", headers=auth, timeout=30).json()
        caixa = d.get("buy_box_winner") or {}
        fotos = d.get("pictures") or [{}]
        return {"preco": caixa.get("price"), "nome": d.get("name"), "imagem": fotos[0].get("url")}

    item = re.search(r"MLB-?(\d+)", url)
    if not item:
        raise RuntimeError("nao achei o codigo MLB no link")
    d = requests.get(f"https://api.mercadolibre.com/items/MLB{item.group(1)}", headers=auth, timeout=30).json()
    if "price" not in d:
        raise RuntimeError(d.get("message", "resposta sem preco"))
    fotos = d.get("pictures") or [{}]
    return {"preco": d.get("price"), "nome": d.get("title"),
            "imagem": fotos[0].get("secure_url") or d.get("thumbnail")}


def verificar_lista(cfg: dict, estado: dict) -> list[Oferta]:
    produtos = cfg.get("produto", [])
    if not produtos:
        return []
    queda_min = cfg.get("vigiados", {}).get("queda_minima_percentual", 10)
    vistos = estado.setdefault("vigiados", {})
    ofertas = []

    for prod in produtos:
        url = prod.get("url", "")
        loja = prod.get("loja", "").lower().replace(" ", "")
        if not preenchido(url):
            continue
        if loja == "shein":
            log.info("Shein bloqueia robos (captcha), entao nao da para vigiar preco. "
                     "Use o postar-oferta.bat para postar ofertas da Shein.")
            continue
        link = prod.get("link_afiliado", "")
        cupom = prod.get("cupom") or cfg.get("cupons", {}).get(loja)
        if not preenchido(link) and loja == "renner":
            link = link_awin(cfg, url) or ""
        if not preenchido(link) and preenchido(cupom):
            link = url  # parceria por cupom: o link e o normal, a comissao vem do cupom
        if not preenchido(link):
            log.info(f"Vigiado sem link de afiliado, pulando: {url}")
            continue

        try:
            if loja == "mercadolivre":
                info = ler_mercadolivre(cfg, url)
            else:
                info = ler_pagina_produto(url)
        except Exception as erro:
            log.info(f"Vigiado [{loja}] nao consegui abrir a pagina ({erro}): {url}")
            continue
        preco = info.get("preco")
        if not preco:
            log.info(f"Vigiado [{loja}] nao achei o preco na pagina: {url}")
            continue

        nome = prod.get("nome") or info.get("nome") or "Oferta"
        nome = re.sub(r"\s*\|\s*Ref:.*$", "", nome)  # tira o codigo de referencia (Kaisan)
        reg = vistos.setdefault(url, {})
        # "referencia" = preco normal do produto; so muda quando o preco NAO esta em queda
        # (assim uma queda nao se perde se nao der para postar nesta rodada).
        anterior = reg.get("referencia")
        reg["maior_preco"] = max(reg.get("maior_preco", preco), preco)
        reg["menor_preco"] = min(reg.get("menor_preco", preco), preco)
        reg["ultimo_preco"] = preco
        reg["visto_em"] = datetime.now().isoformat(timespec="minutes")

        alvo = parse_preco(prod.get("preco_alvo"))
        caiu = anterior is not None and preco <= anterior * (1 - queda_min / 100)
        if not caiu:
            reg["referencia"] = preco
        ultimo_postado = reg.get("ultimo_postado")
        if ultimo_postado is not None and preco > ultimo_postado * (1 + queda_min / 100):
            reg.pop("ultimo_postado")  # preco subiu de novo: uma nova queda pode ser postada
            ultimo_postado = None
        bateu_alvo = alvo is not None and preco <= alvo
        ja_postado_igual = ultimo_postado is not None and preco >= ultimo_postado

        situacao = f"{reais(preco)}" + (f" (antes {reais(anterior)})" if anterior else " (primeira leitura)")
        log.info(f"Vigiado [{loja}] {nome[:40]}: {situacao}")

        # Produto que a loja ja mostra em promocao ("De R$ X por R$ Y")
        antigo_loja = info.get("preco_antigo")
        em_promocao = bool(antigo_loja) and preco <= antigo_loja * (1 - queda_min / 100)

        if (caiu or bateu_alvo or em_promocao) and not ja_postado_igual:
            de = antigo_loja or (anterior if (anterior and anterior > preco) else reg["maior_preco"])
            extras = []
            if preco <= reg["menor_preco"] and reg["maior_preco"] > preco:
                extras.append("📉 Menor preço que já vimos!")
            ofertas.append(Oferta(
                loja=loja, nome=nome, preco=preco,
                preco_antigo=de if de and de > preco else None,
                link=link, imagem=prod.get("imagem") or info.get("imagem"),
                chave=f"vigiado:{url}", extras=extras, categoria=loja,
                cupom=cupom if preenchido(cupom) else None,
            ))
            reg["_candidato"] = preco
    return ofertas


# --------------------------------------------------------------------------- #
# Estado (o que ja foi postado)
# --------------------------------------------------------------------------- #

def carregar_estado() -> dict:
    if ARQ_ESTADO.exists():
        try:
            return json.loads(ARQ_ESTADO.read_text(encoding="utf-8"))
        except Exception:
            log.info("estado.json corrompido — comecando do zero.")
    return {}


def salvar_estado(estado: dict) -> None:
    ARQ_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")


def nome_base(nome: str) -> str:
    """Mesmo produto em outra cor/tamanho tem o mesmo comeco de nome (ex.: 'Short Canelado Azul/Rosa')."""
    return re.sub(r"[^a-z0-9]", "", nome.lower())[:22]


def pode_postar(oferta: Oferta, estado: dict, dias_repetir: int) -> bool:
    if oferta.chave.startswith("vigiado:"):
        return True  # regra propria em verificar_lista
    parecido = estado.get("nomes_postados", {}).get(nome_base(oferta.nome))
    if parecido and datetime.now() - datetime.fromisoformat(parecido) < timedelta(days=dias_repetir):
        return False  # outra cor/versao do mesmo produto saiu ha pouco
    postado = estado.get("postados", {}).get(oferta.chave)
    if not postado:
        return True
    quando = datetime.fromisoformat(postado["em"])
    mais_barato = oferta.preco < postado["preco"] * 0.95
    return mais_barato or datetime.now() - quando > timedelta(days=dias_repetir)


def marcar_postado(oferta: Oferta, estado: dict) -> None:
    agora = datetime.now().isoformat(timespec="minutes")
    if oferta.chave.startswith("vigiado:"):
        url = oferta.chave.split(":", 1)[1]
        reg = estado.setdefault("vigiados", {}).setdefault(url, {})
        reg["ultimo_postado"] = oferta.preco
        reg["referencia"] = oferta.preco
        reg.pop("_candidato", None)
    else:
        estado.setdefault("postados", {})[oferta.chave] = {"preco": oferta.preco, "em": agora}
    estado.setdefault("nomes_postados", {})[nome_base(oferta.nome)] = agora


def limpar_estado(estado: dict) -> None:
    limite = datetime.now() - timedelta(days=60)
    postados = estado.get("postados", {})
    for chave in [k for k, v in postados.items() if datetime.fromisoformat(v["em"]) < limite]:
        del postados[chave]
    nomes = estado.get("nomes_postados", {})
    for chave in [k for k, v in nomes.items() if datetime.fromisoformat(v) < datetime.now() - timedelta(days=30)]:
        del nomes[chave]
    rastreados = estado.get("posts_rastreados", {})
    for chave in [k for k, v in rastreados.items() if datetime.fromisoformat(v["em"]) < datetime.now() - timedelta(days=180)]:
        del rastreados[chave]
    for reg in estado.get("vigiados", {}).values():
        reg.pop("_candidato", None)


# --------------------------------------------------------------------------- #
# Rodada principal
# --------------------------------------------------------------------------- #

def dentro_do_horario(cfg: dict) -> bool:
    ag = cfg.get("agenda", {})
    inicio, fim = ag.get("postar_a_partir_de", "08:00"), ag.get("postar_ate", "23:00")
    agora = datetime.now().strftime("%H:%M")
    return inicio <= agora <= fim if inicio <= fim else (agora >= inicio or agora <= fim)


def rodada(cfg: dict, tg: Telegram, modo_teste: bool) -> None:
    estado = carregar_estado()
    ag = cfg.get("agenda", {})
    max_posts = int(ag.get("maximo_de_posts_por_rodada", 5))
    pausa = int(ag.get("segundos_entre_posts", 60))
    dias_repetir = int(ag.get("dias_para_repetir_oferta", 7))

    # Ofertas que a Nicole encaminhou para o bot: postam na hora, com os links dela
    if not modo_teste:
        if processar_encaminhados(cfg, estado, tg):
            salvar_estado(estado)

    ofertas = (buscar_shopee(cfg) + buscar_shopee_feed(cfg) + buscar_feed_awin(cfg) + buscar_renner_promocoes(cfg)
               + buscar_hardyn(cfg)
               + verificar_lista(cfg, estado))
    # Resumo da rodada (para conferir de longe se todas as lojas responderam)
    contagem: dict[str, int] = {}
    for o in ofertas:
        contagem[o.loja] = contagem.get(o.loja, 0) + 1
    estado["ultima_rodada"] = {"em": datetime.now().isoformat(timespec="minutes"), "ofertas_por_loja": contagem}

    # Filtros gerais: conteudo que nao combina com o canal e descontos "bons demais"
    # (preco antigo inflado so para parecer promocao).
    filtros = cfg.get("filtros", {})
    bloqueadas = [p.lower() for p in filtros.get("palavras_bloqueadas", [])]
    desconto_max = filtros.get("desconto_maximo", 0)
    ofertas = [
        o for o in ofertas
        if not any(p in o.nome.lower() for p in bloqueadas)
        and not (desconto_max and o.desconto > desconto_max)
    ]
    # Gosto da Nicole ([estilo] no config): o que ela escolheria sobe na fila, o que ela acha feio sai
    estilo = cfg.get("estilo", {})
    evitar = [p.lower() for p in estilo.get("evitar", [])]
    gosta = [p.lower() for p in estilo.get("preferidos", [])]
    def combina_mal(nome: str) -> bool:  # ex.: legging + cor dificil de combinar
        n = nome.lower()
        return any(any(re.search(rf"\b{re.escape(t)}s?\b", n) for t in c.get("pecas", []))  # "calça" != "calçado"
                   and any(cor in n for cor in c.get("cores", []))
                   for c in estilo.get("evitar_combinacoes", []))
    ofertas = [o for o in ofertas if not any(p in o.nome.lower() for p in evitar) and not combina_mal(o.nome)]
    for o in ofertas:
        if any(p in o.nome.lower() for p in gosta):
            o.prioridade = o.ordem + float(estilo.get("bonus", 30))
    candidatas_do_dia = list(ofertas)  # para o resumo diario (inclui as ja postadas)
    ofertas = [o for o in ofertas if pode_postar(o, estado, dias_repetir)]

    # Vigiados primeiro (sao quedas reais de preco), depois maiores descontos.
    # Intercala as lojas para o canal nao ficar so com uma.
    ofertas.sort(key=lambda o: (not o.chave.startswith("vigiado:"), -o.ordem))
    # Evita postar varias versoes do mesmo produto (ex.: o mesmo item em 3 cores)
    parecidos, unicas = set(), []
    for o in ofertas:
        base = re.sub(r"[^a-z0-9]", "", o.nome.lower())[:22]
        if base not in parecidos:
            parecidos.add(base)
            unicas.append(o)
    ofertas = unicas
    por_loja: dict[str, list[Oferta]] = {}
    for o in ofertas:
        por_loja.setdefault(o.grupo, []).append(o)
    # Reveza as lojas: comeca pela que esta ha mais tempo sem aparecer no canal
    vez = estado.get("ultima_vez_loja", {})
    # Lojas de roupa (parceiras) aparecem com espaco entre um post e outro,
    # para o canal nao virar so roupa. So entram antes se nao houver outra opcao.
    espacos = ag.get("intervalo_por_loja", {})
    def descansando(grupo: str) -> bool:
        minimo = espacos.get(grupo.split("-")[0] if grupo.startswith("shopee-") else grupo, 0)
        ultimo_g = vez.get(grupo)
        return bool(minimo and ultimo_g and
                    (datetime.now() - datetime.fromisoformat(ultimo_g)).total_seconds() / 60 < minimo)
    livres = {g: l for g, l in por_loja.items() if not descansando(g)}
    if livres:
        por_loja = livres
    por_loja = dict(sorted(por_loja.items(), key=lambda kv: vez.get(kv[0], "")))
    fila = []
    while any(por_loja.values()):
        for lista in por_loja.values():
            if lista:
                fila.append(lista.pop(0))
    fila = fila[:max_posts]

    # Respeita o intervalo entre posts mesmo se o robo for reiniciado
    intervalo = int(ag.get("verificar_a_cada_minutos", 30))
    ultimo = estado.get("ultimo_post")
    if fila and not modo_teste and ultimo:
        minutos = (datetime.now() - datetime.fromisoformat(ultimo)).total_seconds() / 60
        if minutos < intervalo * 0.75:  # folga para atrasos do agendador da nuvem
            log.info(f"Ultimo post foi ha {minutos:.0f} min — esperando completar {intervalo} min.")
            fila = []

    log.info(f"{len(ofertas)} ofertas novas encontradas; postando {len(fila)}.")
    if fila and not modo_teste and not dentro_do_horario(cfg):
        log.info("Fora do horario de postagem — guardando para a proxima rodada.")
        fila = []

    for i, oferta in enumerate(fila):
        try:
            tg.enviar(montar_mensagem(oferta, cfg), oferta.imagem)
            if not modo_teste:
                marcar_postado(oferta, estado)
                estado["ultimo_post"] = datetime.now().isoformat(timespec="seconds")
                estado.setdefault("ultima_vez_loja", {})[oferta.grupo] = estado["ultimo_post"]
                log.info(f"  ✔ postado: [{oferta.loja}] {oferta.nome[:50]}")
        except Exception as erro:
            log.info(f"  ✘ erro ao postar '{oferta.nome[:40]}': {erro}")
        if not modo_teste and i < len(fila) - 1:
            time.sleep(pausa)

    # Dias promocionais e cupons oficiais da Shopee (dentro do horario de postagem)
    if modo_teste or dentro_do_horario(cfg):
        try:
            avisar_dia_promocional(cfg, estado, tg, modo_teste)
            avisar_cupons_shopee(cfg, estado, tg, modo_teste)
        except Exception as erro:
            log.info(f"  ✘ erro nos avisos de cupons/dias promocionais: {erro}")

    # Resumo diario para a Nicole repassar no Canal do WhatsApp (1x por dia)
    rc = cfg.get("resumo_whatsapp", {})
    hoje = datetime.now().strftime("%Y-%m-%d")
    if (rc.get("ativo") and preenchido(rc.get("chat_id")) and not modo_teste
            and datetime.now().hour >= int(rc.get("hora", 19)) and estado.get("resumo_enviado_em") != hoje):
        try:
            enviados = enviar_resumo_whatsapp(tg, candidatas_do_dia, cfg, estado)
            if enviados:
                estado["resumo_enviado_em"] = hoje
                for o in enviados:  # o que foi para o WhatsApp nao repete no Telegram por 7 dias
                    marcar_postado(o, estado)
                log.info("  ✔ resumo do dia enviado no seu privado")
        except Exception as erro:
            log.info(f"  ✘ erro ao enviar o resumo do dia: {erro}")

    limpar_estado(estado)
    if not modo_teste:
        salvar_estado(estado)


def escolher_para_resumo(ofertas: list[Oferta], qtd: int) -> list[Oferta]:
    """O melhor de cada loja, revezando, sem repetir produto parecido."""
    filas: dict[str, list[Oferta]] = {}
    for o in sorted(ofertas, key=lambda o: -o.ordem):
        filas.setdefault(o.grupo, []).append(o)
    filas = dict(sorted(filas.items(), key=lambda kv: kv[0] != "favoritas"))  # favoritas primeiro
    escolhidas = []
    while len(escolhidas) < qtd and any(filas.values()):
        for fila in filas.values():
            if fila and len(escolhidas) < qtd:
                escolhidas.append(fila.pop(0))
    return escolhidas


def legenda_whatsapp(o: Oferta, link: str | None = None) -> str:
    """Legenda pronta para o WhatsApp (*negrito*, ~riscado~)."""
    loja = NOMES_LOJAS.get(o.loja, o.loja.title())
    nome = o.nome.strip()
    nome = nome[:90] + "..." if len(nome) > 90 else nome
    linhas = [f"🔥 *{o.desconto}% OFF*" if o.desconto >= 1 else "🔥 *Achado do dia*", "", f"🛍️ {nome}", ""]
    if o.desconto >= 1:
        linhas.append(f"De ~{reais(o.preco_antigo)}~")
        linhas.append(f"💰 *Por {reais(o.preco)}* na {loja}")
    else:
        linhas.append(f"💰 *{reais(o.preco)}* na {loja}")
    if o.cupom:
        linhas.append(f"🎟️ Cupom: *{o.cupom}*")
    linhas += ["", f"🛒 {link or o.link.replace('sub_id=telegram', 'sub_id=whatsapp')}", "",
               "#publi Link/cupom de parceria: posso ganhar uma comissão, sem custo extra pra você."]
    return "\n".join(linhas)


def enviar_resumo_whatsapp(tg: "Telegram", ofertas: list[Oferta], cfg: dict, estado: dict) -> list[Oferta]:
    """Manda no privado cada achado separado (imagem pronta + legenda) para encaminhar ao WhatsApp."""
    from cartao import gerar_cartao  # so precisa do Pillow aqui

    rc = cfg.get("resumo_whatsapp", {})
    chat = str(rc["chat_id"])
    escolhidas = escolher_para_resumo(ofertas, int(rc.get("quantidade", 5)))
    if not escolhidas:
        return []
    tg.enviar_privado(chat, f"💸 Achados do dia ({datetime.now():%d/%m}) para o Canal do WhatsApp — "
                            f"{len(escolhidas)} posts abaixo, cada um com imagem e legenda.")
    for o in escolhidas:
        loja = NOMES_LOJAS.get(o.loja, o.loja.title())
        foto = gerar_cartao(o.nome, o.preco, o.preco_antigo, loja, o.imagem, o.cupom, "Achados da @bonentti")
        num = registrar_post(cfg, estado, "whatsapp", o.nome, o.loja, o.link, o.preco, o.preco_antigo,
                             o.categoria, o.cupom)
        tg.enviar_foto_privado(chat, foto, legenda_whatsapp(o, link_rastreado(cfg, o.link, "whatsapp", num)))
        time.sleep(1)
    if rc.get("rodape"):
        tg.enviar_privado(chat, rc["rodape"])
    return escolhidas


def montar_resumo_whatsapp(ofertas: list[Oferta], cfg: dict) -> str:
    """Texto pronto para colar no WhatsApp (*negrito*, ~riscado~), com os melhores achados."""
    rc = cfg.get("resumo_whatsapp", {})
    qtd = int(rc.get("quantidade", 5))
    filas: dict[str, list[Oferta]] = {}
    for o in sorted(ofertas, key=lambda o: -o.ordem):  # o melhor de cada loja, revezando
        filas.setdefault(o.loja, []).append(o)
    escolhidas = []
    while len(escolhidas) < qtd and any(filas.values()):
        for fila in filas.values():
            if fila and len(escolhidas) < qtd:
                escolhidas.append(fila.pop(0))
    if not escolhidas:
        return ""
    numeros = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
    linhas = [f"💸 *Achados do dia* ({datetime.now():%d/%m})", ""]
    for i, o in enumerate(escolhidas):
        nome = o.nome.strip()
        nome = nome[:80] + "..." if len(nome) > 80 else nome
        loja = NOMES_LOJAS.get(o.loja, o.loja.title())
        linhas.append(f"{numeros[i % len(numeros)]} *{nome}*")
        if o.desconto >= 1:
            linhas.append(f"~{reais(o.preco_antigo)}~ por *{reais(o.preco)}* (-{o.desconto}%) na {loja}")
        else:
            linhas.append(f"*{reais(o.preco)}* na {loja}")
        if o.cupom:
            linhas.append(f"🎟️ Cupom: *{o.cupom}*")
        linhas.append(f"🛒 {o.link.replace('sub_id=telegram', 'sub_id=whatsapp')}")  # vendas por canal
        linhas.append("")
    if rc.get("rodape"):
        linhas.append(rc["rodape"])
    return "\n".join(linhas).strip()


# --------------------------------------------------------------------------- #
# Cupons e dias promocionais da Shopee
# --------------------------------------------------------------------------- #

CANAL_OFICIAL_SHOPEE = "afiliadosshopeebroficial"  # canal oficial da Shopee para afiliados
_INTERNO = re.compile(r"afiliad|criador|comiss|inscrev|inscri|regulamento|forms\.gle|corrida de vendas", re.I)


def _posts_canal_oficial() -> list[dict]:
    """Le a pre-visualizacao publica do canal oficial de afiliados da Shopee no Telegram."""
    t = requests.get(f"https://t.me/s/{CANAL_OFICIAL_SHOPEE}", headers={"User-Agent": NAVEGADOR}, timeout=30).text
    posts = []
    for b in re.split(r'<div class="tgme_widget_message_wrap', t)[1:]:
        pid = re.search(r'data-post="[^/]+/(\d+)"', b)
        quando = re.search(r'datetime="([^"]+)"', b)
        corpo = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
        if not (pid and quando and corpo):
            continue
        texto = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", corpo.group(1))))
        posts.append({"id": int(pid.group(1)), "em": datetime.fromisoformat(quando.group(1)), "texto": texto})
    return posts


def montar_aviso_cupom(texto: str, id_af: str) -> str | None:
    """Transforma um post oficial com cupons num aviso para o canal (com o link de afiliada)."""
    if not re.search(r"cupo", texto, re.I) or re.search(r"forms\.gle|inscrev", texto, re.I):
        return None
    paginas = re.findall(r"https?://(?:www\.)?shopee\.com\.br/m/[^\s#]+", texto)
    codigos = re.findall(r"\b([A-Z0-9]{6,20})\s*[:–-]\s*([^\n]{5,90})", texto)
    if not paginas and not codigos:
        return None
    linhas_txt = [l.strip() for l in texto.splitlines() if l.strip()]
    titulo = next((l for l in linhas_txt if not _INTERNO.search(l) and "http" not in l), "Cupons na Shopee")
    e = html.escape
    msg = ["🎟️ <b>CUPONS NA SHOPEE</b>", "", e(titulo[:120])]
    if codigos:
        msg.append("")
        for codigo, desc in codigos[:6]:
            msg.append(f"🎟️ <code>{e(codigo)}</code>: {e(desc.strip())}")
    if paginas:
        link = link_shopee_afiliado(paginas[0], id_af)
        msg += ["", f'👉 Pegue os cupons aqui: {e(link)}']
    else:  # so codigo: leva para a Shopee para usar no carrinho
        link = link_shopee_afiliado("https://shopee.com.br/", id_af)
        msg += ["", f'👉 Use o cupom no carrinho: {e(link)}']
    msg += ["", "⚠️ Cupons são limitados e acabam rápido.",
            "#publi 🔗 Link de parceria: posso ganhar uma comissão, sem custo extra pra você."]
    return "\n".join(msg)


def avisar_cupons_shopee(cfg: dict, estado: dict, tg: "Telegram", modo_teste: bool) -> int:
    """Posta no canal os cupons novos que a Shopee divulgou no canal oficial de afiliados."""
    cc = cfg.get("cupons_shopee", {})
    id_af = str(cfg.get("shopee", {}).get("id_afiliado", ""))
    if not cc.get("ativo", False) or not preenchido(id_af):
        return 0
    try:
        posts = _posts_canal_oficial()
    except Exception as erro:
        log.info(f"Cupons Shopee: nao consegui ler o canal oficial: {erro}")
        return 0
    ultimo = estado.get("cupons_ultimo_id", 0)
    limite = datetime.now().astimezone() - timedelta(hours=int(cc.get("validade_horas", 6)))
    enviados = 0
    for p in sorted(posts, key=lambda p: p["id"]):
        if p["id"] <= ultimo:
            continue
        if not modo_teste:
            estado["cupons_ultimo_id"] = p["id"]
        if p["em"] < limite:  # antigo demais: cupom provavelmente ja acabou
            continue
        aviso = montar_aviso_cupom(p["texto"], id_af)
        if aviso and enviados < int(cc.get("maximo_por_rodada", 2)):
            tg.enviar(aviso)
            enviados += 1
            log.info(f"  ✔ cupom avisado (post oficial {p['id']})")
    return enviados


def avisar_dia_promocional(cfg: dict, estado: dict, tg: "Telegram", modo_teste: bool) -> bool:
    """Nos dias especiais (11.11, 12.12, Black Friday...), avisa no canal logo cedo."""
    dc = cfg.get("dias_promocionais", {})
    if not dc.get("ativo", False):
        return False
    hoje = datetime.now()
    chave = hoje.strftime("%Y-%m-%d")
    if estado.get("dia_promocional_avisado") == chave:
        return False
    nome = None
    for item in dc.get("datas", []):  # "MM-DD Nome" ou "AAAA-MM-DD Nome"
        data, _, rotulo = item.partition(" ")
        if data in (hoje.strftime("%m-%d"), chave):
            nome = rotulo.strip() or "Dia de promoções"
    if not nome and dc.get("datas_duplas", True) and hoje.day == hoje.month:
        nome = f"{hoje.day}.{hoje.month}"  # 1.1, 2.2 ... 12.12
    if not nome:
        return False
    id_af = str(cfg.get("shopee", {}).get("id_afiliado", ""))
    link = link_shopee_afiliado(dc.get("link_cupons", "https://shopee.com.br/"), id_af)
    e = html.escape
    texto = "\n".join([
        f"📅 <b>HOJE É {e(nome.upper()) if nome.lower().startswith('dia ') else 'DIA DE ' + e(nome.upper())} NA SHOPEE!</b>", "",
        "Promoções e cupons o dia todo. Vou postando aqui os melhores achados de lojas oficiais 👇", "",
        f'🎟️ Veja as ofertas e cupons do dia: {e(link)}', "",
        "🔔 Ative as notificações para não perder nada!",
        "#publi 🔗 Link de parceria: posso ganhar uma comissão, sem custo extra pra você.",
    ])
    tg.enviar(texto)
    if not modo_teste:
        estado["dia_promocional_avisado"] = chave
    log.info(f"  ✔ aviso de dia promocional: {nome}")
    return True


_URL = re.compile(r"(?:https?://|(?<![\w.])(?=(?:amzn\.to|a\.co|link\.amazon|shope\.ee|s\.shopee\.com\.br)/))\S+", re.I)
_LINHA_DE_DIVULGADOR = re.compile(
    r"(@\w{3,}|t\.me|chat\.whatsapp|whatsapp\.com|entre no|entra no|participe|meu grupo|nosso grupo|"
    r"meu canal|nosso canal|compartilhe|link na bio|#\w+)", re.I)


def limpar_texto_encaminhado(texto: str) -> str:
    """Tira links, @ e convites do divulgador original; mantem a descricao da oferta."""
    linhas = []
    for linha in texto.splitlines():
        sem_link = _URL.sub("", linha)
        if _LINHA_DE_DIVULGADOR.search(sem_link):
            continue
        sem_link = re.sub(r"\s+$", "", sem_link)
        # linha que so tinha o link (ex.: "👉 https://...") fica vazia: descarta
        if sem_link.strip() and not re.fullmatch(r"[\W_]*(comprar?|compre|link|aqui|clique)?[\W_]*",
                                                  sem_link.strip(), re.I):
            linhas.append(sem_link)
        elif linhas and linhas[-1] != "":
            linhas.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()


def processar_encaminhados(cfg: dict, estado: dict, tg: "Telegram") -> int:
    """Ofertas que a Nicole encaminha para o bot no privado (de outros grupos ou dela mesma):
    o robo troca os links pelos dela (Amazon, Shopee, Renner) e posta no canal."""
    dona = str(cfg.get("resumo_whatsapp", {}).get("chat_id", ""))
    if not preenchido(dona):
        return 0
    try:
        r = tg._api("getUpdates", {"offset": int(estado.get("telegram_offset", 0)) + 1, "timeout": 0,
                                   "allowed_updates": json.dumps(["message"])})
    except Exception as erro:
        log.info(f"Encaminhados: nao consegui ler as mensagens do bot: {erro}")
        return 0
    postados = 0
    for upd in r.get("result", []):
        estado["telegram_offset"] = upd["update_id"]
        msg = upd.get("message") or {}
        if str((msg.get("from") or {}).get("id")) != dona:
            continue  # so aceita ofertas mandadas pela propria Nicole
        texto = msg.get("text") or msg.get("caption") or ""
        entidades = msg.get("entities") or msg.get("caption_entities") or []
        urls = [en["url"] for en in entidades if en.get("type") == "text_link"] + _URL.findall(texto)
        if not urls:
            continue
        links, vistos = [], set()
        for u in urls:
            novo = converter_link(cfg, u.rstrip(").,!"))
            if novo and novo not in vistos:
                vistos.add(novo)
                links.append(novo)
        if not links:
            tg.enviar_privado(dona, "⚠️ Não postei: não achei link da Amazon, Shopee ou Renner nessa "
                                    "mensagem (link de outra loja eu não uso, porque a comissão iria para outra pessoa).")
            continue
        corpo = limpar_texto_encaminhado(texto)
        e = html.escape
        partes = [e(corpo), ""] if corpo else []
        if len(links) == 1:
            partes.append(f"🛒 Comprar aqui: {e(links[0])}")
        else:
            partes += [f"🛒 {e(l)}" for l in links]
        rodape = cfg.get("mensagem", {}).get("rodape", "")
        if rodape:
            partes += ["", e(rodape)]
        post = "\n".join(partes)
        fotos = msg.get("photo") or []
        try:
            if fotos and len(post) <= 1024:
                tg._api("sendPhoto", {"chat_id": tg.canal, "photo": fotos[-1]["file_id"],
                                      "caption": post, "parse_mode": "HTML"})
            else:
                tg._api("sendMessage", {"chat_id": tg.canal, "text": post, "parse_mode": "HTML"})
            postados += 1
            estado["ultimo_post"] = datetime.now().isoformat(timespec="seconds")
            zap = [corpo, ""] if corpo else []
            titulo = next((l.strip() for l in corpo.splitlines() if len(l.strip()) > 8), "oferta encaminhada")
            for l in links:
                loja_l = "amazon" if "amazon." in l else "renner" if "awin1.com" in l else "shopee"
                num = registrar_post(cfg, estado, "whatsapp", titulo, loja_l, l)
                zap.append(f"🛒 {link_rastreado(cfg, l, 'whatsapp', num)}")
            zap += ["", "#publi"]
            tg.enviar_privado(dona, "✅ Postei no canal com os seus links. Versão para o WhatsApp:")
            tg.enviar_privado(dona, "\n".join(zap))
            log.info(f"  ✔ oferta encaminhada postada ({len(links)} link(s))")
        except Exception as erro:
            log.info(f"  ✘ erro ao postar oferta encaminhada: {erro}")
            tg.enviar_privado(dona, f"⚠️ Não consegui postar essa oferta: {erro}")
    return postados


def carregar_config() -> dict:
    if not ARQ_CONFIG.exists():
        raise SystemExit("Nao achei o config.toml na pasta do robo.")
    try:
        with open(ARQ_CONFIG, "rb") as f:
            cfg = tomllib.load(f)
        # Senhas ficam fora do config.toml (que vai para o GitHub):
        # no computador, em segredos.toml; na nuvem, em variaveis de ambiente.
        arq_segredos = PASTA / "segredos.toml"
        if arq_segredos.exists():
            with open(arq_segredos, "rb") as f:
                for secao, valores in tomllib.load(f).items():
                    cfg.setdefault(secao, {}).update(valores)
    except tomllib.TOMLDecodeError as erro:
        raise SystemExit(f"Tem um erro de digitacao no config.toml ou segredos.toml: {erro}")
    if os.environ.get("TELEGRAM_TOKEN"):
        cfg.setdefault("telegram", {})["token_do_bot"] = os.environ["TELEGRAM_TOKEN"].strip()
    if os.environ.get("SHOPEE_FEED_URL"):
        cfg.setdefault("shopee", {})["urls_do_feed"] = os.environ["SHOPEE_FEED_URL"].split()
    return cfg


def main() -> None:
    configurar_log()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description="Robo de Achadinhos")
    sub = p.add_subparsers(dest="comando")
    sub.add_parser("teste", help="uma verificacao, so mostra (nao posta)")
    sub.add_parser("uma-vez", help="uma verificacao, posta de verdade")
    sub.add_parser("testar-telegram", help="manda mensagem de teste no canal")
    lr = sub.add_parser("link-renner", help="gera link de afiliado Awin para a Renner")
    lr.add_argument("url")
    la = sub.add_parser("link-amazon", help="gera o seu link de associada para um produto da Amazon")
    la.add_argument("url", nargs="?")
    ls = sub.add_parser("link-shopee", help="gera o seu link de afiliada para qualquer pagina da Shopee")
    ls.add_argument("url", nargs="?")
    mp = sub.add_parser("postar", help="posta uma oferta manual")
    mp.add_argument("--loja")
    mp.add_argument("--nome")
    mp.add_argument("--preco")
    mp.add_argument("--de", default=None, help="preco antigo (opcional)")
    mp.add_argument("--link", help="seu link de afiliado")
    mp.add_argument("--imagem", default=None)
    mp.add_argument("--so-mostrar", action="store_true")
    mp.add_argument("--confirmar", action="store_true", help="posta sem perguntar")
    mp.add_argument("--cupom", default=None)
    mp.add_argument("--chamada", default=None, help="frase de impacto no topo")
    args = p.parse_args()
    cfg = carregar_config()

    if args.comando == "link-shopee":
        url = args.url or input("Cole o link da Shopee (produto, loja ou pagina de cupons): ").strip()
        id_af = str(cfg.get("shopee", {}).get("id_afiliado", ""))
        url = url.split("?")[0] if "/m/" in url else url  # paginas de campanha: tira rastros de outros
        origem = input("Onde vai postar? (whatsapp/instagram/telegram) [whatsapp]: ").strip() or "whatsapp"
        print()
        print("Seu link de afiliada (copie e use):")
        print(link_shopee_afiliado(url, id_af, origem))
        return

    if args.comando == "link-amazon":
        url = args.url or input("Cole o link da Amazon: ").strip()
        print(converter_link(cfg, url) or "Nao achei o codigo do produto (ASIN) nesse link.")
        return

    if args.comando == "link-renner":
        link = link_awin(cfg, args.url)
        print(link or "Preencha id_publisher e id_anunciante_renner na secao [awin] do config.toml.")
        return

    if args.comando == "postar":
        if not args.loja:  # modo perguntas (usado pelo postar-oferta.bat)
            print("\n=== Postar uma oferta no canal ===")
            args.loja = input("Loja (shein / shopee / renner / mercadolivre): ").strip() or "shein"
            args.nome = input("Nome do produto: ").strip()
            args.preco = input("Preço AGORA (ex.: 59,90): ").strip()
            args.de = input("Preço ANTES (ex.: 99,90 — deixe vazio se não souber): ").strip() or None
            args.link = input("Seu link de afiliado: ").strip()
            args.imagem = input("Link da foto (opcional, Enter para pular): ").strip() or None
        if not (args.nome and args.preco and args.link and parse_preco(args.preco)):
            raise SystemExit("Faltou nome, preço ou link (ou o preço está em formato estranho).")
        loja = args.loja.lower().replace(" ", "")
        link = args.link
        if loja == "renner" and "awin1.com" not in link:
            link = link_awin(cfg, link) or link
        if loja == "amazon":
            link = converter_link(cfg, link) or link
        oferta = Oferta(
            loja=loja, nome=args.nome, preco=parse_preco(args.preco),
            preco_antigo=parse_preco(args.de), link=link, imagem=args.imagem,
            cupom=args.cupom or cfg.get("cupons", {}).get(loja), chamada=args.chamada,
        )
        texto = montar_mensagem(oferta, cfg)
        Telegram(cfg, True).enviar(texto, oferta.imagem)  # mostra antes
        if args.so_mostrar:
            return
        if args.confirmar or input("Postar no canal? (s/n): ").strip().lower().startswith("s"):
            Telegram(cfg, False).enviar(texto, oferta.imagem)
            log.info("Oferta postada.")
        else:
            print("Cancelado, nada foi postado.")
        return

    if args.comando == "testar-telegram":
        Telegram(cfg, False).enviar("✅ Robô de Achadinhos conectado ao canal!")
        log.info("Mensagem de teste enviada. Confira o canal.")
        return

    modo_teste = args.comando == "teste"
    tg = Telegram(cfg, modo_teste)
    if args.comando in ("teste", "uma-vez"):
        rodada(cfg, tg, modo_teste)
        return

    minutos = int(cfg.get("agenda", {}).get("verificar_a_cada_minutos", 60))
    log.info(f"Robo ligado. Verificando a cada {minutos} minutos. (Ctrl+C para parar)")
    while True:
        try:
            rodada(cfg, tg, False)
        except Exception as erro:
            log.info(f"Erro inesperado na rodada (tento de novo depois): {erro}")
        time.sleep(minutos * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nRobo desligado.")
