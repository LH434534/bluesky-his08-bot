#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_engine.py — camada de IA para o bot Bluesky (stdlib only).
Providers suportados (via env):
  OPENAI_API_KEY     -> api.openai.com (chat completions, modelo gpt-4o-mini por padrão)
  ANTHROPIC_API_KEY  -> api.anthropic.com (messages, modelo claude-3-haiku por padrão)
  GEMINI_API_KEY     -> generativelanguage.googleapis.com (gemini-1.5-flash por padrão)
  AI_API_KEY + AI_API_URL + AI_MODEL -> qualquer endpoint compatível com OpenAI /v1/chat/completions

Uso:
  ai = AIEngine()
  ai.available()          # True se alguma chave existe
  ai.generate_post()      # gera um post (str)
  ai.generate_reply(text) # gera resposta para um post (str)
"""
import json
import os
import urllib.request
import urllib.error

MAX_TRIES = 2


class AIEngine:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.provider = self._detect_provider()

    # ------------------------------------------------------------ providers
    def _detect_provider(self):
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

    def _clean(self, text):
        text = (text or "").strip().strip("\"'`")
        # remove emojis duplicados/linhas vazias extras
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)[:280]

    # ------------------------------------------------------------ completions
    def _complete(self, system, user, temperature=0.9):
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

    # ------------------------------------------------------------ geradores
    def generate_post(self):
        """Gera um post curto, natural, estilo influenciador criativo."""
        sys = (
            "Você é um influenciador brasileiro autêntico no Bluesky. "
            "Crie posts curtos (máx 200 caracteres), naturais, com leve emoção "
            "e sem hashtags exageradas. Nunca use markdown. Varia o estilo."
        )
        user = (
            "Gere 1 post original para minha rede social agora. "
            "Pode ser sobre música, arte, dia a dia, criatividade ou motivação. "
            "Sem emojis demais (máx 2)."
        )
        return self._clean(self._complete(sys, user))

    def generate_reply(self, original):
        """Gera resposta curta e contextual para um post específico."""
        sys = (
            "Você é um influenciador brasileiro simpático comentando posts no Bluesky. "
            "Responda com 1 frase curta (máx 120 caracteres), contextual, positiva, "
            "sem emojis demais. Nunca use markdown."
        )
        user = f"O post é: \"{original[:200]}\"\nEscreva sua resposta:"
        return self._clean(self._complete(sys, user, temperature=0.8))


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
