"""VibeClipper, editor: aplica o que a pessoa mudou no editor (editor.py) e renderiza o corte de novo.

O editor grava T/corte-N/edicao.json:
  {
    "camera":  {"modo": "auto" | "manual", "chaves": [{"t": s, "cx": 0 a 1, "z": zoom}]},
    "legenda": {"cor": "#FA3F32", "tam": 100, "y": 1305, "grupos": [{"ini": s, "fim": s, "texto": "...", "enfase": [...]}]},
    "facecam": {"visivel": true, "x": px, "y": px, "larg": px, "raio": px},
    "efeitos": true
  }
  - camera manual: a câmera passa pelos pontos-chave (centro x e zoom), com movimento suave entre eles; z < 1 abre
    (fundo desfocado em cima e embaixo), z > 1 aproxima;
  - legenda: os blocos de texto do jeito que a pessoa editou (texto, tempo, palavras de destaque), cor, tamanho e altura;
  - facecam: posição e tamanho no vertical 1080x1920 (ou escondida).

  python scripts/edicao.py T 2 --saida O/corte-2.mp4      (o editor chama isto no botão Renderizar)
"""
import argparse
import json
import os
import re
import shutil
import sys

import numpy as np

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
LARG, ALT = 1080, 1920


def _suave(u):
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def camera_de_chaves(chaves, dur, passo=0.1):
    """Pontos-chave -> caminho [[t, cx, z]] a cada `passo` s, suave entre as chaves (o mesmo que o preview do editor)."""
    ks = sorted(({"t": float(k["t"]), "cx": float(k["cx"]), "z": float(k["z"])} for k in chaves), key=lambda k: k["t"])
    if not ks:
        return []
    out = []
    for t in np.arange(0.0, dur + passo / 2, passo):
        if t <= ks[0]["t"]:
            cx, z = ks[0]["cx"], ks[0]["z"]
        elif t >= ks[-1]["t"]:
            cx, z = ks[-1]["cx"], ks[-1]["z"]
        else:
            j = next(i for i in range(len(ks) - 1) if ks[i]["t"] <= t <= ks[i + 1]["t"])
            a, b = ks[j], ks[j + 1]
            u = _suave((t - a["t"]) / max(b["t"] - a["t"], 1e-6))
            cx, z = a["cx"] + (b["cx"] - a["cx"]) * u, a["z"] + (b["z"] - a["z"]) * u
        out.append([round(float(t), 3), round(cx, 4), round(min(max(z, 0.35), 3.0), 4)])
    return out


def hex_ass(cor):
    c = cor.lstrip("#")
    return f"&H00{c[4:6]}{c[2:4]}{c[0:2]}".upper()


