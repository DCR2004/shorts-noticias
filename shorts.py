"""Shorts de notícias automáticos (Brasil) — tudo num único ficheiro.

Uso:
  python shorts.py                  # cria e publica um Short no YouTube
  python shorts.py --sem-upload     # só cria o vídeo (pasta saida/)
  python shorts.py --teste-offline  # vídeo de demonstração sem internet nem chaves
  python shorts.py --obter-token client_secret.json   # (opcional) obter o refresh token do YouTube
"""
import argparse
import asyncio
import calendar
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


# ======================================================================
# UTIL
# ======================================================================
RAIZ = Path(__file__).resolve().parent
FONTES_DIR = RAIZ / "assets" / "fonts"
HISTORICO = RAIZ / "data" / "historico.json"

FONTES_URL = "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-{}.ttf"


def garantir_fontes() -> None:
    """Descarrega a fonte Montserrat (licença OFL) se ainda não existir."""
    FONTES_DIR.mkdir(parents=True, exist_ok=True)
    for peso in ("Black", "ExtraBold", "SemiBold"):
        destino = FONTES_DIR / f"Montserrat-{peso}.ttf"
        if destino.exists() and destino.stat().st_size > 10000:
            continue
        r = requests.get(FONTES_URL.format(peso), timeout=60)
        r.raise_for_status()
        destino.write_bytes(r.content)


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

STOP = set("""
a o as os um uma uns umas de do da dos das em no na nos nas num numa por para pra pelo pela pelos
pelas com sem sob sobre ao aos à às e ou que se é são foi era ser ter tem têm mais menos como após
diz dizem afirma contra entre até já não sim seu sua seus suas ele ela eles elas isso isto este esta
esse essa nesta neste nesse nessa vai vão será sobre também quando onde quem qual quais porque muito
ano anos dia dias hoje ontem amanhã após durante veja saiba ao-vivo vivo video vídeo
""".split())


def log(msg: str) -> None:
    print(msg, flush=True)


