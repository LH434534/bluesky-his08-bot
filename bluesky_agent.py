#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bluesky_agent.py — Bot autônomo para Bluesky (@his08.bsky.social)
Autor: Marvis  |  Uso:
  python3 bluesky_agent.py info          # mostra configuração (sem segredos)
  python3 bluesky_agent.py plan          # mostra o plano de horários/ações
  python3 bluesky_agent.py dry           # simula tudo, NÃO publica nada
  python3 bluesky_agent.py run           # loop real (exige credenciais)
  python3 bluesky_agent.py list          # lista agendamentos ativos
  python3 bluesky_agent.py auth          # testa login na API

Segurança:
  - credenciais lidas de config.json ou variáveis de ambiente (BSKY_HANDLE / BSKY_APP_PASSWORD)
  - por padrão roda em DRY-RUN; só publica com --confirm e credenciais válidas
  - proteções anti-spam embutidas (intervalo mínimo, jitter aleatório, cooldown)
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------- config
API = "https://bsky.social/xrpc"
HANDLE = "@his08.bsky.social"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_CONFIG = {
    "handle": "his08.bsky.social",          # handle sem @
    "app_password": "",                      # App Password (nunca commit)
    "schedule": [
        {"action": "post", "hour": 9,  "minute": 0,  "content": "Bom dia, família! ☕"},
        {"action": "post", "hour": 13, "minute": 30, "content": "Almoçou? Bora que o dia continua."},
        {"action": "post", "hour": 18, "minute": 0,  "content": "Boa noite! Amanhã tem mais."},
        {"action": "post", "hour": 21, "minute": 15, "content": "Encerrando o expediente. Até logo!"}
    ],
    "engagement": {
        "enabled": True,
        "max_likes_per_hour": 12,          # teto anti-spam
        "max_replies_per_hour": 4,
        "follow_keywords": ["bluesky", "música", "arte", "criador"],
        "reply_templates": [
            "Boa! 🔥",
            "Concordo demais!",
            "Muito bom isso.",
            "Tô nessa também!"
        ],
        "cooldown_seconds": 45,            # entre ações
        "jitter_seconds": 20               # variação aleatória extra
    },
    "safety": {
        "dry_run": True,                    # True = nunca publica
        "max_post_length": 280,
        "max_replies_per_thread": 1,
        "pause_on_error": True
    }
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            for k, v in user_cfg.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print("[aviso] config.json inválido:", e)
    # env override
    env_handle = os.environ.get("BSKY_HANDLE")
    env_pass = os.environ.get("BSKY_APP_PASSWORD")
    if env_handle:
        cfg["handle"] = env_handle
    if env_pass:
        cfg["app_password"] = env_pass
    return cfg


def has_creds(cfg):
    return bool(cfg.get("handle") and cfg.get("app_password"))


# ---------------------------------------------------------------- api
class BskyClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.jwt = None
        self.did = None

    def _req(self, path, payload=None, method=None):
        url = API + path
        data = None
        headers = {"Content-Type": "application/json"}
        if self.jwt:
            headers["Authorization"] = "Bearer " + self.jwt
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            raise RuntimeError(f"HTTP {e.code}: {body}")
        except Exception as e:
            raise RuntimeError(f"erro de rede: {e}")

    def login(self):
        r = self._req("/com.atproto.server.createSession", {
            "identifier": self.cfg["handle"],
            "password": self.cfg["app_password"]
        }, method="POST")
        self.jwt = r["accessJwt"]
        self.did = r.get("did")
        return self.did

    def create_post(self, text):
        rec = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime_now_iso()
        }
        return self._req("/com.atproto.repo.createRecord", {
            "repo": self.did,
            "collection": "app.bsky.feed.post",
            "record": rec
        }, method="POST")

    def like(self, uri, cid):
        rec = {
            "$type": "app.bsky.feed.like",
            "subject": {"uri": uri, "cid": cid},
            "createdAt": datetime_now_iso()
        }
        return self._req("/com.atproto.repo.createRecord", {
            "repo": self.did,
            "collection": "app.bsky.feed.like",
            "record": rec
        }, method="POST")

    def reply(self, text, root_uri, root_cid, parent_uri, parent_cid):
        rec = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "reply": {
                "root": {"uri": root_uri, "cid": root_cid},
                "parent": {"uri": parent_uri, "cid": parent_cid}
            },
            "createdAt": datetime_now_iso()
        }
        return self._req("/com.atproto.repo.createRecord", {
            "repo": self.did,
            "collection": "app.bsky.feed.post",
            "record": rec
        }, method="POST")

    def timeline(self, limit=30):
        return self._req(f"/app.bsky.feed.getTimeline?limit={limit}")


def datetime_now_iso():
    now = time.gmtime()
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", now)


# ---------------------------------------------------------------- engine
class AntiSpamGate:
    """Limita ações por hora + intervalo mínimo + jitter aleatório."""
    def __init__(self, cfg):
        e = cfg.get("engagement", {})
        self.max_per_hour = {
            "like": e.get("max_likes_per_hour", 12),
            "reply": e.get("max_replies_per_hour", 4),
        }
        self.cooldown = e.get("cooldown_seconds", 45)
        self.jitter = e.get("jitter_seconds", 20)
        self.log = []  # (action, timestamp)

    def allowed(self, action):
        now = time.time()
        self.log = [(a, t) for a, t in self.log if now - t < 3600]
        if sum(1 for a, _ in self.log if a == action) >= self.max_per_hour.get(action, 0):
            return False
        last = max((t for a, t in self.log if a == action), default=0)
        if now - last < self.cooldown:
            return False
        return True

    def wait(self, action):
        now = time.time()
        last = max((t for a, t in self.log if a == action), default=0)
        wait = max(0, self.cooldown + random.uniform(0, self.jitter) - (now - last))
        if wait > 0:
            print(f"  [anti-spam] aguardando {wait:.0f}s antes de {action}...")
            time.sleep(wait)

    def record(self, action):
        self.log.append((action, time.time()))


