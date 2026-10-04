#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""healthcheck.py — vigilância externa do bot (self-healing).

Executado por healthcheck.yml (GitHub Actions) a cada 2h + manual.

O que faz:
  1. Lê state.json do repo (fonte da verdade).
  2. Se last_heartbeat estiver ausente OU mais velho que 6h → o bot morreu:
     re-dispatcha imediatamente o workflow principal (reanimação) e registra
     incidente de disponibilidade.
  3. Se heartbeat recente → registra "saudável" (métrica de disponibilidade).
  4. Grava health_history.json (máx. 200) com merge em 409 (corrida segura),
     formando o histórico de disponibilidade/recuperações.
  5. Exit 0 sempre (job verde no Actions; o log é a fonte de observação).
"""
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
BOT_WORKFLOW_ID = os.environ.get("BSKY_WORKFLOW_ID", "373662044")
HEARTBEAT_MAX_AGE_S = int(os.environ.get("BSKY_HEARTBEAT_MAX_AGE_S", "21600"))  # 6h
HISTORY_LIMIT = int(os.environ.get("BSKY_HEALTH_HISTORY_LIMIT", "200"))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def gh(method, path, payload=None):
    url = "https://api.github.com" + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "marvis-bot-healthcheck")
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


def read_state():
    code, resp = gh("GET", f"/repos/{REPO}/contents/state.json")
    if code != 200:
        print(f"[health] state.json inacessível ({code}) — não é possível julgar")
        return None
    try:
        raw = base64.b64decode((resp.get("content") or "").replace("\n", ""))
        return json.loads(raw)
    except Exception as e:
        print("[health] state.json corrompido:", e)
        return None


def heartbeat_age(state):
    hb = (state or {}).get("last_heartbeat") or ""
    if not hb:
        return None
    try:
        dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        return time.time() - dt.timestamp()
    except Exception:
        return None


def dispatch_bot():
    code, resp = gh("POST", f"/repos/{REPO}/actions/workflows/{BOT_WORKFLOW_ID}/dispatches",
                    {"ref": "main"})
    if code in (202, 204):
        print("[health] workflow do bot RE-DISPATCHADO (reanimação)")
        return True
    print(f"[health] falha ao re-dispatchar: {code} {resp}")
    return False


def append_history(rec):
    """Merge de health_history.json com retry em 409 (evita corrida de execuções)."""
    content = None
    for attempt in range(3):
        code, existing = gh("GET", f"/repos/{REPO}/contents/health_history.json")
        history = []
        if code == 200:
            try:
                raw = base64.b64decode((existing.get("content") or "").replace("\n", ""))
                history = json.loads(raw)
            except Exception:
                history = []
        history.append(rec)
        history = history[-HISTORY_LIMIT:]
        content = base64.b64encode(json.dumps(history, ensure_ascii=False).encode()).decode()
        payload = {"message": "health history", "content": content,
                   "sha": existing.get("sha") if code == 200 else None}
        put_code, put_resp = gh("PUT", f"/repos/{REPO}/contents/health_history.json", payload)
        if put_code in (200, 201):
            print(f"[health] health_history.json atualizado ({len(history)} registros)")
            return True
        if put_code == 409 and attempt < 2:
            print(f"[health] 409 (tentativa {attempt + 1}) — re-lendo")
            continue
        print(f"[health] falha ao gravar histórico: {put_code}")
        return False


def main():
    if not (REPO and TOKEN):
        print("[health] sem GITHUB_REPOSITORY/GITHUB_TOKEN — abortando")
        sys.exit(1)
    state = read_state()
    age = heartbeat_age(state)
    rec = {"ts": now_iso(), "heartbeat_age_s": age,
           "last_run_id": (state or {}).get("last_run_id", ""),
           "status": "?", "action": "none"}
    if age is None:
        rec["status"] = "sem_heartbeat"
        print("[health] sem heartbeat no estado — bot provavelmente nunca rodou ou morreu")
        ok = dispatch_bot()
        rec["action"] = "reanimar" if ok else "reanimar_falhou"
    elif age > HEARTBEAT_MAX_AGE_S:
        rec["status"] = "morto"
        print(f"[health] heartbeat com {int(age)}s (> {HEARTBEAT_MAX_AGE_S}s) — bot parado")
        ok = dispatch_bot()
        rec["action"] = "reanimar" if ok else "reanimar_falhou"
    else:
        rec["status"] = "saudavel"
        print(f"[health] heartbeat com {int(age)}s — bot saudável")
    append_history(rec)
    print("[health] healthcheck concluído.")
    sys.exit(0)


if __name__ == "__main__":
    main()
