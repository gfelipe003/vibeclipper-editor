"""VibeClipper: efeitos visuais do corte (usados pelo renderizar.py; o editor desenha a prévia com as mesmas contas).

No edicao.json:
  "visual":  {"filtro": "nenhum"|"vivido"|"cinema"|"quente"|"frio"|"pb"|"dramatico"|"vintage",
              "vinheta": 0..1, "grao": 0..1, "barra": bool, "cor_barra": "#FF4D3D", "pos_barra": "base"|"topo"}
  "fx":      [{"tipo": "flash"|"tremor"|"pulso"|"glitch"|"desfoque"|"filtro"|"entrada_preto"|"saida_preto",
               "ini": s, "fim": s, "forca": 0..1, "filtro": "pb"}]           (tempo do corte original)
  "facecam": {..., "forma": "retangulo"|"circulo", "borda": px, "cor_borda": "#FFFFFF", "sombra": bool,
              "brilho": bool, "filtro": nome, "espelhar": bool, "zoom": 1..2}

Os filtros de cor seguem as fórmulas dos filtros CSS (saturate, contrast, brightness, sepia, grayscale) mais uma tinta
multiplicada, pra prévia do editor (ctx.filter) e o vídeo final ficarem iguais.
"""
import math

import numpy as np

LARG, ALT = 1080, 1920

FILTROS = {
    "nenhum": {"css": [], "tinta": None},
    "vivido": {"css": [("saturate", 1.4), ("contrast", 1.08)], "tinta": None},
    "cinema": {"css": [("contrast", 1.12), ("saturate", 0.85), ("brightness", 1.04)], "tinta": ("#A8D8DC", 0.22)},
    "quente": {"css": [("saturate", 1.15), ("contrast", 1.04), ("brightness", 1.08)], "tinta": ("#FFD6A0", 0.32)},
    "frio": {"css": [("saturate", 0.95), ("brightness", 1.08)], "tinta": ("#B4D0FF", 0.32)},
    "pb": {"css": [("grayscale", 1.0), ("contrast", 1.18)], "tinta": None},
    "dramatico": {"css": [("contrast", 1.32), ("saturate", 1.12), ("brightness", 0.92)], "tinta": None},
    "vintage": {"css": [("contrast", 0.88), ("saturate", 0.72), ("sepia", 0.28), ("brightness", 1.08)], "tinta": None},
}


def _rgb(cor):
    c = (cor or "#FFFFFF").lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _matriz_css(op, a):
    """Matriz 3x4 (RGB, 0..1) de um filtro CSS."""
    I = np.eye(3)
    off = np.zeros(3)
    if op == "saturate":
        s = a
        M = np.array([[0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s],
                      [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s],
                      [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s]])
    elif op == "grayscale":
        b = 1 - min(max(a, 0), 1)
        M = np.array([[0.2126 + 0.7874 * b, 0.7152 - 0.7152 * b, 0.0722 - 0.0722 * b],
                      [0.2126 - 0.2126 * b, 0.7152 + 0.2848 * b, 0.0722 - 0.0722 * b],
                      [0.2126 - 0.2126 * b, 0.7152 - 0.7152 * b, 0.0722 + 0.9278 * b]])
    elif op == "sepia":
        b = 1 - min(max(a, 0), 1)
        M = np.array([[0.393 + 0.607 * b, 0.769 - 0.769 * b, 0.189 - 0.189 * b],
                      [0.349 - 0.349 * b, 0.686 + 0.314 * b, 0.168 - 0.168 * b],
                      [0.272 - 0.272 * b, 0.534 - 0.534 * b, 0.131 + 0.869 * b]])
    elif op == "contrast":
        M, off = I * a, np.full(3, 0.5 - 0.5 * a)
    elif op == "brightness":
        M = I * a
    else:
        M = I
    return np.hstack([M, off[:, None]])


_cache_filtro = {}