def carregar_config() -> dict:
    with open(RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def carregar_historico() -> list:
    try:
        return json.loads(HISTORICO.read_text(encoding="utf-8"))
    except Exception:
        return []


def guardar_historico(hist: list) -> None:
    HISTORICO.parent.mkdir(parents=True, exist_ok=True)
    # mantém só os últimos 400 registos
    HISTORICO.write_text(json.dumps(hist[-400:], ensure_ascii=False, indent=1), encoding="utf-8")


def limpar_html(texto: str) -> str:
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    texto = html.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", texto)


def tokens(texto: str) -> set:
    """Palavras significativas (cortadas a 6 letras para juntar plural/singular)."""
    out = set()
    for p in normalizar(texto).split():
        if p.isdigit() and p not in ("2025", "2026", "2027"):
            out.add(p)                     # números ajudam a juntar a mesma notícia (ex.: "14,25%")
        elif len(p) > 2 and p not in STOP:
            out.add(p[:6])
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def semelhanca(a: set, b: set) -> float:
    """Mistura de Jaccard e sobreposição (títulos curtos vs longos)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return max(inter / len(a | b), inter / min(len(a), len(b)) * 0.75)


def duracao_media(caminho) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(caminho)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def correr(cmd: list, cwd=None) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Comando falhou: {' '.join(map(str, cmd[:6]))}...\n{r.stderr[-2500:]}")


def resumo_github(texto: str) -> None:
    """Escreve no resumo da execução do GitHub Actions (se existir)."""
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as f:
            f.write(texto + "\n")


# ======================================================================
# NEWS
# ======================================================================
def _data(entry):
    for campo in ("published_parsed", "updated_parsed"):
        st = entry.get(campo)
        if st:
            try:
                return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
            except Exception:
                pass
    return None


def recolher(fontes: list, horas_max: int) -> list:
    agora = datetime.now(timezone.utc)
    limite = agora - timedelta(hours=horas_max)
    itens = []
    for fonte in fontes:
        nome, url = fonte["nome"], fonte["url"]
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:
            log(f"  ✗ {nome}: {type(e).__name__} {str(e)[:80]}")
            continue
        n = 0
        for e in feed.entries[:50]:
            titulo = limpar_html(e.get("title", ""))
            link = e.get("link", "")
            if not titulo or not link:
                continue
            data = _data(e) or agora
            if data < limite or data > agora + timedelta(hours=2):
                continue
            resumo = limpar_html(e.get("summary", "") or e.get("description", ""))[:600]
            itens.append({
                "fonte": nome, "titulo": titulo, "link": link, "resumo": resumo,
                "publicado": data.isoformat(), "idade_h": round((agora - data).total_seconds() / 3600, 1),
                "tokens": tokens(titulo + " " + resumo[:160]),
                "tokens_titulo": tokens(titulo),
            })
            n += 1
        log(f"  ✓ {nome}: {n} notícias recentes")
    return itens


def agrupar(itens: list, limiar: float = 0.36) -> list:
    """Junta notícias sobre o mesmo assunto vindas de veículos diferentes."""
    grupos = []
    for it in sorted(itens, key=lambda x: x["idade_h"]):
        melhor, melhor_s = None, 0.0
        for g in grupos:
            s = max(semelhanca(it["tokens_titulo"], m["tokens_titulo"]) for m in g["itens"][:6])
            if s > melhor_s:
                melhor, melhor_s = g, s
        if melhor is not None and melhor_s >= limiar:
            melhor["itens"].append(it)
            melhor["tokens"] |= it["tokens_titulo"]
        else:
            grupos.append({"itens": [it], "tokens": set(it["tokens_titulo"])})
    for g in grupos:
        fontes = {i["fonte"] for i in g["itens"]}
        idade = min(i["idade_h"] for i in g["itens"])
        g["fontes"] = sorted(fontes)
        g["idade_h"] = idade
        # importância: nº de veículos a cobrir + bónus por ser recente
        g["pontuacao"] = len(fontes) + max(0.0, 1.0 - idade / 10)
    return sorted(grupos, key=lambda g: g["pontuacao"], reverse=True)


def remover_ja_cobertos(grupos: list, historico: list, dias: int) -> list:
    limite = datetime.now(timezone.utc) - timedelta(days=dias)
    recentes = []
    links = set()
    for h in historico:
        links.update(h.get("links", []))
        try:
            if datetime.fromisoformat(h["data"]) >= limite:
                recentes.append(set(h.get("tokens", [])))
        except Exception:
            pass
    out = []
    for g in grupos:
        if any(i["link"] in links for i in g["itens"]):
            continue
        if any(semelhanca(g["tokens"], r) >= 0.45 for r in recentes):
            continue
        out.append(g)
    return out


def texto_artigo(url: str, max_chars: int = 3500) -> str:
    try:
        import trafilatura
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        texto = trafilatura.extract(r.text, include_comments=False, include_tables=False,
                                    favor_precision=True) or ""
        return texto[:max_chars]
    except Exception as e:
        log(f"  (não consegui ler o artigo {url[:60]}: {type(e).__name__})")
        return ""


def artigos_do_grupo(grupo: dict, maximo: int = 3) -> list:
    """Lê o texto de até "maximo" veículos diferentes sobre o mesmo assunto."""
    vistos, artigos = set(), []
    for it in grupo["itens"]:
        if it["fonte"] in vistos:
            continue
        vistos.add(it["fonte"])
        texto = texto_artigo(it["link"])
        if len(texto) < 300:
            texto = (texto + "\n" + it["resumo"]).strip()
        artigos.append({"fonte": it["fonte"], "titulo": it["titulo"], "link": it["link"], "texto": texto})
        time.sleep(0.5)
        if len(artigos) >= maximo:
            break
    return artigos


# ======================================================================
# WRITER
# ======================================================================
URL = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"

SISTEMA = (
    "Você é editor de um canal brasileiro de notícias curtas (YouTube Shorts). "
    "Seu trabalho é jornalismo sério: preciso, neutro e verificável. "
    "Você nunca inventa fatos, números, datas, nomes ou citações, nunca dá opinião "
    "e sempre atribui afirmações polêmicas à fonte (\"segundo a Polícia Federal\", \"de acordo com o g1\")."
)


def _extrair_json(texto: str):
    texto = texto.strip()
    texto = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", texto)
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", texto, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def chamar_gemini(prompt: str, modelos: list, esquema: dict | None = None) -> dict:
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        raise RuntimeError("Falta o segredo GEMINI_API_KEY.")
    ultimo_erro = ""
    for modelo in modelos:
        usar_esquema = esquema is not None
        for tentativa in range(4):
            corpo = {
                "systemInstruction": {"parts": [{"text": SISTEMA}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            }
            if usar_esquema:
                corpo["generationConfig"]["responseSchema"] = esquema
            try:
                r = requests.post(URL.format(modelo=modelo), json=corpo, timeout=180,
                                  headers={"x-goog-api-key": chave, "Content-Type": "application/json"})
            except requests.RequestException as e:
                ultimo_erro = str(e)
                time.sleep(5 * (tentativa + 1))
                continue
            if r.status_code == 200:
                dados = r.json()
                try:
                    partes = dados["candidates"][0]["content"]["parts"]
                    texto = "".join(p.get("text", "") for p in partes if not p.get("thought"))
                    log(f"  IA: {modelo}")
                    return _extrair_json(texto)
                except Exception as e:
                    ultimo_erro = f"resposta inesperada: {e} {str(dados)[:300]}"
                    time.sleep(3)
                    continue
            ultimo_erro = f"{modelo} HTTP {r.status_code}: {r.text[:300]}"
            log(f"  {ultimo_erro}")
            if r.status_code == 400 and usar_esquema:
                usar_esquema = False       # tenta de novo sem esquema
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                if r.status_code == 429 and tentativa >= 1:
                    break                 # quota esgotada -> próximo modelo
                time.sleep(15 * (tentativa + 1))
                continue
            break                         # 403/404/etc -> próximo modelo
    raise RuntimeError(f"Gemini falhou em todos os modelos. Último erro: {ultimo_erro}")


ESQ_ESCOLHA = {
    "type": "OBJECT",
    "properties": {
        "id": {"type": "INTEGER"},
        "categoria": {"type": "STRING"},
        "motivo": {"type": "STRING"},
    },
    "required": ["id", "categoria", "motivo"],
}


def escolher(grupos: list, historico: list, cfg: dict) -> dict:
    linhas = []
    for i, g in enumerate(grupos):
        it = g["itens"][0]
        linhas.append(
            f"[{i}] (veículos: {len(g['fontes'])} — {', '.join(g['fontes'][:5])}; há {g['idade_h']:.0f}h) "
            f"{it['titulo']} — {it['resumo'][:220]}")
    ultimos = [f"- {h.get('categoria', '')}: {h.get('manchete', '')}" for h in historico[-12:]]
    prompt = f"""Escolha UMA notícia para o próximo Short do canal.

Critérios, por ordem:
1. Interesse público e impacto na vida dos brasileiros (política, economia, serviços, saúde, clima, ciência, Brasil no mundo, grandes acontecimentos internacionais).
2. Fato confirmado: prefira assuntos cobertos por vários veículos.
3. Variedade: evite repetir a mesma categoria dos últimos vídeos e NUNCA repita um assunto já coberto.
4. Evitar: {cfg['noticias']['evitar']}.

Últimos vídeos publicados (não repetir):
{chr(10).join(ultimos) or '- (nenhum)'}

Notícias disponíveis:
{chr(10).join(linhas)}

Responda em JSON: {{"id": <número da notícia>, "categoria": "<POLÍTICA|ECONOMIA|BRASIL|MUNDO|SAÚDE|CIÊNCIA|TECNOLOGIA|ESPORTE|CLIMA|JUSTIÇA|SEGURANÇA|CULTURA>", "motivo": "<uma frase>"}}"""
    res = chamar_gemini(prompt, cfg["ia"]["modelos"], ESQ_ESCOLHA)
    res["id"] = int(res["id"])
    if not 0 <= res["id"] < len(grupos):
        res["id"] = 0
    return res


ESQ_ROTEIRO = {
    "type": "OBJECT",
    "properties": {
        "suficiente": {"type": "BOOLEAN"},
        "titulo_youtube": {"type": "STRING"},
        "manchete": {"type": "STRING"},
        "titulo_topo": {"type": "STRING"},
        "titulo_destaque": {"type": "STRING"},
        "etiqueta": {"type": "STRING"},
        "roteiro": {"type": "STRING"},
        "descricao": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "buscas_commons": {"type": "ARRAY", "items": {"type": "STRING"}},
        "buscas_imagens": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["suficiente", "titulo_youtube", "manchete", "titulo_topo", "titulo_destaque", "etiqueta",
                 "roteiro", "descricao", "hashtags", "tags", "buscas_commons", "buscas_imagens"],
}


def escrever(artigos: list, categoria: str, cfg: dict) -> dict:
    textos = "\n\n".join(
        f"### Fonte: {a['fonte']}\nTítulo: {a['titulo']}\nTexto:\n{a['texto']}" for a in artigos)
    pmin, pmax = cfg["ia"]["palavras_min"], cfg["ia"]["palavras_max"]
    chamada = cfg["canal"]["chamada_final"]
    prompt = f"""Escreva um Short de notícia (narração de 45 a 55 segundos) com base APENAS nos textos abaixo.

REGRAS DO ROTEIRO (campo "roteiro"):
- Português do Brasil, linguagem simples, frases curtas, feito para ser FALADO.
- Entre {pmin} e {pmax} palavras.
- Primeira frase = o fato principal, de forma direta e chamativa, mas SEM exagero nem clickbait.
- Depois: os detalhes mais importantes (quem, o quê, quando, onde, números) e o contexto.
- Use apenas informações presentes nos textos. Não invente nem deduza. Se algo for incerto, diga que é incerto.
- Se algum dos textos tratar de um assunto diferente dos outros, ignore esse texto.
- Atribua acusações, estimativas e declarações a quem as fez ("segundo...", "de acordo com...").
- Tom neutro e imparcial: sem adjetivos de opinião; se houver lados em disputa, apresente os dois.
- Escreva números e siglas como devem ser lidos em voz alta quando houver ambiguidade (ex.: "1,5 bilhão de reais" em vez de "R$ 1,5 bi").
- Não diga "neste vídeo" nem "olá". Termine exatamente com: "{chamada}"

OUTROS CAMPOS:
- "suficiente": false se os textos não tiverem informação suficiente/confiável para uma notícia correta.
- "titulo_youtube": até 80 caracteres, informativo e atrativo, sem mentir nem exagerar, sem emojis.
- "manchete": até 50 caracteres, resumo da notícia.
- "titulo_topo": até 30 caracteres, a primeira parte do título que aparece no vídeo (em branco). Ex.: "PF INVESTIGA VIAGEM".
- "titulo_destaque": até 24 caracteres, a parte mais forte do título (em amarelo, letras grandes). Ex.: "DE FLÁVIO AOS EUA". Os dois juntos formam uma frase factual, sem exagero.
- "etiqueta": uma palavra em maiúsculas (ex.: {categoria}).
- "descricao": 2 a 3 frases resumindo a notícia.
- "hashtags": 3 a 5 hashtags relevantes, sem o símbolo #.
- "tags": 6 a 10 palavras-chave.
- "buscas_commons": 3 a 5 pesquisas para encontrar FOTOS REAIS do tema no Wikimedia Commons, só com NOMES PRÓPRIOS centrais da notícia, do mais importante para o menos: pessoas públicas (nome como são conhecidas, ex.: "Flávio Bolsonaro"), instituições (ex.: "Polícia Federal", "Supremo Tribunal Federal", "Anvisa", "Banco Central do Brasil"), locais ou edifícios (ex.: "Palácio do Planalto", "Congresso Nacional", nome da cidade). Sem frases nem verbos.
- "buscas_imagens": 3 pesquisas EM INGLÊS para fotos genéricas de banco de imagens que ilustrem o assunto (ex.: "police car brazil", "medicine injection pen", "stock market screen"). Sem pessoas específicas nem marcas.

TEXTOS DAS FONTES:
{textos}"""
    res = chamar_gemini(prompt, cfg["ia"]["modelos"], ESQ_ROTEIRO)
    for campo in ESQ_ROTEIRO["required"]:
        if campo not in res:
            raise RuntimeError(f"A IA não devolveu o campo '{campo}'.")
    n = len(res["roteiro"].split())
    log(f"  Roteiro com {n} palavras")
    if n < 50:
        res["suficiente"] = False
    return res


# ======================================================================
# VOICE
# ======================================================================
async def _edge(texto: str, voz: str, velocidade: str, saida):
    import edge_tts
    com = edge_tts.Communicate(texto, voz, rate=velocidade, boundary="WordBoundary")
    palavras = []
    with open(saida, "wb") as f:
        async for c in com.stream():
            if c["type"] == "audio":
                f.write(c["data"])
            elif c["type"] == "WordBoundary":
                ini = c["offset"] / 1e7
                palavras.append({"texto": c["text"], "ini": ini, "fim": ini + c["duration"] / 1e7})
    return palavras


def _estimar_tempos(texto: str, duracao: float) -> list:
    """Sem tempos da voz: distribui as palavras pela duração, com pausas na pontuação."""
    partes = texto.split()
    pesos = []
    for p in partes:
        w = len(p) + 2
        if re.search(r"[.!?]$", p):
            w += 6
        elif re.search(r"[,;:]$", p):
            w += 3
        pesos.append(w)
    total = sum(pesos) or 1
    escala = (duracao - 0.3) / total
    t, out = 0.15, []
    for p, w in zip(partes, pesos):
        fala = (len(p) + 2) * escala
        out.append({"texto": p, "ini": t, "fim": t + fala})
        t += w * escala
    return out


def sintetizar(texto: str, cfg: dict, pasta) -> tuple:
    mp3 = pasta / "narracao.mp3"
    voz = cfg["voz"]["nome"]
    vel = cfg["voz"].get("velocidade", "+0%")

    # 1) Voz neural do Microsoft Edge (gratuita, com tempos por palavra)
    for tentativa in range(3):
        try:
            palavras = asyncio.run(_edge(texto, voz, vel, mp3))
            dur = duracao_media(mp3)
            if dur > 3:
                if len(palavras) < len(texto.split()) * 0.5:
                    palavras = _estimar_tempos(texto, dur)
                log(f"  Voz: {voz} ({dur:.1f}s)")
                return mp3, palavras, dur
        except Exception as e:
            log(f"  Voz Edge falhou ({type(e).__name__}: {str(e)[:100]}), tentativa {tentativa + 1}")

    # 2) Google Translate TTS (gTTS), sotaque do Brasil
    try:
        from gtts import gTTS
        gTTS(texto, lang="pt", tld="com.br").save(str(mp3))
        acelerado = pasta / "narracao_rapida.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3), "-filter:a", "atempo=1.15",
                        str(acelerado)], check=True)
        dur = duracao_media(acelerado)
        log(f"  Voz: gTTS ({dur:.1f}s)")
        return acelerado, _estimar_tempos(texto, dur), dur
    except Exception as e:
        log(f"  gTTS falhou ({type(e).__name__})")

    # 3) espeak-ng local (só para testes sem internet)
    if shutil.which("espeak-ng"):
        wav = pasta / "narracao.wav"
        voz_local = "mb-br4" if shutil.which("mbrola") else "pt-br"
        r = subprocess.run(["espeak-ng", "-v", voz_local, "-s", "165", "-w", str(wav), texto],
                           capture_output=True)
        if r.returncode != 0 or not wav.exists() or wav.stat().st_size < 1000:
            subprocess.run(["espeak-ng", "-v", "pt-br", "-s", "165", "-w", str(wav), texto], check=True)
        dur = duracao_media(wav)
        log(f"  Voz: espeak-ng local ({dur:.1f}s) — só para teste")
        return wav, _estimar_tempos(texto, dur), dur

    raise RuntimeError("Não foi possível gerar a narração.")


# ======================================================================
# CAPTIONS
# ======================================================================
ASS_AMARELO = r"&H00D4FF&"   # #FFD400 (o ASS usa BGR)
ASS_BRANCO = r"&HFFFFFF&"

CABECALHO = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Leg,Montserrat Black,88,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,7,4,5,70,150,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _t(seg: float) -> str:
    seg = max(0.0, seg)
    h = int(seg // 3600)
    m = int(seg % 3600 // 60)
    s = seg % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _limpo(p: str) -> str:
    p = p.replace("\\", "").replace("{", "(").replace("}", ")")
    return p.upper()


LIGACOES = {"e", "o", "a", "os", "as", "de", "do", "da", "dos", "das", "em", "no", "na", "que",
            "um", "uma", "por", "para", "com", "ao", "à", "se", "nos", "nas", "pelo", "pela"}


def _blocos(palavras: list) -> list:
    blocos, atual = [], []
    for i, w in enumerate(palavras):
        atual.append(w)
        prox = palavras[i + 1] if i + 1 < len(palavras) else None
        letras = sum(len(x["texto"]) for x in atual)
        pausa = prox is not None and prox["ini"] - w["fim"] > 0.28
        pontuacao = bool(re.search(r"[.,;:!?]$", w["texto"]))
        if pausa or pontuacao or prox is None:
            blocos.append(atual)
            atual = []
        elif len(atual) >= 3 or letras >= 16:
            # não terminar um bloco numa palavra de ligação ("e", "de", "que"...)
            if len(atual) > 1 and atual[-1]["texto"].lower() in LIGACOES:
                blocos.append(atual[:-1])
                atual = [atual[-1]]
            else:
                blocos.append(atual)
                atual = []
    return [b for b in blocos if b]


def gerar_ass(palavras: list, caminho, duracao_total: float, y: int = 1440) -> None:
    palavras = [w for w in palavras if w["texto"].strip()]
    blocos = _blocos(palavras)
    linhas = [CABECALHO]
    for bi, bloco in enumerate(blocos):
        prox_ini = blocos[bi + 1][0]["ini"] if bi + 1 < len(blocos) else duracao_total
        fim_bloco = min(bloco[-1]["fim"] + 0.35, prox_ini)
        for wi, w in enumerate(bloco):
            ini = w["ini"] if wi else max(0.0, w["ini"] - 0.04)
            fim = bloco[wi + 1]["ini"] if wi + 1 < len(bloco) else fim_bloco
            if fim <= ini:
                fim = ini + 0.05
            partes = []
            for k, x in enumerate(bloco):
                txt = _limpo(re.sub(r"[.,;:!?]+$", "", x["texto"]) if k == len(bloco) - 1 else x["texto"])
                txt = re.sub(r"[;:]$", "", txt)
                if k == wi:
                    partes.append(f"{{\\c{ASS_AMARELO}}}{txt}{{\\c{ASS_BRANCO}}}")
                else:
                    partes.append(txt)
            anim = r"{\fscx85\fscy85\t(0,90,\fscx100\fscy100)}" if wi == 0 else ""
            texto = f"{{\\pos(505,{y})}}{anim}" + " ".join(partes)
            linhas.append(f"Dialogue: 0,{_t(ini)},{_t(fim)},Leg,,0,0,0,,{texto}\n")
    with open(caminho, "w", encoding="utf-8") as f:
        f.writelines(linhas)


# ======================================================================
# VISUALS
# ======================================================================
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
UA_WIKI = "ShortsNoticiasBot/1.0 (https://github.com/DCR2004/shorts-noticias)"
PEXELS = "https://api.pexels.com/v1/search"
LICENCAS_LIVRES = re.compile(r"^(cc0( 1\.0)?|public domain|pd|attribution|cc[ -]by(-sa)?([ -][\d.]+)?( [a-z]{2})?)$", re.I)


def _meta(meta: dict, campo: str) -> str:
    return limpar_html((meta.get(campo) or {}).get("value", ""))[:90]


def commons(buscas: list, quantidade: int, pasta, usados: set) -> list:
    """Fotos reais do tema (pessoas, instituições, locais) no Wikimedia Commons, só com licenças livres."""
    fotos = []
    for q in buscas or []:
        if len(fotos) >= quantidade:
            break
        chave = tokens(q)
        try:
            r = requests.get(COMMONS_API, params={
                "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                "gsrlimit": 15, "gsrsearch": f"{q} filetype:bitmap", "prop": "imageinfo",
                "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600},
                headers={"User-Agent": UA_WIKI}, timeout=25)
            r.raise_for_status()
            paginas = sorted(r.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
        except Exception as e:
            log(f"  Commons '{q}': {type(e).__name__}")
            continue
        candidatos = []
        for p in paginas:
            ii = (p.get("imageinfo") or [{}])[0]
            meta = ii.get("extmetadata", {})
            titulo = p.get("title", "")
            lic = _meta(meta, "LicenseShortName").strip()
            if titulo in usados or any(f["id"] == titulo for f in fotos):
                continue
            if ii.get("mime") not in ("image/jpeg", "image/png") or ii.get("width", 0) < 900:
                continue
            if not LICENCAS_LIVRES.match(lic) or re.search(r"\b(NC|ND)\b", lic):
                continue
            # o ficheiro tem de mencionar o termo pesquisado (no nome ou na descrição)
            if chave and not (chave & tokens(titulo + " " + _meta(meta, "ImageDescription"))):
                continue
            paisagem = ii.get("width", 0) >= ii.get("height", 1)
            candidatos.append((0 if paisagem else 1, p.get("index", 99), titulo, ii, meta, lic))
        candidatos.sort(key=lambda c: (c[0], c[1]))
        por_busca = 0
        for _, _, titulo, ii, meta, lic in candidatos:
            if por_busca >= 2 or len(fotos) >= quantidade:
                break
            try:
                img = requests.get(ii.get("thumburl") or ii["url"], headers={"User-Agent": UA_WIKI}, timeout=40)
                img.raise_for_status()
                destino = pasta / f"commons_{len(fotos)}.jpg"
                destino.write_bytes(img.content)
                Image.open(destino).verify()
            except Exception:
                continue
            autor = _meta(meta, "Artist") or "autor desconhecido"
            fotos.append({"caminho": destino, "id": titulo, "autor": autor, "licenca": lic,
                          "fonte": "Wikimedia Commons", "url": ii.get("descriptionurl", "")})
            por_busca += 1
    log(f"  Fotos do tema (Wikimedia Commons): {len(fotos)}")
    return fotos


def pexels(buscas: list, quantidade: int, pasta, usados: set) -> list:
    """Fotos genéricas (licença Pexels) para completar quando faltam fotos do tema."""
    chave = os.environ.get("PEXELS_API_KEY")
    if not chave or quantidade <= 0:
        return []
    fotos = []
    for q in (buscas or [])[:quantidade + 2]:
        try:
            r = requests.get(PEXELS, params={"query": q, "orientation": "landscape", "per_page": 15},
                             headers={"Authorization": chave, "User-Agent": UA}, timeout=20)
            r.raise_for_status()
            cand = [f for f in r.json().get("photos", [])
                    if f"pexels:{f['id']}" not in usados and all(x["id"] != f"pexels:{f['id']}" for x in fotos)]
            if not cand:
                continue
            foto = random.choice(cand[:6])
            url = foto["src"]["original"] + "?auto=compress&cs=tinysrgb&fit=crop&w=1920&h=1080"
            img = requests.get(url, headers={"User-Agent": UA}, timeout=40)
            img.raise_for_status()
            destino = pasta / f"pexels_{len(fotos)}.jpg"
            destino.write_bytes(img.content)
            Image.open(destino).verify()
            fotos.append({"caminho": destino, "id": f"pexels:{foto['id']}", "autor": foto.get("photographer", "Pexels"),
                          "licenca": "Licença Pexels", "fonte": "Pexels", "url": foto.get("url", "")})
        except Exception as e:
            log(f"  Pexels '{q}': {type(e).__name__} {str(e)[:80]}")
        if len(fotos) >= quantidade:
            break
    log(f"  Fotos genéricas (Pexels): {len(fotos)}")
    return fotos


PALETAS = [
    ((12, 24, 58), (148, 22, 40)),
    ((8, 40, 64), (16, 120, 140)),
    ((28, 16, 60), (190, 70, 30)),
    ((10, 30, 30), (40, 110, 70)),
    ((20, 20, 28), (90, 90, 120)),
]


def fundos_gerados(quantidade: int, pasta) -> list:
    """Fundos abstratos (gradiente + formas suaves) quando não há fotos."""
    out = []
    for i in range(quantidade):
        a, b = PALETAS[(i + random.randint(0, 9)) % len(PALETAS)]
        w, h = 1920, 1080
        img = Image.new("RGB", (w, h))
        px = ImageDraw.Draw(img)
        for x in range(0, w, 4):
            t = 0.5 + 0.5 * math.sin((x / w - 0.5) * math.pi * 0.9)
            cor = tuple(int(a[k] * (1 - t) + b[k] * t) for k in range(3))
            px.rectangle([x, 0, x + 4, h], fill=cor)
        camada = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(camada)
        for _ in range(7):
            r = random.randint(150, 450)
            x, y = random.randint(-100, w), random.randint(-100, h)
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, random.randint(10, 28)))
        camada = camada.filter(ImageFilter.GaussianBlur(30))
        img = Image.alpha_composite(img.convert("RGBA"), camada).convert("RGB")
        destino = pasta / f"fundo_{i}.jpg"
        img.save(destino, quality=92)
        out.append({"caminho": destino, "id": None, "autor": None})
    return out


def obter_imagens(roteiro: dict, cfg: dict, pasta, historico: list) -> list:
    n = int(cfg["imagens"].get("quantidade", 4))
    usados = set()
    for h in historico[-80:]:
        usados.update(h.get("fotos", []))
        usados.update(f"pexels:{x}" for x in h.get("fotos_pexels", []))
    fotos = commons(roteiro.get("buscas_commons", []), n, pasta, usados)
    if len(fotos) < n:
        fotos += pexels(roteiro.get("buscas_imagens", []), n - len(fotos), pasta, usados)
    if not fotos:
        log("  Sem fotos: a usar fundos gerados")
        return fundos_gerados(n, pasta)
    return fotos


# ======================================================================
# RENDER
# ======================================================================
L, A, FPS = 1080, 1920, 30
FAIXA_Y, FAIXA_H = 650, 608          # faixa central 16:9 com a foto/vídeo
VERMELHO = (225, 29, 46, 255)
AMARELO = (255, 212, 0, 255)


def _fonte(nome: str, tamanho: int):
    return ImageFont.truetype(str(FONTES_DIR / f"Montserrat-{nome}.ttf"), tamanho)


def _quebrar(texto: str, fonte, largura: int, draw) -> list:
    linhas, atual = [], ""
    for p in texto.split():
        teste = f"{atual} {p}".strip()
        if draw.textlength(teste, font=fonte) <= largura:
            atual = teste
        else:
            if atual:
                linhas.append(atual)
            atual = p
    if atual:
        linhas.append(atual)
    return linhas


def dividir_manchete(manchete: str) -> tuple:
    palavras = manchete.split()
    meio = max(1, len(palavras) // 2)
    return " ".join(palavras[:meio]), " ".join(palavras[meio:])


def _texto_com_sombra(img, xy, texto, fonte, cor):
    sombra = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sombra).text((xy[0] + 3, xy[1] + 5), texto, font=fonte, fill=(0, 0, 0, 210))
    img.alpha_composite(sombra.filter(ImageFilter.GaussianBlur(5)))
    ImageDraw.Draw(img).text(xy, texto, font=fonte, fill=cor)


def camada_grafica(caminho, titulo_topo: str, titulo_destaque: str, etiqueta: str, canal: str, fontes: list) -> None:
    """Faixa de cima: etiqueta, canal, manchete em duas cores, fontes. Linhas amarelas à volta da faixa central."""
    img = Image.new("RGBA", (L, A), (0, 0, 0, 0))
    sombra = Image.new("L", (L, A), 0)
    ds = ImageDraw.Draw(sombra)
    for y in range(FAIXA_Y):
        ds.line([(0, y), (L, y)], fill=int(150 - 60 * y / FAIXA_Y))
    escuro = Image.new("RGBA", (L, A), (0, 0, 0, 255))
    escuro.putalpha(sombra)
    img.alpha_composite(escuro)
    d = ImageDraw.Draw(img)

    # etiqueta + canal
    f_tag = _fonte("ExtraBold", 32)
    etiqueta = (etiqueta or "NOTÍCIA").upper()[:18]
    tw = d.textlength(etiqueta, font=f_tag)
    x0, y0 = 60, 118
    d.rounded_rectangle([x0, y0, x0 + tw + 34, y0 + 54], radius=10, fill=VERMELHO)
    d.text((x0 + 17, y0 + 27), etiqueta, font=f_tag, fill="white", anchor="lm")
    d.text((x0 + tw + 56, y0 + 27), canal, font=_fonte("SemiBold", 32), fill=(255, 255, 255, 235), anchor="lm")

    # manchete: parte branca + parte amarela (maior); encolhe até caber na faixa de cima
    data_br = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%d/%m/%Y")
    txt_fontes = ("Fonte: " + ", ".join(fontes) if fontes else "Resumo de notícias") + f" · {data_br}"
    larg = L - 120
    topo, destaque = titulo_topo.upper().strip(), titulo_destaque.upper().strip()
    for escala in [1.0, 0.92, 0.84, 0.76, 0.68, 0.6]:
        f_topo, f_dest = _fonte("Black", int(66 * escala)), _fonte("Black", int(104 * escala))
        l_topo = _quebrar(topo, f_topo, larg, d)[:2] if topo else []
        l_dest = _quebrar(destaque, f_dest, larg, d)[:2] if destaque else []
        altura = len(l_topo) * int(66 * escala * 1.12) + len(l_dest) * int(104 * escala * 1.08) + 95
        if 200 + altura <= FAIXA_Y - 20:
            break
    y = 200
    for linha in l_topo:
        _texto_com_sombra(img, (60, y), linha, f_topo, (255, 255, 255, 255))
        y += int(66 * escala * 1.12)
    for linha in l_dest:
        _texto_com_sombra(img, (60, y), linha, f_dest, AMARELO)
        y += int(104 * escala * 1.08)
    d = ImageDraw.Draw(img)
    d.text((62, y + 12), _quebrar(txt_fontes, _fonte("SemiBold", 28), larg, d)[0],
           font=_fonte("SemiBold", 28), fill=(236, 236, 236, 255))
    d.text((62, y + 52), "Narração gerada por IA · Imagens de arquivo",
           font=_fonte("SemiBold", 22), fill=(200, 200, 200, 255))

    # linhas amarelas a enquadrar a faixa central
    d.rectangle([0, FAIXA_Y - 6, L, FAIXA_Y - 1], fill=AMARELO)
    d.rectangle([0, FAIXA_Y + FAIXA_H, L, FAIXA_Y + FAIXA_H + 5], fill=AMARELO)
    img.save(caminho)


def _base_segmento(foto: dict, destino) -> None:
    """Fundo do segmento: a própria foto desfocada e escura + crédito por baixo da faixa."""
    im = Image.open(foto["caminho"]).convert("RGB")
    bg = ImageOps.fit(im, (L, A), centering=(0.5, 0.35)).filter(ImageFilter.GaussianBlur(38))
    bg = ImageEnhance.Brightness(bg).enhance(0.42).convert("RGBA")
    if foto.get("autor"):
        d = ImageDraw.Draw(bg)
        credito = f"Foto: {foto['autor']} · {foto.get('licenca', '')} · {foto.get('fonte', '')}"
        f = _fonte("SemiBold", 22)
        while d.textlength(credito, font=f) > L - 80 and len(credito) > 20:
            credito = credito[:-4] + "…"
        d.text((L - 40, FAIXA_Y + FAIXA_H + 16), credito, font=f, fill=(220, 220, 220, 255), anchor="ra")
    bg.convert("RGB").save(destino, quality=93)


_MOVIMENTOS = [
    ("1+0.08*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),            # aproximar
    ("1.08-0.08*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),         # afastar
    ("1.08", "(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),                  # esquerda -> direita
    ("1.08", "(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),              # direita -> esquerda
]


def _segmento(foto: dict, saida, dur: float, mov: int, pasta, i: int) -> None:
    n = max(2, int(round(dur * FPS)))
    base = pasta / f"base_{i}.jpg"
    _base_segmento(foto, base)
    z, x, y = (s.format(n=n) for s in _MOVIMENTOS[mov % len(_MOVIMENTOS)])
    filtro = (f"[1:v]scale=2160:1216:force_original_aspect_ratio=increase,"
              f"crop=2160:1216:(iw-2160)/2:(ih-1216)*0.3,setsar=1,"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={L}x{FAIXA_H}:fps={FPS}[fg];"
              f"[0:v][fg]overlay=0:{FAIXA_Y}:shortest=1,format=yuv420p[v]")
    correr(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(base),
            "-i", str(foto["caminho"]), "-filter_complex", filtro, "-map", "[v]", "-frames:v", str(n),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-r", str(FPS), str(saida)])


def fundo(fotos: list, duracao: float, pasta) -> object:
    T = 0.45  # duração da transição
    k = len(fotos)
    dur_seg = (duracao + T * (k - 1)) / k
    segs = []
    inicio = random.randint(0, 3)
    for i, foto in enumerate(fotos):
        s = pasta / f"seg_{i}.mp4"
        _segmento(foto, s, dur_seg, inicio + i, pasta, i)
        segs.append(s)
    saida = pasta / "fundo.mp4"
    if k == 1:
        shutil.copy(segs[0], saida)
        return saida
    entradas, filtros, ant, off = [], [], "[0:v]", 0.0
    for s in segs:
        entradas += ["-i", str(s)]
    for i in range(1, k):
        off += dur_seg - T
        rot = f"[x{i}]"
        filtros.append(f"{ant}[{i}:v]xfade=transition=fade:duration={T}:offset={off:.3f}{rot}")
        ant = rot
    correr(["ffmpeg", "-y", "-v", "error", *entradas, "-filter_complex", ";".join(filtros),
            "-map", ant, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", str(FPS), str(saida)])
    return saida


def montar(fotos: list, audio, ass, grafico, duracao_audio: float, pasta, saida) -> None:
    total = duracao_audio + 0.9
    log(f"  A montar vídeo de {total:.1f}s com {len(fotos)} imagens")
    bg = fundo(fotos, total, pasta)
    fontes_local = pasta / "fonts"
    fontes_local.mkdir(exist_ok=True)
    for f in FONTES_DIR.glob("*.ttf"):
        shutil.copy(f, fontes_local / f.name)
    filtro = (f"[0:v][1:v]overlay=0:0,subtitles={ass.name}:fontsdir=fonts,format=yuv420p[v];"
              f"[2:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,apad[a]")
    correr(["ffmpeg", "-y", "-v", "error", "-i", bg.name, "-i", grafico.name, "-i", str(audio),
            "-filter_complex", filtro, "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(saida.resolve())],
           cwd=pasta)


# ======================================================================
# YOUTUBE
# ======================================================================
ESCOPO = ["https://www.googleapis.com/auth/youtube.upload"]


def _credenciais():
    from google.oauth2.credentials import Credentials
    faltam = [k for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN") if not os.environ.get(k)]
    if faltam:
        raise RuntimeError(f"Faltam os segredos do YouTube: {', '.join(faltam)}")
    return Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=ESCOPO,
    )


def enviar(video, titulo: str, descricao: str, tags: list, cfg: dict) -> dict:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    yt_cfg = cfg["youtube"]
    modo = yt_cfg.get("modo", "agendado")
    status = {
        "selfDeclaredMadeForKids": False,
        "embeddable": True,
        "license": "youtube",
        "containsSyntheticMedia": bool(yt_cfg.get("marcar_conteudo_sintetico", False)),
    }
    publicar_em = None
    if modo == "publico":
        status["privacyStatus"] = "public"
    elif modo == "privado":
        status["privacyStatus"] = "private"
    else:
        status["privacyStatus"] = "private"
        horas = float(yt_cfg.get("horas_revisao", 1))
        publicar_em = datetime.now(timezone.utc) + timedelta(hours=horas)
        status["publishAt"] = publicar_em.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # o YouTube rejeita "<" e ">" no título/descrição; as tags somam no máximo ~500 caracteres
    titulo = titulo.replace("<", "").replace(">", "").strip()
    descricao = descricao.replace("<", "").replace(">", "")
    tags_ok, soma = [], 0
    for t in tags:
        t = t.replace("<", "").replace(">", "").replace(",", " ").strip()[:60]
        if t and soma + len(t) + 3 <= 480:
            tags_ok.append(t)
            soma += len(t) + 3
    corpo = {
        "snippet": {
            "title": titulo[:100],
            "description": descricao[:4900],
            "tags": tags_ok,
            "categoryId": str(yt_cfg.get("categoria", "25")),
            "defaultLanguage": yt_cfg.get("idioma", "pt-BR"),
            "defaultAudioLanguage": yt_cfg.get("idioma", "pt-BR"),
        },
        "status": status,
    }
    yt = build("youtube", "v3", credentials=_credenciais(), cache_discovery=False)
    pedido = yt.videos().insert(
        part="snippet,status", body=corpo,
        notifySubscribers=bool(yt_cfg.get("notificar_inscritos", True)),
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=-1, resumable=True),
    )
    resposta = None
    while resposta is None:
        _, resposta = pedido.next_chunk()
    vid = resposta["id"]
    log(f"  ✓ Enviado: https://youtube.com/shorts/{vid} ({status['privacyStatus']}"
        + (f", publica às {publicar_em:%H:%M} UTC" if publicar_em else "") + ")")
    return {"id": vid, "privacidade": status["privacyStatus"],
            "publica_em": publicar_em.isoformat() if publicar_em else None}


# ======================================================================
# MAIN
# ======================================================================
def descricao_completa(roteiro: dict, artigos: list, creditos: list) -> str:
    partes = [roteiro["descricao"].strip(), "", "Fontes:"]
    for a in artigos:
        partes.append(f"• {a['fonte']}: {a['link']}")
    partes += ["", "Resumo escrito e narrado com ajuda de inteligência artificial a partir das fontes acima."]
    creditos = [c for c in creditos if c.get("autor")]
    if creditos:
        partes += ["", "Imagens de arquivo:"]
        for c in creditos:
            partes.append(f"• Foto: {c['autor']} ({c.get('licenca', '')}), via {c.get('fonte', '')}: {c.get('url', '')}")
    tags = ["Shorts", "Notícias", "Brasil"] + [h.lstrip("#").replace(" ", "") for h in roteiro.get("hashtags", [])]
    vistos, hashtags = set(), []
    for t in tags:
        if t and t.lower() not in vistos:
            vistos.add(t.lower())
            hashtags.append("#" + t)
    partes += ["", " ".join(hashtags[:8])]
    return "\n".join(partes)


def escolher_e_escrever(cfg: dict, historico: list):
    log("1) A recolher notícias...")
    itens = recolher(cfg["noticias"]["fontes"], int(cfg["noticias"]["horas_max"]))
    if not itens:
        raise RuntimeError("Nenhuma fonte RSS respondeu.")
    grupos = agrupar(itens)
    grupos = remover_ja_cobertos(grupos, historico, int(cfg["noticias"]["dias_sem_repetir"]))
    log(f"   {len(itens)} notícias, {len(grupos)} assuntos novos")
    if not grupos:
        return None
    candidatos = grupos[:25]
    for tentativa in range(3):
        if not candidatos:
            break
        log("2) A escolher a notícia...")
        escolha = escolher(candidatos, historico, cfg)
        grupo = candidatos.pop(escolha["id"])
        log(f"   Escolhida: {grupo['itens'][0]['titulo']}  [{escolha['categoria']}] ({', '.join(grupo['fontes'])})")
        log(f"   Motivo: {escolha['motivo']}")
        artigos = artigos_do_grupo(grupo)
        log("3) A escrever o roteiro...")
        roteiro = escrever(artigos, escolha["categoria"], cfg)
        if roteiro.get("suficiente", True):
            roteiro["categoria"] = escolha["categoria"]
            return grupo, artigos, roteiro
        log("   Informação insuficiente; a tentar outra notícia.")
    return None


TESTE_OFFLINE = {
    "artigos": [{"fonte": "Teste do sistema", "titulo": "Demonstração", "link": "https://example.com", "texto": ""}],
    "roteiro": {
        "suficiente": True,
        "titulo_youtube": "Teste: como funciona o canal de notícias automático",
        "manchete": "Vídeo de teste do canal de notícias",
        "titulo_topo": "Vídeo de teste",
        "titulo_destaque": "Canal de notícias",
        "etiqueta": "TESTE",
        "roteiro": ("Este é um vídeo de demonstração do sistema automático de notícias. Cinco vezes por dia, "
                    "o sistema lê as principais manchetes de vários jornais brasileiros, escolhe o assunto mais "
                    "importante e escreve um resumo curto e neutro, com as fontes citadas. Depois, uma voz de "
                    "inteligência artificial narra o texto, as legendas aparecem palavra por palavra e o vídeo é "
                    "publicado sozinho no YouTube. Siga o canal para mais notícias."),
        "descricao": "Vídeo de demonstração do sistema automático de Shorts de notícias.",
        "hashtags": ["Teste"], "tags": ["teste"], "buscas_commons": [], "buscas_imagens": [],
    },
}


def obter_token(caminho_json: str) -> int:
    """Alternativa ao OAuth Playground (precisa de um cliente OAuth do tipo 'App para computador')."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(caminho_json, ESCOPO)
    cred = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    dados = json.loads(Path(caminho_json).read_text())
    cliente = dados.get("installed") or dados.get("web") or {}
    print("\nCopia estes valores para os Secrets do GitHub:\n")
    print("YT_CLIENT_ID     =", cliente.get("client_id"))
    print("YT_CLIENT_SECRET =", cliente.get("client_secret"))
    print("YT_REFRESH_TOKEN =", cred.refresh_token)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-upload", action="store_true", help="não envia para o YouTube")
    ap.add_argument("--teste-offline", action="store_true", help="vídeo de demonstração sem internet")
    ap.add_argument("--obter-token", metavar="CLIENT_SECRET_JSON", help="obter o refresh token do YouTube")
    args = ap.parse_args()
    if args.obter_token:
        return obter_token(args.obter_token)
    garantir_fontes()

    cfg = carregar_config()
    historico = carregar_historico()
    saida_dir = RAIZ / "saida"
    saida_dir.mkdir(exist_ok=True)

    if args.teste_offline:
        grupo, artigos, roteiro = None, TESTE_OFFLINE["artigos"], TESTE_OFFLINE["roteiro"]
        args.sem_upload = True
    else:
        r = escolher_e_escrever(cfg, historico)
        if r is None:
            log("Sem notícias novas para publicar agora. Até à próxima execução!")
            resumo_github("Sem notícias novas nesta execução.")
            return 0
        grupo, artigos, roteiro = r

    with tempfile.TemporaryDirectory() as tmp:
        pasta = Path(tmp)
        log("4) A gerar a narração...")
        audio, palavras, dur = sintetizar(roteiro["roteiro"], cfg, pasta)

        log("5) A preparar imagens e grafismo...")
        if args.teste_offline:
            fotos = fundos_gerados(int(cfg["imagens"]["quantidade"]), pasta)
        else:
            fotos = obter_imagens(roteiro, cfg, pasta, historico)
        nomes_fontes = list(dict.fromkeys(a["fonte"] for a in artigos))
        topo, destaque = roteiro.get("titulo_topo", ""), roteiro.get("titulo_destaque", "")
        if not topo or not destaque:
            topo, destaque = dividir_manchete(roteiro["manchete"])
        grafico = pasta / "grafico.png"
        camada_grafica(grafico, topo, destaque, roteiro["etiqueta"], cfg["canal"]["nome"], nomes_fontes)
        ass = pasta / "legendas.ass"
        gerar_ass(palavras, ass, dur + 0.9)

        log("6) A montar o vídeo...")
        carimbo = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        video = saida_dir / f"short_{carimbo}.mp4"
        montar(fotos, audio, ass, grafico, dur, pasta, video)
        log(f"   Vídeo: {video} ({video.stat().st_size / 1e6:.1f} MB)")

    descricao = descricao_completa(roteiro, artigos, fotos)
    meta = {"titulo": roteiro["titulo_youtube"], "descricao": descricao, "tags": roteiro["tags"],
            "roteiro": roteiro["roteiro"], "video": video.name}
    (saida_dir / "ultimo.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    resumo = [f"## {roteiro['titulo_youtube']}", "", roteiro["roteiro"], "", "**Fontes:** " +
              ", ".join(f"[{a['fonte']}]({a['link']})" for a in artigos)]

    if args.sem_upload:
        log("Modo sem upload: vídeo criado mas não enviado.")
        resumo.append("\n_Modo de teste: não foi publicado._")
        resumo_github("\n".join(resumo))
        return 0

    log("7) A enviar para o YouTube...")
    res = enviar(video, roteiro["titulo_youtube"], descricao, roteiro["tags"], cfg)
    resumo.append(f"\n**YouTube:** https://youtube.com/shorts/{res['id']} ({res['privacidade']})")
    resumo_github("\n".join(resumo))

    historico.append({
        "data": datetime.now(timezone.utc).isoformat(),
        "manchete": roteiro["manchete"], "titulo": roteiro["titulo_youtube"],
        "categoria": roteiro.get("categoria", ""),
        "links": [i["link"] for i in grupo["itens"]] if grupo else [],
        "tokens": sorted(grupo["tokens"] if grupo else tokens(roteiro["manchete"])),
        "video_id": res["id"], "fotos": [f["id"] for f in fotos if f.get("id")],
    })
    guardar_historico(historico)
    return 0


if __name__ == "__main__":
    sys.exit(main())
