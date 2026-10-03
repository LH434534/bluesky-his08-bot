#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tick.py — execução única do bot (usado pelo GitHub Actions).
Login -> posta se bater com o horário agendado -> 1 rodada de engajamento -> sai.
Limites por tick: 3 likes / 1 reply (cron 15min => 12 likes e 4 replies por hora)."""
import json
import os
import random
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import bluesky_agent as ba

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

now = time.localtime()
cur_min = now.tm_hour * 60 + now.tm_min

force_post = os.environ.get("BSKY_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "sim")

# 1) post agendado (tolerância 5 min) ou post forçado
if force_post:
    texto = os.environ.get("BSKY_FORCE_TEXT", "🔥 Primeiro post do bot no ar! Sigam @his08.bsky.social e fiquem ligados.")
    try:
        client.create_post(texto)
        print(f"  [post-forcado] {texto}")
    except RuntimeError as e:
        print("  [erro post]", e)

for s in cfg["schedule"]:
    if s.get("action") != "post":
        continue
    target = s["hour"] * 60 + s["minute"]
    if abs(cur_min - target) <= 5:
        try:
            client.create_post(s["content"])
            print(f"  [post] {s['content']}")
        except RuntimeError as e:
            print("  [erro post]", e)

# 2) engajamento: 1 rodada, máx 3 likes / 1 reply por tick
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
                try:
                    client.reply(
                        random.choice(cfg["engagement"]["reply_templates"]),
                        uri, cid, uri, cid
                    )
                    reply_done += 1
                    print(f"  [reply] {text[:50]}")
                except RuntimeError as e:
                    print("  [erro reply]", e)
            if likes_done >= 3 and reply_done >= 1:
                break
    except RuntimeError as e:
        print("  [erro timeline]", e)

print("[i] tick concluído.")
