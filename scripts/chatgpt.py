"""VibeClipper + ChatGPT (API da OpenAI): título, descrição e tags pra postar, e a thumb, tudo a partir do corte.

Usa a chave OPENAI_API_KEY das Configurações (.env). É a API paga por uso da OpenAI (platform.openai.com), separada do
ChatGPT Plus/grátis. O editor (aba ChatGPT) chama isto; dá pra usar na mão também:

  python scripts/chatgpt.py textos trabalho/<slug> <n> ["deixa o título mais polêmico"]
  python scripts/chatgpt.py thumb  trabalho/<slug> <n> ["fundo vermelho, eu surpreso"] [--ref foto.png ...]
  python scripts/chatgpt.py modelos

Tudo de cada corte fica em trabalho/<slug>/corte-N/chatgpt.json (histórico de pedidos e respostas, o resultado atual e
as thumbs) e os arquivos (referências, prints e thumbs) em trabalho/<slug>/corte-N/chatgpt/.
Os modelos: os das Configurações (OPENAI_MODELO_TEXTO / OPENAI_MODELO_IMAGEM) ou, se vazios, o melhor que a chave tem.
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import uuid

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
# preferência quando a pessoa não escolheu: do mais novo/capaz pro mais antigo (o 1º que a chave tiver)
PREF_TEXTO = ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-6.1-sol", "gpt-5.6-luna", "gpt-5.5", "gpt-5.2", "gpt-5.1", "gpt-5",
              "gpt-5-mini", "gpt-4.1", "gpt-4o"]
PREF_IMAGEM = ["gpt-image-2", "gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini"]
TAMANHOS = {"vertical": "1024x1536", "horizontal": "1536x1024", "quadrada": "1024x1024"}
_cache = {}


class Erro(Exception):
    pass


def chave():
    import config
    k = config.ler().get("OPENAI_API_KEY")
    if not k:
        raise Erro("falta a chave da API da OpenAI: coloque em Configurações")
    return k


def _req(metodo, caminho, k=None, timeout=180, **kw):
    import requests
    try:
        r = requests.request(metodo, BASE + caminho, headers={"Authorization": f"Bearer {k or chave()}"},
                             timeout=timeout, **kw)
    except requests.RequestException as e:
        raise Erro(f"sem conexão com a OpenAI ({e.__class__.__name__}): confira a internet")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message") or r.text
        except ValueError:
            msg = r.text
        if r.status_code == 401:
            raise Erro("a chave da OpenAI foi recusada: confira em Configurações")
        if r.status_code == 429:
            raise Erro("a OpenAI recusou por limite ou falta de crédito: veja o saldo em platform.openai.com "
                       f"(Billing). Detalhe: {msg[:200]}")
        raise Erro(f"a OpenAI deu erro {r.status_code}: {msg[:400]}")
    return r.json()


def modelos(k=None):
    """Os modelos que a chave enxerga (guardado por 10 minutos)."""
    k = k or chave()
    c = _cache.get(k)
    if c and time.time() - c[0] < 600:
        return c[1]
    ids = sorted(m["id"] for m in _req("GET", "/models", k, timeout=30).get("data", []))
    _cache[k] = (time.time(), ids)
    return ids


def _de_texto(m):
    return m.startswith(("gpt-", "o")) and not re.search(
        r"image|audio|realtime|tts|transcribe|search|embedding|moderation|instruct|codex|dall-e|whisper|babbage|davinci", m)


def separar(ids):
    return {"texto": [m for m in ids if _de_texto(m)], "imagem": [m for m in ids if "image" in m]}


def modelo(tipo):
    import config
    escolhido = config.ler().get("OPENAI_MODELO_TEXTO" if tipo == "texto" else "OPENAI_MODELO_IMAGEM")
    if escolhido:
        return escolhido
    ids = modelos()
    for m in (PREF_TEXTO if tipo == "texto" else PREF_IMAGEM):
        if m in ids:
            return m
    cand = separar(ids)[tipo]
    if not cand:
        raise Erro(f"a sua chave não tem nenhum modelo de {tipo} da OpenAI")
    return sorted(cand)[-1]


def testar(k):
    ids = modelos(k)
    s = separar(ids)
    return {"ok": True, "texto": s["texto"], "imagem": s["imagem"],
            "msg": f"chave ok: {len(s['texto'])} modelos de texto e {len(s['imagem'])} de imagem"}


# ---------- dados do corte ----------
def pasta(T, n):
    p = os.path.join(T, f"corte-{n}", "chatgpt")
    os.makedirs(p, exist_ok=True)
    return p


def arq_hist(T, n):
    return os.path.join(T, f"corte-{n}", "chatgpt.json")


def historico(T, n):
    try:
        return json.load(open(arq_hist(T, n), encoding="utf-8"))
    except (OSError, ValueError):
        return {"mensagens": [], "resultado": None, "thumbs": [], "refs": []}


def gravar(T, n, h):
    tmp = arq_hist(T, n) + ".tmp"
    json.dump(h, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, arq_hist(T, n))


def contexto(T, n):
    """O que o ChatGPT precisa saber do corte: título do vídeo, título/legenda/hook do corte e o que é falado nele."""
    cs = json.load(open(os.path.join(T, "cortes.json"), encoding="utf-8"))["cortes"]
    c = next(x for x in cs if int(x["n"]) == int(n))
    info = {}
    if os.path.exists(os.path.join(T, "info.json")):
        info = json.load(open(os.path.join(T, "info.json"), encoding="utf-8"))
    fala = " ".join(w["p"] for w in c.get("palavras", []))
    ed = os.path.join(T, f"corte-{n}", "edicao.json")          # se a pessoa editou a legenda, vale o texto editado
    if os.path.exists(ed):
        grupos = (json.load(open(ed, encoding="utf-8")).get("legenda") or {}).get("grupos") or []
        if grupos:
            fala = " ".join(g.get("texto", "") for g in grupos)
    return {"video": info.get("title") or os.path.basename(T), "canal": info.get("channel") or "",
            "titulo": c.get("titulo", ""), "legenda": c.get("legenda", ""), "hook": c.get("hook", ""),
            "dur": c.get("dur"), "fala": fala[:6000]}


def sem_markdown(s):
    s = re.sub(r"\*+|__+|`+|^#+\s*", "", str(s or ""), flags=re.M)
    return re.sub(r"[ \t]+\n", "\n", s).strip()


def _limpa_tags(tags):
    if isinstance(tags, str):
        tags = re.split(r"[,\n]", tags)
    out = []
    for t in tags or []:
        t = re.sub(r"\s+", " ", sem_markdown(t).lstrip("#")).strip()
        if t and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out[:20]


SISTEMA_TEXTOS = (
    "Você é especialista em vídeos curtos (YouTube Shorts, Instagram Reels, TikTok) no Brasil. A partir do que é falado "
    "num corte, você escreve o que a pessoa vai usar pra postar. Responda SÓ com um JSON assim: "
    '{"titulo": "...", "descricao": "...", "tags": ["...", "..."]}.\n'
    "- titulo: até 70 caracteres, forte e curioso, fiel ao que acontece no corte, sem hashtag, sem emoji no começo.\n"
    "- descricao: 2 a 4 frases curtas que contam do que se trata e chamam pra assistir/comentar/seguir. Texto puro: "
    "NUNCA use markdown, asterisco (*, **, ***), sublinhado, # ou listas. Pode quebrar linha. Sem hashtag (elas vão nas tags).\n"
    "- tags: 10 a 15 palavras-chave/hashtags relevantes, sem o #, em minúsculas, do mais específico pro mais geral.\n"
    "Tudo em português do Brasil. Se a pessoa pedir ajustes, refaça o JSON inteiro aplicando o pedido.")


def gerar_textos(T, n, pedido=""):
    h = historico(T, n)
    ctx = contexto(T, n)
    base = (f"Vídeo original: {ctx['video']}" + (f" (canal {ctx['canal']})" if ctx["canal"] else "") +
            f"\nTítulo de trabalho do corte: {ctx['titulo']}\nDuração: {ctx['dur']} s\nHook (começo): {ctx['hook']}\n"
            f"\nO que é falado no corte:\n{ctx['fala']}")
    msgs = [{"role": "system", "content": SISTEMA_TEXTOS}, {"role": "user", "content": base}]
    for m in h["mensagens"]:                                   # a conversa anterior sobre os textos (pra refinar)
        if m.get("tipo") != "textos":
            continue
        if m["papel"] == "voce" and m.get("texto"):
            msgs.append({"role": "user", "content": m["texto"]})
        elif m["papel"] == "chatgpt" and m.get("resultado"):
            msgs.append({"role": "assistant", "content": json.dumps(m["resultado"], ensure_ascii=False)})
    pedido = (pedido or "").strip()
    msgs.append({"role": "user", "content": pedido or "Gere o título, a descrição e as tags desse corte."})
    mod = modelo("texto")
    corpo = {"model": mod, "messages": msgs, "response_format": {"type": "json_object"}}
    d = _req("POST", "/chat/completions", json=corpo)
    txt = d["choices"][0]["message"].get("content") or "{}"
    try:
        r = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except ValueError:
        raise Erro("o ChatGPT não devolveu o JSON esperado; tente de novo")
    res = {"titulo": sem_markdown(r.get("titulo")), "descricao": sem_markdown(r.get("descricao")),
           "tags": _limpa_tags(r.get("tags"))}
    agora = time.time()
    h["mensagens"].append({"papel": "voce", "tipo": "textos", "texto": pedido or "Gerar título, descrição e tags",
                           "t": agora})
    h["mensagens"].append({"papel": "chatgpt", "tipo": "textos", "resultado": res, "modelo": mod, "t": time.time()})
    h["resultado"] = res
    gravar(T, n, h)
    return h


def salvar_ref(T, n, nome, dados):
    ext = os.path.splitext(nome)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise Erro("mande uma imagem png, jpg ou webp")
    arq = f"ref-{uuid.uuid4().hex[:8]}{'.jpg' if ext == '.jpeg' else ext}"
    open(os.path.join(pasta(T, n), arq), "wb").write(dados)
    h = historico(T, n)
    h.setdefault("refs", []).append({"arquivo": arq, "nome": os.path.basename(nome)[:80], "t": time.time()})
    gravar(T, n, h)
    return h


def print_momento(T, n, t):
    """Um quadro do corte (16:9 na resolução máxima) no tempo t (s, tempo do corte)."""
    arq = f"print-{uuid.uuid4().hex[:8]}.jpg"
    fonte = os.path.join(T, f"corte-{n}", "fonte.mp4")
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{max(0.0, float(t)):.3f}", "-i", fonte,
                        "-frames:v", "1", "-q:v", "2", os.path.join(pasta(T, n), arq)], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(os.path.join(pasta(T, n), arq)):
        raise Erro("não consegui tirar o print: " + (r.stderr or "")[-200:])
    h = historico(T, n)
    h.setdefault("refs", []).append({"arquivo": arq, "nome": f"print em {float(t):.1f} s", "t": time.time(), "print": True})
    gravar(T, n, h)
    return h


def tirar_ref(T, n, arq):
    h = historico(T, n)
    h["refs"] = [r for r in h.get("refs", []) if r["arquivo"] != arq]
    gravar(T, n, h)
    return h


def gerar_thumb(T, n, pedido="", refs=(), formato="vertical", qualidade="medium", base_ultima=False):
    h = historico(T, n)
    ctx = contexto(T, n)
    tit = (h.get("resultado") or {}).get("titulo") or ctx["titulo"]
    tamanho = TAMANHOS.get(formato, TAMANHOS["vertical"])
    arqs = [os.path.join(pasta(T, n), os.path.basename(a)) for a in refs]
    arqs = [a for a in arqs if os.path.isfile(a)]
    if base_ultima and h.get("thumbs"):
        ult = os.path.join(pasta(T, n), h["thumbs"][-1]["arquivo"])
        if os.path.isfile(ult):
            arqs.insert(0, ult)
    anteriores = [m["texto"] for m in h["mensagens"] if m.get("tipo") == "thumb" and m["papel"] == "voce" and m.get("texto")]
    prompt = (
        f"Thumbnail {'vertical (9:16)' if formato == 'vertical' else 'horizontal (16:9)' if formato == 'horizontal' else 'quadrada'} "
        "pra um vídeo curto (Shorts/Reels/TikTok), chamativa, com cara de miniatura profissional do YouTube: contraste "
        "alto, cores fortes, um foco claro, expressão marcante. Se fizer sentido, um texto curto e grande (no máximo 4 "
        "palavras) em português, bem legível, sem erro de ortografia.\n"
        f"Título do vídeo: {tit}\nDo que se trata: {ctx['hook']} {ctx['fala'][:700]}\n"
        + (("Imagens de referência anexadas: use a(s) pessoa(s), rostos, roupas, personagens e cenário delas, mantendo "
            "a aparência fiel." + (" A 1ª imagem é a thumb anterior: parta dela e aplique só o que foi pedido." if
                                     base_ultima and h.get("thumbs") else "") + "\n") if arqs else "")
        + (f"Pedidos anteriores da pessoa (já aplicados): {' | '.join(anteriores[-3:])}\n" if anteriores and base_ultima else "")
        + (f"PEDIDO DA PESSOA (o mais importante): {pedido.strip()}\n" if pedido and pedido.strip() else ""))
    mod = modelo("imagem")
    q = qualidade if qualidade in ("low", "medium", "high") else "medium"
    if arqs:
        files = [("image[]", (os.path.basename(a), open(a, "rb"),
                              "image/png" if a.endswith(".png") else "image/webp" if a.endswith(".webp") else "image/jpeg"))
                 for a in arqs[:10]]
        d = _req("POST", "/images/edits", timeout=300, files=files,
                 data={"model": mod, "prompt": prompt, "size": tamanho, "quality": q, "n": "1"})
    else:
        d = _req("POST", "/images/generations", timeout=300,
                 json={"model": mod, "prompt": prompt, "size": tamanho, "quality": q, "n": 1})
    item = (d.get("data") or [{}])[0]
    if item.get("b64_json"):
        img = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        import requests
        img = requests.get(item["url"], timeout=120).content
    else:
        raise Erro("a OpenAI não devolveu a imagem")
    arq = f"thumb-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}.png"
    open(os.path.join(pasta(T, n), arq), "wb").write(img)
    agora = time.time()
    h = historico(T, n)
    h["mensagens"].append({"papel": "voce", "tipo": "thumb", "texto": (pedido or "").strip() or "Criar thumb",
                           "refs": [os.path.basename(a) for a in arqs], "formato": formato, "qualidade": q, "t": agora})
    h["mensagens"].append({"papel": "chatgpt", "tipo": "thumb", "arquivo": arq, "modelo": mod, "t": time.time()})
    h.setdefault("thumbs", []).append({"arquivo": arq, "formato": formato, "t": time.time()})
    gravar(T, n, h)
    return h


def main():
    import argparse
    import comum
    comum.preparar()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("acao", choices=["textos", "thumb", "modelos"])
    ap.add_argument("pasta", nargs="?")
    ap.add_argument("n", nargs="?", type=int)
    ap.add_argument("pedido", nargs="?", default="")
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--formato", default="vertical", choices=list(TAMANHOS))
    ap.add_argument("--qualidade", default="medium", choices=["low", "medium", "high"])
    a = ap.parse_args()
    try:
        if a.acao == "modelos":
            print(json.dumps({**separar(modelos()), "escolhidos": {"texto": modelo("texto"), "imagem": modelo("imagem")}},
                             ensure_ascii=False, indent=1))
        elif a.acao == "textos":
            r = gerar_textos(a.pasta, a.n, a.pedido)["resultado"]
            print(f"TÍTULO: {r['titulo']}\n\nDESCRIÇÃO:\n{r['descricao']}\n\nTAGS: {', '.join(r['tags'])}")
        else:
            refs = []
            for f in a.ref:
                refs.append(salvar_ref(a.pasta, a.n, f, open(f, "rb").read())["refs"][-1]["arquivo"])
            h = gerar_thumb(a.pasta, a.n, a.pedido, refs, a.formato, a.qualidade)
            print("thumb:", os.path.join(pasta(a.pasta, a.n), h["thumbs"][-1]["arquivo"]))
    except Erro as e:
        raise SystemExit(f"[chatgpt] {e}")


if __name__ == "__main__":
    main()
