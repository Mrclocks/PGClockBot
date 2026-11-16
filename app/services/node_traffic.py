"""Merge PasarGuard node list with realtime / traffic fields."""

from __future__ import annotations

from typing import Any

from app.services.formatting import (
    format_bytes,
    format_bytes_parts,
    format_bytes_rate,
    format_bytes_rate_parts,
    format_bytes_ratio_parts,
    node_status_fa,
)
from app.services.host_gauges import tone_class
from app.services.host_metrics import format_memory_ratio
from app.services.pasarguard import as_list


def _as_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _as_float(value: Any) -> float | None:
    if value is None or value == "" or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_bytes(obj: dict, *keys: str) -> int | None:
    for key in keys:
        if key in obj and obj.get(key) is not None:
            n = _as_int(obj.get(key))
            if n is not None:
                return n
    return None


def _pick_float(obj: dict, *keys: str) -> float | None:
    for key in keys:
        if key in obj and obj.get(key) is not None:
            n = _as_float(obj.get(key))
            if n is not None:
                return n
    return None


def _clamp_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, min(100.0, float(value))), 1)


def _index_realtime(raw: Any) -> dict[int, dict]:
    """Normalize realtime_stats payload → {node_id: stats_dict}.

    PasarGuard returns ``dict[int, NodeRealtimeStats | None]`` (id-keyed).
    """
    out: dict[int, dict] = {}
    if raw is None:
        return out
    rows: list = []
    if isinstance(raw, dict):
        # { "1": {...}, "2": {...} } or { "nodes": [...] } or { "stats": [...] }
        for key in ("nodes", "stats", "realtime", "items", "data"):
            if key in raw:
                rows = as_list(raw, key) or ([] if not isinstance(raw[key], list) else raw[key])
                break
        if not rows:
            # id-keyed map (None values skipped)
            for k, v in raw.items():
                if isinstance(v, dict) and str(k).isdigit():
                    out[int(k)] = v
            return out
    elif isinstance(raw, list):
        rows = raw
    for row in rows:
        if not isinstance(row, dict):
            continue
        nid = row.get("node_id") if row.get("node_id") is not None else row.get("id")
        try:
            nid_i = int(nid)
        except (TypeError, ValueError):
            continue
        out[nid_i] = row
    return out


def _status_kind(status: Any) -> str:
    st = str(status or "").strip().lower()
    if st in {"connected", "online", "healthy"}:
        return "ok"
    if "connect" in st:
        return "warn"
    if st in {"error", "offline", "unhealthy", "disabled", "disconnected"}:
        return "err"
    return "neutral"


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "—"
    return f"{int(round(pct))}٪"


