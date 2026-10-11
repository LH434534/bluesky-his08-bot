#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""healthcheck.py — vigilante AUTÔNOMO remoto (roda no GitHub Actions).

Diferente de um automático (que só segue rotina), este script DECIDE:
  - último run do bot terminou em failure -> REANIMA com re-dispatch (cooldown anti-loop)
  - nenhum run concluído nas últimas 3h -> REANIMA (mesmo se heartbeat local parecer vivo)
  - heartbeat local morto (>6h) -> REANIMA
  - tudo saudável -> apenas registra
Histórico persistido em health_history.json (merge 409-tolerant).
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Token vem SEMPRE do ambiente (workflow injeta secrets.GH_PAT).
# NUNCA colocar fallback hardcoded aqui — repo é público e vazar PAT é risco grave.
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = "LH434534/bluesky-his08-bot"
WF_ID = os.environ.get("BSKY_WORKFLOW_ID", "373662044")
STATE_FILE = "state.json"
HIST_FILE = "health_history.json"

HEARTBEAT_STALE_S = 6 * 3600
RUNS_STALE_S = 3 * 3600
REANIM_COOLDOWN_S = 60 * 60

TZ = time.strftime("%Z")


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')} {TZ}] {msg}", flush=True)


def gh(method, path, payload=None):
    if not TOKEN:
        return 0, {"error": "GITHUB_TOKEN_ausente"}
    url = f"https://api.github.com/repos/{REPO}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "marvis-healthcheck-autonomo")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        return e.code, (json.loads(body) if body else {})
    except Exception as e:
        return 0, {"error": repr(e)}


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def dispatch():
    code, resp = gh("POST", f"actions/workflows/{WF_ID}/dispatches", {"ref": "main"})
    return code == 204


def append_history(entry):
    hist = read_json(HIST_FILE)
    if not isinstance(hist, list):
        hist = []
    hist.append(entry)
    hist = hist[-200:]
    try:
        write_json(HIST_FILE, hist)
    except Exception as e:
        log(f"erro persistindo histórico: {e}")


def main():
    log("vigilante autônomo iniciado")
    hist = read_json(HIST_FILE)
    if not isinstance(hist, list):
        hist = []
    last_reanim = 0.0
    for e in reversed(hist):
        if e.get("action") == "reanimar" and e.get("ts"):
            try:
                last_reanim = datetime.fromisoformat(e["ts"]).timestamp()
                break
            except Exception:
                pass
    now = time.time()

    # 1) estado do último run do BOT (fonte primária de verdade)
    #    filtra pelo workflow do bot — não pode ver o próprio run do healthcheck
    state = {}
    reason = None
    code, resp = gh("GET", "actions/workflows/workflow.yml/runs?per_page=5")
    if code == 200:
        runs = resp.get("workflow_runs", [])
        if runs:
            newest = runs[0]
            status, concl = newest["status"], newest.get("conclusion")
            try:
                created = datetime.fromisoformat(newest["created_at"].replace("Z", "+00:00")).timestamp()
            except Exception:
                created = now
            age = now - created
            state = {"run_id": newest["id"], "status": status,
                     "conclusion": concl, "run_age_s": int(age)}
            if status == "completed" and concl == "failure":
                reason = f"ultimo_run_failure({newest['id']},ha_{int(age)}s)"
            elif age > RUNS_STALE_S:
                reason = f"sem_run_recente({int(age)}s)"
            elif status in ("in_progress", "queued"):
                reason = "run_em_andamento"
            else:
                reason = f"saudavel(run_{newest['id']},ha_{int(age)}s)"
        else:
            reason = "sem_runs"
    else:
        state = {"api_error": code}
        reason = f"api_runs_{code}"

    # 2) heartbeat local (informação auxiliar)
    local = read_json(STATE_FILE)
    hb_ts = local.get("last_heartbeat") or local.get("ts")
    hb_stale = False
    if hb_ts:
        try:
            hb_time = datetime.fromisoformat(str(hb_ts).replace("Z", "+00:00")).timestamp()
            hb_stale = (now - hb_time) > HEARTBEAT_STALE_S
        except Exception:
            pass

    # 3) decisão autônoma
    action = "monitorar"
    cooldown_left = int(REANIM_COOLDOWN_S - (now - last_reanim))
    if (reason and reason.startswith(("ultimo_run_failure", "sem_run_recente", "sem_runs"))) or hb_stale:
        if cooldown_left > 0:
            action = "cooldown"
            log(f"[decisão] {reason} quer reanimar, mas cooldown {cooldown_left}s restante")
        else:
            action = "reanimar"
            log(f"[decisão] REANIMAR — {reason}, heartbeat_local_stale={hb_stale}")
            ok = dispatch()
            log(f"[reanimação] dispatch={'OK' if ok else 'FALHOU'}")
            append_history({"ts": datetime.now(timezone.utc).isoformat(),
                            "action": "reanimar", "ok": ok, "motivo": reason})
    else:
        log(f"[decisão] {reason} — nada a fazer")
    append_history({"ts": datetime.now(timezone.utc).isoformat(),
                    "action": action, "motivo": reason, "state": state,
                    "heartbeat_stale": hb_stale})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"[erro fatal] {e!r}")
        sys.exit(1)
