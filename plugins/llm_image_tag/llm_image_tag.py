#!/usr/bin/env python3
"""
LLM Image Tag Plugin

This plugin uses a vision-capable LLM to suggest tags for an image. It can be started
from the UI (operations menu on the image page) or run as a task.

It is self-contained and does not require the CommunityScrapers repo at runtime.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any, Optional, List, Dict
import io
from PIL import Image

# Stash helper classes
try:
    from StashPluginHelper import StashPluginHelper, taskQueue  # type: ignore
except Exception:
    from stash_helper_fallback import StashPluginHelper, taskQueue  # type: ignore

# ----------------------------
# Configuration and utilities
# ----------------------------

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "gemma3:4b-it-q8_0"
DEFAULT_TEMP = 0.7
DEFAULT_MAX_TOKENS = -1
DEFAULT_TIMEOUT = 3600.0

PROMPT_DEFAULT = (
    "You are a tagging assistant. Look carefully at the image and return ONLY a JSON array "
    "of short, general-purpose tags that DIRECTLY describe what is clearly visible in the image. "
    "Include as many tags as are applicable; there is no strict upper limit. "
    "Use lowercase ASCII letters/digits; multiword tags may contain spaces. "
    "Do NOT use dashes; use spaces between words. "
    "Do NOT guess or infer hidden attributes. Include a tag only if it is clearly visible in the image. "
    "No private data, no people-identification or names, no hashes, no numbering, no explanations, "
    "no code fences, no extra text."
)

# Do not set a hard-coded default for llmBaseUrl here to avoid masking UI-saved config.
# The final value is resolved in _resolve_base_url() with robust precedence.
settings = {
    "llmModel": DEFAULT_MODEL,
    "llmTemp": DEFAULT_TEMP,
    "llmMaxTokens": DEFAULT_MAX_TOKENS,
    "llmTimeout": DEFAULT_TIMEOUT,
    "zzdebugTracing": False,
}

try:
    from llm_image_tag_settings import config  # type: ignore
except Exception:
    config = {}

stash = StashPluginHelper(settings=settings, config=config, maxbytes=10 * 1024 * 1024)

def _fetch_llm_base_url_from_settings() -> Optional[str]:
    """
    Best-effort fetch of saved 'llmBaseUrl' from Stash via GraphQL configuration.
    """
    try:
        query = """
            query($ids: [ID!]) {
                configuration {
                    plugins(include: $ids)
                }
            }
        """
        variables = {"ids": ["llm_image_tag", "LLMImageTag"]}
        resp = stash._graphql(query, variables)  # type: ignore[attr-defined]
        if not isinstance(resp, dict):
            return None
        plugins_map = (((resp.get("data") or {}).get("configuration") or {}).get("plugins")) or {}
        if not isinstance(plugins_map, dict):
            return None
        for pid in variables["ids"]:
            settings_map = plugins_map.get(pid)
            if isinstance(settings_map, dict):
                v = settings_map.get("llmBaseUrl")
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None
    except Exception:
        return None


def _resolve_base_url() -> str:
    """
    Resolve LLM base URL with robust precedence similar to whisper_transcribe:
      1) explicit args.llmBaseUrl
      2) UI setting via helper
      3) raw JSON payload in settings/pluginSettings
      4) GraphQL configuration fetch
      5) env LLM_BASE_URL
      6) built-in default
    """
    # 1 explicit arg
    try:
        arg_url = ((stash.JSON_INPUT or {}).get("args") or {}).get("llmBaseUrl") if isinstance(stash.JSON_INPUT, dict) else None
        if isinstance(arg_url, str) and arg_url.strip():
            return arg_url.strip().rstrip("/")
    except Exception:
        pass

    # 2 helper-provided UI setting
    try:
        ui_url = stash.Setting("llmBaseUrl", None)
        if isinstance(ui_url, str) and ui_url.strip():
            return ui_url.strip().rstrip("/")
    except Exception:
        pass

    # 3 direct read of raw JSON payload
    raw_url = None
    try:
        if isinstance(stash.JSON_INPUT, dict):
            settings_src = stash.JSON_INPUT.get("settings") or {}
            if isinstance(settings_src, dict):
                raw_url = settings_src.get("llmBaseUrl")
            elif isinstance(settings_src, list):
                for item in settings_src:
                    if isinstance(item, dict) and item.get("key") == "llmBaseUrl":
                        raw_url = item.get("value")
                        break
            if not raw_url:
                alt_src = stash.JSON_INPUT.get("pluginSettings") or {}
                if isinstance(alt_src, dict):
                    raw_url = alt_src.get("llmBaseUrl")
                elif isinstance(alt_src, list):
                    for item in alt_src:
                        if isinstance(item, dict) and item.get("key") == "llmBaseUrl":
                            raw_url = item.get("value")
                            break
        if isinstance(raw_url, str) and raw_url.strip():
            return raw_url.strip().rstrip("/")
    except Exception:
        pass

    # 4 GraphQL configuration fetch
    try:
        fetched = _fetch_llm_base_url_from_settings()
        if isinstance(fetched, str) and fetched.strip():
            return fetched.strip().rstrip("/")
    except Exception:
        pass

    # 5 environment variable
    env_url = os.getenv("LLM_BASE_URL")
    if isinstance(env_url, str) and env_url.strip():
        return env_url.strip().rstrip("/")

    # 6 default
    return DEFAULT_BASE_URL.rstrip("/")

def _env_or_setting(name: str, env: str, default: Any) -> Any:
    v = stash.Setting(name, None)
    if v is None:
        v = os.getenv(env, None)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return v

BASE_URL: str = _resolve_base_url()
MODEL: str = str(_env_or_setting("llmModel", "LLM_MODEL", DEFAULT_MODEL))
TEMP: float = float(_env_or_setting("llmTemp", "LLM_TEMP", DEFAULT_TEMP))
MAX_TOKENS: int = int(_env_or_setting("llmMaxTokens", "LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS))
TIMEOUT: float = float(_env_or_setting("llmTimeout", "LLM_TIMEOUT", DEFAULT_TIMEOUT))
API_KEY: str = os.getenv("LLM_API_KEY", "none")
PROMPT: str = os.getenv("LLM_TAG_PROMPT", PROMPT_DEFAULT)
INCLUDE_TAG_DESCRIPTIONS: bool = _env_or_setting("includeTagDescriptions", "LLM_INCLUDE_TAG_DESCRIPTIONS", True)
if isinstance(INCLUDE_TAG_DESCRIPTIONS, str):
    INCLUDE_TAG_DESCRIPTIONS = INCLUDE_TAG_DESCRIPTIONS.strip().lower() in ("true", "1", "yes", "on")

def _http_get(url: str, timeout: float = TIMEOUT) -> tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            headers = {k: v for k, v in resp.headers.items()}
            return (getattr(resp, "status", 200), headers, body)
    except urllib.error.HTTPError as e:
        return (e.code, {k: v for k, v in e.headers.items()} if e.headers else {}, e.read() if hasattr(e, "read") else b"")
    except Exception as e:
        raise RuntimeError(f"HTTP GET failed for {url}: {e}") from e

def _http_post_json(url: str, json_body: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: float = TIMEOUT) -> Dict[str, Any]:
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(json_body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception as e:
                raise RuntimeError(f"Non-JSON response from {url}: {raw[:500]!r} ({e})") from e
    except urllib.error.HTTPError as e:
        detail = getattr(e, "read", lambda: b"")()
        raise RuntimeError(f"HTTP {e.code} {e.reason} from {url}: {detail[:500].decode('utf-8', errors='ignore')}") from e
    except Exception as e:
        raise RuntimeError(f"HTTP POST failed for {url}: {e}") from e

def _read_image_bytes(path_or_url: str) -> tuple[bytes, str]:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        status, headers, body = _http_get(path_or_url, timeout=TIMEOUT)
        if status < 200 or status >= 300:
            raise RuntimeError(f"Failed to fetch image URL {path_or_url}: HTTP {status}")
        content_type = headers.get("Content-Type") or "application/octet-stream"
        data, mime = body, content_type
    else:
        mime = mimetypes.guess_type(os.path.basename(path_or_url))[0] or "image/jpeg"
        with open(path_or_url, "rb") as f:
            data = f.read()

    # Convert WebP images to PNG (or JPEG) in memory before base64 encoding
    if mime == "image/webp":
        try:
            img = Image.open(io.BytesIO(data))
            with io.BytesIO() as out:
                img.save(out, format="PNG")
                data = out.getvalue()
                mime = "image/png"
        except Exception as e:
            # Log but continue with original data if conversion fails
            stash.Error(f"Failed to convert WebP to PNG: {e}")

    return data, mime

def _message_content_to_str(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        parts: list[str] = []
        for part in msg:
            if isinstance(part, str):
                parts.append(part)
                continue
            if isinstance(part, dict):
                txt = part.get("text") or part.get("content")
                if txt:
                    parts.append(str(txt))
        if parts:
            return "\n".join(parts)
    if msg is None:
        return ""
    try:
        return json.dumps(msg)
    except Exception:
        return str(msg)

def _call_llm_b64_image_nonstreaming(b64: str, mime: str, existing_tags: Optional[list[dict[str, str]]] = None) -> str:
    url = f"{BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    messages: list[dict[str, Any]] = [{"role": "system", "content": PROMPT}]
    if existing_tags:
        intro, payload = _format_tags_for_prompt(existing_tags, INCLUDE_TAG_DESCRIPTIONS)
        messages.append({"role": "user", "content": [{"type": "text", "text": intro}]})
        messages.append({"role": "user", "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]})
    messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]})

    # Log text-only parts
    try:
        log_lines: list[str] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                log_lines.append(f"{m.get('role')}: {content}")
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        log_lines.append(f"{m.get('role')}: {part.get('text','')}")
        if log_lines:
            stash.Error("[LLMImageTag] Prompt (text only):\n" + "\n".join(log_lines))
    except Exception:
        pass

    payload = {"model": MODEL, "messages": messages, "temperature": TEMP, "max_tokens": MAX_TOKENS}
    data = _http_post_json(url, payload, headers=headers, timeout=TIMEOUT)
    try:
        msg = (data["choices"][0]["message"]) or {}
        content = _message_content_to_str(msg.get("content"))
        if not content:
            content = _message_content_to_str(msg)
        return content
    except Exception:
        raise RuntimeError(f"Unexpected LLM response: {data!r}")


def _write_stream_progress(image_id, request_id, reasoning, output, done, error=None):
    if not image_id or not request_id:
        return
    results_dir = os.path.join(_plugin_dir(), "results")
    os.makedirs(results_dir, exist_ok=True)
    safe_request_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(request_id).strip())
    payload = {
        "image_id": int(image_id),
        "done": bool(done),
        "reasoning": str(reasoning) if reasoning else "",
        "output": str(output) if output else "",
        "error": str(error) if error else None,
    }
    tmp_path = os.path.join(results_dir, f"{image_id}_{safe_request_id}_stream.json.tmp")
    final_path = os.path.join(results_dir, f"{image_id}_{safe_request_id}_stream.json")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, final_path)


def _call_llm_b64_image_streaming(b64, mime, existing_tags, request_id, image_id):
    url = f"{BASE_URL}/chat/completions"
    h = {"Content-Type": "application/json"}
    if API_KEY and API_KEY != "none":
        h["Authorization"] = f"Bearer {API_KEY}"

    messages: list[dict[str, Any]] = [{"role": "system", "content": PROMPT}]
    if existing_tags:
        intro, payload = _format_tags_for_prompt(existing_tags, INCLUDE_TAG_DESCRIPTIONS)
        messages.append({"role": "user", "content": [{"type": "text", "text": intro}]})
        messages.append({"role": "user", "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]})
    messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]})

    payload = {"model": MODEL, "messages": messages, "temperature": TEMP, "max_tokens": MAX_TOKENS, "stream": True}

    try:
        log_lines: list[str] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                log_lines.append(f"{m.get('role')}: {content}")
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        log_lines.append(f"{m.get('role')}: {part.get('text','')}")
        if log_lines:
            stash.Error("[LLMImageTag] Prompt (text only):\n" + "\n".join(log_lines))
    except Exception:
        pass

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")

    reasoning_parts: list[str] = []
    output_parts: list[str] = []
    partial_line = ""

    _write_stream_progress(image_id, request_id, "", "", done=False)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            while True:
                byte = resp.read(1)
                if not byte:
                    break
                partial_line += byte.decode("utf-8", errors="replace")

                if not partial_line.endswith("\n"):
                    continue

                line = partial_line.strip()
                partial_line = ""
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices")
                if not choices or not isinstance(choices, list):
                    continue
                delta = (choices[0] or {}).get("delta", {})
                if not isinstance(delta, dict):
                    continue

                rc = delta.get("reasoning_content")
                c = delta.get("content")

                if isinstance(rc, str) and rc:
                    reasoning_parts.append(rc)
                if isinstance(c, str) and c:
                    output_parts.append(c)

                _write_stream_progress(
                    image_id, request_id,
                    "".join(reasoning_parts),
                    "".join(output_parts),
                    done=False,
                )
    except Exception as e:
        _write_stream_progress(
            image_id, request_id,
            "".join(reasoning_parts),
            "".join(output_parts),
            done=True, error=str(e),
        )
        raise

    reasoning_text = "".join(reasoning_parts)
    output_text = "".join(output_parts)

    if not output_text and not reasoning_text:
        raise RuntimeError("No content received from LLM stream")

    _write_stream_progress(image_id, request_id, reasoning_text, output_text, done=True)

    return reasoning_text, output_text


def _call_llm_b64_image(b64: str, mime: str, existing_tags: Optional[list[dict[str, str]]] = None, request_id: Optional[str] = None, image_id: Optional[int] = None) -> tuple[str, str]:
    if request_id and image_id:
        try:
            reasoning, content = _call_llm_b64_image_streaming(b64, mime, existing_tags, request_id, image_id)
            return reasoning, content
        except Exception as e:
            stash.Error(f"[LLMImageTag] Streaming failed ({e}), falling back to non-streaming")

    content = _call_llm_b64_image_nonstreaming(b64, mime, existing_tags)
    reasoning = _extract_reasoning_from_content(content)
    return reasoning, content


def _extract_reasoning_from_content(content: str) -> str:
    think_match = re.search(r"<think>(.*?)</think>", content, flags=re.DOTALL | re.IGNORECASE)
    if think_match:
        return think_match.group(1).strip()
    return ""



def _strip_think_blocks(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

def _parse_tags(text: str) -> List[str]:
    text = text.strip()
    tags: List[str] = []
    start = text.find("["); end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        maybe_json = text[start : end + 1]
        try:
            arr = json.loads(maybe_json)
            if isinstance(arr, list):
                tags = [str(x) for x in arr]
        except Exception:
            pass
    if not tags:
        sep = "," if "," in text else "\n"
        tags = [t.strip() for t in text.split(sep)]

    cleaned: List[str] = []
    for t in tags:
        t = t.strip().strip("#").strip().lower()
        t = "".join(ch for ch in t if (ch.isalnum() or ch in "- _"))
        if 1 <= len(t) <= 50:
            cleaned.append(t)
    seen = set()
    uniq: List[str] = []
    for t in cleaned:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq

def _existing_tags() -> list[dict[str, str]]:
    try:
        query = """
            query($filter: FindFilterType) {
              findTags(filter: $filter) {
                tags { name aliases description ignore_auto_tag }
              }
            }
        """
        variables = {"filter": {"per_page": -1}}
        resp = stash._graphql(query, variables)  # type: ignore[attr-defined]
        entries: list[dict[str, str]] = []
        if isinstance(resp, dict):
            tags = (((resp.get("data") or {}).get("findTags") or {}).get("tags")) or []
            for t in tags or []:
                if t.get("ignore_auto_tag"):
                    continue
                name = t.get("name")
                description = t.get("description") or ""
                if name:
                    entries.append({"name": str(name), "description": str(description)})
                for alias in t.get("aliases") or []:
                    entries.append({"name": str(alias), "description": str(description)})
        # de-dupe by name
        seen = set()
        uniq: list[dict[str, str]] = []
        for entry in entries:
            name = entry["name"]
            if name not in seen:
                seen.add(name)
                uniq.append(entry)
        return uniq
    except Exception as e:
        stash.Error(f"[LLMImageTag] Failed to fetch existing tags: {e}")
        return []

def _format_tags_for_prompt(tags: list[dict[str, str]], include_descriptions: bool) -> tuple[str, str]:
    if include_descriptions:
        intro = (
            "The following input is a JSON array of existing tags already in the database, each with a name "
            "and description. Use the descriptions to understand what each tag means. Choose from these tags "
            "where applicable, but you may suggest additional tags that are not in this list. "
            "Do not guess or infer hidden attributes."
        )
        payload = tags
    else:
        intro = (
            "The following input is a JSON array of existing tags already in the database. "
            "Choose from these where applicable, but you may suggest additional tags that are not in this list. "
            "Do not guess or infer hidden attributes."
        )
        payload = [entry["name"] for entry in tags]
    return intro, payload

def _fetch_image_path(image_id: int) -> Optional[str]:
    try:
        query = """
            query($id: ID!) {
              findImage(id: $id) {
                paths { image }
                files { path }
              }
            }
        """
        resp = stash._graphql(query, {"id": str(image_id)})  # type: ignore[attr-defined]
        img = (resp or {}).get("data", {}).get("findImage") or {}
        path = None
        paths = img.get("paths") or {}
        if isinstance(paths, dict):
            path = paths.get("image")
        if not path:
            files = img.get("files") or []
            if isinstance(files, list) and files:
                path = (files[0] or {}).get("path")
        return path
    except Exception as e:
        stash.Error(f"[LLMImageTag] GraphQL path lookup failed for image {image_id}: {e}")
        return None

def tags_from_image(path_or_url: str, request_id: Optional[str] = None, image_id: Optional[int] = None) -> tuple[List[str], str, str]:
    try:
        existing = _existing_tags()
    except Exception:
        existing = []
    try:
        data, mime = _read_image_bytes(path_or_url)
        b64 = base64.b64encode(data).decode("utf-8")
        reasoning, content = _call_llm_b64_image(b64, mime, existing_tags=existing, request_id=request_id, image_id=image_id)
        stash.Error(f"[LLMImageTag] LLM raw output: {content}")
        cleaned = _strip_think_blocks(content)
        tags = _parse_tags(cleaned)
        return tags, reasoning, content
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"[LLMImageTag] Tagging failed for {path_or_url}: {e}\n{tb}")
        return [], "", ""

def tag_image(image_id: int, request_id: Optional[str] = None) -> tuple[Optional[List[str]], str, str]:
    path = _fetch_image_path(image_id)
    if not path:
        stash.Error(f"[LLMImageTag] No image path found for id={image_id}")
        return None, "", ""
    tags, reasoning, output = tags_from_image(path, request_id=request_id, image_id=image_id)
    if not tags:
        stash.Error(f"[LLMImageTag] No tags returned for image {image_id}")
    else:
        stash.Error(f"[LLMImageTag] Suggested tags for image {image_id}: {tags}")

    return tags, reasoning, output

def tag_image_task() -> None:
    try:
        image_id = stash.JSON_INPUT.get("args", {}).get("image_id") if stash.JSON_INPUT else None
        request_id = stash.JSON_INPUT.get("args", {}).get("request_id") if stash.JSON_INPUT else None
        if image_id is None:
            stash.Error("[LLMImageTag] No image_id supplied to tag_image_task")
            return
        image_id = int(image_id)
        tags, reasoning, output = tag_image(image_id, request_id=request_id)
        error = None
        if tags is None:
            error = "No image path found."
            tags = []
        _write_result(image_id, tags, error=error, request_id=request_id, reasoning=reasoning, output=output)
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"[LLMImageTag] Exception in tag_image_task: {e}\nTraceBack={tb}")
        try:
            image_id = stash.JSON_INPUT.get("args", {}).get("image_id") if stash.JSON_INPUT else None
            if image_id is not None:
                request_id = stash.JSON_INPUT.get("args", {}).get("request_id") if stash.JSON_INPUT else None
                _write_result(int(image_id), [], error=str(e), request_id=request_id, reasoning="", output="")
        except Exception:
            pass

def _plugin_dir() -> str:
    sc = stash.JSON_INPUT.get("server_connection") or stash.JSON_INPUT.get("serverConnection") or {}
    if isinstance(sc, dict):
        for key in ("plugin_dir", "PluginDir", "pluginDir"):
            val = sc.get(key)
            if isinstance(val, str) and val:
                return val
    return os.path.dirname(os.path.abspath(__file__))

def _write_result(image_id: int, tags: List[str], error: Optional[str] = None, request_id: Optional[str] = None, reasoning: str = "", output: str = "") -> None:
    results_dir = os.path.join(_plugin_dir(), "results")
    os.makedirs(results_dir, exist_ok=True)
    safe_request_id = None
    if isinstance(request_id, str) and request_id.strip():
        safe_request_id = re.sub(r"[^A-Za-z0-9_-]", "_", request_id.strip())
    payload = {
        "image_id": image_id,
        "tags": tags,
        "error": error,
        "request_id": safe_request_id,
        "reasoning": reasoning,
        "output": output,
    }
    suffix = f"_{safe_request_id}" if safe_request_id else ""
    tmp_path = os.path.join(results_dir, f"{image_id}{suffix}.json.tmp")
    final_path = os.path.join(results_dir, f"{image_id}{suffix}.json")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, final_path)

# -------------
# Entry point
# -------------
try:
    if stash.Setting("zzdebugTracing", False):
        stash.Error(f"[LLMImageTag] Using BASE_URL={BASE_URL!r} model={MODEL!r} temp={TEMP} max_tokens={MAX_TOKENS} timeout={TIMEOUT}")
    if stash.PLUGIN_TASK_NAME == "tag_image_task":
        stash.Error(f"PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}")
        tag_image_task()
    elif stash.JSON_INPUT and (stash.JSON_INPUT.get("args", {}).get("mode") == "tag_image_task"):
        stash.Error("Dispatch via args.mode=tag_image_task")
        tag_image_task()
    else:
        stash.Error(f"[LLMImageTag] No task specified (PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}). Nothing to do.")
except Exception as e:
    tb = traceback.format_exc()
    stash.Error(f"[LLMImageTag] Exception while running plugin: {e}\nTraceBack={tb}")

# Ensure valid JSON on stdout for raw interface
try:
    print("null")
except Exception:
    pass