def enrich_nodes_with_traffic(nodes: list | None, realtime: Any = None) -> list[dict]:
    """Attach uplink/downlink/total (+ CPU/RAM/rates when realtime provides them)."""
    rt = _index_realtime(realtime)
    enriched: list[dict] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        n = dict(node)
        try:
            nid = int(n.get("id"))
        except (TypeError, ValueError):
            nid = None
        stats = rt.get(nid) if nid is not None else None
        src = stats if isinstance(stats, dict) else {}
        # Prefer realtime; fall back to fields already on the node
        uplink = _pick_bytes(
            src,
            "uplink",
            "upload",
            "outgoing_bandwidth",
            "outgoing",
            "sent",
            "tx",
        )
        if uplink is None:
            uplink = _pick_bytes(
                n,
                "uplink",
                "upload",
                "outgoing_bandwidth",
                "outgoing",
                "traffic_up",
                "tx",
            )
        downlink = _pick_bytes(
            src,
            "downlink",
            "download",
            "incoming_bandwidth",
            "incoming",
            "received",
            "rx",
        )
        if downlink is None:
            downlink = _pick_bytes(
                n,
                "downlink",
                "download",
                "incoming_bandwidth",
                "incoming",
                "traffic_down",
                "rx",
            )
        total = _pick_bytes(src, "traffic", "total", "used_traffic", "bandwidth")
        if total is None:
            total = _pick_bytes(n, "traffic", "total", "used_traffic", "bandwidth")
        if total is None and uplink is not None and downlink is not None:
            total = int(uplink) + int(downlink)
        elif total is None:
            total = uplink if uplink is not None else downlink

        # Live rates — PasarGuard NodeRealtimeStats:
        # outgoing_bandwidth_speed ≈ uplink (upload), incoming ≈ downlink (download)
        rate_up = _pick_bytes(
            src,
            "outgoing_bandwidth_speed",
            "uplink_speed",
            "upload_speed",
            "outgoing_speed",
            "tx_speed",
        )
        rate_down = _pick_bytes(
            src,
            "incoming_bandwidth_speed",
            "downlink_speed",
            "download_speed",
            "incoming_speed",
            "rx_speed",
        )

        cpu = _clamp_pct(_pick_float(src, "cpu_usage", "cpu", "cpu_percent"))
        if cpu is None:
            cpu = _clamp_pct(_pick_float(n, "cpu_usage", "cpu", "cpu_percent"))
        cpu_cores = _as_int(src.get("cpu_cores")) if "cpu_cores" in src else None
        if cpu_cores is None:
            cpu_cores = _as_int(n.get("cpu_cores")) if "cpu_cores" in n else None

        mem_used = _pick_bytes(src, "mem_used", "memory_used", "ram_used")
        mem_total = _pick_bytes(src, "mem_total", "memory_total", "ram_total")
        mem_pct = _clamp_pct(_pick_float(src, "memory_percent", "mem_percent", "ram_percent"))
        if mem_pct is None and mem_used is not None and mem_total:
            try:
                mem_pct = _clamp_pct((float(mem_used) / float(mem_total)) * 100.0)
            except (TypeError, ValueError, ZeroDivisionError):
                mem_pct = None

        n["_traffic_up"] = uplink
        n["_traffic_down"] = downlink
        n["_traffic_total"] = total
        up_amt, up_unit = format_bytes_parts(uplink) if uplink is not None else ("—", "")
        down_amt, down_unit = (
            format_bytes_parts(downlink) if downlink is not None else ("—", "")
        )
        tot_amt, tot_unit = format_bytes_parts(total) if total is not None else ("—", "")
        n["_traffic_up_num"], n["_traffic_up_unit"] = up_amt, up_unit
        n["_traffic_down_num"], n["_traffic_down_unit"] = down_amt, down_unit
        n["_traffic_total_num"], n["_traffic_total_unit"] = tot_amt, tot_unit
        n["_traffic_up_text"] = format_bytes(uplink) if uplink is not None else "—"
        n["_traffic_down_text"] = format_bytes(downlink) if downlink is not None else "—"
        n["_traffic_total_text"] = format_bytes(total) if total is not None else "—"
        n["_rate_up"] = rate_up
        n["_rate_down"] = rate_down
        rate_up_amt, rate_up_unit = format_bytes_rate_parts(rate_up)
        rate_down_amt, rate_down_unit = format_bytes_rate_parts(rate_down)
        n["_rate_up_num"], n["_rate_up_unit"] = rate_up_amt, rate_up_unit
        n["_rate_down_num"], n["_rate_down_unit"] = rate_down_amt, rate_down_unit
        n["_rate_up_text"] = format_bytes_rate(rate_up)
        n["_rate_down_text"] = format_bytes_rate(rate_down)
        n["_cpu_percent"] = cpu
        n["_cpu_cores"] = cpu_cores if isinstance(cpu_cores, int) and cpu_cores > 0 else None
        if n["_cpu_cores"] is not None:
            n["_cpu_cores_num"] = str(n["_cpu_cores"])
            n["_cpu_cores_unit"] = "هسته"
            n["_cpu_cores_text"] = f"{n['_cpu_cores_num']} {n['_cpu_cores_unit']}"
        else:
            n["_cpu_cores_num"] = ""
            n["_cpu_cores_unit"] = ""
            n["_cpu_cores_text"] = ""
        n["_cpu_text"] = _fmt_pct(cpu)
        n["_cpu_tone"] = tone_class(cpu)
        n["_mem_percent"] = mem_pct
        n["_mem_used"] = mem_used
        n["_mem_total"] = mem_total
        n["_mem_text"] = _fmt_pct(mem_pct)
        if mem_used is not None and mem_total:
            mem_parts = format_bytes_ratio_parts(mem_used, mem_total, precision=1)
            n["_mem_used_num"] = str(mem_parts.get("used") or "—")
            n["_mem_total_num"] = str(mem_parts.get("total") or "—")
            n["_mem_unit"] = str(mem_parts.get("unit") or "")
            n["_mem_ratio_text"] = format_memory_ratio(mem_used, mem_total)
        else:
            n["_mem_used_num"] = ""
            n["_mem_total_num"] = ""
            n["_mem_unit"] = ""
            n["_mem_ratio_text"] = "—"
        n["_mem_tone"] = tone_class(mem_pct)
        status = n.get("status") or n.get("connection_status") or ""
        n["_status"] = status
        n["_status_kind"] = _status_kind(status)
        enriched.append(n)
    return enriched


