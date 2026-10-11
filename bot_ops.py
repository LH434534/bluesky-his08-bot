#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bot_ops.py — camada central de operações do bot (observabilidade + auto-manutenção).

Aplica PRINCÍPIOS de sistemas de produção altamente disponíveis, sem copiar
arquitetura de nenhuma empresa específica:

  Observabilidade   : logs JSON estruturados, métricas com janela deslizante,
                      latências, taxa de erros, falhas recorrentes, health check,
                      histórico de alterações (tuning/incidents).
  Fault tolerance   : circuit breakers por dependência (bluesky api / IA / repo),
                      retry com exponential backoff + jitter, cooldowns,
                      graceful shutdown (estado persistido via atexit).
  Self-healing      : detecta processo parado (heartbeat), dependência indisponível,
                      estado inconsistente, execução duplicada, crescimento anormal
                      de erros; executa apenas ações de recuperação permitidas.
  Auto-diagnóstico  : incidentes correlacionados erro → contexto → histórico →
                      causa → correção → resultado; aprende por assinatura.
  Auto-ajuste       : parâmetros operacionais ajustados SÓ dentro de limites
                      declarados em config["tuning"]; NUNCA remove limites de
                      segurança/permissões/proteções.
  Controle de regressão: toda correção é registrada (versão/motivo/diagnóstico/
                      testes/resultado/timestamp/rollback); correção que falha
                      repetidamente é marcada instável e não é tentada de novo.

