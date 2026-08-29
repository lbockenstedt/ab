"""FastAPI HTTP routes exposed via an APIRouter, included by main.app (extracted from main.py)."""
import asyncio, git, json, os, re, shutil, threading, time, traceback, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from github import Github
from app_state import mark_pr_approved, update_pr_review
from pr_actions import approve_pr, merge_pr
from branch_policy import parse_names as parse_branch_names
from fastapi import APIRouter

router = APIRouter()

from main import (
    CHAT_CONFIG_DEFAULTS,
    CONFIG_DIR,
    ENV_FILE,
    STARTUP_STAMP_FILE,
    _PROVIDER_CREDIT_CB,
    _PROVIDER_CREDIT_CB_LOCK,
    _apply_closed_label,
    _chat_lock,
    _diag_origin_head,
    _diag_origin_version,
    _fetch_models_for_provider,
    _get_hub_agent_client,
    _get_provider_config,
    _ALL_SLOTS,
    _get_provider_rpm,
    _log_restart_event,
    _persist_config_key,
    _provider_configured,
    _provider_credit_cb_snapshot,
    _reset_llm_semaphore,
    _schedule_check,
    _start_hub_agent,
    _task_state_lock,
    _trigger_spoke_updates,
    app,
    append_chat_message,
    check_for_updates,
    clean_repo_name,
    create_conversation,
    delete_conversation,
    get_conversation,
    get_hub_logs,
    get_log_path,
    get_version,
    load_chats,
    load_config,
    load_processed,
    recompute_issue_counters,
    load_update_state,
    logger,
    parse_module_repo_map,
    process_single_issue,
    rename_conversation,
    run_chat_reply,
    run_local_llm_setup,
    run_local_llm_pull,
    _ollama_models_detailed,
    _ollama_delete,
    OLLAMA_BASE_URL,
    run_scan_cycle,
    save_chats,
    save_config,
    save_processed,
    set_active_chat,
    state,
    templates,
    update_task_state,
    validate_llm_config_on_startup,
)


@router.get("/api/hub-agent/status")
async def hub_agent_status():
    """Current Hub agent connection/approval status + config (for the Settings UI badge)."""
    cfg = load_config()
    return JSONResponse({
        "status": state.get("hub_agent_status", "not_registered"),
        "connected": bool(state.get("hub_agent_connected", False)),
        "last_disconnect": state.get("hub_agent_last_disconnect", ""),
        "message": state.get("hub_agent_message", ""),
        "last_seen": state.get("hub_agent_last_seen", ""),
        "hub_ws_url": (cfg.get("HUB_WS_URL") or "").strip(),
        "hub_agent_id": (cfg.get("HUB_AGENT_ID") or "ab").strip(),
        "has_secret": bool((cfg.get("HUB_AGENT_SECRET") or "").strip()),
        "has_hub_secret": bool((cfg.get("HUB_SECRET") or "").strip()),
    })


@router.post("/api/hub-agent/reregister")
async def hub_agent_reregister():
    """Force re-onboarding: clear the stored session secret and restart the agent.

    Used after a key revoke/re-approval, or to re-trigger the pending-approval
    flow. Returns immediately; the agent reconnects zero-touch in the background.
    """
    try:
        _persist_config_key("HUB_AGENT_SECRET", "")
        client = _get_hub_agent_client()
        if client:
            try:
                client.stop()
            except Exception:
                pass
        # Brief pause so the old loop closes before we start a fresh one.
        time.sleep(1)
        _start_hub_agent()
        return JSONResponse({"status": "restarted", "message": "Hub agent re-registering (approve ab in the Hub WebUI)"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ── Auth: login / logout / first-run setup ───────────────────────────────────
# These paths are in main.py's middleware exempt list; everything else requires a
# session. Note /api/health is exempt too — the watchdog polls it to verify a
# restart, and gating it would make every restart look like a failure.

@router.get("/login")
async def login_page(request: Request, next: str = "", error: str = ""):
    import auth as _a
    if not _a.any_users():
        return RedirectResponse("/setup-admin", status_code=303)
    if _a.verify_session(request.cookies.get(_a.SESSION_COOKIE) or ""):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={
        "setup_mode": False, "error": error, "next_url": next,
        "min_len": _a.MIN_PASSWORD_LEN})


@router.post("/login")
async def login_submit(request: Request):
    import auth as _a
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    nxt = (form.get("next") or "/").strip() or "/"
    # Only ever redirect to a path on THIS host — a user-supplied absolute URL
    # would turn the login form into an open redirect.
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    src = (request.client.host if request.client else "?")
    if not _a.verify_credentials(username, password):
        logger.warning("auth: failed login for %r from %s", username, src)
        return templates.TemplateResponse(
            request=request, name="login.html", status_code=401,
            context={"setup_mode": False, "error": "Invalid username or password.",
                     "next_url": nxt, "min_len": _a.MIN_PASSWORD_LEN})
    logger.info("auth: %r signed in from %s", username, src)
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(_a.SESSION_COOKIE, _a.issue_session(username),
                    max_age=_a.SESSION_TTL_S, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")
    return resp


@router.get("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    import auth as _a
    resp.delete_cookie(_a.SESSION_COOKIE, path="/")
    return resp


@router.get("/setup-admin")
async def setup_admin_page(request: Request, error: str = ""):
    """First-run account creation. 404s once ANY account exists, so it is a
    one-shot bootstrap rather than a permanent unauthenticated endpoint."""
    import auth as _a
    if _a.any_users():
        raise HTTPException(status_code=404, detail="Setup already completed")
    return templates.TemplateResponse(request=request, name="login.html", context={
        "setup_mode": True, "error": error, "next_url": "",
        "min_len": _a.MIN_PASSWORD_LEN})


@router.post("/setup-admin")
async def setup_admin_submit(request: Request):
    import auth as _a
    if _a.any_users():
        raise HTTPException(status_code=404, detail="Setup already completed")
    form = await request.form()
    username = (form.get("username") or "").strip()
    p1, p2 = form.get("password") or "", form.get("password2") or ""

    def _again(msg):
        return templates.TemplateResponse(
            request=request, name="login.html", status_code=400,
            context={"setup_mode": True, "error": msg, "next_url": "",
                     "min_len": _a.MIN_PASSWORD_LEN})

    if p1 != p2:
        return _again("Passwords do not match.")
    ok, msg = _a.create_user(username, p1)
    if not ok:
        return _again(msg)
    logger.warning("auth: FIRST ACCOUNT %r created from %s — setup is now closed.",
                   username, request.client.host if request.client else "?")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(_a.SESSION_COOKIE, _a.issue_session(username),
                    max_age=_a.SESSION_TTL_S, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")
    return resp


# ── Auth: Azure Entra ID (OIDC) single sign-on ──────────────────────────────
# These three endpoints are in main.py's middleware exempt list (a user has no
# session yet while signing in). /setup/oidc-config requires a session (admin).

def _sso_error_page(message: str, status: int = 400):
    """A styled HTML page for an SSO failure (browsers land here mid-redirect,
    so a JSON body would be unreadable)."""
    from fastapi.responses import HTMLResponse
    safe = (message or "Single sign-on failed.").replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign-in failed — AppBuilder</title><script src="https://cdn.tailwindcss.com"></script>
<style>body{{background:#0f172a}}.card{{background:#fff;border-radius:.75rem;
box-shadow:0 20px 45px rgba(0,0,0,.35)}}.btn{{background:#01A982}}.btn:hover{{background:#018f6f}}</style>
</head><body class="min-h-screen flex items-center justify-center p-6">
<div class="card w-full max-w-md p-8">
<h1 class="text-xl font-bold text-[#263040] mb-2">Sign-in failed</h1>
<div class="mb-5 p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">{safe}</div>
<a href="/login" class="btn inline-block py-2 px-4 rounded-lg text-white font-bold text-sm">Back to sign in</a>
</div></body></html>"""
    return HTMLResponse(content=html, status_code=status)


@router.get("/auth/oidc/enabled")
async def oidc_enabled():
    """Cheap probe the login page uses to decide whether to show the Microsoft
    button. ``ready`` means enough is configured for a login to succeed."""
    import oidc as _o
    cfg = _o.get_oidc_config()
    return {"enabled": bool(cfg.enabled and cfg.ready)}


@router.get("/auth/oidc/login")
async def oidc_login(request: Request):
    """Mint PKCE + state + nonce, stash them in a signed cookie, and redirect the
    browser to Entra's authorize endpoint."""
    import oidc as _o
    cfg = _o.get_oidc_config()
    if not (cfg.enabled and cfg.ready):
        return _sso_error_page("Microsoft sign-in is not configured on this server.")
    try:
        doc = await _o.discover(cfg)
        state, nonce, verifier, challenge = _o.new_pkce()
        url = _o.authorize_url(cfg, doc, state, nonce, challenge)
    except Exception as e:  # noqa: BLE001
        logger.exception("oidc: could not start login")
        return _sso_error_page(f"Could not start Microsoft sign-in: {e}", status=502)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(_o.STATE_COOKIE, _o.sign_state_cookie(state, nonce, verifier),
                    max_age=_o._STATE_TTL_S, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")
    return resp


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, code: str = "", state: str = "",
                        error: str = "", error_description: str = ""):
    """Validate the state cookie, exchange the code, verify the id-token, enforce
    the optional group gate, provision the user, and open a session."""
    import oidc as _o
    if error:
        return _sso_error_page(f"Microsoft returned an error: {error_description or error}")
    cfg = _o.get_oidc_config()
    if not (cfg.enabled and cfg.ready):
        return _sso_error_page("Microsoft sign-in is not configured on this server.")
    cookie = request.cookies.get(_o.STATE_COOKIE) or ""
    parsed = _o.verify_state_cookie(cookie)
    if not parsed:
        return _sso_error_page("Sign-in expired or the state was tampered with. "
                               "Please try again.")
    exp_state, nonce, verifier = parsed
    if not code or not _o.hmac_eq(state, exp_state):
        return _sso_error_page("State mismatch — sign-in cannot be trusted. Please try again.")
    try:
        doc = await _o.discover(cfg)
        tokens = await _o.exchange_code(cfg, doc, code, verifier)
        id_token = tokens.get("id_token")
        if not id_token:
            raise _o.OidcError("token response had no id_token")
        jwks = await _o.fetch_jwks(doc.get("jwks_uri"))
        claims = _o.verify_id_token(cfg, id_token, nonce, jwks)
        _o.enforce_allowed_group(cfg.allowed_group, _o.extract_member_groups(claims))
        username = _o.provision_entra_user(claims)
    except _o.OidcError as e:
        logger.warning("oidc: login rejected — %s", e)
        return _sso_error_page(str(e), status=403)
    except Exception as e:  # noqa: BLE001
        logger.exception("oidc: callback failed")
        return _sso_error_page(f"Microsoft sign-in failed: {e}", status=502)
    import auth as _a
    src = (request.client.host if request.client else "?")
    logger.info("auth: %r signed in via Entra from %s", username, src)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(_a.SESSION_COOKIE, _a.issue_session(username),
                    max_age=_a.SESSION_TTL_S, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")
    resp.delete_cookie(_o.STATE_COOKIE, path="/")
    return resp


@router.get("/setup/oidc-config")
async def oidc_config_get():
    """Return the stored OIDC config (secret redacted) for the Settings UI."""
    import oidc as _o
    cfg = _o.get_oidc_config()
    try:
        from config_store import load_config
        stored = (load_config() or {}).get("oidc", {}) or {}
    except Exception:  # noqa: BLE001
        stored = {}
    out = _o.redact_config(stored)
    out["ready"] = bool(cfg.ready)
    return out


@router.get("/setup/oidc/groups")
async def oidc_groups_list():
    """List the tenant's Entra groups (id + displayName) for the allowed-group
    picker, so an admin chooses a group by NAME instead of pasting a GUID.

    ``allowed_group`` only ever matches group OBJECT IDs, so hand-entry is the
    main way to lock every user out of AppBuilder; the picker removes that.
    Degrades to ``{groups: [], warning}`` rather than erroring so the UI can
    fall back to manual entry. Behind the normal session gate (``/setup/*`` is
    not in the auth-exempt list)."""
    import oidc as _o
    cfg = _o.get_oidc_config()
    if not (cfg.tenant_id and cfg.client_id):
        return {"groups": [],
                "warning": "Set the tenant ID and client ID first, then save."}
    if not cfg.has_credential:
        return {"groups": [],
                "warning": "Set a client secret or certificate first, then save."}

    def _friendly(msg: str) -> str:
        low = msg.lower()
        if ("authorization_requestdenied" in low or "insufficient privileges" in low
                or " 403" in low):
            return ("The app registration is missing the Microsoft Graph "
                    "Group.Read.All (Application) permission with admin consent "
                    "— add it under API permissions, grant admin consent, then "
                    "retry. You can still type the group object ID by hand. "
                    "(raw: " + msg[:160] + ")")
        if " 401" in low or "invalid_client" in low:
            return ("Entra rejected the app token — check the client secret, or "
                    "that the certificate is uploaded and its thumbprint "
                    "matches. (raw: " + msg[:160] + ")")
        return msg

    try:
        groups = await _o.fetch_directory_groups(cfg)
        groups.sort(key=lambda g: (g.get("displayName") or "").lower())
        return {"groups": groups}
    except _o.OidcError as e:
        return {"groups": [], "warning": _friendly(str(e))}
    except Exception as e:  # noqa: BLE001
        logger.exception("oidc: could not list directory groups")
        return {"groups": [], "warning": _friendly(str(e))}


@router.post("/setup/oidc-config")
async def oidc_config_set(request: Request):
    """Persist the operator-editable OIDC config from the Settings UI."""
    import oidc as _o
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        stored = _o.save_oidc_config(body or {})
    except Exception as e:  # noqa: BLE001
        logger.exception("oidc: could not save config")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "config": stored}


# ── Auth: account management (every account is an admin) ─────────────────────

@router.get("/api/auth/users")
async def auth_list_users():
    import auth as _a
    return {"users": _a.list_users()}


@router.post("/api/auth/users")
async def auth_create_user(request: Request):
    import auth as _a
    data = await request.json()
    ok, msg = _a.create_user((data.get("username") or "").strip(), data.get("password") or "")
    return {"status": "ok" if ok else "error", "message": msg}


@router.post("/api/auth/password")
async def auth_set_password(request: Request):
    import auth as _a
    data = await request.json()
    ok, msg = _a.set_password((data.get("username") or "").strip(), data.get("password") or "")
    return {"status": "ok" if ok else "error", "message": msg}


@router.post("/api/auth/users/delete")
async def auth_delete_user(request: Request):
    import auth as _a
    data = await request.json()
    username = (data.get("username") or "").strip()
    me = getattr(request.state, "user", None)
    if username and username == me:
        # Deleting the account you are signed in as would log you out mid-action
        # and is almost never what was meant.
        return {"status": "error", "message": "You cannot delete the account you are signed in as."}
    ok, msg = _a.delete_user(username)
    return {"status": "ok" if ok else "error", "message": msg}


@router.get("/api/health")
async def health_check():
    """Heartbeat endpoint for the watchdog service."""
    return {"status": "ok"}


@router.post("/api/llm/diag")
async def llm_diag_run(request: Request):
    """Dry-run the model picker for every requirement preset and report which
    endpoint it would choose, and why every other one is an alternative or
    excluded — WITHOUT spending a token.

    Routing is capability/cost-aware over the enumerated endpoint set (the 8
    provider slots are gone), so "can slot N answer a prompt" is no longer the
    useful question. This reports, purely, the ranked resolution the live picker
    (model_selection.select_model) produces for each preset — the same inputs,
    no network calls — making real routing legible and auditable for free.

    Body (all optional): ``preset`` (restrict to one preset label; default all),
    ``overrides`` (dict of LlmRequirements field overrides applied to every
    preset — lets the UI build a custom requirement set).

    Free and fast (the picker is pure), but still run off-thread since
    _enumerate_candidates reads config/live env.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — body is optional
        body = {}
    body = body or {}
    preset = body.get("preset") or None
    overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else None
    try:
        from main import llm_diag  # re-exported from llm_client
        started = time.time()
        report = await asyncio.to_thread(llm_diag, preset=preset, overrides=overrides)
        presets = report.get("presets") or []
        resolved = sum(1 for p in presets if p.get("selected"))
        return {
            "presets": presets,
            "candidate_count": report.get("candidate_count", 0),
            "summary": {"resolved": resolved, "total": len(presets),
                        "candidates": report.get("candidate_count", 0),
                        "elapsed_ms": int((time.time() - started) * 1000)},
        }
    except Exception as e:  # noqa: BLE001 — a diag must report, never 500
        logger.error(f"llm_diag failed: {e}")
        return JSONResponse(status_code=200,
                            content={"presets": [], "summary": None, "error": str(e)[:400]})


@router.post("/api/toggle-pause")
async def toggle_pause():
    state["paused"] = not state["paused"]
    logger.info(f"AppBuilder autonomous operations {'PAUSED' if state['paused'] else 'RESUMED'}")
    return {"status": "success", "paused": state["paused"]}


@router.post("/api/toggle-blackout")
async def toggle_blackout():
    state["blackout"] = not state.get("blackout", False)
    logger.info(f"AppBuilder blackout mode {'ON (triage only)' if state['blackout'] else 'OFF (fixes resumed)'}")
    return {"status": "success", "blackout": state["blackout"]}


@router.post("/api/pr-review/approve")
async def pr_review_approve(request: Request):
    """Human 'Approve' for a pre-reviewed PR (from the PRs Reviewed list). Adds a
    'ab-approved' label + an approval comment and flags it in state. Does
    NOT merge — the human merges/pulls after. This is the human-click path;
    the ONLY other caller of approve_pr is feature auto-drive's own narrow,
    opt-in auto-merge exception (pr_review._maybe_auto_merge — see that
    module's docstring), which never touches human-authored PRs."""
    try:
        data = await request.json()
        repo_name = (data.get("repo") or "").strip()
        number = int(data.get("number"))
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "repo + number required"})
    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No GitHub token configured"})

    def _do_approve():
        # Shared with feature auto-drive's auto-merge path (pr_review.py) —
        # see pr_actions.py's module docstring for why there is exactly one
        # approve implementation, not two.
        approve_pr(Github(token), repo_name, number, actor="human")

    try:
        # PyGithub is synchronous — every call in _do_approve is a blocking
        # HTTP request to the GitHub API. Running it inline on this coroutine
        # blocked the ENTIRE app (uvicorn runs single-process/single-event-
        # loop, no workers=) for however long GitHub took to respond — same
        # class of bug as hub_logs_raw's request_sync. A slow GitHub response
        # (or a client-side timeout mid-stall) surfaced as a network failure
        # ("TypeError: Load failed" in Safari) rather than a slow success.
        await asyncio.get_event_loop().run_in_executor(None, _do_approve)
        mark_pr_approved(repo_name, number, True)
        logger.info("pr_review: %s #%s APPROVED via UI", repo_name, number)
        return {"status": "success", "repo": repo_name, "number": number}
    except Exception as e:  # noqa: BLE001
        logger.error("pr_review approve failed for %s#%s: %s", repo_name, number, e)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.post("/api/pr-review/merge")
async def pr_review_merge(request: Request):
    """Human 'Merge to Main' for a reviewed PR — merges it on GitHub now (the
    human's explicit action from the UI). This is the human-click path; the
    ONLY other caller of merge_pr is feature auto-drive's own narrow, opt-in
    auto-merge exception (pr_review._maybe_auto_merge — see that module's
    docstring), which never touches human-authored PRs and still goes
    through this SAME "must be Approved first" guard, not around it. Returns
    the GitHub error if the PR isn't mergeable (conflicts / required checks)."""
    try:
        data = await request.json()
        repo_name = (data.get("repo") or "").strip()
        number = int(data.get("number"))
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "repo + number required"})
    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No GitHub token configured"})

    def _do_merge():
        # Shared with feature auto-drive's auto-merge path — see pr_actions.py.
        return merge_pr(Github(token), repo_name, number)

    try:
        # See pr_review_approve's identical note: PyGithub is synchronous, so
        # every call inside _do_merge blocks — offload it so a slow GitHub
        # response can't stall the whole app.
        status_code, content = await asyncio.get_event_loop().run_in_executor(None, _do_merge)
        return content if status_code == 200 else JSONResponse(status_code=status_code, content=content)
    except Exception as e:  # noqa: BLE001
        logger.error("pr_review merge failed for %s#%s: %s", repo_name, number, e)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.post("/api/pr-review/deny")
async def pr_review_deny(request: Request):
    """Human 'Deny' for a reviewed PR — closes it on GitHub (no merge) + labels it
    'ab-denied' + a comment, and flags it DENIED in the list (kept, badged).
    Only a human denies; AppBuilder never does."""
    try:
        data = await request.json()
        repo_name = (data.get("repo") or "").strip()
        number = int(data.get("number"))
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "repo + number required"})
    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No GitHub token configured"})

    def _do_deny():
        gh = Github(token)
        repo = gh.get_repo(repo_name)
        pr = repo.get_pull(number)
        label = "ab-denied"
        try:
            repo.get_label(label)
        except Exception:
            try:
                repo.create_label(label, "B60205")
            except Exception:
                pass
        try:
            pr.add_to_labels(label)
        except Exception:
            pass
        try:
            pr.create_issue_comment("⛔ **Denied** via AppBuilder (human review) — closing without merge.")
        except Exception:
            pass
        if not pr.merged and pr.state == "open":
            pr.edit(state="closed")

    try:
        # See pr_review_approve's identical note: PyGithub is synchronous, so
        # every call inside _do_deny blocks — offload it so a slow GitHub
        # response can't stall the whole app.
        await asyncio.get_event_loop().run_in_executor(None, _do_deny)
        update_pr_review(repo_name, number, denied=True)
        logger.info("pr_review: %s #%s DENIED via UI", repo_name, number)
        return {"status": "success", "repo": repo_name, "number": number}
    except Exception as e:  # noqa: BLE001
        logger.error("pr_review deny failed for %s#%s: %s", repo_name, number, e)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


# ---------------------------------------------------------------------------
# Branch promotion (dev -> qa -> main), driven from the footer action bar.
#
# These endpoints only ever DISPATCH .github/workflows/promote.yml, which
# prepares a `promote/<src>-to-<tgt>` branch and OPENS a pull request. Nothing
# here merges, and nothing here pushes to a branch: the repo owner still
# reviews and merges every promotion PR. (AppBuilder's unattended auto-merge
# is a separate, opt-in path gated on `feature_automerge_target_branches` in
# pr_review._automerge_decision -- untouched by this feature.)
#
# PROMOTE_ROUTES is the allowlist. It is checked here as well as in promote.yml
# because AppBuilder's token is the repo owner's PAT and bypasses GitHub
# rulesets -- server-side validation is the enforcement, not a convenience.
# ---------------------------------------------------------------------------

# (source, target) -> is this the qa-skipping override?
PROMOTE_ROUTES = {
    ("dev", "qa"): False,
    ("qa", "main"): False,
    ("dev", "main"): True,   # override: skips qa, still only opens a PR
}


@router.get("/api/promote/repos")
async def promote_repos():
    """Repos the promotion buttons can target: everything AppBuilder already
    monitors, plus its own self-diagnosis repo, which is used as the default
    so the common case (promoting AppBuilder itself) needs no selection."""
    try:
        from github_ops import get_monitored_repos
        cfg = load_config()
        repos = sorted({clean_repo_name(r) for r in (get_monitored_repos(cfg) or []) if r})
        default = ""
        try:
            from main import resolve_self_diagnosis_repo
            default = clean_repo_name(resolve_self_diagnosis_repo(cfg) or "")
        except Exception:  # noqa: BLE001
            default = ""
        if default and default not in repos:
            repos.append(default)
        return {"status": "success", "repos": repos, "default": default or (repos[0] if repos else "")}
    except Exception as e:  # noqa: BLE001
        logger.error("promote: could not list repos: %s", e)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.post("/api/promote")
async def promote_branch(request: Request):
    """Dispatch promote.yml for one repo to OPEN a promotion PR.

    Body: {repo, source, target}. Only the three routes in PROMOTE_ROUTES are
    accepted; anything else is a 400 rather than a dispatch, so this endpoint
    can never be used to aim an arbitrary branch at main.
    """
    try:
        data = await request.json()
        repo_name = clean_repo_name((data.get("repo") or "").strip())
        source = (data.get("source") or "").strip().lower()
        target = (data.get("target") or "").strip().lower()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"status": "error", "message": "repo + source + target required"})

    if not repo_name:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No repository selected"})
    if (source, target) not in PROMOTE_ROUTES:
        allowed = ", ".join(f"{s} -> {t}" for s, t in PROMOTE_ROUTES)
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": f"'{source} -> {target}' is not a permitted promotion route (allowed: {allowed})"})

    is_override = PROMOTE_ROUTES[(source, target)]

    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse(status_code=400, content={"status": "error", "message": "No GitHub token configured"})

    def _do_dispatch():
        repo = Github(token).get_repo(repo_name)
        wf = repo.get_workflow("promote.yml")
        # workflow_dispatch always runs the workflow file as it exists on the
        # ref it is dispatched from; the promotion logic lives on the default
        # branch, which is also what promote.yml's concurrency note assumes.
        ref = repo.default_branch
        # Send `target` ONLY for the override. promote.yml in the sibling repos
        # still has just the `source` input, and GitHub rejects a dispatch that
        # carries an input the workflow does not declare -- so always sending
        # `target` would break the ordinary dev->qa / qa->main buttons on every
        # repo except this one. Omitting it reproduces the historical call
        # exactly, and those repos keep working; dev->main is the only route
        # that genuinely requires the newer workflow.
        inputs = {"source": source}
        if is_override:
            inputs["target"] = target
        ok = wf.create_dispatch(ref, inputs)
        # PyGithub returns False rather than raising when GitHub rejects the
        # dispatch. Treat that as a failure instead of reporting a false
        # success -- for the override the likeliest cause is a promote.yml on
        # that repo that predates the `target` input.
        if ok is False:
            extra = (" This repo's promote.yml may predate the 'target' input "
                     "that the dev -> main override requires.") if is_override else ""
            raise RuntimeError(
                f"GitHub rejected the workflow dispatch for {repo_name} (ref '{ref}').{extra}")
        return f"https://github.com/{repo_name}/actions/workflows/promote.yml"

    try:
        # PyGithub is synchronous — see pr_review_approve's note; offload so a
        # slow GitHub response can't stall the whole single-event-loop app.
        url = await asyncio.get_event_loop().run_in_executor(None, _do_dispatch)
        logger.warning(
            "promote: %s %s -> %s dispatched via UI%s",
            repo_name, source, target, " (OVERRIDE, skips qa)" if is_override else "")
        return {"status": "success", "repo": repo_name, "source": source, "target": target,
                "override": is_override, "url": url,
                "message": (f"Promotion {source} → {target} started for {repo_name}. "
                            f"It opens a pull request; it does not merge.")}
    except Exception as e:  # noqa: BLE001
        logger.error("promote failed for %s (%s -> %s): %s", repo_name, source, target, e)
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@router.post("/api/pr-review/reprocess")
async def pr_review_reprocess(request: Request):
    """Human 'Reprocess' for a reviewed PR — immediately re-runs the full
    pre-review (parity/secrets/lint findings + panel(s)) for this ONE PR,
    bypassing the head-SHA cache so a code change that doesn't move the PR's
    own head (a twin-repo fix, e.g.) or simple impatience with the poll
    cadence doesn't mean waiting up to an hour. Refuses on an already-merged
    PR — nothing to reprocess, the code is final. Runs off-thread (an LLM
    panel pass can take a while) so the request returns immediately."""
    try:
        data = await request.json()
        repo_name = (data.get("repo") or "").strip()
        number = int(data.get("number"))
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "repo + number required"})
    rec = (state.get("pr_reviews") or {}).get("%s#%s" % (repo_name, number)) or {}
    if rec.get("merged"):
        return JSONResponse(status_code=409, content={"status": "error",
            "message": f"PR #{number} is already merged — nothing to reprocess."})

    def run_reprocess():
        try:
            from pr_review import reprocess_one_pr
            reprocess_one_pr(repo_name, number)
            logger.info("pr_review: %s #%s reprocessed via UI", repo_name, number)
        except Exception as e:  # noqa: BLE001
            logger.error("pr_review reprocess failed for %s#%s: %s", repo_name, number, e)

    threading.Thread(target=run_reprocess, daemon=True).start()
    return {"status": "triggered", "message": f"Reprocessing {repo_name}#{number}…"}


