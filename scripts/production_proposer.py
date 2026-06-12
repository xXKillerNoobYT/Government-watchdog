"""Production model-backed proposer over UNTIMED Alpine transcript prose
(GOV-137 / GOV-126 Phase 2).

This is the first live-model component in the chain. It plugs into the Lane-2
adapter as the ``proposer`` injected into :func:`ai_extraction.run_extraction` —
the adapter still overrides every gating field, still routes speakers through the
attribution-safety gate, still rejects orphans, and still enforces ``assert_no_pii``
at the write boundary. This module only *proposes* drafts; it authorizes nothing.

Why it exists (verified on main): GOV-125 produced 0 statements because all 28
real Alpine transcripts are untimed ASR (0/28 carry MM:SS), and
``ai_extraction``'s provider resolves fail-closed to ``offline-disabled`` with no
real proposer. The segment->statement graph was deferred here. So Phase 2 must
BUILD the proposer, anchoring statements to a SOURCE + an exact quoted char-span
(migration 0016 / 1.07 §2 char_span locator_kind), with no timed-segment
requirement.

Three guarantees, in plain code (each independently testable offline):

1. **Source-grounded, fail-closed.** The model returns, per claim, a ``quoted_text``
   it asserts is copied verbatim from the provided source. This module does NOT
   trust the model's character arithmetic — it LOCATES that quote in the source
   text (:func:`str.find`) and DERIVES ``char_start``/``char_end`` itself. A quote
   that is not a literal, contiguous substring of the source has no offsets and
   the claim is DROPPED. A hallucinated or paraphrased "quote" cannot survive,
   because it isn't in the source to find.
2. **Conservative attribution — no name > wrong name.** Untimed ASR carries no
   speaker IDs, and AI can never confirm an identity against official records, so
   this proposer DROPS every speaker the model returns. It never attaches a
   ``speaker`` to a claim. (The adapter's :func:`ai_extraction._apply_ai_speaker`
   safe gate remains the only path any future sourced-speaker hint could take, and
   that gate never binds a name.)
3. **Offline-deterministic by construction.** The actual model call sits behind an
   injectable :class:`ModelClient` seam. Tests inject a stub that returns canned
   claims; the production :class:`AnthropicModelClient` lazy-imports the Anthropic
   SDK and is never constructed in CI. The ``offline-disabled`` fail-closed default
   of ``ai_extraction`` is preserved — nothing here is reached unless a caller
   explicitly builds and injects this proposer.

Config/secrets boundary (1.11 §2.1; AI_GATEWAY §7): the Anthropic API key is read
by the SDK from the environment (``ANTHROPIC_API_KEY``) and is NEVER read into a
config dict, a claim, or the ``ai_extraction_runs`` ledger. Only ``model_name`` /
``model_version`` reach the ledger (recorded by the adapter, not here). Alpine-only,
local/vault-only, reviewer-internal — this module publishes nothing.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Protocol

# Reuse the Lane-2 source-grounded prompt verbatim (versioned + parity-tested) and
# extend it with the char-span instruction below — never re-type the rules.
from ai_extraction import SOURCE_GROUNDED_PROMPT

# Default production model (claude-api skill: default to the latest Claude model).
DEFAULT_MODEL_NAME = "claude-opus-4-8"

# The char-span addendum to the shared source-grounded prompt. It tells the model
# how to return a quote we can deterministically anchor, and reiterates the
# no-name rule (defense in depth — the code drops names regardless).
CHAR_SPAN_INSTRUCTION = """\

UNTIMED-PROSE OUTPUT CONTRACT (this transcript has no timestamps):
- For each claim, return `quoted_text`: an EXACT, contiguous, verbatim substring
  copied character-for-character from the provided source. Do NOT normalize
  whitespace, fix typos, or paraphrase inside `quoted_text`. We locate this exact
  string in the source to anchor the claim; if it is not a literal substring it
  WILL be discarded. Keep each quote focused (a sentence or two).
- `statement_text`: a faithful, neutral paraphrase of what `quoted_text` says.
  No added facts, no inferred motive, no accusation, no legal conclusion.
- `speaker_name`: leave this an EMPTY string. This transcript carries no speaker
  identification; do not guess who spoke. (Any name you return is dropped.)
