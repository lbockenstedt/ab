"""Anthropic Messages API-compatible proxy so external Anthropic clients
(notably Claude Code, pointed at this host via ``ANTHROPIC_BASE_URL``) can use
AppBuilder as an LLM router.

Every inbound ``POST /v1/messages`` is translated into AppBuilder's internal
message shape and routed through :func:`llm_client.call_llm` with an
``LlmRequirements`` derived from the request (size + tool presence), so the
existing capability/cost-aware ``model_selection.select_model`` picks the best
LLM for the job — the same routing the fix engine and chat use. The winning
model's reply is translated back into the Anthropic response envelope (plain
JSON, or a synthetic SSE stream when the client asks for ``stream: true``).

Auth: the WebUI's session middleware is bypassed for ``/v1/*`` (see main's
``_AUTH_EXEMPT_PREFIX``); this router does its own API-key check instead. Set a
key via the ``AB_PROXY_KEY`` env var or the ``llm_proxy_api_key`` config
value and clients must send it as ``x-api-key`` or ``Authorization: Bearer``.
With no key configured the endpoint is open (a warning is logged) — fine for a
trusted LAN, but set a key for anything exposed.

Tool use round-trips: Anthropic ``tools`` / ``tool_use`` / ``tool_result``
blocks map onto the internal OpenAI-style ``tools`` param and tool-call return
shape (``{"text", "tool_calls"}``), so an agentic client's tool loop works
through the proxy.
"""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from llm_client import call_llm
from model_selection import LlmRequirements
from config_store import load_config

logger = logging.getLogger("LlmProxy")

router = APIRouter()

# Model id advertised to clients. Claude Code sends whatever model it's told to
# use; the proxy ignores it for routing (AppBuilder picks the model) and echoes a
# stable synthetic id back so the client has something coherent to display.
_PROXY_MODEL_ID = "ab-router"

# Agentic mode: when the client asks for this model id (Claude Code:
# ANTHROPIC_MODEL=ab-agent), the proxy does NOT do a single passthrough
# call — it runs AppBuilder's OWN server-side agent loop (chat.run_agent_loop)
# with the same CHAT_TOOLS the dashboard chat uses, so an external client (a
# curl, the Claude CLI) gets an agent that can list repos, read files, inspect
# issues, and — when autofix is enabled — trigger the real fix pipeline. This
# makes the LLM router a third fix/feature intake alongside the UI bug report
# and the UI feature request, reusing the exact same build/maintain tools.
_PROXY_AGENT_MODEL_ID = "ab-agent"