@router.post("/api/pr-review/fix")
async def pr_review_fix(request: Request):
    """Human 'Fix' for a reviewed PR — the ONLY trigger for this; AppBuilder never
    applies a PR fix on its own. Generates an AI fix for this PR's pre-review
    findings, gates it through the same skeptical-panel review the bug/issue fix
    pipeline uses, and on approval pushes it as a new commit onto the PR's OWN
    branch (never a new branch/PR). Refuses on an already-merged/closed PR.
    Runs off-thread (LLM fix + review + QA verify can take a while); result is
    posted as a PR comment and the review is reprocessed automatically, so the
    UI picks it up on the next refresh."""
    try:
        data = await request.json()
        repo_name = (data.get("repo") or "").strip()
        number = int(data.get("number"))
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "repo + number required"})
    rec = (state.get("pr_reviews") or {}).get("%s#%s" % (repo_name, number)) or {}
    if rec.get("merged"):
        return JSONResponse(status_code=409, content={"status": "error",
            "message": f"PR #{number} is already merged — nothing to fix."})

    def run_fix():
        try:
            from pr_review import fix_one_pr
            ok, message = fix_one_pr(repo_name, number)
            logger.info("pr_review: %s #%s fix %s — %s", repo_name, number,
                        "applied" if ok else "not applied", message)
        except Exception as e:  # noqa: BLE001
            logger.error("pr_review fix failed for %s#%s: %s", repo_name, number, e)

    threading.Thread(target=run_fix, daemon=True).start()
    return {"status": "triggered", "message": f"Generating a fix for {repo_name}#{number}…"}


@router.get("/")
async def dashboard(request: Request):
    recent_processed = {}
    if state["processed"]:
        now = datetime.now()
        for issue_id, info in state["processed"].items():
            try:
                ts = datetime.fromisoformat(info.get("timestamp", "{}"))
                if (now - ts).days < 7:
                    recent_processed[issue_id] = info
            except:
                recent_processed[issue_id] = info

    # Sort PR reviews by priority: Not Approved -> Approved -> Terminal (Merged/Denied)
    # Stable two-pass sort to maintain recency within each group.
    pr_items = list((state.get("pr_reviews") or {}).items())
    pr_items.sort(key=lambda x: x[1].get("reviewed_at", ""), reverse=True)

    def get_priority(item):
        pr = item[1]
        if pr.get("merged") or pr.get("denied"):
            return 2
        if not pr.get("approved"):
            return 0
        return 1

    pr_items.sort(key=get_priority)

    return templates.TemplateResponse(request=request, name="index.html", context={
        "view": "status",
        "state": {**state, "processed": recent_processed},
        "sorted_pr_reviews": pr_items
    })


@router.get("/api/task-details")
async def get_task_details(task_id: str = None):
    if task_id:
        if task_id not in state["active_tasks"]:
            return JSONResponse(status_code=404, content={"error": "Task not found or no longer active"})

        task = state["active_tasks"][task_id]
        duration = datetime.now() - task["start_time"]
        seconds = int(duration.total_seconds())
        duration_str = f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s"

        return {
            "status": state["status"],
            "task": task["name"],
            "duration": duration_str,
            "stream": task["stream"]
        }

    return {
        "active_tasks": state["active_tasks"],
        "count": len(state["active_tasks"])
    }


@router.get("/api/models")
async def get_models():
    """Fetches available models from both configured LLM providers."""
    config = load_config()
    p1_provider, p1_key, _, p1_url = _get_provider_config(1, config)
    p2_provider, p2_key, _, p2_url = _get_provider_config(2, config)
    # _fetch_models_for_provider makes a BLOCKING requests.get to the provider
    # (up to 10-15s each). Run inline, the two sequential calls froze the whole
    # single-process/single-event-loop app for up to ~30s — long enough for the
    # watchdog's 2s health probe to fail (→ rollback/flapping) and for the
    # browser fetch to time out mid-stall ("TypeError: Load failed"). Offload
    # both to threads and run them concurrently so the loop stays responsive.
    p1, p2 = await asyncio.gather(
        asyncio.to_thread(_fetch_models_for_provider, p1_provider, p1_key, p1_url),
        asyncio.to_thread(_fetch_models_for_provider, p2_provider, p2_key, p2_url),
    )
    return {
        "local_models": p1["models"],
        "cloud_models": p2["models"],
        "local_error": p1["error"],
        "cloud_error": p2["error"],
        "enabled_models": config.get("enabled_models", []),
    }


@router.post("/api/fetch-models")
async def fetch_models_live(request: Request):
    """Fetch available models for a provider.

    Accepts explicit api_key/base_url (for live testing), or just provider name
    to look up credentials already saved in the vault.
    """
    try:
        data = await request.json()
        provider = (data.get("provider") or "openai").strip()
        api_key = (data.get("api_key") or "").strip()
        base_url = (data.get("base_url") or "").strip()

        # If no key supplied, try the vault.
        if not api_key:
            cfg = load_config()
            cred = (cfg.get("llm_credentials") or {}).get(provider.lower()) or {}
            api_key = (cred.get("api_key") or "").strip()
            base_url = base_url or (cred.get("base_url") or "").strip()

        # Blocking requests.get — offload so a slow/unreachable provider can't
        # freeze the event loop (see get_models' note; that stall surfaced as
        # "TypeError: Load failed" when enabling an LLM in Settings).
        result = await asyncio.to_thread(_fetch_models_for_provider, provider, api_key, base_url)
        return {"models": result["models"], "error": result["error"]}
    except Exception as e:
        logger.error(f"fetch-models error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e), "models": []})


@router.get("/api/scheduler/status")
async def scheduler_status():
    config = load_config()
    return _schedule_check(config)


def _hub_connection_diag():
    """Hub-connection + mTLS cert state from the running HubAgentClient (module
    global), so the Diagnostics page can show whether log/update access works and
    whether the WebUI is still self-signed — the cert data we otherwise SSH for."""
    try:
        import hub_agent
        client = hub_agent.hub_agent_client
        if client is None:
            return {"available": False, "reason": "hub agent not started"}
        return client.cert_diagnostics()
    except Exception as e:  # noqa: BLE001 - diagnostics must never 500
        return {"available": False, "error": str(e)}


def _endpoint_perf_rows(config):
    """Per-endpoint perf + availability for the Diagnostics providers table and
    the Settings "Model Performance" panel, keyed by ModelKey
    (provider|base_url|model) rather than slot number — the picker ranks
    endpoints, not slots, and the same model on two boxes is two rows. Sourced
    from llm_perf.py's ModelKey-keyed snapshot (median tok/s + wire latency)
    joined against the live candidate enumeration, so credit/rate/dead-model
    exhaustion shows up as an explicit column instead of a silent skip. Never
    raises — a stats field must never 500 the page."""
    try:
        import llm_client
        import model_registry
        cands = llm_client._enumerate_candidates(config)
        perf = llm_client.get_llm_perf_snapshot()  # {(provider, base_url, model): {n, tps, latency_ms}}
    except Exception:
        return []
    rows = []
    for c in cands:
        p = perf.get(c.get("key")) or {}
        tps = p.get("tps")
        latency = p.get("latency_ms")
        rows.append({
            "key": "|".join((c.get("provider") or "", c.get("base_url") or "", c.get("model") or "")),
            "provider": c.get("provider"),
            "model": c.get("model"),
            "base_url": c.get("base_url"),
            "tier": (c.get("caps") or {}).get("cost_tier"),
            "speed": model_registry.speed_tier(c.get("caps") or {}),
            "capability_rank": model_registry.capability_rank(c.get("caps") or {}),
            "n": p.get("n") or 0,
            "tps": round(tps, 1) if tps is not None else None,
            "latency_ms": round(latency, 1) if latency is not None else None,
            "available": bool(c.get("available")),
            "exhausted": not c.get("available"),
            "exhausted_reason": c.get("unavailable_reason"),
        })
    # Fastest first; unmeasured endpoints sink to the bottom.
    rows.sort(key=lambda r: (r["tps"] is None, -(r["tps"] or 0.0)))
    return rows


def _llm_tps_table():
    """Per-model generation throughput, fastest first.

    Each entry: model, avg, min, max, n. Only models with at least one completed
    generation appear -- an entry with no samples would imply a measurement that
    never happened.
    """
    try:
        import main
        rows = []
        for model, samples in (main.state.get("llm_tps") or {}).items():
            if not samples:
                continue
            rows.append({
                "model": model,
                "avg": round(sum(samples) / len(samples), 1),
                "min": round(min(samples), 1),
                "max": round(max(samples), 1),
                "n": len(samples),
            })
        rows.sort(key=lambda r: r["avg"], reverse=True)
        return rows
    except Exception:  # noqa: BLE001 — a stats field must never 500 the endpoint
        return []


def _llm_tps_avg(model):
    """Mean tok/s over the recent generations recorded for *model*.

    Returns None when nothing has been measured yet, so the UI can show a dash
    rather than a misleading 0 -- "no data" and "zero throughput" are different
    states and conflating them would make a healthy idle box look broken.
    """
    try:
        import main
        samples = (main.state.get("llm_tps") or {}).get(str(model or ""), [])
        if not samples:
            return None
        return round(sum(samples) / len(samples), 1)
    except Exception:  # noqa: BLE001 — a stats field must never 500 the endpoint
        return None


def _system_stats():
    """Live host + process + LLM telemetry for the Diagnostics page. Never raises —
    every field degrades to None/[] so the panel can render partial data. psutil is
    used when present; load average + core count fall back to os primitives."""
    import time
    out = {"cpu": {}, "memory": {}, "disk": {}, "processes": [], "llm": {}, "uptime": {}}
    try:
        out["cpu"]["cores"] = os.cpu_count()
    except Exception:
        pass
    try:
        la = os.getloadavg()  # 1/5/15 min; unavailable on some platforms
        out["cpu"]["load_avg"] = [round(x, 2) for x in la]
        if out["cpu"].get("cores"):
            out["cpu"]["load_pct_1m"] = round(100.0 * la[0] / out["cpu"]["cores"], 1)
    except Exception:
        pass

    try:
        import psutil
    except Exception:
        psutil = None

    if psutil is not None:
        try:
            out["cpu"]["percent"] = psutil.cpu_percent(interval=0.15)
        except Exception:
            pass
        try:
            vm = psutil.virtual_memory()
            out["memory"] = {
                "total_gb": round(vm.total / 1073741824, 1),
                "used_gb": round((vm.total - vm.available) / 1073741824, 1),
                "available_gb": round(vm.available / 1073741824, 1),
                "percent": vm.percent,
            }
        except Exception:
            pass
        try:
            du = psutil.disk_usage(os.getcwd())
            out["disk"] = {
                "total_gb": round(du.total / 1073741824, 1),
                "used_gb": round(du.used / 1073741824, 1),
                "free_gb": round(du.free / 1073741824, 1),
                "percent": du.percent,
            }
        except Exception:
            pass
        try:
            boot = psutil.boot_time()
            out["uptime"]["host_seconds"] = int(time.time() - boot)
        except Exception:
            pass
        # Processes of interest: this app + any ollama/llm runtimes.
        try:
            me = psutil.Process().pid
            procs = []
            for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "cpu_percent", "create_time"]):
                try:
                    nm = (p.info.get("name") or "").lower()
                    cmd = " ".join(p.info.get("cmdline") or []).lower()
                    is_me = p.info["pid"] == me
                    if not (is_me or "ollama" in nm or "ollama" in cmd
                            or "ab" in cmd or "llama" in nm):
                        continue
                    mi = p.info.get("memory_info")
                    procs.append({
                        "pid": p.info["pid"],
                        "name": p.info.get("name"),
                        "label": "ab" if is_me else (p.info.get("name") or "proc"),
                        "rss_mb": round(mi.rss / 1048576, 1) if mi else None,
                        "cpu_percent": p.info.get("cpu_percent"),
                        "self": is_me,
                    })
                except Exception:
                    continue
            out["processes"] = sorted(procs, key=lambda x: (x["rss_mb"] or 0), reverse=True)[:20]
        except Exception:
            pass

    # LLM endpoints — provider/model + availability + throughput, keyed by
    # ModelKey (the picker ranks endpoints, not slots). Exhausted endpoints
    # (credit/rate cooldown, dead model) stay visible with a reason rather
    # than silently vanishing.
    try:
        cfg = load_config()
        perf_rows = _endpoint_perf_rows(cfg)
        active_model = state.get("active_llm")
        active_provider = state.get("active_llm_provider")
        active_row = next(
            (r for r in perf_rows
             if r["model"] == active_model
             and (active_provider is None or r["provider"] == active_provider)),
            None,
        )
        out["llm"] = {
            "endpoints": perf_rows,
            "circuit_breaker": state.get("llm_circuit_breaker"),
            "active_llm": active_model,
            "active_llm_provider": active_provider,
            "active_llm_at": state.get("active_llm_at"),
            "daily_fixes_count": state.get("daily_fixes_count"),
            # Rolling generation throughput for the endpoint currently/last
            # serving, from the ModelKey-keyed perf snapshot (median tok/s over
            # completed generations only, so idle time never dilutes it).
            "tps_avg": (active_row or {}).get("tps"),
            "tps_samples": (active_row or {}).get("n") or 0,
            # EVERY configured endpoint, fastest first — the point is comparing
            # endpoints against each other on this box, which a single
            # active-endpoint figure cannot answer.
            "perf_by_key": perf_rows,
        }
    except Exception:
        pass

    # AppBuilder process uptime from the startup stamp.
    try:
        with open(STARTUP_STAMP_FILE, "r") as f:
            started = json.load(f).get("started_at")
        if started:
            out["uptime"]["started_at"] = started
    except Exception:
        pass
    return out


