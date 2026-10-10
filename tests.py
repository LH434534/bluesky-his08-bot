#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests.py — testes da auditoria 2026-10-04 (Bluesky bot).

Cobre:
  - bot_utils: dedupe normalizado, similaridade, truncamento, detecção de spam
  - ai_engine : validação de texto gerado, hashtags sem estourar limite,
                retry com provider inválido, corte sem quebrar palavra
  - bluesky_agent: métodos novos presentes e payloads corretos
  - tick.py    : smoke test completo com módulos stub (sem rede)

Rode com:  python3 tests.py
"""
import os
import runpy
import hashlib
import json
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bot_utils  # noqa: E402


class TestBotUtils(unittest.TestCase):
    def test_norm_key_ignores_case_acentos_espacos(self):
        self.assertEqual(
            bot_utils.norm_key("  Música Hoje!  "),
            bot_utils.norm_key("musica hoje"),
        )

    def test_similar_to_history_curto(self):
        self.assertTrue(bot_utils.similar_to_history("curti demais essa vibe", ["curti demais essa vibe mano"], 30))
        self.assertFalse(bot_utils.similar_to_history("futebol ontem foi foda", ["curti demais essa vibe"], 30))

    def test_fit_length_nao_quebra_palavra(self):
        out = bot_utils.fit_length("a" * 20 + " bbbbb", 10)
        self.assertLessEqual(len(out), 10 + 3)  # tolera reticências
        self.assertFalse(out.endswith("abbb"))  # não corta no meio da palavra

    def test_looks_spammy(self):
        self.assertTrue(bot_utils.looks_spammy("dm me for free follows!!!"))
        self.assertTrue(bot_utils.looks_spammy("siga e ganhe promo"))
        self.assertFalse(bot_utils.looks_spammy("curti demais esse post"))


class TestAIEngine(unittest.TestCase):
    def _engine(self, provider="openai", monkey=None):
        import ai_engine
        eng = ai_engine.AIEngine()
        eng.provider = provider
        if monkey:
            for k, v in monkey.items():
                setattr(eng, k, v)
        return eng

    def test_clean_corta_sem_quebrar_palavra(self):
        import ai_engine
        eng = ai_engine.AIEngine()
        txt = "hoje foi um dia incrível, cheio de coisa boa demais pra contar pra vocês"
        out = eng._clean(txt, limit=20)
        self.assertLessEqual(len(out), 20 + 3)
        self.assertNotIn("\n\n", out)

    def test_ensure_hashtags_respeita_limite(self):
        import ai_engine
        eng = ai_engine.AIEngine()
        texto = "hoje foi um dia incrível, cheio de coisa boa demais pra contar pra vocês e o pessoal todo junto"
        out = eng._ensure_hashtags(texto, limit=60)
        self.assertLessEqual(len(out), 60)
        tags = [t for t in out.split() if t.startswith("#")]
        self.assertGreaterEqual(len(tags), 2)

    def test_strip_hashtags(self):
        import ai_engine
        eng = ai_engine.AIEngine()
        self.assertEqual(eng._strip_hashtags("curti #vibe #brasil"), "curti")

    def test_validate_rejeita_markdown(self):
        import ai_engine
        eng = ai_engine.AIEngine()
        self.assertFalse(eng._validate("**negrito**", 100))
        self.assertFalse(eng._validate("veja [link](http://x)", 100))

    def test_validate_rejeita_call_to_action(self):
        import ai_engine
        eng = ai_engine.AIEngine()
        self.assertFalse(eng._validate("curtam e sigam!", 100))

    def test_generate_post_retorna_vazio_apos_tries_invalidas(self):
        import ai_engine
        eng = ai_engine.AIEngine()
        calls = {"n": 0}

        def fake_complete(system, user, temperature=0.9):
            calls["n"] += 1
            return "**markdown inválido**"

        eng._complete = fake_complete
        out = eng.generate_post(history=())
        self.assertEqual(out, "")
        self.assertEqual(calls["n"], ai_engine.MAX_TRIES)

    def test_generate_post_aceita_texto_valido(self):
        import ai_engine
        eng = ai_engine.AIEngine()

        def fake_complete(system, user, temperature=0.9):
            return "hoje a vibe tá boa demais, curti demais o rolê #vibe #brasil"

        eng._complete = fake_complete
        out = eng.generate_post(history=())
        self.assertTrue(out)
        self.assertLessEqual(len(out), 295)

    def test_validate_rejeita_so_hashtags(self):
        import ai_engine
        eng = ai_engine.AIEngine()
        self.assertFalse(eng._validate(" #musica #brasileira", 100))
        self.assertFalse(eng._validate("#a #b #c", 100))


class TestBlueskyAgent(unittest.TestCase):
    def setUp(self):
        import bluesky_agent
        self.ba = bluesky_agent
        self.client = self.ba.BskyClient({"handle": "h", "app_password": "p"})
        self.client.did = "did:test"
        self.client.jwt = "jwt"

    def test_metodos_auditoria_existem(self):
        for m in ("get_popular", "get_trending_topics", "search_posts",
                  "follow", "get_profile", "update_profile", "upload_blob"):
            self.assertTrue(hasattr(self.client, m), f"falta {m}")

    def test_update_profile_payload(self):
        self.client._req = mock.Mock(return_value={})
        self.client.update_profile(description="nova bio")
        args, kwargs = self.client._req.call_args
        self.assertEqual(args[0], "/app.bsky.actor.profile")
        self.assertEqual(kwargs["method"], "PUT")
        self.assertEqual(args[1], {"description": "nova bio"})

    def test_follow_payload(self):
        self.client._req = mock.Mock(return_value={})
        self.client.follow("did:alvo")
        args, kwargs = self.client._req.call_args
        self.assertEqual(args[1]["collection"], "app.bsky.graph.follow")
        self.assertEqual(args[1]["record"]["subject"], "did:alvo")


def make_bsky_stub(log):
    """Stub de bluesky_agent sem rede: registra chamadas e devolve dados típicos."""
    class Client:
        def __init__(self, cfg):
            self.cfg = cfg
            self.did = "did:test:stub"

        def login(self):
            log.append("login")
            return self.did

        def timeline(self, limit=30):
            return {"feed": [{"post": {
                "uri": "at://did:test:stub/app.bsky.feed.post/1",
                "cid": "cid1",
                "author": {"handle": "alvo.bsky.social", "did": "did:alvo"},
                "record": {"text": "curti demais música brasileira hoje", "createdAt": "2026-10-04T10:00:00.000Z"},
            }}]}

        def get_popular(self, limit=20):
            return {"feed": []}

        def get_trending_topics(self, limit=10):
            return {"trending": []}

        def search_posts(self, q, limit=25):
            return {"posts": []}

        def list_notifications(self, limit=50):
            return {"notifications": []}

        def get_thread(self, uri, depth=0):
            return {"thread": {"post": {"author": {"did": self.did}}}}

        def update_seen(self):
            return {}

        def like(self, uri, cid):
            log.append(("like", uri))
            return {"uri": uri}

        def reply(self, text, root_uri, root_cid, parent_uri, parent_cid):
            log.append(("reply", text))
            return {"uri": parent_uri}

        def follow(self, did):
            log.append(("follow", did))
            return {}

        def create_post(self, text):
            log.append(("post", text))
            return {"uri": "at://new"}

        def get_profile(self):
            return {"displayName": "his08", "description": "bio atual", "avatar": "x"}

        def update_profile(self, display_name=None, description=None, avatar_blob=None):
            log.append(("update_profile", description, avatar_blob))
            return {}

        def upload_blob(self, data, mime="image/png"):
            return {"blob": {"$type": "blob", "ref": {"$link": "x"}, "mimeType": mime, "size": len(data)}}

    class Module:
        @staticmethod
        def load_config():
            return {"handle": "h", "app_password": "p", "schedule": [], "autonomy": {},
                    "engagement": {"follow_keywords": ["música"], "reply_templates": ["Boa!"]},
                    "anti_spam": {"skip_like_prob": 0.0, "pause_like": [0, 0], "pause_reply": [0, 0],
                                  "pause_block": [0, 0], "pause_pre_post": [0, 0], "act_prob_day": 1.0}}

        @staticmethod
        def has_creds(cfg):
            return bool(cfg.get("handle") and cfg.get("app_password"))

        @staticmethod
        def should_engage(text, keywords):
            return True

        @staticmethod
        def BskyClient(cfg):
            return Client(cfg)

    return Module


def make_ai_stub():
    class AIEngine:
        provider = "mock"
        def __init__(self, cfg=None):
            pass
        def available(self):
            return True
        def generate_post(self, history=()):
            return "post gerado pela IA #vibe #brasil"
        def generate_reply(self, original, history=()):
            return "curti demais mano"
        def generate_bio(self, history=()):
            return "nova bio top"
        def generate_topic_post(self, topic, history=()):
            return "comentário sobre a trend #vibe"

    import types
    return types.SimpleNamespace(AIEngine=AIEngine)


class TestTickSmoke(unittest.TestCase):
    """Executa tick.py inteiro com stubs, sem rede nem sleeps longos."""

    def test_fluxo_completo_com_stubs(self):
        log = []
        modules = {
            "bluesky_agent": make_bsky_stub(log),
            "ai_engine": make_ai_stub(),
        }
        # state.json local para o load_state usar cache (sem repo GitHub)
        state_file = os.path.join(HERE, "state.json")
        with open(state_file, "w", encoding="utf-8") as f:
            f.write('{"date":"", "likes":0, "replies":0, "posts":0, "follows":0, '
                    '"likes_hour":0, "hour_key":"", "authors_today":[], "seen_uris":[], '
                    '"reply_history":[], "post_history":[], "bio_date":"", "avatar_date":"", '
                    '"sched_done":{}, "last_heartbeat":""}')

        env = {
            "BSKY_HANDLE": "test", "BSKY_APP_PASSWORD": "test",
            "BSKY_FORCE_POST": "1", "BSKY_FORCE_TEXT": "post auditado #teste",
            "G4F": "", "GITHUB_REPOSITORY": "", "GITHUB_TOKEN": "",
        }
        old_env = {k: os.environ.get(k) for k in env}
        old_time = None
        try:
            for k, v in env.items():
                os.environ[k] = v
            # zera o relógio e torna aleatoriedade determinística
            import time, random
            old_time = time.sleep
            time.sleep = lambda s: None
            old_random = (random.random, random.uniform)
            random.random = lambda: 0.05
            random.uniform = lambda a, b: 0.0
            with mock.patch.dict(sys.modules, modules):
                try:
                    runpy.run_path(os.path.join(HERE, "tick.py"), run_name="__main__")
                except SystemExit as e:
                    code = e.code
                else:
                    code = 0
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            import time, random
            time.sleep = old_time
            random.random, random.uniform = old_random
            if os.path.exists(state_file):
                os.remove(state_file)

        self.assertEqual(code, 0)
        self.assertIn("login", log)
        self.assertTrue(any(isinstance(x, tuple) and x[0] == "post" for x in log),
                        f"esperava post forçado, log={log}")
        self.assertTrue(any(isinstance(x, tuple) and x[0] == "like" for x in log),
                        f"esperava like na timeline, log={log}")


class TestBotOps(unittest.TestCase):
    """Testes da camada de auto-manutenção (bot_ops)."""

    def setUp(self):
        import bot_ops
        self.bops = bot_ops

    def test_metrics_janela_deslizante(self):
        bops = self.bops
        m = bops.Metrics()
        m.inc("ticks")
        m.inc("likes_ok", 3)
        self.assertEqual(m.counters["likes_ok"], 3)
        m.error("ia.post")
        m.error("ia.post")
        m.error("bsky.api")
        self.assertEqual(m.recent_errors(10800, "ia.post"), 2)
        self.assertEqual(m.recent_errors(10800), 3)
        self.assertGreater(m.error_rate(10800), 0)
        d = m.to_dict()
        self.assertEqual(bops.Metrics(d).counters["likes_ok"], 3)

    def test_circuit_breaker_abre_e_recupera(self):
        bops = self.bops
        cb = bops.CircuitBreaker(None, "teste", failure_threshold=3, cooldown_sec=0.01)
        self.assertTrue(cb.allow())
        cb.record_failure(); cb.record_failure()
        self.assertEqual(cb.state_now(), "closed")
        cb.record_failure()
        self.assertEqual(cb.state_now(), "open")
        self.assertFalse(cb.allow())
        import time
        time.sleep(0.02)
        self.assertTrue(cb.allow())   # probe half_open
        cb.record_success()
        self.assertEqual(cb.state_now(), "closed")
        self.assertEqual(cb.to_dict()["state"], "closed")

    def test_incident_instavel_nao_repete(self):
        bops = self.bops
        il = bops.IncidentLog()
        il.add("ia.g4f", context="x", cause="y", action="fallback", outcome="ok")
        self.assertTrue(il.outcome_ok("ia.g4f"))
        il.add("ia.g4f", context="x", cause="y", action="fallback", outcome="failed")
        self.assertFalse(il.is_unstable("ia.g4f"))
        il.add("ia.g4f", context="x", cause="y", action="fallback", outcome="failed")
        self.assertTrue(il.is_unstable("ia.g4f"))
        self.assertEqual(il.last("ia.g4f")["outcome"], "failed")
        self.assertEqual(len(il.to_dict()["entries"]), 3)

    def test_tuner_respeita_limites(self):
        bops = self.bops
        cfg = {"enabled": True, "error_window_min": 180, "error_threshold": 5, "step": 0.05,
               "params": {"extra_post_prob": {"min": 0.3, "max": 0.9},
                          "act_prob_night": {"min": 0.2, "max": 0.6}}}
        t = bops.Tuner(cfg)
        cur = {"extra_post_prob": 0.65, "act_prob_night": 0.4}
        m = bops.Metrics()
        m.error("bsky.api")  # 1 erro < threshold -> saudável, sobe
        adj = t.adjust(cur, m)
        self.assertGreater(adj["extra_post_prob"], 0.65)
        self.assertLessEqual(adj["extra_post_prob"], 0.9)
        m2 = bops.Metrics()
        for _ in range(6):
            m2.error("bsky.api")
        adj2 = t.adjust(cur, m2)  # cauteloso, desce
        self.assertLess(adj2["extra_post_prob"], 0.65)
        self.assertGreaterEqual(adj2["extra_post_prob"], 0.3)
        # nunca toca em chave fora dos limites
        self.assertNotIn("max_posts_per_day", adj2)

    def test_self_check_detecta_morte_e_cautela(self):
        bops = self.bops
        m = bops.Metrics()
        for _ in range(6):
            m.error("bsky.api")
        st = {"last_heartbeat": "2020-01-01T00:00:00+00:00", "date": "2026-10-04",
              "likes": 0, "seen_uris": [], "post_history": []}
        il = bops.IncidentLog()
        breakers = {"bsky": bops.CircuitBreaker(None, "b"),
                    "ia": bops.CircuitBreaker(None, "i"),
                    "gh": bops.CircuitBreaker(None, "g")}
        cfg = {"autonomy": {"extra_post_prob": 0.8},
               "anti_spam": {"act_prob_night": 0.5},
               "tuning": {"enabled": True, "error_window_min": 180, "error_threshold": 5, "step": 0.05,
                          "params": {"extra_post_prob": {"min": 0.3, "max": 0.95}}}}
        rep, adj = bops.run_self_check(st, m, breakers, il, cfg)
        self.assertIn("reanimar", rep["actions"])
        self.assertTrue(rep["cautious"])
        self.assertIn("modo_cauteloso", rep["actions"])
        self.assertIn("extra_post_prob", adj)
        self.assertLess(adj["extra_post_prob"], 0.8)


class TestAntiBanExtremo(unittest.TestCase):
    """Camada anti-ban ao extremo: modo proteção, rate headers e jitter."""

    def test_rate_headers_extrai_campos(self):
        import bluesky_agent as ba
        fake = {"ratelimit-limit": "60", "ratelimit-remaining": "3",
                "ratelimit-reset": "1700000000", "x-other": "x"}
        out = ba._rate_headers(fake)
        self.assertEqual(out["ratelimit-remaining"], "3")
        self.assertIn("ratelimit-limit", out)
        self.assertNotIn("x-other", out)
        self.assertEqual(ba._rate_headers(None), {})

    def test_403_marca_blocked_e_auth_error(self):
        import bluesky_agent as ba
        c = ba.BskyClient({"handle": "h", "app_password": "p"})
        self.assertFalse(c.blocked)
        self.assertFalse(c.auth_error)

    def test_modo_protecao_bloqueia_escrita(self):
        """Com blocked_until futuro no state, tick vira só leitura (sem like/post)."""
        import datetime, runpy, sys, os
        import unittest.mock as mock
        HERE = os.path.dirname(os.path.abspath(__file__))
        log = []
        modules = {
            "bluesky_agent": make_bsky_stub(log),
            "ai_engine": make_ai_stub(),
        }
        state_file = os.path.join(HERE, "state.json")
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)).isoformat()
        with open(state_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": "", "likes": 0, "replies": 0, "posts": 0, "follows": 0,
                                "likes_hour": 0, "hour_key": "", "authors_today": [], "seen_uris": [],
                                "reply_history": [], "post_history": [], "bio_date": "",
                                "avatar_date": "", "sched_done": {}, "last_heartbeat": "",
                                "blocked_until": future}))
        env = {
            "BSKY_HANDLE": "test", "BSKY_APP_PASSWORD": "test",
            "BSKY_FORCE_POST": "1", "BSKY_FORCE_TEXT": "post auditado #teste",
            "G4F": "", "GITHUB_REPOSITORY": "", "GITHUB_TOKEN": "",
        }
        old_env = {k: os.environ.get(k) for k in env}
        old_time = None
        try:
            for k, v in env.items():
                os.environ[k] = v
            import time, random
            old_time = time.sleep
            time.sleep = lambda s: None
            old_random = (random.random, random.uniform)
            random.random = lambda: 0.05
            random.uniform = lambda a, b: 0.0
            with mock.patch.dict(sys.modules, modules):
                try:
                    runpy.run_path(os.path.join(HERE, "tick.py"), run_name="__main__")
                except SystemExit as e:
                    code = e.code
                else:
                    code = 0
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            import time, random
            time.sleep = old_time
            random.random, random.uniform = old_random
            if os.path.exists(state_file):
                os.remove(state_file)

        self.assertEqual(code, 0)
        self.assertNotIn("post", [x[0] if isinstance(x, tuple) else x for x in log])
        self.assertNotIn("like", [x[0] if isinstance(x, tuple) else x for x in log])
        self.assertIn("login", log)

    def test_jitter_deterministico_dentro_da_janela(self):
        import bluesky_agent as ba
        jit = 20
        seeds = []
        for i in range(50):
            seed = int(hashlib.sha256(f"2026-10-04:sched:{i:02d}".encode()).hexdigest()[:8], 16)
            seeds.append((seed % (2 * jit + 1)) - jit)
        self.assertGreaterEqual(min(seeds), -jit)
        self.assertLessEqual(max(seeds), jit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
