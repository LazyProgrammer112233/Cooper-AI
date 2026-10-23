"""Per-company formula choices ("our UBO counts outlets with an order, not an invoice").

Stored as one small JSON file per company on this machine.
"""

from __future__ import annotations

import json
import threading

from . import config
from .catalog import METRICS

_lock = threading.Lock()


def _path(company_id: int):
    config.PREFS_DIR.mkdir(parents=True, exist_ok=True)
    return config.PREFS_DIR / f"company_{int(company_id)}.json"


def load(company_id: int) -> dict[str, str]:
    p = _path(company_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k in METRICS}
    except (OSError, json.JSONDecodeError):
        return {}


def save(company_id: int, metric_id: str, variant_id: str) -> dict[str, str]:
    if metric_id not in METRICS:
        raise KeyError(metric_id)
    if variant_id not in {v.id for v in METRICS[metric_id].variants}:
        raise KeyError(variant_id)
    with _lock:
        data = load(company_id)
        data[metric_id] = variant_id
        _path(company_id).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def reset(company_id: int, metric_id: str | None = None) -> dict[str, str]:
    with _lock:
        data = load(company_id)
        if metric_id:
            data.pop(metric_id, None)
        else:
            data = {}
        _path(company_id).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data
