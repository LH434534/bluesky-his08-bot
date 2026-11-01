#!/usr/bin/env python3
"""Pré-checagem de credencial — não publica nada.

Serve para validar BSKY_HANDLE / BSKY_APP_PASSWORD sem esperar o job de 5h30.

Ordem do diagnóstico (importante):
  1. secrets presentes?
  2. a App Password abre sessão?   ← é o que realmente importa
  3. só se o login falhar, investiga o handle (para separar "senha ruim"
     de "conta não existe")

Códigos de saída:
  0  credencial válida
  2  secret ausente
  3  credencial rejeitada pelo PDS (401/403) — senha ruim ou handle inválido
  4  handle não resolve publicamente (conta não existe / foi renomeada)
  5  falha de rede até o PDS

Nunca imprime a senha, nem em modo verboso.
"""
from __future__ import annotations

import os
import sys

try:
    import requests
except ImportError:
    print("ERRO: requests não instalado (pip install requests)")
    sys.exit(6)

PDS = os.environ.get("BSKY_PDS", "https://bsky.social").rstrip("/")
PUBLIC = "https://public.api.bsky.app"

handle = os.environ.get("BSKY_HANDLE", "").strip().lstrip("@")
apppw = os.environ.get("BSKY_APP_PASSWORD", "").strip()


def mask(s: str) -> str:
    """Mostra só o formato, nunca o conteúdo."""
    if not s:
        return "(vazio)"
    return f"presente · {len(s)} chars"


def mask_handle(h: str) -> str:
    """O handle é público, mas mascaramos parte dele por padrão."""
    if not h:
        return "(vazio)"
    if "." in h:
        local, _, domain = h.partition(".")
        shown = local[:2] if len(local) > 2 else local
        return f"{shown}{'*' * max(0, len(local) - len(shown))}.{domain}"
    return h[:2] + "*" * max(0, len(h) - 2)


print("=" * 62)
print("pré-checagem de credencial — Bluesky / AT Protocol")
print("=" * 62)
print(f"PDS      : {PDS}")
print(f"handle   : {mask_handle(handle)}  ({len(handle)} chars)")
print(f"app pass : {mask(apppw)}   [nunca exibida]")
print()

# --- 1. secrets -------------------------------------------------------------
if not handle or not apppw:
    print("FALHA: secret ausente.")
    print(f"  BSKY_HANDLE       = {'ok' if handle else 'FALTANDO'}")
    print(f"  BSKY_APP_PASSWORD = {'ok' if apppw else 'FALTANDO'}")
    print("  → Settings → Secrets and variables → Actions")
    sys.exit(2)

# --- 2. o login funciona? (o que realmente importa) -------------------------
print("[1/2] a App Password abre sessão?")
try:
    r = requests.post(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": apppw}, timeout=30,
    )
except requests.RequestException as e:
    print(f"  SEM REDE até {PDS}: {type(e).__name__}: {str(e)[:140]}")
    print("  → bloqueio de saída no ambiente, não é falha de credencial")
    sys.exit(5)

if r.status_code == 200:
    d = r.json()
    print(f"  OK — sessão criada para @{d.get('handle', handle)}")
    print(f"      DID {d.get('did')}")
    print()
    print("RESULTADO: credencial válida. O bot deve postar normalmente.")
    sys.exit(0)

print(f"  REJEITADA ({r.status_code}): {r.text[:160]}")
print()

# --- 3. login falhou: é a senha ou o handle? --------------------------------
print("[2/2] o handle existe? (consulta pública, dispensa credencial)")
handle_ok = None
try:
    g = requests.get(
        f"{PUBLIC}/xrpc/com.atproto.identity.resolveHandle",
        params={"handle": handle}, timeout=25,
    )
    if g.status_code == 200:
        handle_ok = True
        print(f"  SIM — DID {g.json().get('did', '?')}")
    elif g.status_code == 400:
        handle_ok = False
        print(f"  NÃO — resolveHandle devolveu 400 para o handle configurado")
    else:
        print(f"  inconclusivo ({g.status_code}): {g.text[:120]}")
except requests.RequestException as e:
    print(f"  consulta pública falhou ({type(e).__name__}) — inconclusivo")

print()
if handle_ok is False:
    print("RESULTADO: o handle configurado NÃO EXISTE no Bluesky.")
    print(f"  handle em uso: {mask_handle(handle)}")
    print("  → a conta pode ter sido renomeada, excluída ou o secret está")
    print("    apontando para um handle antigo.")
    print("  → atualize BSKY_HANDLE com o handle atual da conta.")
    sys.exit(4)

print("RESULTADO: credencial rejeitada pelo PDS.")
print("  1. entre em bsky.app com a conta do bot")
print("  2. Settings → Privacy and Security → App Passwords")
print("  3. crie uma nova senha de app (aparece uma vez — copie na hora)")
print("  4. GitHub → repo → Settings → Secrets → BSKY_APP_PASSWORD")
sys.exit(3)