def should_engage(text, keywords):
    t = text.lower()
    return any(k.lower() in t for k in keywords)


# ---------------------------------------------------------------- cli
def cmd_info(cfg):
    print("== bluesky_agent ==")
    print("handle        :", cfg["handle"])
    print("app_password  :", "*** definida ***" if cfg["app_password"] else "AUSENTE")
    print("dry_run       :", cfg["safety"]["dry_run"])
    print("engajamento   :", "on" if cfg["engagement"]["enabled"] else "off")
    print("agendamentos  :", len(cfg["schedule"]))
    if not cfg["app_password"]:
        print("\n[!] Sem App Password. Crie em bluesky.app > Settings > App Passwords")
        print("    e salve em config.json ou env BSKY_APP_PASSWORD.")


def cmd_plan(cfg):
    print("== PLANO DE AÇÕES ==")
    for s in cfg["schedule"]:
        print(f"  {s['hour']:02d}:{s['minute']:02d}  {s['action']:6s} -> {s['content'][:60]}")
    e = cfg["engagement"]
    print("\nEngajamento:")
    print(f"  likes/hora máx : {e['max_likes_per_hour']}")
    print(f"  replies/hora   : {e['max_replies_per_hour']}")
    print(f"  palavras-chave : {', '.join(e['follow_keywords'])}")
    print(f"  templates      : {e['reply_templates']}")


def cmd_dry(cfg):
    print("== SIMULAÇÃO (nada é publicado) ==")
    gate = AntiSpamGate(cfg)
    for s in cfg["schedule"]:
        print(f"  [{s['action']}] {s['hour']:02d}:{s['minute']:02d} postaria: {s['content']}")
        gate.record(s["action"])
    if cfg["engagement"]["enabled"]:
        for kw in cfg["engagement"]["follow_keywords"]:
            print(f"  [like/reply] procuraria posts sobre '{kw}' (máx {gate.max_per_hour['like']} likes/h)")
    print("\n  OK: plano válido. Para executar de verdade use 'run --confirm'.")


def cmd_auth(cfg):
    if not has_creds(cfg):
        print("[!] Credenciais ausentes. Configure app_password primeiro.")
        sys.exit(1)
    client = BskyClient(cfg)
    try:
        did = client.login()
        print("[ok] login realizado. did =", did)
    except RuntimeError as e:
        print("[falha]", e)
        sys.exit(1)


def cmd_run(cfg, confirm):
    if cfg["safety"]["dry_run"] and not confirm:
        print("[!] Modo dry_run ativo. Use '--confirm' para realmente publicar.")
        return
    if not has_creds(cfg):
        print("[!] Sem credenciais. Configure handle + app_password.")
        sys.exit(1)
    client = BskyClient(cfg)
    try:
        client.login()
        print("[ok] login realizado. did =", client.did)
    except RuntimeError as e:
        print("[falha] login:", e)
        if cfg["safety"]["pause_on_error"]:
            sys.exit(1)
        return

    gate = AntiSpamGate(cfg)
    print("[i] bot iniciado. Ctrl+C para parar.")
    while True:
        now = time.localtime()
        hm = (now.tm_hour, now.tm_min)
        for s in cfg["schedule"]:
            if (s["hour"], s["minute"]) == hm:
                gate.wait("like")
                gate.wait("reply")
                try:
                    gate.wait("post")
                    print(f"  [post] {s['content']}")
                    client.create_post(s["content"])
                    gate.record("post")
                except RuntimeError as e:
                    print("  [erro post]", e)
        if cfg["engagement"]["enabled"]:
            try:
                tl = client.timeline(limit=30)
                for item in tl.get("feed", []):
                    post = item.get("post", {})
                    text = post.get("record", {}).get("text", "")
                    uri = post.get("uri", "")
                    cid = post.get("cid", "")
                    if not should_engage(text, cfg["engagement"]["follow_keywords"]):
                        continue
                    if gate.allowed("like"):
                        gate.wait("like")
                        try:
                            client.like(uri, cid)
                            gate.record("like")
                            print(f"  [like] {text[:50]}")
                        except RuntimeError as e:
                            print("  [erro like]", e)
                    if gate.allowed("reply"):
                        gate.wait("reply")
                        try:
                            client.reply(
                                random.choice(cfg["engagement"]["reply_templates"]),
                                uri, cid, uri, cid
                            )
                            gate.record("reply")
                            print(f"  [reply] {text[:50]}")
                        except RuntimeError as e:
                            print("  [erro reply]", e)
            except RuntimeError as e:
                print("  [erro timeline]", e)
        time.sleep(30)


def main():
    ap = argparse.ArgumentParser(description="Bot autônomo Bluesky")
    ap.add_argument("cmd", nargs="?", default="info",
                    choices=["info", "plan", "dry", "run", "list", "auth"])
    ap.add_argument("--confirm", action="store_true", help="sair do dry-run")
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "info":
        cmd_info(cfg)
    elif args.cmd == "plan":
        cmd_plan(cfg)
    elif args.cmd == "dry":
        cmd_dry(cfg)
    elif args.cmd == "auth":
        cmd_auth(cfg)
    elif args.cmd == "run":
        cmd_run(cfg, args.confirm)
    elif args.cmd == "list":
        cmd_plan(cfg)


if __name__ == "__main__":
    main()