def _sum_optional(values: list[int | None]) -> int | None:
    present = [int(v) for v in values if v is not None]
    if not present:
        return None
    return sum(present)


def _metric_parts(amount: str, unit: str) -> dict[str, str]:
    return {"num": amount or "—", "unit": unit or ""}


def node_overview_card(node: dict) -> dict[str, Any]:
    """Lean, sanitized card payload for PG overview SSR + /pg/metrics poll."""
    try:
        nid = int(node.get("id"))
    except (TypeError, ValueError):
        nid = None
    name = str(node.get("name") or node.get("address") or "—").strip() or "—"
    # Cap name length for JSON surface (display only)
    if len(name) > 80:
        name = name[:77] + "…"
    rate_up_parts = _metric_parts(
        str(node.get("_rate_up_num") or "—"), str(node.get("_rate_up_unit") or "")
    )
    rate_down_parts = _metric_parts(
        str(node.get("_rate_down_num") or "—"), str(node.get("_rate_down_unit") or "")
    )
    traffic_up_parts = _metric_parts(
        str(node.get("_traffic_up_num") or "—"), str(node.get("_traffic_up_unit") or "")
    )
    traffic_down_parts = _metric_parts(
        str(node.get("_traffic_down_num") or "—"), str(node.get("_traffic_down_unit") or "")
    )
    cpu_cores_parts = _metric_parts(
        str(node.get("_cpu_cores_num") or ""), str(node.get("_cpu_cores_unit") or "")
    )
    if cpu_cores_parts["num"] in {"", "—"} and not cpu_cores_parts["unit"]:
        cpu_cores_parts = {"num": "", "unit": ""}
    mem_used_num = str(node.get("_mem_used_num") or "")
    mem_total_num = str(node.get("_mem_total_num") or "")
    mem_unit = str(node.get("_mem_unit") or "")
    mem_parts = {
        "used": mem_used_num or "—",
        "total": mem_total_num or "—",
        "unit": mem_unit,
    }
    if not mem_unit and mem_used_num in {"", "—"}:
        mem_parts = {"used": "", "total": "", "unit": ""}
    return {
        "id": nid,
        "name": name,
        "status": node_status_fa(node.get("_status") or node.get("status") or node.get("connection_status")),
        "status_kind": node.get("_status_kind") or _status_kind(node.get("status")),
        "cpu_percent": node.get("_cpu_percent"),
        "cpu_text": node.get("_cpu_text") or "—",
        "cpu_tone": node.get("_cpu_tone") or "neutral",
        "cpu_cores_text": node.get("_cpu_cores_text") or "",
        "cpu_cores_parts": cpu_cores_parts,
        "mem_percent": node.get("_mem_percent"),
        "mem_text": node.get("_mem_text") or "—",
        "mem_ratio_text": node.get("_mem_ratio_text") or "—",
        "mem_parts": mem_parts,
        "mem_tone": node.get("_mem_tone") or "neutral",
        "traffic_up_text": node.get("_traffic_up_text") or "—",
        "traffic_down_text": node.get("_traffic_down_text") or "—",
        "traffic_up": traffic_up_parts,
        "traffic_down": traffic_down_parts,
        "rate_up": node.get("_rate_up"),
        "rate_down": node.get("_rate_down"),
        "rate_up_text": node.get("_rate_up_text") or "—",
        "rate_down_text": node.get("_rate_down_text") or "—",
        "rate_up_parts": rate_up_parts,
        "rate_down_parts": rate_down_parts,
    }