@router.get("/api/settings/module-repo-map/suggest")
def module_repo_map_suggest():
    """Suggest module_repo_map entries from the repos already being monitored.

    Only ALIASES are suggested. resolve_module_repo already auto-matches a module
    whose name equals a monitored repo's basename ("pxmx" -> "owner/pxmx"), so an
    entry for those is redundant clutter. The map earns its keep where the Hub's
    module_type differs from the repo name — firewall/opnsense, nac/cppm, hub/lm,
    ipam/netbox, directory/ldap, simulation/cs, certificates/le, storage/truenas.

    Repos are never invented: a type is only suggested when a MONITORED repo shares
    the basename of its known repo, and the owner is taken from that monitored repo
    rather than the hardcoded default — so a fork or a different org maps correctly.
    Existing entries are reported separately and never overwritten; the operator's
    own mapping always wins.
    """
    try:
        from main import MODULE_TYPE_REPO  # re-exported from log_scan
    except Exception as e:  # noqa: BLE001
        return {"error": f"module type map unavailable: {e}", "suggested": {}}
    cfg = load_config()
    monitored = [clean_repo_name(r) for r in (get_monitored_repos(cfg) or []) if r]
    by_base = {}
    for r in monitored:
        by_base.setdefault(str(r).split("/")[-1].lower(), r)

    existing = cfg.get("module_repo_map") or {}
    if not isinstance(existing, dict):
        existing = {}
    existing_keys = {str(k).strip().lower() for k in existing}

    suggested, kept, redundant, unmapped = {}, {}, [], []
    for mtype, default_repo in sorted(MODULE_TYPE_REPO.items()):
        base = str(default_repo).split("/")[-1].lower()
        mkey = str(mtype).strip().lower()
        if mkey in existing_keys:
            kept[mtype] = existing.get(mtype, existing.get(mkey))
            continue
        if mkey == base:
            # Auto-match already resolves this one; an entry would add nothing.
            redundant.append(mtype)
            continue
        target = by_base.get(base)
        if target:
            suggested[mtype] = target
        else:
            unmapped.append({"module_type": mtype, "needs_repo": default_repo})

    merged = dict(existing)
    merged.update(suggested)
    as_text = ", ".join(f"{k}={v}" for k, v in sorted(merged.items()))
    return {
        "suggested": suggested,
        "kept_existing": kept,
        "redundant_auto_matched": sorted(redundant),
        "unmapped": unmapped,
        "monitored_count": len(monitored),
        "merged_text": as_text,
    }


@router.get("/api/system-stats")
async def system_stats():
    """Host/process/LLM telemetry for the Diagnostics → System panel."""
    return _system_stats()


@router.get("/api/diagnostics")
async def diagnostics():
    """Surfaces running-vs-disk-vs-origin versions, stale-code state, per-provider
    status (including the previously-silent skip reasons), and update/restart state,
    so the user can see what is wrong from the UI instead of reading CLI logs."""
    config = load_config()

    # Startup stamp — which commit this process booted on.
    running_commit, running_version, started_at, pid, main_mtime = None, None, None, None, None
    try:
        with open(STARTUP_STAMP_FILE, "r") as f:
            stamp = json.load(f)
        running_commit = stamp.get("commit")
        if running_commit == "unknown":
            running_commit = None
        running_version = stamp.get("version")
        started_at = stamp.get("started_at")
        pid = stamp.get("pid")
        main_mtime = stamp.get("main_mtime")
    except Exception:
        pass

    # On-disk HEAD.
    disk_commit = None
    try:
        disk_commit = git.Repo(os.getcwd()).head.commit.hexsha
    except Exception:
        pass

    origin_commit = _diag_origin_head()

    update_state = load_update_state()
    update_pending_exists = os.path.exists(os.path.join(CONFIG_DIR, "update_pending"))

    # Resolve the last-known-good commit's VERSION so the UI can show a
    # version label instead of a raw commit SHA.
    lkg_commit = update_state.get("last_known_good_commit")
    lkg_version = None
    if lkg_commit:
        try:
            lkg_version = git.Repo(os.getcwd()).git.show(f"{lkg_commit}:VERSION").strip() or None
        except Exception:
            lkg_version = None

    providers = _endpoint_perf_rows(config)

    return {
        "versions": {
            "running": running_commit,
            "disk": disk_commit,
            "origin": origin_commit,
            "label": get_version(),
            "running_version": running_version,
            "disk_version": get_version(),
            "origin_version": _diag_origin_version(),
            "stale": bool(disk_commit and running_commit and disk_commit != running_commit),
        },
        "process": {"pid": pid, "started_at": started_at, "main_mtime": main_mtime},
        "update": {
            "pending": update_pending_exists,
            "restart_pending": bool(state.get("restart_pending")),
            "last_known_good_commit": update_state.get("last_known_good_commit"),
            "last_known_good_version": lkg_version,
            "failed_commits": update_state.get("failed_commits", []),
            "restart_log": state.get("restart_log", []),
        },
        "providers": providers,
        "watchdog_signal": update_pending_exists,
        "hub_connection": _hub_connection_diag(),
        "bug_ingest": state.get("bug_ingest", {}),
        "feature_ingest": state.get("feature_ingest", {}),
        "heartbeat": {
            "agent_status": state.get("hub_agent_status", "not_registered"),
            "approved": state.get("hub_agent_status") == "approved",
            "approved_at": state.get("hub_agent_approved_at", ""),
            "suppressed": bool(state.get("heartbeat_suppression")),
            "suppression_reason": (state.get("heartbeat_suppression") or {}).get("reason"),
            "suppression_at": (state.get("heartbeat_suppression") or {}).get("at"),
        },
    }


CLAUDE_INSTALL_REQUEST = os.path.join(CONFIG_DIR, "claude_install_request.json")
CLAUDE_INSTALL_STATUS = os.path.join(CONFIG_DIR, "claude_install_status.json")


def _request_claude_install():
    """Ask ab-watchdog to install the Claude Code CLI for the service user.

    This service is cap-locked and cannot escalate, so installation is delegated
    the same way ollama-setup is: drop a request file, the watchdog runs the root
    helper. The helper installs AS the service user because `claude` uses per-user
    session auth — a root-owned install would resolve on PATH and still never
    authenticate for the account that runs it.
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CLAUDE_INSTALL_REQUEST, "w") as f:
            json.dump({"svc_user": os.environ.get("USER") or os.environ.get("LOGNAME") or "svc_bg",
                       "at": time.time()}, f)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"claude-install: could not write request: {e}")
        return False


@router.post("/api/claude-cli/install")
def claude_cli_install():
    """Trigger the Claude Code CLI install (delegated to the privileged watchdog)."""
    if _claude_bin_rt() != "claude":
        return {"status": "already_installed", "detail": f"claude is already at {_claude_bin_rt()}"}
    if not _request_claude_install():
        return {"status": "error", "detail": "could not queue the install request"}
    return {"status": "queued",
            "detail": "Installing Claude Code for the service user — this takes up to a minute."}


@router.get("/api/claude-cli/install-status")
def claude_cli_install_status():
    """Progress of a delegated Claude Code install (written by the watchdog)."""
    try:
        with open(CLAUDE_INSTALL_STATUS, "r") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — absent until the first install runs
        return {"state": "idle", "stream": "", "returncode": None}


def _claude_bin_rt():
    """Resolved absolute path to the ``claude`` CLI for the subprocess calls below.

    Falls back to the bare name when unresolved so subprocess still raises
    FileNotFoundError and the existing not_found handlers report it — the UI
    message then names the override rather than just saying "not in PATH".
    """
    try:
        from main import claude_bin  # re-exported from llm_client
        return claude_bin() or "claude"
    except Exception:  # noqa: BLE001 — resolution must never break the endpoint
        return "claude"


_CLAUDE_NOT_FOUND = ("'claude' binary not found. The service runs as its own user with "
                     "systemd's minimal PATH, so a binary that works in your shell can still "
                     "be invisible here. Set 'claude_binary' in Settings to the absolute path "
                     "(e.g. /root/.local/bin/claude), or symlink it into /usr/local/bin.")


@router.get("/api/claude-cli/status")
def claude_cli_status():
    """Check whether the local claude CLI is installed and authenticated.
    Runs as a sync handler so FastAPI threads it — avoids blocking the event loop.
    """
    import subprocess
    try:
        probe = subprocess.run(
            [_claude_bin_rt(), "--output-format", "json"],
            input="ping", capture_output=True, text=True, timeout=15,
        )
        output = probe.stdout.strip()
        try:
            data = json.loads(output)
            result_text = data.get("result", "")
            if "Not logged in" in result_text or "/login" in result_text:
                return {"status": "needs_auth",
                        "detail": "Claude CLI installed but not authenticated. Use 'Start Login Flow' to log in."}
            if data.get("is_error") and data.get("result"):
                return {"status": "error", "detail": result_text[:300]}
            return {"status": "authenticated", "detail": "Claude CLI authenticated and ready."}
        except (json.JSONDecodeError, KeyError):
            r = subprocess.run([_claude_bin_rt(), "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return {"status": "ok", "version": r.stdout.strip() or r.stderr.strip()}
            return {"status": "error", "detail": (r.stderr or r.stdout).strip()[:300]}
    except FileNotFoundError:
        return {"status": "not_found", "detail": _CLAUDE_NOT_FOUND}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": "CLI probe timed out — may be authenticating"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.post("/api/claude-cli/auth/start")
def claude_cli_auth_start():
    """Start 'claude auth login' as a background process and capture the OAuth URL.

    Uses a throwaway shell script as BROWSER so the URL is written to a temp file
    AND printed to claude's stderr — two independent capture paths.  The process
    stays alive (stdin=PIPE) so the approval code can be piped back later.
    """
    import subprocess, select as _select, tempfile, stat

    # Pre-flight: there is nothing to log into if the CLI isn't installed. Queue
    # the install (delegated to the privileged watchdog) and tell the operator to
    # retry, instead of failing with a bare FileNotFoundError they then have to
    # decode. Installing here — rather than only at install time — is what makes
    # "Start Login Flow" work on a box where Claude Code was never installed.
    if _claude_bin_rt() == "claude" and not shutil.which("claude"):
        queued = _request_claude_install()
        return {"status": "installing" if queued else "error",
                "detail": ("Claude Code isn't installed. Installing it for the service user now "
                           "(up to a minute) — then click Start Login Flow again."
                           if queued else
                           "Claude Code isn't installed and the install request could not be "
                           "queued. Check that ab-watchdog is running.")}

    # Kill any existing auth process first.
    old = state.get("claude_auth_proc")
    if old and old.poll() is None:
        try:
            old.terminate()
        except Exception:
            pass
    state["claude_auth_proc"] = None
    state["claude_auth_url"] = ""
    state["claude_auth_done"] = False

    try:
        # Write a tiny browser-wrapper script.  When claude calls $BROWSER URL it:
        #   1. Writes the URL to a temp file (readable even if pipe buffering delays it)
        #   2. Prints it to stdout (inherited from claude → our pipe)
        url_file = "/tmp/_claude_auth_url.txt"
        browser_script = "/tmp/_claude_browser.sh"
        try:
            with open(url_file, "w") as f:
                f.write("")
            with open(browser_script, "w") as f:
                f.write(f'#!/bin/sh\nprintf "%s\\n" "$@" | tee "{url_file}" >&2\n')
            os.chmod(browser_script, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        except Exception:
            browser_script = "/bin/echo"   # fallback to original approach

        env = os.environ.copy()
        env["BROWSER"] = browser_script
        # Some distributions also check BROWSER_OPENER / OPENER
        env["BROWSER_OPENER"] = browser_script
        # Suppress any DISPLAY so electron-based openers fall back to $BROWSER
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)

        proc = subprocess.Popen(
            [_claude_bin_rt(), "auth", "login"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        state["claude_auth_proc"] = proc

        # If claude prompts "Open browser? [y/N]" we answer yes immediately.
        # This is non-blocking — if stdin isn't being read, write() still returns.
        try:
            proc.stdin.write("y\n")
            proc.stdin.flush()
        except Exception:
            pass

        # Read stdout+stderr for up to 25 s, scanning every half-second for a URL.
        # Also poll the temp file as a second capture path.
        lines = []
        url_found = ""
        deadline = time.time() + 25
        while time.time() < deadline:
            ready, _, _ = _select.select([proc.stdout, proc.stderr], [], [], 0.5)
            for stream in ready:
                line = stream.readline()
                if line:
                    lines.append(line)
                    for u in re.findall(r'https?://\S+', line):
                        u = u.rstrip(".,;)\"'")
                        if "claude.ai" in u or "anthropic.com" in u or "oauth" in u.lower() or "auth" in u.lower():
                            url_found = u
                            break
            if url_found:
                break
            # Check the temp file written by the browser script
            try:
                with open(url_file) as f:
                    raw = f.read().strip()
                if raw:
                    url_found = raw.splitlines()[0].strip()
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                break

        # Final pass: scan everything captured for any URL (less targeted)
        combined = "".join(lines)
        if not url_found:
            for u in re.findall(r'https?://\S+', combined):
                u = u.rstrip(".,;)\"'")
                if u:
                    url_found = u
                    break
        # Also try the temp file one more time
        if not url_found:
            try:
                with open(url_file) as f:
                    raw = f.read().strip()
                if raw:
                    url_found = raw.splitlines()[0].strip()
            except Exception:
                pass

        state["claude_auth_url"] = url_found

        if proc.poll() == 0:
            state["claude_auth_done"] = True
            return {"status": "authenticated", "url": "", "output": combined[:3000]}

        return {
            "status": "pending",
            "url": url_found,
            "output": combined[:3000] or "(no output yet — process is running, waiting for claude auth login…)",
        }

    except FileNotFoundError:
        return {"status": "not_found", "detail": _CLAUDE_NOT_FOUND}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("/api/claude-cli/auth/poll")
def claude_cli_auth_poll():
    """Check whether the background auth process has completed."""
    proc = state.get("claude_auth_proc")
    url = state.get("claude_auth_url", "")

    if state.get("claude_auth_done"):
        return {"status": "authenticated", "url": url}

    if proc is None:
        # No process — check live whether CLI is authenticated.
        try:
            r = subprocess.run(
                [_claude_bin_rt(), "--output-format", "json"],
                input="ping", capture_output=True, text=True, timeout=10,
            )
            try:
                data = json.loads(r.stdout.strip())
                if "Not logged in" in data.get("result", ""):
                    return {"status": "needs_auth", "url": ""}
                return {"status": "authenticated", "url": ""}
            except Exception:
                pass
        except Exception:
            pass
        return {"status": "no_process", "url": ""}

    rc = proc.poll()
    if rc is None:
        # Still running — drain any new output and look for a URL.
        extra = []
        try:
            import select as _select
            while True:
                ready, _, _ = _select.select([proc.stdout, proc.stderr], [], [], 0)
                if not ready:
                    break
                for s in ready:
                    line = s.readline()
                    if line:
                        extra.append(line)
        except Exception:
            pass
        combined = "".join(extra)
        if not url:
            for u in re.findall(r'https?://\S+', combined):
                u = u.rstrip(".,;)\"'")
                if u:
                    url = u
                    state["claude_auth_url"] = url
                    break
        # Also check the temp file written by the browser wrapper script.
        if not url:
            try:
                with open("/tmp/_claude_auth_url.txt") as f:
                    raw = f.read().strip()
                if raw:
                    url = raw.splitlines()[0].strip()
                    state["claude_auth_url"] = url
            except Exception:
                pass
        # Re-send "y\n" in case claude is still prompting for browser confirmation.
        if not url:
            try:
                proc.stdin.write("y\n")
                proc.stdin.flush()
            except Exception:
                pass
        return {"status": "pending", "url": url, "output": combined[:500]}

    # Process exited.
    if rc == 0:
        state["claude_auth_done"] = True
        state["claude_auth_proc"] = None
        return {"status": "authenticated", "url": url}
    # Non-zero exit — collect remaining stderr.
    try:
        remaining, _ = proc.communicate(timeout=2)
    except Exception:
        remaining = ""
    state["claude_auth_proc"] = None
    return {"status": "error", "detail": f"auth login exited {rc}: {remaining[:300]}", "url": url}


@router.post("/api/claude-cli/auth/submit-code")
async def claude_cli_auth_submit_code(request: Request):
    """Send an authorization code to the waiting 'claude auth login' process via stdin.

    After the user visits the OAuth URL, claude.ai shows an approval code.
    The user pastes it here and we forward it to the subprocess's stdin.
    Blocking subprocess I/O runs in a thread via asyncio.to_thread so the
    event loop is never blocked.
    """
    data = await request.json()
    code = (data.get("code") or "").strip()
    if not code:
        return {"status": "error", "detail": "No code provided."}

    proc = state.get("claude_auth_proc")
    if proc is None or proc.poll() is not None:
        return {"status": "error", "detail": "No active auth process — click 'Start Login Flow' first."}

    def _blocking_submit(proc, code):
        import select as _select
        try:
            proc.stdin.write(code + "\n")
            proc.stdin.flush()
        except Exception as e:
            return {"status": "error", "detail": f"Failed to send code to auth process: {e}"}

        lines = []
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                ready, _, _ = _select.select([proc.stdout, proc.stderr], [], [], 0.5)
                for s in ready:
                    line = s.readline()
                    if line:
                        lines.append(line)
            except Exception:
                break
            if proc.poll() is not None:
                break

        rc = proc.poll()
        output = "".join(lines)
        if rc == 0:
            state["claude_auth_done"] = True
            state["claude_auth_proc"] = None
            return {"status": "authenticated", "output": output}
        if rc is not None:
            state["claude_auth_proc"] = None
            return {"status": "error", "detail": f"Auth process exited {rc}: {output[:300]}"}
        return {"status": "pending", "output": output,
                "message": "Code submitted — authentication in progress. Click 'Check Status' in a moment."}

    return await asyncio.to_thread(_blocking_submit, proc, code)


@router.post("/api/toggle-model")
async def toggle_model(request: Request):
    """Toggles a model's enabled status in the configuration."""
    try:
        data = await request.json()
        model_name = data.get("model")
        enabled = data.get("enabled")

        if not model_name or enabled is None:
            return JSONResponse(status_code=400, content={"error": "Missing model or enabled status"})

        config = load_config()
        enabled_list = config.get("enabled_models", [])

        if enabled and model_name not in enabled_list:
            enabled_list.append(model_name)
        elif not enabled and model_name in enabled_list:
            enabled_list.remove(model_name)

        config["enabled_models"] = enabled_list
        save_config(config)

        return {"status": "success", "enabled_models": enabled_list}
    except Exception as e:
        logger.error(f"Error toggling model: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/logs")
