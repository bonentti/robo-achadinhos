"""Avisa a Nicole no Telegram (privado) quando um produto da Amazon fica abaixo de um preço.

Uso: python alerta_preco.py ASIN PRECO_LIMITE "Nome curto" [PRECO_ATUAL]
(PRECO_ATUAL opcional: usa esse valor em vez de ler a página com curl.)
Sai com código 0 e imprime o preço encontrado; só manda mensagem se o preço estiver abaixo do limite.
"""
import re, subprocess, sys, time
from robo import carregar_config, Telegram, link_amazon, reais

asin, limite, nome = sys.argv[1], float(sys.argv[2]), sys.argv[3]
url = f"https://www.amazon.com.br/dp/{asin}"
if len(sys.argv) > 4:  # preço já lido de fora (ex.: pelo Chrome, quando a Amazon bloqueia o curl)
    preco = float(sys.argv[4])
else:
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    pg = ""
    for _ in range(3):  # a Amazon às vezes devolve página de bloqueio; o curl passa melhor que o requests
        pg = subprocess.run(["curl", "-s", "-A", UA, "-H", "Accept-Language: pt-BR", url],
                            capture_output=True, timeout=60).stdout.decode("utf-8", "ignore")
        if "a-offscreen" in pg:
            break
        time.sleep(5)
    m = re.search(r'class="a-price[^"]*"[^>]*>\s*<span class="a-offscreen">R\$\s*([\d.]+,\d{2})', pg) \
        or re.search(r'a-offscreen">R\$\s*([\d.]+,\d{2})', pg)
    if not m:
        print("SEM_PRECO (Amazon pode ter bloqueado a leitura)")
        sys.exit(0)
    preco = float(m.group(1).replace(".", "").replace(",", "."))
print(f"{nome}: {reais(preco)} (limite {reais(limite)})")
if preco < limite:
    cfg = carregar_config()
    dona = cfg["resumo_whatsapp"]["chat_id"]
    Telegram(cfg, False).enviar_privado(dona, (
        f"🔔 Baixou! {nome}\n"
        f"Agora: *{reais(preco)}* (você queria abaixo de {reais(limite)})\n"
        f"🛒 {link_amazon(cfg, url)}"))
    print("AVISO_ENVIADO")