def tempo_ass(s):
    cs = int(round(max(0.0, s) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _limpa(p):
    return re.sub(r"[,;:.…]+$", "", p).upper().replace("{", "(").replace("}", ")")


def legenda_de_grupos(leg, meta, dur):
    """Monta o .ass (mesmo estilo do legendar.py) com os blocos que a pessoa editou. Devolve (ass, meta com os grupos)."""
    from PIL import ImageFont
    fonte_arq = os.path.join(meta["pasta_fonte"], meta["fonte"])
    fonte_nome = meta["fonte_nome"]
    tam = int(leg.get("tam", 100))
    cor = leg.get("cor") or meta["cor"]
    y_glob = int(leg.get("y", 1305))
    fnt = ImageFont.truetype(fonte_arq, tam)
    linhas = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {LARG}", f"PlayResY: {ALT}", "WrapStyle: 2",
              "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
              "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
              "MarginR, MarginV, Encoding",
              f"Style: Corte,{fonte_nome},{tam},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,1,0,1,7,3,5,"
              "40,40,0,1", "", "[Events]",
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    grupos = []
    cor_ass = hex_ass(cor)
    for g in sorted(leg.get("grupos", []), key=lambda g: g["ini"]):
        pal = [_limpa(p) for p in str(g.get("texto", "")).split() if _limpa(p)]
        if not pal:
            continue
        ini, fim = max(0.0, float(g["ini"])), min(float(g["fim"]), dur)
        if fim - ini < 0.08:
            continue
        enf = {_limpa(e) for e in g.get("enfase", [])}
        texto = " ".join(pal)
        larg = fnt.getlength(texto) + 14 * len(pal)
        fs = tam if larg <= 960 else int(tam * 960 / larg)
        y = int(g.get("y", y_glob))
        partes = ["{\\c" + cor_ass + "&}" + p + "{\\c&H00FFFFFF&}" if (p in enf or re.search(r"\d", p)) else p for p in pal]
        anim = "{\\an5\\pos(%d,%d)\\fs%d\\fscx86\\fscy86\\t(0,90,\\fscx100\\fscy100)}" % (LARG // 2, y, fs)
        linhas.append(f"Dialogue: 0,{tempo_ass(ini)},{tempo_ass(fim)},Corte,,0,0,0,,{anim}{' '.join(partes)}")
        grupos.append({"ini": round(ini, 3), "fim": round(fim, 3), "texto": texto, "y": y, "fs": fs,
                       "enfase": [p for p in pal if p in enf or re.search(r"\d", p)]})
    novo = dict(meta, cor=cor, grupos=grupos, editada=True)
    return "\n".join(linhas) + "\n", novo


def trechos_removidos(ed, dur):
    rs = []
    for r in ed.get("remover", []) or []:
        a, b = max(0.0, float(r[0])), min(float(r[1]), dur)
        if b - a >= 0.05:
            rs.append([a, b])
    rs.sort()
    juntos = []
    for a, b in rs:                                                         # junta os que se encostam
        if juntos and a <= juntos[-1][1]:
            juntos[-1][1] = max(juntos[-1][1], b)
        else:
            juntos.append([a, b])
    return juntos


def no_final(t, remover):
    """Tempo do corte original -> tempo no vídeo final (descontando os trechos removidos); None se t foi removido."""
    tirado = 0.0
    for a, b in remover:
        if t >= b:
            tirado += b - a
        elif t >= a:
            return None
    return t - tirado


def remapear_grupos(grupos, remover):
    out = []
    for g in grupos:
        ini, fim = g["ini"], g["fim"]
        for a, b in remover:                                                # encolhe o bloco que encosta num trecho tirado
            if a <= ini < b:
                ini = b
            if a < fim <= b:
                fim = a
        if fim - ini < 0.08:
            continue
        i2, f2 = no_final(ini, remover), no_final(fim - 1e-4, remover)
        if i2 is None or f2 is None or f2 <= i2:
            continue
        out.append(dict(g, ini=round(i2, 3), fim=round(f2 + 1e-4, 3)))
    return out


def renderizar_editado(T, n, saida):
    import enquadrar
    import efeitos
    import renderizar
    pasta = os.path.join(T, f"corte-{n}")
    plano = json.load(open(os.path.join(pasta, "plano.json"), encoding="utf-8"))
    meta = json.load(open(os.path.join(pasta, "legenda.json"), encoding="utf-8"))
    ed = json.load(open(os.path.join(pasta, "edicao.json"), encoding="utf-8"))
    cam = ed.get("camera") or {}
    if cam.get("modo") == "manual" and cam.get("chaves"):
        enquadrar.aplicar_camera(plano, camera_de_chaves(cam["chaves"], plano["dur"]), "editor: câmera manual",
                                 manual=True)
    if ed.get("facecam"):
        plano.setdefault("gameplay", {})["facecam"] = {k: v for k, v in ed["facecam"].items() if k != "chaves"}
    remover = trechos_removidos(ed, plano["dur"])
    pasta_midias = os.path.join(T, "midias")
    fonte_arq = os.path.join(meta["pasta_fonte"], meta["fonte"])
    sobre, audios = [], []
    for m in ed.get("midias", []) or []:
        tipo = m.get("tipo")
        if tipo in ("imagem", "audio"):
            arq = os.path.join(pasta_midias, os.path.basename(m.get("arquivo", "")))
            if not os.path.isfile(arq):
                print(f"[edicao] aviso: arquivo não encontrado: {m.get('arquivo')}", file=sys.stderr)
                continue
        if tipo == "imagem":
            sobre.append(dict(m, arquivo=arq))
        elif tipo == "texto":
            sobre.append(dict(m, fonte_arq=fonte_arq))
        elif tipo == "audio":
            t = float(m.get("t", 0))
            for a, b in remover:                                            # começa num trecho tirado: começa logo depois
                if a <= t < b:
                    t = b
            t2 = no_final(t, remover)
            if t2 is not None:
                audios.append({"arquivo": arq, "t": round(t2, 3), "volume_db": m.get("volume_db", -6),
                               "ini_arq": m.get("ini_arq", 0), "dur": m.get("dur"), "fade": m.get("fade", True)})
    plano["extras"] = {"remover": remover, "facecam_chaves": (ed.get("facecam") or {}).get("chaves") or [],
                       "sobreposicoes": sobre, "audios": audios}
    arq_plano = os.path.join(pasta, "plano-editado.json")
    json.dump(plano, open(arq_plano, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    leg = ed.get("legenda")
    if leg and leg.get("grupos") is not None:
        leg_final = dict(leg, grupos=remapear_grupos(leg["grupos"], remover))
    else:
        leg_final = {"cor": meta["cor"], "tam": 100, "y": meta["grupos"][0]["y"] if meta.get("grupos") else 1305,
                     "grupos": remapear_grupos(meta.get("grupos", []), remover)}
    dur_final = plano["dur"] - sum(b - a for a, b in remover)
    ass, meta2 = legenda_de_grupos(leg_final, meta, dur_final)
    arq_ass = os.path.join(pasta, "legenda-editada.ass")
    open(arq_ass, "w", encoding="utf-8").write(ass)
    json.dump(meta2, open(os.path.splitext(arq_ass)[0] + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    sfx = []
    if ed.get("efeitos", True):                     # whoosh nos zooms (tempo do original) e pop nos destaques (já no final)
        for s in efeitos.planejar(plano, None):
            t2 = no_final(s["t"], remover)
            if t2 is not None:
                sfx.append(dict(s, t=round(t2, 3)))
        sfx += [s for s in efeitos.planejar({"zooms": []}, meta2)]
        sfx.sort(key=lambda s: s["t"])
    arq_sfx = os.path.join(pasta, "sfx-editado.json")
    json.dump({"pasta_sons": efeitos.SONS, "sons": sfx}, open(arq_sfx, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # guarda a versão automática (a 1ª) antes de sobrescrever
    if os.path.exists(saida):
        orig = os.path.join(os.path.dirname(saida), "automatico", os.path.basename(saida))
        if not os.path.exists(orig):
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            shutil.copyfile(saida, orig)
    renderizar.renderizar(os.path.join(pasta, "fonte.mp4"), plano, saida, arq_ass, arq_sfx)
    return saida


def main():
    import comum
    comum.preparar()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trabalho", help="a pasta do vídeo em trabalho/ (T)")
    ap.add_argument("n", type=int)
    ap.add_argument("--saida", required=True)
    a = ap.parse_args()
    renderizar_editado(a.trabalho, a.n, a.saida)


if __name__ == "__main__":
    main()
