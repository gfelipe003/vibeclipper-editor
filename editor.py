"""VibeClipper: o sistema no navegador (página inicial + editor), rodando só no seu computador.

  python editor.py                     # ou dois cliques no "Abrir editor.bat"
  python editor.py meu-video --porta 8765

Abre http://127.0.0.1:8765 no navegador:
  - PÁGINA INICIAL: cola um link do YouTube ou manda um arquivo de vídeo e o sistema faz os cortes sozinho
    (scripts/automatico.py; a escolha dos momentos usa a sua chave da API do Claude ou o Claude Code logado);
  - CONFIGURAÇÕES (/config): a chave da API do Claude, a da ElevenLabs (opcional) e o modelo padrão, guardadas no .env;
  - EDITOR (/editar): câmera por pontos-chave e zoom rápido, legenda, facecam (fixa ou animada no tempo, "facecam grande
    aqui"), mídia por cima (imagens, textos) e áudio extra, cortar trechos, renderizar e baixar.
Só escuta em 127.0.0.1 (nada sai do seu computador).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(AQUI, "scripts"))
TRAB = os.path.join(AQUI, "trabalho")
SAIDA = os.path.join(AQUI, "saida")
PAGINA = os.path.join(AQUI, "editor", "index.html")
INICIO = os.path.join(AQUI, "editor", "inicio.html")
CONFIG = os.path.join(AQUI, "editor", "config.html")
TAREFAS = os.path.join(TRAB, "_tarefas")
UPLOADS = os.path.join(TRAB, "_uploads")
SLUG = re.compile(r"^[A-Za-z0-9_.-]+$")
TIPOS = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".mkv": "video/x-matroska",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
         ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".aac": "audio/aac",
         ".ttf": "font/ttf", ".otf": "font/otf"}

estados = {}            # (slug, n) -> {"estado": "renderizando"|"pronto"|"erro", "msg": str, "ini": t}
previas = {}            # caminho -> threading.Event (prévia em geração)
trava = threading.Lock()
fila = []               # tarefas automáticas esperando (link ou arquivo -> cortes prontos), uma por vez
fila_cv = threading.Condition()
rodando = {}            # id -> Popen


PRESETS = os.path.join(AQUI, "presets")                 # presets de mídia (valem pra todos os vídeos)
PRESETS_ARQ = os.path.join(PRESETS, "presets.json")
PRESETS_MIDIAS = os.path.join(PRESETS, "arquivos")      # cópias das imagens e áudios usados nos presets


def presets_ler():
    try:
        return json.load(open(PRESETS_ARQ, encoding="utf-8")).get("presets", [])
    except (OSError, ValueError):
        return []


def presets_gravar(lista):
    os.makedirs(PRESETS, exist_ok=True)
    tmp = PRESETS_ARQ + ".tmp"
    json.dump({"presets": lista}, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, PRESETS_ARQ)


def preset_salvar(d):
    """Guarda um ou mais itens de mídia (imagem, texto, áudio) com os tempos contados a partir do 1º item."""
    import shutil
    import uuid
    nome = re.sub(r"\s+", " ", str(d.get("nome") or "")).strip()[:60]
    itens = [dict(m) for m in (d.get("itens") or []) if m.get("tipo") in ("imagem", "texto", "audio")]
    if not nome or not itens:
        raise ValueError("dê um nome e escolha pelo menos uma mídia")
    T = pasta_T(d.get("slug") or "")
    base = min(float(m.get("t", 0) if m["tipo"] == "audio" else m.get("ini", 0)) for m in itens)
    os.makedirs(PRESETS_MIDIAS, exist_ok=True)
    for m in itens:
        m.pop("id", None)
        if m["tipo"] == "audio":
            m["t"] = round(float(m.get("t", 0)) - base, 3)
        else:
            m["ini"], m["fim"] = round(float(m["ini"]) - base, 3), round(float(m["fim"]) - base, 3)
        if m["tipo"] in ("imagem", "audio"):
            src = os.path.join(T, "midias", os.path.basename(m.get("arquivo", "")))
            if not os.path.isfile(src):
                raise ValueError(f"arquivo não encontrado: {m.get('arquivo')}")
            novo = uuid.uuid4().hex[:6] + "_" + os.path.basename(src)
            shutil.copyfile(src, os.path.join(PRESETS_MIDIAS, novo))
            m["arquivo"] = novo
    p = {"id": uuid.uuid4().hex[:10], "nome": nome, "criado": time.time(), "itens": itens}
    lista = presets_ler()
    lista.append(p)
    presets_gravar(lista)
    return p


def preset_aplicar(pid, slug):
    """Copia os arquivos do preset pra pasta de mídia do vídeo e devolve os itens (tempos relativos)."""
    import shutil
    p = next((x for x in presets_ler() if x["id"] == pid), None)
    if not p:
        raise FileNotFoundError("preset não encontrado")
    pasta = os.path.join(pasta_T(slug), "midias")
    os.makedirs(pasta, exist_ok=True)
    itens = []
    for m in p["itens"]:
        m = dict(m)
        if m["tipo"] in ("imagem", "audio"):
            src = os.path.join(PRESETS_MIDIAS, os.path.basename(m["arquivo"]))
            dst = os.path.join(pasta, os.path.basename(m["arquivo"]))
            if not os.path.exists(dst):
                shutil.copyfile(src, dst)
        itens.append(m)
    return itens


def preset_apagar(pid):
    lista = presets_ler()
    fora = [x for x in lista if x["id"] == pid]
    lista = [x for x in lista if x["id"] != pid]
    presets_gravar(lista)
    usados = {m.get("arquivo") for x in lista for m in x["itens"]}
    for x in fora:
        for m in x["itens"]:
            a = m.get("arquivo")
            if a and a not in usados and os.path.isfile(os.path.join(PRESETS_MIDIAS, os.path.basename(a))):
                os.remove(os.path.join(PRESETS_MIDIAS, os.path.basename(a)))


def nome_seguro(nome, padrao="arquivo"):
    base, ext = os.path.splitext(os.path.basename(nome or padrao))
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-")[:60] or padrao
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext.lower())[:6]
    return base + ext


def gravar_corpo(handler, destino):
    """Grava o corpo do POST direto no disco (vídeos grandes, sem segurar na memória)."""
    falta = int(handler.headers.get("Content-Length", 0) or 0)
    if falta <= 0:
        raise ValueError("arquivo vazio")
    tmp = destino + ".parcial"
    with open(tmp, "wb") as f:
        while falta > 0:
            b = handler.rfile.read(min(4 << 20, falta))
            if not b:
                break
            f.write(b)
            falta -= len(b)
    if falta > 0:
        os.remove(tmp)
        raise ValueError("o envio foi interrompido")
    os.replace(tmp, destino)
    return destino


def info_midia(arq):
    ext = os.path.splitext(arq)[1].lower()
    out = {"arquivo": os.path.basename(arq)}
    if TIPOS.get(ext, "").startswith("image/"):
        from PIL import Image
        with Image.open(arq) as im:
            out.update(tipo="imagem", w=im.width, h=im.height)
    else:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", arq],
                           capture_output=True, text=True)
        out.update(tipo="audio", dur=round(float(r.stdout.strip() or 0), 2))
    return out


def tarefas():
    out = []
    if os.path.isdir(TAREFAS):
        for f in os.listdir(TAREFAS):
            if f.endswith(".json") and not f.startswith("_"):
                d = ler(os.path.join(TAREFAS, f))
                if d:
                    out.append(d)
    out.sort(key=lambda d: d.get("inicio", 0), reverse=True)
    return out[:30]


def modelo_tarefa(pedido):
    """O modelo que a pessoa escolheu na página, ou o padrão das Configurações."""
    import config
    m = pedido or config.ler().get("VIBECLIPPER_MODELO", "opus")
    return m if m in ("opus", "sonnet") else "opus"


def nova_tarefa(dados):
    import uuid
    tid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    url = (dados.get("url") or "").strip()
    up = dados.get("upload")
    mais = (dados.get("mais") or "").strip()
    if mais:
        T = pasta_T(mais)                                                  # valida o nome
        for d in tarefas():
            if d.get("slug") == mais and d.get("estado") in ("rodando", "na fila"):
                raise ValueError("esse vídeo já está sendo processado; espere terminar")
        args = ["--mais", mais]
        origem = (ler(os.path.join(T, "info.json"), {}) or {}).get("title") or mais
    elif url:
        if not re.match(r"^https?://", url):
            raise ValueError("cole um link que comece com http:// ou https://")
        args = ["--url", url]
        origem = url
    elif up:
        arq = os.path.join(UPLOADS, os.path.basename(up))
        if not os.path.isfile(arq):
            raise ValueError("o arquivo enviado não foi encontrado")
        args = ["--arquivo", arq, "--mover"]
        origem = re.sub(r"^[0-9a-f]{8}_", "", os.path.basename(up))
    else:
        raise ValueError("mande um link ou um arquivo de vídeo")
    gp = dados.get("gameplay", "auto")
    args += ["--gameplay", gp if gp in ("auto", "sim", "nao") else "auto",
             "--cortes", str(max(0, min(12, int(dados.get("cortes") or 0)))),
             "--idioma", re.sub(r"[^a-z]", "", str(dados.get("idioma") or "pt"))[:5] or "pt",
             "--modelo", modelo_tarefa(dados.get("modelo")), "--tarefa", tid]
    orientacao = re.sub(r"\s+", " ", str(dados.get("orientacao") or "")).strip()[:2000]
    if orientacao:
        args += ["--orientacao", orientacao]
    if dados.get("renderizar") in (True, "sim"):
        args += ["--renderizar"]
    os.makedirs(TAREFAS, exist_ok=True)
    d = {"id": tid, "origem": origem, "estado": "na fila", "etapa": 0, "msg": "esperando a vez", "progresso": 0,
         "inicio": time.time(), "slug": mais or None, "titulo": ("Mais cortes: " + origem) if mais else None}
    json.dump(d, open(os.path.join(TAREFAS, f"{tid}.json"), "w", encoding="utf-8"), ensure_ascii=False)
    with fila_cv:
        fila.append((tid, args))
        fila_cv.notify()
    return d


def trabalhador():
    """Roda as tarefas automáticas uma por vez (cada uma usa a placa de vídeo inteira)."""
    while True:
        with fila_cv:
            while not fila:
                fila_cv.wait()
            tid, args = fila.pop(0)
        st = ler(os.path.join(TAREFAS, f"{tid}.json"), {}) or {}
        if st.get("estado") == "cancelado":
            continue
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        p = subprocess.Popen([sys.executable, os.path.join(AQUI, "scripts", "automatico.py"), *args], cwd=AQUI, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rodando[tid] = p
        p.wait()
        rodando.pop(tid, None)
        st = ler(os.path.join(TAREFAS, f"{tid}.json"), {}) or {}
        if st.get("estado") in ("rodando", "na fila"):                     # morreu sem dizer
            st.update(estado="cancelado" if p.returncode in (1, -9, 15, -15) and st.get("cancelar") else "erro",
                      erro=st.get("erro") or f"o processo parou (código {p.returncode})", fim=time.time())
            json.dump(st, open(os.path.join(TAREFAS, f"{tid}.json"), "w", encoding="utf-8"), ensure_ascii=False)


def testar_chave(d):
    """Confere a chave da API do Claude sem gastar nada (consulta o modelo, não gera texto)."""
    import anthropic
    import config
    chave = (d.get("chave") or "").strip() or config.ler().get("ANTHROPIC_API_KEY")
    if not chave:
        return {"ok": False, "msg": "nenhuma chave pra testar"}
    mid = config.MODELOS_API.get(d.get("modelo") or config.ler().get("VIBECLIPPER_MODELO"), config.MODELOS_API["opus"])
    try:
        m = anthropic.Anthropic(api_key=chave, max_retries=1, timeout=20).models.retrieve(mid)
        return {"ok": True, "msg": f"chave funcionando ✓ ({m.display_name})"}
    except anthropic.AuthenticationError:
        return {"ok": False, "msg": "chave recusada: confira se copiou inteira"}
    except anthropic.PermissionDeniedError:
        return {"ok": False, "msg": "a chave não tem acesso a esse modelo"}
    except anthropic.NotFoundError:
        return {"ok": False, "msg": "modelo não encontrado pra essa chave"}
    except anthropic.APIConnectionError:
        return {"ok": False, "msg": "sem conexão com a API do Claude"}
    except anthropic.APIStatusError as e:
        return {"ok": False, "msg": f"erro {e.status_code}: {e.message}"}


def tamanho_pasta(p):
    total = 0
    for raiz, _, arqs in os.walk(p):
        for a in arqs:
            try:
                total += os.path.getsize(os.path.join(raiz, a))
            except OSError:
                pass
    return total


def apagar_projeto(slug):
    """Apaga um vídeo inteiro: trabalho/<slug> e saida/<slug> (e as tarefas já terminadas dele). Devolve os bytes liberados."""
    import shutil
    import stat
    if not SLUG.match(slug) or slug.startswith("_"):
        raise ValueError("nome de vídeo inválido")
    for d in tarefas():
        if d.get("slug") == slug and d.get("estado") in ("rodando", "na fila"):
            raise ValueError("esse vídeo está sendo processado agora; cancele ou espere terminar")
    for (s, _n), st in list(estados.items()):
        if s == slug and st.get("estado") == "renderizando":
            raise ValueError("um corte desse vídeo está renderizando; espere terminar")
    falhas = []

    def tenta_de_novo(func, caminho, _exc):                    # arquivo só-leitura no Windows: libera e tenta de novo
        try:
            os.chmod(caminho, stat.S_IWRITE)
            func(caminho)
        except OSError:
            falhas.append(caminho)
    liberado = 0
    for base in (TRAB, SAIDA):
        p = os.path.join(base, slug)
        if os.path.isdir(p):
            liberado += tamanho_pasta(p)
            shutil.rmtree(p, onerror=tenta_de_novo)
    for f in os.listdir(TAREFAS) if os.path.isdir(TAREFAS) else []:
        if f.endswith(".json"):
            d = ler(os.path.join(TAREFAS, f), {}) or {}
            if d.get("slug") == slug:
                for ext in (".json", ".log"):
                    try:
                        os.remove(os.path.join(TAREFAS, f[:-5] + ext))
                    except OSError:
                        pass
    if falhas:
        raise ValueError(f"não consegui apagar {len(falhas)} arquivo(s) (talvez abertos em outro programa). "
                         "Feche o que estiver usando e tente de novo")
    return liberado


def lista_projetos():
    out = []
    for s in projetos():
        T = os.path.join(TRAB, s)
        info = ler(os.path.join(T, "info.json"), {}) or {}
        cs = (ler(os.path.join(T, "cortes.json"), {}) or {}).get("cortes", [])
        prontos = sum(os.path.exists(os.path.join(SAIDA, s, f"corte-{c['n']}.mp4")) for c in cs)
        out.append({"slug": s, "titulo": info.get("title") or s, "cortes": len(cs), "prontos": prontos,
                    "miniatura": os.path.exists(os.path.join(T, "miniatura.jpg")),
                    "tamanho": tamanho_pasta(T) + tamanho_pasta(os.path.join(SAIDA, s)),
                    "quando": os.path.getmtime(os.path.join(T, "cortes.json"))})
    return out


def pasta_T(slug):
    if not SLUG.match(slug):
        raise ValueError("nome de vídeo inválido")
    p = os.path.join(TRAB, slug)
    if not os.path.isfile(os.path.join(p, "cortes.json")):
        raise FileNotFoundError(slug)
    return p


def ler(p, padrao=None):
    if not os.path.exists(p):
        return padrao
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def projetos():
    out = []
    if os.path.isdir(TRAB):
        for s in os.listdir(TRAB):
            c = os.path.join(TRAB, s, "cortes.json")
            if SLUG.match(s) and os.path.isfile(c):
                out.append((os.path.getmtime(c), s))
    return [s for _, s in sorted(out, reverse=True)]


def resumo(slug):
    T = pasta_T(slug)
    cs = ler(os.path.join(T, "cortes.json"))
    info = ler(os.path.join(T, "info.json"), {}) or {}
    return {"slug": slug, "titulo": info.get("title") or info.get("titulo") or slug,
            "cortes": [{"n": c["n"], "titulo": c["titulo"], "dur": c["dur"], "nota": c.get("nota"),
                        "legenda": c.get("legenda", ""), "editado": os.path.exists(os.path.join(T, f"corte-{c['n']}", "edicao.json")),
                        "final": os.path.exists(os.path.join(SAIDA, slug, f"corte-{c['n']}.mp4"))}
                       for c in cs["cortes"] if os.path.exists(os.path.join(T, f"corte-{c['n']}", "plano.json"))]}


def dados_corte(slug, n):
    import renderizar
    T = pasta_T(slug)
    pasta = os.path.join(T, f"corte-{n}")
    plano = ler(os.path.join(pasta, "plano.json"))
    meta = ler(os.path.join(pasta, "legenda.json"), {}) or {}
    ed = ler(os.path.join(pasta, "edicao.json"))
    grupos = meta.get("grupos", [])
    padrao = {
        "camera": {"modo": "auto", "chaves": []},
        "legenda": {"cor": meta.get("cor", "#FFD400"), "tam": 100,
                    "y": int(grupos[0]["y"]) if grupos else 1305,
                    "grupos": [{"ini": g["ini"], "fim": g["fim"], "texto": g["texto"], "enfase": g.get("enfase", [])}
                               for g in grupos]},
        "facecam": {"visivel": True, "larg": renderizar.CAM_LARG, "raio": renderizar.CAM_RAIO, "x": None, "y": None},
        "efeitos": True,
    }
    return {"plano": plano, "edicao": ed or padrao, "padrao": padrao, "tem_edicao": ed is not None,
            "fonte_nome": meta.get("fonte_nome", "Montserrat Black"),
            "constantes": {"LARG": 1080, "ALT": 1920, "CAM_LARG": renderizar.CAM_LARG, "CAM_RAIO": renderizar.CAM_RAIO,
                           "CAM_MARGEM": renderizar.CAM_MARGEM, "CAM_BAIXO": renderizar.CAM_BAIXO},
            "final": os.path.exists(os.path.join(SAIDA, slug, f"corte-{n}.mp4")),
            "estado": estados.get((slug, n))}


def previa(slug, n):
    """Cópia leve (1280 de largura, um quadro-chave a cada 1/3 s) do fonte.mp4 pra o navegador buscar no tempo rápido."""
    import comum
    pasta = os.path.join(pasta_T(slug), f"corte-{n}")
    src, dst = os.path.join(pasta, "fonte.mp4"), os.path.join(pasta, "previa.mp4")
    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    with trava:
        ev = previas.get(dst)
        dono = ev is None
        if dono:
            ev = previas[dst] = threading.Event()
    if not dono:
        ev.wait(600)
        return dst
    try:
        comum.preparar()
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        v = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26"] if "h264_nvenc" in enc else \
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]
        tmp = dst + ".parcial.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-vf", "scale=1280:-2", *v, "-g", "10",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", tmp], check=True)
        os.replace(tmp, dst)
    finally:
        ev.set()
    return dst


def renderizar_bg(slug, n):
    import importlib
    import enquadrar, efeitos, visual, renderizar, edicao                   # noqa: E401
    for m in (enquadrar, efeitos, visual, renderizar, edicao):                      # pega correções nos scripts sem reiniciar
        importlib.reload(m)
    T = pasta_T(slug)
    saida = os.path.join(SAIDA, slug, f"corte-{n}.mp4")
    estados[(slug, n)] = {"estado": "renderizando", "msg": "renderizando...", "ini": time.time()}
    try:
        edicao.renderizar_editado(T, n, saida)
        estados[(slug, n)] = {"estado": "pronto", "msg": f"pronto em {time.time() - estados[(slug, n)]['ini']:.0f} s",
                              "ini": estados[(slug, n)]["ini"], "quando": time.time()}
    except BaseException as e:                                             # SystemExit do ffmpeg também
        traceback.print_exc()
        estados[(slug, n)] = {"estado": "erro", "msg": f"erro: {e}", "ini": time.time()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _arquivo(self, p, tipo):
        tam = os.path.getsize(p)
        rng = self.headers.get("Range")
        ini, fim = 0, tam - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    ini = int(m.group(1))
                    fim = int(m.group(2)) if m.group(2) else tam - 1
                elif m.group(2):
                    ini = max(0, tam - int(m.group(2)))
            fim = min(fim, tam - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {ini}-{fim}/{tam}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fim - ini + 1))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(ini)
            falta = fim - ini + 1
            try:
                while falta > 0:
                    b = f.read(min(1 << 20, falta))
                    if not b:
                        break
                    self.wfile.write(b)
                    falta -= len(b)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            partes = [urllib.parse.unquote(x) for x in u.path.strip("/").split("/") if x]
            if not partes or partes in (["editar"], ["config"]):
                b = open(INICIO if not partes else PAGINA if partes == ["editar"] else CONFIG, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)
            elif partes == ["estilo.css"]:                                  # visual comum das 3 páginas
                b = open(os.path.join(os.path.dirname(PAGINA), "estilo.css"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/css; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)
            elif partes == ["api", "projetos"]:
                ps = projetos()
                ini = self.server.inicial if self.server.inicial in ps else (ps[0] if ps else None)
                self._json({"projetos": ps, "inicial": ini, "lista": lista_projetos()})
            elif partes == ["api", "tarefas"]:
                self._json({"tarefas": tarefas()})
            elif partes[:2] == ["api", "chatgpt"] and len(partes) == 4:
                import chatgpt
                import config
                T = pasta_T(partes[2])
                self._json({**chatgpt.historico(T, int(partes[3])), "tem_chave": bool(config.ler().get("OPENAI_API_KEY"))})
            elif partes[0] == "chatgpt-arq" and len(partes) == 4:
                arq = os.path.join(pasta_T(partes[1]), f"corte-{int(partes[2])}", "chatgpt", os.path.basename(partes[3]))
                self._arquivo(arq, TIPOS.get(os.path.splitext(arq)[1].lower(), "application/octet-stream"))
            elif partes == ["api", "presets"]:
                self._json({"presets": presets_ler()})
            elif partes[0] == "preset-arquivo" and len(partes) == 2:
                arq = os.path.join(PRESETS_MIDIAS, os.path.basename(partes[1]))
                self._arquivo(arq, TIPOS.get(os.path.splitext(arq)[1].lower(), "application/octet-stream"))
            elif partes == ["api", "config"]:
                import config
                import automatico
                self._json({**config.publico(), "claude_code": bool(automatico.achar_claude()),
                            "arquivo": os.path.relpath(config.ARQ, AQUI)})
            elif partes[:2] == ["api", "tarefa"] and len(partes) == 4 and partes[3] == "log" and SLUG.match(partes[2]):
                p = os.path.join(TAREFAS, f"{partes[2]}.log")
                linhas = open(p, encoding="utf-8", errors="replace").read().splitlines()[-120:] if os.path.exists(p) else []
                self._json({"log": "\n".join(linhas)})
            elif partes[0] == "miniatura" and len(partes) == 2 and SLUG.match(partes[1]):
                self._arquivo(os.path.join(TRAB, partes[1], "miniatura.jpg"), "image/jpeg")
            elif partes[0] == "midia-extra" and len(partes) == 3:
                arq = os.path.join(pasta_T(partes[1]), "midias", os.path.basename(partes[2]))
                self._arquivo(arq, TIPOS.get(os.path.splitext(arq)[1].lower(), "application/octet-stream"))
            elif partes[:2] == ["api", "projeto"] and len(partes) == 3:
                self._json(resumo(partes[2]))
            elif partes[:2] == ["api", "corte"] and len(partes) == 4:
                self._json(dados_corte(partes[2], int(partes[3])))
            elif partes[:2] == ["api", "estado"] and len(partes) == 4:
                self._json(estados.get((partes[2], int(partes[3]))) or {"estado": "parado"})
            elif partes[0] == "midia" and len(partes) == 4:
                slug, n, qual = partes[1], int(partes[2]), partes[3]
                if qual == "previa.mp4":
                    self._arquivo(previa(slug, n), "video/mp4")
                elif qual == "final.mp4":
                    pasta_T(slug)
                    self._arquivo(os.path.join(SAIDA, slug, f"corte-{n}.mp4"), "video/mp4")
                elif qual == "fonte.ttf":
                    meta = ler(os.path.join(pasta_T(slug), f"corte-{n}", "legenda.json"))
                    self._arquivo(os.path.join(meta["pasta_fonte"], meta["fonte"]), "font/ttf")
                else:
                    self._json({"erro": "não achei"}, 404)
            else:
                self._json({"erro": "não achei"}, 404)
        except (FileNotFoundError, ValueError) as e:
            self._json({"erro": str(e)}, 404)
        except Exception as e:                                              # noqa: BLE001
            traceback.print_exc()
            self._json({"erro": str(e)}, 500)

    def do_POST(self):
        try:
            if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.porta}",
                                                  f"http://localhost:{self.server.porta}"):
                return self._json({"erro": "origem não permitida"}, 403)
            u = urllib.parse.urlparse(self.path)
            partes = [urllib.parse.unquote(x) for x in u.path.strip("/").split("/") if x]
            q = urllib.parse.parse_qs(u.query)
            if partes == ["api", "upload"]:                                 # vídeo pra cortar (página inicial)
                import uuid
                os.makedirs(UPLOADS, exist_ok=True)
                nome = uuid.uuid4().hex[:8] + "_" + nome_seguro((q.get("nome") or ["video.mp4"])[0], "video")
                if os.path.splitext(nome)[1] not in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts"):
                    return self._json({"erro": "mande um vídeo (mp4, mov, mkv, webm, avi)"}, 400)
                gravar_corpo(self, os.path.join(UPLOADS, nome))
                return self._json({"upload": nome})
            if partes[:2] == ["api", "midia"] and len(partes) == 3:         # imagem/áudio pro editor
                pasta = os.path.join(pasta_T(partes[2]), "midias")
                os.makedirs(pasta, exist_ok=True)
                nome = nome_seguro((q.get("nome") or ["midia"])[0], "midia")
                if not TIPOS.get(os.path.splitext(nome)[1], "").startswith(("image/", "audio/")):
                    return self._json({"erro": "mande uma imagem (png, jpg, webp, gif) ou um áudio (mp3, wav, m4a, ogg)"}, 400)
                base, ext = os.path.splitext(nome)
                k = 2
                while os.path.exists(os.path.join(pasta, nome)):
                    nome = f"{base}-{k}{ext}"
                    k += 1
                gravar_corpo(self, os.path.join(pasta, nome))
                return self._json(info_midia(os.path.join(pasta, nome)))
            corpo = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            if partes == ["api", "config"]:
                import config
                d = json.loads(corpo.decode("utf-8") or "{}")
                if d.get("VIBECLIPPER_IA") not in (None, "", "auto", "api", "claude_code"):
                    return self._json({"erro": "modo inválido"}, 400)
                if d.get("VIBECLIPPER_MODELO") not in (None, "", "opus", "sonnet"):
                    return self._json({"erro": "modelo inválido"}, 400)
                config.salvar({k: v for k, v in d.items() if k in config.CHAVES})
                return self._json({"ok": True, **config.publico()})
            if partes == ["api", "config", "testar-openai"]:
                import chatgpt
                d = json.loads(corpo.decode("utf-8") or "{}")
                try:
                    return self._json(chatgpt.testar(d.get("chave") or chatgpt.chave()))
                except chatgpt.Erro as e:
                    return self._json({"ok": False, "msg": str(e)})
            if partes[:2] == ["api", "chatgpt"] and len(partes) == 5:              # aba ChatGPT do editor
                import chatgpt
                T, n, acao = pasta_T(partes[2]), int(partes[3]), partes[4]
                if not os.path.isdir(os.path.join(T, f"corte-{n}")):
                    raise FileNotFoundError(f"corte {n}")
                try:
                    if acao == "ref":
                        if len(corpo) > 25 * 1024 * 1024:
                            return self._json({"erro": "imagem grande demais (máximo 25 MB)"}, 400)
                        return self._json(chatgpt.salvar_ref(T, n, (q.get("nome") or ["ref.png"])[0], corpo))
                    d = json.loads(corpo.decode("utf-8") or "{}")
                    if acao == "textos":
                        return self._json(chatgpt.gerar_textos(T, n, str(d.get("pedido") or "")[:2000]))
                    if acao == "thumb":
                        return self._json(chatgpt.gerar_thumb(
                            T, n, str(d.get("pedido") or "")[:2000], [str(x) for x in (d.get("refs") or [])][:10],
                            d.get("formato") or "vertical", d.get("qualidade") or "medium", bool(d.get("base_ultima"))))
                    if acao == "print":
                        return self._json(chatgpt.print_momento(T, n, float(d.get("t") or 0)))
                    if acao == "tirar-ref":
                        return self._json(chatgpt.tirar_ref(T, n, os.path.basename(str(d.get("arquivo") or ""))))
                    if acao == "resultado":                                # a pessoa editou o título/descrição/tags
                        h = chatgpt.historico(T, n)
                        h["resultado"] = {"titulo": str(d.get("titulo") or ""), "descricao": str(d.get("descricao") or ""),
                                          "tags": [str(t) for t in (d.get("tags") or [])][:30]}
                        chatgpt.gravar(T, n, h)
                        return self._json(h)
                except chatgpt.Erro as e:
                    return self._json({"erro": str(e)}, 400)
            if partes == ["api", "config", "testar"]:
                return self._json(testar_chave(json.loads(corpo.decode("utf-8") or "{}")))
            if partes == ["api", "presets"]:
                return self._json(preset_salvar(json.loads(corpo.decode("utf-8") or "{}")))
            if partes[:2] == ["api", "presets"] and len(partes) == 4 and SLUG.match(partes[2]):
                if partes[3] == "apagar":
                    preset_apagar(partes[2])
                    return self._json({"ok": True})
                if partes[3] == "usar":
                    d = json.loads(corpo.decode("utf-8") or "{}")
                    return self._json({"itens": preset_aplicar(partes[2], d.get("slug") or "")})
            if partes == ["api", "tarefa"]:
                return self._json(nova_tarefa(json.loads(corpo.decode("utf-8") or "{}")))
            if partes[:2] == ["api", "tarefa"] and len(partes) == 4 and partes[3] == "cancelar" and SLUG.match(partes[2]):
                tid = partes[2]
                arq = os.path.join(TAREFAS, f"{tid}.json")
                st = ler(arq, {}) or {}
                st.update(estado="cancelado", cancelar=True, fim=time.time(), msg="cancelado")
                json.dump(st, open(arq, "w", encoding="utf-8"), ensure_ascii=False)
                p = rodando.get(tid)
                if p:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
                    else:
                        p.kill()
                return self._json({"ok": True})
            if partes[:2] in (["api", "salvar"], ["api", "renderizar"]) and len(partes) == 4:
                slug, n = partes[2], int(partes[3])
                pasta = os.path.join(pasta_T(slug), f"corte-{n}")
                ed = json.loads(corpo.decode("utf-8"))
                with open(os.path.join(pasta, "edicao.json"), "w", encoding="utf-8") as f:
                    json.dump(ed, f, ensure_ascii=False, indent=1)
                if partes[1] == "renderizar":
                    if (estados.get((slug, n)) or {}).get("estado") == "renderizando":
                        return self._json({"ok": False, "msg": "já está renderizando"})
                    threading.Thread(target=renderizar_bg, args=(slug, n), daemon=True).start()
                return self._json({"ok": True})
            if partes[:2] == ["api", "apagar-video"] and len(partes) == 3:
                return self._json({"ok": True, "liberado": apagar_projeto(partes[2])})
            if partes == ["api", "limpar-tudo"]:
                liberado, erros = 0, []
                for s in projetos():
                    try:
                        liberado += apagar_projeto(s)
                    except ValueError as e:
                        erros.append(f"{s}: {e}")
                up = 0
                if os.path.isdir(UPLOADS):                                  # envios que sobraram de tarefas que falharam
                    for f in os.listdir(UPLOADS):
                        try:
                            up += os.path.getsize(os.path.join(UPLOADS, f))
                            os.remove(os.path.join(UPLOADS, f))
                        except OSError:
                            pass
                return self._json({"ok": not erros, "liberado": liberado + up, "erros": erros})
            if partes[:2] == ["api", "abrir-pasta"] and len(partes) == 3:
                pasta_T(partes[2])                                          # valida o nome
                p = os.path.join(SAIDA, partes[2])
                os.makedirs(p, exist_ok=True)
                if os.name == "nt":
                    os.startfile(p)                                          # noqa: S606 (Explorer na pasta dos cortes)
                else:
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
                return self._json({"ok": True})
            if partes[:2] == ["api", "descartar"] and len(partes) == 4:
                p = os.path.join(pasta_T(partes[2]), f"corte-{int(partes[3])}", "edicao.json")
                if os.path.exists(p):
                    os.remove(p)
                return self._json({"ok": True})
            self._json({"erro": "não achei"}, 404)
        except Exception as e:                                              # noqa: BLE001
            traceback.print_exc()
            self._json({"erro": str(e)}, 500)


def main():
    import comum
    comum.preparar()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", nargs="?", default=None, help="a pasta do vídeo em trabalho/ (padrão: a mais recente)")
    ap.add_argument("--porta", type=int, default=8765)
    ap.add_argument("--sem-navegador", action="store_true")
    a = ap.parse_args()
    ps = projetos()
    inicial = a.video or (ps[0] if ps else None)
    if a.video:
        pasta_T(a.video)
    # tarefas que estavam rodando quando o editor fechou: marca como interrompidas
    for d in tarefas():
        if d.get("estado") in ("rodando", "na fila"):
            d.update(estado="erro", erro="o editor foi fechado no meio (mande de novo)", fim=time.time())
            json.dump(d, open(os.path.join(TAREFAS, f"{d['id']}.json"), "w", encoding="utf-8"), ensure_ascii=False)
    srv = ThreadingHTTPServer(("127.0.0.1", a.porta), Handler)
    srv.inicial, srv.porta = inicial, a.porta
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{a.porta}/"
    print(f"[editor] aberto em {url}  (Ctrl+C pra fechar)")
    threading.Thread(target=trabalhador, daemon=True).start()
    # prepara as prévias em segundo plano (a 1ª vez demora uns segundos por corte)
    def preparar_previas():
        if not inicial:
            return
        for c in resumo(inicial)["cortes"]:
            try:
                previa(inicial, c["n"])
            except Exception as e:                                          # noqa: BLE001
                print(f"[editor] prévia do corte {c['n']}: {e}")
    threading.Thread(target=preparar_previas, daemon=True).start()
    if not a.sem_navegador:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[editor] fechado")


if __name__ == "__main__":
    main()
