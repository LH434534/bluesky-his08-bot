#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bot_utils.py — helpers puros compartilhados (sem rede, testáveis).

Centraliza funções que o tick.py e o ai_engine.py usam para deduplicação,
corte de texto e detecção de spam, para que possam ser testadas isoladamente.
"""
import re
import unicodedata


def norm_key(text, n=40):
    """Chave de dedupe: minúsculas, sem acentos, sem espaços/pontuação."""
    t = (text or "").lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[^\w]", "", t)
    return t[:n]


def similar_to_history(text, history, n=40):
    """True se o texto normalizado é prefixo/contido em qualquer item do histórico
    (detecta repetição com pequenas variações sem exigir igualdade total)."""
    key = norm_key(text, n)
    if len(key) < 8:
        return False
    for h in (history or ()):
        hkey = norm_key(h, n)
        if key in hkey or hkey in key:
            return True
    return False


def fit_length(text, limit=295):
    """Corta no limite sem quebrar palavra; adiciona '...' quando trunca."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + "..." if cut.rstrip() else text[:limit]


def looks_spammy(text):
    """Versão pura do filtro anti-spam usado para engajamento (sem dict)."""
    t = (text or "").strip()
    if len(t) < 5:
        return True
    if len(t) > 500:
        return True
    if t.count("http") > 2:
        return True
    if t.isupper() and len(t) > 40:
        return True
    if re.search(
        r"\b(dm|direct message|free\s*follow|follow\s*back|telegram|whatsapp|onlyfans|promo|sorteio|ganhe)\b",
        t, re.I,
    ):
        return True
    return False
