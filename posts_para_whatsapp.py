"""
Pega os posts de HOJE do canal do Telegram e manda no seu privado, um por um,
com imagem pronta e legenda no formato do WhatsApp (para encaminhar).

Uso:  python posts_para_whatsapp.py
"""
import html
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests

import robo
from cartao import gerar_cartao

BR = timezone(timedelta(hours=-3))
ID_AFILIADA = "18326310376"


def ler_posts_de_hoje(canal: str) -> list[dict]:
    t = requests.get(f"https://t.me/s/{canal.lstrip('@')}", headers={"User-Agent": robo.NAVEGADOR}, timeout=30).text
    posts = []
    for b in re.split(r'<div class="tgme_widget_message_wrap', t)[1:]:
        q = re.search(r'datetime="([^"]+)"', b)
        corpo = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
        if not (q and corpo):
            continue
        quando = datetime.fromisoformat(q.group(1)).astimezone(BR)
        if quando.date() != datetime.now(BR).date():
            continue
        links = re.findall(r'href="(https?://[^"]+)"', corpo.group(1))
        foto = re.search(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", b)
        texto = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", corpo.group(1))))
        posts.append({"quando": quando, "texto": texto, "links": links, "foto": foto.group(1) if foto else None})
    return posts


def consertar_link(link: str) -> str:
    """Links antigos ficaram com '&amp;' (perdiam o ID de afiliada). Refaz o link certo, marcado como whatsapp."""
    for _ in range(4):
        if "&amp;" not in link:
            break
        link = html.unescape(link)
    if "an_redir" in link:
        q = parse_qs(urlparse(link).query)
        origem = (q.get("origin_link") or [""])[0]
        if origem:
            return robo.link_shopee_afiliado(origem, ID_AFILIADA, "whatsapp")
    return link.replace("sub_id=telegram", "sub_id=whatsapp")


def para_whatsapp(post: dict) -> tuple[bytes | None, str]:
    """Converte um post do canal em (imagem, legenda do WhatsApp)."""
    texto = post["texto"]
    link = consertar_link(next((l for l in post["links"] if "t.me/" not in l), ""))
    nome = re.search(r"🛍️\s*(.+)", texto)
    por = re.search(r"Por R\$\s*([\d.,]+)", texto) or re.search(r"💰\s*R\$\s*([\d.,]+)", texto)
    if nome and por:  # post de produto
        de = re.search(r"De R\$\s*([\d.,]+)", texto)
        cupom = re.search(r"Cupom:\s*([A-Z0-9]+)", texto)
        loja = re.search(r"🏬\s*(.+)", texto)
        oficial = re.search(r"Loja oficial:\s*(.+)", texto)
        nome_loja = loja.group(1).strip() if loja else "Shopee"
        o = robo.Oferta(loja=nome_loja.lower().replace(" ", ""), nome=nome.group(1).strip(),
                        preco=robo.parse_preco(por.group(1)),
                        preco_antigo=robo.parse_preco(de.group(1)) if de else None, link=link,
                        cupom=cupom.group(1) if cupom else None)
        imagem = gerar_cartao(o.nome, o.preco, o.preco_antigo, nome_loja, post["foto"], o.cupom,
                              "Achados da @bonentti")
        legenda = robo.legenda_whatsapp(o)
        if oficial:
            legenda = legenda.replace("\n\n🛒", f"\n✅ Loja oficial: {oficial.group(1).strip()}\n\n🛒", 1)
        return imagem, legenda
    # Aviso (cupom / dia promocional): titulo em *negrito* e link visivel e consertado
    linhas = texto.splitlines()
    if linhas:
        linhas[0] = f"*{linhas[0].strip()}*"
    legenda = "\n".join(linhas)
    legenda = re.sub(r"https://shope\.ee/\S+", lambda m: consertar_link(m.group(0)), legenda)
    if link and link not in legenda:
        legenda += f"\n\n👉 {link}"
    return None, legenda


def main():
    cfg = robo.carregar_config()
    chat = str(cfg.get("resumo_whatsapp", {}).get("chat_id", ""))
    if not robo.preenchido(chat):
        sys.exit("Falta o chat_id em [resumo_whatsapp] no config.toml.")
    posts = ler_posts_de_hoje(cfg["telegram"]["canal"])
    if not posts:
        print("Nenhum post hoje ainda.")
        return
    tg = robo.Telegram(cfg, False)
    tg.enviar_privado(chat, f"📲 {len(posts)} post(s) de hoje no formato do WhatsApp. Encaminhe cada um:")
    for p in sorted(posts, key=lambda p: p["quando"]):
        imagem, legenda = para_whatsapp(p)
        if imagem:
            tg.enviar_foto_privado(chat, imagem, legenda)
        else:
            tg.enviar_privado(chat, legenda)
        time.sleep(1)
    print(f"Enviados {len(posts)} post(s) no seu privado.")


if __name__ == "__main__":
    main()