async def get_logs(request: Request):
    logs = ""
    log_rows = []
    try:
        current_log = get_log_path()
        with open(current_log, "r") as f:
            lines = f.readlines()
        tail = [l.rstrip("\n") for l in lines[-100:]]
        logs = "\n".join(reversed(tail))  # raw string kept for the Copy button
        # Structured rows (newest first) so the Logs view renders in the SAME
        # Component | Timestamp | Message table as Hub Logs. Parse "TS - COMPONENT
        # - LEVEL - msg"; a line without that shape (traceback continuation) gets
        # a blank component and shows verbatim.
        for line in reversed(tail):
            if not line.strip():
                continue
            parts = line.split(" - ", 2)
            module = parts[1].strip() if len(parts) >= 3 and line[:4].isdigit() else ""
            log_rows.append({"module": module, "log": line})
    except Exception as e:
        logs = f"Error reading logs from {get_log_path()}: {e}"
        log_rows = [{"module": "", "log": logs}]
    return templates.TemplateResponse(request=request, name="index.html",
                                      context={"view": "logs", "logs": logs,
                                               "log_rows": log_rows, "state": state})


@router.get("/hub-logs")
async def get_hub_logs_page(request: Request):
    config = load_config()
    hub_url = (config.get("HUB_QUERY_URL") or "").strip()
    fetch_error = None
    fetch_status = None
    # Sync model: the Hub Logs page reads the LOCAL mirror only — no live
    # GET_LOGS pull on every page view. The poller's scan_hub_logs →
    # sync_hub_logs refreshes the mirror once per cycle; this page just shows
    # the latest synced snapshot. Connectivity is reflected by whether the
    # mirror has recent data (and the Diagnostics card's hub status dot),
    # not by a per-view live probe.
    logs = get_hub_logs()
    if logs:
        fetch_status = 200
    else:
        fetch_error = ("No synced logs yet — waiting for the first scan cycle. "
                       "If this persists, confirm ab is approved+connected "
                       "in the Hub WebUI (Setup → Spokes).")
    fetch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"view": "hub-logs", "hub_logs": logs, "state": state,
                 "hub_fetch_time": fetch_time, "hub_fetch_error": fetch_error,
                 "hub_fetch_status": fetch_status, "hub_url": hub_url},
    )


@router.get("/api/hub-logs/raw")
async def hub_logs_raw():
    """Return the raw Hub logs (via the authenticated agent) for debugging."""
    client = _get_hub_agent_client()
    if not client:
        return JSONResponse({"error": "Hub agent not configured"}, status_code=400)
    # client.request_sync() is a THREAD-BLOCKING bridge (future.result()) meant
    # for plain-def callers running on their own worker thread (see
    # _trigger_spoke_updates / _wait_for_spokes_online in workers.py). Calling
    # it directly here blocked THIS route's coroutine for up to 25s — and since
    # uvicorn runs single-process/single-event-loop (no workers=), that froze
    # EVERY concurrent request on the server, not just this one. Worse while
    # the hub connection is flapping (request() times out at the full 20s
    # instead of failing fast), which is exactly when an operator reaches for
    # this debug endpoint. run_in_executor moves the blocking wait to a pool
    # thread so the event loop — and every other page/request — stays free.
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, lambda: client.request_sync("GET_LOGS", {}, timeout=20))
    if not isinstance(result, dict):
        return JSONResponse({"error": "Hub agent not approved/connected"}, status_code=503)
    return JSONResponse({
        "status_code": 200,
        "content_type": "application/json",
        "body_preview": json.dumps(result)[:5000],
        "body_length": len(json.dumps(result)),
        "logs": result.get("logs", []),
    })


_LOG_ANALYSIS_LOCK = threading.Lock()
_LOG_ANALYSIS_TASK = "LogAnalysis"
_LOG_ANALYSIS_MAX_CHARS = 16000
_LOG_ANALYSIS_WINDOW_DEFAULT = 30      # default window (min) — configurable via Settings


def _log_analysis_window_min():
    """Configured log-analysis window / precompute interval in minutes (Settings →
    log_analysis_interval_min; default 30). Governs both how far back the LLM looks AND
    how often the idle precompute runs."""
    try:
        return max(1, int(load_config().get("log_analysis_interval_min", _LOG_ANALYSIS_WINDOW_DEFAULT)))
    except (TypeError, ValueError):
        return _LOG_ANALYSIS_WINDOW_DEFAULT