- `confidence`: high / medium / low for how clearly the source supports the claim.
Return ONLY claims you can ground in an exact quote. Returning zero claims is a
valid, correct answer when nothing is cleanly groundable."""

# Structured-output JSON schema the model is constrained to (claude-api skill:
# output_config.format). additionalProperties:false on every object.
CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement_text": {"type": "string"},
                    "quoted_text": {"type": "string"},
                    "is_verbatim": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "speaker_name": {"type": "string"},
                },
                "required": ["statement_text", "quoted_text", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

_ALLOWED_CONFIDENCE = frozenset({"high", "medium", "low"})


class ModelClient(Protocol):
    """The injected model seam. ``extract`` returns a list of RAW claim dicts.

    A raw claim has keys ``statement_text``, ``quoted_text``, ``confidence`` and
    optionally ``speaker_name`` / ``is_verbatim``. The production implementation
    calls Claude; tests inject a deterministic stub. Implementations MUST be
    side-effect-free with respect to the database (they receive only text).
    """

    def extract(self, source_text: str, *, source_id: str) -> list[dict[str, Any]]:
        ...


class AnthropicModelClient:
    """Production :class:`ModelClient` backed by Claude (lazy SDK import).

    Never constructed in CI. The Anthropic SDK reads the API key from the
    environment; this class never touches, stores, or logs the key. Returns ``[]``
    on a safety refusal (``stop_reason == "refusal"``) — fail-closed.
    """

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL_NAME,
        max_tokens: int = 16000,
        effort: str = "high",
        client: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = client  # injectable for integration tests; None => real SDK

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        import anthropic  # lazy: not a CI dependency

        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        return self._client

    def extract(self, source_text: str, *, source_id: str) -> list[dict[str, Any]]:
        client = self._ensure_client()
        response = client.messages.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": CLAIM_SCHEMA},
            },
            system=SOURCE_GROUNDED_PROMPT + CHAR_SPAN_INSTRUCTION,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Source id: {source_id}\n"
                        "Extract grounded claims from ONLY the text between the "
                        "<source> markers below.\n\n"
                        f"<source>\n{source_text}\n</source>"
                    ),
                }
            ],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return []  # fail-closed: a refused request yields no claims
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        if not text.strip():
            return []
        data = json.loads(text)
        claims = data.get("claims", [])
        return claims if isinstance(claims, list) else []


# A loader: (conn, source_id) -> the already-preserved prose for that source.
SourceTextLoader = Callable[[sqlite3.Connection, str], str]
# A loader: (conn, source_id) -> pointer provenance fields for that source.
ProvenanceLoader = Callable[[sqlite3.Connection, str], dict[str, Any]]


def load_source_text(conn: sqlite3.Connection, source_id: str) -> str:
    """Read the already-preserved untimed prose for a source (NO re-fetch).

    Reads ``transcripts.full_text`` for the source (GOV-125 materialized one
    ``transcripts`` row per transcript document, source-anchored). Concatenates if
    a source has multiple transcript rows; returns ``""`` if none — the proposer
    then yields zero claims for that source (correct, not an error).
    """
    rows = conn.execute(
        "SELECT full_text FROM transcripts WHERE source_id = ? ORDER BY id",
        (source_id,),
    ).fetchall()
    return "\n\n".join(r[0] for r in rows if r and r[0])


def load_source_provenance(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    """Resolve the pointer provenance fields validate_pointer requires.

    ``original_url`` / ``scan_date`` / ``captured_at_utc`` are mandatory on every
    evidence_link pointer (1.07 §2.2). For an already-preserved local transcript
    they come from the materialized ``transcripts`` row; ``original_url`` falls
    back to a stable ``gov-source://`` URI so the pointer is always complete and we
    never INVENT a public URL the source does not have.
    """
    from ai_extraction import _now_utc_iso  # reuse the shared timestamp helper

    row = conn.execute(
        "SELECT video_url, fetch_time_utc FROM transcripts WHERE source_id = ? ORDER BY id LIMIT 1",
        (source_id,),
    ).fetchone()
    now = _now_utc_iso()
    original_url = (row["video_url"] if row and row["video_url"] else None) or f"gov-source://{source_id}"
    captured_at = (row["fetch_time_utc"] if row and row["fetch_time_utc"] else None) or now
    return {
        "original_url": original_url,
        "archive_status": "not_checked",
        "scan_date": now[:10],
        "captured_at_utc": captured_at,
    }


def _normalize_confidence(value: Any) -> str:
    return value if value in _ALLOWED_CONFIDENCE else "low"


def _ground_claim(
    source_id: str,
    source_text: str,
    raw: dict[str, Any],
    *,
    index: int,
    provenance: dict[str, Any],
) -> dict[str, Any] | None:
    """Turn one RAW model claim into a grounded, char-span-anchored claim dict.

    Returns ``None`` (DROP, fail-closed) when the claim cannot be source-grounded:
    a missing/empty paraphrase or quote, or — the load-bearing check — a
    ``quoted_text`` that is not a literal contiguous substring of ``source_text``.
    The char-span is DERIVED from the substring match, never taken from the model.
    The speaker is ALWAYS dropped (no name > wrong name).
    """
    statement_text = (raw.get("statement_text") or "").strip()
    quoted_text = raw.get("quoted_text") or ""
    if not statement_text or not quoted_text.strip():
        return None

    # The grounding gate: the quote must appear verbatim in the source. First
    # occurrence anchors it. Not found -> ungrounded -> dropped.
    char_start = source_text.find(quoted_text)
    if char_start < 0:
        return None
    char_end = char_start + len(quoted_text)

    confidence = _normalize_confidence(raw.get("confidence"))
    statement_id = f"{source_id}:ai:{char_start:08d}:{index:04d}"
    evidence_link = {
        "to_source_id": source_id,
        "relation": "references",
        "locator_kind": "char_span",
        "char_start": char_start,
        "char_end": char_end,
        "quoted_text": quoted_text,  # verbatim span; vault-only by the allowlist
        "is_verbatim": 1,            # the QUOTE is verbatim by construction
        "verification_status": "machine_extracted_unreviewed",
        "confidence": confidence,
        "original_url": provenance["original_url"],
        "archive_status": provenance["archive_status"],
        "scan_date": provenance["scan_date"],
        "captured_at_utc": provenance["captured_at_utc"],
    }
    return {
        "statement_id": statement_id,
        "statement_text": statement_text,
        "is_verbatim": 0,            # the STATEMENT is a paraphrase, never verbatim
        "confidence": confidence,
        "evidence_links": [evidence_link],
        # NOTE: no "speaker" key — conservative attribution drops every name.
    }


def build_claude_proposer(
    model_client: ModelClient,
    *,
    source_text_loader: SourceTextLoader = load_source_text,
    provenance_loader: ProvenanceLoader = load_source_provenance,
    max_claims_per_source: int | None = None,
) -> Callable[[sqlite3.Connection, list[str], list[str]], list[dict[str, Any]]]:
    """Build a :data:`ai_extraction.Proposer` over untimed source prose.

    The returned callable has the source-only signature
    ``(conn, source_ids, segment_ids) -> list[claim]`` that
    :func:`ai_extraction.run_extraction` injects. ``segment_ids`` is ignored — this
    is the source-anchored (untimed) path, so it should be empty. For each source:
    load preserved prose, ask the injected model for raw claims, then GROUND and
    shape each one (drop the ungrounded fail-closed, drop every speaker name).

    ``max_claims_per_source`` bounds output per source; when set and exceeded, the
    overflow is dropped and the drop is surfaced (never silently truncated) by the
    grounded-claim count being below the raw count — the caller's run ledger
    records ``output_count`` vs the model's claim volume.
    """

    def _proposer(
        conn: sqlite3.Connection,
        source_ids: list[str],
        segment_ids: list[str],
    ) -> list[dict[str, Any]]:
        grounded: list[dict[str, Any]] = []
        for source_id in source_ids:
            source_text = source_text_loader(conn, source_id)
            if not source_text.strip():
                continue
            provenance = provenance_loader(conn, source_id)
            raw_claims = model_client.extract(source_text, source_id=source_id)
            kept = 0
            for index, raw in enumerate(raw_claims):
                if max_claims_per_source is not None and kept >= max_claims_per_source:
                    break
                claim = _ground_claim(
                    source_id, source_text, raw, index=index, provenance=provenance
                )
                if claim is not None:
                    grounded.append(claim)
                    kept += 1
        return grounded

    return _proposer
