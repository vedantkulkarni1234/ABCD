#!/usr/bin/env python3
#
# ██╗   ██╗ ██████╗ ██╗██████╗
# ██║   ██║██╔═══██╗██║██╔══██╗
# ██║   ██║██║   ██║██║██║  ██║
# ╚██╗ ██╔╝██║   ██║██║██║  ██║
#  ╚████╔╝ ╚██████╔╝██║██████╔╝
#   ╚═══╝   ╚═════╝ ╚═╝╚═════╝
#
# VOID // GEMINI ORCHESTRATOR
# Ruthless multi-agent bug bounty workflow console (authorized testing only).
#
# Run:
#   python3 app.py

from __future__ import annotations

import json
import mimetypes
import os
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import gradio as gr

try:
    import google.generativeai as genai
    from google.generativeai.types import HarmBlockThreshold, HarmCategory
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "google-generativeai is required. Install with: pip install google-generativeai"
    ) from e


# ----------------------------
# UI skin (dark, green, red)
# ----------------------------

CSS = r"""
:root {
  --void-bg: #050607;
  --void-panel: #0b0e10;
  --void-border: #183018;
  --void-green: #39ff14;
  --void-green-dim: #18b84a;
  --void-red: #ff2a2a;
  --void-text: #c8ffd2;
  --void-muted: #83b38c;
}

html, body, .gradio-container { background: var(--void-bg) !important; color: var(--void-text) !important; }

/* Panels */
.gr-block, .gr-box, .gr-panel, .gr-form, .gr-group {
  background: var(--void-panel) !important;
  border-color: var(--void-border) !important;
}

/* Tabs */
button[role="tab"] {
  background: transparent !important;
  color: var(--void-muted) !important;
  border-bottom: 1px solid var(--void-border) !important;
}
button[role="tab"][aria-selected="true"] {
  color: var(--void-green) !important;
  border-bottom: 1px solid var(--void-green-dim) !important;
}

/* Inputs */
textarea, input, .wrap {
  background: #020304 !important;
  color: var(--void-text) !important;
  border-color: var(--void-border) !important;
}

/* Buttons */
button, .gr-button {
  background: #07110a !important;
  border-color: var(--void-border) !important;
  color: var(--void-green) !important;
}
button:hover {
  border-color: var(--void-green-dim) !important;
}

/* Chat */
.gr-chatbot {
  background: #050607 !important;
  border: 1px solid var(--void-border) !important;
}

/* Code blocks (monokai-ish) */
pre, code {
  background: #0a0f0d !important;
  color: #d7ffd9 !important;
  border: 1px solid #163a22 !important;
}

/* Critical findings accent */
.void-critical { color: var(--void-red) !important; font-weight: 700; }

/* Void banner container */
.void-hero {
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 18px 0 6px 0;
}
.void-hero pre {
  font-size: 16px;
  line-height: 1.05;
  color: var(--void-green);
  border: none !important;
  background: transparent !important;
}

.void-disclaimer {
  opacity: 0.75;
  font-size: 12px;
  color: var(--void-muted);
}
"""


# ----------------------------
# Core "agent" primitives
# ----------------------------

ChatHistory = List[Tuple[str, str]]


@dataclass
class AgentSpec:
    key: str
    title: str
    temperature: float
    system_prompt: str


@dataclass
class AgentState:
    chat: ChatHistory
    memory_summary: str


# ----------------------------
# Gemini wiring
# ----------------------------

# Safety: user asked for BLOCK_NONE on dangerous content.
# IMPORTANT: We still steer the model to produce authorized-testing guidance and
# avoid generating actionable wrongdoing. The UI is a tool; the prompts are the control layer.
DEFAULT_SAFETY = [
    {
        "category": HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        "threshold": HarmBlockThreshold.BLOCK_NONE,
    }
]


def _pick_best_flash_model(api_key: str) -> str:
    """Pick the best available flash model.

    We prefer experimental/preview '2.5 flash' if present, otherwise fall back to
    a stable 1.5 flash.

    This runs only after an API key exists.
    """

    genai.configure(api_key=api_key)

    # Priority order: newest → oldest. Keep this list short and defensive.
    preferred = [
        "gemini-2.5-flash-exp",
        "gemini-2.5-flash-preview",
        "gemini-2.5-flash",
        "gemini-2.0-flash-exp",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ]

    try:
        available = {m.name.replace("models/", "") for m in genai.list_models()}
    except Exception:
        return "gemini-1.5-flash"

    for name in preferred:
        if name in available:
            return name

    # Last resort: keep it 'flash'.
    for name in sorted(available):
        if "flash" in name:
            return name

    return "gemini-1.5-flash"


def _model(api_key: str, model_name: str, temperature: float) -> genai.GenerativeModel:
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name=model_name,
        safety_settings=DEFAULT_SAFETY,
        generation_config={
            "temperature": temperature,
            "top_p": 0.95,
            "max_output_tokens": 2048,
        },
    )


# ----------------------------
# Artifact ingestion (files/folders)
# ----------------------------

URL_RE = re.compile(r"https?://[^\s\"\'\)\]]+", re.IGNORECASE)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
DOMAIN_RE = re.compile(r"\b(?:(?:[a-z0-9-]+)\.)+(?:[a-z]{2,})\b", re.IGNORECASE)
PARAM_RE = re.compile(r"[\?&]([a-zA-Z0-9_\-]{1,64})=", re.IGNORECASE)

TECH_HINTS: Dict[str, re.Pattern[str]] = {
    "react": re.compile(r"\breact\b", re.IGNORECASE),
    "nextjs": re.compile(r"\bnext(?:\.js)?\b", re.IGNORECASE),
    "vue": re.compile(r"\bvue\b", re.IGNORECASE),
    "angular": re.compile(r"\bangular\b", re.IGNORECASE),
    "svelte": re.compile(r"\bsvelte\b", re.IGNORECASE),
    "jquery": re.compile(r"\bjquery\b", re.IGNORECASE),
    "graphql": re.compile(r"\bgraphql\b", re.IGNORECASE),
    "apollo": re.compile(r"\bapollo\b", re.IGNORECASE),
    "express": re.compile(r"\bexpress\b", re.IGNORECASE),
    "django": re.compile(r"\bdjango\b", re.IGNORECASE),
    "rails": re.compile(r"\brails\b", re.IGNORECASE),
    "laravel": re.compile(r"\blaravel\b", re.IGNORECASE),
    "spring": re.compile(r"\bspring\b", re.IGNORECASE),
    "cloudflare": re.compile(r"\bcloudflare\b", re.IGNORECASE),
}


def _coerce_paths(upload: Any) -> List[str]:
    """Normalize Gradio File outputs to filesystem paths."""

    if not upload:
        return []

    # Gradio can return:
    # - a single path string
    # - a dict with name
    # - an object with .name
    # - a list of any of the above
    if isinstance(upload, list):
        out: List[str] = []
        for item in upload:
            out.extend(_coerce_paths(item))
        return out

    if isinstance(upload, str):
        return [upload]

    if isinstance(upload, dict) and "name" in upload:
        return [str(upload["name"])]

    if hasattr(upload, "name"):
        return [str(getattr(upload, "name"))]

    # Fallback.
    return [str(upload)]


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    # Heuristic: NUL bytes tend to indicate binary.
    return b"\x00" in data[:4096]


def _read_head(path: str, max_bytes: int = 220_000) -> str:
    try:
        with open(path, "rb") as f:
            blob = f.read(max_bytes)
        if _looks_binary(blob):
            return "<binary>"
        return blob.decode("utf-8", errors="replace")
    except Exception:
        return "<unreadable>"


def _parse_har(text: str) -> Dict[str, Any]:
    try:
        doc = json.loads(text)
    except Exception:
        return {"entries": []}

    entries = []
    for e in (doc.get("log", {}) or {}).get("entries", [])[:2000]:
        req = e.get("request", {}) or {}
        url = req.get("url")
        method = req.get("method")
        q = req.get("queryString") or []
        params = [p.get("name") for p in q if isinstance(p, dict) and p.get("name")]
        entries.append({"method": method, "url": url, "params": params})

    return {"entries": entries}


def _extract_intel(text: str) -> Dict[str, List[str]]:
    urls = sorted(set(URL_RE.findall(text)))
    ips = sorted(set(IP_RE.findall(text)))
    emails = sorted(set(EMAIL_RE.findall(text)))
    domains = sorted(set(DOMAIN_RE.findall(text)))

    params: List[str] = []
    for u in urls:
        params.extend(PARAM_RE.findall(u))
    params = sorted(set(params))

    return {
        "urls": urls[:400],
        "domains": domains[:400],
        "ips": ips[:200],
        "emails": emails[:200],
        "params": params[:400],
    }


