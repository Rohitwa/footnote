"""Foothold per-company RAG — self-contained, cloud-free retrieval.

The Memory tab's "talk to your memory" feature and (later) grounded
recommendations retrieve over ONE company's memory at a time. Per the locked
design decision (2026-06-17): brute-force, no pgvector, and NO cloud
embeddings — so v1 ranks lexically (BM25 + recency). The retriever is
deliberately pluggable: when the on-device SLM embedder lands, `retrieve()`
can swap its scorer for cosine over on-device vectors without touching callers.

Kept independent of the core PMIS pipeline (ingestion/, retrieval/) so Foothold
stays separately deployable.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional

from targets import db as tdb

# Tiny English stoplist — enough to stop common words dominating BM25.
_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "is", "are", "was", "were", "be", "been", "it", "this", "that",
    "as", "at", "by", "from", "we", "i", "you", "they", "he", "she", "do",
    "did", "does", "what", "who", "when", "where", "why", "how", "their",
    "our", "his", "her", "them", "us", "me", "my", "about", "into", "has",
    "have", "had", "will", "would", "should", "can", "could",
}

_WORD = re.compile(r"[a-z0-9]+")


def _tok(text: str) -> List[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1]


def build_corpus(company_id: str) -> List[Dict[str, Any]]:
    """All retrievable memory chunks for one company. Each chunk:
    {source, ref_id, label, text, ts}. Reuses existing tdb readers — no new
    tables, no writes."""
    chunks: List[Dict[str, Any]] = []
    co = tdb.get_company(company_id) or {}

    # The case (static background — always relevant context).
    for field, label in (("leak", "The leak"), ("lever", "The door"),
                          ("spine", "The case")):
        if co.get(field):
            chunks.append({"source": "case", "ref_id": field, "label": label,
                           "text": co[field], "ts": co.get("updated_at")})

    for c in tdb.list_communications(company_id):
        who = f" with {c['with_name']}" if c.get("with_name") else ""
        chunks.append({
            "source": "comm", "ref_id": c["id"],
            "label": f"{(c.get('direction') or '')}/{c.get('kind') or 'touch'}{who}".strip("/"),
            "text": f"{c.get('kind') or ''} {c.get('with_name') or ''} {c.get('notes') or ''}".strip(),
            "ts": c.get("ts"),
        })

    for n in tdb.list_notes(company_id):
        chunks.append({"source": "note", "ref_id": n["id"],
                       "label": n.get("kind") or "note",
                       "text": n.get("content") or "", "ts": n.get("ts")})

    for a in tdb.list_artifacts(company_id):
        chunks.append({"source": "artifact", "ref_id": a["id"],
                       "label": f"{a.get('kind') or 'doc'}",
                       "text": a.get("title") or "", "ts": a.get("created_at")})

    for s in tdb.list_signals(company_id):
        chunks.append({"source": "signal", "ref_id": s["id"],
                       "label": s.get("kind") or "signal",
                       "text": f"{s.get('headline') or ''} {s.get('detail') or ''}".strip(),
                       "ts": s.get("event_date")})

    # Research deep-dives — split into paragraph chunks so retrieval is precise.
    for r in tdb.list_research_logs(company_id):
        for i, para in enumerate(p for p in (r.get("content") or "").split("\n\n") if p.strip()):
            chunks.append({"source": "research", "ref_id": f"{r['id']}#{i}",
                           "label": r.get("title") or "research",
                           "text": para.strip(), "ts": r.get("created_at")})

    return [c for c in chunks if c["text"].strip()]


def _recency_boost(ts: Optional[str]) -> float:
    """Small multiplicative nudge favouring fresher memory (14-day half-life,
    capped so it never dominates lexical relevance)."""
    if not ts:
        return 1.0
    try:
        from datetime import datetime
        age = (datetime.utcnow() - datetime.fromisoformat(str(ts)[:19])).total_seconds() / 86400
        return 1.0 + 0.25 * math.pow(0.5, max(0.0, age) / 14.0)
    except (ValueError, TypeError):
        return 1.0


def retrieve(company_id: str, query: str, k: int = 4,
             corpus: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Top-k memory chunks for `query` via BM25 + recency. Returns chunks with
    an added `score`. Pluggable: a future on-device embedder would replace the
    BM25 block with cosine over vectors, same signature."""
    corpus = corpus if corpus is not None else build_corpus(company_id)
    q_terms = _tok(query)
    if not corpus or not q_terms:
        return []

    docs = [_tok(c["text"] + " " + c.get("label", "")) for c in corpus]
    N = len(docs)
    avg_len = sum(len(d) for d in docs) / N if N else 0.0
    df: Dict[str, int] = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1

    k1, b = 1.5, 0.75
    scored: List[Dict[str, Any]] = []
    for chunk, d in zip(corpus, docs):
        dl = len(d) or 1
        score = 0.0
        for term in q_terms:
            if term not in df:
                continue
            tf = d.count(term)
            if not tf:
                continue
            idf = math.log(1 + (N - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / (avg_len or 1)))
        if score > 0:
            scored.append({**chunk, "score": round(score * _recency_boost(chunk.get("ts")), 4)})

    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:k]


# ── Answer synthesis ────────────────────────────────────────────────────
# Extractive-first (100% local). If SARVAM_API_KEY is set, synthesise a short
# grounded answer over the retrieved snippets using Sarvam-M (already the app's
# in-app LLM via llm_capture) — degrades to extractive when absent/erroring.
# No OpenAI, no new cloud dependency.

_SYS = ("You are a sales-memory assistant. Answer the question using ONLY the "
        "MEMORY snippets provided about this one company. Be concise (<=3 "
        "sentences). If the snippets don't contain the answer, say so plainly. "
        "Never invent facts not in the snippets.")


def answer(company_id: str, query: str, k: int = 4) -> Dict[str, Any]:
    hits = retrieve(company_id, query, k=k)
    sources = [{"source": h["source"], "ref_id": h["ref_id"],
                "label": h.get("label", ""), "text": h["text"][:240],
                "ts": (str(h.get("ts"))[:10] if h.get("ts") else "")}
               for h in hits]
    if not hits:
        return {"answer": "Nothing in this company's memory matches that yet.",
                "sources": [], "synthesized": False}

    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not key:
        return {"answer": "", "sources": sources, "synthesized": False}

    context = "\n".join(f"- [{h['source']}] {h['text'][:300]}" for h in hits)
    try:
        import requests
        from targets.llm_capture import SARVAM_CHAT_URL
        resp = requests.post(
            SARVAM_CHAT_URL,
            headers={"api-subscription-key": key, "Content-Type": "application/json"},
            json={"model": "sarvam-105b", "temperature": 0.1, "messages": [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": f"MEMORY:\n{context}\n\nQUESTION: {query}"},
            ]},
            timeout=30,
        )
        if resp.status_code == 200:
            txt = resp.json()["choices"][0]["message"]["content"].strip()
            return {"answer": txt, "sources": sources, "synthesized": True}
    except Exception:  # noqa: BLE001 — extractive fallback never fails the call
        pass
    return {"answer": "", "sources": sources, "synthesized": False}
