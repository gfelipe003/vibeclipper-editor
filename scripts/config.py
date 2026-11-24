"""VibeClipper: configurações da pessoa (chaves e preferências), guardadas no arquivo .env da pasta da skill.

O .env fica SÓ no computador da pessoa: ele está no .gitignore e nunca vai pro Git. Modelo em .env.example.

  ANTHROPIC_API_KEY    chave da API do Claude (console.anthropic.com), pra escolher os momentos sem o Claude Code
  ELEVENLABS_API_KEY   chave da ElevenLabs (opcional, transcrição paga e mais precisa)
  VIBECLIPPER_IA       quem escolhe os momentos: auto (padrão) | api | claude_code
  VIBECLIPPER_MODELO   modelo padrão pra escolher: opus (padrão) | sonnet
  VIBECLIPPER_CLAUDE   caminho do Claude Code, se ele não for achado sozinho (opcional)
"""
import os

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARQ = os.path.join(RAIZ, ".env")
CHAVES = ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "VIBECLIPPER_IA", "VIBECLIPPER_MODELO", "VIBECLIPPER_CLAUDE")
SEGREDOS = ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY")
PADRAO = {"VIBECLIPPER_IA": "auto", "VIBECLIPPER_MODELO": "opus"}
# os IDs exatos dos modelos da API do Claude
MODELOS_API = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5"}


def ler():
    """Configuração atual: o .env, com as variáveis de ambiente por cima (quem já usa variável continua funcionando)."""
    d = dict(PADRAO)
    if os.path.exists(ARQ):
        for linha in open(ARQ, encoding="utf-8"):
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in CHAVES and v:
                d[k] = v
    for k in CHAVES:
        if os.environ.get(k):
            d[k] = os.environ[k].strip()
    return d


def salvar(novos):
    """Grava no .env. Valor vazio ou None = não mexe; "__apagar__" = apaga."""
    atual = {}
    if os.path.exists(ARQ):
        for linha in open(ARQ, encoding="utf-8"):
            if "=" in linha and not linha.strip().startswith("#"):
                k, v = linha.split("=", 1)
                atual[k.strip()] = v.strip()
    for k, v in novos.items():
        if k not in CHAVES or v is None or v == "":
            continue
        if v == "__apagar__":
            atual.pop(k, None)
        else:
            atual[k] = str(v).strip().replace("\n", "").replace("\r", "")
    linhas = ["# VibeClipper: suas chaves e preferências. Este arquivo é SEU: não envie pra ninguém nem pro Git.", ""]
    linhas += [f"{k}={atual[k]}" for k in CHAVES if atual.get(k)]
    tmp = ARQ + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    os.replace(tmp, ARQ)


def mascarar(v):
    if not v:
        return ""
    return ("•" * 6) + v[-4:] if len(v) > 8 else "••••"


def publico():
    """O que a página de configurações pode ver: nunca a chave inteira."""
    d = ler()
    return {k: (mascarar(d.get(k)) if k in SEGREDOS else d.get(k, "")) for k in CHAVES} | \
           {f"tem_{k}": bool(d.get(k)) for k in SEGREDOS}