def _extract_tech(text: str) -> List[str]:
    hits: List[str] = []
    for name, pat in TECH_HINTS.items():
        if pat.search(text):
            hits.append(name)
    return sorted(set(hits))


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def build_artifact_pack(paths: Sequence[str]) -> Dict[str, Any]:
    """Build a compact artifact pack.

    Secret sauce (in code comments only):
    - Smart context compression: we cap file counts, byte counts, and include only
      the most relevant slices.
    - Auto-extraction: we derive IoCs/endpoints/params from raw bytes + parsed HAR.
    """

    # Cap: we are building *prompt context*, not a data lake.
    max_files = 60
    max_text_per_file = 40_000

    files = [p for p in paths if p and os.path.exists(p)][:max_files]

    text_blobs: List[Dict[str, str]] = []
    images: List[str] = []
    har_entries: List[Dict[str, Any]] = []
    skipped_binary: List[str] = []

    intel_aggregate = {
        "urls": set(),
        "domains": set(),
        "ips": set(),
        "emails": set(),
        "params": set(),
        "tech": set(),
    }

    for p in files:
        ext = Path(p).suffix.lower()
        mime = _guess_mime(p)

        if mime.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            images.append(p)
            continue

        head = _read_head(p)
        if head == "<binary>":
            skipped_binary.append(p)
            continue

        head = head[:max_text_per_file]
        text_blobs.append({"path": p, "mime": mime, "text": head})

        intel = _extract_intel(head)
        for k, vals in intel.items():
            intel_aggregate[k].update(vals)

        intel_aggregate["tech"].update(_extract_tech(head))

        if ext == ".har" or ("application/json" in mime and '"log"' in head and '"entries"' in head):
            har = _parse_har(head)
            for e in har.get("entries", [])[:1200]:
                if e.get("url"):
                    har_entries.append(e)
                    intel_aggregate["urls"].add(e.get("url"))
                    for prm in e.get("params") or []:
                        intel_aggregate["params"].add(prm)

    intel = {k: sorted(v)[:500] for k, v in intel_aggregate.items()}

    # Compact summary string for the LLM.
    summary_lines = [
        f"Artifacts: {len(files)} file(s) | Text: {len(text_blobs)} | Images: {len(images)} | HAR entries: {len(har_entries)} | Skipped binary: {len(skipped_binary)}",
    ]

    if intel["domains"]:
        summary_lines.append("Top domains: " + ", ".join(intel["domains"][:25]))
    if intel["urls"]:
        summary_lines.append("Top URLs: " + ", ".join(intel["urls"][:20]))
    if intel["params"]:
        summary_lines.append("Top params: " + ", ".join(intel["params"][:35]))
    if intel["tech"]:
        summary_lines.append("Tech hints: " + ", ".join(intel["tech"][:30]))

    return {
        "summary": "\n".join(summary_lines),
        "intel": intel,
        "text_blobs": text_blobs,
        "images": images,
        "har_entries": har_entries[:1200],
        "skipped_binary": skipped_binary[:120],
    }


def _format_artifacts_for_prompt(pack: Dict[str, Any]) -> str:
    intel = pack.get("intel", {})

    def _bul(k: str, limit: int) -> str:
        vals = intel.get(k) or []
        if not vals:
            return ""
        return f"- {k}: " + ", ".join(vals[:limit])

    lines = [
        pack.get("summary", "Artifacts: (none)"),
        _bul("tech", 40),
        _bul("domains", 30),
        _bul("urls", 25),
        _bul("params", 40),
        _bul("ips", 30),
        _bul("emails", 30),
    ]

    skipped = pack.get("skipped_binary", [])
    if skipped:
        lines.append("- skipped_binary_files: " + ", ".join(list(skipped)[:12]))

    blobs = pack.get("text_blobs", [])
    if blobs:
        lines.append("\nText excerpts (truncated):")
        for b in blobs[:8]:
            snippet = (b.get("text") or "")
            snippet = snippet[:2500]
            lines.append(f"\n[FILE] {b.get('path')} ({b.get('mime')})\n{snippet}")

    har = pack.get("har_entries", [])
    if har:
        lines.append("\nHAR quick index (method url params):")
        for e in har[:60]:
            method = e.get("method") or "?"
            url = e.get("url") or ""
            params = ",".join((e.get("params") or [])[:12])
            lines.append(f"- {method} {url} | {params}")

    return "\n".join([l for l in lines if l])


def _upload_images_for_prompt(api_key: str, paths: Sequence[str]) -> List[Any]:
    """Upload images to Gemini so the model can 'see' screenshots.

    NOTE: genai.upload_file is supported by google-generativeai. If upload fails,
    we fall back to text-only.
    """

    if not paths:
        return []

    genai.configure(api_key=api_key)
    uploaded: List[Any] = []
    for p in paths[:8]:
        try:
            uploaded.append(genai.upload_file(p))
        except Exception:
            continue
    return uploaded


# ----------------------------
# Prompting engine ("secret sauce")
# ----------------------------

# The user asked for:
# - Automatic chain-of-thought v2 prompting
# - Self-criticism loop
# - Dynamic tool-calling simulation (JSON)
# - Smart context compression
# - Automatic extraction of intel
#
# Policy note: we never display chain-of-thought. We explicitly instruct the model
# to keep private reasoning private and output only concise operator-grade results.


def _render_transcript(chat: ChatHistory, last_n: int = 10) -> str:
    buf: List[str] = []
    for u, a in chat[-last_n:]:
        buf.append(f"USER: {u}")
        buf.append(f"ASSISTANT: {a}")
    return "\n".join(buf)


def _maybe_compress_memory(api_key: str, model_name: str, spec: AgentSpec, state: AgentState) -> AgentState:
    """Compress long histories into a durable memory summary.

    This keeps the chat 'remembering everything' without blindly stuffing the entire
    transcript into every prompt.
    """

    total_chars = sum(len(u) + len(a) for u, a in state.chat)
    if total_chars < 40_000:
        return state

    # Keep the last few turns verbatim; summarize the rest.
    keep_tail = 10
    head = state.chat[:-keep_tail]
    tail = state.chat[-keep_tail:]

    if not head:
        return state

    summarizer = _model(api_key, model_name, temperature=0.2)
    head_transcript = _render_transcript(head, last_n=len(head))

    prompt = textwrap.dedent(
        f"""
        You are a memory compressor for a security testing assistant.

        Goal: produce a compact, durable 'memory' that preserves:
        - target(s), scope constraints, environment details
        - key endpoints, params, auth/session behaviors
        - hypotheses tested, results, anomalies, errors
        - next steps and open questions

        Rules:
        - Be terse, structured, and factual.
        - Do NOT include chain-of-thought.
        - Output <= 25 bullet points.

        Existing memory (may be empty):
        {state.memory_summary or '(none)'}

        Transcript to compress:
        {head_transcript}
        """
    ).strip()

    try:
        res = summarizer.generate_content(prompt)
        mem = (res.text or "").strip()
        state = AgentState(chat=tail, memory_summary=mem)
        return state
    except Exception:
        return state


def _gemini_call(
    *,
    api_key: str,
    model_name: str,
    spec: AgentSpec,
    state: AgentState,
    user_text: str,
    artifact_pack: Dict[str, Any],
    temperature_override: Optional[float] = None,
) -> str:
    """Single agent call with critique loop.

    Implementation notes:
    - We do a draft → critique → final loop.
    - Critique is hidden from the UI (not stored).
    - We allow multimodal (screenshots) by uploading images and attaching them as parts.
    """

    state = _maybe_compress_memory(api_key, model_name, spec, state)

    transcript = _render_transcript(state.chat, last_n=10)
    artifacts_text = _format_artifacts_for_prompt(artifact_pack)

    base_prompt = textwrap.dedent(
        f"""
        SYSTEM (role):
        {spec.system_prompt}

        SESSION MEMORY (compressed):
        {state.memory_summary or '(none)'}

        CONVERSATION (recent):
        {transcript or '(no prior messages)'}

        ARTIFACT INTEL:
        {artifacts_text}

        USER MESSAGE:
        {user_text}

        OPERATOR OUTPUT RULES:
        - Think step-by-step privately. Do NOT reveal chain-of-thought.
        - Output should be actionable for authorized security testing, but avoid instructions that enable real-world harm.
        - Prefer structured output: headings, checklists, tables.
        - Mark high-severity findings with: [CRITICAL]
        """
    ).strip()

    temp = temperature_override if temperature_override is not None else spec.temperature
    mdl = _model(api_key, model_name, temperature=temp)

    parts: List[Any] = [base_prompt]
    img_files = _upload_images_for_prompt(api_key, artifact_pack.get("images", []))
    parts.extend(img_files)

    # Draft
    draft = ""
    try:
        draft_res = mdl.generate_content(parts)
        draft = (draft_res.text or "").strip()
    except Exception as e:
        return f"[ERROR] Gemini call failed: {e}"

    # Critique (hidden)
    critic = _model(api_key, model_name, temperature=0.15)
    critique_prompt = textwrap.dedent(
        f"""
        You are an internal red-team reviewer.

        Task: critique the DRAFT answer for:
        - missing angles, weak assumptions, scope mistakes
        - unclear steps or poor prioritization
        - false positives / hallucinations

        Output:
        - <= 12 bullet points.
        - No chain-of-thought.

        DRAFT:
        {draft}
        """
    ).strip()

    critique = ""
    try:
        critique_res = critic.generate_content(critique_prompt)
        critique = (critique_res.text or "").strip()
    except Exception:
        critique = "(critique unavailable)"

    # Final
    final_prompt = textwrap.dedent(
        f"""
        Revise the DRAFT using the CRITIQUE.

        Rules:
        - No chain-of-thought.
        - Be terse, correct, and tactical.
        - If uncertain, label uncertainty and propose validation steps.

        DRAFT:
        {draft}

        CRITIQUE:
        {critique}
        """
    ).strip()

    try:
        final_res = mdl.generate_content(final_prompt)
        return (final_res.text or "").strip()
    except Exception:
        return draft


# ----------------------------
# Orchestration (Void tab)
# ----------------------------

