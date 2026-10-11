#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_replies.py — auditoria de coerência: resposta do bot vs post comentado.

Lista os últimos replies do bot (author feed) e imprime pares
[resposta do bot] x [post original] para avaliação de sentido.
Somente leitura; não publica nada.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error

API = "https://bsky.social/xrpc"
HANDLE = os.environ.get("BSKY_HANDLE", "")
PASSW = os.environ.get("BSKY_APP_PASSWORD", "")
DID = "did:plc:dh6eweh7brbtraluugpvpruj"
LIMIT_FEED = int(os.environ.get("AUDIT_LIMIT", "60"))
MAX_PARES = int(os.environ.get("AUDIT_MAX_PAIRS", "40"))


def rpc(method, path, payload=None, token=None):
    url = API + "/" + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "marvis-audit-replies")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": body[:200]}
    except Exception as e:
        return 0, {"error": repr(e)}


def clean(text, n=200):
    return (text or "").replace("\n", " ").strip()[:n]


def main():
    if not (HANDLE and PASSW):
        print("sem credenciais BSKY_HANDLE/BSKY_APP_PASSWORD", flush=True)
        sys.exit(2)
    c, sess = rpc("POST", "com.atproto.server.createSession",
                  {"identifier": HANDLE, "password": PASSW})
    if c != 200:
        print("login falhou:", c, sess, flush=True)
        sys.exit(1)
    token = sess.get("accessJwt", "")
    print("login ok:", sess.get("handle"), flush=True)

    c, feed = rpc("GET", f"app.bsky.feed.getAuthorFeed?actor={DID}&limit={LIMIT_FEED}", token=token)
    if c != 200:
        print("feed falhou:", c, feed, flush=True)
        sys.exit(1)
    items = feed.get("feed", [])
    print("posts obtidos:", len(items), flush=True)

    replies = []
    for it in items:
        post = it.get("post", {}) or {}
        rec = post.get("record", {}) or {}
        if not isinstance(rec, dict):
            continue
        if rec.get("$type") != "app.bsky.feed.post":
            continue
        reply = rec.get("reply") or {}
        parent = reply.get("parent") or {}
        puri = parent.get("uri", "")
        if not puri:
            continue
        replies.append({
            "uri": post.get("uri", ""),
            "text": clean(rec.get("text"), 250),
            "parent_uri": puri,
            "created": clean(rec.get("createdAt"), 19),
        })
    print("replies encontradas:", len(replies), flush=True)
    print("----", flush=True)

    # busca o post original (parent) de cada reply e imprime o par
    for rp in replies[:MAX_PARES]:
        puri = rp["parent_uri"]
        ptext = "(sem uri)"
        try:
            c2, presp = rpc("GET", "app.bsky.feed.getPosts?uris=" + urllib.parse.quote(puri), token=token)
            if c2 == 200 and presp.get("posts"):
                prec = presp["posts"][0].get("record", {}) or {}
                ptext = clean(prec.get("text"), 200)
        except Exception as e:
            ptext = f"(erro ao buscar parent: {e!r})"
        print("RESPOSTA_BOT:", rp["text"], flush=True)
        print("  em:", rp["created"], flush=True)
        print("POST_ORIGINAL:", ptext, flush=True)
        print("----", flush=True)

    print("fim da auditoria", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("erro fatal:", repr(e), flush=True)
        sys.exit(1)