def build_nodes_overview(nodes: list | None, realtime: Any = None) -> dict[str, Any]:
    """Owner PG overview: enriched cards + aggregate live uplink/downlink rates."""
    enriched = enrich_nodes_with_traffic(nodes, realtime)
    cards = [node_overview_card(n) for n in enriched]
    rate_up = _sum_optional([n.get("_rate_up") for n in enriched])
    rate_down = _sum_optional([n.get("_rate_down") for n in enriched])
    up_num, up_unit = format_bytes_rate_parts(rate_up)
    down_num, down_unit = format_bytes_rate_parts(rate_down)
    return {
        "nodes": cards,
        "live": {
            "rate_up": rate_up,
            "rate_down": rate_down,
            "rate_up_text": format_bytes_rate(rate_up),
            "rate_down_text": format_bytes_rate(rate_down),
            "rate_up_num": up_num,
            "rate_up_unit": up_unit,
            "rate_down_num": down_num,
            "rate_down_unit": down_unit,
        },
    }


def nodes_overview_json(overview: dict[str, Any]) -> dict[str, Any]:
    """Whitelist JSON for polling — never dump raw PasarGuard payloads."""
    live = overview.get("live") if isinstance(overview.get("live"), dict) else {}
    nodes_out: list[dict[str, Any]] = []
    for card in overview.get("nodes") or []:
        if not isinstance(card, dict):
            continue
        nodes_out.append(
            {
                "id": card.get("id"),
                "name": card.get("name"),
                "status": card.get("status"),
                "status_kind": card.get("status_kind"),
                "cpu_percent": card.get("cpu_percent"),
                "cpu_text": card.get("cpu_text"),
                "cpu_tone": card.get("cpu_tone"),
                "cpu_cores_text": card.get("cpu_cores_text") or "",
                "cpu_cores_parts": card.get("cpu_cores_parts") or {"num": "", "unit": ""},
                "mem_percent": card.get("mem_percent"),
                "mem_text": card.get("mem_text"),
                "mem_ratio_text": card.get("mem_ratio_text"),
                "mem_parts": card.get("mem_parts") or {"used": "", "total": "", "unit": ""},
                "mem_tone": card.get("mem_tone"),
                "traffic_up": card.get("traffic_up") or {"num": "—", "unit": ""},
                "traffic_down": card.get("traffic_down") or {"num": "—", "unit": ""},
                "rate_up_parts": card.get("rate_up_parts") or {"num": "—", "unit": ""},
                "rate_down_parts": card.get("rate_down_parts") or {"num": "—", "unit": ""},
            }
        )
    return {
        "live": {
            # Bytes per second summed over the nodes, for the live chart; None when unknown.
            "rate_up": live.get("rate_up") if isinstance(live.get("rate_up"), int) else None,
            "rate_down": live.get("rate_down") if isinstance(live.get("rate_down"), int) else None,
            "rate_up_num": live.get("rate_up_num") or "—",
            "rate_up_unit": live.get("rate_up_unit") or "",
            "rate_down_num": live.get("rate_down_num") or "—",
            "rate_down_unit": live.get("rate_down_unit") or "",
        },
        "nodes": nodes_out,
    }