def _parse_log_ts(line):
    """Parse the leading 'YYYY-MM-DD HH:MM:SS' of a log line, or None."""
    try:
        return datetime.strptime((line or "")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _window_lines_since(lines, minutes, get_text=lambda x: x, newest_first=False):
    """Keep only lines within the last `minutes`. `lines` may be strings or dicts
    (via get_text). Chronological (oldest-first) by default; set newest_first for
    a top-down list (e.g. hub rows). Continuation lines with no timestamp inherit
    the current keep state. Empty result → caller should fall back to a line tail."""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(minutes=minutes)
    out = []
    if newest_first:
        for item in lines:
            ts = _parse_log_ts(get_text(item))
            if ts is not None and ts < cutoff:
                break
            out.append(item)
        out.reverse()
    else:
        keeping = False
        for item in lines:
            ts = _parse_log_ts(get_text(item))
            if ts is not None:
                keeping = ts >= cutoff
            if keeping:
                out.append(item)
    return out


def _collect_logs_for_analysis(source, window_minutes=None):
    """Return (title, text) of recent logs for `source` ('self' | 'hub'). With
    window_minutes, restrict to that trailing time window (falling back to a line
    tail if too little is in-window); else the last lines. Char-capped for the LLM."""
    if source == "hub":
        rows = get_hub_logs() or []
        if window_minutes:
            win = _window_lines_since(rows, window_minutes,
                                      get_text=lambda r: r.get("log", "") if isinstance(r, dict) else str(r),
                                      newest_first=True)
            rows = win if len(win) >= 3 else rows[:400]
        else:
            rows = rows[:600]
        lines = [f"[{r.get('module', '?')}] {r.get('log', '')}" if isinstance(r, dict) else str(r) for r in rows]
        text = "\n".join(lines)
        title = f"Hub logs (last {window_minutes} min)" if window_minutes else "Hub logs (mirrored)"
    else:
        try:
            with open(get_log_path(), "r") as f:
                all_lines = f.readlines()
        except Exception as e:  # noqa: BLE001
            return "AppBuilder service logs", f"(could not read AppBuilder log: {e})"
        if window_minutes:
            win = _window_lines_since(all_lines, window_minutes)
            all_lines = win if len(win) >= 5 else all_lines[-400:]
        else:
            all_lines = all_lines[-400:]
        text = "".join(all_lines)
        title = f"AppBuilder service logs (last {window_minutes} min)" if window_minutes else "AppBuilder service logs"
    if len(text) > _LOG_ANALYSIS_MAX_CHARS:
        text = text[-_LOG_ANALYSIS_MAX_CHARS:]
    return title, text


def _run_log_analysis(source, window_minutes=None, precomputed=False):
    """Read the current logs and ask AppBuilder's own LLM whether anything is wrong,
    what it means, and what to check. Streams into the LogAnalysis task (live
    'thought process') and stores the final answer in state['log_analysis']."""
    from main import analyze_logs, parse_log_verdict, is_llm_cooldown_error  # re-exported from llm_client
    from model_selection import LlmRequirements
    title, log_text = _collect_logs_for_analysis(source, window_minutes=window_minutes)
    update_task_state(task_id=_LOG_ANALYSIS_TASK, task_name=f"Analyzing {title}", action="start")
    try:
        reqs = LlmRequirements(complexity="small", latency_sensitive=True,
                               deprioritize_local=True,
                               min_context_tokens=len(log_text) // 4)
        raw = analyze_logs(log_text, title=f"{title} for the AppBuilder system",
                           task_id=_LOG_ANALYSIS_TASK, requirements=reqs)
        verdict, result = parse_log_verdict(raw)  # strip the machine VERDICT line for display
        state["log_analysis"] = {
            "running": False, "source": source, "title": title, "precomputed": precomputed,
            "verdict": verdict,
            "result": result, "error": None, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if is_llm_cooldown_error(e):
            msg = f"All LLM providers are cooling down / unavailable: {e}"
        logger.warning(f"log-analysis failed: {e}")
        state["log_analysis"] = {
            "running": False, "source": source, "title": title, "precomputed": precomputed,
            "result": "", "error": msg, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        update_task_state(task_id=_LOG_ANALYSIS_TASK, action="end")


def _log_analysis_busy():
    """True if the system is doing LLM-heavy work (a fix build/review/triage/chat) —
    the idle pre-compute must not compete with that for the single LLM slot."""
    for t in (state.get("active_tasks") or {}).values():
        name = (t.get("name") or "").lower()
        if any(k in name for k in ("build", "review", "triag", "fix attempt", "verif", "chat", "identify")):
            return True
    return False


def log_health_worker():
    """When AppBuilder is idle, pre-compute a health snapshot of the last N minutes of its
    own logs so the Log Analysis panel shows a ready answer on page open. Cheap, respects
    the single LLM slot (skips while a fix/chat is running), and refreshes at most every
    N minutes — N = log_analysis_interval_min (Settings, default 30). The user's Refresh
    button (runLogAnalysis) always overrides with a live run."""
    import time as _t
    from main import _startup_grace_remaining  # re-exported from workers
    last = 0.0
    while True:
        try:
            _t.sleep(60)
            if state.get("paused") or state.get("blackout"):
                continue
            if _startup_grace_remaining():
                continue  # let ollama/services finish starting before precomputing
            window_min = _log_analysis_window_min()
            if (_t.time() - last) < window_min * 60:
                continue
            if _log_analysis_busy():
                continue
            if not _LOG_ANALYSIS_LOCK.acquire(blocking=False):
                continue
            try:
                state["log_analysis"] = {"running": True, "source": "self", "title": None,
                                         "result": "", "error": None, "at": None, "precomputed": True}
                _run_log_analysis("self", window_minutes=window_min, precomputed=True)
                last = _t.time()
            finally:
                _LOG_ANALYSIS_LOCK.release()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"log_health_worker cycle error: {e}")
            _t.sleep(30)


@router.post("/api/log-analysis/run")
async def log_analysis_run(request: Request):
    """Kick off an LLM analysis of the current logs. Body: {"source": "self"|"hub"}.
    Non-blocking — the UI polls /api/log-analysis for progress + the final result."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    source = "hub" if str((body or {}).get("source")) == "hub" else "self"
    if not _LOG_ANALYSIS_LOCK.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "An analysis is already running."}, status_code=409)
    try:
        # Seed a running marker the UI can poll immediately.
        state["log_analysis"] = {"running": True, "source": source, "title": None,
                                 "result": "", "error": None, "at": None}

        def _worker():
            try:
                # Analyze only the recent window (same as the idle precompute), so the
                # LLM sees just the last-N-min of activity, not the whole tail.
                _run_log_analysis(source, window_minutes=_log_analysis_window_min())
            finally:
                _LOG_ANALYSIS_LOCK.release()

        threading.Thread(target=_worker, name="log-analysis", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        _LOG_ANALYSIS_LOCK.release()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "task_id": _LOG_ANALYSIS_TASK}


@router.get("/api/log-analysis")
async def log_analysis_status():
    """Current log-analysis state: running flag, the live partial (LLM tokens streamed
    so far), and the final result/error once done."""
    la = dict(state.get("log_analysis") or {"running": False, "result": "", "error": None})
    # While running, surface the live streamed tokens as the partial.
    if la.get("running"):
        task = (state.get("active_tasks") or {}).get(_LOG_ANALYSIS_TASK) or {}
        la["partial"] = task.get("stream", "")
    return la


DEFAULT_ENV = {
    "GITHUB_TOKEN": "",
    "LLM_PROVIDER_1": "openai",
    "LLM_API_KEY_1": "",
    "LLM_MODEL_1": "gpt-4o",
    "LLM_BASE_URL_1": "",
    "LLM_PROVIDER_2": "anthropic",
    "LLM_API_KEY_2": "",
    "LLM_MODEL_2": "claude-opus-4-5",
    "LLM_BASE_URL_2": "",
    "LLM_PROVIDER_3": "google",
    "LLM_API_KEY_3": "",
    "LLM_MODEL_3": "gemini-1.5-pro",
    "LLM_BASE_URL_3": "",
    "LLM_PROVIDER_4": "ollama",
    "LLM_API_KEY_4": "",
    "LLM_MODEL_4": "",
    "LLM_BASE_URL_4": "",
    "QA_API_URL": "",
    "QA_REPO": "",
    "QA_TEST_COMMAND": "pytest",
    "POLL_INTERVAL_SECONDS": "3600",
    "UPDATE_API_URL": "",
    "HUB_QUERY_URL": "",
    "HUB_WS_URL": "",
    "HUB_AGENT_ID": "ab",
    "POST_UPDATE_COOLDOWN_MINUTES": "10",
    "LOG_FILE_PATH": "/var/log/ab.log",
    "DEV_BRANCH": "dev",
    "LLM_TIMEOUT": "900",
    "MAX_CONCURRENT_FIXES": "5",
    "TRIAGE_STRICTNESS": "Moderate",
    "LLM_RPM_1": "0",
    "LLM_RPM_2": "0",
    "LLM_RPM_3": "0",
    "LLM_RPM_4": "0",
    "LLM_MAX_RETRIES": "5",
    "LLM_BACKOFF_BASE": "2.0",
    "LLM_BACKOFF_MAX": "600.0",
    "LLM_MAX_CONCURRENT": "1",
    "PROD_VERIFICATION_DAYS": "7",
    "MAX_ISSUES_PER_CYCLE": "15",
    "POLL_INTERVAL_SECONDS": "3600",
    "CHAT_SYSTEM_PROMPT": "",
    "CHAT_HISTORY_WINDOW": "20",
    "LLM_LOG_MAX_ENTRIES": "200",
    "LLM_LOG_MAX_CHARS": "60000",
    "SCHEDULER_WORK_START_HOUR": "7",
    "SCHEDULER_WORK_END_HOUR": "18",
    "SCHEDULER_DAILY_BUDGET": "50",
    "SCHEDULER_WORK_CAP_PCT": "25",
    "SCHEDULER_WORK_POLL_INTERVAL": "3600",
    "SCHEDULER_CRITICAL_LABEL": "critical",
    "SCHEDULER_BUG_LABEL": "bug",
}


# Live GitHub repo list for the settings "Monitored Repositories" multi-select.
# Cached in `state` with a TTL so we don't hit GitHub on every /settings load;
# a failed/missing-token fetch returns [] and the template falls back to the
# free-text "additional repos" input alone.
_GITHUB_REPOS_TTL = 300


def _fetch_github_repos_sync(token: str) -> list:
    """Best-effort list of the configured token's accessible GitHub repos
    (``owner/repo``). Filters to non-archived repos the user can push to (where
    AppBuilder could actually file/fix issues), sorted by name, capped at 200 so
    the settings page stays snappy. Returns ``[]`` on any failure."""
    if not token:
        return []
    try:
        gh = Github(token)
        repos = []
        for r in gh.get_user().get_repos(affiliation="owner,organization_member"):
            try:
                if getattr(r, "archived", False):
                    continue
                perms = getattr(r, "permissions", None)
                # Keep repos we can push to; skip read-only collaborator repos.
                if perms is not None and not getattr(perms, "push", False):
                    continue
                repos.append(r.full_name)
            except Exception:
                continue
            if len(repos) >= 200:
                break
        repos.sort(key=lambda s: s.lower())
        return repos
    except Exception as e:
        logger.warning("settings: GitHub repo list fetch failed: %s", e)
        return []


@router.get("/settings")
async def settings_page(request: Request):
    load_dotenv(override=True)
    settings = DEFAULT_ENV.copy()
    for k in DEFAULT_ENV:
        val = os.getenv(k)
        if val: settings[k] = val
    config = load_config()
    # Self-log scan defaults ON (self-diagnosis) until explicitly turned off
    # via the Settings toggle; display-only default so the checkbox renders
    # checked on a never-saved install.
    # claude_binary lives in config (not DEFAULT_ENV), so expose it on `settings`
    # for the Settings form field to round-trip its saved value.
    settings["claude_binary"] = config.get("claude_binary", "")
    settings["ollama_preload_timeout_s"] = config.get("ollama_preload_timeout_s", 3600)
    settings["gate_scans_on_model_preload"] = config.get("gate_scans_on_model_preload", True)
    settings["model_gate_max_wait_s"] = config.get("model_gate_max_wait_s", 3600)
    # Pretty-printed for the textarea editor; save_settings re-parses it back
    # to a list on save (feature_boundaries itself, not this string, is the
    # value everything else reads).
    settings["feature_boundaries_json"] = json.dumps(config.get("feature_boundaries") or [], indent=2)
    config.setdefault("self_log_scan_enabled", True)
    # PR pre-review defaults OFF (opt-in); display-only default so the checkbox renders.
    config.setdefault("pr_review_enabled", False)
    # Skeptical reviewer panel on PR pre-review (advisory) — OFF (opt-in sub-option).
    config.setdefault("pr_review_llm_enabled", False)
    # Narrow state-logic/control-flow panel on PR pre-review (advisory) — a SECOND,
    # independent opt-in sub-option (see pr_review._state_logic_review).
    config.setdefault("pr_review_state_logic_enabled", False)
    config.setdefault("batch_enabled", False)
    config.setdefault("prompt_caching_enabled", True)
    # Source knobs (default ON keeps the LM bug-fix pipeline working; the per-
    # module log grid + fix-log-detected are opt-in, default OFF, so the operator
    # enables noisy sources one at a time).
    config.setdefault("bug_reports_enabled", True)
    config.setdefault("feature_requests_enabled", True)
    config.setdefault("fix_logdetected_enabled", False)
    # Project skills (skills_loader.py) — repo-committed recipes (add-simulation,
    # dual-copy-guard, ...) AppBuilder follows for fix/build work. Feature
    # auto-drive's build stage depends entirely on these loading; previously
    # configurable only by editing config.json directly.
    config.setdefault("skills_enabled", True)
    config.setdefault("skills_repo", "lbockenstedt/lm")
    config.setdefault("skills_path", ".claude/skills")
    config.setdefault("skills_ttl_s", 3600)
    # Feature auto-drive (feature_drive.py / feature_build.py / pr_review.py's
    # _automerge_decision). Off by default; feature_boundaries seeds from
    # feature_boundary.DEFAULT_BOUNDARIES on first save (see save_settings)
    # rather than here, so an install that never opens this tab still gets a
    # sane starting list the moment it's turned on.
    config.setdefault("feature_drive_enabled", False)
    config.setdefault("feature_drive_label", "enhancement")
    config.setdefault("feature_drive_require_marker", True)
    config.setdefault("feature_drive_repos", [])
    config.setdefault("feature_drive_max_per_cycle", 1)
    config.setdefault("feature_build_timeout_s", 1800)
    config.setdefault("feature_require_docs", True)
    if "feature_boundaries" not in config:
        from feature_boundary import DEFAULT_BOUNDARIES
        config["feature_boundaries"] = DEFAULT_BOUNDARIES
    # Auto-merge — the deliberate, narrowly-scoped invariant exception (see
    # pr_review.py's module docstring + _automerge_decision). Several
    # independent gates, all default-closed: this toggle, feature_drive_enabled,
    # the per-repo allowlist, the per-target-branch allowlist, and the
    # confidence threshold (defaults to 1.0) — effectively OFF until an
    # operator deliberately enables it AND opts a repo in AND opts a target
    # branch in AND lowers the threshold. Global, not per-repo, to start.
    config.setdefault("feature_automerge_enabled", False)
    config.setdefault("feature_automerge_repos", [])
    # Opt-in allowlist of target branches auto-merge is permitted onto —
    # defaults to empty, same philosophy as feature_automerge_repos. Since
    # 2026-08-29 this (not PR authorship) is what keeps main structurally
    # ineligible: ANY PR (human or bot) targeting an allowlisted branch (e.g.
    # "dev") can clear _automerge_decision if both review panels approve at
    # threshold; a PR targeting main, or any branch not listed here, never
    # can. See pr_review._automerge_decision.
    config.setdefault("feature_automerge_target_branches", [])
    config.setdefault("feature_automerge_min_confidence", 1.0)
    config.setdefault("feature_automerge_require_clean", True)
    # DEFAULT-DENY allowlist gate (feature_allowlist): even a clean, high-
    # confidence, boundary-free PR only auto-merges if its diff is a provably-
    # additive shape (docs-only / log-only / tooltip-only). On by default so
    # the safe fallback for anything behaviour-changing is human approval.
    # `feature_automerge_allowlist` = None means "use feature_allowlist's
    # DEFAULT_ALLOWLIST"; an operator may narrow it to a subset.
    config.setdefault("feature_automerge_require_allowlist", True)
    config.setdefault("feature_automerge_allowlist", None)
    # Azure fleet access for the chat agent (az_console). OFF by default; when
    # on, the chat can diagnose the live LM servers via the secretless SP login
    # helper. Mutation is separately gated and defaults OFF (read-only), so the
    # safe default is diagnosis-only. Only available on LM-AB where the helper
    # exists; elsewhere the tools report Azure access as unavailable.
    config.setdefault("CHAT_AZURE_ENABLED", False)
    config.setdefault("CHAT_AZURE_ALLOW_MUTATION", False)
    config.setdefault("AZURE_LM_RESOURCE_GROUP", "LM")
    config.setdefault("AZURE_LOGIN_HELPER", "/usr/local/bin/lm-az-login")
    config.setdefault("enabled_log_modules", [])
    # Module list for the per-module log-filing grid = the operator's module→repo
    # map keys (a module must map to a repo before its logs can be filed).
    _mrm = config.get("module_repo_map") or {}
    log_module_options = sorted(_mrm.keys()) if isinstance(_mrm, dict) else []
    repo_tests = config.get("repo_tests", {})
    repo_tests_str = ", ".join([f"{k}:{v}" for k, v in repo_tests.items()])
    settings["GITHUB_TOKEN"] = config.get("GITHUB_TOKEN") or settings.get("GITHUB_TOKEN", "")
    settings["LLM_TIMEOUT"] = config.get("LLM_TIMEOUT") or settings.get("LLM_TIMEOUT", "900")
    labels = config.get("monitored_labels", ["automated-fix"])
    settings["monitored_labels_str"] = ", ".join(labels)

    # Live multi-select options: cached GitHub repo list (TTL-bounded) UNION the
    # currently-monitored repos so any repo already monitored but not in the
    # fetched list (e.g. a fork the token can't enumerate) still shows as a
    # pre-checked checkbox the user can toggle off.
    cache = state.get("github_repos_cache") or {}
    now = time.time()
    if cache and (now - cache.get("ts", 0)) < _GITHUB_REPOS_TTL:
        github_repos = cache.get("repos", []) or []
    else:
        token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
        github_repos = await asyncio.to_thread(_fetch_github_repos_sync, token) if token else []
        state["github_repos_cache"] = {"ts": now, "repos": github_repos}
    monitored = list(config.get("monitored_repos") or [])
    monitored_set = set(monitored)
    extra_monitored = [r for r in monitored if r not in set(github_repos)]
    repo_options = list(github_repos) + extra_monitored  # union, monitored last
    # Trusted repos use the same checkbox treatment: fetched GitHub list UNION
    # any already-trusted repos not in that list, so an external trusted repo
    # still shows as a toggleable pre-checked box.
    trusted = list(config.get("trusted_repos") or [])
    trusted_set = set(trusted)
    extra_trusted = [r for r in trusted if r not in set(github_repos)]
    trusted_options = list(github_repos) + extra_trusted
    # feature_drive_repos: empty list means "all monitored repos" (the
    # classifier's own default), same checkbox treatment as monitored/trusted.
    feature_drive_repos = list(config.get("feature_drive_repos") or [])
    feature_drive_repos_set = set(feature_drive_repos)
    extra_fd_repos = [r for r in feature_drive_repos if r not in set(github_repos)]
    feature_drive_repo_options = list(github_repos) + extra_fd_repos
    # feature_automerge_repos: SEPARATE opt-in list from feature_drive_repos —
    # a repo can build features without ever being allowed to auto-merge them.
    # Defaults to empty (nothing auto-merges); see routes.py's config.setdefault
    # above and pr_review._automerge_decision.
    feature_automerge_repos = list(config.get("feature_automerge_repos") or [])
    feature_automerge_repos_set = set(feature_automerge_repos)
    extra_am_repos = [r for r in feature_automerge_repos if r not in set(github_repos)]
    feature_automerge_repo_options = list(github_repos) + extra_am_repos

    # SECURITY: llm_credentials/llm_entries carry plaintext api_key values.
    # They used to flow into the template raw via the **config merge below,
    # which embedded every configured key directly in the served HTML
    # (readable via view-source, cached by any proxy, no JS required) — the
    # same class of leak GET /api/llm/config already guards against for API
    # consumers via safe_creds. Redact api_key to a presence flag here too;
    # the edit forms show a "•••• already set" placeholder instead of the
    # real value, and only send a NEW key back on save when the operator
    # actually types into the field (see saveCredential/saveEntry JS +
    # save_llm_credential/update_llm_entry's "only overwrite if present in
    # the payload" handling).
    _safe_llm_credentials = {
        p: {"base_url": v.get("base_url", ""), "has_key": bool(v.get("api_key"))}
        for p, v in (config.get("llm_credentials") or {}).items()
    }
    # health: per-entry "is this failing right now" for the Settings badge —
    # resolves the same provider/model/base_url an actual call would use (an
    # entry's own base_url overrides its provider's shared credential, same
    # fallback llm_client._iter_configured_endpoints applies) so the lookup
    # key matches what _call_provider_timed records against. See
    # llm_client.get_llm_entry_health's docstring for why this surfaces at
    # all (lm#452/#469/#444 found stuck on a permanently-broken entry with
    # no visible signal in the UI).
    import llm_client as _llm_client
    _llm_creds = config.get("llm_credentials") or {}
    _safe_llm_entries = [
        {**e, "api_key": "", "has_key": bool(e.get("api_key")),
         "health": _llm_client.get_llm_entry_health(
             e.get("provider") or "openai", e.get("base_url") or (_llm_creds.get((e.get("provider") or "openai").lower().strip()) or {}).get("base_url", ""),
             e.get("model") or "")}
        for e in (config.get("llm_entries") or [])
    ]

    # Model Registry editor data — the curated rules as editable JSON (always
    # re-rendered from current config so a save from any tab round-trips it),
    # a read-only preview of the effective merged registry (curated rules +
    # auto-discovered stubs), and the unclassified auto stubs an operator
    # should promote to curated. See Phase 7c / plan §8.
    import model_registry as _registry
    _curated = config.get("model_registry") or _registry.DEFAULT_MODEL_RULES
    # Lazy top-up: an existing config frozen before ollama2 (or any local/free
    # provider) gained a DEFAULT rule gets the missing rule appended here so it
    # shows in the JSON editor + preview and round-trips into persistence on the
    # next save. Idempotent, append-only — never reorders the operator's rules.
    _curated, _mr_added = _registry.upgrade_local_free_rules(_curated)
    _curated, _mr_added_cap = _registry.upgrade_capable_local_rules(_curated)
    _curated, _mr_claude_paid = _registry.reclassify_claude_cli_paid(_curated)
    _curated, _mr_claude_models = _registry.upgrade_claude_cli_model_rules(_curated)
    _curated, _mr_copilot_models = _registry.upgrade_copilot_model_rules(_curated)
    _curated, _mr_oc_models = _registry.upgrade_ollama_cloud_model_rules(_curated)
    _curated, _mr_oc_tools = _registry.enable_ollama_cloud_tools(_curated)
    _curated, _mr_or_router = _registry.upgrade_openrouter_free_router_rule(_curated)
    _curated, _mr_cp_tools = _registry.enable_copilot_tools(_curated)
    _curated, _mr_ranks = _registry.backfill_capability_ranks(_curated)
    _curated, _mr_speeds = _registry.backfill_speed_tiers(_curated)
    _registry_rules_json = json.dumps(_curated, indent=2)
    _auto = config.get("model_registry_auto") or []
    _registry_preview = (
        [{"provider": r.get("provider"), "match": r.get("match"),
          "cost_tier": r.get("cost_tier"), "max_complexity": r.get("max_complexity"),
          "context_window": r.get("context_window"),
          "speed_tier": _registry.speed_tier(r),
          "capability_rank": _registry.capability_rank(r),
          "supports_tools": r.get("supports_tools"),
          "native_agentic_tools": r.get("native_agentic_tools"),
          "supports_structured_output": r.get("supports_structured_output"),
          "supports_batch": r.get("supports_batch"),
          "source": "curated", "enabled": r.get("enabled", True)}
         for r in _curated] +
        [{"provider": r.get("provider"), "match": r.get("model"),
          "cost_tier": r.get("cost_tier"), "max_complexity": r.get("max_complexity"),
          "context_window": r.get("context_window"),
          "speed_tier": _registry.speed_tier(r),
          "capability_rank": _registry.capability_rank(r),
          "supports_tools": r.get("supports_tools"),
          "native_agentic_tools": r.get("native_agentic_tools"),
          "supports_structured_output": r.get("supports_structured_output"),
          "supports_batch": r.get("supports_batch"),
          "source": "auto", "enabled": True}
         for r in _auto]
    )
    _registry_unclassified = [
        {"provider": r.get("provider"), "model": r.get("model")}
        for r in _auto if (r.get("cost_tier") or "unknown") == "unknown"
    ]

    return templates.TemplateResponse(request=request, name="index.html", context={
        "view": "settings",
        "settings": {**settings, **config, "repo_tests_str": repo_tests_str, "monitored_labels_str": settings["monitored_labels_str"],
                     "llm_credentials": _safe_llm_credentials, "llm_entries": _safe_llm_entries,
                     # SECURITY: same rule as llm_credentials above -- the raw
                     # proxy key must never be embedded in the served HTML, so
                     # the field renders from a presence flag only.
                     "llm_proxy_api_key": "",
                     "llm_proxy_api_key_configured": bool(config.get("llm_proxy_api_key"))},
        "registry_rules_json": _registry_rules_json,
        "registry_preview": _registry_preview,
        "registry_unclassified": _registry_unclassified,
        "available_labels": state.get("available_labels", []),
        "repo_options": repo_options,
        "monitored_set": monitored_set,
        "trusted_options": trusted_options,
        "trusted_set": trusted_set,
        "feature_drive_repo_options": feature_drive_repo_options,
        "feature_drive_repos_set": feature_drive_repos_set,
        "feature_automerge_repo_options": feature_automerge_repo_options,
        "feature_automerge_repos_set": feature_automerge_repos_set,
        "log_module_options": log_module_options,
        "state": state,
    })


@router.get("/diagnostics")
async def diagnostics_page(request: Request):
    """Diagnostics view — versions, stale-code state, per-provider status, and
    update/restart state. Data is fetched live via /api/diagnostics by refreshDiagnostics()."""
    return templates.TemplateResponse(request=request, name="index.html", context={
        "view": "diagnostics",
        "state": state,
    })


@router.post("/save_settings")
async def save_settings(request: Request):
    form_data = await request.form()
    data = dict(form_data)

    config_data = load_config()

    labels_mode = data.get("label_mode", "SPECIFIC")
    if labels_mode == "ANY":
        labels = ["ANY"]
    elif labels_mode == "NONE":
        labels = ["NONE"]
    else:
        # ------------------------------------------------------------------
        # BUGFIX: 'dict' object has no attribute 'getlist'
        #
        # The previous code called form_data.getlist("monitored_labels")
        # inside a try/except AttributeError. However, if form_data is a
        # plain dict (not a Starlette FormData / MultiDict), calling
        # .getlist() raises an UNCAUGHT AttributeError in some code paths,
        # producing the log error: "'dict' object has no attribute 'getlist'".
        #
        # Fix: Use hasattr() to explicitly check whether the object supports
        # getlist() before calling it. If it does (Starlette FormData), use
        # getlist() to retrieve all checked checkbox values. If it does not
        # (plain dict), fall back to dict.get() with manual list handling so
        # we never raise an AttributeError.
        # ------------------------------------------------------------------
        if hasattr(form_data, "getlist"):
            labels_list = form_data.getlist("monitored_labels")
        else:
            # form_data is a plain dict — use .get() with manual list handling.
            val = form_data.get("monitored_labels", [])
            if isinstance(val, list):
                labels_list = val
            elif isinstance(val, str) and val:
                labels_list = [val]
            else:
                labels_list = []

        custom_labels_raw = data.get("custom_labels", "")
        if custom_labels_raw:
            custom_labels = [x.strip() for x in custom_labels_raw.split(",") if x.strip()]
            labels_list.extend(custom_labels)

        if not labels_list:
            labels = ["automated-fix"]
        else:
            labels = list(set(labels_list))

    if "label_mode" in data or "monitored_labels" in form_data or "custom_labels" in data:
        config_data["monitored_labels"] = labels

    # Monitored repos now arrive as repeated checkbox values (getlist) plus an
    # optional free-text "additional repos" field for repos not in the fetched
    # GitHub list. Merge + dedup (preserve order). Handled here, NOT in the
    # `updates` dict below — dict(form_data) collapses multi-values to the last
    # checkbox, which would silently drop the rest.
    if hasattr(form_data, "getlist"):
        checked_repos = form_data.getlist("monitored_repos")
    else:
        checked_repos = [data["monitored_repos"]] if data.get("monitored_repos") else []
    extra_raw = data.get("monitored_repos_extra", "") or ""
    extra_repos = [clean_repo_name(x.strip()) for x in extra_raw.replace("\\n", ",").split(",") if x.strip()]
    monitored_repos = [clean_repo_name(x) for x in checked_repos if x and str(x).strip()]
    for r in extra_repos:
        if r and r not in monitored_repos:
            monitored_repos.append(r)
    config_data["monitored_repos"] = list(dict.fromkeys(monitored_repos))

    # Trusted repos: same checkbox + free-text treatment as monitored.
    if hasattr(form_data, "getlist"):
        checked_trusted = form_data.getlist("trusted_repos")
    else:
        checked_trusted = [data["trusted_repos"]] if data.get("trusted_repos") else []
    extra_trusted_raw = data.get("trusted_repos_extra", "") or ""
    extra_trusted = [clean_repo_name(x.strip()) for x in extra_trusted_raw.replace("\\n", ",").split(",") if x.strip()]
    trusted_repos = [clean_repo_name(x) for x in checked_trusted if x and str(x).strip()]
    for r in extra_trusted:
        if r and r not in trusted_repos:
            trusted_repos.append(r)
    config_data["trusted_repos"] = list(dict.fromkeys(trusted_repos))

    updates = {
        "default_branch": lambda v: v,
        "dev_branch": lambda v: v,
        # Branch-cleanup policy. Stored as lists so branch_policy can consume
        # them directly; the form supplies a comma-separated string.
        "protected_branches": parse_branch_names,
        "auto_branch_prefixes": parse_branch_names,
        "GITHUB_TOKEN": lambda v: v,
        "LLM_PROVIDER_1": lambda v: v,
        "LLM_API_KEY_1": lambda v: v,
        "LLM_MODEL_1": lambda v: v,
        "LLM_BASE_URL_1": lambda v: v,
        "LLM_PROVIDER_2": lambda v: v,
        "LLM_API_KEY_2": lambda v: v,
        "LLM_MODEL_2": lambda v: v,
        "LLM_BASE_URL_2": lambda v: v,
        "LLM_PROVIDER_3": lambda v: v,
        "LLM_API_KEY_3": lambda v: v,
        "LLM_MODEL_3": lambda v: v,
        "LLM_BASE_URL_3": lambda v: v,
        "LLM_PROVIDER_4": lambda v: v,
        "LLM_API_KEY_4": lambda v: v,
        "LLM_MODEL_4": lambda v: v,
        "LLM_BASE_URL_4": lambda v: v,
        "LLM_TIMEOUT": lambda v: v,
        "MAX_CONCURRENT_FIXES": lambda v: v,
        "TRIAGE_STRICTNESS": lambda v: v,
        "LLM_RPM_1": lambda v: v,
        "LLM_RPM_2": lambda v: v,
        "LLM_RPM_3": lambda v: v,
        "LLM_RPM_4": lambda v: v,
        "LLM_MAX_RETRIES": lambda v: v,
        "LLM_BACKOFF_BASE": lambda v: v,
        "LLM_BACKOFF_MAX": lambda v: v,
        "LLM_MAX_CONCURRENT": lambda v: v,
        "PROD_VERIFICATION_DAYS": lambda v: v,
        "MAX_ISSUES_PER_CYCLE": lambda v: v,
        "POLL_INTERVAL_SECONDS": lambda v: v,
        "self_diagnosis_repo": lambda v: clean_repo_name(v.strip()) if v and v.strip() else "",
        # File-a-Bug: which repo ab files user-submitted WebUI bug reports
        # into (and where the fix pipeline then runs). Defaults to lbockenstedt/lm.
        "bug_report_repo": lambda v: clean_repo_name(v.strip()) if v and v.strip() else "",
        "module_repo_map": lambda v: parse_module_repo_map(v),
        # Chat-agent numeric settings (stored as strings by the form; coerce to int).
        "CHAT_TOOL_MAX_ITERATIONS": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_TOOL_MAX_ITERATIONS"],
        "CHAT_TOOL_MAX_TOKENS": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_TOOL_MAX_TOKENS"],
        "CHAT_INDEX_ISSUE_LIMIT": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_INDEX_ISSUE_LIMIT"],
        "CHAT_INDEX_CACHE_TTL": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_INDEX_CACHE_TTL"],
        "CHAT_FIX_PROPOSAL_TTL": lambda v: int(v) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["CHAT_FIX_PROPOSAL_TTL"],
        "FIX_MAX_FILES": lambda v: max(1, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["FIX_MAX_FILES"],
        "FIX_MAX_FILE_CHARS": lambda v: max(1, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["FIX_MAX_FILE_CHARS"],
        "FIX_MAX_CONTEXT_CHARS": lambda v: max(1, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["FIX_MAX_CONTEXT_CHARS"],
        "FIX_MAX_OUTPUT_TOKENS": lambda v: max(1, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["FIX_MAX_OUTPUT_TOKENS"],
        "HEARTBEAT_STALE_S": lambda v: max(30, int(v)) if str(v).strip().isdigit() else CHAT_CONFIG_DEFAULTS["HEARTBEAT_STALE_S"],
        "heartbeat_exclude": lambda v: [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else ([s.strip() for s in str(v).replace(",", "\n").splitlines() if s.strip()] if v else []),
        "CHAT_SYSTEM_PROMPT": lambda v: v.strip() if v else "",
        "CHAT_HISTORY_WINDOW": lambda v: int(v) if str(v).strip().isdigit() else 20,
        "LLM_LOG_MAX_ENTRIES": lambda v: int(v) if str(v).strip().isdigit() else 200,
        "LLM_LOG_MAX_CHARS": lambda v: int(v) if str(v).strip().isdigit() else 60000,
        "SCHEDULER_WORK_START_HOUR": lambda v: int(v) if str(v).strip().isdigit() else 7,
        "SCHEDULER_WORK_END_HOUR": lambda v: int(v) if str(v).strip().isdigit() else 18,
        "SCHEDULER_DAILY_BUDGET": lambda v: int(v) if str(v).strip().isdigit() else 50,
        "SCHEDULER_WORK_CAP_PCT": lambda v: int(v) if str(v).strip().isdigit() else 25,
        "SCHEDULER_WORK_POLL_INTERVAL": lambda v: int(v) if str(v).strip().isdigit() else 3600,
        "SCHEDULER_CRITICAL_LABEL": lambda v: v.strip() if v else "critical",
        "SCHEDULER_BUG_LABEL": lambda v: v.strip() if v else "bug",
        "QA_API_URL": lambda v: v.strip() if v else "",
        "QA_REPO": lambda v: v.strip() if v else "",
        "QA_TEST_COMMAND": lambda v: v.strip() if v else "pytest",
        "HUB_QUERY_URL": lambda v: v.strip() if v else "",
        "HUB_WS_URL": lambda v: v.strip() if v else "",
        "HUB_AGENT_ID": lambda v: v.strip() if v else "ab",
        "POST_UPDATE_COOLDOWN_MINUTES": lambda v: max(0, int(v)) if str(v).isdigit() else 10,
    }


    for key, transform in updates.items():
        if key in data:
            val = data[key]
            if "repos" in key and not val:
                config_data[key] = []
            else:
                config_data[key] = transform(val)

    config_data["direct_push_enabled"] = data.get("direct_push_enabled") == "on"
    # Unchecked checkboxes are simply absent from the form, so an explicit
    # comparison is what distinguishes "off" from "not submitted".
    config_data["delete_merged_branches"] = data.get("delete_merged_branches") == "on"
    # Agentic LLM router (/v1/messages) toggles. agentic_default (OFF): route
    # every proxy request through AppBuilder's agent loop, not just model=
    # ab-agent. autofix_enabled (OFF, a second gate behind the required key):
    # let the agentic router trigger real fixes — still panel + boundary gated.
    config_data["llm_proxy_agentic_default"] = data.get("llm_proxy_agentic_default") == "on"
    config_data["llm_proxy_autofix_enabled"] = data.get("llm_proxy_autofix_enabled") == "on"
    # LLM router API key. /v1/* bypasses the WebUI session middleware and does
    # its own key check, and llm_proxy._authorized now FAILS CLOSED, so this is
    # the only thing standing between the router (and the agentic fix pipeline
    # behind it) and anyone who can reach the host.
    #
    # Same secret discipline as the OIDC client_secret and the llm_credentials
    # api_keys: the field is never rendered with its real value, so a blank
    # submit means "operator did not retype it" and must KEEP the stored key
    # rather than wipe it -- otherwise saving any other Automation setting
    # would silently disable authentication. Clearing is explicit.
    if "llm_proxy_api_key" in data:
        _pk = str(data.get("llm_proxy_api_key") or "")
        if _pk.strip() == "__CLEAR__":
            config_data["llm_proxy_api_key"] = ""
        elif _pk.strip():
            config_data["llm_proxy_api_key"] = _pk.strip()
    # PR pre-review toggle (default OFF): comment parity/drift findings on open PRs.
    config_data["pr_review_enabled"] = data.get("pr_review_enabled") == "on"
    # Advisory skeptical-panel sub-option (unifies PR review with the AI-fix reviewer).
    config_data["pr_review_llm_enabled"] = data.get("pr_review_llm_enabled") == "on"
    # Advisory state-logic/control-flow panel sub-option (independent of the above).
    config_data["pr_review_state_logic_enabled"] = data.get("pr_review_state_logic_enabled") == "on"
    # Test-regression sub-option (default OFF): actually clones + runs the repo's
    # test suite for a PR's head vs. base branch and flags NEW failures — real
    # code execution against PR-authored content, opt-in per install (see
    # check_test_regressions.py for why this is a different risk/cost class
    # from the other, pure-static Tier-1 checks).
    config_data["pr_test_regression_enabled"] = data.get("pr_test_regression_enabled") == "on"
    config_data["batch_enabled"] = data.get("batch_enabled") == "on"
    config_data["prompt_caching_enabled"] = data.get("prompt_caching_enabled") == "on"
    # File-a-Bug toggle (defaults on so the footer button works out of the box).
    config_data["bug_report_enabled"] = data.get("bug_report_enabled") != "off"
    config_data["qa_enabled"] = data.get("qa_enabled") == "on"
    config_data["skip_review"] = data.get("skip_review") == "on"
    # Log Analysis window / idle-precompute interval in minutes (default 30). Governs
    # both how far back the LLM looks and how often the idle snapshot refreshes.
    _lai = str(data.get("log_analysis_interval_min") or "").strip()
    try:
        config_data["log_analysis_interval_min"] = max(1, int(_lai)) if _lai else 30
    except (TypeError, ValueError):
        config_data["log_analysis_interval_min"] = 30
    # Post-boot grace (seconds) before AppBuilder runs LLM/scan work — lets ollama +
    # services finish starting after a reboot so it doesn't 404 on /api/chat. 0 = off.
    _sg = str(data.get("startup_grace_seconds") or "").strip()
    try:
        config_data["startup_grace_seconds"] = max(0, int(_sg)) if _sg else 300
    except (TypeError, ValueError):
        config_data["startup_grace_seconds"] = 300
    # Hold scans until the ensemble models are resident (default on). Ollama
    # serialises requests, so a scan that starts first queues the preload behind a
    # multi-minute CPU build.
    config_data["gate_scans_on_model_preload"] = bool(data.get("gate_scans_on_model_preload"))
    # Cap on that wait, so a preload that can never succeed cannot wedge scanning.
    _mg = str(data.get("model_gate_max_wait_s") or "").strip()
    try:
        config_data["model_gate_max_wait_s"] = max(60, int(_mg)) if _mg else 3600
    except (TypeError, ValueError):
        config_data["model_gate_max_wait_s"] = 3600
    # How long to allow a MODEL LOAD (preload) before giving up. Separate from
    # LLM_TIMEOUT, which bounds inference: a cold load is disk -> RAM plus a
    # num_ctx-sized KV allocation, and on a CPU-only box a 14B at 32k context can
    # exceed the old hardcoded 900s. Default 3600.
    _pt = str(data.get("ollama_preload_timeout_s") or "").strip()
    try:
        config_data["ollama_preload_timeout_s"] = max(60, int(_pt)) if _pt else 3600
    except (TypeError, ValueError):
        config_data["ollama_preload_timeout_s"] = 3600
    # Absolute path to the `claude` CLI. The service runs as its own user with
    # systemd's minimal PATH, so a binary that resolves in an operator's shell can
    # be invisible here; claude_bin() probes the usual install dirs, and this is
    # the escape hatch when it lives somewhere else. Blank = auto-detect.
    config_data["claude_binary"] = str(data.get("claude_binary") or "").strip()
    # Ollama context window (num_ctx). Default 16384 so fix/log prompts don't 400 with
    # "prompt is longer than the context length". Raise for very large prompts.
    _nc = str(data.get("ollama_num_ctx") or "").strip()
    try:
        config_data["ollama_num_ctx"] = max(2048, int(_nc)) if _nc else 32768
    except (TypeError, ValueError):
        config_data["ollama_num_ctx"] = 32768
    # Ollama CPU threads (num_thread). 0 = ollama default (~physical cores). Raise on a
    # CPU box to speed the big models — set to your allocated physical core count.
    _nt = str(data.get("ollama_num_thread") or "").strip()
    try:
        config_data["ollama_num_thread"] = max(0, int(_nt)) if _nt else 0
    except (TypeError, ValueError):
        config_data["ollama_num_thread"] = 0
    # Keep ollama models resident (keep_alive; -1 = forever) + preload them at startup so
    # the ensemble doesn't reload big models from disk on every switch.
    config_data["ollama_keep_alive"] = (str(data.get("ollama_keep_alive") or "").strip() or "-1")
    config_data["ollama_preload_models"] = data.get("ollama_preload_models") != "off"
    # OLLAMA_MAX_LOADED_MODELS — how many models the ollama SERVER keeps resident at once.
    # Written to ollama's systemd env by Local LLM Setup (set >= ensemble size to hold all
    # models loaded). Server-side setting, not per-request.
    _ml = str(data.get("ollama_max_loaded_models") or "").strip()
    try:
        config_data["ollama_max_loaded_models"] = max(1, int(_ml)) if _ml else 3
    except (TypeError, ValueError):
        config_data["ollama_max_loaded_models"] = 3
    # Heartbeat triage files issues for modules with a missing/stale heartbeat
    # but NO error in the logs. Off by default (error-log-only filing); opt-in
    # if you want dead-module detection independent of the error log.
    config_data["heartbeat_triage_enabled"] = data.get("heartbeat_triage_enabled") == "on"
    # ── Source noise-control knobs ────────────────────────────────────────────
    # BugFixes from LM (default ON — keeps the bug-fix pipeline working).
    config_data["bug_reports_enabled"] = data.get("bug_reports_enabled") != "off"
    # Feature Requests from LM (default ON; independently toggleable).
    config_data["feature_requests_enabled"] = data.get("feature_requests_enabled") != "off"
    # Project skills (skills_loader.py). Default-ON via config.setdefault above
    # (settings_page), so a genuine uncheck on THIS save is trustworthy — unlike
    # the != "off" idiom used just above, which needs a hidden "off" companion
    # input that doesn't exist for this field, so it's read the unambiguous way.
    config_data["skills_enabled"] = data.get("skills_enabled") == "on"
    config_data["skills_repo"] = (data.get("skills_repo") or "lbockenstedt/lm").strip()
    config_data["skills_path"] = (data.get("skills_path") or ".claude/skills").strip()
    _skills_ttl = str(data.get("skills_ttl_s") or "").strip()
    config_data["skills_ttl_s"] = int(_skills_ttl) if _skills_ttl.isdigit() else 3600

    # ── Feature auto-drive (Phase 1: classify only) ─────────────────────────
    _save_warnings = []
    config_data["feature_drive_enabled"] = data.get("feature_drive_enabled") == "on"
    config_data["feature_drive_label"] = (data.get("feature_drive_label") or "enhancement").strip()
    config_data["feature_drive_require_marker"] = data.get("feature_drive_require_marker") == "on"
    _fdmpc = str(data.get("feature_drive_max_per_cycle") or "").strip()
    config_data["feature_drive_max_per_cycle"] = int(_fdmpc) if _fdmpc.isdigit() else 1
    # List-type, same getlist-merge pattern as monitored_repos (routes.py's own
    # BUGFIX comment above explains why this must live OUTSIDE the `updates`
    # dict: dict(form_data) collapses repeated values to the last one).
    if hasattr(form_data, "getlist"):
        _fd_checked = form_data.getlist("feature_drive_repos")
    else:
        _v = data.get("feature_drive_repos")
        _fd_checked = [_v] if _v else []
    _fd_extra_raw = data.get("feature_drive_repos_extra", "") or ""
    _fd_extra = [clean_repo_name(x.strip()) for x in _fd_extra_raw.replace("\n", ",").split(",") if x.strip()]
    _fd_repos = [clean_repo_name(x) for x in _fd_checked if x and str(x).strip()]
    for _r in _fd_extra:
        if _r and _r not in _fd_repos:
            _fd_repos.append(_r)
    config_data["feature_drive_repos"] = list(dict.fromkeys(_fd_repos))

    # Boundary list — a JSON textarea, always re-rendered with the CURRENT
    # value on every page load (settings_page: settings["feature_boundaries_json"]),
    # so a save from any OTHER tab round-trips it unchanged rather than wiping
    # it. Bad JSON / wrong shape here is a mistake worth telling the operator
    # about, but must never silently discard their already-saved list — on any
    # validation failure this branch simply doesn't touch config_data, so the
    # value load_config() already put there (a few lines up) survives.
    if "feature_boundaries_json" in data:
        _fb_raw = data.get("feature_boundaries_json") or "[]"
        try:
            _fb_parsed = json.loads(_fb_raw)
            if not isinstance(_fb_parsed, list):
                raise ValueError("must be a JSON array")
            for _b in _fb_parsed:
                if not isinstance(_b, dict) or not _b.get("id"):
                    raise ValueError("every boundary needs at least an \"id\"")
            config_data["feature_boundaries"] = _fb_parsed
        except Exception as _fbe:
            _save_warnings.append(f"feature_boundaries JSON was invalid ({_fbe}) — kept the previous value")

    # Model Registry — the curated capability/cost rules, a JSON textarea always
    # re-rendered from current config (settings_page: registry_rules_json) so a
    # save from any OTHER tab round-trips it unchanged. Same boundary discipline
    # as feature_boundaries above: validate HERE in the imperative block (never
    # the `updates` table, which also writes .env), and on any failure leave
    # config_data["model_registry"] untouched so the previous rules survive.
    if "model_registry_json" in data:
        _mr_raw = data.get("model_registry_json") or "[]"
        try:
            _mr_parsed = json.loads(_mr_raw)
            if not isinstance(_mr_parsed, list):
                raise ValueError("must be a JSON array")
            for _r in _mr_parsed:
                if not isinstance(_r, dict) or not _r.get("id"):
                    raise ValueError("every rule needs at least an \"id\"")
                if not _r.get("provider") or not _r.get("match"):
                    raise ValueError("every rule needs a \"provider\" and a \"match\" glob")
            config_data["model_registry"] = _mr_parsed
        except Exception as _mre:
            _save_warnings.append(f"model_registry JSON was invalid ({_mre}) — kept the previous rules")
        else:
            # Belt-and-suspenders top-up on save too: if a save arrives whose
            # rules predate a local/free default (e.g. ollama2), append the
            # missing local/free rule so the persisted list is complete.
            # Idempotent + append-only. Mirrors the settings_page read top-up.
            import model_registry as _registry_save
            config_data["model_registry"], _ = _registry_save.upgrade_local_free_rules(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.upgrade_capable_local_rules(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.reclassify_claude_cli_paid(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.upgrade_claude_cli_model_rules(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.upgrade_copilot_model_rules(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.upgrade_ollama_cloud_model_rules(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.enable_ollama_cloud_tools(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.upgrade_openrouter_free_router_rule(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.enable_copilot_tools(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.backfill_capability_ranks(
                config_data["model_registry"])
            config_data["model_registry"], _ = _registry_save.backfill_speed_tiers(
                config_data["model_registry"])

    config_data["feature_build_timeout_s"] = int(data.get("feature_build_timeout_s")) \
        if str(data.get("feature_build_timeout_s") or "").strip().isdigit() else 1800
    config_data["feature_require_docs"] = data.get("feature_require_docs") == "on"

    # ── Auto-merge (the deliberate invariant exception — see pr_review.py) ──
    config_data["feature_automerge_enabled"] = data.get("feature_automerge_enabled") == "on"
    config_data["feature_automerge_require_clean"] = data.get("feature_automerge_require_clean") == "on"
    _amc = str(data.get("feature_automerge_min_confidence") or "").strip()
    try:
        config_data["feature_automerge_min_confidence"] = max(0.0, min(1.0, float(_amc))) if _amc else 1.0
    except (TypeError, ValueError):
        config_data["feature_automerge_min_confidence"] = 1.0
    # Same list pattern as feature_drive_repos above — a SEPARATE opt-in list,
    # deliberately not unioned with it (a repo can build without ever being
    # allowed to auto-merge).
    if hasattr(form_data, "getlist"):
        _am_checked = form_data.getlist("feature_automerge_repos")
    else:
        _v2 = data.get("feature_automerge_repos")
        _am_checked = [_v2] if _v2 else []
    _am_extra_raw = data.get("feature_automerge_repos_extra", "") or ""
    _am_extra = [clean_repo_name(x.strip()) for x in _am_extra_raw.replace("\n", ",").split(",") if x.strip()]
    _am_repos = [clean_repo_name(x) for x in _am_checked if x and str(x).strip()]
    for _r2 in _am_extra:
        if _r2 and _r2 not in _am_repos:
            _am_repos.append(_r2)
    config_data["feature_automerge_repos"] = list(dict.fromkeys(_am_repos))
    # Target-branch allowlist — free-text comma/newline separated, same
    # parsing style as _am_extra above. No checkbox list here (unlike repos,
    # there's no cheap "known branches" enumeration without an extra GitHub
    # API call per repo) — this is deliberately just a short opt-in list like
    # ["dev"], not meant to hold many entries.
    _am_branches_raw = data.get("feature_automerge_target_branches", "") or ""
    _am_branches = [b.strip() for b in _am_branches_raw.replace("\n", ",").split(",") if b.strip()]
    config_data["feature_automerge_target_branches"] = list(dict.fromkeys(_am_branches))

    # Auto-FIX log-detected / automated-fix issues (default OFF; Bug + Critical
    # always fix). Stops the fixer churning on log-scraped issues.
    config_data["fix_logdetected_enabled"] = data.get("fix_logdetected_enabled") == "on"
    # Per-module hub-log auto-filing: only CHECKED modules have their errors
    # filed as issues. Empty = OFF for every module (the default) → enable one at
    # a time. getlist so repeated checkbox values aren't collapsed by dict(form_data).
    if hasattr(form_data, "getlist"):
        config_data["enabled_log_modules"] = list(dict.fromkeys(form_data.getlist("enabled_log_modules")))
    else:
        _elm = form_data.get("enabled_log_modules", [])
        config_data["enabled_log_modules"] = _elm if isinstance(_elm, list) else ([_elm] if _elm else [])
    # Self-log scan: scan AppBuilder's OWN logs for internal errors + file them
    # as GitHub issues in self_diagnosis_repo. On by default (self-diagnosis);
    # turn OFF to stop AppBuilder from monitoring/filing its own logs.
    config_data["self_log_scan_enabled"] = data.get("self_log_scan_enabled") == "on"
    config_data["CHAT_TOOLS_ENABLED"] = data.get("CHAT_TOOLS_ENABLED") == "on"
    # Multi-agent orchestration (default off): a chat request is planned into a
    # sub-task DAG whose independent parts run in parallel across distinct
    # endpoints, then merged. The two bounds are clamped to >=1.
    config_data["ORCHESTRATOR_ENABLED"] = data.get("ORCHESTRATOR_ENABLED") == "on"
    config_data["ORCHESTRATOR_MAX_PARALLEL"] = max(1, int(data.get("ORCHESTRATOR_MAX_PARALLEL"))) \
        if str(data.get("ORCHESTRATOR_MAX_PARALLEL") or "").strip().isdigit() else 3
    config_data["ORCHESTRATOR_MAX_TASKS"] = max(1, int(data.get("ORCHESTRATOR_MAX_TASKS"))) \
        if str(data.get("ORCHESTRATOR_MAX_TASKS") or "").strip().isdigit() else 5
    # Planner/router model: a ModelKey pin (provider|base_url|model) for the
    # planning turn, or "" for the automatic smart+fast pick. Stored verbatim.
    config_data["ORCHESTRATOR_PLANNER_PIN"] = str(data.get("ORCHESTRATOR_PLANNER_PIN") or "").strip()
    # chat_pin: a hard ModelKey pin (provider|base_url|model) for chat, or ""
    # for the auto picker. Replaces the old int chat_slot; stored verbatim.
    config_data["chat_pin"] = str(data.get("chat_pin") or "").strip()
    config_data["SCHEDULER_ENABLED"] = data.get("SCHEDULER_ENABLED") == "on"
    config_data["SCHEDULER_WEEKEND_FULL"] = data.get("SCHEDULER_WEEKEND_FULL") == "on"
    config_data["TRIAGE_ONLY_MODE"] = data.get("TRIAGE_ONLY_MODE") == "on"

    repo_tests_raw = data.get("repo_tests", "")
    if repo_tests_raw:
        new_tests = {}
        for pair in repo_tests_raw.split(","):
            if ":" in pair:
                repo, cmd = pair.split(":", 1)
                new_tests[repo.strip()] = cmd.strip()
        config_data["repo_tests"] = new_tests

    save_config(config_data)

    env_vars = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    env_vars[k] = v

    for k, v in data.items():
        if k in updates:
            env_vars[k] = v

    with open(ENV_FILE, "w") as f:
        for k, v in env_vars.items():
            f.write(f"{k}={v}\n")

    _reset_llm_semaphore()

    try:
        validate_llm_config_on_startup()
    except Exception as ve:
        logger.warning(f"Post-save LLM validation failed (non-fatal): {ve}")

    # AJAX saves (Settings tabs) request JSON + a toast instead of a full
    # redirect/reload. Honor that when the client signals Accept: application/json.
    _save_msg = "Settings saved"
    if _save_warnings:
        _save_msg += " (" + "; ".join(_save_warnings) + ")"
    if "application/json" in (request.headers.get("accept") or ""):
        return {"status": "ok", "message": _save_msg}
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/api/llm/credentials")
async def save_llm_credential(request: Request):
    """Save or update a provider credential in the vault."""
    data = await request.json()
    provider = (data.get("provider") or "").lower().strip()
    if not provider:
        return JSONResponse(status_code=400, content={"error": "provider required"})
    config = load_config()
    creds = config.setdefault("llm_credentials", {})
    existing = creds.get(provider) or {}
    # api_key is only present in the payload when the operator actually typed
    # into the (never-populated-with-the-real-value) key field on the
    # Settings page — see settings_page's redaction + saveCredential's JS.
    # Absent means "leave the stored key alone", not "clear it"; an empty
    # string IS a legitimate value here (an explicit clear when the operator
    # types into the field then deletes it).
    creds[provider] = {
        "api_key": (data.get("api_key") if "api_key" in data else existing.get("api_key")) or "",
        "base_url": (data.get("base_url") or "").strip(),
    }
    save_config(config)
    return {"status": "ok", "provider": provider}


@router.delete("/api/llm/credentials")
async def delete_llm_credential(request: Request):
    """Deconfigure a provider: drop its stored credential (api_key + base_url)
    from the vault. Existing llm_entries that reference the provider are left
    intact (they may still work via a per-entry key/url) — the response reports
    how many still point at it so the operator can decide whether to clean them
    up."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    provider = (data.get("provider") or "").lower().strip()
    if not provider:
        return JSONResponse(status_code=400, content={"error": "provider required"})
    config = load_config()
    creds = config.get("llm_credentials") or {}
    removed = provider in creds
    creds.pop(provider, None)
    config["llm_credentials"] = creds
    # Copilot stores an OAuth token, not a pasted key — clear the in-flight
    # device code + cached API token too so a deconfigure is a full sign-out.
    if provider == "copilot":
        config.pop("copilot_device", None)
        state.pop("copilot_auth", None)
        state.pop("copilot_device", None)
    save_config(config)
    entries_using = sum(1 for e in (config.get("llm_entries") or [])
                        if (e.get("provider") or "").lower().strip() == provider)
    logger.info("LLM credential deconfigured for '%s' (removed=%s, %d entr%s still reference it).",
                provider, removed, entries_using, "y" if entries_using == 1 else "ies")
    return {"status": "ok", "provider": provider, "removed": removed, "entries_using": entries_using}


def _copilot_poll_loop(device_code, interval, provider):
    """SERVER-SIDE device-flow poll: after the user authorizes in GitHub, poll for the
    token here (independent of the browser, so a page reload/restart can't strand it),
    store it as the copilot credential, and report progress via state['copilot_auth']."""
    import requests as _rq, time as _t
    from urllib.parse import parse_qs
    from main import COPILOT_CLIENT_ID, GITHUB_OAUTH_TOKEN_URL
    poll = max(5, int(interval or 5))
    deadline = _t.time() + 900  # GitHub device codes live ~15 min
    state["copilot_auth"] = {"status": "pending", "message": "Waiting for you to authorize in GitHub…"}
    while _t.time() < deadline:
        _t.sleep(poll)
        try:
            r = _rq.post(GITHUB_OAUTH_TOKEN_URL, headers={"Accept": "application/json"},
                         data={"client_id": COPILOT_CLIENT_ID, "device_code": device_code,
                               "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, timeout=20)
            if r.headers.get("content-type", "").startswith("application/json"):
                d = r.json()
            else:
                d = {k: v[0] for k, v in parse_qs(r.text).items()}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Copilot poll request error: {e}")
            continue
        if d.get("access_token"):
            cfg = load_config()
            cfg.setdefault("llm_credentials", {})[provider] = {"api_key": d["access_token"], "base_url": ""}
            cfg.pop("copilot_device", None)
            save_config(cfg)
            state["copilot_auth"] = {"status": "authorized", "message": "Copilot connected."}
            logger.info(f"Copilot device flow: authorized + stored credential for '{provider}'.")
            return
        err = d.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            poll += 5
            continue
        logger.warning(f"Copilot poll: GitHub error={err!r} desc={d.get('error_description')!r}")
        state["copilot_auth"] = {"status": "error",
                                 "message": (f"{err}: {d.get('error_description') or ''}".strip(": ")
                                             or "authorization failed")}
        return
    state["copilot_auth"] = {"status": "error", "message": "Device code expired — click Sign in again."}


@router.post("/api/copilot/device-start")
async def copilot_device_start():
    """Begin GitHub Copilot OAuth device flow: get a device+user code and kick off a
    SERVER-SIDE poll loop that completes the auth once the user authorizes in GitHub."""
    import requests as _rq
    from main import COPILOT_CLIENT_ID, GITHUB_DEVICE_CODE_URL
    try:
        r = _rq.post(GITHUB_DEVICE_CODE_URL, headers={"Accept": "application/json"},
                     data={"client_id": COPILOT_CLIENT_ID, "scope": "read:user"}, timeout=20)
        d = r.json()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(e)})
    if not d.get("device_code"):
        return JSONResponse(status_code=502,
                            content={"error": d.get("error_description") or "device code request failed"})
    interval = int(d.get("interval", 5))
    logger.info("Copilot device flow: issued user code %s (verify at %s)",
                d.get("user_code"), d.get("verification_uri"))
    threading.Thread(target=_copilot_poll_loop, args=(d["device_code"], interval, "copilot"),
                     daemon=True, name="copilot-auth").start()
    return {"user_code": d.get("user_code"), "verification_uri": d.get("verification_uri"),
            "expires_in": d.get("expires_in"), "interval": interval}


@router.get("/api/copilot/status")
async def copilot_status():
    """Progress of the in-flight Copilot device-flow auth (polled by the UI)."""
    return state.get("copilot_auth") or {"status": "idle", "message": ""}


@router.post("/api/copilot/signout")
async def copilot_signout(request: Request):
    """Clear a stored Copilot authorization: drop the credential (GitHub token) + any
    in-flight device code, and evict the cached Copilot API token."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    provider = (body.get("provider") or "copilot").lower().strip() or "copilot"
    config = load_config()
    creds = config.get("llm_credentials") or {}
    gh = (creds.get(provider) or {}).get("api_key")
    creds.pop(provider, None)
    config["llm_credentials"] = creds
    config.pop("copilot_device", None)
    save_config(config)
    state.pop("copilot_auth", None)
    state.pop("copilot_device", None)
    try:
        from main import _COPILOT_TOKEN_CACHE
        if gh:
            _COPILOT_TOKEN_CACHE.pop(gh, None)
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"Copilot: signed out '{provider}' (cleared credential + token cache).")
    return {"status": "ok"}


@router.get("/api/copilot/models")
async def copilot_models(provider: str = "copilot"):
    """List models available to this Copilot subscription (for the model dropdown)."""
    import requests as _rq
    from main import _copilot_api_token, _copilot_headers, COPILOT_API_BASE
    cred = (load_config().get("llm_credentials") or {}).get((provider or "copilot").lower()) or {}
    gh = cred.get("api_key")
    if not gh:
        return {"models": [], "error": "not authenticated — sign in with GitHub first"}
    try:
        # _copilot_api_token + the models GET are blocking HTTP (up to 20s) —
        # offload so a slow Copilot response can't freeze the event loop.
        def _fetch():
            tok = _copilot_api_token(gh)
            r = _rq.get(f"{COPILOT_API_BASE}/models", headers=_copilot_headers(tok), timeout=20)
            data = r.json()
            return sorted({m.get("id") for m in (data.get("data") or []) if m.get("id")})
        models = await asyncio.to_thread(_fetch)
        return {"models": models}
    except Exception as e:  # noqa: BLE001
        return {"models": [], "error": str(e)}


def _refresh_llm_endpoints():
    """Kick an immediate re-probe of the configured LLM endpoints.

    Without this, `state["llm_endpoints_online"]` — which the header Providers
    chip renders from — was only rebuilt by connectivity_worker's 15-minute
    tick, so a just-added provider/model did not appear until then. Imported
    lazily because workers imports main, and a module-level import here would
    close an import cycle. Best-effort: never let a refresh failure break the
    save that triggered it.
    """
    try:
        from workers import refresh_llm_endpoints_async
        refresh_llm_endpoints_async()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"_refresh_llm_endpoints failed: {e}")


@router.post("/api/llm/entries")
async def create_llm_entry(request: Request):
    """Create a new named provider/model entry."""
    data = await request.json()
    entry = {
        "id": str(uuid.uuid4())[:12],
        "label": (data.get("label") or "").strip(),
        "provider": (data.get("provider") or "openai").lower().strip(),
        "model": (data.get("model") or "").strip(),
        "rpm": int(data.get("rpm") or 0),
        # Per-entry base_url / api_key override the shared per-provider credential,
        # so e.g. three `ollama` entries can independently target local CPU
        # (http://localhost:11434), a remote-GPU box on the LAN, and Ollama Cloud.
        "base_url": (data.get("base_url") or "").strip(),
        "api_key": (data.get("api_key") or "").strip(),
        # Enabled endpoints are ranked by the capability/cost picker for every
        # call; a disabled entry stays configured but out of routing. Missing =
        # enabled (the default for a freshly added endpoint).
        "enabled": bool(data.get("enabled", True)),
    }
    if not entry["model"]:
        return JSONResponse(status_code=400, content={"error": "model required"})
    config = load_config()
    config.setdefault("llm_entries", []).append(entry)
    save_config(config)
    _refresh_llm_endpoints()
    return {"status": "ok", "entry": entry}


@router.put("/api/llm/entries/{entry_id}")
async def update_llm_entry(entry_id: str, request: Request):
    """Update an existing named provider/model entry."""
    data = await request.json()
    config = load_config()
    entries = config.get("llm_entries") or []
    for e in entries:
        if e.get("id") == entry_id:
            e["label"] = (data.get("label") or e.get("label") or "").strip()
            e["provider"] = (data.get("provider") or e.get("provider") or "openai").lower().strip()
            e["model"] = (data.get("model") or e.get("model") or "").strip()
            # rpm=0 means UNLIMITED, and 0 is falsy -- chaining `or` through the
            # stored value made unlimited unsettable: saving 0 silently restored
            # the previous limit. Fall back only when the key is absent, matching
            # the partial-update convention used for the fields below.
            if "rpm" in data:
                try:
                    e["rpm"] = max(0, int(data.get("rpm") or 0))
                except (TypeError, ValueError):
                    e["rpm"] = 0
            # Per-entry overrides. Only overwrite when the key is present in the
            # payload so a partial update doesn't wipe an existing value.
            if "base_url" in data:
                e["base_url"] = (data.get("base_url") or "").strip()
            if "api_key" in data:
                e["api_key"] = (data.get("api_key") or "").strip()
            if "enabled" in data:
                e["enabled"] = bool(data.get("enabled"))
            save_config(config)
            _refresh_llm_endpoints()
            return {"status": "ok", "entry": e}
    return JSONResponse(status_code=404, content={"error": "entry not found"})


@router.delete("/api/llm/entries/{entry_id}")
async def delete_llm_entry(entry_id: str):
    """Delete a named endpoint entry."""
    config = load_config()
    config["llm_entries"] = [e for e in (config.get("llm_entries") or []) if e.get("id") != entry_id]
    save_config(config)
    _refresh_llm_endpoints()
    return {"status": "ok"}


@router.get("/api/llm/config")
async def get_llm_config():
    """Return current vault credentials (keys redacted) and entries (keys redacted)."""
    config = load_config()
    creds = config.get("llm_credentials") or {}
    safe_creds = {p: {"configured": bool(v.get("api_key")), "base_url": v.get("base_url", "")}
                  for p, v in creds.items()}
    # SECURITY: entries carry a per-entry plaintext api_key override — never
    # return it. Mirrors safe_creds' configured-flag treatment above.
    safe_entries = [{**e, "api_key": "", "has_key": bool(e.get("api_key"))}
                    for e in (config.get("llm_entries") or [])]
    return {
        "credentials": safe_creds,
        "entries": safe_entries,
    }


@router.get("/api/console/accounts")
async def get_console_accounts():
    """Get available usernames from vault and current console account selection."""
    import auth as _a
    config = load_config()

    # Get all available usernames
    users = _a.list_users()

    # Get current selection
    selected = (config.get("console_accounts") or []).copy()

    return {
        "available": users,
        "selected": selected,
    }


@router.post("/api/console/accounts")
async def save_console_accounts(request: Request):
    """Save selected console account usernames and push to LM hub."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    selected = (data.get("selected") or []).copy()

    # Validate all selected usernames exist
    import auth as _a
    all_users = {u["username"] for u in _a.list_users()}
    invalid = [u for u in selected if u not in all_users]
    if invalid:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid usernames: {', '.join(invalid)}"},
        )

    # Save to config
    config = load_config()
    config["console_accounts"] = selected
    save_config(config)

    # Relay to the LM hub, which owns the console module. Persisted above, so a
    # dropped relay is never data loss — the next save re-pushes it.
    try:
        from hub_agent import hub_agent_client
        if hub_agent_client:
            hub_agent_client.send_console_accounts(selected)
    except Exception as e:  # noqa: BLE001
        logger.debug("Failed to push console accounts to hub: %s", e)

    return {"status": "ok", "selected": selected}


@router.post("/api/local-llm/setup")
async def local_llm_setup(request: Request):
    """Kick off the one-click local (CPU-only) LLM setup in the background.

    Body (all optional, defaults applied): {model, num_ctx, cores}. The model is
    registered as an enabled endpoint (routing is capability/cost-aware now — no
    slot to assign). Returns immediately with the task_id the UI polls.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    model = (data.get("model") or "qwen2.5-coder:14b").strip()
    try:
        num_ctx = int(data.get("num_ctx") or 32768)
    except (TypeError, ValueError):
        num_ctx = 32768
    try:
        cores = int(data.get("cores") or state.get("cpu_count") or os.cpu_count() or 4)
    except (TypeError, ValueError):
        cores = os.cpu_count() or 4
    if "LocalLLMSetup" in state.get("active_tasks", {}):
        return JSONResponse(status_code=409, content={"status": "busy", "message": "A local LLM setup is already running."})
    threading.Thread(target=run_local_llm_setup, args=(model, num_ctx, cores), daemon=True).start()
    return {"status": "started", "task_id": "LocalLLMSetup"}


@router.get("/api/local-llm/status")
async def local_llm_status():
    """Whether a setup is running + the last-run summary + detected core count."""
    return {
        "running": "LocalLLMSetup" in state.get("active_tasks", {}),
        "last": state.get("local_llm_setup") or {},
        "cpu_count": state.get("cpu_count") or os.cpu_count() or 4,
    }


@router.get("/api/local-llm/models")
async def local_llm_models(base_url: str = ""):
    """List the models on an ollama endpoint (defaults to the local server).
    Pass ?base_url=http://<host>:11434 to manage a remote instance (e.g. the M4)."""
    url = (base_url or OLLAMA_BASE_URL).strip() or OLLAMA_BASE_URL
    # _ollama_models_detailed makes a blocking HTTP call — offload it so a slow
    # or unreachable ollama endpoint can't stall the whole event loop.
    models = await asyncio.to_thread(_ollama_models_detailed, url)
    return {"base_url": url, "models": models}


@router.post("/api/local-llm/pull")
async def local_llm_pull(request: Request):
    """Pull a model in the background. Body: {model, base_url?}. Poll progress via
    /api/task-details?task_id=LocalLLMPull."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    model = (data.get("model") or "").strip()
    if not model:
        return JSONResponse(status_code=400, content={"message": "model required"})
    base_url = (data.get("base_url") or OLLAMA_BASE_URL).strip() or OLLAMA_BASE_URL
    if "LocalLLMPull" in state.get("active_tasks", {}):
        return JSONResponse(status_code=409, content={"message": "A model pull is already running."})
    threading.Thread(target=run_local_llm_pull, args=(model, base_url), daemon=True).start()
    return {"status": "started", "task_id": "LocalLLMPull"}


@router.post("/api/local-llm/delete")
async def local_llm_delete(request: Request):
    """Delete a model. Body: {model, base_url?}."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    model = (data.get("model") or "").strip()
    if not model:
        return JSONResponse(status_code=400, content={"message": "model required"})
    base_url = (data.get("base_url") or OLLAMA_BASE_URL).strip() or OLLAMA_BASE_URL
    ok, msg = _ollama_delete(model, base_url)
    if not ok:
        return JSONResponse(status_code=502, content={"message": f"Delete failed: {msg}"})
    return {"status": "ok", "message": f"{model}: {msg}"}


@router.post("/clear_history")
async def clear_history():
    """Clears all processed issues and resets success/failure counters."""
    global state
    logger.info("Clearing all issue history and resetting counters.")

    state["processed"] = {}
    save_processed({})
    recompute_issue_counters({})

    return {"status": "success", "message": "All history and tasks have been cleared."}


def _close_issue_on_github(issue_id: str):
    """Close one issue on GitHub with the ``ab-dismissed`` label + an
    explanatory comment. Returns (ok, message). Best-effort per step — label
    create/apply and the comment never raise. Shared by the single-issue
    delete and the bulk delete-all sweep so the close logic isn't duplicated."""
    if ":" not in issue_id:
        raise ValueError("Invalid issue_id")
    repo_name, issue_num_str = issue_id.rsplit(":", 1)
    issue_num = int(issue_num_str)
    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise ValueError("No GitHub token configured")
    gh = Github(token)
    repo = gh.get_repo(repo_name)
    issue = repo.get_issue(issue_num)

    # Ensure the dismissal label exists in the repo; create it if not.
    label_name = "ab-dismissed"
    try:
        repo.get_label(label_name)
    except Exception:
        try:
            repo.create_label(label_name, "b60205",
                              "Marked by AppBuilder as not a real issue — will not be reopened")
        except Exception as le:
            logger.warning(f"Could not create label '{label_name}': {le}")
    try:
        issue.add_to_labels(label_name)
    except Exception as le:
        logger.warning(f"Could not apply label '{label_name}' to #{issue_num}: {le}")
    try:
        issue.create_comment(
            "🤖 **AppBuilder**: This issue has been marked as **not a real issue** and dismissed. "
            "It will not be automatically reopened or processed again."
        )
    except Exception:
        pass
    if issue.state != "closed":
        issue.edit(state="closed")
        return True, f"Issue #{issue_num} labelled '{label_name}' and closed on GitHub."
    return True, f"Issue #{issue_num} labelled '{label_name}' (was already closed)."


_DISMISS_MAX_RETRIES = 5


def _close_issue_on_github_with_retry(issue_id: str):
    """Runs _close_issue_on_github with up to _DISMISS_MAX_RETRIES attempts
    (short exponential backoff between tries), recording the outcome in
    state["dismiss_jobs"][issue_id] so the WebUI can poll for completion and
    toast the result instead of blocking the request on the GitHub round trip."""
    global state
    last_err = None
    for attempt in range(1, _DISMISS_MAX_RETRIES + 1):
        try:
            ok, msg = _close_issue_on_github(issue_id)
            state["dismiss_jobs"][issue_id] = {"status": "done", "message": msg}
            logger.info(f"Dismissed issue {issue_id} on GitHub (attempt {attempt}): {msg}")
            return
        except Exception as e:
            last_err = e
            logger.warning(f"Dismiss {issue_id}: GitHub close attempt {attempt}/{_DISMISS_MAX_RETRIES} failed: {e}")
            if attempt < _DISMISS_MAX_RETRIES:
                time.sleep(min(2 ** (attempt - 1), 10))
    state["dismiss_jobs"][issue_id] = {
        "status": "error",
        "message": f"GitHub close failed after {_DISMISS_MAX_RETRIES} attempts: {last_err}",
    }
    logger.error(f"Dismiss {issue_id}: gave up after {_DISMISS_MAX_RETRIES} attempts: {last_err}")


@router.post("/delete_issue")
async def delete_issue(request: Request):
    """Remove an issue from local history immediately; close it on GitHub in a
    background thread (retried up to _DISMISS_MAX_RETRIES times) so the button
    doesn't block on the GitHub round trip. The WebUI polls /dismiss_status/
    to toast the real outcome once the background job finishes."""
    global state
    data = await request.json()
    issue_id = data.get("issue_id", "").strip()
    if not issue_id or ":" not in issue_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid issue_id"})

    repo_name, issue_num_str = issue_id.rsplit(":", 1)
    try:
        issue_num = int(issue_num_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid issue number"})

    # Remove from local processed history.
    processed = load_processed()
    was_in_history = issue_id in processed
    if was_in_history:
        processed.pop(issue_id)
        state["processed"] = processed
        save_processed(processed)
        recompute_issue_counters(processed)

    state["dismiss_jobs"][issue_id] = {"status": "pending", "message": ""}
    threading.Thread(target=_close_issue_on_github_with_retry, args=(issue_id,), daemon=True).start()

    return {
        "status": "success",
        "message": f"{'Removed from history. ' if was_in_history else ''}Closing on GitHub in the background…",
        "issue_id": issue_id,
        "background": True,
    }


@router.get("/dismiss_status")
async def dismiss_status(issue_id: str):
    """Polled by the WebUI after a Dismiss click to learn when the background
    GitHub close (with retry) has finished, so it can toast the real result.
    issue_id (e.g. "owner/repo:123") comes in as a query param, not a path
    segment, since it contains a "/" that path routing would mangle."""
    job = state["dismiss_jobs"].get(issue_id)
    if job is None:
        return {"status": "unknown"}
    return job


@router.post("/delete_all_issues")
async def delete_all_issues(request: Request):
    """Clear every issue from local history and close them all on GitHub.
    Local history + counters are wiped immediately (the feed empties on
    reload); the GitHub closes run in a background thread so a large set
    can't time out the request. Mirrors the retry_all_failed background
    pattern. Each issue is closed best-effort — one failure doesn't abort
    the sweep."""
    global state
    processed = load_processed()
    to_close = list(processed.keys())
    if not to_close:
        return {"status": "no_issues", "message": "No issues in history to delete."}

    # Clear local history + counters now; close on GitHub in the background
    # against the snapshot so the clear doesn't race the sweep.
    state["processed"] = {}
    save_processed({})
    recompute_issue_counters({})

    def _bulk_close():
        closed = failed = 0
        for issue_id in to_close:
            try:
                ok, _msg = _close_issue_on_github(issue_id)
                if ok:
                    closed += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                logger.warning(f"delete_all: could not close {issue_id} on GitHub: {e}")
        logger.info(f"delete_all_issues: closed {closed}/{len(to_close)} on GitHub, {failed} failed.")

    threading.Thread(target=_bulk_close, daemon=True).start()
    return {
        "status": "success",
        "message": f"Cleared {len(to_close)} issue(s) from history. Closing them on GitHub in the background.",
    }


@router.post("/resolve_issue")
async def resolve_issue(request: Request):
    """Mark an issue as resolved: close it on GitHub and set its local status to fixed."""
    global state
    data = await request.json()
    issue_id = data.get("issue_id", "").strip()
    if not issue_id or ":" not in issue_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid issue_id"})

    repo_name, issue_num_str = issue_id.rsplit(":", 1)
    try:
        issue_num = int(issue_num_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid issue number"})

    # Close the issue on GitHub first. We only update the local status to fixed
    # if this succeeds (including the already-closed case); on failure we leave
    # the local state untouched so the UI and history don't claim a fix that
    # never landed on GitHub.
    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse(status_code=500, content={
            "status": "error",
            "message": "GitHub close failed: No GitHub token configured. Local status left unchanged.",
        })

    def _do_resolve():
        gh = Github(token)
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(issue_num)

        try:
            issue.create_comment(
                "🤖 **AppBuilder**: This issue has been marked as **resolved** and is now closed. "
                "It will not be automatically reopened or processed again."
            )
        except Exception:
            pass

        if issue.state != "closed":
            issue.edit(state="closed")
            msg = f"Issue #{issue_num} closed on GitHub."
        else:
            msg = f"Issue #{issue_num} was already closed on GitHub."
        # Human sign-off → tell the hub the bug report is fixed, ALWAYS — even when the
        # issue was already closed on GitHub. (This used to be inside the "if not
        # closed" branch, so an already-closed issue never flipped the LM report to
        # Fixed.)
        try:
            from fix_engine import _notify_bug_fixed
            _notify_bug_fixed(issue)  # LM "File a Bug" → hub → UI shows "Fixed"
        except Exception:
            pass
        # Apply the closed label (best-effort; existing labels kept).
        _apply_closed_label(repo, issue, issue_id)
        return msg

    try:
        # PyGithub is synchronous — every call inside _do_resolve blocks, same
        # note as pr_review_approve. Offloaded so a slow GitHub response can't
        # stall the whole app.
        github_msg = await asyncio.get_event_loop().run_in_executor(None, _do_resolve)
        logger.info(f"Resolved issue {issue_id}: status -> closed, {github_msg}")
    except Exception as e:
        logger.warning(f"Could not close {issue_id} on GitHub: {e}")
        return JSONResponse(status_code=502, content={
            "status": "error",
            "message": f"GitHub close failed: {e}. Local status left unchanged.",
        })

    # GitHub close succeeded — clicking Resolved is a HUMAN sign-off, so the issue
    # moves into the RESOLVED bucket (status "resolved"), NOT Closed. Counters are
    # re-derived from the store so it lands in exactly one bucket.
    processed = load_processed()
    local_msg = "No local history entry to update, but "
    if issue_id in processed:
        entry = processed[issue_id]
        entry["status"] = "resolved"
        entry["timestamp"] = datetime.now().isoformat()
        entry["decision_reason"] = "Human-confirmed resolved."
        processed[issue_id] = entry
        save_processed(processed)
        recompute_issue_counters(processed)
        state["processed"] = processed
        local_msg = "Marked resolved (human-confirmed). "

    return {
        "status": "success",
        "message": f"{local_msg}{github_msg}",
    }


@router.post("/update_now")
async def update_now():
    updated, msg = check_for_updates()
    logger.info(f"Manual update check: {msg}")
    return {"status": "success", "message": msg}


@router.post("/api/clear-credit-cooldown/{n}")
async def clear_credit_cooldown(n: int):
    """Manually clear the 1-hour credit-exhaustion cooldown for provider n (1/2/3)."""
    if n not in (1, 2, 3, 4):
        return JSONResponse(status_code=400, content={"error": "n must be 1, 2, 3, or 4"})
    with _PROVIDER_CREDIT_CB_LOCK:
        _PROVIDER_CREDIT_CB[n]["cooldown_until"] = 0.0
        _PROVIDER_CREDIT_CB[n]["tripped_at"] = None
        _PROVIDER_CREDIT_CB[n]["reason"] = None
    state["provider_credit_cb"] = _provider_credit_cb_snapshot()
    logger.info(f"Credit cooldown for Provider {n} manually cleared.")
    return {"status": "cleared", "provider": n}


@router.post("/trigger_fix")
async def trigger_fix(request: Request):
    data = await request.json()
    repo_name = data.get("repo_name")
    issue_num = data.get("issue_num")
    llm_pref = data.get("llm_preference")

    if not repo_name or not issue_num:
        return JSONResponse(status_code=400, content={"message": "Missing repo_name or issue_num"})

    logger.info(f"Manual trigger: Fixing {repo_name}:{issue_num} with preference {llm_pref}")

    def run_fix():
        success, msg = process_single_issue(repo_name, int(issue_num), llm_preference=llm_pref)
        if success:
            logger.info(f"Manual fix successful for {repo_name}:{issue_num}")
        else:
            logger.error(f"Manual fix failed for {repo_name}:{issue_num}: {msg}")

    threading.Thread(target=run_fix, daemon=True).start()
    return {"status": "triggered", "message": f"Fix process started for {repo_name}:{issue_num}"}


@router.post("/scan_now")
async def scan_now():
    def trigger():
        state["status"] = "Manual Scan"
        run_scan_cycle()
    threading.Thread(target=trigger, daemon=True).start()
    return {"status": "triggered", "message": "Manual scan cycle started in background."}


@router.post("/retry_issue")
async def retry_issue(request: Request):
    data = await request.json()
    issue_id = data.get("issue_id")

    if not issue_id or ":" not in issue_id:
        return JSONResponse(status_code=400, content={"message": "Invalid issue_id format. Expected 'repo:num'"})

    repo_name, issue_num = issue_id.split(":")

    logger.info(f"Manual retry: {issue_id}")

    def run_fix():
        success, msg = process_single_issue(repo_name, int(issue_num))
        if success:
            logger.info(f"Manual retry successful for {issue_id}")
        else:
            logger.error(f"Manual retry failed for {issue_id}: {msg}")

    threading.Thread(target=run_fix, daemon=True).start()
    return {"status": "triggered", "message": f"Retry started for {issue_id}"}


@router.post("/reopen_issue")
async def reopen_issue(request: Request):
    """Reopen a AppBuilder-closed issue on GitHub and re-queue it — for when AppBuilder
    reported a fix that did NOT actually resolve the bug (e.g. it committed +
    verified in its sandbox but the push never landed). Reopens the issue, strips
    the ``ab-closed`` / ``ab-dismissed`` labels that suppress
    re-processing, clears its stored processed state, and kicks off a fresh fix."""
    data = await request.json()
    issue_id = data.get("issue_id")
    if not issue_id or ":" not in issue_id:
        return JSONResponse(status_code=400, content={"message": "Invalid issue_id format. Expected 'repo:num'"})
    repo_name, issue_num_str = issue_id.rsplit(":", 1)
    try:
        issue_num = int(issue_num_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"message": "Invalid issue number"})
    config = load_config()
    token = config.get("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return JSONResponse(status_code=400, content={"message": "No GitHub token configured"})
    def _do_reopen():
        gh = Github(token)
        repo = gh.get_repo(repo_name)
        issue = repo.get_issue(issue_num)
        for lbl in ("ab-closed", "ab-dismissed"):
            try:
                issue.remove_from_labels(lbl)
            except Exception:  # noqa: BLE001 — label may not be present
                pass
        if issue.state != "open":
            issue.edit(state="open")
        try:
            issue.create_comment(
                "🔁 **AppBuilder**: Reopened by the operator — the previous fix did not "
                "actually resolve this. Re-queued for another attempt."
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        # PyGithub is synchronous — every call inside _do_reopen blocks, same
        # note as pr_review_approve. Offloaded so a slow GitHub response can't
        # stall the whole app.
        await asyncio.get_event_loop().run_in_executor(None, _do_reopen)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Reopen failed for {issue_id}: {e}")
        return JSONResponse(status_code=500, content={"message": f"Reopen failed: {e}"})
    # Mark the issue "reopened" (rather than deleting its record) so: (a) the base
    # closed/resolved counter drops NOW that it's no longer closed, and (b) the flag
    # carries into the eventual re-close so it's tallied in the ReOpened buckets — not
    # double-counted in the base "Issues Closed" total. Status "reopened" is not a
    # terminal status the scanner skips, and the direct re-queue below drives the fix.
    try:
        processed = load_processed()
        prior = processed.get(issue_id, {})
        processed[issue_id] = {
            "status": "reopened",
            "reopened": True,
            "original_body": prior.get("original_body", ""),
            # Preserve the prior fix so the re-fix can diff "what changed since" and
            # triage the regression (falls back to parsing the issue's Commit: comment).
            "prior_fix_commit": prior.get("commit"),
            "prior_fix_files": prior.get("files"),
            "timestamp": datetime.now().isoformat(),
        }
        save_processed(processed)
        recompute_issue_counters(processed)
        state["processed"] = processed
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Reopen: could not update processed state for {issue_id}: {e}")
    logger.info(f"Manual reopen: {issue_id}")

    def run_fix():
        success, msg = process_single_issue(repo_name, issue_num)
        logger.info(f"Reopen re-fix {'ok' if success else 'FAILED'} for {issue_id}: {msg}")

    threading.Thread(target=run_fix, daemon=True).start()
    return {"status": "triggered", "message": f"Reopened + re-queued {issue_id}"}


@router.post("/retry_all_failed")
async def retry_all_failed(request: Request):
    """Retries all issues that currently have a 'failed' or 'non-actionable' status with a given LLM preference."""
    data = await request.json()
    llm_pref = data.get("llm_preference")

    processed = load_processed()
    to_retry = [issue_id for issue_id, info in processed.items()
                if info.get("status") in ["failed", "non-actionable"]]

    if not to_retry:
        return {"status": "no_issues", "message": "No failed or non-actionable issues found to retry."}

    logger.info(f"Bulk retry triggered for {len(to_retry)} issues with preference {llm_pref}: {to_retry}")

    def bulk_run():
        config = load_config()
        max_w = int(config.get("MAX_CONCURRENT_FIXES", 2))
        with ThreadPoolExecutor(max_workers=max_w) as ex:
            futs = [
                ex.submit(process_single_issue, *issue_id.split(":"), llm_preference=llm_pref)
                for issue_id in to_retry
                if not state.get("paused")
            ]
            for f in futs:
                try:
                    f.result()
                except Exception as e:
                    logger.error(f"Bulk retry error: {e}")

    threading.Thread(target=bulk_run, daemon=True).start()
    return {"status": "triggered", "message": f"Bulk retry started for {len(to_retry)} issues using {llm_pref} LLM."}


@router.post("/restart")
async def restart_service():
    """Manual restart: flag the dedicated restart_worker instead of fire-and-forget
    sudo, so the restart goes through the same verified, detached, grace-windowed path
    as automatic self-updates."""
    logger.info("Manual restart requested — flagging restart_worker.")
    state["restart_pending"] = True
    _log_restart_event("manual_restart", "manual restart requested", ok=True)
    return {"status": "success", "message": "Restart scheduled (grace window applies)."}


@router.post("/trigger_hub_update")
async def trigger_hub_update():
    """Triggers an update on the Hub + all its spokes and agents.

    Uses the authenticated hub-agent WebSocket (TRIGGER_ALL_UPDATES) — the same
    path the post-fix auto-update uses. The old trigger_infrastructure_update()
    HTTP POST to UPDATE_API_URL is NOT used here: it hit a NetBox-sync endpoint
    (never a hub-update trigger) and the hub never honored those static-token
    HTTP calls, so the button silently no-op'd.
    """
    result = _trigger_spoke_updates(load_config())
    msg = result if isinstance(result, str) else "Hub update triggered"
    ok = msg.lower().startswith("hub update triggered")
    return {"status": "success" if ok else "error", "message": msg}


@router.get("/chat")
async def chat_page(request: Request, chat_id: str = None):
    """Server-rendered Chat view; renders the sidebar + the active conversation."""
    store = load_chats()
    if chat_id and set_active_chat(chat_id):
        store = load_chats()
    active_id = store["active_id"]
    conv = get_conversation(store, active_id) or store["conversations"][0]
    chats_list = [{"id": c["id"], "title": c.get("title", "")} for c in store["conversations"]]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "view": "chat",
            "state": state,
            "chats": chats_list,
            "active_chat_id": conv["id"],
            "active_chat_title": conv.get("title", "") or "New chat",
            "chat_history": conv.get("messages", []),
        },
    )


@router.post("/api/chat/new")
async def chat_new():
    """Creates a new empty conversation and makes it active."""
    cid = create_conversation()
    return {"chat_id": cid}


@router.post("/api/chat")
async def chat_send(request: Request):
    """Accepts a user message for a conversation, persists it, kicks off a reply."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body"})
    message = (data.get("message") or "").strip() if isinstance(data, dict) else ""
    if not message:
        return JSONResponse(status_code=400, content={"message": "Message is required"})
    chat_id = (data.get("chat_id") or "").strip() if isinstance(data, dict) else ""
    if not chat_id:
        chat_id = load_chats()["active_id"]

    appended = append_chat_message(chat_id, {
        "role": "user",
        "content": message,
        "ts": datetime.now().isoformat(),
    })
    if appended is None:
        return JSONResponse(status_code=404, content={"message": "Conversation not found"})

    with _chat_lock:
        state["chat_streams"][chat_id] = {"stream": "", "done": False, "error": None}
    update_task_state(chat_id, "Chat", action="start")

    threading.Thread(target=run_chat_reply, args=(chat_id,), daemon=True).start()
    return {"chat_id": chat_id}


@router.get("/api/chat/stream")
async def chat_stream(chat_id: str):
    """Polls the live assistant stream and completion state for a conversation."""
    with _chat_lock:
        entry = state["chat_streams"].get(chat_id)
        if entry is None:
            return {"done": True, "stream": "", "error": "Unknown chat_id"}
        stream_text = entry.get("stream", "")
        done = bool(entry.get("done"))
        error = entry.get("error")
    # Fold in any partial progress call_llm streamed into active_tasks.
    with _task_state_lock:
        task = state["active_tasks"].get(chat_id)
        if task and task.get("stream"):
            stream_text = task["stream"]
    return {"done": done, "stream": stream_text, "error": error}


@router.post("/api/chat/rename")
async def chat_rename(request: Request):
    """Renames a conversation."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body"})
    chat_id = (data.get("chat_id") or "").strip() if isinstance(data, dict) else ""
    title = (data.get("title") or "").strip() if isinstance(data, dict) else ""
    if not chat_id:
        return JSONResponse(status_code=400, content={"message": "chat_id is required"})
    ok = rename_conversation(chat_id, title)
    return {"ok": ok}


@router.post("/api/chat/delete")
async def chat_delete(request: Request):
    """Deletes a conversation and selects a new active one."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body"})
    chat_id = (data.get("chat_id") or "").strip() if isinstance(data, dict) else ""
    if not chat_id:
        return JSONResponse(status_code=400, content={"message": "chat_id is required"})
    new_active = delete_conversation(chat_id)
    with _chat_lock:
        state["chat_streams"].pop(chat_id, None)
    return {"active_chat_id": new_active}


@router.post("/api/chat/clear")
async def chat_clear():
    """Clears the active conversation's messages (keeps the conversation shell)."""
    with _chat_lock:
        store = load_chats()
        conv = get_conversation(store, store["active_id"])
        if conv:
            conv["messages"] = []
            conv["title"] = ""
        save_chats(store)
        state["chat_streams"].pop(store["active_id"], None)
    return {"ok": True}


@router.post("/api/chat/confirm_fix")
async def chat_confirm_fix(request: Request):
    """Confirms a chat-proposed automated fix and launches it in the background.

    The chat agent's propose_fix tool does NOT mutate GitHub; it registers a
    single-use, TTL-bounded confirmation token in state["chat_fix_proposals"]
    and emits a :::confirm_fix block the UI renders as a Confirm button. Only
    when the user clicks Confirm does this endpoint run: it validates + consumes
    the token, then launches process_single_issue in a daemon thread (the fix
    run clones, runs tests, and can take minutes — it must NOT block the chat or
    the request). Returns the pipeline's own issue_id task_id so the UI can watch
    progress via /api/task-details. Never returns any API key.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"message": "Invalid JSON body"})
    token = (data.get("token") or "").strip() if isinstance(data, dict) else ""
    if not token:
        return JSONResponse(status_code=400, content={"message": "Missing token"})

    config = load_config()
    ttl = int(config.get("CHAT_FIX_PROPOSAL_TTL", 600) or 600)
    with _chat_lock:
        prop = state.get("chat_fix_proposals", {}).pop(token, None)
    if not prop:
        return JSONResponse(status_code=410, content={"message": "Proposal expired or already used"})
    if time.time() - float(prop.get("created", 0)) > ttl:
        return JSONResponse(status_code=410, content={"message": "Proposal expired"})

    repo_name = prop.get("repo")
    issue_num = prop.get("number")
    pref = prop.get("llm_preference")
    if not repo_name or issue_num is None:
        return JSONResponse(status_code=400, content={"message": "Invalid proposal"})

    # Use the pipeline's own issue_id form so /api/task-details latches onto the
    # update_task_state entries process_single_issue creates internally.
    task_id = f"{repo_name}:{issue_num}"

    def _run():
        try:
            ok, msg = process_single_issue(repo_name, issue_num, llm_preference=pref)
            logger.info(f"Chat-triggered fix {repo_name}:{issue_num} -> ok={ok} msg={msg}")
        except Exception as e:
            logger.error(f"Chat-triggered fix {repo_name}:{issue_num} failed: {e}\n{traceback.format_exc()}")

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "triggered", "task_id": task_id, "repo": repo_name, "number": issue_num}


__all__ = [
    'hub_agent_status',
    'hub_agent_reregister',
    'health_check',
    'toggle_pause',
    'toggle_blackout',
    'dashboard',
    'get_task_details',
    'get_models',
    'fetch_models_live',
    'scheduler_status',
    'diagnostics',
    'claude_cli_status',
    'claude_cli_auth_start',
    'claude_cli_auth_poll',
    'claude_cli_auth_submit_code',
    'toggle_model',
    'get_logs',
    'get_hub_logs_page',
    'hub_logs_raw',
    'settings_page',
    'diagnostics_page',
    'save_settings',
    'save_llm_credential',
    'create_llm_entry',
    'update_llm_entry',
    'delete_llm_entry',

    'get_llm_config',
    'local_llm_setup',
    'local_llm_status',
    'clear_history',
    'delete_issue',
    'dismiss_status',
    'delete_all_issues',
    'resolve_issue',
    'update_now',
    'clear_credit_cooldown',
    'trigger_fix',
    'scan_now',
    'retry_issue',
    'retry_all_failed',
    'restart_service',
    'trigger_hub_update',
    'chat_page',
    'chat_new',
    'chat_send',
    'chat_stream',
    'chat_rename',
    'chat_delete',
    'chat_clear',
    'chat_confirm_fix',
    'log_analysis_run',
    'log_analysis_status',
    'log_health_worker',
    'DEFAULT_ENV',
    'router',
]
