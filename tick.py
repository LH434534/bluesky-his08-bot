#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tick.py — execução única do bot 100000% AUTÔNOMO.
Não pede permissão para nada. A cada 15 min (cron) ele:
  - lê a timeline e feeds em alta (trending)
  - curte posts, comenta nos posts dos outros (resposta IA contextual)
  - segue perfis interessantes de vez em quando
  - posta: agendado + extra aleatório + post sobre o que está em alta
  - edita a bio sozinho (muda ~55% dos dias)
  - troca a foto de perfil sozinho (~12% dos dias)
Anti-spam embutido: limites por tick (likes/replies), cooldown com jitter.
Env de IA (opcionais): G4F=1 (grátis) | OPENAI/ANTHROPIC/GEMINI keys | AI_API_KEY...
"""
import hashlib
import json
import os
import random
import sys
import time
import urllib.request

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
test_only = os.environ.get("BSKY_AI_TEST_ONLY", "").strip().lower() in ("1", "true", "yes", "sim")
if ia_ok:
    print(f"[ia] provider={ai.provider} — conteúdo gerado por IA" + (" [TESTE, sem publicar]" if test_only else ""))
else:
    print("[ia] sem provider de IA — usando fallback fixo")

auto = cfg.get("autonomy", {})
engage = cfg.get("engagement", {})


# ------------------------------------------------------------ helpers
def day_seed(tag):
    """Seed determinística por dia: mesma decisão em todos os ticks do dia."""
    h = hashlib.sha256(f"{time.strftime('%Y-%m-%d')}:{tag}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def chance(tag, pct):
    return (day_seed(tag) % 100) < pct


def post_info(item):
    if not item:
        return {"text": "", "uri": "", "cid": "", "handle": "?", "did": ""}
    # feed items vêm como {"post": {...}}; search items vêm como post direto
    post = item.get("post") or item
    author = post.get("author", {}) or {}
    return {
        "text": (post.get("record", {}) or {}).get("text", "") or "",
        "uri": post.get("uri", "") or "",
        "cid": post.get("cid", "") or "",
        "handle": author.get("handle", "?"),
        "did": author.get("did", ""),
    }


def sleep_human(lo, hi):
    time.sleep(random.uniform(lo, hi))


# ------------------------------------------------------------ 0) modo teste
if test_only:
    if not ia_ok:
        print("[teste] nenhum provider de IA disponível")
        sys.exit(0)
    try:
        print("[teste] post :", ai.generate_post())
        print("[teste] reply:", ai.generate_reply("Que dia lindo hoje, a música tá no ar!"))
        print("[teste] bio :", ai.generate_bio())
        print("[teste] topic:", ai.generate_topic_post("música brasileira em alta"))
    except Exception as e:
        print("[teste] erro:", e)
    print("[i] tick concluído (modo teste).")
    sys.exit(0)


# ------------------------------------------------------------ 1) contexto: o que tá acontecendo
timeline_posts = []
try:
    tl = client.timeline(limit=30)
    timeline_posts = [it for it in tl.get("feed", []) if it.get("post")]
    print(f"[contexto] timeline lida: {len(timeline_posts)} posts")
except RuntimeError as e:
    print("  [erro timeline]", e)

trending_posts = []
trending_topics = []
trending_feed = auto.get("trending_feed", "at://did:plc:z72i7hdynmk6r22z27h6tv6ah/app.bsky.feed.generator/whats-hot")
if auto.get("trending_enabled", True):
    # 1) nativo: posts populares (pode estar descontinuado -> 501, seguimos)
    try:
        hot = client.get_popular(limit=20)
        trending_posts = [it for it in hot.get("feed", []) if it.get("post")]
        if trending_posts:
            print(f"[contexto] posts em alta lidos: {len(trending_posts)}")
    except RuntimeError as e:
        print("  [erro trending]", e)
    # 2) nativo: tópicos em alta + posts do tópico principal
    if not trending_posts:
        try:
            tt = client.get_trending_topics(limit=10)
            trending_topics = [t.get("topic", "") for t in tt.get("trending", []) if t.get("topic")]
            if trending_topics:
                print(f"[contexto] tópicos em alta: {', '.join(trending_topics[:3])}")
                try:
                    hot2 = client.search_posts(trending_topics[0], limit=15)
                    trending_posts = [it for it in hot2.get("posts", []) if it.get("uri")]
                    print(f"[contexto] posts do tópico em alta: {len(trending_posts)}")
                except RuntimeError as e:
                    print("  [erro trending]", e)
        except RuntimeError as e:
            print("  [erro trending]", e)
    # 3) fallback: busca por palavras-chave culturais
    if not trending_posts:
        for kw in ["música", "arte", "futebol", "cinema", "Brasil", "tecnologia"]:
            try:
                r = client.search_posts(kw, limit=10)
                posts = [it for it in r.get("posts", []) if it.get("uri")]
                if posts:
                    trending_posts = posts
                    print(f"[contexto] busca '{kw}': {len(posts)} posts")
                    break
            except RuntimeError as e:
                print("  [erro trending]", e)

force_post = os.environ.get("BSKY_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "sim")
cur_min = time.localtime().tm_hour * 60 + time.localtime().tm_min
max_likes = auto.get("max_likes_per_tick", 4)
max_replies = auto.get("max_replies_per_tick", 2)
likes_done = 0
replies_done = 0


def do_like(info):
    global likes_done
    if likes_done >= max_likes:
        return
    try:
        client.like(info["uri"], info["cid"])
        likes_done += 1
        print(f"  [like] {info['handle']}: {info['text'][:50]}")
        sleep_human(4, 9)
    except RuntimeError as e:
        print("  [erro like]", e)


def do_reply(info, text=None):
    global replies_done
    if replies_done >= max_replies:
        return
    if not text:
        if ia_ok:
            try:
                text = ai.generate_reply(info["text"])
                print("  [ia] reply gerado")
            except Exception as e:
                print("  [erro ia]", e, "— usando template fixo")
        if not text:
            text = random.choice(engage.get("reply_templates", ["Boa! 🔥"]))
    try:
        client.reply(text, info["uri"], info["cid"], info["uri"], info["cid"])
        replies_done += 1
        print(f"  [reply] {info['handle']}: {text}")
        sleep_human(6, 12)
    except RuntimeError as e:
        print("  [erro reply]", e)


# ------------------------------------------------------------ 2) engajamento na timeline
for item in timeline_posts:
    if likes_done >= max_likes and replies_done >= max_replies:
        break
    info = post_info(item)
    if not info["text"]:
        continue
    if ba.should_engage(info["text"], engage.get("follow_keywords", [])):
        if likes_done < max_likes and random.random() < 0.8:
            do_like(info)
        if replies_done < max_replies and random.random() < 0.45:
            do_reply(info)

# ------------------------------------------------------------ 3) engajamento no trending
for item in trending_posts:
    if likes_done >= max_likes and replies_done >= max_replies:
        break
    info = post_info(item)
    if not info["text"]:
        continue
    if likes_done < max_likes and random.random() < 0.7:
        do_like(info)
    if replies_done < max_replies and random.random() < 0.4:
        do_reply(info)

# follow ocasional (perfis que estão em alta ou na timeline)
follow_prob = auto.get("follow_prob", 0.0)
candidate = next((post_info(it) for it in (trending_posts + timeline_posts) if post_info(it).get("did")), None)
if follow_prob > 0 and candidate and candidate["did"] and random.random() < follow_prob:
    try:
        client.follow(candidate["did"])
        print(f"  [follow] @{candidate['handle']}")
        sleep_human(3, 6)
    except RuntimeError as e:
        print("  [erro follow]", e)

# ------------------------------------------------------------ 4) postar
def safe_post(texto, tag):
    if not texto:
        return
    try:
        client.create_post(texto)
        print(f"  [post-{tag}] {texto}")
    except RuntimeError as e:
        print(f"  [erro post-{tag}]", e)

# 4a) post forçado (workflow_dispatch)
if force_post:
    texto = os.environ.get("BSKY_FORCE_TEXT", "").strip()
    if not texto and ia_ok:
        try:
            texto = ai.generate_post()
        except Exception as e:
            print("  [erro ia]", e)
    if not texto:
        texto = "🔥 Primeiro post do bot no ar! Sigam @his08.bsky.social e fiquem ligados."
    safe_post(texto, "forcado")

# 4b) post agendado (tolerância 5 min)
for s in cfg.get("schedule", []):
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
    safe_post(texto, "agendado")

# 4c) post extra aleatório
extra_prob = auto.get("extra_post_prob", 0.06)
if ia_ok and random.random() < extra_prob:
    try:
        safe_post(ai.generate_post(), "extra")
    except Exception as e:
        print("  [erro post-extra]", e)

# 4d) post sobre o que está em alta
trend_prob = auto.get("trending_post_prob", 0.35)
if ia_ok and (trending_posts or trending_topics) and random.random() < trend_prob:
    tema = None
    if trending_topics:
        tema = random.choice(trending_topics)
    elif trending_posts:
        tema = post_info(random.choice(trending_posts))["text"]
    if tema:
        try:
            safe_post(ai.generate_topic_post(tema), "trending")
        except Exception as e:
            print("  [erro post-trending]", e)

# ------------------------------------------------------------ 5) perfil: bio + avatar
def safe_update_profile(**kw):
    """Mescla com o profile atual para não apagar displayName/bio existentes.

    avatar só é enviado quando um blob novo é passado (avatar_blob). O avatar
    atual NÃO é reenviado (o servidor mantém por merge quando omitido).
    """
    try:
        cur = client.get_profile() or {}
    except RuntimeError as e:
        print("  [erro get_profile]", e)
        cur = {}
    display_name = kw.get("display_name", cur.get("displayName"))
    description = kw.get("description", cur.get("description"))
    avatar = kw.get("avatar_blob")  # None = não mexer no avatar
    try:
        client.update_profile(display_name=display_name, description=description, avatar_blob=avatar)
        return True
    except RuntimeError as e:
        print("  [erro update_profile]", e)
        return False


force_bio = os.environ.get("BSKY_FORCE_BIO", "").strip().lower() in ("1", "true", "yes", "sim")
bio_prob = auto.get("bio_daily_prob", 0.0)
if ia_ok and (force_bio or (bio_prob > 0 and chance("bio", bio_prob))):
    try:
        bio = ai.generate_bio()
        if bio:
            if safe_update_profile(description=bio):
                print(f"  [bio] atualizada: {bio}")
    except Exception as e:
        print("  [erro bio]", e)

force_avatar = os.environ.get("BSKY_FORCE_AVATAR", "").strip().lower() in ("1", "true", "yes", "sim")
avatar_prob = auto.get("avatar_daily_prob", 0.0)
if force_avatar or (avatar_prob > 0 and chance("avatar", avatar_prob)):
    seed = day_seed("avatar")
    urls = [
        f"https://robohash.org/his08-{seed}.png?size=400x400",
        f"https://ui-avatars.com/api/?name=his08&size=400&background=1e3a5f&color=fff",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                img = r.read()
            mime = r.headers.get("Content-Type", "image/png").split(";")[0].strip() or "image/png"
            if not img:
                continue
            blob = client.upload_blob(img, mime)
            b = blob.get("blob")
            if b and safe_update_profile(avatar_blob=b):
                print(f"  [avatar] foto de perfil trocada ({mime}, {len(img)} bytes)")
                break
        except Exception as e:
            print("  [erro avatar]", e)

# verificação do estado final do perfil
try:
    p = client.get_profile() or {}
    bio_now = (p.get("description") or "").replace("\n", " ")[:120]
    print(f"[perfil] displayName={p.get('displayName')!r} bio={bio_now!r} avatar={'OK' if p.get('avatar') else 'vazio'}")
except RuntimeError as e:
    print("  [erro get_profile-final]", e)

print("[i] tick concluído.")
