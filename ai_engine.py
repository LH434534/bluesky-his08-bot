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
    "5) NUNCA: pareça motivacional genérico, nunca liste hashtags, nunca use markdown, nunca escreva como influencer "
    "de marketing, nunca termine com 'curtam' ou 'sigam'. "
    "6) Máx 2 emojis por texto, e só emojis que mlk de 18 usa de verdade (fire, risada, sparkles, triste, joia, coração). "
    "7) Varia o estilo a cada texto: nunca repita estrutura. Às vezes só uma frase, às vezes um desabafo, às vezes zoeira."
)


def persona_system(base):
    """Injeta a persona humana em qualquer system prompt."""
    return PERSONA_HUMANO + " " + base



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

    def _clean(self, text):
        text = (text or "").strip().strip("\"'`")
        # remove emojis duplicados/linhas vazias extras
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)[:280]

    # ------------------------------------------------------------ completions
    def _complete(self, system, user, temperature=0.9):
        if self.provider == "g4f":
            # GitHub: xtekky/gpt4free — IA gratuita sem key (model=auto escolhe provedor)
            # timeout de 50s por chamada para não travar o tick (g4f pode pendurar)
            try:
                from g4f.client import Client as G4FClient
            except ImportError as e:
                raise RuntimeError("g4f não instalado (pip install -U g4f)") from e
            import concurrent.futures

            def _call():
                client = G4FClient()
                resp = client.chat.completions.create(
                    model="auto",
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    web_search=False,
                )
                return resp.choices[0].message.content

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(_call)
                    return fut.result(timeout=50)
            except Exception as e:
                print("  [ia] g4f timeout/falha:", type(e).__name__, str(e)[:120])
                return None

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

    # ------------------------------------------------------------ geradores
    def generate_post(self):
        """Gera um post curto, natural, estilo influenciador criativo."""
        sys = persona_system(
            "Escreva um post curto (máx 200 caracteres) para a sua rede social. "
            "Pode ser sobre música, jogo, futebol, rolê, faculdade, tédio, crush, zoeira ou o que te passar na cabeça."
        )
        user = "O que você vai postar agora? Escreve como você fala mesmo, do jeito que digitaria no celular."
        return self._clean(self._complete(sys, user))

    def generate_reply(self, original):
        """Gera resposta curta e contextual para um post específico."""
        sys = persona_system(
            "Responda com 1 frase curta (máx 120 caracteres), do jeito que um mlk de 18 responde um amigo "
            "ou alguém que ele nem conhece no feed: natural, sincero, às vezes engraçado, às vezes só concordando. "
            "Nunca pareça resposta pronta de atendente."
        )
        user = f"O post é: \"{original[:200]}\"\nEscreva sua resposta:"
        return self._clean(self._complete(sys, user, temperature=0.8))

    def generate_bio(self):
        """Gera uma bio nova e autêntica para o perfil."""
        sys = persona_system(
            BLUESKY_POLICY + " "
            "Escreva uma bio curta (máx 120 caracteres) pro seu perfil, do jeito que um mlk de 18 escreve. "
            "Sem hashtags, sem markdown, sem emojis demais (máx 2)."
        )
        user = "Escreva uma bio nova e original para meu perfil agora."
        return self._clean(self._complete(sys, user, temperature=0.9))

    def generate_topic_post(self, topic):
        """Gera um post comentando sobre um assunto que está em alta."""
        sys = persona_system(
            "Comente o assunto em alta com um post curto (máx 200 caracteres), do jeito que um mlk de 18 comenta "
            "uma trend ao vivo: opinião sincera, leve, sem parecer jornalista nem copy."
        )
        user = (
            f"O assunto em alta é: \"{topic[:200]}\"\n"
            "Escreva 1 post seu comentando sobre isso, como se você estivesse "
            "acompanhando a trend ao vivo."
        )
        return self._clean(self._complete(sys, user, temperature=0.85))


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
