"""
Windows compatibility shim for gltest's direct-mode message injection.

gltest.direct.loader._inject_message_to_fd0 (genlayer-test==0.29.2) does:
    os.dup2(fd, 0)   # duplicate the temp file's fd onto stdin
    os.close(fd)     # close the original fd
    os.unlink(path)  # delete the temp file

On POSIX this works because unlinking an open file just removes the
directory entry while the still-open fd (now living at fd 0) keeps the
data alive. On Windows, os.unlink refuses to remove a file that any
handle still has open - fd 0 still points at it via dup2 - so this raises
PermissionError (WinError 32) on every direct-mode contract deploy.

This is an upstream bug in the library, not in the contract under test.
We patch os.unlink to swallow exactly that failure so test collection
can proceed; the OS will actually delete the temp file once fd 0 is
closed/reused at process exit.
"""
import os

_original_unlink = os.unlink


def _tolerant_unlink(path, *args, **kwargs):
    try:
        _original_unlink(path, *args, **kwargs)
    except PermissionError:
        pass


os.unlink = _tolerant_unlink


# ----------------------------------------------------------------------------
# gltest.direct.wasi_mock._handle_web_render (genlayer-test==0.29.2) hardcodes
# `{"ok": {"image": b""}}` for mode="screenshot" REGARDLESS of any registered
# vm.mock_web(...) response -- it only ever honors the mock body for
# text/html mode. The real SDK (genlayer/gl/nondet/web.py) then does
# `PIL.Image.open(io.BytesIO(raw))` on that image, which always fails on
# empty bytes with PIL.UnidentifiedImageError. This makes it impossible to
# direct-mode test screenshot-type evidence in AgentIntentSettlement with
# the stock library, independent of contract correctness.
#
# Patch the mock handler (not the SDK) so mode="screenshot" honors the
# registered mock's body as raw image bytes, exactly like text/html mode
# already does. Tests then register a real, tiny, valid PNG as the mock
# body so the full pipeline -- including the real PIL decode -- is
# genuinely exercised end-to-end.
# ----------------------------------------------------------------------------
from gltest.direct import wasi_mock as _wasi_mock


def _patched_handle_web_render(vm, data):
    url = data.get("url", "")
    mode = data.get("mode", "text")

    mock_data = vm._match_web_mock(url, "GET")
    if mock_data:
        body = mock_data.get("body", "")
        if "response" in mock_data:
            body = mock_data["response"].get("body", "")
        if mode == "screenshot":
            if isinstance(body, str):
                body = body.encode("utf-8")
            return {"ok": {"image": body}}
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        return {"ok": {"text": body}}

    strict = getattr(vm, "_strict_mock_mode", False)
    if strict:
        registered = [f"GET {p.pattern}" for p, r in vm._web_mocks]
        raise _wasi_mock.MockNotFoundError(
            f"[strict] No web mock for WebRender {url}\n"
            f"  Registered: {registered or '(none)'}"
        )

    live_handler = getattr(vm, "_live_web_handler", None)
    if live_handler is not None:
        resp = live_handler({"url": url, "method": "GET", "headers": {}, "body": None})
        resp_data = resp.get("ok", {}).get("response", {})
        body = resp_data.get("body", b"")
        if mode == "screenshot":
            if isinstance(body, str):
                body = body.encode("utf-8")
            return {"ok": {"image": body}}
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        return {"ok": {"text": body}}

    registered = [f"GET {p.pattern}" for p, r in vm._web_mocks]
    raise _wasi_mock.MockNotFoundError(
        f"No web mock for WebRender {url}\n"
        f"  Registered: {registered or '(none)'}"
    )


_wasi_mock._handle_web_render = _patched_handle_web_render


# ----------------------------------------------------------------------------
# gltest.direct.wasi_mock._handle_gl_call (genlayer-test==0.29.2) has NO case
# for the "ExecPromptTemplate" gl_call request type -- the request
# gl.eq_principle.prompt_non_comparative uses internally (distinct from the
# plain "ExecPrompt" request gl.nondet.exec_prompt uses, which the mock
# already supports). Unpatched, this means prompt_non_comparative silently
# resolves to None in direct-mode tests -- a real gap in the test harness,
# not the SDK or this contract, that AgentIntentSettlement now depends on
# since switching from a hand-rolled comparative validator_fn (which hit a
# real DETERMINISTIC_VIOLATION on live GenVM) to this SDK-native primitive.
#
# Fix: handle ExecPromptTemplate by echoing the leader's own "input" text
# back as the agreed answer by default (a well-formed input surviving an
# equivalence check unchanged is the realistic behavior these tests
# construct), while still letting vm.mock_llm(pattern, response) override
# per call by matching against that same input text -- exactly the pattern
# tests/direct/*.py already use for the plain "ExecPrompt" case.
# ----------------------------------------------------------------------------
_original_handle_gl_call = _wasi_mock._handle_gl_call


def _patched_handle_gl_call(vm, request):
    if isinstance(request, dict) and "ExecPromptTemplate" in request:
        return _handle_exec_prompt_template(vm, request["ExecPromptTemplate"])
    return _original_handle_gl_call(vm, request)


def _handle_exec_prompt_template(vm, data):
    import json as _json

    match_text = data.get("input") or data.get("validator_answer") or ""

    override = vm._match_llm_mock(match_text) if match_text else None
    if override is not None:
        if not isinstance(override, str):
            override = _json.dumps(override)
        return {"ok": override}

    return {"ok": match_text}


_wasi_mock._handle_gl_call = _patched_handle_gl_call
