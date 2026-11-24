"""VibeClipper, etapa 1 (assiste): transcreve o áudio inteiro com o tempo de cada palavra.

O Whisper não só escreve o que foi falado: ele guarda o timestamp de cada palavra (início e fim). É com esse tempo que a
escolha acha o hook exato, que o zoom cai no fim da frase e que cada legenda aparece e some na hora certa.

uso:
  python scripts/transcrever.py trabalho/audio.m4a --saida trabalho/transcricao.json [--modelo turbo] [--idioma pt]
                                [--prompt "nomes próprios do vídeo"]

Dois motores:
  - whisper (padrão): o Whisper LOCAL (faster-whisper), grátis, nada sai do computador. Roda na placa de vídeo NVIDIA se tiver
    (float16); senão no processador (int8, mais lento). Na 1ª vez baixa o modelo (o turbo tem ~1,6 GB), uma vez só.
  - elevenlabs (opcional, pago): --motor elevenlabs manda o áudio pro Speech to Text da ElevenLabs, que é ainda mais
    preciso. Precisa da chave da API: variável ELEVENLABS_API_KEY ou um arquivo .env na pasta da skill com a linha
    ELEVENLABS_API_KEY=sua_chave. A saída é a mesma, então o resto da skill funciona igual.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid

AQUI = os.path.dirname(os.path.abspath(__file__))
FIM_FRASE = re.compile(r"[.!?…]['\")»]*$")


def preparar_cuda():
    """No Windows, o faster-whisper precisa achar as DLLs do cuBLAS e do cuDNN (pacotes pip nvidia-*). Põe as pastas no PATH."""
    if os.name != "nt":
        return
    for nome in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc", "nvidia.cuda_runtime"):
        try:
            mod = __import__(nome, fromlist=["x"])
        except ImportError:
            continue
        for base in list(getattr(mod, "__path__", [])):
            pasta = os.path.join(base, "bin")
            if os.path.isdir(pasta):
                os.environ["PATH"] = pasta + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(pasta)
                except OSError:
                    pass


def carregar_modelo(modelo="turbo"):
    preparar_cuda()
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")   # aviso do Windows sem importância (o download funciona)
    from faster_whisper import WhisperModel
    try:
        m = WhisperModel(modelo, device="cuda", compute_type="float16")
        return m, "cuda/float16"
    except Exception as e:  # sem GPU ou sem CUDA: cai pro processador
        print(f"[transcrever] placa de vídeo indisponível ({str(e)[:80]}), usando o processador", file=sys.stderr)
        return WhisperModel(modelo, device="cpu", compute_type="int8"), "cpu/int8"


def ler_audio(caminho, sr=16000):
    """Decodifica qualquer áudio/vídeo com o ffmpeg em mono 16 kHz (o formato do Whisper)."""
    import numpy as np
    b = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", caminho, "-vn", "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(b, np.float32).copy()


def transcrever(audio, modelo="turbo", idioma="pt", prompt=None):
    audio = ler_audio(audio)
    m, disp = carregar_modelo(modelo)
    try:
        segs, info = m.transcribe(audio, language=idioma, word_timestamps=True, beam_size=5, vad_filter=True,
                                  condition_on_previous_text=False, initial_prompt=prompt)
        segs = list(segs)
    except RuntimeError as e:
        if not disp.startswith("cuda"):
            raise
        print(f"[transcrever] erro na placa de vídeo ({str(e)[:80]}), refazendo no processador", file=sys.stderr)
        from faster_whisper import WhisperModel
        m, disp = WhisperModel(modelo, device="cpu", compute_type="int8"), "cpu/int8"
        segs, info = m.transcribe(audio, language=idioma, word_timestamps=True, beam_size=5, vad_filter=True,
                                  condition_on_previous_text=False, initial_prompt=prompt)
        segs = list(segs)
    saida = {"modelo": f"faster-whisper {modelo} ({disp})", "idioma": info.language, "duracao": round(info.duration, 2),
             "segmentos": []}
    for s in segs:
        palavras = [{"p": w.word.strip(), "ini": round(float(w.start), 3), "fim": round(float(w.end), 3),
                     "prob": round(float(w.probability), 3)} for w in (s.words or []) if w.word.strip()]
        saida["segmentos"].append({"ini": round(float(s.start), 3), "fim": round(float(s.end), 3),
                                   "texto": s.text.strip(), "palavras": palavras})
    return saida


# --- ElevenLabs (opcional) -------------------------------------------------------------------------------------------

def chave_elevenlabs():
    if os.environ.get("ELEVENLABS_API_KEY"):
        return os.environ["ELEVENLABS_API_KEY"].strip()
    for env in (os.path.join(AQUI, "..", ".env"), os.path.join(os.getcwd(), ".env")):
        if os.path.exists(env):
            for linha in open(env, encoding="utf-8"):
                k, _, v = linha.strip().partition("=")
                if k.strip() == "ELEVENLABS_API_KEY" and v.strip():
                    return v.strip().strip('"').strip("'")
    raise SystemExit("[transcrever] falta a chave da ElevenLabs: crie um arquivo .env na pasta da skill com a linha "
                     "ELEVENLABS_API_KEY=sua_chave (ou use o Whisper, que é grátis: tire o --motor elevenlabs)")


def duracao_de(arq):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", arq],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def segmentos_de_palavras(pal, pausa=0.8, max_pal=40):
    """Junta as palavras soltas em segmentos (frase terminada, pausa longa ou 40 palavras), no formato do Whisper."""
    segs, at = [], []
    for i, w in enumerate(pal):
        at.append(w)
        prox = pal[i + 1] if i + 1 < len(pal) else None
        if prox is None or FIM_FRASE.search(w["p"]) or prox["ini"] - w["fim"] >= pausa or len(at) >= max_pal:
            segs.append({"ini": at[0]["ini"], "fim": at[-1]["fim"], "texto": " ".join(x["p"] for x in at), "palavras": at})
            at = []
    return segs


def transcrever_elevenlabs(audio, idioma="pt", modelo="scribe_v2"):
    import urllib.error
    import urllib.request
    fronteira = uuid.uuid4().hex
    campos = {"model_id": modelo, "timestamps_granularity": "word", "tag_audio_events": "false", "diarize": "false"}
    if idioma:
        campos["language_code"] = idioma
    corpo = b"".join(f"--{fronteira}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in campos.items())
    nome = os.path.basename(audio)
    corpo += (f"--{fronteira}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{nome}\"\r\n"
              f"Content-Type: application/octet-stream\r\n\r\n").encode()
    corpo += open(audio, "rb").read() + f"\r\n--{fronteira}--\r\n".encode()
    req = urllib.request.Request("https://api.elevenlabs.io/v1/speech-to-text", data=corpo, method="POST",
                                 headers={"xi-api-key": chave_elevenlabs(),
                                          "Content-Type": f"multipart/form-data; boundary={fronteira}"})
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[transcrever] a ElevenLabs recusou ({e.code}): {e.read().decode('utf-8', 'replace')[:400]}")
    pal = []
    for w in d.get("words", []):
        if w.get("type") != "word" or w.get("start") is None or not str(w.get("text", "")).strip():
            continue
        prob = math.exp(w["logprob"]) if isinstance(w.get("logprob"), (int, float)) else 1.0
        pal.append({"p": w["text"].strip(), "ini": round(float(w["start"]), 3), "fim": round(float(w["end"]), 3),
                    "prob": round(prob, 3)})
    return {"modelo": f"ElevenLabs {modelo}", "idioma": d.get("language_code", idioma),
            "duracao": round(duracao_de(audio) or (pal[-1]["fim"] if pal else 0.0), 2),
            "segmentos": segmentos_de_palavras(pal)}


def realinhar_soltas(transcricao, audio, gap=2.0, sr=16000):
    """Conserta a palavra que o Whisper grudou no fim da frase errada: em fala solta (gameplay, live), ele às vezes põe a
    1ª palavra da PRÓXIMA frase no fim da anterior, esticada em cima do silêncio (ex.: "tem algo estranho aqui, tem" e o 2º
    "tem" só é dito 15 s depois). Aí a legenda mostra uma palavra que ninguém falou. Pra cada palavra seguida de uma pausa
    longa, mede a voz (300-3500 Hz) no tempo dela; se é o mesmo nível do ruído da pausa (sem voz), ela vai pra logo antes da
    próxima palavra, que é onde foi dita. Não muda a ordem das palavras nem as frases (as linhas da transcrição numerada)."""
    import numpy as np
    b = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", audio, "-vn", "-af", "highpass=f=300,lowpass=f=3500",
                        "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(b, np.float32)

    def rms(t0, t1):
        s = a[max(0, int(t0 * sr)):max(0, int(t1 * sr))]
        return float(np.sqrt(np.mean(s.astype(np.float64) ** 2))) if len(s) else 0.0

    pal = palavras(transcricao)
    orig = [(w["ini"], w["fim"]) for w in pal]
    movidas = []
    voz_tipica = float(np.median([rms(a0, a1) for a0, a1 in orig if a1 - a0 >= 0.12])) if orig else 0.0

    def sem_voz(i, g0, g1):
        """Sem voz = no nível do ruído da pausa E bem abaixo da voz normal da pessoa (tiroteio alto no jogo não engana)."""
        janelas = [rms(t, t + 0.5) for t in np.arange(g0, max(g0, g1 - 0.5), 0.5)] or [rms(g0, g1)]
        ruido = float(np.median(janelas)) + 1e-5
        voz = rms(*orig[i])
        dur = orig[i][1] - orig[i][0]
        if dur > 0.6:                                                       # esticada em cima do silêncio: o caso típico
            # quanto dela tem voz de verdade, em fatias de 0,1 s (o comecinho pode pegar o fim da palavra anterior)
            lim = max(1.6 * ruido, 0.45 * voz_tipica)
            fatias = [rms(t, t + 0.1) for t in np.arange(orig[i][0], orig[i][1] - 0.05, 0.1)]
            return bool(fatias) and float(np.mean([f > lim for f in fatias])) < 0.35
        return voz < 1.15 * ruido and voz < 0.45 * voz_tipica

    for i in range(len(pal) - 1):
        if orig[i + 1][0] - orig[i][1] < gap:                               # só onde o Whisper deixou uma pausa longa
            continue
        g0, g1 = orig[i][1] + 0.15, orig[i + 1][0] - 0.15
        # a(s) palavra(s) antes da pausa, de trás pra frente, no máximo 3 ("eu pensei"), enquanto não tiverem voz
        grupo = []
        j = i
        while j >= 0 and len(grupo) < 3 and sem_voz(j, g0, g1) and (j == i or orig[j + 1][0] - orig[j][1] < 0.6):
            grupo.append(j)
            j -= 1
        fim = pal[i + 1]["ini"] - 0.03
        for j in grupo:                                                     # cola cada uma logo antes da seguinte
            d = min(0.35, 0.12 + 0.05 * len(pal[j]["p"]))
            movidas.append(f"{pal[j]['p']} {pal[j]['ini']:.2f}->{fim - d:.2f}")
            pal[j]["ini"], pal[j]["fim"] = round(fim - d, 3), round(fim, 3)
            fim = pal[j]["ini"] - 0.02
    for s in transcricao["segmentos"]:
        if s["palavras"]:
            s["ini"], s["fim"] = s["palavras"][0]["ini"], max(x["fim"] for x in s["palavras"])
    transcricao["realinhadas"] = list(reversed(movidas))
    return transcricao


def palavras(transcricao):
    """Lista plana de palavras [{p, ini, fim}] (usada pelas outras etapas)."""
    return [w for s in transcricao["segmentos"] for w in s["palavras"]]


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import comum
    comum.preparar()                                       # UTF-8 no terminal e o ffmpeg no PATH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio")
    ap.add_argument("--saida", required=True)
    ap.add_argument("--motor", choices=["whisper", "elevenlabs"], default="whisper")
    ap.add_argument("--modelo", default=None,
                    help="whisper: turbo (padrão, large-v3-turbo, ~1,6 GB), medium, small, base, large-v3; "
                         "elevenlabs: scribe_v2 (padrão)")
    ap.add_argument("--idioma", default="pt")
    ap.add_argument("--prompt", default=None, help="contexto opcional pro Whisper (título do vídeo, nomes próprios)")
    ap.add_argument("--so-realinhar", action="store_true",
                    help="não transcreve: só conserta as palavras soltas de uma transcrição que já existe (--saida)")
    a = ap.parse_args()
    t0 = time.time()
    if a.so_realinhar:
        r = realinhar_soltas(json.load(open(a.saida, encoding="utf-8")), a.audio)
        with open(a.saida, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        print(f"[transcrever] {len(r['realinhadas'])} palavras soltas realinhadas: {r['realinhadas']}")
        return
    if a.motor == "elevenlabs":
        r = transcrever_elevenlabs(a.audio, a.idioma, a.modelo or "scribe_v2")
    else:
        r = transcrever(a.audio, a.modelo or "turbo", a.idioma, a.prompt)
    r = realinhar_soltas(r, a.audio)
    r["segundos_gastos"] = round(time.time() - t0, 1)
    os.makedirs(os.path.dirname(os.path.abspath(a.saida)), exist_ok=True)
    with open(a.saida, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    n = sum(len(s["palavras"]) for s in r["segmentos"])
    print(f"[transcrever] {len(r['segmentos'])} segmentos, {n} palavras, {r['duracao'] / 60:.1f} min de áudio "
          f"em {r['segundos_gastos'] / 60:.1f} min ({r['modelo']}) -> {a.saida}")


if __name__ == "__main__":
    main()