def orchestrate_void(
    *,
    api_key: str,
    model_name: str,
    specs: Dict[str, AgentSpec],
    agent_states: Dict[str, AgentState],
    user_text: str,
    artifact_pack: Dict[str, Any],
) -> Tuple[str, Dict[str, AgentState]]:
    """Void orchestrator: plan → delegate → synthesize.

    We intentionally *simulate* tool calling via JSON so the orchestrator can
    dispatch to specialist agents deterministically.
    """

    void_spec = specs["void"]

    planner = _model(api_key, model_name, temperature=0.2)
    artifacts_text = _format_artifacts_for_prompt(artifact_pack)

    plan_prompt = textwrap.dedent(
        f"""
        SYSTEM:
        {void_spec.system_prompt}

        You are the orchestrator. Decide whether to call specialist agents.

        Allowed agent names (must match exactly):
        - Recon Observatory
        - Param Miner
        - XSS Arsenal
        - SQLi / Auth Lab
        - Prototype Pollution Lab
        - SSRF / RCE Forge
        - Nuclei Temple
        - Report Autopilot
        - Payload Kitchen
        - Ops Console

        Output format (MUST be strict JSON only):
        {{
          "calls": [
            {{"agent": "Recon Observatory", "task": "..."}},
            {{"agent": "Param Miner", "task": "..."}}
          ]
        }}

        Constraints:
        - calls: 0 to 4
        - task must be specific and bounded.
        - If the user input is a single domain, focus on scoping, recon strategy, and safe testing workflow.
        - Do NOT include chain-of-thought.

        ARTIFACT INTEL:
        {artifacts_text}

        USER MESSAGE:
        {user_text}
        """
    ).strip()

    try:
        plan_res = planner.generate_content(plan_prompt)
        plan_text = (plan_res.text or "").strip()
    except Exception as e:
        return f"[ERROR] Orchestrator planning failed: {e}", agent_states

    # Strict JSON parse (no freeform).
    calls: List[Dict[str, str]] = []
    try:
        plan_obj = json.loads(plan_text)
        calls = plan_obj.get("calls") or []
        if not isinstance(calls, list):
            calls = []
    except Exception:
        calls = []

    # Delegate.
    delegated: List[Tuple[str, str]] = []
    for c in calls[:4]:
        agent_title = (c or {}).get("agent")
        task = (c or {}).get("task")
        if not agent_title or not task:
            continue

        # Resolve title -> key
        target_key = None
        for k, s in specs.items():
            if s.title == agent_title and k != "void":
                target_key = k
                break
        if not target_key:
            continue

        st = agent_states.get(target_key) or AgentState(chat=[], memory_summary="")

        # We treat orchestrator-delegation as a user message into the specialist.
        st.chat.append((f"[VOID TASK] {task}", "..."))

        reply = _gemini_call(
            api_key=api_key,
            model_name=model_name,
            spec=specs[target_key],
            state=st,
            user_text=task,
            artifact_pack=artifact_pack,
        )

        st.chat[-1] = (st.chat[-1][0], reply)
        agent_states[target_key] = st
        delegated.append((agent_title, reply))

    # Synthesize final operator output.
    synthesis_parts = [
        "You are VOID. Synthesize the delegated outputs into an execution-ready plan.",
        "Rules: no chain-of-thought; be tactical; prioritize; highlight [CRITICAL].",
        "User message:",
        user_text,
        "\nArtifact intel:",
        artifacts_text,
    ]

    if delegated:
        synthesis_parts.append("\nSpecialist outputs:")
        for title, out in delegated:
            synthesis_parts.append(f"\n[{title}]\n{out}")

    # Use the normal call path for critique loop.
    void_state = agent_states.get("void") or AgentState(chat=[], memory_summary="")
    void_state.chat.append((user_text, "..."))

    final = _gemini_call(
        api_key=api_key,
        model_name=model_name,
        spec=void_spec,
        state=void_state,
        user_text="\n".join(synthesis_parts),
        artifact_pack=artifact_pack,
        temperature_override=0.3,
    )

    void_state.chat[-1] = (void_state.chat[-1][0], final)
    agent_states["void"] = void_state

    return final, agent_states


# ----------------------------
# Agent prompts (specialists)
# ----------------------------

COMMON_PROMPT_GUARDRAILS = textwrap.dedent(
    """
    You are operating inside an authorized bug bounty / red-team engagement.

    Non-negotiables:
    - Stay within provided scope.
    - Prefer validation and reproducibility.
    - Output must be operational (what to do next), but avoid instructions that enable real-world harm.

    Response style:
    - Use short sections: Situation, Signals, Hypotheses, Tests, Evidence, Next Moves.
    - Include commands/snippets only when they are safe and directly relevant.
    """
).strip()


