#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tick.py — execução única do bot (usado pelo GitHub Actions).
Login -> posta se bater com o horário agendado (texto: IA ou fallback) ->
1 rodada de engajamento (replies: IA ou fallback) -> sai.
Limites por tick: 3 likes / 1 reply (cron 15min => 12 likes e 4 replies por hora).

Env de IA (opcionais — sem nenhuma, o bot usa textos fixos):
  OPENAI_API_KEY | ANTHROPIC_API_KEY | GEMINI_API_KEY | AI_API_KEY(+AI_API_URL+AI_MODEL)
  IA_ENABLED=1/true  -> força uso da IA (se indisponível, loga aviso e segue fallback)
"""
import json
import os
import random
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import bluesky_agent as ba
import ai_engine as aie

cfg = ba.load_config()

if not ba.has_creds(cfg):
    print("[!] Sem credenciais (env BSKY_HANDLE / BSKY_APP_PASSWORD).")
    sys.exit(1)

client = ba.BskyClient(cfg)
try:
    client.login()
    print("[ok] login realizado. did =", client.did)
except RuntimeError as e:
    print("[falha] login:", e)
    sys.exit(1)

ai = aie.AIEngine()
ia_ok = ai.available()
ia_forced = os.environ.get("IA_ENABLED", "").strip().lower() in ("1", "true", "yes", "sim")
test_only = os.environ.get("BSKY_AI_TEST_ONLY", "").strip().lower() in ("1", "true", "yes", "sim")
if ia_ok:
    print(f"[ia] provider={ai.provider} — conteúdo gerado por IA" + (" [TESTE, sem publicar]" if test_only else ""))
elif ia_forced:
    print("[ia] IA_ENABLED=1 mas nenhum provider de IA disponível; usando fallback fixo")

# 0) teste de IA: gera conteúdo, loga e sai SEM publicar
if test_only:
    if not ia_ok:
        print("[teste] nenhum provider de IA disponível")
        sys.exit(0)
    try:
        print("[teste] post :", ai.generate_post())
        print("[teste] reply:", ai.generate_reply("Que dia lindo hoje, a música tá no ar!"))
    except Exception as e:
        print("[teste] erro:", e)
    print("[i] tick concluído (modo teste).")
    sys.exit(0)

now = time.localtime()
cur_min = now.tm_hour * 60 + now.tm_min

force_post = os.environ.get("BSKY_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "sim")

# 1) post forçado (texto do input OU gerado por IA se marcado)
if force_post:
    texto = os.environ.get("BSKY_FORCE_TEXT", "").strip()
    if not texto and ia_ok:
        try:
            texto = ai.generate_post()
            print("  [ia] post gerado")
        except Exception as e:
            print("  [erro ia]", e)
    if not texto:
        texto = "🔥 Primeiro post do bot no ar! Sigam @his08.bsky.social e fiquem ligados."
    try:
        client.create_post(texto)
        print(f"  [post-forcado] {texto}")
    except RuntimeError as e:
        print("  [erro post]", e)

# 2) post agendado (tolerância 5 min)
for s in cfg["schedule"]:
    if s.get("action") != "post":
        continue
    target = s["hour"] * 60 + s["minute"]
    if abs(cur_min - target) > 5:
        continue
    texto = s["content"]
    if ia_ok:
        try:
            texto = ai.generate_post()
            print("  [ia] post agendado gerado")
        except Exception as e:
            print("  [erro ia]", e, "— usando texto fixo")
    try:
        client.create_post(texto)
        print(f"  [post] {texto}")
    except RuntimeError as e:
        print("  [erro post]", e)

# 3) engajamento: 1 rodada, máx 3 likes / 1 reply por tick
if cfg["engagement"].get("enabled", False):
    likes_done = 0
    reply_done = 0
    try:
        tl = client.timeline(limit=30)
        for item in tl.get("feed", []):
            post = item.get("post", {})
            text = post.get("record", {}).get("text", "")
            uri = post.get("uri", "")
            cid = post.get("cid", "")
            if not ba.should_engage(text, cfg["engagement"]["follow_keywords"]):
                continue
            if likes_done < 3:
                try:
                    client.like(uri, cid)
                    likes_done += 1
                    print(f"  [like] {text[:50]}")
                    time.sleep(random.uniform(5, 10))
                except RuntimeError as e:
                    print("  [erro like]", e)
            if reply_done < 1:
                reply_text = random.choice(cfg["engagement"]["reply_templates"])
                if ia_ok:
                    try:
                        reply_text = ai.generate_reply(text)
                        print("  [ia] reply gerado")
                    except Exception as e:
                        print("  [erro ia]", e, "— usando template fixo")
                try:
                    client.reply(reply_text, uri, cid, uri, cid)
                    reply_done += 1
                    print(f"  [reply] {reply_text}")
                except RuntimeError as e:
                    print("  [erro reply]", e)
            if likes_done >= 3 and reply_done >= 1:
                break
    except RuntimeError as e:
        print("  [erro timeline]", e)

print("[i] tick concluído.")
