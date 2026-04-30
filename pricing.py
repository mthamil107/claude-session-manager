"""
Per-session cost calculation for Claude Code .jsonl conversations.

Pricing data is pulled from LiteLLM's community-maintained price catalogue
(public JSON, updated multiple times a week). Falls back to a bundled
default if no synced copy exists.

Public API:
    sync_pricing(force=False) -> (count_updated, last_sync_iso)
    load_pricing() -> dict[model_id -> {input, output, cache_write, cache_read}]
    compute_session_cost(jsonl_path) -> {total_usd, by_model, tokens}
    format_cost(usd) -> str
"""

import json
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PRICING_FILE = SCRIPT_DIR / "pricing.json"
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Bundled defaults — per-token USD. Source: Anthropic public pricing pages
# as of 2026-04-27. Will be overwritten on first successful Sync.
DEFAULT_PRICING = {
    "claude-opus-4-7":        {"input": 15e-6, "output": 75e-6, "cache_write": 18.75e-6, "cache_read": 1.5e-6},
    "claude-opus-4-6":        {"input": 15e-6, "output": 75e-6, "cache_write": 18.75e-6, "cache_read": 1.5e-6},
    "claude-sonnet-4-6":      {"input":  3e-6, "output": 15e-6, "cache_write":  3.75e-6, "cache_read": 0.3e-6},
    "claude-haiku-4-5":       {"input":  1e-6, "output":  5e-6, "cache_write":  1.25e-6, "cache_read": 0.1e-6},
    "claude-haiku-4-5-20251001": {"input": 1e-6, "output": 5e-6, "cache_write": 1.25e-6, "cache_read": 0.1e-6},
}


def _normalize_litellm_entry(entry):
    """Convert LiteLLM's per-token cost fields into our schema."""
    return {
        "input":       float(entry.get("input_cost_per_token", 0) or 0),
        "output":      float(entry.get("output_cost_per_token", 0) or 0),
        "cache_write": float(entry.get("cache_creation_input_token_cost", 0) or 0),
        "cache_read":  float(entry.get("cache_read_input_token_cost", 0) or 0),
    }


def sync_pricing(timeout=15):
    """Fetch the latest pricing from LiteLLM and save a filtered copy locally.
    Returns (model_count, iso_timestamp)."""
    req = urllib.request.Request(
        LITELLM_URL,
        headers={"User-Agent": "claude-session-manager/1.0 (+sync_pricing)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    # Keep only entries that look like Anthropic / Claude pricing
    filtered = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        provider = (entry.get("litellm_provider") or "").lower()
        if "anthropic" not in provider and "claude" not in name.lower() and "bedrock" not in provider:
            continue
        if "input_cost_per_token" not in entry:
            continue
        filtered[name] = _normalize_litellm_entry(entry)

    payload = {
        "_meta": {
            "synced_at": datetime.now().isoformat(timespec="seconds"),
            "source": LITELLM_URL,
            "model_count": len(filtered),
        },
        "models": filtered,
    }
    with open(PRICING_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return (len(filtered), payload["_meta"]["synced_at"])


def load_pricing():
    """Return a flat dict of model_id -> price dict.
    Merges synced data over bundled defaults."""
    merged = dict(DEFAULT_PRICING)
    if PRICING_FILE.exists():
        try:
            with open(PRICING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, prices in data.get("models", {}).items():
                merged[name] = prices
        except Exception:
            pass
    return merged


def get_pricing_meta():
    """Return metadata about the last sync (or None)."""
    if not PRICING_FILE.exists():
        return None
    try:
        with open(PRICING_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("_meta")
    except Exception:
        return None


def _lookup_price(model_name, pricing_table):
    """Find a price entry for a model name. Tries exact, then prefix, then contains."""
    if not model_name:
        return None
    if model_name in pricing_table:
        return pricing_table[model_name]
    # Try without provider prefix (e.g. "anthropic/claude-..." -> "claude-...")
    if "/" in model_name:
        suffix = model_name.split("/", 1)[1]
        if suffix in pricing_table:
            return pricing_table[suffix]
    # Fuzzy: any key that contains or is contained
    name_lower = model_name.lower()
    for key, val in pricing_table.items():
        kl = key.lower()
        if kl in name_lower or name_lower in kl:
            return val
    return None


def compute_session_cost(jsonl_path, pricing_table=None):
    """Walk a .jsonl file, sum per-model token usage and dollar cost.

    Returns:
        {
            "total_usd": float,
            "by_model": { model_name: { "usd": float, "input": int, "output": int,
                                        "cache_write": int, "cache_read": int,
                                        "calls": int, "priced": bool } },
            "tokens": { totals across all models }
        }
    """
    if pricing_table is None:
        pricing_table = load_pricing()

    by_model = {}
    totals = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total_usd": 0.0}

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message", {})
                usage = msg.get("usage")
                model = msg.get("model") or rec.get("model") or "unknown"
                if not usage:
                    continue

                inp  = int(usage.get("input_tokens", 0) or 0)
                out  = int(usage.get("output_tokens", 0) or 0)
                cw   = int(usage.get("cache_creation_input_tokens", 0) or 0)
                cr   = int(usage.get("cache_read_input_tokens", 0) or 0)

                price = _lookup_price(model, pricing_table)
                if price:
                    cost = (
                        inp * price["input"] +
                        out * price["output"] +
                        cw  * price["cache_write"] +
                        cr  * price["cache_read"]
                    )
                    priced = True
                else:
                    cost = 0.0
                    priced = False

                slot = by_model.setdefault(model, {
                    "usd": 0.0, "input": 0, "output": 0,
                    "cache_write": 0, "cache_read": 0, "calls": 0, "priced": priced,
                })
                slot["usd"]         += cost
                slot["input"]       += inp
                slot["output"]      += out
                slot["cache_write"] += cw
                slot["cache_read"]  += cr
                slot["calls"]       += 1
                slot["priced"]       = priced

                totals["input"]      += inp
                totals["output"]     += out
                totals["cache_write"] += cw
                totals["cache_read"] += cr
                totals["total_usd"]  += cost
    except Exception:
        pass

    return {"total_usd": totals["total_usd"], "by_model": by_model, "tokens": totals}


def format_cost(usd):
    if usd is None or usd == 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.4f}"
    if usd < 1:
        return f"${usd:.3f}"
    return f"${usd:,.2f}"