def _specs() -> Dict[str, AgentSpec]:
    # "Void" is the orchestrator; specialists are single-focus operators.
    return {
        "void": AgentSpec(
            key="void",
            title="Void",
            temperature=0.3,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are VOID, the orchestrator.

                Core responsibilities:
                - Turn messy operator input (domains, screenshots, HAR, JS dumps) into a clean plan.
                - Identify the highest leverage attack surfaces quickly.
                - Delegate sub-tasks to specialists and fuse results.
                - Maintain a running picture of scope, auth boundaries, and evidence.

                Output:
                - a prioritized plan (P0/P1/P2)
                - a minimal evidence log template (what to capture)
                - a "what to ask next" list if missing data
                """
            ).strip(),
        ),
        "recon": AgentSpec(
            key="recon",
            title="Recon Observatory",
            temperature=0.3,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are the Recon Observatory.

                Mission:
                - Infer tech stack, hosting/CDN/WAF hints, auth patterns, subdomain topology.
                - Extract endpoints and behaviors from artifacts (HAR/JS/screenshots).
                - Produce an evidence-driven recon map and a safe enumeration plan.

                Deliverables:
                - Target map (domains → apps → auth boundaries)
                - Endpoint clusters (auth, graphql, files, admin, api)
                - High-signal anomalies to probe
                """
            ).strip(),
        ),
        "param": AgentSpec(
            key="param",
            title="Param Miner",
            temperature=0.3,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are Param Miner.

                Mission:
                - Extract parameters from URLs, HAR, JS, forms.
                - Cluster them by function (auth/session, routing, files, ids, feature flags).
                - Propose safe test matrices (type confusion, parsing differentials, caching).

                Output:
                - Param inventory with guessed types
                - Candidate "weird" params (debug, admin, next, redirect, returnUrl, callback)
                - Minimal test plan with expected evidence
                """
            ).strip(),
        ),
        "xss": AgentSpec(
            key="xss",
            title="XSS Arsenal",
            temperature=0.35,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are XSS Arsenal.

                Mission:
                - Identify reflection/sinks from artifacts and UI screenshots.
                - Propose safe payload *patterns* and context-specific checks without providing weaponized exploit chains.
                - Focus on modern frameworks, DOM XSS, template injection lookalikes.

                Output:
                - Candidate sinks and contexts (HTML/attr/JS/URL/CSS)
                - Evidence checklist (where to capture reflection)
                - Hardening / root-cause notes for report quality
                """
            ).strip(),
        ),
        "sqli_auth": AgentSpec(
            key="sqli_auth",
            title="SQLi / Auth Lab",
            temperature=0.3,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are SQLi / Auth Lab.

                Mission:
                - Identify authentication/session boundaries.
                - Detect SQLi indicators (error patterns, timing, response differentials) from HAR/logs.
                - Propose safe validation steps, logging strategy, and minimal PoC evidence.

                Output:
                - Auth diagram (entry points, tokens, cookies, refresh flows)
                - SQLi signal checklist (errors, timing, boolean)
                - Proof strategy: what to record and how to keep it safe
                """
            ).strip(),
        ),
        "proto": AgentSpec(
            key="proto",
            title="Prototype Pollution Lab",
            temperature=0.35,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are Prototype Pollution Lab.

                Mission:
                - Hunt for JS object merges, query/body parsing quirks, and gadget surfaces.
                - From JS bundles, identify libraries (lodash, qs, hoek, jquery, etc.) and risky patterns.
                - Propose safe verification steps and observable side-effects.

                Output:
                - Suspected merge points / parsing layers
                - Likely gadgets to look for (without exploit chains)
                - Evidence plan
                """
            ).strip(),
        ),
        "ssrf_rce": AgentSpec(
            key="ssrf_rce",
            title="SSRF / RCE Forge",
            temperature=0.3,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are SSRF / RCE Forge.

                Mission:
                - Identify URL fetch surfaces (webhooks, importers, image fetch, PDF generation, redirects).
                - Suggest safe, authorized verification methods (out-of-band logging endpoints, allowlist bypass checks).
                - Prioritize impact analysis and containment.

                Output:
                - Candidate fetch surfaces
                - Verification plan (safe, minimal)
                - Impact ladder (metadata exposure → internal services → credential pivot)
                """
            ).strip(),
        ),
        "nuclei": AgentSpec(
            key="nuclei",
            title="Nuclei Temple",
            temperature=0.3,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are Nuclei Temple.

                Mission:
                - Turn recon intel into a safe scanning strategy.
                - Generate detection ideas and reporting structure.

                Constraints:
                - Do not output aggressive mass-scanning instructions.
                - Focus on scoped, rate-limited, authorized checks.

                Output:
                - Template ideas (high level)
                - Safe scan plan (what/why)
                - Triage playbook
                """
            ).strip(),
        ),
        "report": AgentSpec(
            key="report",
            title="Report Autopilot",
            temperature=0.25,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are Report Autopilot.

                Mission:
                - Convert messy findings into a clean bounty report.
                - Enforce: repro steps, impact, scope proof, severity reasoning, remediation.

                Output:
                - Title + Summary
                - Steps to reproduce (minimal)
                - Impact and exploitation constraints
                - Suggested fix
                """
            ).strip(),
        ),
        "payload": AgentSpec(
            key="payload",
            title="Payload Kitchen",
            temperature=0.9,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are Payload Kitchen (creative operator).

                Mission:
                - Generate *test case variations* and *input mutation strategies*.
                - Avoid weaponization; focus on benign probes that reveal parsing/sanitization behavior.

                Output:
                - Mutation families by context (URL, header, JSON, form)
                - Expected signals
                - How to safely capture evidence
                """
            ).strip(),
        ),
        "memory": AgentSpec(
            key="memory",
            title="Memory Palace (chat + findings)",
            temperature=0.3,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are Memory Palace.

                Mission:
                - Index and recall session history across all agents.
                - Surface contradictions, missing evidence, and next steps.
                - Help the operator draft a clean narrative.

                Output:
                - "What we know" vs "What we assume" vs "What to verify"
                - A short checklist for next actions
                """
            ).strip(),
        ),
        "kitchen_sink": AgentSpec(
            key="kitchen_sink",
            title="Ops Console",
            temperature=0.35,
            system_prompt=textwrap.dedent(
                f"""
                {COMMON_PROMPT_GUARDRAILS}

                You are Ops Console.

                Mission:
                - Cross-check scope rules, rate limits, credentials hygiene.
                - Provide operational hygiene: notes templates, evidence capture, timelines.

                Output:
                - a compact operations checklist
                - risk controls for testing
                """
            ).strip(),
        ),
    }


# ----------------------------
# Gradio app
# ----------------------------

VOID_ASCII = r"""
██╗   ██╗ ██████╗ ██╗██████╗
██║   ██║██╔═══██╗██║██╔══██╗
██║   ██║██║   ██║██║██║  ██║
╚██╗ ██╔╝██║   ██║██║██║  ██║
 ╚████╔╝ ╚██████╔╝██║██████╔╝
  ╚═══╝   ╚═════╝ ╚═╝╚═════╝

        V O I D  //  O R A C L E
""".strip("\n")


def _init_states(specs: Dict[str, AgentSpec]) -> Dict[str, AgentState]:
    return {k: AgentState(chat=[], memory_summary="") for k in specs}


DEFAULT_MODEL_FALLBACK = "gemini-1.5-flash"


def _settings_file() -> Path:
    p = os.getenv("VOID_SETTINGS_FILE")
    if p:
        return Path(p).expanduser()
    return Path.home() / ".config" / "void-gemini-orchestrator" / "settings.json"


def _read_persisted_settings() -> Dict[str, Any]:
    path = _settings_file()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
    finally:
        try:
            os.chmod(str(tmp), 0o600)
        except Exception:
            pass

    tmp.replace(path)
    try:
        os.chmod(str(path), 0o600)
    except Exception:
        pass


def _persist_settings(settings: Dict[str, Any]) -> None:
    st = settings or {}
    api_key = (st.get("api_key") or "").strip()
    if not api_key:
        return

    payload = {
        "api_key": api_key,
        "model_name": st.get("model_name") or DEFAULT_MODEL_FALLBACK,
        "model_detected_at": st.get("model_detected_at"),
        "key_validated_at": st.get("key_validated_at"),
        "has_25_flash": bool(st.get("has_25_flash")),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    _atomic_write_json(_settings_file(), payload)


def _delete_persisted_settings() -> None:
    try:
        _settings_file().unlink(missing_ok=True)
    except Exception:
        pass


def _mask_api_key(api_key: str) -> str:
    api_key = (api_key or "").strip()
    if not api_key:
        return "(none)"
    if len(api_key) <= 8:
        return "•" * len(api_key)
    return f"{api_key[:4]}…{api_key[-4:]}"


def _validate_api_key_and_pick_model(api_key: str) -> Tuple[bool, str, str, bool]:
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "API key is empty.", DEFAULT_MODEL_FALLBACK, False

    genai.configure(api_key=api_key)

    try:
        available = {m.name.replace("models/", "") for m in genai.list_models()}
    except Exception as e:
        return False, f"Validation failed: {e}", DEFAULT_MODEL_FALLBACK, False

    preferred = [
        "gemini-2.5-flash-exp",
        "gemini-2.5-flash-preview",
        "gemini-2.5-flash",
        "gemini-2.0-flash-exp",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ]

    model_name = DEFAULT_MODEL_FALLBACK
    for name in preferred:
        if name in available:
            model_name = name
            break

    has_25_flash = any(n.startswith("gemini-2.5-flash") for n in available)
    return True, "OK", model_name, has_25_flash


def settings_load_initial() -> Dict[str, Any]:
    env_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    disk = _read_persisted_settings()

    st: Dict[str, Any] = {
        "api_key": "",
        "api_key_source": None,
        "model_name": DEFAULT_MODEL_FALLBACK,
        "model_detected_at": None,
        "key_validated_at": None,
        "key_last_validation_error": None,
        "has_25_flash": False,
    }

    # Prefer environment variables for deployments (they don't touch disk). If not present,
    # fall back to persisted local settings.
    if env_key:
        st.update({"api_key": env_key, "api_key_source": "env"})
        return st

    if isinstance(disk, dict) and (disk.get("api_key") or "").strip():
        st.update(
            {
                "api_key": (disk.get("api_key") or "").strip(),
                "api_key_source": "disk",
                "model_name": disk.get("model_name") or DEFAULT_MODEL_FALLBACK,
                "model_detected_at": disk.get("model_detected_at"),
                "key_validated_at": disk.get("key_validated_at"),
                "key_last_validation_error": None,
                "has_25_flash": bool(disk.get("has_25_flash")),
            }
        )

    return st


def settings_status(settings: Dict[str, Any]) -> str:
    st = settings or {}
    api_key = (st.get("api_key") or "").strip()

    if not api_key:
        err = (st.get("key_last_validation_error") or "").strip()
        err_line = f"\nLast error: {err}" if err else ""
        return textwrap.dedent(
            f"""
            [!] Gemini API key not set.{err_line}
            - Model: {DEFAULT_MODEL_FALLBACK} (fallback)
            - Tip: Settings → add key → Save & Validate
            """
        ).strip()

    source = st.get("api_key_source") or "session"
    model_name = st.get("model_name") or DEFAULT_MODEL_FALLBACK
    detected = st.get("model_detected_at") or "(not detected yet)"

    validated_at = st.get("key_validated_at")
    err = st.get("key_last_validation_error")
    if validated_at and not err:
        headline = "[OK] Key validated."
    elif validated_at and err:
        headline = f"[!] Validation error: {err}"
    else:
        headline = "[!] Key loaded (not yet validated)."

    validated = validated_at or "(not validated yet)"
    has_25 = "yes" if st.get("has_25_flash") else "no / unknown"

    return textwrap.dedent(
        f"""
        {headline}
        - Key: {_mask_api_key(api_key)}  (source: {source})
        - Preferred Flash model: {model_name}
        - Model detection: {detected}
        - Key validation: {validated}
        - Gemini 2.5 Flash available: {has_25}
        """
    ).strip()


def settings_save_and_validate(
    api_key: str, persist_to_disk: bool, current: Dict[str, Any]
) -> Tuple[Dict[str, Any], str]:
    api_key = (api_key or "").strip()
    st = dict(current or {})

    ok, msg, model_name, has_25_flash = _validate_api_key_and_pick_model(api_key)
    if not ok:
        st["key_last_validation_error"] = msg
        st["key_validated_at"] = datetime.utcnow().isoformat() + "Z"
        return st, settings_status(st)

    st.update(
        {
            "api_key": api_key,
            "api_key_source": "disk" if persist_to_disk else "session",
            "model_name": model_name,
            "model_detected_at": datetime.utcnow().isoformat() + "Z",
            "key_validated_at": datetime.utcnow().isoformat() + "Z",
            "key_last_validation_error": None,
            "has_25_flash": has_25_flash,
        }
    )

    if persist_to_disk:
        _persist_settings(st)

    return st, settings_status(st)


def settings_validate_current(current: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    st = dict(current or {})
    api_key = (st.get("api_key") or "").strip()
    ok, msg, model_name, has_25_flash = _validate_api_key_and_pick_model(api_key)

    st["key_validated_at"] = datetime.utcnow().isoformat() + "Z"
    st["has_25_flash"] = has_25_flash

    if not ok:
        st["key_last_validation_error"] = msg
        return st, settings_status(st)

    st["key_last_validation_error"] = None
    st["model_name"] = model_name
    st["model_detected_at"] = datetime.utcnow().isoformat() + "Z"
    return st, settings_status(st)


def settings_clear_key(current: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    _delete_persisted_settings()
    st = dict(current or {})
    st.update(
        {
            "api_key": "",
            "api_key_source": None,
            "model_name": DEFAULT_MODEL_FALLBACK,
            "model_detected_at": None,
            "key_validated_at": None,
            "key_last_validation_error": None,
            "has_25_flash": False,
        }
    )
    return st, settings_status(st)


def agent_send(
    agent_key: str,
    user_text: str,
    uploads: Any,
    folder_uploads: Any,
    settings: Dict[str, Any],
    agent_states: Dict[str, AgentState],
    findings: List[Dict[str, Any]],
) -> Tuple[ChatHistory, Dict[str, AgentState], List[Dict[str, Any]]]:
    specs = _specs()

    user_text = (user_text or "").strip()
    if not user_text:
        return agent_states.get(agent_key, AgentState([], "")).chat, agent_states, findings

    api_key = (settings or {}).get("api_key")
    model_name = (settings or {}).get("model_name") or "gemini-1.5-flash"

    st = agent_states.get(agent_key) or AgentState(chat=[], memory_summary="")
    st.chat.append((user_text, "..."))

    # Build artifact pack for this turn.
    paths = _coerce_paths(uploads) + _coerce_paths(folder_uploads)
    pack = build_artifact_pack(paths)

    if not api_key:
        reply = "[ERROR] Gemini API key missing. Open Settings tab and apply your key."
    else:
        reply = _gemini_call(
            api_key=api_key,
            model_name=model_name,
            spec=specs[agent_key],
            state=st,
            user_text=user_text,
            artifact_pack=pack,
        )

    st.chat[-1] = (st.chat[-1][0], reply)
    agent_states[agent_key] = st

    return st.chat, agent_states, findings


def void_send(
    user_text: str,
    uploads: Any,
    folder_uploads: Any,
    settings: Dict[str, Any],
    agent_states: Dict[str, AgentState],
    findings: List[Dict[str, Any]],
) -> Tuple[ChatHistory, Dict[str, AgentState], List[Dict[str, Any]]]:
    specs = _specs()

    user_text = (user_text or "").strip()
    if not user_text:
        return agent_states.get("void", AgentState([], "")).chat, agent_states, findings

    api_key = (settings or {}).get("api_key")
    model_name = (settings or {}).get("model_name") or "gemini-1.5-flash"

    paths = _coerce_paths(uploads) + _coerce_paths(folder_uploads)
    pack = build_artifact_pack(paths)

    if not api_key:
        st = agent_states.get("void") or AgentState(chat=[], memory_summary="")
        st.chat.append((user_text, "[ERROR] Gemini API key missing. Open Settings tab and apply your key."))
        agent_states["void"] = st
        return st.chat, agent_states, findings

    final, agent_states = orchestrate_void(
        api_key=api_key,
        model_name=model_name,
        specs=specs,
        agent_states=agent_states,
        user_text=user_text,
        artifact_pack=pack,
    )

    return agent_states["void"].chat, agent_states, findings


def save_last_reply(agent_key: str, agent_states: Dict[str, AgentState], findings: List[Dict[str, Any]]):
    st = agent_states.get(agent_key)
    if not st or not st.chat:
        return findings

    user, assistant = st.chat[-1]
    findings = list(findings or [])
    findings.append(
        {
            "ts": datetime.utcnow().isoformat() + "Z",
            "agent": agent_key,
            "user": user,
            "assistant": assistant,
        }
    )
    return findings


def memory_palace_view(agent_states: Dict[str, AgentState], findings: List[Dict[str, Any]]) -> str:
    lines = ["# Memory Palace", ""]

    # Findings first.
    lines.append("## Saved Findings")
    if not findings:
        lines.append("(none)")
    else:
        for f in findings[-50:]:
            lines.append(f"- **{f.get('ts')}** [{f.get('agent')}] {str(f.get('assistant') or '')[:240]}")

    lines.append("\n## Agent Chats (tail)")
    for k, st in (agent_states or {}).items():
        if not st.chat:
            continue
        lines.append(f"\n### {k}")
        for u, a in st.chat[-6:]:
            lines.append(f"- U: {u[:160]}")
            lines.append(f"  - A: {a[:220]}")

    return "\n".join(lines)


def memory_send(
    user_text: str,
    uploads: Any,
    folder_uploads: Any,
    settings: Dict[str, Any],
    agent_states: Dict[str, AgentState],
    findings: List[Dict[str, Any]],
) -> Tuple[ChatHistory, str, Dict[str, AgentState], List[Dict[str, Any]]]:
    """Memory Palace agent: sees the whole system state."""

    specs = _specs()

    user_text = (user_text or "").strip()
    if not user_text:
        st = agent_states.get("memory") or AgentState(chat=[], memory_summary="")
        return st.chat, memory_palace_view(agent_states, findings), agent_states, findings

    api_key = (settings or {}).get("api_key")
    model_name = (settings or {}).get("model_name") or "gemini-1.5-flash"

    paths = _coerce_paths(uploads) + _coerce_paths(folder_uploads)
    pack = build_artifact_pack(paths)

    st = agent_states.get("memory") or AgentState(chat=[], memory_summary="")
    st.chat.append((user_text, "..."))

    if not api_key:
        reply = "[ERROR] Gemini API key missing. Open Settings tab and apply your key."
    else:
        # Provide global state as additional context.
        global_view = memory_palace_view(agent_states, findings)
        reply = _gemini_call(
            api_key=api_key,
            model_name=model_name,
            spec=specs["memory"],
            state=st,
            user_text=f"{user_text}\n\n---\nGLOBAL MEMORY:\n{global_view}",
            artifact_pack=pack,
        )

    st.chat[-1] = (st.chat[-1][0], reply)
    agent_states["memory"] = st

    return st.chat, memory_palace_view(agent_states, findings), agent_states, findings


CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\n(.*?)```", re.DOTALL)


def extract_code_fences(text: str, max_blocks: int = 4) -> str:
    blocks = CODE_FENCE_RE.findall(text or "")
    if not blocks:
        return ""
    return "\n\n".join(b.strip() for b in blocks[:max_blocks]).strip()


def extract_first_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None

    fenced = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            obj = json.loads(fenced.group(1))
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = dec.raw_decode(text[i:])
            return obj if isinstance(obj, dict) else None
        except Exception:
            continue

    return None


def _list_text(items: Sequence[str], limit: int = 120) -> str:
    items = [i for i in (items or []) if i]
    items = list(items)[:limit]
    return "\n".join(items)


def _scope_canvas_markdown(scope_text: str, seeds_text: str) -> str:
    combined = "\n".join([(scope_text or "").strip(), (seeds_text or "").strip()]).strip()
    intel = _extract_intel(combined) if combined else {"urls": [], "domains": [], "ips": [], "emails": [], "params": []}

    lines = ["### Scope Canvas", ""]
    if scope_text:
        lines.append("**Declared scope (raw):**")
        lines.append("```\n" + (scope_text.strip()[:9000]) + "\n```")

    lines.append("**Detected in scope/seeds:**")
    lines.append(f"- Domains: {len(intel.get('domains') or [])}")
    lines.append(f"- URLs: {len(intel.get('urls') or [])}")
    lines.append(f"- IPs: {len(intel.get('ips') or [])}")
    lines.append(f"- Emails: {len(intel.get('emails') or [])}")
    lines.append(f"- Params: {len(intel.get('params') or [])}")

    return "\n".join(lines)


def build_ui() -> gr.Blocks:
    specs = _specs()
    initial_settings = settings_load_initial()

    with gr.Blocks(css=CSS, theme=gr.themes.Base(), title="VOID // Gemini Orchestrator") as demo:
        settings_state = gr.State(initial_settings)
        agent_states = gr.State(_init_states(specs))
        findings_state = gr.State([])  # list[dict]

        with gr.Tabs():
            # -----------------
            # VOID (main) — high-bandwidth conversation + artifact ingestion
            # -----------------
            with gr.TabItem("Void"):
                gr.HTML(f"<div class='void-hero'><pre>{VOID_ASCII}</pre></div>")

                void_chat = gr.Chatbot(label=None, height=520)

                void_input = gr.Textbox(
                    label=None,
                    placeholder="Drop your target, scope, screenshots, HAR files, JS files, or just rage-type your ideas…",
                    lines=6,
                )
                with gr.Row():
                    void_files = gr.File(label="Artifacts (files)", file_count="multiple")
                    void_folder = gr.File(label="Artifacts (folder)", file_count="directory")

                with gr.Row():
                    void_send_btn = gr.Button("Engage")
                    void_save_btn = gr.Button("Save last reply → Findings")

                void_save_btn.click(
                    fn=lambda a, f: save_last_reply("void", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

                gr.Markdown(
                    "<div class='void-disclaimer'>Authorized testing only. Stay within scope. Capture evidence. Report responsibly.</div>"
                )

            # -----------------
            # Recon Observatory — scope + source expansion + intel dashboard
            # -----------------
            with gr.TabItem("Recon Observatory"):
                gr.Markdown(
                    """
                    ### Recon Observatory
                    **Purpose:** intake scope + seeds, expand coverage, and keep a live dashboard of discovered URLs/domains/params.
                    """
                )

                with gr.Row():
                    with gr.Column(scale=7):
                        scope_tb = gr.Textbox(
                            label="In-scope targets / constraints",
                            placeholder="E.g. *.example.com, api.example.com, 192.0.2.0/24, out-of-scope: admin.example.com",
                            lines=4,
                        )
                        seeds_tb = gr.Textbox(
                            label="Seeds (starting URLs/domains)",
                            placeholder="Paste known endpoints, domains, app store links, repo URLs…",
                            lines=4,
                        )

                        with gr.Accordion("Input sources (evidence)", open=True):
                            with gr.Row():
                                recon_files = gr.File(label="Files (HAR/JS/screenshots)", file_count="multiple")
                                recon_folder = gr.File(label="Folder drop", file_count="directory")

                        recon_directive = gr.Textbox(
                            label="Recon directive",
                            placeholder="What should we map next? (tech fingerprinting, endpoint mining, auth surfaces, subdomain strategy…)",
                            lines=3,
                        )

                        with gr.Row():
                            recon_extract_btn = gr.Button("Extract intel (local)")
                            recon_send_btn = gr.Button("Ask Recon (Gemini)")
                            recon_save_btn = gr.Button("Save last reply → Findings")

                        recon_chat = gr.Chatbot(label="Recon log", height=260)

                    with gr.Column(scale=5):
                        recon_scope_view = gr.Markdown(value="### Scope Canvas\n(press **Extract intel**)")
                        recon_intel_summary = gr.Markdown(value="### Intake Summary\n(press **Extract intel**)")

                        with gr.Accordion("Domains", open=False):
                            recon_domains = gr.Code(language="text")
                        with gr.Accordion("URLs", open=False):
                            recon_urls = gr.Code(language="text")
                        with gr.Accordion("Params", open=False):
                            recon_params = gr.Code(language="text")
                        with gr.Accordion("IPs / Emails", open=False):
                            with gr.Row():
                                recon_ips = gr.Code(language="text")
                                recon_emails = gr.Code(language="text")
                        with gr.Accordion("Tech hints", open=False):
                            recon_tech = gr.Code(language="text")

                def _recon_extract(scope_text, seeds_text, uploads, folder_uploads):
                    combined = "\n".join([(scope_text or "").strip(), (seeds_text or "").strip()]).strip()
                    declared = _extract_intel(combined) if combined else {"urls": [], "domains": [], "ips": [], "emails": [], "params": []}

                    paths = _coerce_paths(uploads) + _coerce_paths(folder_uploads)
                    pack = build_artifact_pack(paths)
                    intel = dict(pack.get("intel") or {})

                    merged: Dict[str, List[str]] = {}
                    for k in ["urls", "domains", "ips", "emails", "params"]:
                        merged[k] = sorted(set(intel.get(k, [])) | set(declared.get(k, [])))
                    merged["tech"] = sorted(set(intel.get("tech", [])))

                    scope_md = _scope_canvas_markdown(scope_text, seeds_text)
                    summary_md = "### Intake Summary\n\n```\n" + (pack.get("summary") or "") + "\n```"

                    return (
                        scope_md,
                        summary_md,
                        _list_text(merged.get("domains", []), 220),
                        _list_text(merged.get("urls", []), 220),
                        _list_text(merged.get("params", []), 220),
                        _list_text(merged.get("ips", []), 120),
                        _list_text(merged.get("emails", []), 120),
                        _list_text(merged.get("tech", []), 120),
                    )

                recon_extract_btn.click(
                    fn=_recon_extract,
                    inputs=[scope_tb, seeds_tb, recon_files, recon_folder],
                    outputs=[
                        recon_scope_view,
                        recon_intel_summary,
                        recon_domains,
                        recon_urls,
                        recon_params,
                        recon_ips,
                        recon_emails,
                        recon_tech,
                    ],
                )

                def _recon_send(directive, scope_text, seeds_text, up, fol, settings, states, findings):
                    prompt = textwrap.dedent(
                        f"""
                        RECON MODE

                        Scope (respect strictly):
                        {scope_text}

                        Seeds:
                        {seeds_text}

                        Directive:
                        {directive}

                        Output format:
                        - 10 next recon actions (ordered)
                        - Coverage gaps
                        - Data to request/collect next
                        """
                    ).strip()
                    return agent_send("recon", prompt, up, fol, settings, states, findings)

                recon_send_btn.click(
                    fn=_recon_send,
                    inputs=[recon_directive, scope_tb, seeds_tb, recon_files, recon_folder, settings_state, agent_states, findings_state],
                    outputs=[recon_chat, agent_states, findings_state],
                )
                recon_directive.submit(
                    fn=_recon_send,
                    inputs=[recon_directive, scope_tb, seeds_tb, recon_files, recon_folder, settings_state, agent_states, findings_state],
                    outputs=[recon_chat, agent_states, findings_state],
                )
                recon_save_btn.click(
                    fn=lambda a, f: save_last_reply("recon", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # Param Miner — structured extraction + wordlist staging
            # -----------------
            with gr.TabItem("Param Miner"):
                gr.Markdown(
                    """
                    ### Param Miner
                    **Purpose:** turn traces (HAR/URLs/JS) into a prioritized parameter + endpoint candidate set.
                    """
                )

                with gr.Row():
                    with gr.Column(scale=6):
                        param_target = gr.Textbox(
                            label="Target surface (optional)",
                            placeholder="E.g. https://api.example.com or /graphql or /search",
                            lines=2,
                        )
                        param_notes = gr.Textbox(
                            label="Notes / constraints",
                            placeholder="Auth required? Rate limits? Known parameter patterns?",
                            lines=3,
                        )
                        with gr.Row():
                            param_files = gr.File(label="Artifacts", file_count="multiple")
                            param_folder = gr.File(label="Folder", file_count="directory")

                        with gr.Row():
                            param_extract_btn = gr.Button("Extract params (local)")
                            param_send_btn = gr.Button("Ask Param Miner (Gemini)")
                            param_save_btn = gr.Button("Save last reply → Findings")

                        param_question = gr.Textbox(
                            label="Question",
                            placeholder="What params look vulnerable? Which endpoints deserve fuzzing?",
                            lines=2,
                        )

                        param_chat = gr.Chatbot(label="Param Miner reasoning", height=260)

                    with gr.Column(scale=6):
                        param_summary = gr.Markdown(value="### Extraction\n(press **Extract params**)")
                        param_candidates = gr.Code(label="Parameter candidates", language="text")
                        endpoint_candidates = gr.Code(label="Endpoint/URL candidates", language="text")
                        wordlist_out = gr.Code(label="Wordlist (newline)", language="text")

                def _param_extract(up, fol, target, notes):
                    paths = _coerce_paths(up) + _coerce_paths(fol)
                    pack = build_artifact_pack(paths)
                    intel = pack.get("intel") or {}

                    params = list(intel.get("params") or [])
                    urls = list(intel.get("urls") or [])

                    summary = ["### Extraction", "", "```", pack.get("summary") or "", "```"]
                    if target:
                        summary.append(f"\n**Target surface:** {target}")
                    if notes:
                        summary.append(f"\n**Notes:** {notes}")

                    return (
                        "\n".join(summary),
                        _list_text(params, 260),
                        _list_text(urls, 180),
                        _list_text(params, 800),
                    )

                param_extract_btn.click(
                    fn=_param_extract,
                    inputs=[param_files, param_folder, param_target, param_notes],
                    outputs=[param_summary, param_candidates, endpoint_candidates, wordlist_out],
                )

                def _param_send(question, target, notes, up, fol, settings, states, findings):
                    prompt = textwrap.dedent(
                        f"""
                        PARAM MINING MODE

                        Target surface:
                        {target}

                        Constraints:
                        {notes}

                        Question:
                        {question}

                        Output format:
                        - High-value params (prioritized)
                        - Suspected sinks (if any)
                        - Suggested fuzzing strategy
                        """
                    ).strip()
                    return agent_send("param", prompt, up, fol, settings, states, findings)

                param_send_btn.click(
                    fn=_param_send,
                    inputs=[param_question, param_target, param_notes, param_files, param_folder, settings_state, agent_states, findings_state],
                    outputs=[param_chat, agent_states, findings_state],
                )
                param_question.submit(
                    fn=_param_send,
                    inputs=[param_question, param_target, param_notes, param_files, param_folder, settings_state, agent_states, findings_state],
                    outputs=[param_chat, agent_states, findings_state],
                )
                param_save_btn.click(
                    fn=lambda a, f: save_last_reply("param", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # XSS Arsenal — configuration-led prompt + snippet staging
            # -----------------
            with gr.TabItem("XSS Arsenal"):
                gr.Markdown(
                    """
                    ### XSS Arsenal
                    **Purpose:** model the injection context and constraints first; then generate a safe, structured test plan.
                    """
                )

                with gr.Row():
                    with gr.Column(scale=4):
                        xss_context = gr.Dropdown(
                            label="Injection context",
                            choices=[
                                "HTML body",
                                "HTML attribute",
                                "JavaScript string",
                                "JavaScript template literal",
                                "URL parameter",
                                "JSON value",
                                "SVG context",
                            ],
                            value="URL parameter",
                        )
                        xss_sink = gr.Dropdown(
                            label="Suspected sink",
                            choices=[
                                "unknown",
                                "innerHTML",
                                "outerHTML",
                                "document.write",
                                "setAttribute",
                                "eval / Function",
                                "location / href",
                            ],
                            value="unknown",
                        )
                        xss_filters = gr.CheckboxGroup(
                            label="Constraints / filters",
                            choices=["WAF present", "CSP present", "Input is URL-encoded", "HTML entity encoding", "JSON escaping"],
                        )
                        xss_goal = gr.Textbox(
                            label="Goal",
                            placeholder="What do you want to confirm? Reflection? execution? bypass?",
                            lines=2,
                        )

                        with gr.Row():
                            xss_files = gr.File(label="Artifacts", file_count="multiple")
                            xss_folder = gr.File(label="Folder", file_count="directory")

                        with gr.Row():
                            xss_send_btn = gr.Button("Build test plan")
                            xss_save_btn = gr.Button("Save last reply → Findings")

                    with gr.Column(scale=4):
                        xss_sample = gr.Code(
                            label="Request/response snippet (optional)",
                            language="http",
                            value="",
                        )
                        xss_staged = gr.Code(
                            label="Staged snippets (from response code fences)",
                            language="text",
                            value="",
                        )

                    with gr.Column(scale=6):
                        xss_chat = gr.Chatbot(label="XSS reasoning", height=520)

                def _xss_send(goal, ctx, sink, filters, sample, up, fol, settings, states, findings):
                    prompt = textwrap.dedent(
                        f"""
                        XSS MODE

                        Context: {ctx}
                        Sink: {sink}
                        Constraints: {', '.join(filters or []) or '(none)'}

                        Goal:
                        {goal}

                        Evidence snippet:
                        ```
                        {sample}
                        ```

                        Output format:
                        - Assumptions
                        - Test plan (steps)
                        - Payload *patterns* (avoid reckless / out-of-scope)
                        - Expected signals
                        """
                    ).strip()

                    chat, states, findings = agent_send("xss", prompt, up, fol, settings, states, findings)
                    last = chat[-1][1] if chat else ""
                    staged = extract_code_fences(last)
                    return chat, staged, states, findings

                xss_send_btn.click(
                    fn=_xss_send,
                    inputs=[xss_goal, xss_context, xss_sink, xss_filters, xss_sample, xss_files, xss_folder, settings_state, agent_states, findings_state],
                    outputs=[xss_chat, xss_staged, agent_states, findings_state],
                )
                xss_save_btn.click(
                    fn=lambda a, f: save_last_reply("xss", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # SQLi / Auth Lab — decision-board + confidence framing
            # -----------------
            with gr.TabItem("SQLi / Auth Lab"):
                gr.Markdown(
                    """
                    ### SQLi / Auth Lab
                    **Purpose:** evaluate exploitability with a checklist mindset (assumptions → tests → confidence).
                    """
                )

                with gr.Row():
                    with gr.Column(scale=5):
                        sqli_mode = gr.Radio(label="Focus", choices=["SQLi", "Auth"], value="SQLi")
                        sqli_db = gr.Dropdown(label="DB hint", choices=["unknown", "mysql", "postgres", "mssql", "sqlite"], value="unknown")
                        sqli_req = gr.Code(label="Sample request", language="http", value="")
                        sqli_question = gr.Textbox(label="Question", placeholder="Where should we probe first? What signals matter?", lines=2)
                        with gr.Row():
                            sqli_files = gr.File(label="Artifacts", file_count="multiple")
                            sqli_folder = gr.File(label="Folder", file_count="directory")
                        with gr.Row():
                            sqli_send_btn = gr.Button("Evaluate")
                            sqli_save_btn = gr.Button("Save last reply → Findings")

                    with gr.Column(scale=7):
                        sqli_board = gr.Markdown(value="### Decision board\n(press **Evaluate**)\n")
                        sqli_conf = gr.Label(value="confidence: unknown")
                        sqli_chat = gr.Chatbot(label="Lab notes", height=360)

                CONF_RE = re.compile(r"confidence\s*[:\-]\s*(\d{1,3})%", re.IGNORECASE)

                def _sqli_send(mode, db, req, question, up, fol, settings, states, findings):
                    prompt = textwrap.dedent(
                        f"""
                        {mode.upper()} LAB MODE
                        DB hint: {db}

                        Request:
                        ```http
                        {req}
                        ```

                        Question:
                        {question}

                        Output format:
                        - Hypothesis
                        - Test checklist
                        - Safety constraints
                        - Confidence: <0-100>%
                        """
                    ).strip()

                    chat, states, findings = agent_send("sqli_auth", prompt, up, fol, settings, states, findings)
                    last = chat[-1][1] if chat else ""

                    m = CONF_RE.search(last)
                    conf = f"confidence: {m.group(1)}%" if m else "confidence: unknown"

                    board = "### Decision board\n\n" + "\n".join((last or "").splitlines()[:26])
                    return chat, board, conf, states, findings

                sqli_send_btn.click(
                    fn=_sqli_send,
                    inputs=[sqli_mode, sqli_db, sqli_req, sqli_question, sqli_files, sqli_folder, settings_state, agent_states, findings_state],
                    outputs=[sqli_chat, sqli_board, sqli_conf, agent_states, findings_state],
                )
                sqli_save_btn.click(
                    fn=lambda a, f: save_last_reply("sqli_auth", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # Prototype Pollution Lab — vector modelling + PoC snippet staging
            # -----------------
            with gr.TabItem("Prototype Pollution Lab"):
                gr.Markdown(
                    """
                    ### Prototype Pollution Lab
                    **Purpose:** describe the vector (param/path/sink) precisely, then iterate PoCs with evidence.
                    """
                )

                with gr.Row():
                    with gr.Column(scale=5):
                        proto_param = gr.Textbox(label="Input vector (parameter/key)", placeholder="E.g. __proto__, constructor, deep[__proto__][x]", lines=2)
                        proto_path = gr.Textbox(label="Property path to overwrite", placeholder="E.g. toString, isAdmin, auth.role", lines=2)
                        proto_sink = gr.Dropdown(label="Sink hint", choices=["unknown", "merge/deep-extend", "lodash", "qs", "custom"], value="unknown")
                        proto_req = gr.Code(label="Sample request", language="http", value="")
                        with gr.Row():
                            proto_files = gr.File(label="Artifacts", file_count="multiple")
                            proto_folder = gr.File(label="Folder", file_count="directory")
                        with gr.Row():
                            proto_send_btn = gr.Button("Draft PoCs")
                            proto_save_btn = gr.Button("Save last reply → Findings")

                    with gr.Column(scale=7):
                        proto_staged = gr.Code(label="Staged PoC snippets", language="text", value="")
                        proto_chat = gr.Chatbot(label="Lab notes", height=420)

                def _proto_send(param, path, sink, req, up, fol, settings, states, findings):
                    prompt = textwrap.dedent(
                        f"""
                        PROTOTYPE POLLUTION MODE
                        Vector: {param}
                        Overwrite target: {path}
                        Sink hint: {sink}

                        Request:
                        ```http
                        {req}
                        ```

                        Output format:
                        - Preconditions
                        - 3 PoC variants
                        - Verification steps
                        """
                    ).strip()

                    chat, states, findings = agent_send("proto", prompt, up, fol, settings, states, findings)
                    last = chat[-1][1] if chat else ""
                    return chat, extract_code_fences(last), states, findings

                proto_send_btn.click(
                    fn=_proto_send,
                    inputs=[proto_param, proto_path, proto_sink, proto_req, proto_files, proto_folder, settings_state, agent_states, findings_state],
                    outputs=[proto_chat, proto_staged, agent_states, findings_state],
                )
                proto_save_btn.click(
                    fn=lambda a, f: save_last_reply("proto", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # SSRF / RCE Forge — chain planning + comparative mitigations
            # -----------------
            with gr.TabItem("SSRF / RCE Forge"):
                gr.Markdown(
                    """
                    ### SSRF / RCE Forge
                    **Purpose:** map reachable internal surfaces and build a controlled chain (what to try vs what to rule out).
                    """
                )

                with gr.Row():
                    with gr.Column(scale=5):
                        ssrf_target = gr.Textbox(label="Internal target hypothesis", placeholder="E.g. metadata service, redis, internal admin panel", lines=2)
                        ssrf_egress = gr.CheckboxGroup(
                            label="Assumed egress/protocols",
                            choices=["http", "https", "gopher", "ftp", "file", "dns"],
                        )
                        ssrf_req = gr.Code(label="Sample request", language="http", value="")
                        with gr.Row():
                            ssrf_files = gr.File(label="Artifacts", file_count="multiple")
                            ssrf_folder = gr.File(label="Folder", file_count="directory")
                        with gr.Row():
                            ssrf_send_btn = gr.Button("Plan chain")
                            ssrf_save_btn = gr.Button("Save last reply → Findings")

                    with gr.Column(scale=7):
                        ssrf_plan = gr.Markdown(value="### Chain plan\n(press **Plan chain**)\n")
                        ssrf_risk = gr.Label(value="risk: unknown")
                        ssrf_chat = gr.Chatbot(label="Forge notes", height=360)

                RISK_RE = re.compile(r"risk\s*[:\-]\s*(low|medium|high|critical)", re.IGNORECASE)

                def _ssrf_send(target, egress, req, up, fol, settings, states, findings):
                    prompt = textwrap.dedent(
                        f"""
                        SSRF/RCE MODE

                        Internal target hypothesis:
                        {target}

                        Assumed allowed protocols:
                        {', '.join(egress or []) or 'unknown'}

                        Request:
                        ```http
                        {req}
                        ```

                        Output format:
                        - Reachability assumptions
                        - Chain steps (try / observe / stop conditions)
                        - Mitigations comparison table
                        - Risk: low|medium|high|critical
                        """
                    ).strip()

                    chat, states, findings = agent_send("ssrf_rce", prompt, up, fol, settings, states, findings)
                    last = chat[-1][1] if chat else ""
                    m = RISK_RE.search(last or "")
                    risk = f"risk: {m.group(1).lower()}" if m else "risk: unknown"
                    plan = "### Chain plan\n\n" + (last or "")
                    return chat, plan, risk, states, findings

                ssrf_send_btn.click(
                    fn=_ssrf_send,
                    inputs=[ssrf_target, ssrf_egress, ssrf_req, ssrf_files, ssrf_folder, settings_state, agent_states, findings_state],
                    outputs=[ssrf_chat, ssrf_plan, ssrf_risk, agent_states, findings_state],
                )
                ssrf_save_btn.click(
                    fn=lambda a, f: save_last_reply("ssrf_rce", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # Nuclei Temple — scan plan builder
            # -----------------
            with gr.TabItem("Nuclei Temple"):
                gr.Markdown(
                    """
                    ### Nuclei Temple
                    **Purpose:** turn scope into a controlled scan plan (templates, severity, rate limits, evidence capture).
                    """
                )

                with gr.Row():
                    with gr.Column(scale=5):
                        nuclei_scope = gr.Textbox(label="Scope list", placeholder="One domain/URL per line", lines=6)
                        nuclei_focus = gr.CheckboxGroup(
                            label="Template focus",
                            choices=["cves", "misconfiguration", "exposures", "takeovers", "technologies", "fuzzing"],
                        )
                        nuclei_constraints = gr.Textbox(label="Constraints", placeholder="Rate limit, auth requirements, exclusions…", lines=2)
                        with gr.Row():
                            nuclei_files = gr.File(label="Artifacts", file_count="multiple")
                            nuclei_folder = gr.File(label="Folder", file_count="directory")
                        with gr.Row():
                            nuclei_send_btn = gr.Button("Build scan plan")
                            nuclei_save_btn = gr.Button("Save last reply → Findings")

                    with gr.Column(scale=7):
                        nuclei_cmd = gr.Code(label="Command draft (from code fences)", language="bash", value="")
                        nuclei_plan = gr.Markdown(value="### Plan\n(press **Build scan plan**)\n")
                        nuclei_chat = gr.Chatbot(label="Temple notes", height=280)

                def _nuclei_send(scope, focus, constraints, up, fol, settings, states, findings):
                    prompt = textwrap.dedent(
                        f"""
                        NUCLEI MODE

                        Scope:
                        {scope}

                        Template focus:
                        {', '.join(focus or []) or 'unspecified'}

                        Constraints:
                        {constraints}

                        Output format:
                        - A safe scan plan
                        - A `nuclei` command in a code block
                        """
                    ).strip()

                    chat, states, findings = agent_send("nuclei", prompt, up, fol, settings, states, findings)
                    last = chat[-1][1] if chat else ""
                    cmd = extract_code_fences(last)
                    return chat, cmd, "### Plan\n\n" + (last or ""), states, findings

                nuclei_send_btn.click(
                    fn=_nuclei_send,
                    inputs=[nuclei_scope, nuclei_focus, nuclei_constraints, nuclei_files, nuclei_folder, settings_state, agent_states, findings_state],
                    outputs=[nuclei_chat, nuclei_cmd, nuclei_plan, agent_states, findings_state],
                )
                nuclei_save_btn.click(
                    fn=lambda a, f: save_last_reply("nuclei", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # Report Autopilot — form-first structured output + confidence
            # -----------------
            with gr.TabItem("Report Autopilot"):
                gr.Markdown(
                    """
                    ### Report Autopilot
                    **Purpose:** convert evidence into a structured, reviewable report draft (with explicit confidence).
                    """
                )

                with gr.Row():
                    with gr.Column(scale=5):
                        rpt_title = gr.Textbox(label="Vulnerability title", placeholder="Short, specific", lines=2)
                        rpt_asset = gr.Textbox(label="Affected asset / URL", placeholder="https://app.example.com/path", lines=2)
                        rpt_sev = gr.Dropdown(label="Severity", choices=["info", "low", "medium", "high", "critical"], value="medium")
                        rpt_impact = gr.Textbox(label="Impact", placeholder="What can an attacker do (within scope)?", lines=3)
                        rpt_steps = gr.Textbox(label="Steps to reproduce", placeholder="Step-by-step, deterministic", lines=5)
                        rpt_notes = gr.Textbox(label="Extra notes / constraints", lines=2)
                        with gr.Row():
                            rpt_files = gr.File(label="Evidence files", file_count="multiple")
                            rpt_folder = gr.File(label="Evidence folder", file_count="directory")

                        with gr.Row():
                            rpt_send_btn = gr.Button("Draft report")
                            rpt_save_btn = gr.Button("Save last reply → Findings")

                        report_chat = gr.Chatbot(label="Report conversation", height=260)

                    with gr.Column(scale=7):
                        rpt_conf = gr.Label(value="confidence: unknown")
                        rpt_json = gr.JSON(label="Structured fields", value={})
                        rpt_md = gr.Markdown(value="### Draft\n(press **Draft report**)\n")

                def _report_send(title, asset, sev, impact, steps, notes, up, fol, settings, states, findings):
                    prompt = textwrap.dedent(
                        f"""
                        REPORT MODE

                        Title: {title}
                        Asset: {asset}
                        Severity: {sev}
                        Impact: {impact}

                        Steps to reproduce:
                        {steps}

                        Notes:
                        {notes}

                        Return:
                        1) A JSON object in a fenced ```json block with keys:
                           title, asset, severity, summary, impact, steps_to_reproduce, remediation, references, confidence
                        2) Then a clean Markdown report body.
                        """
                    ).strip()

                    chat, states, findings = agent_send("report", prompt, up, fol, settings, states, findings)
                    last = chat[-1][1] if chat else ""
                    obj = extract_first_json(last) or {}
                    conf = obj.get("confidence")
                    conf_label = f"confidence: {conf}" if conf is not None else "confidence: unknown"
                    return chat, conf_label, obj, "### Draft\n\n" + (last or ""), states, findings

                rpt_send_btn.click(
                    fn=_report_send,
                    inputs=[rpt_title, rpt_asset, rpt_sev, rpt_impact, rpt_steps, rpt_notes, rpt_files, rpt_folder, settings_state, agent_states, findings_state],
                    outputs=[report_chat, rpt_conf, rpt_json, rpt_md, agent_states, findings_state],
                )
                rpt_save_btn.click(
                    fn=lambda a, f: save_last_reply("report", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # Payload Kitchen — intent + constraints → staged snippets
            # -----------------
            with gr.TabItem("Payload Kitchen"):
                gr.Markdown(
                    """
                    ### Payload Kitchen
                    **Purpose:** produce payload *families* for a goal, with constraints and verification signals.
                    """
                )

                with gr.Row():
                    with gr.Column(scale=5):
                        p_goal = gr.Dropdown(
                            label="Goal",
                            choices=["xss", "sqli", "ssrf", "proto", "auth", "open redirect", "misc"],
                            value="xss",
                        )
                        p_constraints = gr.Textbox(label="Constraints", placeholder="Encoding/WAF/CSP/length limits/scope rules…", lines=3)
                        p_context = gr.Code(label="Context snippet", language="text", value="")
                        with gr.Row():
                            p_files = gr.File(label="Artifacts", file_count="multiple")
                            p_folder = gr.File(label="Folder", file_count="directory")
                        with gr.Row():
                            p_send_btn = gr.Button("Generate")
                            p_save_btn = gr.Button("Save last reply → Findings")

                    with gr.Column(scale=7):
                        payload_staged = gr.Code(label="Staged snippets", language="text", value="")
                        payload_chat = gr.Chatbot(label="Kitchen notes", height=420)

                def _payload_send(goal, constraints, ctx, up, fol, settings, states, findings):
                    prompt = textwrap.dedent(
                        f"""
                        PAYLOAD MODE

                        Goal: {goal}
                        Constraints: {constraints}

                        Context:
                        ```
                        {ctx}
                        ```

                        Output format:
                        - Payload patterns in code blocks
                        - Verification signals
                        - Stop conditions / scope reminders
                        """
                    ).strip()

                    chat, states, findings = agent_send("payload", prompt, up, fol, settings, states, findings)
                    last = chat[-1][1] if chat else ""
                    return chat, extract_code_fences(last), states, findings

                p_send_btn.click(
                    fn=_payload_send,
                    inputs=[p_goal, p_constraints, p_context, p_files, p_folder, settings_state, agent_states, findings_state],
                    outputs=[payload_chat, payload_staged, agent_states, findings_state],
                )
                p_save_btn.click(
                    fn=lambda a, f: save_last_reply("payload", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # Memory Palace — system-wide recall + saved findings
            # -----------------
            with gr.TabItem("Memory Palace"):
                mem_chat = gr.Chatbot(label=None, height=360)
                mem_view = gr.Markdown(value=memory_palace_view(_init_states(specs), []))

                mem_input = gr.Textbox(label=None, placeholder="Ask Memory Palace to recall, cross-check, or synthesize…", lines=3)
                with gr.Row():
                    mem_files = gr.File(label="Files", file_count="multiple")
                    mem_folder = gr.File(label="Folder", file_count="directory")

                with gr.Row():
                    mem_send_btn = gr.Button("Recall")
                    mem_save_btn = gr.Button("Save last reply → Findings")

                mem_send_btn.click(
                    fn=memory_send,
                    inputs=[mem_input, mem_files, mem_folder, settings_state, agent_states, findings_state],
                    outputs=[mem_chat, mem_view, agent_states, findings_state],
                )
                mem_input.submit(
                    fn=memory_send,
                    inputs=[mem_input, mem_files, mem_folder, settings_state, agent_states, findings_state],
                    outputs=[mem_chat, mem_view, agent_states, findings_state],
                )
                mem_save_btn.click(
                    fn=lambda a, f: save_last_reply("memory", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

                refresh_btn = gr.Button("Refresh view")
                refresh_btn.click(
                    fn=memory_palace_view,
                    inputs=[agent_states, findings_state],
                    outputs=[mem_view],
                )

            # -----------------
            # Ops Console — raw / ad-hoc with minimal ceremony
            # -----------------
            with gr.TabItem("Ops Console"):
                gr.Markdown(
                    """
                    ### Ops Console
                    **Purpose:** high-density, ad-hoc tasks. Bring your own structure.
                    """
                )

                with gr.Row():
                    with gr.Column(scale=5):
                        ops_prompt = gr.Textbox(label="Instruction", placeholder="Give a precise task; include desired output format.", lines=6)
                        with gr.Row():
                            ops_files = gr.File(label="Artifacts", file_count="multiple")
                            ops_folder = gr.File(label="Folder", file_count="directory")
                        with gr.Row():
                            ops_send_btn = gr.Button("Run")
                            ops_save_btn = gr.Button("Save last reply → Findings")

                    with gr.Column(scale=7):
                        ops_chat = gr.Chatbot(label="Ops output", height=520)

                ops_send_btn.click(
                    fn=lambda t, up, fol, s, st, f: agent_send("kitchen_sink", t, up, fol, s, st, f),
                    inputs=[ops_prompt, ops_files, ops_folder, settings_state, agent_states, findings_state],
                    outputs=[ops_chat, agent_states, findings_state],
                )
                ops_prompt.submit(
                    fn=lambda t, up, fol, s, st, f: agent_send("kitchen_sink", t, up, fol, s, st, f),
                    inputs=[ops_prompt, ops_files, ops_folder, settings_state, agent_states, findings_state],
                    outputs=[ops_chat, agent_states, findings_state],
                )
                ops_save_btn.click(
                    fn=lambda a, f: save_last_reply("kitchen_sink", a, f),
                    inputs=[agent_states, findings_state],
                    outputs=[findings_state],
                )

            # -----------------
            # Settings — API key vault (save/update/validate/persist)
            # -----------------
            with gr.TabItem("Settings"):
                gr.Markdown(
                    """
                    ### Settings: Gemini 2.5 Flash API Key

                    This app runs on a server. If you choose **Persist to disk**, the key is stored on the **server's filesystem**
                    (mode `0600`) and reused across sessions.
                    """
                )

                settings_status_md = gr.Markdown(value=settings_status(initial_settings))

                api_key_in = gr.Textbox(
                    label="Gemini API key (masked)",
                    placeholder="Paste key here (will be cleared after save)",
                    type="password",
                )
                persist_ck = gr.Checkbox(label="Persist on server (recommended)", value=True)

                with gr.Row():
                    save_validate_btn = gr.Button("Save & Validate")
                    validate_btn = gr.Button("Validate current")
                    clear_btn = gr.Button("Clear stored key")

                def _save_validate_ui(key, persist, st):
                    new_st, status = settings_save_and_validate(key, persist, st)
                    return new_st, status, ""

                def _validate_ui(st):
                    new_st, status = settings_validate_current(st)
                    return new_st, status

                def _clear_ui(st):
                    new_st, status = settings_clear_key(st)
                    return new_st, status, ""

                save_validate_btn.click(
                    fn=_save_validate_ui,
                    inputs=[api_key_in, persist_ck, settings_state],
                    outputs=[settings_state, settings_status_md, api_key_in],
                )
                validate_btn.click(
                    fn=_validate_ui,
                    inputs=[settings_state],
                    outputs=[settings_state, settings_status_md],
                )
                clear_btn.click(
                    fn=_clear_ui,
                    inputs=[settings_state],
                    outputs=[settings_state, settings_status_md, api_key_in],
                )

                gr.Markdown(
                    """
                    **Notes**
                    - Key validation uses `list_models()` to confirm the key works and to detect the best available **Flash** model.
                    - If `GEMINI_API_KEY` / `GOOGLE_API_KEY` is set in the environment, it is used by default (no disk writes).
                    """
                )

        # Void can delegate into specialist agents. To make that visible without relying on
        # any State change events (which vary across Gradio versions), we wire Void's send
        # events to *also* return the latest chat histories for every specialist tab.
        def _get_chat(states: Dict[str, AgentState], key: str) -> ChatHistory:
            st = (states or {}).get(key)
            return st.chat if st else []

        def void_send_sync(user_text, uploads, folder_uploads, settings, states, findings):
            _void_chat, states, findings = void_send(
                user_text,
                uploads,
                folder_uploads,
                settings,
                states,
                findings,
            )
            return (
                _get_chat(states, "void"),
                _get_chat(states, "recon"),
                _get_chat(states, "param"),
                _get_chat(states, "xss"),
                _get_chat(states, "sqli_auth"),
                _get_chat(states, "proto"),
                _get_chat(states, "ssrf_rce"),
                _get_chat(states, "nuclei"),
                _get_chat(states, "report"),
                _get_chat(states, "payload"),
                states,
                findings,
            )

        void_send_btn.click(
            fn=void_send_sync,
            inputs=[void_input, void_files, void_folder, settings_state, agent_states, findings_state],
            outputs=[
                void_chat,
                recon_chat,
                param_chat,
                xss_chat,
                sqli_chat,
                proto_chat,
                ssrf_chat,
                nuclei_chat,
                report_chat,
                payload_chat,
                agent_states,
                findings_state,
            ],
        )
        void_input.submit(
            fn=void_send_sync,
            inputs=[void_input, void_files, void_folder, settings_state, agent_states, findings_state],
            outputs=[
                void_chat,
                recon_chat,
                param_chat,
                xss_chat,
                sqli_chat,
                proto_chat,
                ssrf_chat,
                nuclei_chat,
                report_chat,
                payload_chat,
                agent_states,
                findings_state,
            ],
        )

        demo.load(fn=settings_status, inputs=[settings_state], outputs=[settings_status_md])

    return demo


if __name__ == "__main__":
    # Keep it simple: single-file runnable.
    # If you need to expose publicly, set share=True.
    build_ui().launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT", "7860")))

# One-liner:
#   python3 app.py
