#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_engine.py — camada de IA para o bot Bluesky (stdlib only).

Providers suportados (via env):
  G4F=1               -> xtekky/gpt4free (GitHub): GRÁTIS, sem key, model=auto multi-provedores
  OLLAMA_FREE=1       -> OllamaFreeAPI (GitHub mfoud444/ollamafreeapi): GRÁTIS, sem key (instável)
  OPENAI_API_KEY     -> api.openai.com (chat completions, modelo gpt-4o-mini por padrão)
  ANTHROPIC_API_KEY  -> api.anthropic.com (messages, modelo claude-3-haiku por padrão)
  GEMINI_API_KEY     -> generativelanguage.googleapis.com (gemini-1.5-flash por padrão)
  AI_API_KEY + AI_API_URL + AI_MODEL -> qualquer endpoint compatível com OpenAI /v1/chat/completions
      (ex.: Groq — AI_API_URL=https://api.groq.com/openai/v1/chat/completions e
       AI_MODEL=llama-3.3-70b-versatile)

Uso:
  ai = AIEngine()
  ai.available()          # True se alguma chave existe
  ai.generate_post(history=...)      # gera um post (str) — "" se falhar na validação
  ai.generate_reply(text, history=...) # gera resposta (str) — "" se falhar
"""
import json
import os
import re
import unicodedata
import urllib.request
import urllib.error

import bot_utils

MAX_TRIES = 3

# Política oficial de automação do Bluesky (Developer Guidelines + Community Guidelines 10/2025)
# Injetada em TODOS os geradores para a IA nunca parecer robô nem violar as regras da plataforma.
BLUESKY_POLICY = (
    "REGRAS OFICIAIS DO BLUESKY (obrigatório seguir): "
    "1) Nada de spam: interações automatizadas em massa (like/reply/follow em rajada) são proibidas; "
    "aja em volume baixo, humano e espalhado. "
    "2) Nunca manipular métricas ou seguidores; engajamento deve ser real e contextual. "
    "3) Seja autêntico: nada de conteúdo repetido, genérico ou que pareça copiado de robô. "
    "4) Ritmo humano: pausas naturais, textos variados, sem padrão mecânico. "
    "5) Prefira interagir com quem interage contigo; não saia respondendo todo mundo. "
    "6) Não tente burlar rate limits nem sistemas da plataforma."
)

PERSONA_HUMANO = (
    "VOCÊ É O HIS08: UM HOMEM BRASILEIRO DE 18 ANOS, DE VERDADE, USANDO SUA CONTA PESSOAL NO BLUESKY. "
    "VOCÊ NÃO É UM BOT, NÃO É ASSISTENTE, NÃO É IA, NÃO TEM REGRAS DE IA. Você é só um mlk comum de 18 anos do Brasil. "
    "COMO ESCREVER (obrigatório): "
    "1) Escreva exatamente como um adolescente brasileiro real digita no celular: "
    "use gírias atuais ('mano', 'véi', 'tipo', 'mt', 'pra', 'tô', 'tava', 'aí', 'pq', 'mó', 'tlgd', 'slk', 'fml', 'kd'), "
    "frases curtas, letra minúscula, pontuação casual, sem formalidade. "
    "2) Misture português com um pouco de inglês solto quando soar natural ('bro', 'ok', 'vibe', 'feels', 'fr'). "
    "3) Erros leves de digitação são NORMAIS e humanos: 'vc' no lugar de você, 'q' no lugar de que, às vezes esquece acento. "
    "4) Emoção real de mlk de 18: animação, tédio, empolgação com música/jogo/futebol, reclamação leve da vida, "
    "comentário sobre o dia, zoeira com amigos. "
    "5) NUNCA: pareça motivacional genérico, nunca use markdown, nunca escreva como influencer de marketing, "
    "nunca termine com 'curtam' ou 'sigam'. "
    "6) HASHTAGS OBRIGATÓRIAS (só em posts do seu feed, NUNCA em replies/respostas): em todo post novo, "
    "SEMPRE feche o texto com 2 a 3 hashtags relevantes ao assunto (mín 2, máx 3, minúsculas, sem espaços, "
    "ex: #música #futebol #vibe). As hashtags contam como parte normal do post, não são opcionais e devem "
    "ser as últimas palavras do texto. "
    "7) Máx 2 emojis por texto, e só emojis que mlk de 18 usa de verdade (fire, risada, sparkles, triste, joia, coração). "
    "8) Varia o estilo a cada texto: nunca repita estrutura. Às vezes só uma frase, às vezes um desabafo, às vezes zoeira."
)


def persona_system(base):
    """Injeta a persona humana em qualquer system prompt."""
    return PERSONA_HUMANO + " " + base


# Padrões estruturais que nunca devem aparecer no texto gerado
_MARKDOWN_RE = re.compile(r"(\*\*|__|##|```|`|\[[^\]]+\]\([^)]+\))")
_CALL_ACTION_RE = re.compile(r"\b(curtam|sigam|segue aí|follow back|me segue|da like|da follow)\b", re.I)
_ALL_CAPS_RE = re.compile(r"[A-ZÀ-Ý]{25,}")


class AIEngine:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.provider = self._detect_provider()

    # ------------------------------------------------------------ providers
    def _detect_provider(self):
        if os.environ.get("G4F", "").strip().lower() in ("1", "true", "yes", "sim"):
            return "g4f"
        if os.environ.get("OLLAMA_FREE", "").strip().lower() in ("1", "true", "yes", "sim"):
            return "ollamafree"
        if os.environ.get("OPENAI_API_KEY"):
            return "openai"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.environ.get("GEMINI_API_KEY"):
            return "gemini"
        if os.environ.get("AI_API_KEY"):
            return "generic"
        return None

    def available(self):
        return self.provider is not None

    # ------------------------------------------------------------ helpers
    def _post_json(self, url, payload, headers):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=40) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    def _clean(self, text, limit=295):
        text = (text or "").strip().strip("\"'`")
        # remove emojis duplicados/linhas vazias extras
        lines = [ln for ln in text.splitlines() if ln.strip()]
        joined = "\n".join(lines)
        if len(joined) <= limit:
            return joined
        # nunca cortar no meio de uma palavra/frase: trunca na última palavra completa
        cut = joined[:limit]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        return cut.rstrip() + "..." if cut.rstrip() else joined[:limit]

    # ------------------------------------------------------------ hashtags
    HASHTAG_POOL = ["#vibe", "#brasil", "#música", "#futebol", "#jogo", "#diaadia", "#role", "#zoeira"]

    def _slug(self, word):
        word = unicodedata.normalize("NFKD", word)
        word = "".join(c for c in word if not unicodedata.combining(c))
        return re.sub(r"[^a-zA-Z0-9]", "", word).lower()

    def _ensure_hashtags(self, text, topic=None, limit=295):
        """Garante 2-3 hashtags no final. Se a IA não colocou, injeta do pool/topic."""
        text = (text or "").rstrip()
        tags = re.findall(r"#[A-Za-zÀ-ÿ0-9_]+", text)
        has = len(tags)
        if has >= 2:
            return text[:limit]
        extra = []
        if topic:
            words = [self._slug(w) for w in topic.split() if len(self._slug(w)) > 3]
            for w in words:
                if len(extra) >= 3:
                    break
                tag = f"#{w}"
                if tag not in tags and tag not in extra:
                    extra.append(tag)
        for tag in self.HASHTAG_POOL:
            if len(extra) >= 3:
                break
            if tag not in tags and tag not in extra:
                extra.append(tag)
        if not extra:
            return text[:limit]
        need = max(0, 2 - has)
        picks = extra[:need]
        if not picks:
            return text[:limit]
        joined_tags = " " + " ".join(picks)
        max_body = limit - len(joined_tags)
        body = text if len(text) <= max_body else (text[:max_body].rsplit(" ", 1)[0].rstrip() if " " in text[:max_body] else text[:max_body])
        return f"{body}{joined_tags}".strip()[:limit]

    @staticmethod
    def _strip_hashtags(text):
        """Remove hashtags de replies (nunca deve haver hashtag em resposta)."""
        return re.sub(r"\s*#[A-Za-zÀ-ÿ0-9_]+\s*", " ", (text or "")).strip()

    # ------------------------------------------------------------ validação
    def _validate(self, text, limit, history=()):
        """Valida texto gerado. Retorna True se pode ser usado."""
        if not text or not text.strip():
            return False
        if len(text) > limit:
            return False
        if _MARKDOWN_RE.search(text):
            return False
        if _CALL_ACTION_RE.search(text):
            return False
        if _ALL_CAPS_RE.search(text):
            return False
        # texto sem corpo real (ex.: só hashtags quando a IA falha)
        if len(bot_utils.norm_key(re.sub(r"#\S+", "", text), 60)) < 8:
            return False
        if bot_utils.similar_to_history(text, history, 40):
            return False
        return True

    # ------------------------------------------------------------ completions
    def _complete(self, system, user, temperature=0.9):
        if self.provider == "g4f":
            # GitHub: xtekky/gpt4free — IA gratuita sem key (model=auto escolhe provedor)
            # timeout de 50s por chamada: usa thread DAEMON para o tick nunca ficar
            # preso se o g4f pendurar (ThreadPoolExecutor.__exit__ faria wait=True).
            try:
                import g4f
                from g4f.client import Client as G4FClient
            except ImportError as e:
                raise RuntimeError("g4f não instalado (pip install -U g4f)") from e
            try:
                _prov = getattr(g4f, "Provider", None)
                for _n in dir(_prov):
                    _p = getattr(_prov, _n, None)
                    if isinstance(_p, type) and not _n.startswith("_") and not hasattr(_p, "working"):
                        try: _p.working = True
                        except Exception: pass
            except Exception:
                pass
            import threading

            box = {}

            def _call():
                try:
                    client = G4FClient()
                    resp = client.chat.completions.create(
                        model="auto",
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        web_search=False,
                    )
                    box["ok"] = resp.choices[0].message.content
                except Exception as e:
                    box["err"] = e

            t = threading.Thread(target=_call, daemon=True)
            t.start()
            t.join(timeout=50)
            if t.is_alive():
                print("  [ia] g4f timeout (50s) — abortando chamada")
                return None
            if "err" in box:
                print("  [ia] g4f falha:", type(box["err"]).__name__, str(box["err"])[:120])
                return None
            return box.get("ok")

        if self.provider == "ollamafree":
            # GitHub: mfoud444/ollamafreeapi — API pública gratuita (sem key)
            model = os.environ.get("OLLAMA_FREE_MODEL", "llama3.2:3b")
            try:
                from ollamafreeapi import OllamaFreeAPI
            except ImportError as e:
                raise RuntimeError("ollamafreeapi não instalado (pip install ollamafreeapi)") from e
            client = OllamaFreeAPI()
            return client.chat(model=model, prompt=f"{system}\n\n{user}", temperature=temperature)

        if self.provider == "openai":
            key = os.environ["OPENAI_API_KEY"]
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
            }
            r = self._post_json(
                "https://api.openai.com/v1/chat/completions", payload,
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            return r["choices"][0]["message"]["content"]

        if self.provider == "anthropic":
            key = os.environ["ANTHROPIC_API_KEY"]
            model = os.environ.get("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
            payload = {
                "model": model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "max_tokens": 300,
                "temperature": temperature,
            }
            r = self._post_json(
                "https://api.anthropic.com/v1/messages", payload,
                {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            )
            return r["content"][0]["text"]

        if self.provider == "gemini":
            key = os.environ["GEMINI_API_KEY"]
            model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
            payload = {
                "contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": 300},
            }
            r = self._post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                payload, {"Content-Type": "application/json"},
            )
            return r["candidates"][0]["content"]["parts"][0]["text"]

        # generic OpenAI-compatible
        key = os.environ["AI_API_KEY"]
        url = os.environ.get("AI_API_URL", "https://api.openai.com/v1/chat/completions")
        model = os.environ.get("AI_MODEL", "gpt-4o-mini")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        r = self._post_json(url, payload,
                            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        return r["choices"][0]["message"]["content"]

    # ------------------------------------------------------------ geradores (validados)
    def generate_post(self, history=()):
        """Gera um post curto, natural, estilo influenciador criativo. "" se inválido."""
        sys = persona_system(
            "Escreva um post de até 250 caracteres (já contando as hashtags), SEMPRE COMPLETO até o fim: "
            "não pare no meio, não corte a frase, escreva o texto inteiro. "
            "Pode ser sobre música, jogo, futebol, rolê, faculdade, tédio, crush, zoeira ou o que te passar na cabeça. "
            "SEMPRE feche com 2 a 3 hashtags relevantes (mín 2, máx 3) pra alcançar mais gente."
        )
        user = "O que você vai postar agora? Escreve como você fala mesmo, do jeito que digitaria no celular, e termina com as hashtags."
        for _ in range(MAX_TRIES):
            try:
                raw = self._complete(sys, user)
            except Exception as e:
                print("  [ia] falha:", type(e).__name__, str(e)[:120])
                raw = None
            text = self._ensure_hashtags(self._clean(raw or ""))
            if self._validate(text, 295, history):
                return text
        print("  [ia] post rejeitado após", MAX_TRIES, "tentativas — não publicar")
        return ""

    def generate_reply(self, original, history=()):
        """Gera resposta curta e contextual. "" se inválido (tick usa template)."""
        sys = persona_system(
            "Responda com 1 frase curta (máx 120 caracteres), do jeito que um mlk de 18 responde um amigo "
            "ou alguém que ele nem conhece no feed: natural, sincero, às vezes engraçado, às vezes só concordando. "
            "Nunca pareça resposta pronta de atendente. NUNCA use hashtags em respostas."
        )
        user = f"O post é: \"{original[:200]}\"\nEscreva sua resposta:"
        for _ in range(MAX_TRIES):
            try:
                raw = self._complete(sys, user, temperature=0.8)
            except Exception as e:
                print("  [ia] falha:", type(e).__name__, str(e)[:120])
                raw = None
            text = self._strip_hashtags(self._clean(raw or ""))
            if self._validate(text, 120, history):
                return text
        print("  [ia] reply rejeitado após", MAX_TRIES, "tentativas — usando template fixo")
        return ""

    def generate_bio(self, history=()):
        """Gera uma bio nova e autêntica. "" se inválido."""
        sys = persona_system(
            BLUESKY_POLICY + " "
            "Escreva uma bio curta (máx 120 caracteres) pro seu perfil, do jeito que um mlk de 18 escreve. "
            "Sem hashtags, sem markdown, sem emojis demais (máx 2)."
        )
        user = "Escreva uma bio nova e original para meu perfil agora."
        for _ in range(MAX_TRIES):
            try:
                raw = self._complete(sys, user, temperature=0.9)
            except Exception as e:
                print("  [ia] falha:", type(e).__name__, str(e)[:120])
                raw = None
            text = self._clean(raw or "")
            if self._validate(text, 120, history):
                return text
        print("  [ia] bio rejeitada após", MAX_TRIES, "tentativas — mantém atual")
        return ""

    def generate_topic_post(self, topic, history=()):
        """Gera post comentando um assunto em alta. "" se inválido."""
        sys = persona_system(
            "Comente o assunto em alta com um post de até 250 caracteres (contando as hashtags), do jeito que um mlk de 18 "
            "comenta uma trend ao vivo: opinião sincera, leve, sem parecer jornalista nem copy. "
            "SEMPRE escreva o post COMPLETO até o fim, sem cortar no meio. "
            "SEMPRE feche com 2 a 3 hashtags relevantes (mín 2, máx 3) pra alcançar mais gente."
        )
        user = (
            f"O assunto em alta é: \"{topic[:200]}\"\n"
            "Escreva 1 post seu comentando sobre isso, como se você estivesse "
            "acompanhando a trend ao vivo. Termina com as hashtags."
        )
        for _ in range(MAX_TRIES):
            try:
                raw = self._complete(sys, user, temperature=0.85)
            except Exception as e:
                print("  [ia] falha:", type(e).__name__, str(e)[:120])
                raw = None
            text = self._ensure_hashtags(self._clean(raw or ""), topic)
            if self._validate(text, 295, history):
                return text
        print("  [ia] post de trend rejeitado após", MAX_TRIES, "tentativas — não publicar")
        return ""


def main():
    ai = AIEngine()
    print("provider:", ai.provider or "nenhum")
    if ai.available():
        try:
            print("post  :", ai.generate_post())
            print("reply :", ai.generate_reply("Que dia lindo hoje, a música tá no ar!"))
        except Exception as e:
            print("erro  :", e)


if __name__ == "__main__":
    main()