def matriz_filtro(nome):
    """Matriz 3x4 pro cv2.transform em BGR (0..255), ou None se o filtro não muda nada."""
    if nome in _cache_filtro:
        return _cache_filtro[nome]
    f = FILTROS.get(nome or "nenhum") or FILTROS["nenhum"]
    if not f["css"] and not f["tinta"]:
        _cache_filtro[nome] = None
        return None
    A = np.hstack([np.eye(3), np.zeros((3, 1))])
    for op, a in f["css"]:
        B = _matriz_css(op, a)
        A = np.hstack([B[:, :3] @ A[:, :3], (B[:, :3] @ A[:, 3:] + B[:, 3:])])
    if f["tinta"]:                                        # multiply com alfa: v * (1 - a + a * cor)
        cor, al = f["tinta"]
        d = np.array([1 - al + al * c / 255 for c in _rgb(cor)])
        A = A * d[:, None]
    A[:, 3] *= 255
    P = [2, 1, 0]                                          # RGB -> BGR
    M = np.zeros((3, 4), np.float32)
    for i in range(3):
        for j in range(3):
            M[i, j] = A[P[i], P[j]]
        M[i, 3] = A[P[i], 3]
    _cache_filtro[nome] = M
    return M


def aplicar_filtro(img, nome):
    import cv2
    M = matriz_filtro(nome)
    if M is None:
        return img
    return cv2.transform(img, M)


# ---------- vinheta, grão, barra ----------
_cache = {}


