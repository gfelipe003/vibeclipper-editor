"""VibeClipper, gameplay: acha os personagens do jogo (gente em pé na tela) pra câmera do corte vertical seguir a ação.

Usado pelo enquadrar.py --gameplay. Detector de pessoas YOLOX-S (OpenCV Zoo, licença Apache 2.0), rodando no OpenCV.

  - o SEU personagem (3ª pessoa) é a pessoa grande perto do centro do jogo;
  - qualquer OUTRO personagem em pé (inimigo, outro jogador, soldado) que aparece na tela puxa a câmera: ela desliza pra
    mostrar você E ele; se os dois não cabem no 9:16, ela abre (zoom pra trás, fundo desfocado em cima e embaixo);
  - a webcam do streamer, corpos no chão e o HUD não contam.

  python scripts/personagens.py T/corte-2/fonte.mp4      (teste: mostra o que achou a cada 0,2 s)
"""
import json
import os
import subprocess
import sys

import numpy as np

MODELO = "object_detection_yolox_2022nov.onnx"
URL = "https://github.com/opencv/opencv_zoo/raw/main/models/object_detection_yolox/object_detection_yolox_2022nov.onnx"
AQUI = os.path.dirname(os.path.abspath(__file__))


def achar_modelo():
    for d in (os.environ.get("VIBECLIPPER_MODELOS", ""), os.path.join(AQUI, "..", "modelos"),
              os.path.join(os.path.expanduser("~"), ".cache", "vibeclipper")):
        p = os.path.join(d, MODELO)
        if d and os.path.exists(p):
            return os.path.abspath(p)
    return None


class DetectorPessoas:
    """YOLOX-S 640x640: devolve pessoas [x, y, w, h, confiança] normalizadas (0 a 1) no quadro dado."""

    def __init__(self, tam=640):
        import cv2
        self.cv2 = cv2
        arq = achar_modelo()
        if not arq:
            raise FileNotFoundError(f"modelo {MODELO} não encontrado (rode o instalar.py)")
        self.net = cv2.dnn.readNet(arq)
        self.tam = tam
        grades, passos = [], []
        for s in (8, 16, 32):
            n = tam // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grades.append(np.stack((xv, yv), 2).reshape(-1, 2))
            passos.append(np.full((n * n, 1), s))
        self.grade = np.concatenate(grades).astype(np.float32)
        self.passo = np.concatenate(passos).astype(np.float32)

    def pessoas(self, bgr, conf=0.35):
        cv2 = self.cv2
        h, w = bgr.shape[:2]
        r = min(self.tam / h, self.tam / w)
        img = np.full((self.tam, self.tam, 3), 114, np.uint8)
        rz = cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), (int(w * r), int(h * r)))
        img[:rz.shape[0], :rz.shape[1]] = rz
        self.net.setInput(np.transpose(img, (2, 0, 1))[None].astype(np.float32))
        d = self.net.forward()[0]
        xy = (d[:, :2] + self.grade) * self.passo
        wh = np.exp(d[:, 2:4]) * self.passo
        sc = d[:, 4] * d[:, 5]                                              # classe 0 do COCO = pessoa
        ok = sc >= conf
        if not ok.any():
            return []
        xy, wh, sc = xy[ok], wh[ok], sc[ok]
        caixas = [[float(cx - bw / 2), float(cy - bh / 2), float(bw), float(bh)] for (cx, cy), (bw, bh) in zip(xy, wh)]
        idx = cv2.dnn.NMSBoxes(caixas, sc.tolist(), conf, 0.45)
        out = []
        for i in np.array(idx).reshape(-1):
            x, y, bw, bh = caixas[i]
            out.append([x / r / w, y / r / h, bw / r / w, bh / r / h, float(sc[i])])
        return out


