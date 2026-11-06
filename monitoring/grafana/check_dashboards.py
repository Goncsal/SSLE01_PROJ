#!/usr/bin/env python3
"""Static checks for the Grafana dashboards.

Parses every dashboard JSON, checks unique dashboard and panel ids, that datasource uids
exist in the provisioning file, and that every metric name used in an expression is known.
Exit status 1 on any problem.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "common", "src"))

import yaml  # noqa: E402
from evcommon import metrics  # noqa: E402

CITIES = ("porto", "lisboa", "aveiro")  # values of the `city` dashboard variable
BASELINE = {
    "baseline:city_grid_power_watts:upper", "baseline:city_grid_power_watts:mean",
    "baseline:city_msgs_rate:upper", "baseline:city_msgs_rate:mean",
    "baseline:city_auth_failures_10m:upper",
}
STANDARD_PREFIXES = ("scrape_", "nginx_", "node_", "container_", "prometheus_")
STANDARD_NAMES = {"up", "ALERTS", "ALERTS_FOR_STATE"}
FUNCTIONS = {
    "sum", "min", "max", "avg", "count", "stddev", "stdvar", "topk", "bottomk", "quantile", "group",
    "count_values", "rate", "irate", "increase", "delta", "idelta", "deriv", "histogram_quantile",
    "label_replace", "label_join", "clamp_max", "clamp_min", "clamp", "abs", "ceil", "floor", "round",
    "sqrt", "exp", "ln", "log2", "log10", "absent", "absent_over_time", "scalar", "vector", "time",
    "sort", "sort_desc", "avg_over_time", "sum_over_time", "min_over_time", "max_over_time",
    "count_over_time", "last_over_time", "present_over_time", "quantile_over_time", "predict_linear",
    "changes", "resets", "timestamp", "day_of_week", "hour", "minute", "month", "year",
}
KEYWORDS = {"by", "without", "on", "ignoring", "group_left", "group_right", "bool", "and", "or",
            "unless", "offset", "inf", "nan"}
HIST_SUFFIXES = ("_bucket", "_sum", "_count")

problems: list[str] = []


def known_metrics() -> set[str]:
    names = {s.name for s in metrics.ALL}
    for s in metrics.ALL:
        if s.type == "histogram":
            names.update(s.name + suf for suf in HIST_SUFFIXES)
    names.update(metrics.CITY_RECORDING_RULES)
    names.update(BASELINE)
    names.update(STANDARD_NAMES)
    return names


def metric_names(expr: str) -> set[str]:
    e = re.sub(r'"(?:\\.|[^"\\])*"', '""', expr)          # string literals
    e = re.sub(r"\{[^{}]*\}", "", e)                       # label matchers
    e = re.sub(r"\[[^\]]*\]", "", e)                       # range selectors
    e = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", e)
    e = re.sub(r"\$\{?\w+\}?", " ", e)                     # dashboard variables
    out = set()
    for m in re.finditer(r"[A-Za-z_:][A-Za-z0-9_:]*", e):
        tok = m.group(0)
        if tok in KEYWORDS or tok in FUNCTIONS:
            continue
        if re.match(r"\s*\(", e[m.end():]):                 # unknown function call
            problems_fn = tok
            problems.append(f"unknown function {problems_fn!r} in: {expr}")
            continue
        out.add(tok)
    return out


def ds_uids(provisioned: set[str], ds) -> list[str]:
    if ds is None:
        return []
    uid = ds.get("uid") if isinstance(ds, dict) else ds
    if "${city}" in uid or "$city" in uid:
        return [uid.replace("${city}", c).replace("$city", c) for c in CITIES]
    return [uid]


def main() -> int:
    with open(os.path.join(HERE, "provisioning", "datasources", "evcharge.yml")) as f:
        prov = {d["uid"] for d in yaml.safe_load(f)["datasources"]}
    known = known_metrics()
    seen_uids: dict[str, str] = {}
    files = sorted(glob.glob(os.path.join(HERE, "dashboards", "*.json")))
    if not files:
        problems.append("no dashboards found")
    npanels = nexpr = 0
    for path in files:
        name = os.path.basename(path)
        try:
            with open(path) as f:
                dash = json.load(f)
        except ValueError as exc:
            problems.append(f"{name}: invalid JSON: {exc}")
            continue
        uid = dash.get("uid")
        if not uid:
            problems.append(f"{name}: missing uid")
        elif uid in seen_uids:
            problems.append(f"{name}: uid {uid} also used by {seen_uids[uid]}")
        else:
            seen_uids[uid] = name
        ids: set[int] = set()
        for panel in dash.get("panels", []):
            npanels += 1
            pid = panel.get("id")
            if pid in ids:
                problems.append(f"{name}: duplicate panel id {pid}")
            ids.add(pid)
            where = f"{name} panel {pid} ({panel.get('title')})"
            if not panel.get("title"):
                problems.append(f"{where}: no title")
            if panel.get("type") == "text":
                continue
            for u in ds_uids(prov, panel.get("datasource")):
                if u not in prov:
                    problems.append(f"{where}: unknown datasource {u}")
            for t in panel.get("targets", []):
                for u in ds_uids(prov, t.get("datasource")):
                    if u not in prov:
                        problems.append(f"{where}: unknown datasource {u}")
                expr = t.get("expr", "")
                nexpr += 1
                if not expr:
                    problems.append(f"{where}: empty expression")
                for mname in metric_names(expr):
                    if mname in known or mname.startswith(STANDARD_PREFIXES):
                        continue
                    problems.append(f"{where}: unknown metric {mname}")
    status = "FAIL" if problems else "ok"
    for p in problems:
        print("problem:", p)
    print(f"{status}: {len(files)} dashboards, {npanels} panels, {nexpr} expressions, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