def _wants_agentic(body: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """True when this request should run the server-side agent loop instead of a
    single passthrough. Triggered by the requested model id (contains
    ``ab-agent``, or an operator-configured ``llm_proxy_agent_model_ids``
    entry) or globally via ``llm_proxy_agentic_default``."""
    if cfg.get("llm_proxy_agentic_default"):
        return True
    model = str(body.get("model") or "").strip().lower()
    if not model:
        return False
    ids = cfg.get("llm_proxy_agent_model_ids") or [_PROXY_AGENT_MODEL_ID]
    return any(str(i).lower() in model for i in ids)


def _proxy_fix_proposal(descriptor: Dict[str, Any], text: str,
                        cfg: Dict[str, Any], gh: Any) -> str:
    """on_fix_proposal handler for the agentic router. propose_fix itself is
    non-mutating; this decides what the router DOES with a proposal.

    Runs a cheap pre-build boundary PRE-FLIGHT (feature_boundary.prefilter over
    the issue's title/body) so the caller is told up front when a fix can only
    ever come back as a human-reviewed PR (core-systems boundary). Then, only
    when ``llm_proxy_autofix_enabled`` is opted in, triggers the real
    process_single_issue pipeline (full autonomy) — which clones, fixes, runs
    tests, and gates the merge through the review panel, with core-systems
    diffs forced to a human-reviewed PR (never direct-push). Defaults OFF so an
    open/keyless proxy never mutates without an explicit operator opt-in."""
    repo = descriptor.get("repo")
    number = descriptor.get("number")
    pref = descriptor.get("llm_preference")
    prefix = (text + "\n\n") if text else ""

    # ── Pre-build boundary pre-flight (informational) ──
    preflight = ""
    try:
        import feature_boundary
        title = descriptor.get("title") or ""
        body = ""
        if gh is not None and repo and number is not None:
            try:
                iss = gh.get_repo(repo).get_issue(int(number))
                title = title or (iss.title or "")
                body = iss.body or ""
            except Exception:  # noqa: BLE001 — pre-flight is best-effort
                pass
        hits = feature_boundary.prefilter(title, body, cfg.get("feature_boundaries") or [])
        hard_hits = (hits or {}).get("hits") or []
        soft_hits = (hits or {}).get("soft_hits") or []
        if hard_hits:
            ids = ", ".join(h.get("id", "?") for h in hard_hits)
            preflight = ("\n\n⚠️ Pre-flight: this issue likely touches core-systems "
                         f"boundary rule(s) [{ids}], so any fix will come back as a "
                         "human-reviewed PR — it can never auto-merge or direct-push.")
        elif soft_hits:
            ids = ", ".join(h.get("id", "?") for h in soft_hits)
            preflight = ("\n\n📋 Pre-flight: this issue may relate to boundary rule(s) "
                         f"[{ids}]; if the actual diff touches them it will be forced "
                         "to a human-reviewed PR.")
    except Exception:  # noqa: BLE001
        pass

    if not cfg.get("llm_proxy_autofix_enabled", False):
        return (prefix + f"🔧 A fix is warranted for {repo}#{number}, but autonomous "
                "fixing is DISABLED on this router (set llm_proxy_autofix_enabled to "
                "enable). Trigger it from the dashboard chat's Confirm button, or "
                "enable autofix to let the router run it." + preflight)

    # ── Full autonomy (opt-in): trigger the real pipeline in the background ──
    try:
        from fix_engine import process_single_issue

        def _run():
            try:
                ok, msg = process_single_issue(repo, number, llm_preference=pref)
                logger.info("Router-triggered fix %s#%s -> ok=%s msg=%s", repo, number, ok, msg)
            except Exception as e:  # noqa: BLE001
                logger.error("Router-triggered fix %s#%s failed: %s", repo, number, e)

        threading.Thread(target=_run, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        return prefix + f"Tried to trigger the fix for {repo}#{number} but couldn't launch it: {e}"

    return (prefix + f"✅ Triggered the automated fix pipeline for {repo}#{number} "
            f"(LLM preference: {pref or 'auto'}). It will clone, generate the fix, run "
            "tests, then the review panel gates the merge — direct-push only when a "
            "trusted+owned repo is Approved AND the diff doesn't touch a core-systems "
            "boundary; otherwise it comes back as a human-reviewed PR." + preflight)


def _run_agentic(system: Any, a_messages: List[Dict[str, Any]],
                 cfg: Dict[str, Any]) -> Tuple[str, str]:
    """Run AppBuilder's server-side agent loop for one /v1/messages turn and
    return (final_text, model_label). Builds the same context index + system
    prompt the dashboard chat uses so the router agent has repo/issue awareness
    and the CHAT_TOOLS."""
    import chat as _chat
    from github import Github

    token = cfg.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    gh = Github(token) if token else None

    base_system = cfg.get("CHAT_SYSTEM_PROMPT") or "You are a helpful assistant."
    client_system = _system_text(system)
    index_text = _chat.build_chat_context_index(cfg, gh=gh)
    system_prompt = "\n\n".join(p for p in (base_system, client_system, index_text) if p)

    internal = _to_internal_messages(None, a_messages)  # our own system is prepended below
    messages = [{"role": "system", "content": system_prompt}] + internal
    # The router agent often reads large source files in several chunked
    # read_file calls, so give it more headroom than the dashboard default
    # before the forced final-answer turn kicks in. Dedicated key so it doesn't
    # inherit a low CHAT_TOOL_MAX_ITERATIONS.
    max_iter = int(cfg.get("llm_proxy_agent_max_iterations") or 10)
    max_result_chars = int(cfg.get("CHAT_TOOL_MAX_TOKENS", 12000) or 12000) * 4
    task_id = f"proxy-agent-{uuid.uuid4().hex[:8]}"

    # Steer the router toward a SMARTER model than the dashboard chat's cost-
    # first default: agentic diagnosis reasons over code across several tool
    # turns and must synthesize, so default to complexity=large + prefer_capable
    # (pick the smartest tier first) and drop latency_sensitive. All knobs are
    # operator-overridable; llm_proxy_agent_prefer_capable=false reverts to
    # cost-first, and llm_proxy_agent_pin hard-pins an exact model.
    complexity = cfg.get("llm_proxy_agent_complexity") or "large"
    prefer_capable = cfg.get("llm_proxy_agent_prefer_capable", True)
    restrict = cfg.get("llm_proxy_agent_restrict") or None
    pin = cfg.get("llm_proxy_agent_pin") or None
    tool_reqs = LlmRequirements(complexity=complexity, needs_tools=True,
                                prefer_capable=bool(prefer_capable),
                                restrict=restrict, pin_key=pin)
    final_reqs = LlmRequirements(complexity=complexity,
                                 prefer_capable=bool(prefer_capable),
                                 restrict=restrict, pin_key=pin)

    final_text = _chat.run_agent_loop(
        messages, cfg, gh, task_id=task_id,
        max_iter=max_iter, max_result_chars=max_result_chars,
        status_cb=lambda s: logger.info("agentic %s: %s", task_id, s),
        on_fix_proposal=lambda desc, text: _proxy_fix_proposal(desc, text, cfg, gh),
        tool_requirements=tool_reqs, final_requirements=final_reqs,
    )
    return final_text or "", task_id


def _interactive_config() -> Dict[str, Any]:
    """load_config() with interactive routing bounds layered on top.

    call_llm's defaults are tuned for the autonomous fix engine, where a 15-min
    generation is fine: LLM_TIMEOUT=900s per attempt, LLM_MAX_RETRIES=5 (6
    attempts) PER candidate, tried sequentially down the whole failover chain.
    For a human waiting in Claude Code that turns one slow or stuck upstream
    into a multi-minute hang that finally surfaces as "all upstream LLMs
    failed" — the client gives up long before the router does, and the wedged
    call keeps holding the shared PICKER slot behind it.

    Shrink per-attempt time and drop same-endpoint retries so failover reaches
    a working model in seconds (or returns the real error fast). Operators can
    override via LLM_PROXY_* config keys. Read live on every request so a
    settings change needs no restart.
    """
    cfg = dict(load_config() or {})
    cfg["LLM_TIMEOUT"] = int(cfg.get("LLM_PROXY_TIMEOUT") or 120)
    cfg["LLM_MAX_RETRIES"] = int(cfg.get("LLM_PROXY_MAX_RETRIES") or 0)
    cfg["LLM_5XX_MAX_RETRIES"] = 0
    return cfg


def _proxy_deadline(cfg: Dict[str, Any]) -> float:
    """Hard wall-clock ceiling for the whole routing attempt. asyncio.wait_for
    can't cancel the worker thread, but it stops the CLIENT hanging: a
    pathological chain returns a prompt 504 instead of an open connection."""
    return float(cfg.get("LLM_PROXY_DEADLINE") or 150)


# ── Auth ────────────────────────────────────────────────────────────────────
def _configured_key() -> str:
    env = (os.environ.get("AB_PROXY_KEY") or "").strip()
    if env:
        return env
    try:
        return str((load_config() or {}).get("llm_proxy_api_key") or "").strip()
    except Exception:  # noqa: BLE001 - config read must never 500 the proxy
        return ""


def _presented_key(request: Request) -> str:
    key = (request.headers.get("x-api-key") or "").strip()
    if key:
        return key
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth


def _authorized(request: Request) -> bool:
    want = _configured_key()
    if not want:
        logger.warning("LLM proxy request served with NO api key configured — "
                       "endpoint is open. Set AB_PROXY_KEY or "
                       "llm_proxy_api_key to require authentication.")
        return True
    return _presented_key(request) == want


# ── Request translation (Anthropic -> internal) ─────────────────────────────
def _system_text(system: Any) -> str:
    """Anthropic ``system`` is a string or a list of text blocks."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _stringify_content(content: Any) -> str:
    """A tool_result's ``content`` is a string or a list of text/other blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif "text" in b:
                    parts.append(str(b.get("text")))
                else:
                    parts.append(json.dumps(b))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _to_internal_messages(system: Any,
                          messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate an Anthropic request's system+messages into AppBuilder's internal
    message list ({role, content[, tool_calls]} / {role:tool,...})."""
    out: List[Dict[str, Any]] = []
    sys_txt = _system_text(system)
    if sys_txt:
        out.append({"role": "system", "content": sys_txt})

    for m in messages or []:
        role = m.get("role")
        content = m.get("content")

        # Simple string content — the common case for plain chat.
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": role, "content": "" if content is None else str(content)})
            continue

        if role == "assistant":
            text_parts, tool_calls = [], []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b.get("id") or f"toolu_{uuid.uuid4().hex[:8]}",
                        "function": {"name": b.get("name") or "",
                                     "arguments": json.dumps(b.get("input") or {})},
                    })
            msg: Dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        else:
            # user (or any non-assistant) message: tool_result blocks become
            # internal tool messages; remaining text becomes a user message.
            text_parts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    out.append({
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id") or "unknown",
                        "content": _stringify_content(b.get("content")),
                    })
                elif b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
            if text_parts:
                out.append({"role": "user", "content": "\n".join(text_parts)})
    return out


def _to_internal_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    """Anthropic tools ({name, description, input_schema}) -> internal flat spec
    ({name, description, parameters}) that llm_client's converters accept."""
    if not isinstance(tools, list) or not tools:
        return None
    return [{"name": t.get("name", ""),
             "description": t.get("description", ""),
             "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}
            for t in tools if isinstance(t, dict)]


def _estimate_tokens(system: Any, messages: List[Dict[str, Any]]) -> int:
    """Rough input-token estimate (~4 chars/token) to drive routing (complexity
    tier + minimum context window)."""
    chars = len(_system_text(system))
    for m in (messages or []):
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    chars += len(json.dumps(b))
    return max(1, chars // 4)


def _build_requirements(system: Any, messages: List[Dict[str, Any]],
                        has_tools: bool, max_tokens: int) -> LlmRequirements:
    est_in = _estimate_tokens(system, messages)
    if est_in > 16000:
        complexity = "large"
    elif est_in > 3000 or has_tools:
        complexity = "medium"
    else:
        complexity = "small"
    # Ask the picker for a model whose context window fits input + requested
    # output headroom (select_model applies its own 1.25x margin on top).
    min_ctx = est_in + max(256, int(max_tokens or 1024))
    return LlmRequirements(complexity=complexity, needs_tools=has_tools,
                           min_context_tokens=min_ctx)


# ── Response translation (internal -> Anthropic) ────────────────────────────
def _to_anthropic_content(result: Any) -> Tuple[List[Dict[str, Any]], str]:
    """Turn call_llm's return (a text string, or {"text", "tool_calls"}) into
    Anthropic content blocks + a stop_reason."""
    blocks: List[Dict[str, Any]] = []
    stop_reason = "end_turn"
    if isinstance(result, dict):
        text = result.get("text") or ""
        tool_calls = result.get("tool_calls") or []
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            blocks.append({"type": "tool_use",
                           "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:8]}",
                           "name": fn.get("name") or "",
                           "input": args or {}})
        if tool_calls:
            stop_reason = "tool_use"
    else:
        blocks.append({"type": "text", "text": result or ""})
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks, stop_reason


def _message_envelope(msg_id: str, model: str, blocks: List[Dict[str, Any]],
                      stop_reason: str, in_tok: int, out_tok: int) -> Dict[str, Any]:
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _stream_events(envelope: Dict[str, Any]):
    """Emit a valid Anthropic SSE sequence for an already-computed message.
    Non-incremental (one delta per block) — the upstream result is complete
    before streaming begins — but protocol-correct for streaming clients."""
    msg_id = envelope["id"]
    blocks = envelope["content"]
    in_tok = envelope["usage"]["input_tokens"]
    out_tok = envelope["usage"]["output_tokens"]

    start_msg = {**envelope, "content": [],
                 "stop_reason": None,
                 "usage": {"input_tokens": in_tok, "output_tokens": 0}}
    yield _sse("message_start", {"type": "message_start", "message": start_msg})

    for i, block in enumerate(blocks):
        if block.get("type") == "tool_use":
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": i,
                "content_block": {"type": "tool_use", "id": block["id"],
                                  "name": block["name"], "input": {}}})
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": i,
                "delta": {"type": "input_json_delta",
                          "partial_json": json.dumps(block.get("input") or {})}})
        else:
            yield _sse("content_block_start", {
                "type": "content_block_start", "index": i,
                "content_block": {"type": "text", "text": ""}})
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": i,
                "delta": {"type": "text_delta", "text": block.get("text", "")}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": i})

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": envelope["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": out_tok}})
    yield _sse("message_stop", {"type": "message_stop"})


# ── Routes ──────────────────────────────────────────────────────────────────
@router.post("/v1/messages")
async def messages(request: Request):
    if not _authorized(request):
        return JSONResponse(status_code=401, content={
            "type": "error",
            "error": {"type": "authentication_error",
                      "message": "Missing or invalid api key (x-api-key / Authorization: Bearer)."}})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Body is not valid JSON."}})

    system = body.get("system")
    a_messages = body.get("messages") or []
    a_tools = body.get("tools")
    want_stream = bool(body.get("stream"))
    try:
        max_tokens = int(body.get("max_tokens") or 1024)
    except Exception:
        max_tokens = 1024

    icfg = _interactive_config()

    # ── Agentic mode ──────────────────────────────────────────────────────
    # model=ab-agent (or llm_proxy_agentic_default): run AppBuilder's own
    # server-side agent loop with CHAT_TOOLS instead of a single passthrough, so
    # the caller gets an agent that can investigate the repo and (with autofix
    # enabled) trigger the real fix pipeline.
    if _wants_agentic(body, icfg):
        try:
            final_text, task_id = await asyncio.wait_for(
                asyncio.to_thread(_run_agentic, system, a_messages, icfg),
                timeout=_proxy_deadline(icfg))
        except asyncio.TimeoutError:
            logger.warning("LLM proxy agentic run exceeded %ss deadline", _proxy_deadline(icfg))
            return JSONResponse(status_code=504, content={
                "type": "error",
                "error": {"type": "api_error",
                          "message": ("Agentic routing timed out before the agent "
                                      "finished. Increase LLM_PROXY_DEADLINE or narrow "
                                      "the request.")}})
        except Exception as e:  # noqa: BLE001
            logger.exception("LLM proxy agentic run failed")
            return JSONResponse(status_code=502, content={
                "type": "error",
                "error": {"type": "api_error", "message": f"Agentic routing failed: {e}"}})

        blocks = [{"type": "text", "text": final_text}]
        in_tok = _estimate_tokens(system, a_messages)
        out_tok = max(1, len(final_text) // 4)
        model_label = _PROXY_AGENT_MODEL_ID
        msg_id = f"msg_{uuid.uuid4().hex}"
        logger.info("LLM proxy: agentic run %s complete (stream=%s)", task_id, want_stream)
        envelope = _message_envelope(msg_id, model_label, blocks, "end_turn", in_tok, out_tok)
        if want_stream:
            return StreamingResponse(_stream_events(envelope), media_type="text/event-stream")
        return JSONResponse(content=envelope)

    internal_msgs = _to_internal_messages(system, a_messages)
    internal_tools = _to_internal_tools(a_tools)
    reqs = _build_requirements(system, a_messages, bool(internal_tools), max_tokens)

    used: Dict[str, Any] = {}
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                call_llm, prompt="", task_id=f"proxy-{uuid.uuid4().hex[:8]}",
                messages=internal_msgs, tools=internal_tools, stream=False,
                requirements=reqs, used_model_out=used, config=icfg),
            timeout=_proxy_deadline(icfg))
    except asyncio.TimeoutError:
        logger.warning("LLM proxy routing exceeded %ss deadline", _proxy_deadline(icfg))
        return JSONResponse(status_code=504, content={
            "type": "error",
            "error": {"type": "api_error",
                      "message": ("Upstream LLM routing timed out — no endpoint "
                                  "responded within the interactive deadline. Check "
                                  "that a configured endpoint is reachable and its "
                                  "model is loaded.")}})
    except Exception as e:  # noqa: BLE001 - surface as an API error, never 500-crash
        logger.exception("LLM proxy call_llm failed")
        return JSONResponse(status_code=502, content={
            "type": "error",
            "error": {"type": "api_error", "message": f"Upstream LLM routing failed: {e}"}})

    blocks, stop_reason = _to_anthropic_content(result)
    in_tok = _estimate_tokens(system, a_messages)
    out_tok = max(1, sum(len(b.get("text", "")) for b in blocks) // 4)
    model_label = (f"{used.get('provider')}/{used.get('model')}"
                   if used.get("model") else _PROXY_MODEL_ID)
    msg_id = f"msg_{uuid.uuid4().hex}"
    logger.info("LLM proxy: routed to %s (stream=%s, tools=%s, stop=%s)",
                model_label, want_stream, bool(internal_tools), stop_reason)

    envelope = _message_envelope(msg_id, model_label, blocks, stop_reason, in_tok, out_tok)
    if want_stream:
        return StreamingResponse(_stream_events(envelope), media_type="text/event-stream")
    return JSONResponse(content=envelope)


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Anthropic's token-count endpoint. Claude Code calls it before some
    requests; return a size estimate so those calls don't 404."""
    if not _authorized(request):
        return JSONResponse(status_code=401, content={
            "type": "error",
            "error": {"type": "authentication_error", "message": "Missing or invalid api key."}})
    try:
        body = await request.json()
    except Exception:
        body = {}
    est = _estimate_tokens(body.get("system"), body.get("messages") or [])
    return JSONResponse(content={"input_tokens": est})


@router.get("/v1/models")
async def models(request: Request):
    """Minimal model list so clients that enumerate models get one coherent id.
    Routing is decided per-request by AppBuilder regardless of the id chosen."""
    if not _authorized(request):
        return JSONResponse(status_code=401, content={
            "type": "error",
            "error": {"type": "authentication_error", "message": "Missing or invalid api key."}})
    now = int(time.time())
    return JSONResponse(content={"data": [
        {"type": "model", "id": _PROXY_MODEL_ID, "display_name": "AppBuilder Router",
         "created_at": now}]})
