"""Quantos segundos faltam para o próximo vídeo automático (secção "agenda" do config.yaml).

Usa só a biblioteca padrão do Python, para correr sem instalar nada.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

BRASILIA = timezone(timedelta(hours=-3))


def _ler(texto: str, chave: str, padrao: str) -> str:
    m = re.search(rf'^\s*{chave}:\s*"?([^"#\n]+?)"?\s*(#.*)?$', texto, re.M)
    return m.group(1).strip() if m else padrao


def segundos_ate_proximo(agora: datetime | None = None) -> int:
    texto = (Path(__file__).resolve().parent / "config.yaml").read_text(encoding="utf-8")
    intervalo = max(10, int(_ler(texto, "intervalo_minutos", "30")))
    ih, im = map(int, _ler(texto, "inicio_brasilia", "05:07").split(":"))
    fh, fm = map(int, _ler(texto, "fim_brasilia", "23:07").split(":"))
    agora = agora or datetime.now(BRASILIA)
    for dia in (-1, 0, 1, 2):
        ini = (agora + timedelta(days=dia)).replace(hour=ih, minute=im, second=0, microsecond=0)
        fim = ini.replace(hour=fh, minute=fm)
        if fim < ini:                      # janela que passa a meia-noite
            fim += timedelta(days=1)
        t = ini
        while t <= fim:
            if t > agora + timedelta(seconds=20):
                return int((t - agora).total_seconds())
            t += timedelta(minutes=intervalo)
    return intervalo * 60


if __name__ == "__main__":
    print(segundos_ate_proximo())
