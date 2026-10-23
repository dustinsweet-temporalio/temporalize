"""
Temporalize settings: the Anthropic key and model, and the Temporal target the POC runs on.

The key and the target's API key are stored in MongoDB (local to this machine) and never
returned to the browser; the view shows only whether they are set and the key's last
four characters. A blank saved key falls back to ANTHROPIC_API_KEY from .env.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from temporalize import store
from temporalize.llm import MODELS, api_key, default_model

CLOUD_UI = "https://cloud.temporal.io"


def _last4(key: str) -> str | None:
    return key[-4:] if key else None


def temporal_target(settings: dict | None = None) -> dict:
    """Where the POC's workflows run: the compose dev server, or Temporal Cloud."""
    settings = settings if settings is not None else store.get_settings()
    t = settings.get("temporal") or {}
    if t.get("mode") == "cloud":
        return {"mode": "cloud", "address": t.get("address", ""), "namespace": t.get("namespace", ""),
                "api_key": t.get("api_key", ""), "ui_base": CLOUD_UI,
                "label": "Temporal Cloud", "display_address": t.get("address", "")}
    ui = os.environ.get("TEMPORAL_UI_URL", "").strip() or "http://localhost:8233"
    address = os.environ.get("TEMPORAL_ADDRESS", "").strip() or "localhost:7233"
    port = address.rsplit(":", 1)[-1] if ":" in address else "7233"
    return {"mode": "local", "address": address, "namespace": os.environ.get("TEMPORAL_NAMESPACE", "").strip() or "default",
            "api_key": "", "ui_base": ui.rstrip("/"), "label": "Temporal dev server",
            "display_address": f"localhost:{port}"}


def workflow_url(target: dict, workflow_id: str) -> str:
    return f"{target['ui_base'].rstrip('/')}/namespaces/{target['namespace']}/workflows/{workflow_id}"


def harness_env(target: dict) -> dict[str, str]:
    """Environment the generated POC reads to reach the target."""
    return {"TZ_TEMPORAL_ADDRESS": target["address"], "TZ_TEMPORAL_NAMESPACE": target["namespace"],
            "TZ_TEMPORAL_API_KEY": target.get("api_key") or ""}


def view(settings: dict | None = None) -> dict:
    settings = settings if settings is not None else store.get_settings()
    saved_key = (settings.get("anthropic_api_key") or "").strip()
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    key = saved_key or env_key
    target = temporal_target(settings)
    return {
        "anthropic": {"configured": bool(key), "source": "saved" if saved_key else ("env" if env_key else None),
                      "last4": _last4(key)},
        "model": settings.get("model") or default_model(),
        "models": MODELS,
        "temporal": {"mode": target["mode"], "address": target["display_address"], "namespace": target["namespace"],
                     "api_key_set": bool(target.get("api_key")), "ui_url": target["ui_base"]},
        "saved": bool(settings.get("saved")),
    }


def check_key(key: str, model: str) -> dict:
    """Token counting: checks the key and the model without a billed call."""
    import anthropic

    key = key.strip() or api_key()
    if not key:
        return {"ok": False, "error": "No API key. Paste one, or set ANTHROPIC_API_KEY in .env."}
    try:
        anthropic.Anthropic(api_key=key, timeout=20, max_retries=1).messages.count_tokens(
            model=model or default_model(), messages=[{"role": "user", "content": "ping"}])
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "The key was rejected."}
    except anthropic.NotFoundError:
        return {"ok": False, "error": f"Model {model} was not found for this key."}
    except anthropic.APIError as e:
        return {"ok": False, "error": f"The Anthropic API is not reachable: {e}"}
    return {"ok": True, "last4": _last4(key)}


async def check_temporal(target: dict) -> str:
    """Connects to the target and checks its health; returns the server version or raises."""
    from temporalio.client import Client

    kwargs = {"namespace": target["namespace"]}
    if target["mode"] == "cloud":
        if not (target["address"] and target["namespace"] and target.get("api_key")):
            raise ValueError("Temporal Cloud needs an address, a namespace and an API key.")
        kwargs.update(api_key=target["api_key"], tls=True)
    client = await asyncio.wait_for(Client.connect(target["address"], **kwargs), 15)
    if not await client.service_client.check_health(timeout=timedelta(seconds=5)):
        raise RuntimeError("the server is not serving")
    return target["display_address"]


def snapshot(settings: dict | None = None) -> dict:
    """The settings a run is created with, kept on the run (no secrets): its screen 1, and what it runs with."""
    settings = settings if settings is not None else store.get_settings()
    v = view(settings)
    t = settings.get("temporal") or {}
    return {"model": v["model"], "anthropic": {"source": v["anthropic"]["source"], "last4": v["anthropic"]["last4"]},
            "temporal": {"mode": v["temporal"]["mode"], "address": t.get("address", "") if v["temporal"]["mode"] == "cloud"
                         else "", "display_address": v["temporal"]["address"], "namespace": v["temporal"]["namespace"],
                         "ui_url": v["temporal"]["ui_url"]},
            "captured_at": store.now()}


def run_settings(run: dict) -> dict:
    """
    The settings a run uses: the model and Temporal target captured when it was created, so later
    changes on the setup screen never change an existing run. Secrets are today's (a Cloud API key
    only when the target is the same). Runs from before snapshots use today's settings.
    """
    current = store.get_settings()
    snap = run.get("settings")
    if not snap:
        return current
    t = snap.get("temporal") or {}
    ct = current.get("temporal") or {}
    temporal = {"mode": t.get("mode", "local"), "address": t.get("address", ""), "namespace": t.get("namespace", "")}
    if temporal["mode"] == "cloud":
        temporal["api_key"] = ct.get("api_key", "") if ct.get("address") == temporal["address"] else ""
    return {**current, "model": snap.get("model") or current.get("model"), "temporal": temporal}


def save(body: dict) -> dict:
    """Store what the setup screen sends; blank secrets keep the stored ones."""
    current = store.get_settings()
    fields: dict = {"saved": True, "model": body.get("model") or current.get("model") or default_model()}
    if (body.get("api_key") or "").strip():
        fields["anthropic_api_key"] = body["api_key"].strip()
        os.environ["ANTHROPIC_API_KEY"] = fields["anthropic_api_key"]  # this app's own preflight reads it
    t = body.get("temporal") or {}
    old = current.get("temporal") or {}
    if t.get("mode") == "cloud":
        fields["temporal"] = {"mode": "cloud", "address": (t.get("address") or old.get("address") or "").strip(),
                              "namespace": (t.get("namespace") or old.get("namespace") or "").strip(),
                              "api_key": (t.get("api_key") or "").strip() or old.get("api_key", "")}
    else:
        fields["temporal"] = {"mode": "local", "address": old.get("address", ""), "namespace": old.get("namespace", ""),
                              "api_key": old.get("api_key", "")}
    return view(store.save_settings(fields))