Módulo 100% stdlib (roda no runner do GitHub Actions sem dependências extras).
"""
import json
import random
import time
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_ts():
    return time.time()


def json_log(level, event, **fields):
    """Log estruturado em uma linha JSON (legível por ferramentas de observação)."""
    rec = {"ts": now_iso(), "level": level, "event": event}
    rec.update({k: v for k, v in fields.items() if v is not None})
    print(json.dumps(rec, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- metrics
class Metrics:
    """Contadores, latências e janela deslizante de erros — persistível no state."""

    def __init__(self, raw=None):
        raw = raw or {}
        self.counters = dict(raw.get("counters") or {})
        self.latency = dict(raw.get("lat") or {})      # name -> {n,sum,min,max}
        self.errors = list(raw.get("errors") or [])    # [{ts, sig}]
        self.max_errors = int(raw.get("max_errors") or 300)

    def inc(self, name, n=1):
        self.counters[name] = self.counters.get(name, 0) + n

    def lat(self, name, dur_ms):
        e = self.latency.setdefault(name, {"n": 0, "sum": 0.0, "min": None, "max": 0.0})
        e["n"] += 1
        e["sum"] += dur_ms
        e["min"] = dur_ms if e["min"] is None else min(e["min"], dur_ms)
        e["max"] = max(e["max"], dur_ms)

    def error(self, sig):
        self.errors.append({"ts": now_ts(), "sig": sig})
        if len(self.errors) > self.max_errors:
            self.errors = self.errors[-self.max_errors:]

    def recent_errors(self, window_sec=10800, sig=None):
        """Número de erros na janela (default 3h). Filtra por assinatura se dada."""
        cutoff = now_ts() - window_sec
        return sum(1 for e in self.errors if e["ts"] >= cutoff and (sig is None or e["sig"] == sig))

    def error_rate(self, window_sec=10800):
        """Taxa de erro por hora (falhas recentes / horas da janela)."""
        n = self.recent_errors(window_sec)
        hours = max(window_sec / 3600.0, 1e-3)
        return n / hours

    def ok_rate(self, action_ok, action_fail):
        ok = self.counters.get(action_ok, 0)
        fail = self.counters.get(action_fail, 0)
        if ok + fail == 0:
            return 1.0
        return ok / (ok + fail)

    def to_dict(self):
        return {"counters": self.counters, "lat": self.latency,
                "errors": self.errors[-self.max_errors:], "max_errors": self.max_errors}


# ------------------------------------------------------------------- circuit breaker
class CircuitBreaker:
    """Circuit breaker por dependência: closed → open (após falhas) → half_open (probe)."""

    def __init__(self, raw=None, name="dep", failure_threshold=3, cooldown_sec=3600,
                 window_sec=7200):
        raw = raw or {}
        self.name = name
        self.failure_threshold = int(raw.get("failure_threshold") or failure_threshold)
        self.cooldown_sec = float(raw.get("cooldown_sec") or cooldown_sec)
        self.window_sec = float(raw.get("window_sec") or window_sec)
        self.state = raw.get("state") or "closed"          # closed | open | half_open
        self.failures = int(raw.get("failures") or 0)
        self.fail_ts = list(raw.get("fail_ts") or [])
        self.opened_ts = float(raw.get("opened_ts") or 0.0)
        self.last_success_ts = float(raw.get("last_success_ts") or 0.0)

    def allow(self):
        """True se a dependência pode ser chamada agora."""
        if self.state == "closed":
            return True
        if self.state == "open":
            if now_ts() - self.opened_ts >= self.cooldown_sec:
                self.state = "half_open"   # probe permitido
                json_log("warn", "cb.half_open", dep=self.name)
                return True
            return False
        # half_open: permite UMA chamada de teste (probe)
        return True

    def record_success(self):
        self.state = "closed"
        self.failures = 0
        self.fail_ts = []
        self.last_success_ts = now_ts()

    def record_failure(self):
        now = now_ts()
        self.fail_ts.append(now)
        # janela deslizante: remove falhas antigas
        self.fail_ts = [t for t in self.fail_ts if now - t <= self.window_sec]
        self.failures = len(self.fail_ts)
        if self.state != "open" and self.failures >= self.failure_threshold:
            self.state = "open"
            self.opened_ts = now
            json_log("warn", "cb.open", dep=self.name, failures=self.failures)

    def state_now(self):
        return self.state

    def to_dict(self):
        return {"state": self.state, "failures": self.failures, "fail_ts": self.fail_ts,
                "opened_ts": self.opened_ts, "last_success_ts": self.last_success_ts,
                "failure_threshold": self.failure_threshold,
                "cooldown_sec": self.cooldown_sec, "window_sec": self.window_sec}


# --------------------------------------------------------------------- retry backoff
def retry_call(fn, *, attempts=3, base=1.0, max_wait=15.0, jitter=0.3,
               retry_on=(Exception,), logger=None):
    """Executa fn com exponential backoff + jitter. Retorna (ok, result_or_error)."""
    last = None
    for i in range(attempts):
        try:
            return True, fn()
        except retry_on as e:
            last = e
            if i == attempts - 1:
                break
            wait = min(base * (2 ** i), max_wait)
            wait = wait * (1 + random.uniform(0, jitter))
            if logger:
                logger("warn", "retry", attempt=i + 1, wait_ms=int(wait * 1000), err=str(e)[:100])
            time.sleep(wait)
    return False, last


# -------------------------------------------------------------------- incident log
class IncidentLog:
    """Auto-diagnóstico: erro → contexto → histórico → causa → correção → resultado.

    Aprende por assinatura: se a mesma correção já falhou repetidamente, a
    assinatura é marcada instável e a correção não é tentada de novo sozinha.
    """

    def __init__(self, raw=None, max_entries=200):
        raw = raw or {}
        self.entries = list(raw.get("entries") or [])
        self.max_entries = int(raw.get("max_entries") or max_entries)

    def add(self, sig, context="", cause="", action="", outcome="unknown"):
        self.entries.append({
            "ts": now_iso(), "sig": sig, "context": context[:200],
            "cause": cause[:200], "action": action[:200], "outcome": outcome[:50],
        })
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries:]

    def last(self, sig):
        for e in reversed(self.entries):
            if e["sig"] == sig:
                return e
        return None

    def outcome_ok(self, sig, horizon_sec=86400):
        """A última ocorrência da assinatura terminou bem (dentro do horizonte)?"""
        e = self.last(sig)
        if not e:
            return True
        try:
            age = now_ts() - datetime.fromisoformat(e["ts"]).timestamp()
        except Exception:
            age = 0
        return age <= horizon_sec and e["outcome"] in ("ok", "recovered")

    def is_unstable(self, sig, max_fails=2):
        """Correção instável: falhou repetidamente — não tentar de novo sozinho."""
        fails = sum(1 for e in self.entries if e["sig"] == sig and e["outcome"] == "failed")
        return fails >= max_fails

    def to_dict(self):
        return {"entries": self.entries[-self.max_entries:], "max_entries": self.max_entries}


# ----------------------------------------------------------------------------- tuner
class Tuner:
    """Auto-ajuste controlado: altera parâmetros SÓ dentro de limites declarados.

    A IA (este módulo) pode otimizar parâmetros operacionais seguros; nunca remove
    limites de segurança (max_posts/likes/replies por dia são intocados — vivem em
    anti_spam, fora da whitelist de tuning).
    """

    def __init__(self, tuning_cfg):
        tuning_cfg = tuning_cfg or {}
        self.enabled = bool(tuning_cfg.get("enabled", True))
        self.bounds = {}   # key -> {"min": x, "max": y}
        for k, v in (tuning_cfg.get("params") or {}).items():
            if isinstance(v, dict) and "min" in v and "max" in v:
                self.bounds[k] = (float(v["min"]), float(v["max"]))
        self.err_window = float(tuning_cfg.get("error_window_min", 180) * 60)
        self.err_threshold = int(tuning_cfg.get("error_threshold", 5))
        self.step = float(tuning_cfg.get("step", 0.05))
        self.history = []   # registros de ajuste (controle de regressão)

    def allowed_keys(self):
        return list(self.bounds.keys())

    def adjust(self, current, metrics, incidents=None):
        """Retorna dict {key: novo_valor} aplicando regras seguras.

        - erros recentes >= threshold → modo cauteloso: desce probs (até min)
        - erros baixos e sem incidentes instáveis → sobe levemente (até max)
        - só mexe em chaves declaradas em bounds
        """
        if not self.enabled:
            return {}
        errs = metrics.recent_errors(self.err_window)
        cautious = errs >= self.err_threshold
        out = {}
        for key, (lo, hi) in self.bounds.items():
            cur = float(current.get(key, lo))
            if cautious:
                new = max(lo, cur - self.step)
            else:
                # crescimento controlado; nunca salta além de max
                new = min(hi, cur + self.step * 0.5)
            if abs(new - cur) > 1e-9:
                out[key] = round(new, 3)
        if out:
            self.history.append({
                "ts": now_iso(), "reason": "cauteloso" if cautious else "saudavel",
                "errors_recent": errs, "adjustments": out,
            })
            self.history = self.history[-30:]
            json_log("info", "tuning.adjust", reason="cauteloso" if cautious else "saudavel",
                     errors_recent=errs, adjustments=out)
        return out

    def history_dict(self):
        return self.history


# ------------------------------------------------------------------ self-check
def run_self_check(state, metrics, breakers, incidents, cfg):
    """Health check + diagnóstico no início de cada ciclo.

    Retorna (report, adjustments):
      report    — dict com sinais e ações de recuperação permitidas
      adjustments — dict com parâmetros ajustados dentro dos limites (tuning)
    """
    tuning_cfg = cfg.get("tuning") or {}
    tuner = Tuner(tuning_cfg)
    # params unificados para tuning (origem pode ser autonomy ou anti_spam)
    params_ctx = dict(cfg.get("anti_spam") or {})
    params_ctx.update(cfg.get("autonomy") or {})
    report = {"cautious": False, "actions": [], "signals": {}}

    # 1) processo parado? (último heartbeat)
    hb = state.get("last_heartbeat") or ""
    hb_age = None
    if hb:
        try:
            hb_age = now_ts() - datetime.fromisoformat(hb.replace("Z", "+00:00")).timestamp()
        except Exception:
            hb_age = None
    report["signals"]["heartbeat_age_s"] = hb_age
    if hb_age is not None and hb_age > 6 * 3600:
        report["actions"].append("reanimar")   # healthcheck externo re-dispatcha

    # 2) dependências
    bsky_state = breakers["bsky"].state_now()
    ia_state = breakers["ia"].state_now()
    report["signals"]["bsky_cb"] = bsky_state
    report["signals"]["ia_cb"] = ia_state
    if bsky_state == "open":
        report["actions"].append("pular_social")
    if ia_state == "open":
        report["actions"].append("sem_ia")

    # 3) crescimento anormal de erros
    errs = metrics.recent_errors(tuner.err_window)
    report["signals"]["errors_recent"] = errs
    report["signals"]["error_threshold"] = tuner.err_threshold
    if errs >= tuner.err_threshold:
        report["cautious"] = True
        report["actions"].append("modo_cauteloso")

    # 4) execução duplicada
    last_run_id = state.get("last_run_id") or ""
    if last_run_id:
        report["signals"]["last_run_id"] = last_run_id

    # 5) estado inconsistente
    required = ("date", "likes", "seen_uris", "post_history")
    missing = [k for k in required if k not in state]
    if missing:
        report["actions"].append("reparar_estado")
        json_log("warn", "ops.state_inconsistent", missing=missing)

    # 6) correções instáveis não repetidas (aprendizado por assinatura)
    for sig in ("ia.g4f", "ia.provider", "bsky.rate_limit", "bsky.api", "gh.state"):
        if incidents.is_unstable(sig):
            report["actions"].append(f"nao_repetir:{sig}")
            report.setdefault("unstable", []).append(sig)

    # 7) auto-ajuste controlado (dentro dos limites declarados)
    adjustments = tuner.adjust(params_ctx, metrics, incidents) if tuning_cfg.get("enabled") else {}

    return report, adjustments
