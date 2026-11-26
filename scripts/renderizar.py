"""VibeClipper, etapa 4 (edita): renderiza o corte final na vertical, em 1080p (1080x1920, Full HD), 30 fps, H.264 + AAC.

  python scripts/renderizar.py --fonte trabalho/corte-1/fonte.mp4 --plano trabalho/corte-1/plano.json \
        --legenda trabalho/corte-1/legenda.ass --sfx trabalho/corte-1/sfx.json --saida saida/corte-1.mp4
  python scripts/renderizar.py --fonte ... --plano ... --saida saida/corte-1-cru.mp4 --cru     (só cortado e enquadrado)

  - a moldura 9:16 de cada cena e os zooms em volta do rosto vêm do plano.json (enquadrar.py), quadro a quadro, sem tremer;
  - a legenda .ass (legendar.py) é queimada por cima;
  - o áudio original é normalizado (-14 LUFS, o padrão dos Shorts/Reels) e os efeitos (efeitos.py) entram por baixo;
  - usa a placa NVIDIA (h264_nvenc) se tiver; senão libx264.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import time

import numpy as np

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
from enquadrar import moldura  # noqa: E402
import visual  # noqa: E402

LARG, ALT, FPS = 1080, 1920, 30
CAM_LARG, CAM_RAIO, CAM_MARGEM, CAM_BAIXO = 340, 28, 40, 60   # gameplay: tamanho e posição da webcam no vertical (px)


def enc_video():
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "h264_nvenc" in r:
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "12M",
                "-maxrate", "20M", "-bufsize", "24M", "-profile:v", "high"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high"]


def faixa(plano):
    """A faixa vertical que o decodificador já recorta (fixa em cada cena): a moldura 9:16 mais o quanto ela anda na cena."""
    W = plano["W"]
    anda = max(c.get("x_max", c["x0"]) - c.get("x_min", c["x0"]) for c in plano["cenas"])
    bw = min(W, int(round(plano["moldura_w"] + anda)) // 2 * 2 + 4)
    xs = [min(max(int(c.get("x_min", c["x0"])), 0), W - bw) for c in plano["cenas"]]
    return bw, xs


def caminho_filtro(p, base):
    """Caminho pro filtro ass do ffmpeg: relativo à pasta de saída (sem o "C:" que o filtro entende errado); se estiver em
    outro disco, absoluto com o ":" escapado."""
    try:
        r = os.path.relpath(p, base)
    except ValueError:
        r = os.path.abspath(p)
    return r.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def x_por_cena(plano, xs):
    """Expressão do ffmpeg com o x da faixa de cada cena."""
    exp = str(xs[-1])
    for c, x in zip(reversed(plano["cenas"][:-1]), reversed(xs[:-1])):
        exp = f"if(lt(t\\,{c['fim']:.4f})\\,{x}\\,{exp})"
    return exp


def _suave(u):
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def chave_em(chaves, t, campos):
    """Valor dos `campos` no tempo t, suave entre os pontos-chave (o mesmo que o editor mostra)."""
    ks = chaves
    if t <= ks[0]["t"]:
        return [ks[0][c] for c in campos]
    if t >= ks[-1]["t"]:
        return [ks[-1][c] for c in campos]
    j = next(i for i in range(len(ks) - 1) if ks[i]["t"] <= t <= ks[i + 1]["t"])
    a, b = ks[j], ks[j + 1]
    u = _suave((t - a["t"]) / max(b["t"] - a["t"], 1e-6))
    return [a[c] + (b[c] - a[c]) * u for c in campos]


def mascara_arredondada(w, h, r):
    import cv2
    r = int(max(0, min(r, w // 2 - 1, h // 2 - 1)))
    mask = np.zeros((h, w), np.uint8)
    if r <= 0:
        mask[:] = 255
    else:
        cv2.rectangle(mask, (r, 0), (w - 1 - r, h - 1), 255, -1)
        cv2.rectangle(mask, (0, r), (w - 1, h - 1 - r), 255, -1)
        for px, py in ((r, r), (w - 1 - r, r), (r, h - 1 - r), (w - 1 - r, h - 1 - r)):
            cv2.circle(mask, (px, py), r, 255, -1, cv2.LINE_AA)
    return (mask.astype(np.float32) / 255.0)[:, :, None]


def colar(out, img_bgr, alfa, x, y):
    """Cola img (com alfa 0..1, HxWx1) em out na posição (x, y) do canto de cima à esquerda, cortando o que sair da tela."""
    h, w = img_bgr.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(out.shape[1], x + w), min(out.shape[0], y + h)
    if x1 <= x0 or y1 <= y0:
        return
    src = img_bgr[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.float32)
    a = alfa[y0 - y:y1 - y, x0 - x:x1 - x]
    reg = out[y0:y1, x0:x1].astype(np.float32)
    out[y0:y1, x0:x1] = (src * a + reg * (1 - a)).astype(np.uint8)


def imagem_rgba(arq):
    from PIL import Image
    im = Image.open(arq).convert("RGBA")
    a = np.array(im)
    return a[:, :, 2::-1].copy(), a[:, :, 3:4].astype(np.float32) / 255.0      # BGR, alfa


INCLINACAO = 0.2          # itálico: quanto o topo da letra anda pra direita (fração da altura)
FONTES_PESO = {"bold": ["arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"],
               "normal": ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf"]}


def fonte_texto(s, tam):
    """A fonte do texto por cima: 'black' (padrão) = a fonte forte da legenda; 'bold' e 'normal' = Arial."""
    from PIL import ImageFont
    peso = s.get("peso") or "black"
    cands = FONTES_PESO.get(peso, []) if peso != "black" else []
    cands = cands + [s.get("fonte_arq") or "arial.ttf", "arial.ttf"]
    for c in cands:
        try:
            return ImageFont.truetype(c, tam)
        except OSError:
            continue
    return ImageFont.load_default()


def texto_visivel(texto, frac, modo):
    """Quantos caracteres do texto já apareceram (letra por letra ou palavra por palavra), frac de 0 a 1."""
    if frac >= 1:
        return len(texto)
    if modo == "palavra":
        fins = [m.end() for m in re.finditer(r"\S+", texto)]
        k = math.ceil(frac * len(fins) - 1e-9)
        return fins[k - 1] if k > 0 else 0
    return math.ceil(frac * len(texto) - 1e-9)


def texto_rgba(s, n_vis=None):
    """Texto por cima (do editor): peso, itálico, alinhamento, contorno e fundo opcionais. n_vis = só os primeiros
    caracteres (efeito de digitação), sem mexer no layout. Devolve (BGR, alfa)."""
    from PIL import Image, ImageDraw
    tam = int(s.get("tam", 80))
    fnt = fonte_texto(s, tam)
    texto = str(s.get("texto", ""))
    linhas = texto.split("\n")
    borda = max(2, tam // 12)
    contorno = s.get("contorno", not s.get("fundo"))
    italico = bool(s.get("italico"))
    alin = s.get("alinhamento") or "centro"
    caixas = [fnt.getbbox(l or " ", stroke_width=borda) for l in linhas]
    lw = max(b[2] - b[0] for b in caixas)
    lh = int(tam * 1.2)
    pad = int(tam * 0.35) if s.get("fundo") else borda + 2
    extra = int(round(INCLINACAO * lh)) if italico else 0          # a inclinação precisa de espaço à direita
    W_, H_ = lw + 2 * pad + extra, lh * len(linhas) + 2 * pad
    im = Image.new("RGBA", (W_, H_), (0, 0, 0, 0))
    if s.get("fundo"):
        fc = s.get("cor_fundo", "#000000").lstrip("#")
        ImageDraw.Draw(im).rounded_rectangle((0, 0, W_ - 1, H_ - 1), radius=int(tam * 0.3),
                                             fill=(int(fc[0:2], 16), int(fc[2:4], 16), int(fc[4:6], 16), 230))
    camada = Image.new("RGBA", (W_, H_), (0, 0, 0, 0))
    d = ImageDraw.Draw(camada)
    cor = s.get("cor", "#FFFFFF").lstrip("#")
    rgb = (int(cor[0:2], 16), int(cor[2:4], 16), int(cor[4:6], 16), 255)
    resta = len(texto) if n_vis is None else n_vis
    for i, (l, b) in enumerate(zip(linhas, caixas)):
        largura = b[2] - b[0]
        if alin == "esquerda":
            x = pad - b[0]
        elif alin == "direita":
            x = W_ - extra - pad - largura - b[0]
        else:
            x = (W_ - extra - largura) / 2 - b[0]
        parte = l[:max(0, resta)]
        resta -= len(l) + 1                                         # +1 = a quebra de linha
        if not parte:
            continue
        alvo, dd = (Image.new("RGBA", (W_, H_), (0, 0, 0, 0)), None) if italico else (camada, d)
        (dd or ImageDraw.Draw(alvo)).text((x, pad + i * lh + (lh - tam) / 2 - b[1] * 0.2), parte, font=fnt, fill=rgb,
                                          stroke_width=borda if contorno else 0, stroke_fill=(0, 0, 0, 255))
        if italico:                                                 # inclina a linha: o topo anda pra direita, a base fica
            base = pad + (i + 1) * lh
            camada.alpha_composite(alvo.transform((W_, H_), Image.AFFINE, (1, INCLINACAO, -INCLINACAO * base, 0, 1, 0),
                                                  resample=Image.BICUBIC))
    im.alpha_composite(camada)
    a = np.array(im)
    return a[:, :, 2::-1].copy(), a[:, :, 3:4].astype(np.float32) / 255.0


def _ease(p):
    return 1 - (1 - p) ** 3


def _ease_back(p):
    c1 = 1.70158
    return 1 + (c1 + 1) * (p - 1) ** 3 + c1 * (p - 1) ** 2


def animacao(s, t):
    """Entrada e saída de imagem/texto do editor no tempo t: (opacidade, escala, deslocamento y, fração do texto,
    modo do texto). Igual à prévia do editor (index.html, animacaoMidia)."""
    legado = (s.get("anim") or "fade") == "fade"
    ent = s.get("entrada") or ("fade" if legado else "nenhuma")
    sai = s.get("saida") or ("fade" if legado else "nenhuma")
    d_in = max(0.05, float(s.get("dur_entrada") or 0.15))
    d_out = max(0.05, float(s.get("dur_saida") or 0.15))
    p_in = 1.0 if ent == "nenhuma" else min(1.0, max(0.0, (t - s["ini"]) / d_in))
    p_out = 1.0 if sai == "nenhuma" else min(1.0, max(0.0, (s["fim"] - t) / d_out))
    op, esc, dy, frac, modo = float(s.get("opacidade", 1.0)), 1.0, 0.0, 1.0, "letra"
    for tipo, p, sinal in ((ent, p_in, 1), (sai, p_out, -1)):
        if p >= 1 or tipo == "nenhuma":
            continue
        if tipo == "fade":
            op *= p
        elif tipo == "subir":
            op *= p
            dy += sinal * (1 - _ease(p)) * 90
        elif tipo == "zoom":
            op *= min(1.0, p * 2)
            esc *= max(0.05, 0.4 + 0.6 * (_ease_back(p) if sinal > 0 else _ease(p)))
        elif tipo in ("letra", "palavra"):
            if s.get("tipo") == "texto":
                frac, modo = min(frac, p), tipo
            else:
                op *= p
    return op, esc, dy, frac, modo


def renderizar(fonte, plano, saida, legenda=None, sfx=None, cru=False, ate=None):
    import cv2
    W, H = plano["W"], plano["H"]
    bw, xs = faixa(plano)
    dur = min(plano["dur"], ate) if ate else plano["dur"]
    nq = int(round(dur * FPS))
    crop = f"crop={bw}:{H}:'{x_por_cena(plano, xs)}':0"
    gp = plano.get("gameplay") or {}
    ex = {} if cru else (plano.get("extras") or {})   # do editor: trechos removidos, facecam animada, mídia por cima
    remover = sorted([float(a), float(b)] for a, b in ex.get("remover", []) if float(b) > float(a))
    tirado = sum(max(0.0, min(b, dur) - max(a, 0.0)) for a, b in remover)
    dur_saida = dur - tirado
    cam = gp.get("webcam")
    fc = gp.get("facecam") or {}                 # do editor: posição/tamanho da facecam no vertical, ou escondida
    estilo_cam = dict(fc)                         # forma, borda, sombra, brilho, filtro, espelho, zoom (visual.py)
    if fc.get("visivel") is False:
        cam = None
    fchaves = sorted(ex.get("facecam_chaves") or [], key=lambda k: k["t"])
    vis = ex.get("visual") or {}               # do editor: filtro de cor, vinheta, grão, barra de progresso
    fxs = [f for f in (ex.get("fx") or []) if float(f.get("fim", 0)) > float(f.get("ini", 0))]
    ww = wh = 0
    if cam:                                    # gameplay: a webcam sai no mesmo quadro, colada à direita da faixa do jogo
        cx, cy, cw_, ch_ = (int(round(v * s)) for v, s in zip(cam, (W, H, W, H)))
        larg_base = max(80, min(LARG, int(round((fc.get("larg") or CAM_LARG) / 2)) * 2))
        larg_max = max([larg_base] + [int(k["larg"]) for k in fchaves])
        ww = max(80, min(LARG, int(round(larg_max / 2)) * 2))              # decodifica no maior tamanho e reduz por quadro
        wh = min(int(round(ww * ch_ / max(cw_, 1) / 2)) * 2, H, ALT)
        aspecto = wh / ww
        dec = ["ffmpeg", "-nostdin", "-v", "error", "-i", fonte, "-t", f"{dur:.3f}", "-filter_complex",
               f"[0:v]fps={FPS},split[a][b];[a]{crop}[g];[b]crop={cw_}:{ch_}:{cx}:{cy},"
               f"scale={ww}:{wh}:flags=lanczos,pad={ww}:{H}:0:0[c];[g][c]hstack", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        raio = CAM_RAIO if fc.get("raio") is None else fc["raio"]
        hb = int(round(larg_base * aspecto / 2)) * 2
        cam_x, cam_y = LARG - CAM_MARGEM - larg_base, ALT - CAM_BAIXO - hb
        if fc.get("x") is not None and fc.get("y") is not None:      # None = posição padrão
            cam_x, cam_y = int(fc["x"]), int(fc["y"])
    else:
        dec = ["ffmpeg", "-nostdin", "-v", "error", "-i", fonte, "-t", f"{dur:.3f}", "-vf",
               f"fps={FPS},{crop}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    pasta = os.path.dirname(os.path.abspath(saida))
    os.makedirs(pasta, exist_ok=True)
    vf = "null"
    if legenda and not cru:
        meta = os.path.splitext(legenda)[0] + ".json"
        fontsdir = json.load(open(meta, encoding="utf-8"))["pasta_fonte"] if os.path.exists(meta) else None
        vf = f"ass='{caminho_filtro(legenda, pasta)}'" + (f":fontsdir='{caminho_filtro(fontsdir, pasta)}'" if fontsdir else "")
    entradas = ["-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{LARG}x{ALT}", "-r", str(FPS), "-i", "-",
                "-i", os.path.abspath(fonte)]
    sons = []
    if sfx and not cru:                       # os tempos do sfx já estão no tempo do vídeo final (edicao.py remapeia)
        d = json.load(open(sfx, encoding="utf-8"))
        sons = [dict(s, arq=os.path.join(d["pasta_sons"], s["som"])) for s in d["sons"] if s["t"] < dur_saida]
    for a in ex.get("audios", []):            # áudio extra do editor (música, efeito): tempo do vídeo final
        if os.path.exists(a["arquivo"]) and a["t"] < dur_saida:
            sons.append({"t": float(a["t"]), "arq": a["arquivo"], "ganho_db": float(a.get("volume_db", 0)),
                         "ini_arq": float(a.get("ini_arq", 0)), "dur": a.get("dur"), "fade": a.get("fade", True)})
    for s in sons:
        if s.get("ini_arq"):
            entradas += ["-ss", f"{s['ini_arq']:.3f}"]
        entradas += ["-i", os.path.abspath(s["arq"])]
    sel = ""
    if remover:                               # tira o som dos trechos removidos (o vídeo pula os quadros deles)
        cond = "+".join(f"between(t\\,{a:.3f}\\,{b:.3f})" for a, b in remover)
        sel = f"aselect='not({cond})',asetpts=N/SR/TB,"
    voz = f"[1:a]{sel}aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    if sons:
        fc = [f"{voz}[voz]"]
        for i, s in enumerate(sons):
            ms = int(round(s["t"] * 1000))
            corte = ""
            if s.get("dur"):
                dd = float(s["dur"])
                corte = f"atrim=0:{dd:.3f}," + (f"afade=t=out:st={max(0.0, dd - 0.4):.3f}:d=0.4," if s.get("fade") else "")
            elif s.get("fade"):
                corte = f"atrim=0:{max(0.1, dur_saida - s['t']):.3f},"
            fc.append(f"[{i + 2}:a]aresample=48000,{corte}volume={s['ganho_db']}dB,adelay={ms}|{ms}[s{i}]")
        fc.append("[voz]" + "".join(f"[s{i}]" for i in range(len(sons))) +
                  f"amix=inputs={len(sons) + 1}:normalize=0:duration=first,alimiter=limit=0.95[a]")
    else:
        fc = [f"{voz},alimiter=limit=0.95[a]"]
    fc.append(f"[0:v]{vf},format=yuv420p[v]")
    enc = ["ffmpeg", "-nostdin", "-v", "error", "-y", *entradas, "-filter_complex", ";".join(fc), "-map", "[v]",
           "-map", "[a]", "-t", f"{dur_saida:.3f}", *enc_video(), "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
           "-ar", "48000", "-movflags", "+faststart", os.path.basename(saida)]
    # mídia por cima (imagens e textos do editor), carregada uma vez
    sobre = []
    for s in ex.get("sobreposicoes", []):
        try:
            img, al = imagem_rgba(s["arquivo"]) if s.get("tipo") == "imagem" else texto_rgba(s)
        except (OSError, ValueError) as e:
            print(f"[renderizar] aviso: não deu pra usar {s.get('arquivo') or s.get('texto')} ({e})", file=sys.stderr)
            continue
        if s.get("tipo") == "imagem":
            lw = max(8, int(s.get("larg", 400)))
            lh = max(8, int(round(img.shape[0] * lw / img.shape[1])))
            img = cv2.resize(img, (lw, lh), interpolation=cv2.INTER_AREA)
            al = cv2.resize(al, (lw, lh), interpolation=cv2.INTER_AREA)[:, :, None]
        sobre.append({**s, "img": img, "alfa": al, "parcial": {}})
    lq = bw + ww
    pd = subprocess.Popen(dec, stdout=subprocess.PIPE, bufsize=lq * H * 3 * 2)
    pe = subprocess.Popen(enc, stdin=subprocess.PIPE, cwd=pasta)
    tam = lq * H * 3
    t0 = time.time()
    k = escritos = 0
    while k < nq:
        b = pd.stdout.read(tam)
        if len(b) < tam:
            break
        t = k / FPS
        k += 1
        if remover and any(a <= t < bb for a, bb in remover):              # trecho removido no editor: pula o quadro
            continue
        q = np.frombuffer(b, np.uint8).reshape(H, lq, 3)
        wc = q[:wh, bw:] if cam else None
        q = q[:, :bw] if cam else q
        at = visual.ativos(fxs, t) if fxs else []          # efeitos no tempo agindo neste quadro
        x, y, w, h, _ = moldura(plano, t, sem_zoom=cru)
        ic = next((i for i, c in enumerate(plano["cenas"]) if c["ini"] <= t < c["fim"]), len(plano["cenas"]) - 1)
        x0 = xs[ic]
        s = LARG / w
        m = np.float32([[s, 0, -(x - x0) * s], [0, s, -y * s]])
        if at:                                 # tremor e zoom pulso: mexem só na camada do vídeo
            sc, dx, dy = visual.transformacao(at, t)
            if abs(sc - 1) > 1e-4 or dx or dy:
                A = np.float64([[sc, 0, (1 - sc) * LARG / 2 + dx], [0, sc, (1 - sc) * ALT / 2 + dy], [0, 0, 1]])
                m = (A @ np.vstack([m, [0, 0, 1]]))[:2].astype(np.float32)
                s *= sc
        out = cv2.warpAffine(q, m, (LARG, ALT), flags=cv2.INTER_LINEAR if s < 1 else cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_REPLICATE)
        topo, base = int(np.ceil(max(0.0, float(m[1, 2])))), int(min(ALT, np.floor(float(m[1, 1]) * H + float(m[1, 2]))))
        if topo > 0 or base < ALT:             # câmera aberta (gameplay): em cima e embaixo, o próprio jogo desfocado
            meio = out[topo:base]
            fundo = cv2.resize(cv2.resize(meio, (LARG // 24, ALT // 24), interpolation=cv2.INTER_AREA), (LARG, ALT),
                               interpolation=cv2.INTER_LINEAR)
            fundo = cv2.GaussianBlur(fundo, (0, 0), 9) * 0.55
            out[:topo] = fundo[:topo].astype(np.uint8)
            out[base:] = fundo[base:].astype(np.uint8)
        if at:
            out = visual.desfoque(out, at)
        if cam:                                # webcam, cantos arredondados (posição/tamanho podem mudar no tempo)
            if fchaves:
                fx, fy, fl = chave_em(fchaves, t, ("x", "y", "larg"))
            else:
                fx, fy, fl = cam_x, cam_y, larg_base
            lw = max(40, min(ww, int(round(fl / 2)) * 2))
            lh = max(2, int(round(lw * aspecto / 2)) * 2)
            img = wc if lw == ww else cv2.resize(wc, (lw, lh), interpolation=cv2.INTER_AREA)
            visual.colar_facecam(out, img[:lh, :lw], int(round(fx)), int(round(fy)),
                                 raio * lw / max(larg_base, 1) if fchaves else raio, estilo_cam)
        if vis or at:                          # filtro de cor, glitch, vinheta e grão (o quadro todo)
            out = visual.aplicar_filtro(out, visual.filtro_no_tempo(at, vis.get("filtro")))
            if at:
                out = visual.glitch(out, at, t)
            out = visual.vinheta(out, float(vis.get("vinheta") or 0))
            out = visual.grao(out, float(vis.get("grao") or 0), k)
        for s in sobre:                        # imagens e textos por cima (centro em x, y; entrada e saída animadas)
            if not (s["ini"] <= t < s["fim"]):
                continue
            op, esc, dy, frac, modo = animacao(s, t)
            img, al = s["img"], s["alfa"]
            if frac < 1:                       # digitando: só os primeiros caracteres (o layout não muda)
                n = texto_visivel(str(s.get("texto", "")), frac, modo)
                if n not in s["parcial"]:
                    s["parcial"][n] = texto_rgba(s, n)
                img, al = s["parcial"][n]
            if abs(esc - 1) > 1e-3:
                nw, nh = max(2, int(img.shape[1] * esc)), max(2, int(img.shape[0] * esc))
                img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if esc < 1 else cv2.INTER_LINEAR)
                al = cv2.resize(al, (nw, nh), interpolation=cv2.INTER_AREA if esc < 1 else cv2.INTER_LINEAR)
                al = al if al.ndim == 3 else al[:, :, None]
            if op <= 0.003:
                continue
            ih, iw = img.shape[:2]
            colar(out, img, al * op, int(round(s["x"] - iw / 2)), int(round(s["y"] + dy - ih / 2)))
        if at:                                 # flash branco e fade pro preto
            out = visual.sobreposicao_cor(out, at)
        if vis.get("barra"):                   # barra de progresso (no tempo do vídeo final)
            out = visual.barra(out, (escritos + 1) / max(1.0, dur_saida * FPS), vis.get("cor_barra") or "#FF4D3D",
                               vis.get("pos_barra") or "base")
        pe.stdin.write(out.tobytes())
        escritos += 1
    pe.stdin.close()
    pe.wait()
    pd.stdout.close()
    pd.wait()
    if pe.returncode != 0:
        raise SystemExit(f"[renderizar] o ffmpeg falhou ({saida})")
    print(f"[renderizar] {saida}: {escritos} quadros ({escritos / FPS:.1f} s) em {time.time() - t0:.0f} s"
          f"{' (cru)' if cru else ''}, {len(sons)} sons" + (f", {tirado:.1f} s removidos" if tirado else ""))


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import comum
    comum.preparar()                                       # UTF-8 no terminal e o ffmpeg no PATH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fonte", required=True)
    ap.add_argument("--plano", required=True)
    ap.add_argument("--saida", required=True)
    ap.add_argument("--legenda", default=None)
    ap.add_argument("--sfx", default=None)
    ap.add_argument("--cru", action="store_true", help="só cortado e enquadrado: sem zoom, sem legenda, sem efeito")
    ap.add_argument("--ate", type=float, default=None)
    a = ap.parse_args()
    plano = json.load(open(a.plano, encoding="utf-8"))
    renderizar(a.fonte, plano, a.saida, a.legenda, a.sfx, a.cru, a.ate)


if __name__ == "__main__":
    main()
