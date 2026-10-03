#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tick.py — execução única do bot 100000% AUTÔNOMO + ANTI-BAN EXTREMO.

A cada 15 min (cron) ele age como o dono real do perfil:
  - lê timeline e feeds em alta (trending)
  - curte/comenta posts de outros (IA contextual), seguindo perfis humanos
  - posta: agendado + extra + sobre o que está em alta
  - edita bio e troca avatar sozinho

ANTI-BAN / HUMANIZAÇÃO (o objetivo é NUNCA parecer robô):
  - estado persistente em state.json (repo): contadores diários/horários,
    dedupe global de URIs, autores já interagidos, histórico de textos
  - janela noturna: 23:30–07:30 sem engajamento/posts extras/trending
  - probabilidade de "presença" por tick (dia ~90%, noite ~15%)
  - pausas humanas longas e aleatórias entre ações e blocos
  - limites por dia/hora/tick conservadores
  - nunca curtir/responder o próprio post, posts repetidos, autores repetidos,
    posts antigos (>7 dias), texto suspeito de spam
  - anti-repetição de replies/posts (regenera até fugir do histórico)
  - backoff: ao ver erro de rate limit (429/5xx), para o tick na hora

Env de IA (opcionais): G4F=1 (grátis) | OPENAI/ANTHROPIC/GEMINI keys | AI_API_KEY...
"""
import base64
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

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
asp = cfg.get("anti_spam", {})

# ------------------------------------------------------------ estado persistente
REPO = os.environ.get("GITHUB_REPOSITORY", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
STATE_PATH = os.path.join(BASE, "state.json")


def gh_api(method, path, payload=None):
    url = "https://api.github.com" + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + GH_TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        return e.code, (json.loads(body) if body else {})


def load_state():
    st = {"date": "", "likes": 0, "replies": 0, "posts": 0, "follows": 0,
          "likes_hour": 0, "hour_key": "", "authors_today": [],
          "seen_uris": [], "reply_history": [], "post_history": [],
          "bio_date": "", "avatar_date": ""}
    if not (REPO and GH_TOKEN):
        return st
    code, resp = gh_api("GET", f"/repos/{REPO}/contents/state.json")
    if code != 200:
        return st
    try:
        raw = base64.b64decode((resp.get("content") or "").replace("\n", ""))
        loaded = json.loads(raw)
        for k in st:
            if k in loaded:
                st[k] = loaded[k]
    except Exception as e:
        print("[state] falha ao decodificar:", e)
    return st


def save_state():
    if not (REPO and GH_TOKEN):
        return
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        code, existing = gh_api("GET", f"/repos/{REPO}/contents/state.json")
        payload = {"message": "state update", "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False).encode()).decode()}
        if code == 200:
            payload["sha"] = existing.get("sha")
        gh_api("PUT", f"/repos/{REPO}/contents/state.json", payload)
        print("[state] salvo e sincronizado")
    except Exception as e:
        print("[state] erro ao sincronizar:", e)


state = load_state()


def today():
    return time.strftime("%Y-%m-%d")


def reset_if_new_day():
    if state.get("date") != today():
        state["date"] = today()
        state["likes"] = 0
        state["replies"] = 0
        state["posts"] = 0
        state["follows"] = 0
        state["authors_today"] = []
        state["likes_hour"] = 0
        state["hour_key"] = ""


def hour_key():
    return time.strftime("%Y-%m-%d %H")


def bump(key):
    reset_if_new_day()
    if key in ("likes", "likes_hour"):
        if state.get("hour_key") != hour_key():
            state["hour_key"] = hour_key()
            state["likes_hour"] = 0
    state[key] = state.get(key, 0) + 1


def budget_ok(key, limit):
    reset_if_new_day()
    return state.get(key, 0) < limit


# ------------------------------------------------------------ helpers anti-ban
def day_seed(tag):
    h = hashlib.sha256(f"{time.strftime('%Y-%m-%d')}:{tag}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def chance(tag, pct):
    return (day_seed(tag) % 100) < pct


def sleep_human(lo, hi):
    time.sleep(random.uniform(lo, hi))


def post_info(item):
    if not item:
        return {"text": "", "uri": "", "cid": "", "handle": "?", "did": "",
                "created_at": "", "like_count": 0}
    post = item.get("post") or item
    author = post.get("author", {}) or {}
    rec = post.get("record", {}) or {}
    return {
        "text": rec.get("text", "") or "",
        "uri": post.get("uri", "") or "",
        "cid": post.get("cid", "") or "",
        "handle": author.get("handle", "?"),
        "did": author.get("did", ""),
        "created_at": rec.get("createdAt", "") or "",
        "like_count": post.get("likeCount", 0) or 0,
    }


def iso_age_days(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:
        return 0.0


SPAM_RE = re.compile(r"\b(dm|direct message|free\s*follow|follow\s*back|telegram|whatsapp|onlyfans|promo|sorteio|ganhe)\b", re.I)


def is_spamish(info):
    t = (info["text"] or "").strip()
    if len(t) < 5:
        return True
    if len(t) > 500:
        return True
    if t.count("http") > 2:
        return True
    if t.isupper() and len(t) > 40:
        return True
    if SPAM_RE.search(t):
        return True
    return False


def can_engage(info):
    if not info["uri"] or not info["did"]:
        return False
    if info["did"] == client.did:
        return False
    if info["uri"] in state["seen_uris"]:
        return False
    if info["did"] in state["authors_today"]:
        return False
    if is_spamish(info):
        return False
    max_age = asp.get("max_reply_age_days", 7)
    if info["created_at"] and iso_age_days(info["created_at"]) > max_age:
        return False
    return True


def mark_engaged(info, action):
    reset_if_new_day()
    state["seen_uris"].append(info["uri"])
    if info["did"] not in state["authors_today"]:
        state["authors_today"].append(info["did"])
    max_seen = asp.get("max_state_seen", 3000)
    if len(state["seen_uris"]) > max_seen:
        state["seen_uris"] = state["seen_uris"][-max_seen:]
    max_auth = asp.get("max_authors_per_day", 12)
    if len(state["authors_today"]) > max_auth:
        state["authors_today"] = state["authors_today"][-max_auth:]
    bump("likes" if action == "like" else "replies")


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


# ------------------------------------------------------------ 1) contexto
timeline_posts = []
try:
    tl = client.timeline(limit=30)
    timeline_posts = [it for it in tl.get("feed", []) if it.get("post")]
    print(f"[contexto] timeline lida: {len(timeline_posts)} posts")
except RuntimeError as e:
    print("  [erro timeline]", e)

trending_posts = []
trending_topics = []
if auto.get("trending_enabled", True):
    try:
        hot = client.get_popular(limit=20)
        trending_posts = [it for it in hot.get("feed", []) if it.get("post")]
        if trending_posts:
            print(f"[contexto] posts em alta lidos: {len(trending_posts)}")
    except RuntimeError as e:
        print("  [erro trending]", e)
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
force_bio = os.environ.get("BSKY_FORCE_BIO", "").strip().lower() in ("1", "true", "yes", "sim")
force_avatar = os.environ.get("BSKY_FORCE_AVATAR", "").strip().lower() in ("1", "true", "yes", "sim")

# janela noturna e presença
now_min = time.localtime().tm_hour * 60 + time.localtime().tm_min
night_start = asp.get("night_start_min", 1410)   # 23:30
night_end = asp.get("night_end_min", 450)        # 07:30
is_night = now_min >= night_start or now_min < night_end
act_prob = asp.get("act_prob_night", 0.15) if is_night else asp.get("act_prob_day", 0.9)
human_awake = force_post or force_bio or force_avatar or random.random() < act_prob
if not human_awake:
    print("[humano] este tick o dono não mexeu no perfil (ausente). nada feito.")
    print("[i] tick concluído.")
    sys.exit(0)
if is_night:
    print("[humano] horário noturno — modo repouso (sem interação social)")
else:
    print("[humano] perfil ativo neste tick")

max_likes_tick = auto.get("max_likes_per_tick", 4)
max_replies_tick = auto.get("max_replies_per_tick", 2)
max_likes_day = asp.get("max_likes_per_day", 60)
max_replies_day = asp.get("max_replies_per_day", 20)
max_likes_hour = asp.get("max_likes_per_hour", 10)
max_replies_hour = asp.get("max_replies_per_hour", 4)
likes_done = 0
replies_done = 0
rate_limited = False


def rate_stop(exc):
    global rate_limited
    msg = str(exc)
    if "429" in msg or "500" in msg or "502" in msg or "503" in msg or "RateLimit" in msg:
        rate_limited = True
        print("  [anti-ban] rate limit detectado — parando ações deste tick")
        return True
    return False


def do_like(info):
    global likes_done
    if rate_limited or likes_done >= max_likes_tick:
        return
    if not budget_ok("likes", max_likes_day) or state.get("likes_hour", 0) >= max_likes_hour:
        return
    if not can_engage(info):
        return
    if random.random() < asp.get("skip_like_prob", 0.25):
        print("  [humano] passou reto (nem tudo é like)")
        return
    try:
        client.like(info["uri"], info["cid"])
        mark_engaged(info, "like")
        likes_done += 1
        print(f"  [like] {info['handle']}: {info['text'][:45]}")
        sleep_human(*asp.get("pause_like", [8, 20]))
    except RuntimeError as e:
        print("  [erro like]", e)
        rate_stop(e)


def do_reply(info, text=None):
    global replies_done
    if rate_limited or replies_done >= max_replies_tick:
        return
    if not budget_ok("replies", max_replies_day):
        return
    if not can_engage(info):
        return
    if not text:
        for _ in range(4):
            try:
                text = ai.generate_reply(info["text"])
            except Exception as e:
                print("  [erro ia]", e)
                text = ""
            if text and text not in state["reply_history"]:
                break
            text = ""
        if not text:
            tpl = [t for t in engage.get("reply_templates", ["Boa!"]) if t not in state["reply_history"]]
            text = random.choice(tpl) if tpl else random.choice(engage.get("reply_templates", ["Boa!"]))
    try:
        client.reply(text, info["uri"], info["cid"], info["uri"], info["cid"])
        mark_engaged(info, "reply")
        state["reply_history"].append(text)
        state["reply_history"] = state["reply_history"][-40:]
        replies_done += 1
        print(f"  [reply] {info['handle']}: {text}")
        sleep_human(*asp.get("pause_reply", [10, 25]))
    except RuntimeError as e:
        print("  [erro reply]", e)
        rate_stop(e)


# ------------------------------------------------------------ 2) engajamento (só de dia)
if not is_night and not rate_limited:
    for item in timeline_posts:
        if likes_done >= max_likes_tick and replies_done >= max_replies_tick:
            break
        info = post_info(item)
        if not info["text"]:
            continue
        if ba.should_engage(info["text"], engage.get("follow_keywords", [])):
            if likes_done < max_likes_tick and random.random() < 0.8:
                do_like(info)
            if replies_done < max_replies_tick and random.random() < 0.45:
                do_reply(info)
    sleep_human(*asp.get("pause_block", [30, 90]))

if not is_night and not rate_limited:
    for item in trending_posts:
        if likes_done >= max_likes_tick and replies_done >= max_replies_tick:
            break
        info = post_info(item)
        if not info["text"]:
            continue
        if likes_done < max_likes_tick and random.random() < 0.7:
            do_like(info)
        if replies_done < max_replies_tick and random.random() < 0.4:
            do_reply(info)

# follow ocasional (só de dia, raro)
follow_prob = auto.get("follow_prob", 0.0)
if not is_night and not rate_limited and follow_prob > 0 and random.random() < follow_prob:
    if budget_ok("follows", asp.get("max_follows_per_day", 2)):
        candidate = next((post_info(it) for it in (trending_posts + timeline_posts)
                          if post_info(it).get("did") and post_info(it).get("did") != client.did
                          and "bot" not in post_info(it).get("handle", "").lower()), None)
        if candidate:
            try:
                client.follow(candidate["did"])
                bump("follows")
                print(f"  [follow] @{candidate['handle']}")
                sleep_human(5, 12)
            except RuntimeError as e:
                print("  [erro follow]", e)
                rate_stop(e)


# ------------------------------------------------------------ 4) postar
def safe_post(texto, tag):
    if not texto:
        return
    if texto in state["post_history"]:
        print(f"  [humano] post repetido, descartado ({tag})")
        return
    if not budget_ok("posts", asp.get("max_posts_per_day", 10)):
        return
    try:
        client.create_post(texto)
        bump("posts")
        state["post_history"].append(texto)
        state["post_history"] = state["post_history"][-30:]
        print(f"  [post-{tag}] {texto}")
        sleep_human(*asp.get("pause_block", [30, 90]))
    except RuntimeError as e:
        print(f"  [erro post-{tag}]", e)
        rate_stop(e)


if not rate_limited:
    if force_post:
        texto = os.environ.get("BSKY_FORCE_TEXT", "").strip()
        if not texto and ia_ok:
            try:
                texto = ai.generate_post()
            except Exception as e:
                print("  [erro ia]", e)
        if not texto:
            texto = "🔥 Primeiro post do bot no ar! Sigam @his08.bsky.social e fiquem ligados."
        sleep_human(*asp.get("pause_pre_post", [60, 180]))
        safe_post(texto, "forcado")

    cur_min = time.localtime().tm_hour * 60 + time.localtime().tm_min
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
        sleep_human(*asp.get("pause_pre_post", [60, 180]))
        safe_post(texto, "agendado")
        break

    extra_prob = auto.get("extra_post_prob", 0.06)
    if not is_night and ia_ok and random.random() < extra_prob:
        try:
            sleep_human(*asp.get("pause_pre_post", [60, 180]))
            safe_post(ai.generate_post(), "extra")
        except Exception as e:
            print("  [erro post-extra]", e)

    trend_prob = auto.get("trending_post_prob", 0.35)
    if not is_night and ia_ok and (trending_posts or trending_topics) and random.random() < trend_prob:
        tema = None
        if trending_topics:
            tema = random.choice(trending_topics)
        elif trending_posts:
            tema = post_info(random.choice(trending_posts))["text"]
        if tema:
            try:
                sleep_human(*asp.get("pause_pre_post", [60, 180]))
                safe_post(ai.generate_topic_post(tema), "trending")
            except Exception as e:
                print("  [erro post-trending]", e)


# ------------------------------------------------------------ 5) perfil: bio + avatar
def safe_update_profile(**kw):
    try:
        cur = client.get_profile() or {}
    except RuntimeError as e:
        print("  [erro get_profile]", e)
        cur = {}
    display_name = kw.get("display_name", cur.get("displayName"))
    description = kw.get("description", cur.get("description"))
    avatar = kw.get("avatar_blob")
    try:
        client.update_profile(display_name=display_name, description=description, avatar_blob=avatar)
        return True
    except RuntimeError as e:
        print("  [erro update_profile]", e)
        return False


if not rate_limited:
    bio_prob = auto.get("bio_daily_prob", 0.0)
    if force_bio or (ia_ok and not is_night and bio_prob > 0 and chance("bio", bio_prob) and state.get("bio_date") != today()):
        try:
            bio = ai.generate_bio()
            if bio and safe_update_profile(description=bio):
                state["bio_date"] = today()
                print(f"  [bio] atualizada: {bio}")
                sleep_human(5, 15)
        except Exception as e:
            print("  [erro bio]", e)

    avatar_prob = auto.get("avatar_daily_prob", 0.0)
    if force_avatar or (not is_night and avatar_prob > 0 and chance("avatar", avatar_prob) and state.get("avatar_date") != today()):
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
                    state["avatar_date"] = today()
                    print(f"  [avatar] foto de perfil trocada ({mime}, {len(img)} bytes)")
                    break
            except Exception as e:
                print("  [erro avatar]", e)

    try:
        p = client.get_profile() or {}
        bio_now = (p.get("description") or "").replace("\n", " ")[:120]
        print(f"[perfil] displayName={p.get('displayName')!r} bio={bio_now!r} avatar={'OK' if p.get('avatar') else 'vazio'}")
    except RuntimeError as e:
        print("  [erro get_profile-final]", e)

save_state()
print("[i] tick concluído.")
