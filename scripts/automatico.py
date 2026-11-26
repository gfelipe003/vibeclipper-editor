"""VibeClipper sozinho: de um link do YouTube ou de um arquivo de vídeo até os cortes prontos, sem passar pelo chat.

  python scripts/automatico.py --url "https://youtu.be/..." [--gameplay auto|sim|nao] [--cortes 0] [--idioma pt]
  python scripts/automatico.py --arquivo meu-video.mp4 ...

As mesmas 4 etapas da skill (SKILL.md). A ESCOLHA dos momentos é feita pelo Claude Code em modo automático (claude -p),
com as regras da etapa 2 do SKILL.md, usando a conta do Claude que já está logada no computador (gasta do seu plano o mesmo
que uma conversa curta; o resto roda no seu computador, de graça). O editor (editor.py) chama isto pela página inicial.

  --modelo opus (mais caprichado) | sonnet (mais econômico); padrão: o das Configurações (.env)
  --tarefa ID: grava o andamento em trabalho/_tarefas/ID.json (a página inicial do editor mostra)
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
TRAB = os.path.join(RAIZ, "trabalho")
SAIDA = os.path.join(RAIZ, "saida")
PY = sys.executable
ETAPAS = ["Baixando", "Transcrevendo", "Escolhendo os momentos", "Cortando", "Enquadrando", "Legendando",
          "Pronto"]


class Tarefa:
    def __init__(self, tid, origem):
        self.tid = tid
        self.dir = os.path.join(TRAB, "_tarefas")
        os.makedirs(self.dir, exist_ok=True)
        self.arq = os.path.join(self.dir, f"{tid}.json") if tid else None
        self.log = os.path.join(self.dir, f"{tid}.log") if tid else None
        self.d = {"id": tid, "origem": origem, "estado": "rodando", "etapa": 0, "etapas": ETAPAS, "msg": "começando…",
                  "progresso": 0, "inicio": time.time(), "slug": None, "titulo": None, "cortes": 0}
        self.salvar()

    def salvar(self):
        if self.arq:
            tmp = self.arq + ".tmp"
            json.dump(self.d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            os.replace(tmp, self.arq)

    def etapa(self, i, msg, progresso=None):
        self.d.update(etapa=i, msg=msg)
        if progresso is not None:
            self.d["progresso"] = progresso
        self.salvar()
        self.escrever(f"\n=== {ETAPAS[i]}: {msg}\n")
        print(f"[automatico] {ETAPAS[i]}: {msg}", flush=True)

    def escrever(self, txt):
        if self.log:
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(txt)


def rodar(tarefa, args, cwd=RAIZ, tudo=False):
    """Roda um script da skill; o que ele escreve vai pro log da tarefa. Para com erro claro se ele falhar."""
    tarefa.escrever("$ " + " ".join(str(a) for a in args) + "\n")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    tarefa.escrever(r.stdout + r.stderr)
    if r.returncode != 0:
        fim = (r.stdout + r.stderr).strip().splitlines()[-6:]
        raise RuntimeError("\n".join(fim) or f"falhou: {' '.join(map(str, args[:3]))}")
    return r.stdout + r.stderr if tudo else r.stdout


def slugify(s, maximo=48):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    s = "-".join(p for p in s.split("-") if p)[:maximo].strip("-")
    return s or "video"


def pasta_nova(slug):
    os.makedirs(TRAB, exist_ok=True)
    base, k = slug, 2
    while os.path.exists(os.path.join(TRAB, slug)):
        slug = f"{base}-{k}"
        k += 1
    os.makedirs(os.path.join(TRAB, slug))
    return slug


def achar_claude():
    """O Claude Code: no PATH, numa variável VIBECLIPPER_CLAUDE ou o que vem dentro do app Claude (Windows)."""
    p = os.environ.get("VIBECLIPPER_CLAUDE")
    if p and os.path.isfile(p):
        return p
    w = shutil.which("claude")
    if w:
        return w
    cands = []
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")):
        if base:
            cands += glob.glob(os.path.join(base, "Claude-Data", "*", "claude-code", "*", "*", "claude.exe"))
            cands += glob.glob(os.path.join(base, "Claude", "claude-code", "*", "*", "claude.exe"))
    home = os.path.expanduser("~")
    cands += glob.glob(os.path.join(home, ".claude", "local", "claude*"))
    cands += glob.glob(os.path.join(home, ".local", "bin", "claude*"))

    def versao(c):
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", c)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    cands = [c for c in cands if os.path.isfile(c)]
    return max(cands, key=lambda c: (versao(c), os.path.getmtime(c))) if cands else None


def regras_da_skill():
    """A etapa 2 do SKILL.md (as regras de escolha) + o que vale pra gameplay: a mesma régua do chat."""
    s = open(os.path.join(RAIZ, "SKILL.md"), encoding="utf-8").read()
    m = re.search(r"## Etapa 2: escolhe \(você\)\n(.*?)\nDepois valide", s, re.S)
    regras = m.group(1) if m else ""
    g = re.search(r"- Na \*\*escolha\*\* \(etapa 2\).*?(?=\n- |\n\n)", s, re.S)
    return regras.strip() + ("\n\nSe for gameplay:\n" + g.group(0) if g else "")


def json_da_resposta(txt):
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("a resposta não trouxe um JSON")
    return json.loads(txt[i:j + 1])


def perguntar_claude(tarefa, exe, sistema, pergunta, modelo):
    cwd = tempfile.mkdtemp(prefix="vibeclipper-")                         # fora da skill: não carrega o CLAUDE.md dela
    try:
        args = [exe, "-p", "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
                "--no-session-persistence", "--model", modelo, "--system-prompt", sistema, "--output-format", "json"]
        tarefa.escrever(f"$ claude -p --model {modelo} (pergunta com {len(pergunta)} caracteres)\n")
        r = subprocess.run(args, input=pergunta, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           cwd=cwd, timeout=900)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError(f"o Claude não respondeu: {(r.stderr or '').strip()[-400:]}")
    d = json.loads(r.stdout)
    if d.get("is_error"):
        raise RuntimeError(f"o Claude deu erro: {str(d.get('result'))[:400]}")
    u = d.get("usage", {})
    tarefa.escrever(f"(claude: {u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0) + u.get('cache_creation_input_tokens', 0)}"
                    f" tokens de entrada, {u.get('output_tokens', 0)} de saída)\n")
    return d.get("result", "")


def perguntar_api(tarefa, chave, sistema, pergunta, modelo):
    """A mesma pergunta pela API do Claude, com a chave da pessoa (Configurações). SDK oficial da Anthropic."""
    import anthropic
    import config
    mid = config.MODELOS_API.get(modelo, config.MODELOS_API["opus"])
    cliente = anthropic.Anthropic(api_key=chave, max_retries=3)
    tarefa.escrever(f"$ API do Claude ({mid}), pergunta com {len(pergunta)} caracteres\n")
    try:
        # transcrição longa: streaming evita timeout. Se um classificador recusar, o servidor tenta outro modelo sozinho
        with cliente.beta.messages.stream(
                model=mid, max_tokens=32000, system=sistema, output_config={"effort": "high"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                messages=[{"role": "user", "content": pergunta}]) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise RuntimeError("a chave da API do Claude foi recusada: confira em Configurações")
    except anthropic.PermissionDeniedError:
        raise RuntimeError("a chave da API do Claude não tem permissão pra esse modelo (veja Configurações)")
    except anthropic.RateLimitError:
        raise RuntimeError("a API do Claude está no limite de uso agora: tente de novo em alguns minutos")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"a API do Claude deu erro {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise RuntimeError("sem conexão com a API do Claude: confira a internet")
    if msg.stop_reason == "refusal":
        raise RuntimeError("o Claude recusou essa transcrição (tente o outro modelo em Configurações)")
    u = msg.usage
    tarefa.escrever(f"(claude: {u.input_tokens + (u.cache_read_input_tokens or 0)} tokens de entrada, "
                    f"{u.output_tokens} de saída, modelo {msg.model})\n")
    return "".join(b.text for b in msg.content if b.type == "text")


def escolher(tarefa, T, titulo, n_cortes, gameplay, modelo, orientacao=""):
    import config
    cfg = config.ler()
    modo, chave = cfg.get("VIBECLIPPER_IA", "auto"), cfg.get("ANTHROPIC_API_KEY")
    exe = None if modo == "api" else achar_claude()
    if modo == "api" or (modo == "auto" and chave):
        if not chave:
            raise RuntimeError("falta a chave da API do Claude: coloque em Configurações")
        perguntar = lambda t, s, p, m: perguntar_api(t, chave, s, p, m)       # noqa: E731
    elif exe:
        perguntar = lambda t, s, p, m: perguntar_claude(t, exe, s, p, m)     # noqa: E731
    else:
        raise RuntimeError("ninguém pra escolher os momentos: coloque a sua chave da API do Claude em Configurações "
                           "(ou instale o Claude Code e faça login)")
    numerada = open(os.path.join(T, "transcricao-numerada.md"), encoding="utf-8").read()
    modelo_json = open(os.path.join(T, "escolha.modelo.json"), encoding="utf-8").read()
    sistema = ("Você é o editor de cortes do VibeClipper: escolhe, numa transcrição numerada, os melhores momentos de um "
               "vídeo longo pra virar cortes verticais (Shorts, Reels, TikTok). Siga estas regras à risca:\n\n"
               + regras_da_skill() +
               "\n\nResponda SÓ com o JSON do escolha.json (sem texto antes ou depois, sem ```), no formato do modelo.")
    quantos = (f"Faça exatamente {n_cortes} cortes." if n_cortes else
               "Quantos cortes: siga a regra (5 a 8 num vídeo longo, 2 a 4 em vídeo de menos de 20 minutos).")
    gp = {"sim": "É um vídeo de gameplay (jogo com a webcam do streamer).",
          "nao": "Não é gameplay (é conversa, podcast, aula ou parecido)."}.get(gameplay, (
          "Diga também se é gameplay: ponha no topo do JSON \"gameplay\": true (jogo com webcam do streamer) ou false."))
    orient = ""
    if orientacao:
        orient = ("\n\nORIENTAÇÃO DA PESSOA (vale mais que as regras gerais; siga o que ela pedir):\n"
                  f"\"\"\"\n{orientacao}\n\"\"\"\n"
                  "Os tempos que ela citar (ex.: 12:30, 1:02:10) são do vídeo original: ache as linhas pelo [h:mm:ss] de "
                  "cada uma. Se ela pediu um trecho específico num corte, esse corte TEM que existir e cobrir o trecho, "
                  "mesmo que fuja dos 30 a 75 s (ajuste o começo e o fim pra frase completa mais próxima); escreva no "
                  "\"porque\" dele que veio da orientação. Os outros cortes seguem as regras normais.")
    pergunta = (f"Título do vídeo: {titulo}\n{quantos}\n{gp}{orient}\n\nModelo do escolha.json:\n{modelo_json}\n\n"
                f"Transcrição numerada (leia inteira):\n\n{numerada}")
    resp = perguntar(tarefa, sistema, pergunta, modelo)
    esc = json_da_resposta(resp)
    arq = os.path.join(T, "escolha.json")
    cortes = os.path.join(T, "cortes.json")
    for rodada in range(2):
        json.dump(esc, open(arq, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        out = rodar(tarefa, [PY, "scripts/escolher.py", "validar", arq, "--transcricao", os.path.join(T, "transcricao.json"),
                             "--saida", cortes], tudo=True)

        def fora(l):                                      # 2 s de tolerância: 29,8 s não vale outra chamada
            m = re.search(r"tem ([\d.]+) s", l)
            return not m or not (28.0 <= float(m.group(1)) <= 77.0)
        avisos = [l for l in out.splitlines() if "AVISO" in l and fora(l)]
        if not avisos or rodada == 1:
            break
        tarefa.etapa(2, f"ajustando {len(avisos)} corte(s) fora do tempo")
        resp = perguntar(tarefa, sistema, (
            "O validador avisou:\n" + "\n".join(avisos) + "\n\nCorrija SÓ esses cortes (mude linha_ini/linha_fim pra ficar "
            "entre 30 e 75 s, ou tire o corte se não der) e devolva o escolha.json inteiro. "
            + ("Um corte que cobre o trecho pedido na orientação da pessoa fica como está, mesmo fora do tempo.\n"
               f"Orientação: {orientacao}\n" if orientacao else "") + "\n\nSeu JSON anterior:\n"
            + json.dumps(esc, ensure_ascii=False) + "\n\nTranscrição numerada:\n\n" + numerada), modelo)
        esc = json_da_resposta(resp)
    return esc


def main():
    sys.path.insert(0, AQUI)
    import comum
    comum.preparar()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--arquivo")
    ap.add_argument("--titulo", default=None)
    ap.add_argument("--gameplay", choices=["auto", "sim", "nao"], default="auto")
    ap.add_argument("--cortes", type=int, default=0, help="0 = a skill decide")
    ap.add_argument("--idioma", default="pt")
    ap.add_argument("--modelo", default=None, help="opus | sonnet (padrão: o das Configurações)")
    ap.add_argument("--tarefa", default=None)
    ap.add_argument("--orientacao", default="", help="pedido livre da pessoa pra escolha (ex.: 'de 12:30 a 13:45 num corte')")
    ap.add_argument("--renderizar", action="store_true",
                    help="renderiza os MP4 finais no fim (sem isso, para pronto pra ajustar e renderizar no editor)")
    ap.add_argument("--mover", action="store_true", help="(arquivo) move em vez de copiar (o upload da página inicial)")
    a = ap.parse_args()
    if not a.modelo:
        import config
        a.modelo = config.ler().get("VIBECLIPPER_MODELO", "opus")
    if not a.url and not a.arquivo:
        raise SystemExit("passe --url ou --arquivo")
    tarefa = Tarefa(a.tarefa, a.url or os.path.basename(a.arquivo))
    try:
        # 1. baixa (ou importa o arquivo)
        if a.url:
            tarefa.etapa(0, "lendo o vídeo no YouTube", 2)
            titulo = a.titulo or rodar(tarefa, [PY, "-m", "yt_dlp", "--no-playlist", "--skip-download", "--print",
                                                "%(title)s", a.url]).strip().splitlines()[-1]
            slug = pasta_nova(slugify(titulo))
            T = os.path.join(TRAB, slug)
            tarefa.d.update(slug=slug, titulo=titulo)
            tarefa.etapa(0, "baixando o áudio", 4)
            rodar(tarefa, [PY, "scripts/baixar.py", a.url, "--pasta", T])
        else:
            titulo = a.titulo or os.path.splitext(os.path.basename(a.arquivo))[0]
            titulo = re.sub(r"^[0-9a-f]{8}_", "", titulo)                    # tira o prefixo do upload
            slug = pasta_nova(slugify(titulo))
            T = os.path.join(TRAB, slug)
            tarefa.d.update(slug=slug, titulo=titulo)
            tarefa.etapa(0, "preparando o arquivo", 3)
            ext = os.path.splitext(a.arquivo)[1].lower() or ".mp4"
            orig = os.path.join(T, "original" + ext)
            (shutil.move if a.mover else shutil.copyfile)(a.arquivo, orig)
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", orig],
                               capture_output=True, text=True)
            dur = float(r.stdout.strip() or 0)
            json.dump({"title": titulo, "duration": dur, "arquivo": os.path.basename(orig)},
                      open(os.path.join(T, "info.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            rodar(tarefa, ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", orig, "-vn", "-ac", "2", "-c:a", "aac", "-b:a",
                           "192k", os.path.join(T, "audio.m4a")])
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{dur * 0.1:.1f}", "-i", orig, "-frames:v",
                            "1", "-vf", "scale=640:-2", os.path.join(T, "miniatura.jpg")], capture_output=True)
        # 2. transcreve
        tarefa.etapa(1, "Whisper ouvindo o vídeo (a 1ª vez baixa o modelo)", 10)
        audio = os.path.join(T, "audio.m4a")
        if not os.path.exists(audio):
            audio = next(iter(sorted(glob.glob(os.path.join(T, "audio.*")))), audio)
        rodar(tarefa, [PY, "scripts/transcrever.py", audio, "--saida", os.path.join(T, "transcricao.json"),
                       "--prompt", titulo, "--idioma", a.idioma])
        rodar(tarefa, [PY, "scripts/escolher.py", "preparar", os.path.join(T, "transcricao.json"),
                       "--saida", os.path.join(T, "transcricao-numerada.md"), "--titulo", titulo])
        # 3. escolhe (Claude)
        tarefa.etapa(2, f"o Claude ({a.modelo}) está lendo a transcrição e escolhendo os momentos", 30)
        orientacao = (a.orientacao or "").strip()
        if orientacao:
            open(os.path.join(T, "orientacao.txt"), "w", encoding="utf-8").write(orientacao + "\n")
            tarefa.escrever(f"Orientação da pessoa: {orientacao}\n")
        esc = escolher(tarefa, T, titulo, a.cortes, a.gameplay, a.modelo, orientacao)
        gameplay = a.gameplay == "sim" or (a.gameplay == "auto" and bool(esc.get("gameplay")))
        cs = json.load(open(os.path.join(T, "cortes.json"), encoding="utf-8"))["cortes"]
        tarefa.d.update(cortes=len(cs), gameplay=gameplay)
        # 4. corta
        tarefa.etapa(3, f"{len(cs)} cortes escolhidos; " + ("baixando só esses trechos em alta" if a.url else "cortando"), 40)
        if a.url:
            rodar(tarefa, [PY, "scripts/baixar.py", a.url, "--pasta", T, "--trechos", os.path.join(T, "cortes.json")])
            rodar(tarefa, [PY, "scripts/cortar.py", "--cortes", os.path.join(T, "cortes.json"), "--trechos",
                           os.path.join(T, "trechos"), "--audio", audio, "--pasta", T])
        else:
            rodar(tarefa, [PY, "scripts/cortar.py", "--cortes", os.path.join(T, "cortes.json"), "--video", orig,
                           "--audio", audio, "--pasta", T])
        cs = json.load(open(os.path.join(T, "cortes.json"), encoding="utf-8"))["cortes"]
        # 5. enquadra
        for i, c in enumerate(cs):
            n = c["n"]
            tarefa.etapa(4, f"corte {n} de {len(cs)}" + (" (gameplay: segue os personagens)" if gameplay else ""),
                         50 + int(15 * i / max(1, len(cs))))
            P = os.path.join(T, f"corte-{n}")
            rodar(tarefa, [PY, "scripts/enquadrar.py", os.path.join(P, "fonte.mp4"), "--cortes", os.path.join(T, "cortes.json"),
                           "--n", str(n), "--saida", os.path.join(P, "plano.json")] + (["--gameplay"] if gameplay else []))
        # 6. edita e renderiza (2 por vez)
        O = os.path.join(SAIDA, slug)
        os.makedirs(O, exist_ok=True)
        for c in cs:
            P = os.path.join(T, f"corte-{c['n']}")
            rodar(tarefa, [PY, "scripts/legendar.py", "--cortes", os.path.join(T, "cortes.json"), "--n", str(c["n"]),
                           "--plano", os.path.join(P, "plano.json"), "--saida", os.path.join(P, "legenda.ass")])
            rodar(tarefa, [PY, "scripts/efeitos.py", "--plano", os.path.join(P, "plano.json"), "--legenda",
                           os.path.join(P, "legenda.json"), "--saida", os.path.join(P, "sfx.json")])
        feitos = 0
        if not a.renderizar:
            tarefa.etapa(5, "legenda e efeitos prontos; o render fica pra você no editor", 98)
        for k in (range(0, len(cs), 2) if a.renderizar else []):
            par = cs[k:k + 2]
            tarefa.etapa(5, "renderizando " + " e ".join(f"corte {c['n']}" for c in par) + f" ({feitos} de {len(cs)} prontos)",
                         65 + int(33 * feitos / max(1, len(cs))))
            procs = []
            for c in par:
                P = os.path.join(T, f"corte-{c['n']}")
                args = [PY, "scripts/renderizar.py", "--fonte", os.path.join(P, "fonte.mp4"), "--plano",
                        os.path.join(P, "plano.json"), "--legenda", os.path.join(P, "legenda.ass"), "--sfx",
                        os.path.join(P, "sfx.json"), "--saida", os.path.join(O, f"corte-{c['n']}.mp4")]
                tarefa.escrever("$ " + " ".join(args) + "\n")
                procs.append(subprocess.Popen(args, cwd=RAIZ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                              encoding="utf-8", errors="replace"))
            for p in procs:
                out, _ = p.communicate()
                tarefa.escrever(out or "")
                if p.returncode != 0:
                    raise RuntimeError((out or "").strip().splitlines()[-1] if out else "o render falhou")
            feitos += len(par)
        rodar(tarefa, [PY, "scripts/escolher.py", "resumo", os.path.join(T, "cortes.json"), "--saida",
                       os.path.join(O, "cortes.md")])
        tarefa.d.update(estado="pronto", fim=time.time())
        tarefa.etapa(6, f"{len(cs)} cortes prontos em saida/{slug}/" if a.renderizar else
                     f"{len(cs)} cortes prontos pra ajustar: abra no editor e clique em Renderizar em cada um", 100)
    except Exception as e:                                                   # noqa: BLE001
        tarefa.d.update(estado="erro", erro=str(e), fim=time.time())
        tarefa.salvar()
        tarefa.escrever(f"\nERRO: {e}\n")
        print(f"[automatico] ERRO: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