def analisar(arq, a_cada=0.2, larg=1280, webcam=None):
    """Pessoas a cada `a_cada` s (sem a webcam, sem gente deitada e sem coisa minúscula)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "json", arq], capture_output=True, text=True)
    s = json.loads(r.stdout)["streams"][0]
    W, H = int(s["width"]), int(s["height"])
    alt = int(round(larg * H / W / 2) * 2)
    det = DetectorPessoas()
    p = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", arq, "-vf",
                          f"fps={1 / a_cada:.4f},scale={larg}:{alt}:flags=area", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         stdout=subprocess.PIPE, bufsize=larg * alt * 3 * 2)
    tam = larg * alt * 3
    amostras, k = [], 0
    while True:
        b = p.stdout.read(tam)
        if len(b) < tam:
            break
        q = np.frombuffer(b, np.uint8).reshape(alt, larg, 3)
        ps = []
        for x, y, w, h, c in det.pessoas(q):
            cx, cy = x + w / 2, y + h / 2
            if webcam and webcam[0] - 0.01 <= cx <= webcam[0] + webcam[2] + 0.01 and \
                    webcam[1] - 0.01 <= cy <= webcam[1] + webcam[3] + 0.01:
                continue                                                     # o streamer na webcam
            if h < 0.07 or w * W > 1.3 * h * H:                              # minúsculo ou deitado (corpo no chão)
                continue
            ps.append([round(v, 4) for v in (x, y, w, h, c)])
        amostras.append({"t": round(k * a_cada, 3), "pessoas": ps})
        k += 1
    p.wait()
    return amostras


def _iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return inter / max(a[2] * a[3] + b[2] * b[3] - inter, 1e-6)


def caminho_da_camera(amostras, centro, frac_cw, aspecto_cw, abrir_max=1.9, segura=1.2, suave=0.7):
    """Pra cada amostra: (t, centro x da moldura, z), z < 1 = câmera aberta. frac_cw = largura da moldura 9:16 / largura do
    vídeo. Sem ninguém além de você: o centro padrão do jogo. Com outro personagem: mostra os dois (e o foco é nele)."""
    pts = []
    eu_ant = None
    ultimo_outro, caixa_outros = -99.0, None
    vistos = []                                                              # outros vistos nas últimas amostras (0/1)
    for a in amostras:
        ps = a["pessoas"]
        # você: a pessoa grande perto do centro do jogo (e perto de onde estava)
        def nota(p):
            cx, cy = p[0] + p[2] / 2, p[1] + p[3] / 2
            ref = (eu_ant[0] + eu_ant[2] / 2) if eu_ant else centro
            return p[3] * np.exp(-abs(cx - ref) / 0.18) * (1.0 if 0.12 < cx < 0.75 and cy > 0.3 else 0.2)
        eu = max(ps, key=nota) if ps else None
        if eu is not None and (eu[3] < 0.18 or nota(eu) < 0.05):
            eu = None
        outros = [p for p in ps if eu is None or (_iou(p, eu) < 0.25 and p is not eu)]
        outros = [p for p in outros if p[4] >= 0.45]
        if eu is not None:
            eu_ant = eu
        vistos = (vistos + [1 if outros else 0])[-3:]
        if outros and sum(vistos) >= 2:                                      # 2 de 3 amostras: não é piscada do detector
            x0 = min(p[0] for p in outros)
            x1 = max(p[0] + p[2] for p in outros)
            caixa_outros = (x0, x1)
            ultimo_outro = a["t"]
        # o que precisa caber na tela
        if eu is not None:
            e0, e1 = eu[0], eu[0] + eu[2]
        else:
            e0, e1 = centro - frac_cw * 0.3, centro + frac_cw * 0.3
        if caixa_outros is not None and a["t"] - ultimo_outro <= segura:
            l0, l1 = min(e0, caixa_outros[0]), max(e1, caixa_outros[1])
            larg = (l1 - l0) * 1.12
            larg = min(max(larg, frac_cw), frac_cw * abrir_max)
            c = (l0 + l1) / 2
            if (l1 - l0) * 1.12 > larg:                                      # não cabe nem aberta: o foco é no outro
                oc = (caixa_outros[0] + caixa_outros[1]) / 2
                c = oc + np.clip(((e0 + e1) / 2) - oc, -larg * 0.38, larg * 0.38)
            z = frac_cw / larg
        else:
            c = centro if eu is None else 0.5 * centro + 0.5 * ((e0 + e1) / 2)   # segue você de leve
            z = 1.0
        pts.append([a["t"], float(c), float(z)])
    if not pts:
        return []
    # suaviza (média móvel de ~0,7 s) pra câmera deslizar, sem tremer
    k = max(1, int(round(suave / max(amostras[1]["t"] - amostras[0]["t"], 1e-3)))) if len(amostras) > 1 else 1
    arr = np.array(pts)
    for col in (1, 2):
        v = arr[:, col]
        pad = np.pad(v, (k, k), mode="edge")
        arr[:, col] = np.convolve(pad, np.ones(2 * k + 1) / (2 * k + 1), mode="same")[k:-k]
    return [[round(t, 3), round(c, 4), round(min(z, 1.0), 4)] for t, c, z in arr.tolist()]


if __name__ == "__main__":
    sys.path.insert(0, AQUI)
    import comum
    comum.preparar()
    for am in analisar(sys.argv[1]):
        if am["pessoas"]:
            print(am["t"], am["pessoas"])
