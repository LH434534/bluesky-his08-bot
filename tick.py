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
  - probabilidade de "presença" por tick (dia ~90%, noite ~40%)
  - pausas humanas longas e aleatórias entre ações e blocos
  - limites por dia/hora/tick conservadores
  - nunca curtir/responder o próprio post, posts repetidos, autores repetidos,
    posts antigos (>7 dias), texto suspeito de spam
  - anti-repetição de replies/posts (regenera até fugir do histórico)
  - backoff: ao ver erro de rate limit (429/4xx/5xx), para o tick na hora

ROBUSTEZ (correções de auditoria):
  - heartbeat gravado logo após o login (dead-man's switch p/ healthcheck)
  - estado salvo via atexit/sys.excepthook (crash não perde contadores/dedupe)
  - load_state com fallback para cache local e abort se repo indisponível
  - save_state com retry + merge em 409 (evita corrupção em corrida)
  - exit codes: 0 ok/ausente | 2 rate-limit/estado indisponível | 1 erro real
  - posts truncados defensivamente em 295 e dedupe por chave normalizada

Env de IA (opcionais): G4F=1 (grátis) | OPENAI/ANTHROPIC/GEMINI keys | AI_API_KEY...
"""
import atexit
import base64
import hashlib
import io
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
import bot_utils

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
comment_cfg = cfg.get("comment_replies", {})
max_comment_tick = comment_cfg.get("max_per_tick", 2)
max_comment_day = comment_cfg.get("max_per_day", 10)
max_comment_hour = comment_cfg.get("max_per_hour", 4)
max_thread_replies = comment_cfg.get("max_thread_replies", 1)

# engajamento v2 (autonomia extrema): nicho + retribuição social
ev2 = cfg.get("engagement_v2", {})
niche_keywords = ev2.get("nicho_keywords", ["música", "arte", "cultura", "Brasil"])
search_limit = ev2.get("search_limit", 15)
min_like_count = ev2.get("min_like_count", 1)
reply_in_search_prob = ev2.get("reply_in_search_prob", 0.35)
# imagem procedural em posts + unfollow seletivo (extras autônomos)
img_cfg = cfg.get("images", {})
img_prob = img_cfg.get("prob", 0.6) if img_cfg.get("enabled", True) else 0.0
unf_cfg = cfg.get("unfollow", {})
follow_back_prob = ev2.get("follow_back_prob", 0.8)
max_followback_tick = ev2.get("follow_back_max_per_tick", 2)
max_followback_day = ev2.get("follow_back_max_per_day", 8)
max_followback_hour = ev2.get("follow_back_max_per_hour", 3)
like_back_prob = ev2.get("like_back_prob", 0.7)
max_likeback_tick = ev2.get("like_back_max_per_tick", 3)
max_likeback_day = ev2.get("like_back_max_per_day", 12)
max_likeback_hour = ev2.get("like_back_max_per_hour", 5)
like_comment_prob = ev2.get("like_comment_prob", 0.5)
fan_feed_limit = ev2.get("fan_feed_limit", 6)

# ------------------------------------------------------------ estado persistente
REPO = os.environ.get("GITHUB_REPOSITORY", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
STATE_PATH = os.path.join(BASE, "state.json")

# Exit code final: 0 ok/ausente, 2 rate-limit/estado indisponível, 1 erro real
FINAL_EXIT = 0


def gh_api(method, path, payload=None):
    url = "https://api.github.com" + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + GH_TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "marvis-bot-tick")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        return e.code, (json.loads(body) if body else {})
    except Exception as e:
        print("[gh] erro de rede:", e)
        return 0, {}


_STATE_DEFAULTS = {
    "date": "", "likes": 0, "replies": 0, "posts": 0, "follows": 0,
    "likes_hour": 0, "hour_key": "", "authors_today": [],
    "seen_uris": [], "reply_history": [], "post_history": [],
    "bio_date": "", "avatar_date": "", "sched_done": {}, "last_heartbeat": "",
    "crash_seq": 0, "alert_issue_date": "", "followers_history": {},
    "proactive_rep_today": 0, "unfollows_done": 0,
    "last_run_id": "",
    # resposta a comentários nos próprios posts
    "replies_comment": 0, "replies_comment_hour": 0, "hour_key_comment": "",
    "thread_replied": {},
    # engajamento v2 (autonomia extrema): retribuição social + descoberta de nicho
    "follows_back": 0, "follows_back_hour": 0, "hour_key_followback": "",
    "liked_back": 0, "liked_back_hour": 0, "hour_key_likeback": "",
    "fans_seen": [], "nicho_kw_idx": 0,
    # autonomy_plus (10 sistemas): rede, boost, radar, escudo, horário, afinação
    "network_follows": 0, "network_follows_hour": 0, "hour_key_network": "",
    "boosts": 0, "boosts_hour": 0, "hour_key_boost": "",
    "blocked_dids": [], "blocked_dids_count": 0,
    "engagement_by_hour": {}, "radar_topics": {}, "tune_adj": {},
    # camada anti-ban extrema (persistida entre ticks/ciclos)
    "action_log": [], "last_social_ts": "", "blocked_until": "",
    "author_last_ts": {},
}


def load_state():
    """Carrega estado do repo; fallback para cache local; aborta se nada disponível."""
    st = dict(_STATE_DEFAULTS)
    merged = False
    # 1) tentar repo (fonte da verdade)
    if REPO and GH_TOKEN:
        code, resp = gh_api("GET", f"/repos/{REPO}/contents/state.json")
        if code == 200:
            try:
                raw = base64.b64decode((resp.get("content") or "").replace("\n", ""))
                loaded = json.loads(raw)
                # preserva chaves extras (metrics/cb/incidents/tuning) e preenche defaults
                for k, v in loaded.items():
                    st[k] = v
                for k in _STATE_DEFAULTS:
                    st.setdefault(k, _STATE_DEFAULTS[k])
                merged = True
                # grava cache local para fallback em falhas futuras
                try:
                    with open(STATE_PATH, "w", encoding="utf-8") as f:
                        json.dump(st, f, ensure_ascii=False)
                except Exception as e:
                    print("[state] erro ao gravar cache:", e)
            except Exception as e:
                print("[state] falha ao decodificar repo:", e)
    # 2) fallback: cache local da última execução
    if not merged:
        try:
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                for k, v in cached.items():
                    st[k] = v
                for k in _STATE_DEFAULTS:
                    st.setdefault(k, _STATE_DEFAULTS[k])
                print("[state] repo indisponível — usando cache local (execução anterior)")
                merged = True
        except Exception as e:
            print("[state] falha ao ler cache local:", e)
    # 3) sem estado nenhum e sem repo -> não agir (evita corromper o estado real)
    if not merged:
        print("[state] sem estado (repo e cache indisponíveis) — abortando tick")
        global FINAL_EXIT
        FINAL_EXIT = 2
        sys.exit(2)
    return st


state = load_state()

# ------------------------------------------------ camada de auto-manutenção (bot_ops)
import bot_ops as bops

METRICS = bops.Metrics(state.get("metrics"))
BSKY_CB = bops.CircuitBreaker(state.get("cb_bsky"), "bsky_api", failure_threshold=4, cooldown_sec=5400)
IA_CB = bops.CircuitBreaker(state.get("cb_ia"), "ia", failure_threshold=3, cooldown_sec=7200)
GH_CB = bops.CircuitBreaker(state.get("cb_gh"), "gh_state", failure_threshold=3, cooldown_sec=3600)
INCIDENTS = bops.IncidentLog(state.get("incidents"))
OPS_REPORT, OPS_ADJ = bops.run_self_check(
    state, METRICS, {"bsky": BSKY_CB, "ia": IA_CB, "gh": GH_CB}, INCIDENTS, cfg)
if OPS_ADJ:
    # aplica ajustes controlados (somente chaves com limites declarados)
    auto = dict(auto)
    for k, v in OPS_ADJ.items():
        if k in asp:
            asp[k] = v
        else:
            auto[k] = v
    bops.json_log("info", "ops.tuning_aplicado", ajustes=OPS_ADJ)
if OPS_REPORT.get("cautious"):
    print("[ops] MODO CAUTELOSO — atividade reduzida (erros recentes altos)")
    bops.json_log("warn", "ops.cauteloso", sinais=OPS_REPORT.get("signals"))
if OPS_REPORT.get("unstable"):
    print("[ops] correções instáveis evitadas:", OPS_REPORT["unstable"])
bsky_ok = BSKY_CB.allow()
ia_use = ia_ok and IA_CB.allow()
if not bsky_ok:
    print("[ops] circuit breaker BLUESKY aberto — tick vira heartbeat-only")
if not ia_use:
    print("[ops] circuit breaker IA aberto — fallback fixo até cooldown")


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
        state["sched_done"] = {}
        state["replies_comment"] = 0
        state["replies_comment_hour"] = 0
        state["hour_key_comment"] = ""
        state["follows_back"] = 0
        state["follows_back_hour"] = 0
        state["liked_back"] = 0
        state["liked_back_hour"] = 0
        state["network_follows"] = 0
        state["network_follows_hour"] = 0
        state["boosts"] = 0
        state["boosts_hour"] = 0
        state["proactive_rep_today"] = 0
        state["unfollows_done"] = 0


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


# ------------------------------------------------------------ anti-ban extremo
def _parse_ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _blocked_reason():
    """Motivo do modo proteção persistente (escrita pausada) ou None."""
    bu = state.get("blocked_until") or ""
    ts = _parse_ts(bu) if bu else None
    if ts and time.time() < ts:
        mins = int((ts - time.time()) // 60)
        return f"modo proteção anti-ban ativo (~{mins} min restantes)"
    if bu:
        state["blocked_until"] = ""   # expirou: libera
    return None


def _rate_remaining():
    """Mínimo de requisições restantes informado pelo PDS; -1 = sem informação."""
    h = getattr(client, "last_rate_headers", {}) or {}
    for k in ("ratelimit-remaining", "x-ratelimit-remaining"):
        if h.get(k) is not None:
            try:
                return int(h[k])
            except Exception:
                return -1
    return -1


def _set_block(mins, tag, cause, action="modo_protecao"):
    ts = datetime.now(timezone.utc).timestamp() + mins * 60
    state["blocked_until"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    INCIDENTS.add(tag, context="tick", cause=cause[:150], action=action, outcome="ok")


def burst_ok():
    """Guarda anti-rajada: no máx. N ações de escrita em janela de M minutos."""
    win = asp.get("burst_window_min", 10)
    mx = asp.get("burst_max_per_10min", 5)
    cutoff = time.time() - win * 60
    state["action_log"] = [t for t in state.get("action_log", []) if t >= cutoff]
    return len(state["action_log"]) < mx


def social_gap_ok():
    """Gap mínimo entre ações sociais (evita rajada mesmo dentro do tick)."""
    last = state.get("last_social_ts") or ""
    ts = _parse_ts(last) if last else None
    if ts is None:
        return True
    return (time.time() - ts) >= asp.get("min_social_gap_s", 45)


def record_action():
    """Registra uma ação de escrita para as guardas anti-burst/gap."""
    state.setdefault("action_log", []).append(time.time())
    state["action_log"] = state["action_log"][-120:]
    state["last_social_ts"] = datetime.now(timezone.utc).isoformat()


def save_state():
    """Persiste estado: cache local SEMPRE; repo com retry + merge em 409."""
    if not (REPO and GH_TOKEN):
        return
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print("[state] erro ao gravar cache local:", e)
    content = base64.b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()
    for attempt in range(3):
        try:
            code, existing = gh_api("GET", f"/repos/{REPO}/contents/state.json")
            if code != 200:
                print(f"[state] GET remoto falhou ({code}) — mantém apenas cache local")
                return
            payload = {
                "message": "state update",
                "content": content,
                "sha": existing.get("sha"),
            }
            put_code, put_resp = gh_api("PUT", f"/repos/{REPO}/contents/state.json", payload)
            if put_code in (200, 201):
                print("[state] salvo e sincronizado")
                return
            if put_code == 409:
                # corrida com outra execução: re-lê e tenta de novo (local vence)
                print(f"[state] conflito 409 (tentativa {attempt + 1}) — re-sincronizando")
                continue
            print(f"[state] PUT falhou: {put_code} {put_resp}")
            return
        except Exception as e:
            print("[state] erro ao sincronizar:", e)
            return
    print("[state] falha persistente ao sincronizar — mantém cache local")


# alerta de falha via GitHub Issue (1 por dia, best-effort; não afeta o fluxo)
def open_alert_issue(title, body):
    if not (REPO and GH_TOKEN):
        return False
    if state.get("alert_issue_date") == today():
        return False
    try:
        code, resp = gh_api("POST", f"/repos/{REPO}/issues",
                            {"title": title, "body": body})
        if code in (200, 201):
            state["alert_issue_date"] = today()
            print("[alerta] issue criada:", resp.get("html_url", ""))
            return True
        print(f"[alerta] falha ao criar issue: {code} {resp}")
    except Exception as e:
        print("[alerta] erro ao criar issue:", e)
    return False


# garante persistência mesmo em crash/exit (kill -9 não tem como cobrir)
def _log_crash_and_exit(exc_type, exc_value, tb):
    print(f"[crash] exceção não capturada: {exc_type.__name__}: {exc_value}")
    import traceback
    traceback.print_exception(exc_type, exc_value, tb)
    state["crash_seq"] = state.get("crash_seq", 0) + 1
    if state["crash_seq"] >= 3:
        open_alert_issue(f"[BOT] {state['crash_seq']} crashes seguidos",
                         f"O bot travou {state['crash_seq']} vezes seguidas.\n\n"
                         f"Último erro: {exc_type.__name__}: {exc_value}\n\n"
                         "Cheque os logs da última execução no Actions.")
    save_state()
    sys.__excepthook__(exc_type, exc_value, tb)


sys.excepthook = _log_crash_and_exit
atexit.register(save_state)


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
    return bot_utils.looks_spammy(info["text"])


def can_engage(info):
    if not info["uri"] or not info["did"]:
        return False
    if info["did"] == client.did:
        return False
    if info["uri"] in state["seen_uris"]:
        return False
    if info["did"] in state["authors_today"]:
        return False
    # cooldown por autor: não voltar ao mesmo perfil em poucos minutos
    cooldown = asp.get("author_cooldown_min", 90)
    last = state.get("author_last_ts", {}).get(info["did"])
    if last:
        try:
            if time.time() - float(last) < cooldown * 60:
                return False
        except Exception:
            pass
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
    state.setdefault("author_last_ts", {})[info["did"]] = time.time()
    max_seen = asp.get("max_state_seen", 3000)
    if len(state["seen_uris"]) > max_seen:
        state["seen_uris"] = state["seen_uris"][-max_seen:]
    max_auth = asp.get("max_authors_per_day", 12)
    if len(state["authors_today"]) > max_auth:
        state["authors_today"] = state["authors_today"][-max_auth:]
    bump("likes" if action == "like" else "replies")
    record_action()


# ---- S8 mood rotativo (templates de reply variam com o humor do dia)
def mood_templates():
    m = day_seed("mood") % 3
    base = engage.get("reply_templates", ["Boa!"])
    if m == 1:
        alt = ["véi, isso é genial 🔥", "caralho, que visão", "manda mais disso, pfv",
               "tu resumiu tudo", "brabo dms", "só verdades", "não tankei, melhor post",
               "exatamente isso", "tô surpreso, que foda", "posta mais, por favor"]
    elif m == 2:
        alt = ["curti demais, sério", "que bom que postou isso", "tava precisando ler isso hoje",
               "melhor coisa do feed", "valeu por compartilhar", "bem pensado", "gostei muito",
               "isso me fez o dia", "preciso falar sobre isso depois", "forte demais"]
    else:
        alt = ["Boa! 🔥", "Concordo demais!", "Muito bom isso.", "Tô nessa também!", "Isso aí! 👏"]
    return [t for t in (alt + base) if t not in state["reply_history"]]


# ------------------------------------------------------------ 0) modo diag (só leitura)
diag_notif = os.environ.get("BSKY_DIAG_NOTIF", "").strip().lower() in ("1", "true", "yes", "sim")
if diag_notif:
    try:
        n = client.list_notifications(limit=50)
        notifs = n.get("notifications", [])
        print(f"[diag] total notificações: {len(notifs)}")
        for it in notifs[:10]:
            rec = it.get("record", {}) or {}
            print(f"  - {it.get('reason')} | @{it.get('author', {}).get('handle', '?')} | "
                  f"root={rec.get('reply', {}).get('root', {}).get('uri', '-')} | {rec.get('text', '')[:40]!r}")
    except Exception as e:
        print("[diag] erro:", e)
    print("[i] tick concluído (modo diag).")
    sys.exit(0)


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


# ------------------------------------------------------------ 0.5) heartbeat (dead-man's switch)
state["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
state["last_run_id"] = f"{today()}-{os.getpid()}"
METRICS.inc("ticks")
save_state()


# ------------------------------------------------------------ 1) contexto
timeline_posts = []
try:
    tl = client.timeline(limit=30)
    timeline_posts = [it for it in tl.get("feed", []) if it.get("post")]
    BSKY_CB.record_success()
    print(f"[contexto] timeline lida: {len(timeline_posts)} posts")
except RuntimeError as e:
    print("  [erro timeline]", e)
    METRICS.error("bsky.timeline")
    BSKY_CB.record_failure()

trending_posts = []
trending_topics = []
if auto.get("trending_enabled", True):
    try:
        hot = client.get_popular(limit=20)
        trending_posts = [it for it in hot.get("feed", []) if it.get("post")]
        if trending_posts:
            BSKY_CB.record_success()
            print(f"[contexto] posts em alta lidos: {len(trending_posts)}")
    except RuntimeError as e:
        if "MethodNotImplemented" in str(e) or "501" in str(e):
            # feed "em alta" removido do PDS atual: não é falha do bot, é falta de feature
            print("  [trending] feed 'em alta' indisponível no PDS — usando busca por nicho")
        else:
            print("  [erro trending]", e)
            METRICS.error("bsky.trending")
            BSKY_CB.record_failure()
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
                    if "MethodNotImplemented" not in str(e) and "501" not in str(e):
                        print("  [erro trending]", e)
                        METRICS.error("bsky.trending")
        except RuntimeError as e:
            if "MethodNotImplemented" not in str(e) and "501" not in str(e):
                print("  [erro trending]", e)
                METRICS.error("bsky.trending")
    # descoberta de nicho rotativa: cada tick usa uma keyword diferente (autonomia)
    if not trending_posts and niche_keywords:
        idx = state.get("nicho_kw_idx", 0) % len(niche_keywords)
        kw = niche_keywords[idx]
        state["nicho_kw_idx"] = (idx + 1) % len(niche_keywords)
        try:
            r = client.search_posts(kw, limit=search_limit)
            posts = [it for it in r.get("posts", []) if it.get("uri")]
            if posts:
                trending_posts = posts
                print(f"[contexto] busca '{kw}': {len(posts)} posts")
        except RuntimeError as e:
            if "MethodNotImplemented" not in str(e) and "501" not in str(e):
                print("  [erro trending]", e)
                METRICS.error("bsky.trending")

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
    sys.exit(FINAL_EXIT)
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
follows_back_done = 0
likes_back_done = 0
rate_limited = False

# ---- anti-ban extremo: modo proteção persistente + rate headers do PDS
block_reason = _blocked_reason()
if block_reason:
    print(f"[anti-ban] {block_reason} — apenas leitura neste tick")
_rate_now = _rate_remaining()
if _rate_now >= 0 and _rate_now <= asp.get("min_rate_remaining", 3):
    print(f"[anti-ban] PDS rate limit baixo ({_rate_now}) — sem escrita neste tick")
    rate_limited = True
social_ok = (not is_night) and (not rate_limited) and bsky_ok and (not block_reason) \
            and burst_ok() and social_gap_ok()
if not social_ok and not block_reason and not is_night and bsky_ok and not rate_limited:
    print("[anti-ban] guarda anti-rajada ativa (burst/gap) — sem interação social neste tick")


def rate_stop(exc):
    """Escalada anti-ban: 403 => modo proteção longo; 401 => pausa curta;
    429/408/420 => pausa anti-burst; 5xx => só para este tick."""
    global rate_limited, FINAL_EXIT
    msg = str(exc)
    code = None
    m = re.search(r"HTTP (\d{3})", msg)
    if m:
        code = int(m.group(1))
    hit = any(t in msg for t in ("429", "403", "408", "420", "500", "502", "503", "RateLimit", "enhance your calm"))
    if not hit and code not in (401,):
        return False
    rate_limited = True
    FINAL_EXIT = 2
    METRICS.error("bsky.rate_limit")
    if code == 403 or "403" in msg:
        _set_block(asp.get("block_403_min", 180), "bsky.block_403", msg)
        print(f"  [anti-ban] 403 (possível bloqueio da conta) — modo proteção por "
              f"{asp.get('block_403_min', 180)} min, sem escrita")
    elif code == 401 or "401" in msg:
        _set_block(asp.get("block_ratelimit_min", 30), "bsky.auth_invalid", msg)
        print(f"  [anti-ban] 401 (sessão inválida) — pausa de "
              f"{asp.get('block_ratelimit_min', 30)} min para revalidar")
    elif code in (429, 408, 420) or any(t in msg for t in ("RateLimit", "enhance your calm")):
        _set_block(asp.get("block_ratelimit_min", 30), "bsky.rate_limit", msg, action="parar_tick")
        print(f"  [anti-ban] rate limit detectado — pausa anti-burst de "
              f"{asp.get('block_ratelimit_min', 30)} min")
    else:
        INCIDENTS.add("bsky.rate_limit", context="tick", cause=msg[:150],
                      action="parar_tick", outcome="ok")
        print("  [anti-ban] erro de servidor detectado — parando ações deste tick")
    return True


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
        METRICS.inc("likes_ok")
        BSKY_CB.record_success()
        print(f"  [like] {info['handle']}: {info['text'][:45]}")
        sleep_human(*asp.get("pause_like", [8, 20]))
    except RuntimeError as e:
        print("  [erro like]", e)
        METRICS.error("bsky.like")
        BSKY_CB.record_failure()
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
                text = ai.generate_reply(info["text"], history=state["reply_history"])
                IA_CB.record_success()
            except Exception as e:
                print("  [erro ia]", e)
                METRICS.error("ia.reply")
                IA_CB.record_failure()
                text = ""
            if text and text not in state["reply_history"]:
                break
            text = ""
        if not text:
            tpl = mood_templates()
            text = random.choice(tpl) if tpl else random.choice(engage.get("reply_templates", ["Boa!"]))
    try:
        client.reply(text, info["uri"], info["cid"], info["uri"], info["cid"])
        mark_engaged(info, "reply")
        state["reply_history"].append(text)
        state["reply_history"] = state["reply_history"][-40:]
        replies_done += 1
        METRICS.inc("replies_ok")
        BSKY_CB.record_success()
        print(f"  [reply] {info['handle']}: {text}")
        sleep_human(*asp.get("pause_reply", [10, 25]))
    except RuntimeError as e:
        print("  [erro reply]", e)
        METRICS.error("bsky.reply")
        BSKY_CB.record_failure()
        rate_stop(e)


# ------------------------------------------------------------ 2) engajamento (só de dia)
if social_ok:
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

if social_ok:
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

# ---- 2.4) comentário proativo em posts de terceiros (nicho/busca)
# Só de dia, probabilidade configurável (reply_in_search_prob), 1 reply por tick:
# busca posts frescos da keyword rotativa e comenta — orçamentos/cooldowns do do_reply.
if social_ok and reply_in_search_prob > 0 and state.get("proactive_rep_today", 0) < max_replies_tick:
    if replies_done < max_replies_tick and not rate_limited:
        kw = random.choice(niche_keywords)
        try:
            posts = client.search_posts(kw, limit=search_limit)
            cands = [post_info(p) for p in (posts.get("posts", []) if isinstance(posts, dict) else posts)]
            cands = [c for c in cands if c["text"]]
            cands.sort(key=lambda c: c.get("like_count", 0) or 0, reverse=True)
            if cands and random.random() < reply_in_search_prob:
                info = cands[0]
                do_reply(info)
                state["proactive_rep_today"] = state.get("proactive_rep_today", 0) + 1
        except RuntimeError as e:
            print("  [erro busca-coment]", e)
            METRICS.error("bsky.search")
            BSKY_CB.record_failure()
            rate_stop(e)

# ------------------------------------------------------------ 2.5) responder comentários nos próprios posts
# Via app.bsky.notification.listNotifications: reason=='reply' = alguém comentou
# em post do bot (ou respondeu a um comentário do bot). Só responde quando a RAIZ
# do fio é post do bot (evita meter resposta em fio alheio). Dedupe + orçamentos
# anti-spam idênticos ao resto do bot; no máx. N respostas por thread (padrão 1).


def comment_budget_ok():
    if not budget_ok("replies", max_replies_day):
        return False
    if state.get("replies_comment", 0) >= max_comment_day:
        return False
    if state.get("hour_key_comment") != hour_key():
        state["hour_key_comment"] = hour_key()
        state["replies_comment_hour"] = 0
    if state.get("replies_comment_hour", 0) >= max_comment_hour:
        return False
    return True


def do_comment_reply(notif, root_uri, root_cid, comment_text):
    """Responde um comentário em post próprio. Retorna True se respondeu."""
    global replies_done
    if rate_limited or replies_done >= max_replies_tick:
        return False
    if not comment_budget_ok():
        return False
    author = notif.get("author", {}) or {}
    n_uri = notif.get("uri", "")
    n_cid = notif.get("cid", "")
    if not n_uri or not n_cid or not comment_text:
        return False
    if author.get("did") == client.did:
        return False
    if n_uri in state["seen_uris"]:
        return False
    if is_spamish({"text": comment_text}):
        print("  [coment] ignorado (spam):", comment_text[:40])
        return False
    trep = state.setdefault("thread_replied", {})
    if trep.get(root_uri, 0) >= max_thread_replies:
        return False
    # texto: IA contextual ou template (nunca repetir histórico)
    text = None
    if ia_use:
        for _ in range(3):
            try:
                text = ai.generate_reply(comment_text, history=state["reply_history"])
                IA_CB.record_success()
            except Exception as e:
                print("  [erro ia]", e)
                METRICS.error("ia.reply")
                IA_CB.record_failure()
                text = ""
            if text and text not in state["reply_history"]:
                break
            text = ""
    if not text:
        tpl = mood_templates()
        text = random.choice(tpl) if tpl else random.choice(engage.get("reply_templates", ["Boa!"]))
    try:
        client.reply(text, root_uri, root_cid, n_uri, n_cid)
        state["seen_uris"].append(n_uri)
        trep[root_uri] = trep.get(root_uri, 0) + 1
        if len(trep) > 200:
            for k in list(trep)[:-200]:
                del trep[k]
        state["reply_history"].append(text)
        state["reply_history"] = state["reply_history"][-40:]
        state.setdefault("author_last_ts", {})[author.get("did", "")] = time.time()
        state["replies_comment"] = state.get("replies_comment", 0) + 1
        state["replies_comment_hour"] = state.get("replies_comment_hour", 0) + 1
        bump("replies")
        record_action()
        replies_done += 1
        METRICS.inc("replies_ok")
        BSKY_CB.record_success()
        print(f"  [coment] @{author.get('handle', '?')}: {comment_text[:40]!r} -> {text}")
        sleep_human(*asp.get("pause_reply", [10, 25]))
        return True
    except RuntimeError as e:
        print("  [erro coment]", e)
        METRICS.error("bsky.reply")
        BSKY_CB.record_failure()
        rate_stop(e)
    return False


if social_ok and comment_cfg.get("enabled", True):
    try:
        n = client.list_notifications(limit=comment_cfg.get("notify_limit", 50))
        notifs = n.get("notifications", [])
        BSKY_CB.record_success()
        cands = [it for it in notifs
                 if it.get("reason") == "reply" or
                 (comment_cfg.get("include_mentions", True) and it.get("reason") == "mention")]
        cands.sort(key=lambda it: it.get("indexedAt", ""), reverse=True)
        if cands:
            print(f"[coment] {len(cands)} comentário(s)/menção(ões) para avaliar")
        for it in cands:
            if rate_limited or replies_done >= max_comment_tick:
                break
            rec = it.get("record", {}) or {}
            if not rec.get("text") or not it.get("uri") or not it.get("cid"):
                continue
            if it.get("author", {}).get("did") == client.did:
                continue
            if it["uri"] in state["seen_uris"]:
                continue
            if random.random() >= comment_cfg.get("reply_prob", 0.85):
                print("  [humano] passou reto (não respondeu o comentário)")
                continue
            # raiz do fio: post do bot (mention não tem fio — o próprio post é raiz)
            if it.get("reason") == "mention":
                root_uri, root_cid = it["uri"], it["cid"]
            else:
                root = rec.get("reply", {}).get("root", {}) or {}
                if not root.get("uri"):
                    continue
                try:
                    thr = client.get_thread(root["uri"], depth=0)
                    root_post = (thr.get("thread", {}) or {}).get("post", {}) or {}
                    if (root_post.get("author", {}) or {}).get("did") != client.did:
                        print("  [coment] raiz não é post do bot — ignorado")
                        continue
                except RuntimeError as e:
                    print("  [erro thread]", e)
                    METRICS.error("bsky.thread")
                    BSKY_CB.record_failure()
                    continue
                root_uri, root_cid = root.get("uri"), root.get("cid")
            if not root_uri or not root_cid:
                continue
            do_comment_reply(it, root_uri, root_cid, rec.get("text", ""))
        # marca notificações como lidas (best-effort, só UI — não afeta dedupe)
        try:
            client.update_seen()
        except RuntimeError:
            pass
    except RuntimeError as e:
        print("  [erro notifications]", e)
        METRICS.error("bsky.notifications")
        BSKY_CB.record_failure()

# follow ocasional (só de dia, raro)
follow_prob = auto.get("follow_prob", 0.0)
if social_ok and follow_prob > 0 and random.random() < follow_prob:
    if budget_ok("follows", asp.get("max_follows_per_day", 2)):
        candidate = next((post_info(it) for it in (trending_posts + timeline_posts)
                          if post_info(it).get("did") and post_info(it).get("did") != client.did
                          and "bot" not in post_info(it).get("handle", "").lower()), None)
        if candidate:
            try:
                client.follow(candidate["did"])
                bump("follows")
                record_action()
                METRICS.inc("follows_ok")
                BSKY_CB.record_success()
                print(f"  [follow] @{candidate['handle']}")
                sleep_human(5, 12)
            except RuntimeError as e:
                print("  [erro follow]", e)
                METRICS.error("bsky.follow")
                BSKY_CB.record_failure()
                rate_stop(e)


# ---- 3.5b) unfollow seletivo (saúde da rede, só de dia)
# Deixa de seguir quem não segue de volta, com caps baixos (2/dia, 1/tick),
# só contas seguidas há >= min_age_days e nunca recém-seguidos (fans_seen).
if (unf_cfg.get("enabled", True) and social_ok
        and state.get("unfollows_done", 0) < unf_cfg.get("max_per_day", 2)
        and not rate_limited and not block_reason):
    try:
        mine = client.get_follows(client.did, limit=unf_cfg.get("scan_limit", 100))
        follows = mine.get("records", []) or []
        myf = client.list_followers(client.did, limit=200)
        back = {f.get("did") for f in myf.get("followers", []) if f.get("did")}
        done = 0
        fans = set(state.get("fans_seen", [])) or set()
        for rec in follows:
            if done >= unf_cfg.get("max_per_tick", 1):
                break
            val = rec.get("value", {}) or {}
            subj = val.get("subject", "")
            if not subj or subj == client.did:
                continue
            if subj in back or subj in fans:
                continue
            criado = val.get("createdAt", "") or ""
            if iso_age_days(criado) < unf_cfg.get("min_age_days", 2):
                continue
            if random.random() >= unf_cfg.get("prob", 0.5):
                continue
            client.unfollow(rec.get("uri", ""))
            state["unfollows_done"] = state.get("unfollows_done", 0) + 1
            record_action()
            METRICS.inc("unfollow_ok")
            BSKY_CB.record_success()
            done += 1
            print(f"  [limpeza] unfollow @{subj}")
            sleep_human(*asp.get("pause_block", [15, 30]))
    except RuntimeError as e:
        print("  [erro unfollow]", e)
        METRICS.error("bsky.unfollow")
        BSKY_CB.record_failure()
        rate_stop(e)


# ------------------------------------------------------------ 3.6) engajamento v2 (autonomia extrema)
# retribuição social: follow-back de novos seguidores + like-back de quem curtiu
# + visita a fãs (curtir um post recente deles) — tudo probabilístico e com caps

def ev2_ok(kind):
    """Limites do engajamento v2 (anti-ban): tick, hora e dia."""
    if rate_limited:
        return False
    if kind == "follow":
        return (state.get("follows_back", 0) < max_followback_day
                and state.get("follows_back_hour", 0) < max_followback_hour
                and follows_back_done < max_followback_tick)
    if kind == "like":
        return (state.get("liked_back", 0) < max_likeback_day
                and state.get("liked_back_hour", 0) < max_likeback_hour
                and likes_back_done < max_likeback_tick)
    return True


def is_blocked_did(did):
    return did in state.get("blocked_dids", [])


def do_follow_back(author):
    """Segue de volta um novo seguidor (reciprocidade)."""
    global follows_back_done
    if not ev2_ok("follow"):
        return False
    did = author.get("did", "")
    handle = (author.get("handle", "") or "").lower()
    if not did or did == client.did or "bot" in handle:
        return False
    if did in state.get("fans_seen", []):
        return False
    if is_blocked_did(did):
        return False
    try:
        client.follow(did)
        state["follows_back"] = state.get("follows_back", 0) + 1
        state["follows_back_hour"] = state.get("follows_back_hour", 0) + 1
        state.setdefault("fans_seen", []).append(did)
        if len(state["fans_seen"]) > 500:
            state["fans_seen"] = state["fans_seen"][-500:]
        bump("follows")
        record_action()
        METRICS.inc("followback_ok")
        BSKY_CB.record_success()
        follows_back_done += 1
        print(f"  [followback] @{handle}")
        sleep_human(*asp.get("pause_follow", [8, 18]))
        return True
    except RuntimeError as e:
        print("  [erro followback]", e)
        METRICS.error("bsky.followback")
        BSKY_CB.record_failure()
        rate_stop(e)
    return False


def do_like_back(author, uri, cid):
    """Curti de volta um post (like recebido ou post de fã)."""
    global likes_back_done
    if not ev2_ok("like"):
        return False
    did = author.get("did", "")
    if not did or did == client.did or not uri or not cid:
        return False
    if uri in state["seen_uris"]:
        return False
    if is_blocked_did(did):
        return False
    try:
        client.like(uri, cid)
        state["liked_back"] = state.get("liked_back", 0) + 1
        state["liked_back_hour"] = state.get("liked_back_hour", 0) + 1
        state["seen_uris"].append(uri)
        if len(state["seen_uris"]) > 800:
            state["seen_uris"] = state["seen_uris"][-800:]
        bump("likes")
        record_action()
        METRICS.inc("likeback_ok")
        BSKY_CB.record_success()
        likes_back_done += 1
        print(f"  [likeback] @{author.get('handle', '?')}")
        sleep_human(*asp.get("pause_like", [6, 14]))
        return True
    except RuntimeError as e:
        print("  [erro likeback]", e)
        METRICS.error("bsky.likeback")
        BSKY_CB.record_failure()
        rate_stop(e)
    return False


# ------------------------------------------------------------ 3.7) autonomy_plus — 10 sistemas (o bot vira literalmente o melhor do mundo)
# S1 rede | S2 boost | S3 curador | S4 radar | S5 horário inteligente
# S6 auto-cura | S7 escudo anti-spam | S8 mood rotativo | S9 auto-afinação | S10 thread builder
apx = cfg.get("autonomy_plus", {})
apx_on = apx.get("enabled", True)
net_cfg = apx.get("network", {})
boost_cfg = apx.get("booster", {})
cur_cfg = apx.get("curator", {})
radar_cfg = apx.get("radar", {})
shield_cfg = apx.get("shield", {})
thread_cfg = apx.get("thread", {})
sh_cfg = apx.get("smart_hours", {})
st_cfg = apx.get("self_tune", {})


# ---- S5 horário inteligente: aprende em que horas o perfil recebe engajamento
def collect_engagement_hours(notifs):
    if not (apx_on and sh_cfg.get("enabled", True)):
        return
    ebyh = state.setdefault("engagement_by_hour", {})
    for it in notifs:
        if it.get("reason") not in ("like", "reply", "follow"):
            continue
        idx = (it.get("indexedAt") or "")
        try:
            h = int(idx[11:13]) if len(idx) >= 13 else -1
        except Exception:
            h = -1
        if 0 <= h <= 23:
            ebyh[str(h)] = ebyh.get(str(h), 0) + 1


if social_ok and ev2.get("enabled", True):
    try:
        n = client.list_notifications(limit=ev2.get("notify_limit", 60))
        notifs = n.get("notifications", [])
        BSKY_CB.record_success()
        # S5 (horário inteligente): aprende em que horas o perfil recebe engajamento
        collect_engagement_hours(notifs)
        # follow-back: novos seguidores (probabilístico, respeita caps)
        for it in notifs:
            if rate_limited or follows_back_done >= max_followback_tick:
                break
            if it.get("reason") != "follow":
                continue
            if random.random() >= follow_back_prob:
                continue
            do_follow_back(it.get("author", {}) or {})
        # like-back: quem curtiu nossos posts (probabilístico, respeita caps)
        for it in notifs:
            if rate_limited or likes_back_done >= max_likeback_tick:
                break
            if it.get("reason") != "like":
                continue
            if random.random() >= like_back_prob:
                continue
            rec = it.get("record", {}) or {}
            subj = rec.get("subject", {}) or {}
            if subj.get("uri"):
                do_like_back(it.get("author", {}) or {}, subj.get("uri", ""), subj.get("cid", ""))
        try:
            client.update_seen()
        except RuntimeError:
            pass
    except RuntimeError as e:
        print("  [erro notif v2]", e)
        METRICS.error("bsky.notifications")
        BSKY_CB.record_failure()
    # visita a fãs: curtir um post recente de quem acabamos de seguir (reciprocidade orgânica)
    if ev2.get("fan_feed_like", True) and follows_back_done > 0:
        for did in list(state.get("fans_seen", []))[-follows_back_done:]:
            if rate_limited or likes_back_done >= max_likeback_tick:
                break
            try:
                feed = client.get_author_feed(did, limit=fan_feed_limit)
                posts = [it for it in feed.get("feed", []) if it.get("post")]
                cand = next((p for p in posts
                             if p["post"].get("uri") not in state["seen_uris"]
                             and (p["post"].get("likeCount") or 0) >= min_like_count), None)
                if cand:
                    post = cand["post"]
                    do_like_back({"did": did, "handle": "fã"},
                                 post.get("uri", ""), post.get("cid", ""))
            except RuntimeError as e:
                print("  [erro feed fã]", e)
                METRICS.error("bsky.author_feed")
                BSKY_CB.record_failure()
                break


# ------------------------------------------------------------ 3.7) autonomy_plus — 10 sistemas (o bot vira literalmente o melhor do mundo)
# S1 rede | S2 boost | S3 curador | S4 radar | S5 horário inteligente
# S6 auto-cura | S7 escudo anti-spam | S8 mood rotativo | S9 auto-afinação | S10 thread builder
network_done = 0
boost_done = 0


def apx_hour_ok(key, hkey, cap_hour):
    """Zera contador por hora quando vira a hora; True se ainda cabe no cap."""
    if state.get(hkey) != hour_key():
        state[hkey] = hour_key()
        state[key] = 0
    return state.get(key, 0) < cap_hour


def apx_cap_ok(key, hkey, cap_day, cap_hour, done, cap_tick):
    if rate_limited:
        return False
    if not apx_hour_ok(key, hkey, cap_hour):
        return False
    return state.get(key, 0) < cap_day and done < cap_tick


# ---- S7 escudo anti-spam: identifica e bloqueia perfis suspeitos
SUS_HANDLE = re.compile(r"\b(bot|spam|promo|onlyfans|telegram|whatsapp|sorteio|ganhe|auto)\b", re.I)


def scan_shield(notifs):
    """Bloqueia (probabilístico) perfis suspeitos que interagem com a conta."""
    if not (apx_on and shield_cfg.get("enabled", True)):
        return
    if rate_limited:
        return
    dia = shield_cfg.get("max_per_day", 5)
    if state.get("blocked_dids_count", 0) >= dia:
        return
    for it in notifs:
        if state.get("blocked_dids_count", 0) >= dia:
            break
        auth = it.get("author", {}) or {}
        did = auth.get("did", "")
        handle = (auth.get("handle") or "").lower()
        if not did or did == client.did or did in state.get("blocked_dids", []):
            continue
        if not SUS_HANDLE.search(handle):
            continue
        if random.random() >= shield_cfg.get("prob", 0.6):
            continue
        try:
            client.block(did)
            state.setdefault("blocked_dids", []).append(did)
            state["blocked_dids"] = state["blocked_dids"][-200:]
            state["blocked_dids_count"] = state.get("blocked_dids_count", 0) + 1
            record_action()
            METRICS.inc("shield_block_ok")
            BSKY_CB.record_success()
            print(f"  [escudo] bloqueado @{handle}")
            sleep_human(*asp.get("pause_block", [8, 20]))
        except RuntimeError as e:
            print("  [erro escudo]", e)
            METRICS.error("bsky.shield")
            BSKY_CB.record_failure()
            rate_stop(e)


# ---- S4 radar de tendências: acumula tópicos para o curador e futura análise
def collect_radar(posts, topics):
    if not (apx_on and radar_cfg.get("enabled", True)):
        return
    radar = state.setdefault("radar_topics", {})
    for t in topics:
        if t:
            radar[t] = radar.get(t, 0) + 1
    for item in (posts or [])[:12]:
        txt = post_info(item).get("text", "")
        for kw in niche_keywords:
            if kw.lower() in txt.lower():
                radar[kw] = radar.get(kw, 0) + 1
    mx = radar_cfg.get("max_topics", 50)
    if len(radar) > mx:
        for k in list(sorted(radar, key=radar.get))[:-mx]:
            del radar[k]


# ---- S5 horário inteligente (definição movida para antes do uso no fluxo)


# ---- S5 horário inteligente: aprende em que horas o perfil recebe engajamento
def smart_extra_mult():
    """Multiplicador de postagem com base nas horas de maior engajamento."""
    if not (apx_on and sh_cfg.get("enabled", True)):
        return 1.0
    ebyh = state.get("engagement_by_hour", {})
    if not ebyh:
        return 1.0
    top = sorted(ebyh, key=ebyh.get, reverse=True)[:sh_cfg.get("boost_top_hours", 2)]
    if str(time.localtime().tm_hour) in top:
        return 1.0 + sh_cfg.get("boost_mult", 0.4)
    return 1.0


# ---- S9 auto-afinação: ajusta probabilidades com base em sinais de vida
def self_tune():
    if not (apx_on and st_cfg.get("enabled", True)):
        return
    ebyh = state.get("engagement_by_hour", {})
    total = sum(ebyh.values()) if ebyh else 0
    adj = state.setdefault("tune_adj", {})
    step = st_cfg.get("step", 0.03)
    low = total < st_cfg.get("low_threshold", 2)
    if low:
        adj["extra_post_prob"] = max(auto.get("extra_post_prob", 0.8) - step, 0.4)
        adj["follow_back_prob"] = max(follow_back_prob - step, 0.4)
    else:
        adj["extra_post_prob"] = min(auto.get("extra_post_prob", 0.8) + step, 0.95)
        adj["follow_back_prob"] = min(follow_back_prob + step, 0.9)
    state["tune_adj"] = adj


# ---- S1 rede de crescimento: segue seguidores de quem nos segue (2º grau)
def do_network_follow(did, handle):
    global network_done
    if not apx_cap_ok("network_follows", "hour_key_network",
                      net_cfg.get("max_per_day", 8), net_cfg.get("max_per_hour", 3),
                      network_done, net_cfg.get("max_per_tick", 2)):
        return False
    if not did or did == client.did or "bot" in handle.lower():
        return False
    if did in state.get("fans_seen", []) or is_blocked_did(did):
        return False
    try:
        client.follow(did)
        state["network_follows"] = state.get("network_follows", 0) + 1
        state.setdefault("fans_seen", []).append(did)
        bump("follows")
        record_action()
        METRICS.inc("network_follow_ok")
        BSKY_CB.record_success()
        network_done += 1
        print(f"  [rede] follow @{handle}")
        sleep_human(*asp.get("pause_follow", [8, 18]))
        return True
    except RuntimeError as e:
        print("  [erro rede]", e)
        METRICS.error("bsky.network")
        BSKY_CB.record_failure()
        rate_stop(e)
    return False


# ---- S2 boost: reposta posts populares do nicho (sem quote, só repost)
def do_boost(info):
    global boost_done
    if not apx_cap_ok("boosts", "hour_key_boost",
                      boost_cfg.get("max_per_day", 4), boost_cfg.get("max_per_hour", 2),
                      boost_done, boost_cfg.get("max_per_tick", 1)):
        return False
    if not info["uri"] or not info["cid"] or info["did"] == client.did:
        return False
    if info["uri"] in state["seen_uris"] or is_blocked_did(info["did"]):
        return False
    if is_spamish(info):
        return False
    try:
        client.repost(info["uri"], info["cid"])
        state["boosts"] = state.get("boosts", 0) + 1
        state["seen_uris"].append(info["uri"])
        record_action()
        METRICS.inc("boost_ok")
        BSKY_CB.record_success()
        boost_done += 1
        print(f"  [boost] repost de @{info['handle']}")
        sleep_human(*asp.get("pause_block", [10, 25]))
        return True
    except RuntimeError as e:
        print("  [erro boost]", e)
        METRICS.error("bsky.boost")
        BSKY_CB.record_failure()
        rate_stop(e)
    return False


# ---- S3 curador de nicho: gera post temático da keyword rotativa
def curate_niche_post():
    if not (apx_on and cur_cfg.get("enabled", True) and ia_use):
        return
    if not social_ok:
        return
    if random.random() >= cur_cfg.get("nicho_post_prob", 0.5) * (0.4 if is_night else 1.0):
        return
    if not niche_keywords:
        return
    idx = state.get("nicho_kw_idx", 0) % len(niche_keywords)
    kw = niche_keywords[idx]
    try:
        sleep_human(*asp.get("pause_pre_post", [60, 180]))
        texto = ai.generate_topic_post(kw, history=state["post_history"])
        IA_CB.record_success()
        if texto and len(texto) > 280 and thread_cfg.get("enabled", True):
            safe_thread(texto, "nicho")
        elif texto:
            safe_post(texto, "nicho", img_prob=img_prob)
    except Exception as e:
        print("  [erro post-nicho]", e)
        METRICS.error("ia.post")
        IA_CB.record_failure()


# ---- S6 auto-cura: compacta estruturas de estado e remove lixo
def self_heal():
    if not (apx_on and apx.get("self_heal", True)):
        return
    state["seen_uris"] = state["seen_uris"][-800:]
    state["fans_seen"] = state["fans_seen"][-500:]
    state["action_log"] = state["action_log"][-120:]
    state["reply_history"] = state["reply_history"][-40:]
    state["post_history"] = state["post_history"][-30:]
    tr = state.get("thread_replied", {})
    if len(tr) > 200:
        for k in list(tr)[:-200]:
            del tr[k]
    alts = state.get("author_last_ts", {})
    if len(alts) > 300:
        for k in list(alts)[:-300]:
            del alts[k]
    sched = state.get("sched_done", {})
    for k in list(sched):
        if k != today():
            del sched[k]
    # garante defaults novos
    for k in ("network_follows", "boosts", "blocked_dids_count"):
        state.setdefault(k, 0)
    state.setdefault("blocked_dids", [])
    state.setdefault("engagement_by_hour", {})
    state.setdefault("radar_topics", {})
    state.setdefault("tune_adj", {})


# ---- S10 thread builder: divide texto longo em thread encadeada
def split_chunks(texto, size):
    return [texto[i:i + size] for i in range(0, len(texto), size)]


def safe_thread(texto, tag):
    """Posta texto longo como thread (1º post + replies encadeadas)."""
    if not thread_cfg.get("enabled", True):
        return False
    partes = [bot_utils.fit_length(p, thread_cfg.get("max_chars", 295))
              for p in split_chunks(texto, 280) if p.strip()]
    if len(partes) < 2:
        return False
    if not budget_ok("posts", asp.get("max_posts_per_day", 10)):
        return False
    try:
        first = client.create_post(partes[0])
        bump("posts")
        record_action()
        state["post_history"].append(partes[0])
        parent_uri = (first.get("uri") or "")
        parent_cid = (first.get("cid") or "")
        for p in partes[1:]:
            sleep_human(*asp.get("pause_reply", [6, 15]))
            rep = client.reply(p, parent_uri, parent_cid, parent_uri, parent_cid)
            state["post_history"].append(p)
            parent_uri = (rep.get("uri") or "") or parent_uri
            parent_cid = (rep.get("cid") or "") or parent_cid
        state["post_history"] = state["post_history"][-30:]
        METRICS.inc("posts_ok")
        BSKY_CB.record_success()
        print(f"  [thread-{tag}] {len(partes)} posts encadeados")
        sleep_human(*asp.get("pause_block", [30, 90]))
        return True
    except RuntimeError as e:
        print(f"  [erro thread-{tag}]", e)
        METRICS.error("bsky.post")
        BSKY_CB.record_failure()
        rate_stop(e)
    return False


# ---- execução dos sistemas de leitura/escrita (respeita modo proteção)
if apx_on and bsky_ok and not block_reason:
    # S7 escudo: varre notificações em busca de perfis suspeitos
    if shield_cfg.get("enabled", True):
        try:
            n_sh = client.list_notifications(limit=50)
            scan_shield(n_sh.get("notifications", []))
            BSKY_CB.record_success()
        except RuntimeError as e:
            print("  [erro escudo-notif]", e)
            BSKY_CB.record_failure()

    if social_ok:
        # S1 rede: seguidores de quem nos segue (rede de 2º grau)
        if net_cfg.get("enabled", True) and network_done < net_cfg.get("max_per_tick", 2):
            try:
                myf = client.list_followers(client.did, limit=net_cfg.get("followers_limit", 40))
                my_followers = [f for f in myf.get("followers", []) if f.get("did") != client.did]
                srcs = my_followers[:net_cfg.get("source_limit", 3)]
                for src in srcs:
                    if rate_limited or network_done >= net_cfg.get("max_per_tick", 2):
                        break
                    if is_blocked_did(src.get("did", "")):
                        continue
                    try:
                        fr = client.list_followers(src["did"], limit=net_cfg.get("followers_limit", 40))
                        cands = [f for f in fr.get("followers", []) if f.get("did") != client.did]
                    except RuntimeError as e:
                        print("  [erro rede-followers]", e)
                        BSKY_CB.record_failure()
                        continue
                    for f in cands:
                        if rate_limited or network_done >= net_cfg.get("max_per_tick", 2):
                            break
                        if random.random() < net_cfg.get("prob", 0.35):
                            do_network_follow(f.get("did", ""), f.get("handle", "?"))
            except RuntimeError as e:
                print("  [erro rede]", e)
                BSKY_CB.record_failure()

        # S2 boost: reposta posts populares do contexto
        if boost_cfg.get("enabled", True) and boost_done < boost_cfg.get("max_per_tick", 1):
            cands = [post_info(it) for it in (trending_posts + timeline_posts)
                     if post_info(it).get("uri")]
            cands.sort(key=lambda i: i["like_count"], reverse=True)
            for info in cands:
                if rate_limited or boost_done >= boost_cfg.get("max_per_tick", 1):
                    break
                if info["like_count"] < boost_cfg.get("min_like", 3):
                    break
                if random.random() < boost_cfg.get("prob", 0.4):
                    do_boost(info)

# S4 radar + S9 auto-afinação + S6 auto-cura (leitura/estado: rodam mesmo em modo proteção)
collect_radar(trending_posts, trending_topics)
self_tune()
self_heal()


# ------------------------------------------------------------ 4) postar
def _gen_img(texto):
    """Gera imagem procedural (gradiente + texto) para acompanhar posts.

    Fallback silencioso: se PIL não estiver disponível ou falhar, retorna None
    e o post segue sem imagem (nunca quebra o fluxo de postagem).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    try:
        seed = day_seed("img:" + (texto or "")[:60])
        w, h = 1200, 675
        c1 = ((seed >> 16) & 0xFF, (seed >> 8) & 0xFF, seed & 0xFF)
        c2 = (max(0, c1[0] - 60), max(0, c1[1] - 80), min(255, c1[2] + 50))
        img = Image.new("RGB", (w, h), c1)
        dr = ImageDraw.Draw(img)
        for y in range(h):
            t = y / h
            dr.line([(0, y), (w, y)],
                    fill=(int(c1[0] * (1 - t) + c2[0] * t),
                          int(c1[1] * (1 - t) + c2[1] * t),
                          int(c1[2] * (1 - t) + c2[2] * t)))
        # ruído sutil para não parecer imagem sólida
        for _ in range(160):
            x, y = random.randrange(w), random.randrange(h)
            px = img.getpixel((x, y))
            d = random.randint(-18, 18)
            img.putpixel((x, y), (max(0, min(255, px[0] + d)),
                                  max(0, min(255, px[1] + d)),
                                  max(0, min(255, px[2] + d))))
        # texto curto central (frase do post), fonte padrão ou DejaVu
        font = None
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(path, 44)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        frase = texto.split("\n")[0].strip()[:46]
        if frase:
            dr.text((60, h - 120), frase, fill=(255, 255, 255), font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def safe_post(texto, tag, img_prob=0.0):
    if not texto:
        return
    # corte defensivo (limite do Bluesky é 300) + dedupe por chave normalizada
    texto = bot_utils.fit_length(texto, 295)
    if bot_utils.similar_to_history(texto, state["post_history"]):
        print(f"  [humano] post repetido, descartado ({tag})")
        return
    if not budget_ok("posts", asp.get("max_posts_per_day", 10)):
        return
    embed = None
    if img_prob > 0 and random.random() < img_prob:
        raw = _gen_img(texto)
        if raw:
            try:
                blob = client.upload_blob(raw, "image/png")
                if blob and blob.get("blob"):
                    embed = {
                        "$type": "app.bsky.embed.images",
                        "images": [{"alt": texto[:120], "image": blob["blob"]}],
                    }
                    print(f"  [img-{tag}] imagem anexada ao post")
            except RuntimeError as e:
                print(f"  [erro img-{tag}]", e)
                BSKY_CB.record_failure()
                rate_stop(e)
    try:
        client.create_post(texto, embed=embed)
        bump("posts")
        record_action()
        state["post_history"].append(texto)
        state["post_history"] = state["post_history"][-30:]
        METRICS.inc("posts_ok")
        BSKY_CB.record_success()
        print(f"  [post-{tag}] {texto}")
        sleep_human(*asp.get("pause_block", [30, 90]))
    except RuntimeError as e:
        print(f"  [erro post-{tag}]", e)
        METRICS.error("bsky.post")
        BSKY_CB.record_failure()
        rate_stop(e)


if not rate_limited and bsky_ok and not block_reason:
    if force_post:
        texto = os.environ.get("BSKY_FORCE_TEXT", "").strip()
        if not texto and ia_use:
            try:
                texto = ai.generate_post(history=state["post_history"])
                IA_CB.record_success()
            except Exception as e:
                print("  [erro ia]", e)
                METRICS.error("ia.post")
                IA_CB.record_failure()
        if not texto:
            texto = "🔥 Primeiro post do bot no ar! Sigam @his08.bsky.social e fiquem ligados."
        sleep_human(*asp.get("pause_pre_post", [60, 180]))
        safe_post(texto, "forcado", img_prob=img_prob)

    cur_min = time.localtime().tm_hour * 60 + time.localtime().tm_min
    jit = asp.get("schedule_jitter_min", 20)
    for s in cfg.get("schedule", []):
        if s.get("action") != "post":
            continue
        target = s["hour"] * 60 + s["minute"]
        # jitter determinístico por dia: humano não posta sempre no minuto redondo
        seed = day_seed(f"sched:{s['hour']:02d}:{s['minute']:02d}")
        target += (seed % (2 * jit + 1)) - jit
        # janela de 15 min (cron */30 pode atrasar com g4f lento) + guarda anti-duplicata
        if abs(cur_min - target) > 15:
            continue
        sched_key = f"{s['hour']:02d}:{s['minute']:02d}"
        if state.get("sched_done", {}).get(sched_key) == today():
            continue
        texto = s["content"]
        if ia_use:
            try:
                texto = ai.generate_post(history=state["post_history"])
                IA_CB.record_success()
                print("  [ia] post agendado gerado")
            except Exception as e:
                print("  [erro ia]", e, "— usando texto fixo")
                METRICS.error("ia.post")
                IA_CB.record_failure()
        sleep_human(*asp.get("pause_pre_post", [60, 180]))
        safe_post(texto, "agendado", img_prob=img_prob)
        state.setdefault("sched_done", {})[sched_key] = today()
        break

    extra_prob = auto.get("extra_post_prob", 0.65)
    extra_prob *= smart_extra_mult()   # S5 horário inteligente
    night_mult = 0.4 if is_night else 1.0
    if ia_use and random.random() < extra_prob * night_mult:
        try:
            sleep_human(*asp.get("pause_pre_post", [60, 180]))
            safe_post(ai.generate_post(history=state["post_history"]), "extra", img_prob=img_prob)
            IA_CB.record_success()
        except Exception as e:
            print("  [erro post-extra]", e)
            METRICS.error("ia.post")
            IA_CB.record_failure()

    trend_prob = auto.get("trending_post_prob", 0.35)
    if not is_night and ia_use and (trending_posts or trending_topics) and random.random() < trend_prob:
        tema = None
        if trending_topics:
            tema = random.choice(trending_topics)
        elif trending_posts:
            tema = post_info(random.choice(trending_posts))["text"]
        if tema:
            try:
                sleep_human(*asp.get("pause_pre_post", [60, 180]))
                safe_post(ai.generate_topic_post(tema, history=state["post_history"]), "trending", img_prob=img_prob)
                IA_CB.record_success()
            except Exception as e:
                print("  [erro post-trending]", e)
                METRICS.error("ia.post")
                IA_CB.record_failure()

    # S3 curador de nicho: post temático da keyword rotativa (autônomo)
    curate_niche_post()


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


if not rate_limited and bsky_ok:
    bio_prob = auto.get("bio_daily_prob", 0.0)
    if force_bio or (ia_use and not is_night and bio_prob > 0 and chance("bio", bio_prob) and state.get("bio_date") != today()):
        try:
            bio = ai.generate_bio(history=[state.get("last_bio", "")])
            if bio and safe_update_profile(description=bio):
                state["bio_date"] = today()
                state["last_bio"] = bio
                IA_CB.record_success()
                print(f"  [bio] atualizada: {bio}")
                sleep_human(2, 6)
        except Exception as e:
            print("  [erro bio]", e)
            METRICS.error("ia.bio")
            IA_CB.record_failure()

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
                mime = "image/png"
                img = b""
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

# métrica de crescimento: histórico diário de seguidores (trim 90 dias)
if bsky_ok:
    try:
        prof = client.get_profile() or {}
        fc = prof.get("followersCount") or 0
        fh = state.setdefault("followers_history", {})
        fh[today()] = fc
        keys = sorted(fh.keys())[-90:]
        state["followers_history"] = {k: fh[k] for k in keys}
        print(f"[métrica] seguidores hoje: {fc} (histórico {len(state['followers_history'])} dias)")
    except RuntimeError as e:
        print("  [erro métrica-followers]", e)

# persistência da camada de operações (observabilidade / breakers / incidents / tuning)
state["metrics"] = METRICS.to_dict()
state["cb_bsky"] = BSKY_CB.to_dict()
state["cb_ia"] = IA_CB.to_dict()
state["cb_gh"] = GH_CB.to_dict()
state["incidents"] = INCIDENTS.to_dict()
if OPS_ADJ:
    th = state.setdefault("tuning_history", [])
    th.append({"ts": bops.now_iso(),
               "reason": "cauteloso" if OPS_REPORT.get("cautious") else "saudavel",
               "ajustes": OPS_ADJ})
    state["tuning_history"] = th[-30:]
print("[i] tick concluído.")
sys.exit(FINAL_EXIT)
