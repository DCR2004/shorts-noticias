"""Shorts de notícias automáticos (Brasil) — tudo num único ficheiro.

Uso:
  python shorts.py                  # cria e publica um Short no YouTube
  python shorts.py --sem-upload     # só cria o vídeo (pasta saida/)
  python shorts.py --teste-offline  # vídeo de demonstração sem internet nem chaves
  python shorts.py --obter-token client_secret.json   # (opcional) obter o refresh token do YouTube
"""
import argparse
import asyncio
import base64
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
    anton = FONTES_DIR / "Anton-Regular.ttf"
    if not anton.exists() or anton.stat().st_size < 10000:
        r = requests.get("https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf", timeout=60)
        r.raise_for_status()
        anton.write_bytes(r.content)


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


def correr(cmd: list, cwd=None, timeout=None) -> None:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
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


def chamar_gemini(prompt: str, modelos: list, esquema: dict | None = None, anexos: list | None = None) -> dict:
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        raise RuntimeError("Falta o segredo GEMINI_API_KEY.")
    ultimo_erro = ""
    for modelo in modelos:
        usar_esquema = esquema is not None
        for tentativa in range(4):
            corpo = {
                "systemInstruction": {"parts": [{"text": SISTEMA}]},
                "contents": [{"role": "user", "parts": (anexos or []) + [{"text": prompt}]}],
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


def escolher(grupos: list, historico: list, cfg: dict, preferir_pessoas: bool = False) -> dict:
    linhas = []
    for i, g in enumerate(grupos):
        it = g["itens"][0]
        linhas.append(
            f"[{i}] (veículos: {len(g['fontes'])} — {', '.join(g['fontes'][:5])}; há {g['idade_h']:.0f}h) "
            f"{it['titulo']} — {it['resumo'][:220]}")
    ultimos = [f"- {h.get('categoria', '')}: {h.get('manchete', '')}" for h in historico[-30:]]
    extra = ("5. Este vídeo vai incluir um trecho de entrevista ou discurso de arquivo: prefira notícias centradas "
             "numa figura pública brasileira conhecida (presidente, ministros, governadores, STF, Congresso).\n"
             if preferir_pessoas else "")
    prompt = f"""Escolha UMA notícia para o próximo Short do canal.

Critérios, por ordem:
1. Interesse público e impacto na vida dos brasileiros (política, economia, serviços, saúde, clima, ciência, Brasil no mundo, grandes acontecimentos internacionais).
2. Fato confirmado: prefira assuntos cobertos por vários veículos.
3. Variedade: evite repetir a mesma categoria dos últimos vídeos e NUNCA repita um assunto já coberto.
4. Evitar: {cfg['noticias']['evitar']}.
{extra}
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
        "pessoas": {"type": "ARRAY", "items": {"type": "STRING"}},
        "buscas_trecho": {"type": "ARRAY", "items": {"type": "STRING"}},
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
- "pessoas": até 3 figuras públicas centrais na notícia, com o nome como são conhecidas (ex.: "Lula", "Fernando Haddad", "Alexandre de Moraes"), que possam aparecer a falar sobre este assunto em vídeos de arquivo. Lista vazia se não houver.
- "buscas_trecho": 2 a 4 pesquisas curtas para encontrar vídeos de arquivo em que uma dessas figuras fala sobre ESTE assunto: nome + tema em uma ou duas palavras (ex.: "Lula reforma tributária", "Haddad arcabouço fiscal", "Marina Silva desmatamento"). Lista vazia se não houver.

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


def sintetizar(texto: str, cfg: dict, pasta, nome: str = "narracao") -> tuple:
    mp3 = pasta / f"{nome}.mp3"
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
        acelerado = pasta / f"{nome}_rapida.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3), "-filter:a", "atempo=1.15",
                        str(acelerado)], check=True)
        dur = duracao_media(acelerado)
        log(f"  Voz: gTTS ({dur:.1f}s)")
        return acelerado, _estimar_tempos(texto, dur), dur
    except Exception as e:
        log(f"  gTTS falhou ({type(e).__name__})")

    # 3) espeak-ng local (só para testes sem internet)
    if shutil.which("espeak-ng"):
        wav = pasta / f"{nome}.wav"
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
Style: Leg,Anton,118,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,7,3,5,70,150,0,1

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


def _commons_procurar(q: str, tipo: str) -> list:
    prop = "videoinfo" if tipo == "video" else "imageinfo"
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": 15,
              "gsrsearch": f"{q} filetype:{'video' if tipo == 'video' else 'bitmap'}", "prop": prop}
    if tipo == "video":
        params["viprop"] = "url|size|mime|extmetadata|derivatives"
    else:
        params.update({"iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1600})
    r = requests.get(COMMONS_API, params=params, headers={"User-Agent": UA_WIKI}, timeout=25)
    r.raise_for_status()
    paginas = sorted(r.json().get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    out = []
    for p in paginas:
        info = (p.get(prop) or [{}])[0]
        meta = info.get("extmetadata", {})
        lic = _meta(meta, "LicenseShortName").strip()
        if not LICENCAS_LIVRES.match(lic) or re.search(r"\b(NC|ND)\b", lic):
            continue
        texto = p.get("title", "") + " " + _meta(meta, "ImageDescription") + " " + _meta(meta, "ObjectName")
        if not (tokens(q) & tokens(texto)):
            continue   # o ficheiro tem de mencionar o termo pesquisado
        if re.search(r"wiki(media|pedia|mania|data)|webinar|tutorial|palestra|oficina|workshop", texto, re.I):
            continue   # evita vídeos/fotos sobre a própria Wikipédia, aulas e eventos
        ano = re.search(r"(19|20)\d\d", _meta(meta, "DateTimeOriginal") or _meta(meta, "DateTime"))
        out.append({"titulo": p.get("title", ""), "info": info, "lic": lic, "index": p.get("index", 99),
                    "autor": _meta(meta, "Artist") or "autor desconhecido", "ano": int(ano.group(0)) if ano else 0})
    return out


def commons(buscas: list, quantidade: int, pasta, usados: set) -> list:
    """Fotos reais do tema (pessoas, instituições, locais) no Wikimedia Commons, só com licenças livres."""
    fotos = []
    for q in buscas or []:
        if len(fotos) >= quantidade:
            break
        try:
            cand = _commons_procurar(q, "foto")
        except Exception as e:
            log(f"  Commons '{q}': {type(e).__name__}")
            continue
        cand = [c for c in cand if c["titulo"] not in usados and all(f["id"] != c["titulo"] for f in fotos)
                and c["info"].get("mime") in ("image/jpeg", "image/png") and c["info"].get("width", 0) >= 900]
        cand.sort(key=lambda c: (0 if c["info"].get("width", 0) >= c["info"].get("height", 1) else 1, c["index"]))
        por_busca = 0
        for c in cand:
            if por_busca >= 2 or len(fotos) >= quantidade:
                break
            try:
                img = requests.get(c["info"].get("thumburl") or c["info"]["url"], headers={"User-Agent": UA_WIKI}, timeout=40)
                img.raise_for_status()
                destino = pasta / f"commons_{len(fotos)}.jpg"
                destino.write_bytes(img.content)
                Image.open(destino).verify()
            except Exception:
                continue
            fotos.append({"tipo": "foto", "caminho": destino, "id": c["titulo"], "autor": c["autor"], "licenca": c["lic"],
                          "fonte": "Wikimedia Commons", "url": c["info"].get("descriptionurl", "")})
            por_busca += 1
    log(f"  Fotos do tema (Wikimedia Commons): {len(fotos)}")
    return fotos


def _melhor_versao(info: dict) -> str:
    """Escolhe a versão do vídeo mais próxima de 720p (as cópias do Commons são mais leves que o original)."""
    opcoes = []
    for d in info.get("derivatives") or []:
        h = int(d.get("height") or 0)
        if 360 <= h <= 1080 and d.get("src"):
            opcoes.append((abs(h - 720), d["src"]))
    if opcoes:
        return sorted(opcoes)[0][1]
    return info.get("url", "")


def commons_videos(buscas: list, maximo: int, pasta, usados: set) -> list:
    """Cortes de vídeo reais do tema (Planalto, TV Senado, STF...) com licenças livres no Wikimedia Commons."""
    clips = []
    for q in buscas or []:
        if len(clips) >= maximo:
            break
        try:
            cand = _commons_procurar(q, "video")
        except Exception as e:
            log(f"  Commons vídeo '{q}': {type(e).__name__}")
            continue
        cand = [c for c in cand if c["titulo"] not in usados and all(x["id"] != c["titulo"] for x in clips)
                and float(c["info"].get("duration") or 0) >= 12 and int(c["info"].get("width") or 0) >= 640]
        # mais recentes e em HD primeiro
        cand.sort(key=lambda c: (-(c["ano"] >= 2018), -(int(c["info"].get("height") or 0) >= 720), -c["ano"], c["index"]))
        for c in cand[:2]:
            dur_total = float(c["info"]["duration"])
            inicio = max(2.0, min(dur_total * 0.25, dur_total - 14))
            destino = pasta / f"clip_{len(clips)}.mp4"
            try:
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-user_agent", UA_WIKI, "-ss", f"{inicio:.1f}",
                                "-i", _melhor_versao(c["info"]), "-t", "11", "-an", "-vf", "scale=-2:720",
                                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", str(destino)],
                               check=True, capture_output=True, timeout=150)
                d = duracao_media(destino)
            except Exception as e:
                log(f"  Não consegui cortar '{c['titulo'][:50]}': {type(e).__name__}")
                continue
            if d < 4:
                continue
            clips.append({"tipo": "video", "caminho": destino, "dur": d, "id": c["titulo"], "autor": c["autor"],
                          "licenca": c["lic"], "fonte": "Wikimedia Commons", "ano": c["ano"],
                          "url": c["info"].get("descriptionurl", "")})
            break  # um corte por pesquisa, para variar
    log(f"  Cortes de vídeo do tema (Wikimedia Commons): {len(clips)}")
    return clips


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
            fotos.append({"tipo": "foto", "caminho": destino, "id": f"pexels:{foto['id']}",
                          "autor": foto.get("photographer", "Pexels"), "licenca": "Licença Pexels",
                          "fonte": "Pexels", "url": foto.get("url", "")})
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
        out.append({"tipo": "foto", "caminho": destino, "id": None, "autor": None})
    return out


def obter_imagens(roteiro: dict, cfg: dict, pasta, historico: list, excluir=None) -> list:
    """Devolve a sequência de cortes de vídeo e fotos do tema (alternados)."""
    n = int(cfg["imagens"].get("quantidade", 4))
    max_clips = int(cfg["imagens"].get("cortes_video", 2))
    usados = set(excluir or [])
    for h in historico[-250:]:
        usados.update(h.get("fotos", []))
        usados.update(f"pexels:{x}" for x in h.get("fotos_pexels", []))
    buscas = roteiro.get("buscas_commons", [])
    clips = commons_videos(buscas, max_clips, pasta, usados) if max_clips > 0 else []
    fotos = commons(buscas, n, pasta, usados)
    if len(fotos) < n:
        fotos += pexels(roteiro.get("buscas_imagens", []), n - len(fotos), pasta, usados)
    if not clips and not fotos:
        log("  Sem fotos nem vídeos: a usar fundos gerados")
        return fundos_gerados(n, pasta)
    # alterna: vídeo, foto, vídeo, foto, foto...
    seq, fi = [], 0
    for c in clips:
        seq.append(c)
        if fi < len(fotos):
            seq.append(fotos[fi])
            fi += 1
    seq += fotos[fi:]
    return seq


# ======================================================================
# EXCERTO (trecho real de entrevista/discurso, com o vídeo e o som originais)
# ======================================================================
PAUSA_EXCERTO = 0.3   # pequeno silêncio antes e depois do trecho
_WHISPER = {}

ESQ_TRANSCRICAO = {
    "type": "OBJECT",
    "properties": {"segmentos": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {"ini": {"type": "NUMBER"}, "fim": {"type": "NUMBER"}, "texto": {"type": "STRING"}},
        "required": ["ini", "fim", "texto"]}}},
    "required": ["segmentos"],
}

ESQ_TRECHO = {
    "type": "OBJECT",
    "properties": {
        "usar": {"type": "BOOLEAN"},
        "motivo": {"type": "STRING"},
        "primeira": {"type": "INTEGER"},
        "ultima": {"type": "INTEGER"},
        "orador": {"type": "STRING"},
        "ponte": {"type": "STRING"},
        "traducao": {"type": "STRING"},
    },
    "required": ["usar", "motivo", "primeira", "ultima", "orador", "ponte"],
}


def _data_video(meta: dict) -> str:
    bruto = _meta(meta, "DateTimeOriginal") or _meta(meta, "DateTime")
    m = re.search(r"((?:19|20)\d\d)-(\d\d)-(\d\d)", bruto)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    m = re.search(r"(?:19|20)\d\d", bruto)
    return m.group(0) if m else ""


def _transcrever_whisper(audio, cfg: dict) -> list:
    from faster_whisper import WhisperModel
    nome = cfg.get("excerto", {}).get("modelo_transcricao", "small")
    if nome not in _WHISPER:
        _WHISPER[nome] = WhisperModel(nome, device="cpu", compute_type="int8")
    import numpy as np   # descodifica com o ffmpeg (evita incompatibilidades do PyAV)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(audio), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    segs, info = _WHISPER[nome].transcribe(np.frombuffer(pcm, np.float32), word_timestamps=True,
                                           vad_filter=True, beam_size=2)
    lingua = getattr(info, "language", "pt") or "pt"
    out = []
    for s in segs:
        palavras = [{"texto": w.word.strip(), "ini": float(w.start), "fim": float(w.end)}
                    for w in (s.words or []) if w.word.strip()]
        if palavras:
            out.append({"ini": palavras[0]["ini"], "fim": palavras[-1]["fim"],
                        "texto": s.text.strip(), "palavras": palavras, "lingua": lingua})
    return out


def _transcrever_gemini(audio, cfg: dict) -> list:
    dados = base64.b64encode(Path(audio).read_bytes()).decode()
    res = chamar_gemini(
        "Transcreva este áudio em português, palavra por palavra, sem resumir nem corrigir. "
        "Divida-o em frases e indique o início e o fim de cada uma, em segundos desde o começo do áudio. "
        'Responda em JSON: {"segmentos": [{"ini": 0.0, "fim": 4.2, "texto": "..."}]}',
        cfg["ia"]["modelos"], ESQ_TRANSCRICAO,
        anexos=[{"inline_data": {"mime_type": "audio/mp3", "data": dados}}])
    out = []
    for s in res.get("segmentos", []):
        ini, fim, txt = float(s["ini"]), float(s["fim"]), str(s["texto"]).strip()
        if txt and fim > ini:
            ws = _estimar_tempos(txt, fim - ini)
            out.append({"ini": ini, "fim": fim, "texto": txt,
                        "palavras": [{**w, "ini": w["ini"] + ini, "fim": w["fim"] + ini} for w in ws]})
    return out


def transcrever(audio, cfg: dict) -> list:
    """Transcrição com tempos por palavra (Whisper local; se falhar, Gemini)."""
    try:
        segs = _transcrever_whisper(audio, cfg)
        log(f"  Transcrição (Whisper): {len(segs)} frases")
        return segs
    except Exception as e:
        log(f"  Whisper indisponível ({type(e).__name__}: {str(e)[:100]}); a transcrever com o Gemini")
    try:
        segs = _transcrever_gemini(audio, cfg)
        log(f"  Transcrição (Gemini): {len(segs)} frases")
        return segs
    except Exception as e:
        log(f"  Transcrição falhou: {type(e).__name__}: {str(e)[:100]}")
        return []


def _candidatos_excerto(buscas: list, usados: set, tema: set) -> list:
    vistos, out = set(), []
    for q in buscas:
        try:
            cand = _commons_procurar(q, "video")
        except Exception as e:
            log(f"  Commons trecho '{q}': {type(e).__name__}")
            continue
        for c in cand:
            dur = float(c["info"].get("duration") or 0)
            meta = c["info"].get("extmetadata", {})
            descricao = limpar_html((meta.get("ImageDescription") or {}).get("value", ""))[:700]
            if c["titulo"] in usados or c["titulo"] in vistos or not 15 <= dur <= 3600:
                continue
            tk = tokens(c["titulo"] + " " + descricao)
            if len(tokens(q) & tk) < max(1, (len(tokens(q)) + 1) // 2):
                continue   # a pesquisa (nome e tema) tem de aparecer no título ou na descrição
            vistos.add(c["titulo"])
            c.update({"dur": dur, "busca": q, "data": _data_video(meta), "descricao": descricao,
                      "afinidade": len(tema & tk)})
            out.append(c)
    # primeiro os que falam do mesmo tema; depois recentes, curtos (mais focados) e mais relevantes na pesquisa
    out.sort(key=lambda c: (-min(c["afinidade"], 4), -(c["ano"] >= 2019), -(c["dur"] <= 900), -c["ano"], c["index"]))
    return out


ESQ_PRESEL = {"type": "OBJECT", "properties": {"indices": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
              "required": ["indices"]}


def _preselecionar(cands: list, roteiro: dict, cfg: dict) -> list:
    """Pede à IA os vídeos (pelo título/descrição) com mais probabilidade de falarem do mesmo assunto."""
    lista = cands[:25]
    linhas = [f"[{i}] {c['titulo'][5:110]} ({c['data'] or 's/ data'}; {c['dur'] / 60:.0f} min) — {c['descricao'][:220]}"
              for i, c in enumerate(lista)]
    prompt = f"""NOTÍCIA: {roteiro['manchete']}
RESUMO: {roteiro['roteiro'][:700]}

VÍDEOS DE ARQUIVO DISPONÍVEIS (título, data, duração, descrição):
{chr(10).join(linhas)}

Quais destes vídeos têm boa probabilidade de mostrar uma figura pública a FALAR sobre o MESMO assunto da notícia?
Não basta aparecer a mesma pessoa: o tema tem de ser o mesmo ou diretamente ligado. Prefira entrevistas, discursos e declarações.
Devolva até 3 índices, do mais provável para o menos provável, ou uma lista vazia se nenhum servir.
Responda em JSON: {{"indices": [ ... ]}}"""
    res = chamar_gemini(prompt, cfg["ia"]["modelos"], ESQ_PRESEL)
    out = []
    for i in res.get("indices", []):
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(lista) and lista[i] not in out:
            out.append(lista[i])
    return out[:3]


def _escolher_trecho(segs: list, cand: dict, roteiro: dict, cfg: dict) -> dict:
    dmax = float(cfg.get("excerto", {}).get("duracao_max", 15))
    linhas = [f"[{i}] ({s['ini']:.1f}s a {s['fim']:.1f}s) {s['texto']}" for i, s in enumerate(segs)]
    prompt = f"""Vamos inserir num Short de notícia um TRECHO REAL (vídeo e som originais) de um vídeo de arquivo.

NOTÍCIA DO SHORT
Manchete: {roteiro['manchete']}
Roteiro: {roteiro['roteiro']}

VÍDEO DE ARQUIVO (Wikimedia Commons)
Título: {cand['titulo']}
Descrição: {cand['descricao'] or '(sem descrição)'}
Data: {cand['data'] or 'desconhecida'}
Autor/canal: {cand['autor']}

TRANSCRIÇÃO AUTOMÁTICA (frases numeradas, com tempos; pode ter pequenos erros)
{chr(10).join(linhas)}

Escolha frases CONSECUTIVAS (da "primeira" à "ultima") que formem um trecho que cumpra TODAS estas regras:
- entre {max(5.0, dmax - 9):.0f} e {dmax:.0f} segundos no total;
- dito por UMA figura pública identificável (não por repórter, apresentador ou locutor; sem perguntas de jornalistas);
- ideia completa, que começa e termina em fim de frase e se entende sozinha;
- sobre o MESMO ASSUNTO da notícia (não basta ser a mesma pessoa a falar de outro tema);
- não pode enganar quem vê: nada que pareça uma reação à notícia de hoje se o vídeo for de outra data.
Se nenhum trecho cumprir todas as regras, ou se o vídeo for de outro assunto, responda "usar": false.

"orador": nome e cargo da pessoa na data do vídeo (ex.: "Lula, presidente da República").
"ponte": UMA frase curta para a narração dizer ANTES do trecho: quem fala, QUANDO (mês e ano, ou só o ano) e em que contexto. Ex.: "Em maio de 2023, numa entrevista, o presidente Lula falou sobre o tema:". Termine com dois pontos.
"motivo": uma frase a explicar a escolha ou a recusa.
"traducao": se o trecho NÃO estiver em português, a tradução fiel para português do Brasil (vai para as legendas); se estiver em português, deixe vazio.
Não invente nada que não esteja na transcrição ou nos dados do vídeo."""
    return chamar_gemini(prompt, cfg["ia"]["modelos"], ESQ_TRECHO)


ESQ_BUSCAS_NOTICIAS = {"type": "OBJECT", "properties": {"noticias": {"type": "ARRAY", "items": {
    "type": "OBJECT", "properties": {"id": {"type": "INTEGER"}, "pessoas": {"type": "ARRAY", "items": {"type": "STRING"}},
                                     "buscas": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["id", "pessoas", "buscas"]}}}, "required": ["noticias"]}


def noticias_com_arquivo(grupos: list, cfg: dict, historico: list) -> list:
    """Deixa só as notícias para as quais há, no Wikimedia Commons, vídeos de figuras públicas sobre o mesmo tema."""
    linhas = [f"[{i}] {g['itens'][0]['titulo']}" for i, g in enumerate(grupos)]
    res = chamar_gemini(f"""Para cada notícia abaixo, indique as figuras públicas centrais (nome como são conhecidas) e
1 ou 2 pesquisas curtas (nome + tema em uma ou duas palavras, ex.: "Lula reforma tributária", "Haddad imposto de renda")
para encontrar vídeos de arquivo em que essas pessoas falam sobre esse assunto. Se não houver figura pública central, use listas vazias.

{chr(10).join(linhas)}

Responda em JSON: {{"noticias": [{{"id": 0, "pessoas": ["..."], "buscas": ["..."]}}]}}""",
                        cfg["ia"]["modelos"], ESQ_BUSCAS_NOTICIAS)
    usados = set()
    for h in historico[-300:]:
        usados.update(h.get("fotos", []))
    com_arquivo = []
    for n in res.get("noticias", []):
        try:
            g = grupos[int(n["id"])]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        pessoas = [p for p in n.get("pessoas", []) if p][:2]
        buscas = [b for b in n.get("buscas", []) if b][:2]
        if not pessoas or not buscas:
            continue
        it = g["itens"][0]
        tema = tokens(it["titulo"] + " " + it.get("resumo", "")) - set().union(*(tokens(p) for p in pessoas))
        cands = [c for c in _candidatos_excerto(buscas, usados, tema) if c["afinidade"] >= 1]
        if cands:
            g["_buscas_trecho"], g["_pessoas"], g["_afinidade"] = buscas, pessoas, cands[0]["afinidade"]
            g["_cands"] = cands
            com_arquivo.append(g)
    com_arquivo.sort(key=lambda g: -g["_afinidade"])
    log(f"   Notícias com vídeos de arquivo sobre o tema: {len(com_arquivo)} de {len(grupos)}")
    return com_arquivo


def procurar_excerto(roteiro: dict, cfg: dict, pasta, historico: list):
    """Procura no Wikimedia Commons um trecho de entrevista/discurso sobre o tema e corta-o (vídeo + som)."""
    dmax = float(cfg.get("excerto", {}).get("duracao_max", 15))
    usados = set()
    for h in historico[-300:]:
        usados.update(h.get("fotos", []))
    pessoas = [p.strip() for p in roteiro.get("pessoas", []) if p and p.strip()][:3]
    tematicas = [b.strip() for b in roteiro.get("buscas_trecho", []) if b and b.strip()][:4]
    if not tematicas:   # pessoa + palavra-chave da notícia
        chaves = [t for t in roteiro.get("tags", []) if t and not any(tokens(p) & tokens(t) for p in pessoas)]
        tematicas = [f"{p} {t}" for p in pessoas[:2] for t in chaves[:2]]
    buscas = list(dict.fromkeys(tematicas + (pessoas or roteiro.get("buscas_commons", [])[:2])))
    tema = tokens(roteiro.get("manchete", "") + " " + roteiro.get("roteiro", "")) - set().union(*(tokens(p) for p in pessoas))
    if roteiro.get("_presel"):          # já pré-selecionados ao escolher a notícia
        bons = roteiro["_presel"]
        log(f"  Vídeos de arquivo já pré-selecionados: {len(bons)}")
    else:
        cands = _candidatos_excerto(buscas, usados, tema)
        log(f"  Vídeos de arquivo ({'; '.join(buscas) or '-'}): {len(cands)}")
        if not cands:
            return None
        try:
            bons = _preselecionar(cands, roteiro, cfg)
        except Exception as e:
            log(f"  Pré-seleção falhou ({type(e).__name__}); a usar a afinidade de palavras")
            bons = [c for c in cands if c["afinidade"] >= 2][:2]
    log(f"  Com probabilidade de falar do tema: {len(bons)}" + "".join(f"\n    - {c['titulo'][5:90]}" for c in bons))
    for n, c in enumerate(bons):
        janela = min(c["dur"], 240.0)
        ini_j = 0.0 if c["dur"] <= 270 else max(0.0, min(c["dur"] * 0.12, c["dur"] - janela))
        audio = pasta / f"trecho_audio_{n}.mp3"
        url = None
        for u in dict.fromkeys([_melhor_versao(c["info"]), c["info"].get("url", "")]):   # cópia leve; senão o original
            try:
                correr(["ffmpeg", "-y", "-v", "error", "-user_agent", UA_WIKI, "-ss", f"{ini_j:.1f}", "-i", u,
                        "-t", f"{janela:.1f}", "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio)],
                       timeout=240)
                if duracao_media(audio) >= 10:
                    url = u
                    break
            except Exception:
                pass
        if not url:
            log(f"  Sem som utilizável em '{c['titulo'][5:65]}'")
            continue
        segs = transcrever(audio, cfg)
        if sum(len(s["texto"].split()) for s in segs) < 15:
            log(f"  '{c['titulo'][5:65]}': quase sem fala")
            continue
        try:
            esc = _escolher_trecho(segs, c, roteiro, cfg)
        except Exception as e:
            log(f"  Escolha do trecho falhou: {type(e).__name__}: {str(e)[:100]}")
            continue
        log(f"  '{c['titulo'][5:65]}' ({c['data'] or 's/ data'}): "
            f"{'USAR' if esc.get('usar') else 'não serve'} — {str(esc.get('motivo', ''))[:160]}")
        if not esc.get("usar"):
            continue
        a, b = int(esc.get("primeira", -1)), int(esc.get("ultima", -1))
        if not 0 <= a <= b < len(segs):
            continue
        falas = [w for s in segs[a:b + 1] for w in s["palavras"]]
        t0 = max(0.0, falas[0]["ini"] - 0.12)
        dur = falas[-1]["fim"] + 0.3 - t0
        if not 4.0 <= dur <= dmax + 4:
            log(f"  Trecho com {dur:.1f}s: fora dos limites")
            continue
        clip, som = pasta / "trecho.mp4", pasta / "trecho_som.wav"
        try:
            correr(["ffmpeg", "-y", "-v", "error", "-user_agent", UA_WIKI, "-ss", f"{ini_j + t0:.2f}", "-i", url,
                    "-t", f"{dur + 1.2:.2f}", "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(clip)], timeout=240)
            correr(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-t", f"{dur:.2f}", "-vn",
                    "-af", f"afade=t=in:d=0.08,afade=t=out:st={dur - 0.25:.2f}:d=0.25,"
                           f"loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000", "-ac", "2", str(som)])
        except Exception as e:
            log(f"  Não consegui cortar o trecho: {type(e).__name__}: {str(e)[:160]}")
            continue
        palavras = [{"texto": w["texto"], "ini": w["ini"] - t0, "fim": w["fim"] - t0} for w in falas]
        texto = " ".join(s["texto"] for s in segs[a:b + 1])
        traducao = str(esc.get("traducao") or "").strip()
        if segs[a].get("lingua", "pt") != "pt" and traducao:   # legendas com a tradução
            palavras = _estimar_tempos(traducao, dur)
            texto = f"{traducao} (tradução)"
        log(f"  ✓ Trecho de {dur:.1f}s: {esc['orador']} ({c['data'] or 's/ data'})")
        return {"tipo": "excerto", "caminho": clip, "som": som, "dur": dur, "palavras": palavras,
                "texto": texto, "orador": str(esc["orador"]).strip(),
                "ponte": str(esc["ponte"]).strip(), "data": c["data"], "ano": c["ano"], "id": c["titulo"],
                "autor": c["autor"], "licenca": c["lic"], "fonte": "Wikimedia Commons",
                "url": c["info"].get("descriptionurl", "")}
    return None


def dividir_roteiro(texto: str, ponte: str) -> tuple:
    """Parte o roteiro em duas (por volta de 1/3, num fim de frase) e acrescenta a ponte à 1.ª parte."""
    frases = [f for f in re.split(r"(?<=[.!?])\s+", texto.strip()) if f]
    total, acum, corte = len(texto.split()), 0, 1
    for i, f in enumerate(frases[:-1]):          # a chamada final fica sempre na 2.ª parte
        acum += len(f.split())
        corte = i + 1
        if i >= 1 and acum >= total * 0.33:
            break
    ponte = ponte.strip()
    if ponte and not ponte.endswith(":"):
        ponte = ponte.rstrip(".") + ":"
    return " ".join(frases[:corte] + [ponte]).strip(), " ".join(frases[corte:]).strip()


def narracao_com_excerto(texto: str, excerto: dict, cfg: dict, pasta) -> tuple:
    """Narração 1.ª parte + som original do trecho + narração 2.ª parte, com os tempos das palavras."""
    p1, p2 = dividir_roteiro(texto, excerto["ponte"])
    a1, w1, _ = sintetizar(p1, cfg, pasta, "narracao1")
    a2, w2, _ = sintetizar(p2, cfg, pasta, "narracao2")
    wavs = []
    for i, a in enumerate([a1, excerto["som"], a2]):
        w = pasta / f"parte_{i}.wav"
        correr(["ffmpeg", "-y", "-v", "error", "-i", str(a), "-ar", "48000", "-ac", "2", str(w)])
        wavs.append(w)
    d1, dex, d2 = (duracao_media(w) for w in wavs)
    t_ex = d1 + PAUSA_EXCERTO
    t_pos = t_ex + dex + PAUSA_EXCERTO
    audio = pasta / "narracao_total.wav"
    correr(["ffmpeg", "-y", "-v", "error", "-i", str(wavs[0]), "-i", str(wavs[1]), "-i", str(wavs[2]),
            "-filter_complex", f"[0:a]apad=pad_dur={PAUSA_EXCERTO}[a];[1:a]apad=pad_dur={PAUSA_EXCERTO}[b];"
                               f"[a][b][2:a]concat=n=3:v=0:a=1[o]", "-map", "[o]", str(audio)])
    falas = [dict(w) for w in excerto["palavras"] if w["ini"] < dex]
    if falas:   # aspas no trecho original
        falas[0]["texto"] = '"' + falas[0]["texto"]
        falas[-1]["texto"] = re.sub(r"[.,;:!?]+$", "", falas[-1]["texto"]) + '"'
    palavras = (w1 + [{**w, "ini": w["ini"] + t_ex, "fim": min(w["fim"], dex) + t_ex} for w in falas]
                + [{**w, "ini": w["ini"] + t_pos, "fim": w["fim"] + t_pos} for w in w2])
    excerto["dur"] = dex
    log(f"  Narração {d1:.1f}s + trecho original {dex:.1f}s + narração {d2:.1f}s")
    return audio, palavras, duracao_media(audio), t_ex


def inserir_excerto(itens: list, excerto: dict, t_ex: float, total: float) -> tuple:
    """Põe o trecho na sequência de imagens, exatamente no momento em que o som dele começa."""
    n = len(itens)
    d_ex = excerto["dur"]
    if n <= 1:
        antes, depois = list(itens), list(itens)
    else:
        k = min(n - 1, max(1, round(n * t_ex / max(total - d_ex, 1.0))))
        antes, depois = list(itens[:k]), list(itens[k:])
        if all(it.get("tipo") == "video" for it in antes) and len(depois) > 1:
            j = next((i for i, it in enumerate(depois) if it.get("tipo") != "video"), None)
            if j is not None:          # pelo menos uma foto antes do trecho (os cortes têm duração limitada)
                antes.append(depois.pop(j))
    durs = _duracoes(antes, t_ex) + [d_ex] + _duracoes(depois, max(total - t_ex - d_ex, 1.0))
    return antes + [excerto] + depois, durs


def _excerto_teste(pasta) -> dict:
    """Trecho sintético para o teste offline."""
    clip, som = pasta / "trecho.mp4", pasta / "trecho_som.wav"
    correr(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "9.5",
            "-c:v", "libx264", "-preset", "ultrafast", str(clip)])
    correr(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000",
            "-t", "8", "-ac", "2", str(som)])
    frase = "Esta é uma frase de teste dita por uma figura pública num vídeo de arquivo."
    return {"tipo": "excerto", "caminho": clip, "som": som, "dur": 8.0, "palavras": _estimar_tempos(frase, 8.0),
            "texto": frase, "orador": "Pessoa de teste, cargo de teste", "data": "22/05/2023", "ano": 2023,
            "ponte": "Num vídeo de arquivo de 2023, uma pessoa de teste falou sobre o tema:",
            "id": None, "autor": "Teste", "licenca": "CC0", "fonte": "teste", "url": ""}


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


def camada_grafica(caminho, titulo_topo: str, titulo_destaque: str, etiqueta: str, canal: str, fontes: list,
                   formato: str = "faixas") -> None:
    """Parte de cima: etiqueta, canal, manchete em duas cores e fontes."""
    img = Image.new("RGBA", (L, A), (0, 0, 0, 0))
    sombra = Image.new("L", (L, A), 0)
    ds = ImageDraw.Draw(sombra)
    for y in range(FAIXA_Y):
        ds.line([(0, y), (L, y)], fill=int((200 if formato == "ecra_inteiro" else 150) * (1 - 0.55 * y / FAIXA_Y)))
    if formato == "ecra_inteiro":   # escurece também a zona das legendas
        for y in range(1300, A):
            ds.line([(0, y), (L, y)], fill=int(170 * (y - 1300) / (A - 1300)))
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

    # manchete: parte branca + parte amarela (maior); encolhe até caber
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

    if formato != "ecra_inteiro":   # linhas amarelas a enquadrar a faixa central
        d.rectangle([0, FAIXA_Y - 6, L, FAIXA_Y - 1], fill=AMARELO)
        d.rectangle([0, FAIXA_Y + FAIXA_H, L, FAIXA_Y + FAIXA_H + 5], fill=AMARELO)
    img.save(caminho)


def _png_credito(item: dict, destino, formato: str) -> None:
    """Camada transparente com o crédito da foto/vídeo."""
    img = Image.new("RGBA", (L, A), (0, 0, 0, 0))
    if item.get("tipo") == "excerto":
        _rotulo_excerto(img, item, formato)
    if item.get("autor"):
        d = ImageDraw.Draw(img)
        quem = "Vídeo" if item.get("tipo") in ("video", "excerto") else "Foto"
        ano = f" ({item['ano']})" if item.get("ano") else ""
        credito = f"{quem}: {item['autor']}{ano} · {item.get('licenca', '')} · {item.get('fonte', '')}"
        f = _fonte("SemiBold", 22)
        while d.textlength(credito, font=f) > L - 80 and len(credito) > 20:
            credito = credito[:-4] + "…"
        y = FAIXA_Y + FAIXA_H + 16 if formato != "ecra_inteiro" else 1290
        d.text((L - 38, y + 2), credito, font=f, fill=(0, 0, 0, 200), anchor="ra")
        d.text((L - 40, y), credito, font=f, fill=(225, 225, 225, 255), anchor="ra")
    img.save(destino)


def _rotulo_excerto(img, item: dict, formato: str) -> None:
    """Rótulo do trecho original: "TRECHO ORIGINAL · data" + nome e cargo de quem fala."""
    d = ImageDraw.Draw(img)
    base = FAIXA_Y + FAIXA_H - 128 if formato != "ecra_inteiro" else 1120
    f_tag, f_nome = _fonte("ExtraBold", 26), _fonte("ExtraBold", 34)
    tag = "TRECHO ORIGINAL" + (f" · {item['data']}" if item.get("data") else "")
    tw = d.textlength(tag, font=f_tag)
    d.rounded_rectangle([36, base, 36 + tw + 28, base + 44], radius=8, fill=VERMELHO)
    d.text((50, base + 22), tag, font=f_tag, fill="white", anchor="lm")
    nome = (item.get("orador") or "").upper()
    if nome:
        while d.textlength(nome, font=f_nome) > L - 110 and len(nome) > 12:
            nome = nome[:-3].rstrip() + "…"
        nw = d.textlength(nome, font=f_nome)
        d.rectangle([36, base + 52, 36 + nw + 32, base + 108], fill=(0, 0, 0, 200))
        d.text((52, base + 80), nome, font=f_nome, fill="white", anchor="lm")


def _fundo_desfocado(caminho_img, destino) -> None:
    im = Image.open(caminho_img).convert("RGB")
    bg = ImageOps.fit(im, (L, A), centering=(0.5, 0.35)).filter(ImageFilter.GaussianBlur(38))
    ImageEnhance.Brightness(bg).enhance(0.42).save(destino, quality=93)


_MOVIMENTOS = [
    ("1+0.08*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),            # aproximar
    ("1.08-0.08*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),         # afastar
    ("1.08", "(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),                  # esquerda -> direita
    ("1.08", "(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),              # direita -> esquerda
]
_ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "17", "-pix_fmt", "yuv420p", "-r", str(FPS)]


def _segmento(item: dict, saida, dur: float, mov: int, pasta, i: int, formato: str) -> None:
    n = max(2, int(round(dur * FPS)))
    cred = pasta / f"cred_{i}.png"
    _png_credito(item, cred, formato)
    w, h, ytop = (L, A, 0) if formato == "ecra_inteiro" else (L, FAIXA_H, FAIXA_Y)

    if item.get("tipo") in ("video", "excerto"):
        # vídeo real: no centro (ou ecrã inteiro) + o mesmo vídeo desfocado por trás
        filtro = (f"[0:v]fps={FPS},setsar=1,tpad=stop_mode=clone:stop_duration=15,split[a][b];"
                  f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}[fg];"
                  f"[b]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=6,"
                  f"scale={L}:{A},eq=brightness=-0.28:saturation=0.85[bg];"
                  f"[bg][fg]overlay=0:{ytop}[v0];[v0][1:v]overlay=0:0,format=yuv420p[v]")
        correr(["ffmpeg", "-y", "-v", "error", "-i", str(item["caminho"]), "-loop", "1", "-i", str(cred),
                "-filter_complex", filtro, "-map", "[v]", "-frames:v", str(n), *_ENC, str(saida)])
        return

    # foto: zoom lento + foto desfocada por trás
    base = pasta / f"base_{i}.jpg"
    _fundo_desfocado(item["caminho"], base)
    z, x, y = (s.format(n=n) for s in _MOVIMENTOS[mov % len(_MOVIMENTOS)])
    filtro = (f"[1:v]scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,"
              f"crop={w * 2}:{h * 2}:(iw-{w * 2})/2:(ih-{h * 2})*0.3,setsar=1,"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={w}x{h}:fps={FPS}[fg];"
              f"[0:v][fg]overlay=0:{ytop}:shortest=1[v0];[v0][2:v]overlay=0:0,format=yuv420p[v]")
    correr(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(base),
            "-i", str(item["caminho"]), "-loop", "1", "-i", str(cred), "-filter_complex", filtro,
            "-map", "[v]", "-frames:v", str(n), *_ENC, str(saida)])


def _duracoes(itens: list, total: float) -> list:
    """Tempo de ecrã de cada item: vídeos até 8 s (no máximo 60% do total); fotos dividem o resto."""
    videos = [i for i, it in enumerate(itens) if it.get("tipo") == "video"]
    fotos = [i for i in range(len(itens)) if i not in videos]
    dur = [0.0] * len(itens)
    t_videos = 0.0
    for i in videos:
        dur[i] = min(8.0, itens[i]["dur"] - 0.6)
        t_videos += dur[i]
    if fotos:
        limite = total * 0.6
        if t_videos > limite:
            for i in videos:
                dur[i] *= limite / t_videos
            t_videos = limite
        for i in fotos:
            dur[i] = (total - t_videos) / len(fotos)
    else:   # só vídeos: esticam proporcionalmente (o último fotograma fica parado se faltar)
        for i in videos:
            dur[i] = dur[i] * total / max(t_videos, 0.1)
    return dur


def fundo(itens: list, duracao: float, pasta, formato: str = "faixas", durs: list | None = None) -> object:
    T = 0.45  # duração da transição
    durs = durs or _duracoes(itens, duracao)
    segs = []
    inicio = random.randint(0, 3)
    for i, (item, d) in enumerate(zip(itens, durs)):
        s = pasta / f"seg_{i}.mp4"
        _segmento(item, s, d + T, inicio + i, pasta, i, formato)
        segs.append(s)
    saida = pasta / "fundo.mp4"
    if len(segs) == 1:
        shutil.copy(segs[0], saida)
        return saida
    entradas, filtros, ant, off = [], [], "[0:v]", 0.0
    for s in segs:
        entradas += ["-i", str(s)]
    for i in range(1, len(segs)):
        off += durs[i - 1]
        rot = f"[x{i}]"
        filtros.append(f"{ant}[{i}:v]xfade=transition=fade:duration={T}:offset={off:.3f}{rot}")
        ant = rot
    correr(["ffmpeg", "-y", "-v", "error", *entradas, "-filter_complex", ";".join(filtros),
            "-map", ant, *_ENC, str(saida)])
    return saida


def montar(itens: list, audio, ass, grafico, duracao_audio: float, pasta, saida, formato: str = "faixas",
           durs: list | None = None) -> None:
    total = duracao_audio + 0.9
    log(f"  A montar vídeo de {total:.1f}s com {len(itens)} cortes/imagens ({formato})")
    bg = fundo(itens, total, pasta, formato, durs)
    fontes_local = pasta / "fonts"
    fontes_local.mkdir(exist_ok=True)
    for f in FONTES_DIR.glob("*.ttf"):
        shutil.copy(f, fontes_local / f.name)
    filtro = (f"[0:v][1:v]overlay=0:0,subtitles={ass.name}:fontsdir=fonts,format=yuv420p[v];"
              f"[2:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,apad[a]")
    correr(["ffmpeg", "-y", "-v", "error", "-i", bg.name, "-i", grafico.name, "-i", str(audio),
            "-filter_complex", filtro, "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-r", str(FPS),
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
    modo = os.environ.get("MODO_YT") or yt_cfg.get("modo", "agendado")
    if modo == "config":
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
            quem = {"video": "Vídeo", "excerto": "Trecho de vídeo (som original)"}.get(c.get("tipo"), "Foto")
            partes.append(f"• {quem}: {c['autor']} ({c.get('licenca', '')}), via {c.get('fonte', '')}: {c.get('url', '')}")
    tags = ["Shorts", "Notícias", "Brasil"] + [h.lstrip("#").replace(" ", "") for h in roteiro.get("hashtags", [])]
    vistos, hashtags = set(), []
    for t in tags:
        if t and t.lower() not in vistos:
            vistos.add(t.lower())
            hashtags.append("#" + t)
    partes += ["", " ".join(hashtags[:8])]
    return "\n".join(partes)


def escolher_e_escrever(cfg: dict, historico: list, pasta):
    log("1) A recolher notícias...")
    itens = recolher(cfg["noticias"]["fontes"], int(cfg["noticias"]["horas_max"]))
    if not itens:
        raise RuntimeError("Nenhuma fonte RSS respondeu.")
    grupos = agrupar(itens)
    grupos = remover_ja_cobertos(grupos, historico, int(cfg["noticias"]["dias_sem_repetir"]))
    log(f"   {len(itens)} notícias, {len(grupos)} assuntos novos")
    if not grupos:
        return None
    exc_cfg = cfg.get("excerto", {})
    usar_excerto = bool(exc_cfg.get("ativo", True))
    exigir = usar_excerto and (os.environ.get("EXIGIR_EXCERTO") == "true"
                               or bool(exc_cfg.get("so_noticias_com_excerto", False)))
    candidatos = grupos[:60] if exigir else grupos[:25]   # com trecho obrigatório, olha para mais notícias
    if exigir:
        log("   A ver que notícias têm vídeos de arquivo (entrevistas/discursos) sobre o tema...")
        try:
            com_arquivo = noticias_com_arquivo(candidatos, cfg, historico)
            prontos = []
            for g in com_arquivo[:8]:          # a IA confirma, pelos títulos, que há um vídeo provável
                it = g["itens"][0]
                try:
                    pre = _preselecionar(g["_cands"], {"manchete": it["titulo"], "roteiro": it.get("resumo", "")}, cfg)
                except Exception as e:
                    log(f"   Pré-seleção falhou ({type(e).__name__})")
                    continue
                if pre:
                    g["_presel"] = pre
                    prontos.append(g)
                    log(f"   Com vídeo provável: {it['titulo'][:90]} -> {pre[0]['titulo'][5:80]}")
                    if len(prontos) >= 2:
                        break
            candidatos = (prontos + [g for g in com_arquivo if g not in prontos]) or candidatos
        except Exception as e:
            log(f"   Verificação falhou ({type(e).__name__}); a seguir com todas as notícias")
    for tentativa in range(4 if exigir else 3):
        if not candidatos:
            break
        log("2) A escolher a notícia...")
        escolha = escolher(candidatos, historico, cfg, preferir_pessoas=exigir)
        grupo = candidatos.pop(escolha["id"])
        log(f"   Escolhida: {grupo['itens'][0]['titulo']}  [{escolha['categoria']}] ({', '.join(grupo['fontes'])})")
        log(f"   Motivo: {escolha['motivo']}")
        artigos = artigos_do_grupo(grupo)
        log("3) A escrever o roteiro...")
        roteiro = escrever(artigos, escolha["categoria"], cfg)
        if not roteiro.get("suficiente", True):
            log("   Informação insuficiente; a tentar outra notícia.")
            continue
        roteiro["categoria"] = escolha["categoria"]
        if grupo.get("_buscas_trecho"):   # pesquisas que já deram vídeos de arquivo
            roteiro["buscas_trecho"] = list(dict.fromkeys(grupo["_buscas_trecho"] + roteiro.get("buscas_trecho", [])))
            roteiro["pessoas"] = list(dict.fromkeys(grupo["_pessoas"] + roteiro.get("pessoas", [])))
        if grupo.get("_presel"):
            roteiro["_presel"] = grupo["_presel"]
        excerto = None
        if usar_excerto:
            log("3b) A procurar um trecho de entrevista ou discurso (vídeo + som originais)...")
            try:
                excerto = procurar_excerto(roteiro, cfg, pasta, historico)
            except Exception as e:
                log(f"  Procura de trecho falhou: {type(e).__name__}: {str(e)[:150]}")
            if not excerto:
                log("  Sem trecho adequado" + ("; a tentar outra notícia." if exigir else "; o vídeo segue sem trecho."))
        if excerto or not exigir:
            return grupo, artigos, roteiro, excerto
    return None


def roteiro_demo_trecho(titulo: str, cfg: dict, pasta, historico: list):
    """Vídeo de demonstração do formato com trecho original, a partir de um vídeo do Wikimedia Commons."""
    titulo = titulo if titulo.startswith("File:") else "File:" + titulo
    r = requests.get(COMMONS_API, params={"action": "query", "format": "json", "titles": titulo, "prop": "videoinfo",
                                          "viprop": "url|size|mime|extmetadata|derivatives"},
                     headers={"User-Agent": UA_WIKI}, timeout=25)
    r.raise_for_status()
    pag = next(iter(r.json()["query"]["pages"].values()))
    info = (pag.get("videoinfo") or [{}])[0]
    meta = info.get("extmetadata", {})
    lic = _meta(meta, "LicenseShortName").strip()
    if not info or not LICENCAS_LIVRES.match(lic) or re.search(r"\b(NC|ND)\b", lic):
        raise RuntimeError(f"'{titulo}' não existe ou não tem licença livre ({lic or 'sem licença'}).")
    descricao = limpar_html((meta.get("ImageDescription") or {}).get("value", ""))[:700]
    ano = re.search(r"(19|20)\d\d", _meta(meta, "DateTimeOriginal") or _meta(meta, "DateTime"))
    cand = {"titulo": pag.get("title", titulo), "info": info, "lic": lic, "index": 0,
            "autor": _meta(meta, "Artist") or "autor desconhecido", "ano": int(ano.group(0)) if ano else 0,
            "dur": float(info.get("duration") or 0), "data": _data_video(meta), "descricao": descricao, "afinidade": 9}
    assunto = re.sub(r"\s+", " ", descricao or pag.get("title", "")[5:]).strip()[:300]
    texto = ("Este é um vídeo de teste do novo formato do canal. Quando houver uma entrevista ou um discurso "
             "sobre o tema da notícia, o Short mostra um trecho original, com a imagem e o som de quem fala, "
             "a data e o crédito do vídeo. Depois do trecho, a narração continua com o resto da notícia. "
             "Os trechos vêm de vídeos com licença livre publicados no Wikimedia Commons. "
             + cfg["canal"]["chamada_final"])
    roteiro = {"suficiente": True, "titulo_youtube": "Teste: novo formato com trecho original de entrevista",
               "manchete": f"Demonstração com vídeo de arquivo: {assunto[:120]}", "titulo_topo": "NOVO FORMATO",
               "titulo_destaque": "TRECHO ORIGINAL", "etiqueta": "TESTE", "roteiro": texto,
               "descricao": "Vídeo de teste do formato com um trecho original de entrevista ou discurso.",
               "hashtags": ["Teste"], "tags": ["teste"], "buscas_commons": [], "buscas_imagens": [],
               "pessoas": [], "_presel": [cand], "categoria": "TESTE"}
    excerto = procurar_excerto({**roteiro, "roteiro": f"{texto} Assunto do vídeo de arquivo: {assunto}"},
                               cfg, pasta, historico)
    if not excerto:
        raise RuntimeError("Não foi possível tirar um trecho desse vídeo.")
    roteiro["buscas_commons"] = [excerto["orador"].split(",")[0].strip()]   # fotos de quem fala
    artigos = [{"fonte": "Teste do sistema", "titulo": "Demonstração", "link": cand["info"].get("descriptionurl", ""),
                "texto": ""}]
    return None, artigos, roteiro, excerto


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

    with tempfile.TemporaryDirectory() as tmp:
        pasta = Path(tmp)
        if args.teste_offline:
            grupo, artigos, roteiro = None, TESTE_OFFLINE["artigos"], TESTE_OFFLINE["roteiro"]
            excerto = _excerto_teste(pasta) if os.environ.get("TESTE_EXCERTO") else None
            args.sem_upload = True
        elif os.environ.get("DEMO_TRECHO"):
            log("Demonstração do formato com trecho original...")
            grupo, artigos, roteiro, excerto = roteiro_demo_trecho(os.environ["DEMO_TRECHO"], cfg, pasta, historico)
        else:
            r = escolher_e_escrever(cfg, historico, pasta)
            if r is None:
                log("Nada para publicar agora (sem notícias novas, ou sem trecho adequado). Até à próxima execução!")
                resumo_github("Nada publicado nesta execução: sem notícias novas, ou sem trecho de entrevista adequado.")
                return 0
            grupo, artigos, roteiro, excerto = r

        log("4) A gerar a narração...")
        t_ex = None
        if excerto:
            audio, palavras, dur, t_ex = narracao_com_excerto(roteiro["roteiro"], excerto, cfg, pasta)
        else:
            audio, palavras, dur = sintetizar(roteiro["roteiro"], cfg, pasta)

        log("5) A preparar imagens e grafismo...")
        if args.teste_offline:
            fotos = fundos_gerados(int(cfg["imagens"]["quantidade"]), pasta)
        else:
            fotos = obter_imagens(roteiro, cfg, pasta, historico, excluir={excerto["id"]} if excerto else None)
        nomes_fontes = list(dict.fromkeys(a["fonte"] for a in artigos))
        formato = cfg["imagens"].get("formato", "faixas")
        topo, destaque = roteiro.get("titulo_topo", ""), roteiro.get("titulo_destaque", "")
        if not topo or not destaque:
            topo, destaque = dividir_manchete(roteiro["manchete"])
        grafico = pasta / "grafico.png"
        camada_grafica(grafico, topo, destaque, roteiro["etiqueta"], cfg["canal"]["nome"], nomes_fontes, formato)
        ass = pasta / "legendas.ass"
        gerar_ass(palavras, ass, dur + 0.9, y=1150 if formato == "ecra_inteiro" else 1440)

        log("6) A montar o vídeo...")
        carimbo = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        video = saida_dir / f"short_{carimbo}.mp4"
        itens, durs = fotos, None
        if excerto:
            itens, durs = inserir_excerto(fotos, excerto, t_ex, dur + 0.9)
        montar(itens, audio, ass, grafico, dur, pasta, video, formato, durs)
        log(f"   Vídeo: {video} ({video.stat().st_size / 1e6:.1f} MB)")

    creditos = fotos + ([excerto] if excerto else [])
    descricao = descricao_completa(roteiro, artigos, creditos)
    meta = {"titulo": roteiro["titulo_youtube"], "descricao": descricao, "tags": roteiro["tags"],
            "roteiro": roteiro["roteiro"], "video": video.name}
    (saida_dir / "ultimo.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    resumo = [f"## {roteiro['titulo_youtube']}", "", roteiro["roteiro"], "", "**Fontes:** " +
              ", ".join(f"[{a['fonte']}]({a['link']})" for a in artigos)]
    resumo.append("\n**Imagens:** " + ("; ".join(f"[{f['id']}]({f.get('url', '')}) — {f['autor']} ({f.get('licenca', '')})"
                                                  for f in fotos if f.get("autor")) or "fundos gerados"))
    if excerto:
        resumo.append(f"\n**Trecho original:** {excerto['orador']} ({excerto.get('data') or 's/ data'}) — "
                      f"\"{excerto['texto']}\" — [{excerto.get('id')}]({excerto.get('url', '')}) ({excerto.get('licenca', '')})")

    if args.sem_upload:
        log("Modo sem upload: vídeo criado mas não enviado.")
        resumo.append("\n_Modo de teste: não foi publicado._")
        resumo_github("\n".join(resumo))
        return 0

    log("7) A enviar para o YouTube...")
    res = enviar(video, roteiro["titulo_youtube"], descricao, roteiro["tags"], cfg)
    resumo.append(f"\n**YouTube:** https://youtube.com/shorts/{res['id']} ({res['privacidade']})")
    resumo_github("\n".join(resumo))

    if os.environ.get("DEMO_TRECHO"):
        return 0                      # a demonstração não entra no histórico
    historico.append({
        "data": datetime.now(timezone.utc).isoformat(),
        "manchete": roteiro["manchete"], "titulo": roteiro["titulo_youtube"],
        "categoria": roteiro.get("categoria", ""),
        "links": [i["link"] for i in grupo["itens"]] if grupo else [],
        "tokens": sorted(grupo["tokens"] if grupo else tokens(roteiro["manchete"])),
        "video_id": res["id"], "fotos": [f["id"] for f in creditos if f.get("id")],
    })
    guardar_historico(historico)
    return 0


if __name__ == "__main__":
    sys.exit(main())