def vinheta(out, forca):
    import cv2
    if forca <= 0.01:
        return out
    k = ("vin", round(forca, 3), out.shape[1], out.shape[0])
    if k not in _cache:
        h, w = out.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        d = np.sqrt((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / math.hypot(w / 2, h / 2)
        a = forca * np.clip((d - 0.5) / 0.5, 0, 1)
        _cache[k] = cv2.merge([np.clip((1 - a) * 255, 0, 255).astype(np.uint8)] * 3)
    return cv2.multiply(out, _cache[k], scale=1 / 255)


def grao(out, forca, k):
    import cv2
    if forca <= 0.01:
        return out
    chave = ("grao", out.shape[1], out.shape[0])
    if chave not in _cache:
        rng = np.random.default_rng(7)
        h, w = out.shape[:2]
        _cache[chave] = [cv2.resize(rng.normal(0, 1, (h // 2, w // 2)).astype(np.float32), (w, h),
                                    interpolation=cv2.INTER_NEAREST) for _ in range(6)]
    n = _cache[chave][k % 6] * (34 * forca)
    pos = cv2.merge([np.clip(n, 0, 255).astype(np.uint8)] * 3)
    neg = cv2.merge([np.clip(-n, 0, 255).astype(np.uint8)] * 3)
    return cv2.subtract(cv2.add(out, pos), neg)


def barra(out, progresso, cor, pos="base"):
    h = 12
    w = int(round(out.shape[1] * min(max(progresso, 0.0), 1.0)))
    y0 = 0 if pos == "topo" else out.shape[0] - h
    r, g, b = _rgb(cor)
    out[y0:y0 + h, :] = (out[y0:y0 + h, :].astype(np.float32) * 0.6).astype(np.uint8)    # trilho escuro
    if w > 0:
        out[y0:y0 + h, :w] = (b, g, r)
    return out


# ---------- efeitos no tempo ----------
def _suave(u):
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def _hash(n):
    x = math.sin(n * 12.9898) * 43758.5453
    return x - math.floor(x)


def envelope(fx, t):
    """0..1: quanto o efeito está agindo no tempo t (o mesmo do editor)."""
    a, b = float(fx["ini"]), float(fx["fim"])
    if not (a <= t < b) or b - a < 1e-3:
        return 0.0
    u = (t - a) / (b - a)
    tp = fx["tipo"]
    if tp == "flash":
        return (1 - u) ** 2
    if tp == "pulso":
        return _suave(u / 0.25) if u < 0.25 else 1 - _suave((u - 0.25) / 0.75)
    if tp == "entrada_preto":
        return 1 - u
    if tp == "saida_preto":
        return u
    borda = min(0.12, (b - a) / 3)                           # tremor, glitch, desfoque, filtro: entra e sai rápido
    return min(1.0, (t - a) / borda, (b - t) / borda) if borda > 0 else 1.0


def ativos(fxs, t):
    return [(f, envelope(f, t)) for f in fxs if float(f["ini"]) <= t < float(f["fim"])]


def transformacao(ativos_t, t):
    """(escala, dx, dy) que o tremor e o zoom pulso aplicam na camada do vídeo (px do vertical, em volta do centro)."""
    s, dx, dy = 1.0, 0.0, 0.0
    for f, e in ativos_t:
        k = float(f.get("forca", 0.6))
        if f["tipo"] == "tremor":
            A = 30 * k * e
            dx += A * (math.sin(t * 71) + 0.5 * math.sin(t * 131))
            dy += A * (math.cos(t * 59) + 0.5 * math.sin(t * 97))
            s *= 1 + 2.2 * A / LARG
        elif f["tipo"] == "pulso":
            s *= 1 + 0.28 * k * e
    return s, dx, dy


def desfoque(out, ativos_t):
    import cv2
    sig = max([14 * float(f.get("forca", 0.6)) * e for f, e in ativos_t if f["tipo"] == "desfoque"] or [0])
    return cv2.GaussianBlur(out, (0, 0), sig) if sig > 0.3 else out


def glitch(out, ativos_t, t):
    g = [(f, e) for f, e in ativos_t if f["tipo"] == "glitch"]
    if not g:
        return out
    f, e = g[0]
    k = float(f.get("forca", 0.6)) * e
    n = math.floor(t * 15)
    off = int(round((10 + 26 * _hash(n)) * k))
    if off:
        r = out[:, :, 2].copy()
        b = out[:, :, 0].copy()
        out[:, off:, 2] = r[:, :-off]
        out[:, :-off, 0] = b[:, off:]
    H = out.shape[0]
    for i in range(3):                                       # faixas deslocadas
        if _hash(n * 3 + i + 0.5) > 0.35:
            y = int(_hash(n * 7 + i) * H * 0.9)
            hh = int(20 + _hash(n * 11 + i) * 110)
            d = int(round((_hash(n * 13 + i) - 0.5) * 120 * k))
            if d:
                out[y:y + hh] = np.roll(out[y:y + hh], d, axis=1)
    return out


def sobreposicao_cor(out, ativos_t):
    """Flash branco e fade pro preto (por cima de tudo, menos da legenda)."""
    import cv2
    for f, e in ativos_t:
        a = float(f.get("forca", 0.8)) * e
        if a <= 0.005:
            continue
        if f["tipo"] == "flash":
            out = cv2.addWeighted(out, 1 - a, np.full_like(out, 255), a, 0)
        elif f["tipo"] in ("entrada_preto", "saida_preto"):
            out = cv2.convertScaleAbs(out, alpha=1 - min(a, 1.0))
    return out


def filtro_no_tempo(ativos_t, padrao):
    for f, e in ativos_t:
        if f["tipo"] == "filtro" and e > 0.5:
            return f.get("filtro", "pb")
    return padrao


# ---------- facecam ----------
def _mascara(w, h, r):
    import cv2
    k = ("m", w, h, int(r))
    if k not in _cache:
        r = int(max(0, min(r, w // 2, h // 2)))
        m = np.zeros((h, w), np.uint8)
        if r <= 0:
            m[:] = 255
        else:
            cv2.rectangle(m, (r, 0), (w - 1 - r, h - 1), 255, -1)
            cv2.rectangle(m, (0, r), (w - 1, h - 1 - r), 255, -1)
            for px, py in ((r, r), (w - 1 - r, r), (r, h - 1 - r), (w - 1 - r, h - 1 - r)):
                cv2.circle(m, (px, py), r, 255, -1, cv2.LINE_AA)
        _cache[k] = (m.astype(np.float32) / 255.0)[:, :, None]
    return _cache[k]


def _halo(w, h, r, sigma, pad):
    import cv2
    k = ("h", w, h, int(r), sigma, pad)
    if k not in _cache:
        m = np.zeros((h + 2 * pad, w + 2 * pad), np.float32)
        m[pad:pad + h, pad:pad + w] = _mascara(w, h, r)[:, :, 0]
        _cache[k] = cv2.GaussianBlur(m, (0, 0), sigma)[:, :, None]
    return _cache[k]


def _colar(out, img, alfa, x, y):
    h, w = alfa.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(out.shape[1], x + w), min(out.shape[0], y + h)
    if x1 <= x0 or y1 <= y0:
        return
    a = alfa[y0 - y:y1 - y, x0 - x:x1 - x]
    reg = out[y0:y1, x0:x1].astype(np.float32)
    if isinstance(img, np.ndarray) and img.ndim == 3:
        src = img[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.float32)
    else:                                                     # cor sólida (B, G, R)
        src = np.broadcast_to(np.array(img, np.float32), reg.shape)
    out[y0:y1, x0:x1] = (src * a + reg * (1 - a)).astype(np.uint8)


def geometria_facecam(x, y, w, h, raio, estilo):
    """Caixa que aparece (círculo vira um quadrado no centro da caixa) e o raio dos cantos."""
    if (estilo or {}).get("forma") == "circulo":
        s = min(w, h)
        return x + (w - s) // 2, y + (h - s) // 2, s, s, s / 2
    return x, y, w, h, raio


def colar_facecam(out, img, x, y, raio, estilo):
    """Cola a webcam (img já no tamanho da caixa) com forma, borda, sombra, brilho, filtro, espelho e zoom."""
    import cv2
    est = estilo or {}
    h, w = img.shape[:2]
    z = min(max(float(est.get("zoom") or 1.0), 1.0), 2.5)
    if z > 1.001:                                            # recorta o meio e amplia
        cw, ch = max(2, int(w / z)), max(2, int(h / z))
        img = cv2.resize(img[(h - ch) // 2:(h - ch) // 2 + ch, (w - cw) // 2:(w - cw) // 2 + cw], (w, h),
                         interpolation=cv2.INTER_LINEAR)
    if est.get("espelhar"):
        img = img[:, ::-1]
    x, y, w2, h2, r = geometria_facecam(x, y, w, h, raio, est)
    if (w2, h2) != (w, h):
        img = img[(h - h2) // 2:(h - h2) // 2 + h2, (w - w2) // 2:(w - w2) // 2 + w2]
    w, h = w2, h2
    if est.get("filtro") and est["filtro"] != "nenhum":
        img = aplicar_filtro(np.ascontiguousarray(img), est["filtro"])
    b = int(est.get("borda") or 0)
    cor = _rgb(est.get("cor_borda") or "#FFFFFF")[::-1]
    if est.get("sombra"):
        pad = 48
        hal = _halo(w + 2 * b, h + 2 * b, r + b, 14, pad)
        _colar(out, (0, 0, 0), hal * 0.55, x - b - pad, y - b - pad + 10)
    if est.get("brilho"):
        pad = 56
        hal = _halo(w + 2 * b, h + 2 * b, r + b, 16, pad)
        _colar(out, cor, np.minimum(hal * 1.6, 1.0) * 0.9, x - b - pad, y - b - pad)
    if b > 0:
        _colar(out, cor, _mascara(w + 2 * b, h + 2 * b, r + b), x - b, y - b)
    _colar(out, np.ascontiguousarray(img), _mascara(w, h, r), x, y)
