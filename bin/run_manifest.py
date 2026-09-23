# -*- coding: utf-8 -*-
"""不可变运行清单的读写与哈希工具。"""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone


SCHEMA_VERSION = 1


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read(path):
    if not os.path.isfile(path):
        return {"schemaVersion": SCHEMA_VERSION, "invocations": []}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("run_manifest.json 格式无效或版本不受支持")
    data.setdefault("invocations", [])
    return data


def _write_atomic(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".run-manifest-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def relative_path(path, base_dir):
    if not path:
        return None
    absolute = os.path.abspath(path)
    base = os.path.abspath(base_dir)
    try:
        if os.path.commonpath([base, absolute]) == base:
            return os.path.relpath(absolute, base).replace("\\", "/")
    except ValueError:
        pass
    return absolute


def file_snapshot(path, base_dir=None):
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": relative_path(path, base_dir) if base_dir else os.path.abspath(path),
        "sha256": digest.hexdigest(),
    }


def value_snapshot(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return {"sha256": hashlib.sha256(payload).hexdigest()}


def record_execution(manifest_path, record):
    if not isinstance(record, dict) or not record.get("id"):
        raise ValueError("运行清单记录必须包含 id")
    data = _read(manifest_path)
    incoming = dict(record)
    rows = data["invocations"]
    for index, existing in enumerate(rows):
        if existing.get("id") == incoming["id"]:
            incoming.setdefault("createdAt", existing.get("createdAt") or _now())
            rows[index] = incoming
            break
    else:
        incoming.setdefault("createdAt", _now())
        rows.append(incoming)
    _write_atomic(manifest_path, data)
    return incoming


def _canonical_ref(path, run_root):
    return os.path.normcase(os.path.abspath(
        path if os.path.isabs(path) else os.path.join(run_root, path)))


def _manifest_ref(path, run_root):
    if not path:
        return None
    if os.path.isabs(path):
        return relative_path(path, run_root)
    return os.path.normpath(path).replace("\\", "/")


def record_scoring(manifest_path, trajectory_dir, score_dir, scorer_name,
                   score_mode, scorer_version=None, run_id=None,
                   comparison_method=None, judge_calls=None):
    data = _read(manifest_path)
    run_root = os.path.dirname(os.path.abspath(manifest_path))
    trajectory_ref = _manifest_ref(trajectory_dir, run_root)
    score_ref = _manifest_ref(score_dir, run_root)
    target = _canonical_ref(trajectory_ref, run_root)
    row = None
    for item in data["invocations"]:
        if run_id and item.get("id") == run_id:
            row = item
            break
        current = item.get("trajectoryDir")
        if not run_id and current and _canonical_ref(current, run_root) == target:
            row = item
            break
    if row is None:
        row = {
            "id": run_id or os.path.basename(os.path.normpath(score_dir)),
            "kind": "unknown", "createdAt": _now(),
            "trajectoryDir": trajectory_ref,
        }
        data["invocations"].append(row)
    row.update({
        "scoreDir": score_ref, "scoreMode": score_mode,
        "scoredAt": _now(),
        "scorer": {"name": scorer_name, "version": scorer_version or "unknown"},
    })
    if comparison_method is not None or judge_calls is not None:
        row["scoring"] = {
            "comparisonMethod": comparison_method or "unknown",
            "judgeCalls": int(judge_calls or 0),
        }
    _write_atomic(manifest_path, data)
    return row


def find_run_root(output_path):
    path = os.path.abspath(output_path)
    if os.path.isfile(path):
        path = os.path.dirname(path)
    current = path
    while True:
        if os.path.isfile(os.path.join(current, "run_manifest.json")):
            return current
        parent = os.path.dirname(current)
        if os.path.basename(parent).lower() == "scores":
            return os.path.dirname(parent)
        if os.path.basename(current).lower().startswith("trajectories"):
            return parent
        if parent == current:
            break
        current = parent
    return os.path.dirname(path)
