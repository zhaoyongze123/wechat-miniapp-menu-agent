from __future__ import annotations

import contextvars
import re
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit


_FIELDS = (
    "run_id", "task_id", "source_name", "attempt_id", "action_id", "stage",
    "step", "material_batch_id", "material_version",
)
_ALLOWED_SOURCES = {"capture", "annotation", "connection_test", "material_analysis", "agent", "other"}
_usage_fields = contextvars.ContextVar("model_usage_fields", default={})
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_tokens",
                 "cache_write_tokens", "reasoning_tokens", "image_tokens")
_SQLITE_MAX = 2**63 - 1


@contextmanager
def usage_context(**fields):
    """为当前异步/线程上下文补充请求归属；嵌套上下文退出后恢复原值。"""
    unknown = set(fields) - set(_FIELDS) - {"source"}
    if unknown:
        raise ValueError(f"不支持的 usage context 字段: {', '.join(sorted(unknown))}")
    merged = dict(_usage_fields.get())
    merged.update(fields)
    token = _usage_fields.set(merged)
    try:
        yield
    finally:
        _usage_fields.reset(token)


def _safe_endpoint(endpoint: str) -> str:
    try:
        parts = urlsplit(str(endpoint))
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except (ValueError, TypeError):
        return ""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > _SQLITE_MAX or (isinstance(value, float) and (value != value or value in (float("inf"), float("-inf")))):
        return None
    if int(value) != value:
        return None
    return int(value)


def _get(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _extract_usage(response, provider=""):
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return {key: None for key in _TOKEN_FIELDS}
    # Provider 缓存/推理 token 是分项；只有 Anthropic 的缓存输入可参与 input 总量。
    if provider.lower() == "anthropic":
        inp = _number(usage.get("input_tokens"))
        read_raw = usage.get("cache_read_input_tokens", 0)
        write_raw = usage.get("cache_creation_input_tokens", 0)
        read = _number(read_raw); write = _number(write_raw)
        cache_valid = read is not None and write is not None
        total_in = inp + read + write if inp is not None and cache_valid and inp + read + write <= _SQLITE_MAX else None
        out = _number(usage.get("output_tokens"))
        total = _number(usage.get("total_tokens"))
        if total is None and total_in is not None and out is not None and total_in + out <= _SQLITE_MAX:
            total = total_in + out
        reasoning = _number(_get(usage, "output_tokens_details", "reasoning_tokens"))
        if reasoning is None: reasoning = _number(usage.get("reasoning_tokens"))
        return dict(input_tokens=total_in, output_tokens=out, total_tokens=total,
                    cached_tokens=read if "cache_read_input_tokens" in usage else None,
                    cache_write_tokens=write if "cache_creation_input_tokens" in usage else None,
                    reasoning_tokens=reasoning,
                    image_tokens=_number(_get(usage, "input_tokens_details", "image_tokens")) if "input_tokens" in usage else _number(usage.get("image_tokens")))
    if any(key in usage for key in ("prompt_tokens", "completion_tokens", "prompt_tokens_details", "completion_tokens_details")):
        inp = _number(usage.get("prompt_tokens")); out = _number(usage.get("completion_tokens"))
        cached = _number(_get(usage, "prompt_tokens_details", "cached_tokens"))
        image = _number(_get(usage, "prompt_tokens_details", "image_tokens"))
        reasoning = _number(_get(usage, "completion_tokens_details", "reasoning_tokens"))
    else:
        inp = _number(usage.get("input_tokens")); out = _number(usage.get("output_tokens"))
        cached = _number(_get(usage, "input_tokens_details", "cached_tokens"))
        image = _number(_get(usage, "input_tokens_details", "image_tokens"))
        reasoning = _number(_get(usage, "output_tokens_details", "reasoning_tokens"))
    total = _number(usage.get("total_tokens"))
    if total is None and inp is not None and out is not None and inp + out <= _SQLITE_MAX:
        total = inp + out
    return dict(input_tokens=inp, output_tokens=out, total_tokens=total,
                cached_tokens=cached, cache_write_tokens=None, reasoning_tokens=reasoning,
                image_tokens=image if image is not None else _number(usage.get("image_tokens")))


def _usage_state(values):
    if all(values.get(k) is not None for k in ("input_tokens", "output_tokens", "total_tokens")):
        return "complete"
    if any(values.get(k) is not None for k in _TOKEN_FIELDS):
        return "partial"
    return "missing"


def _parse_date(value, default):
    if value is None:
        return default
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期必须使用 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("日期必须使用 YYYY-MM-DD") from exc


class UsageLedger:
    def __init__(self, task_store):
        self.store = task_store
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS model_calls (
                call_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, local_day TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, endpoint TEXT NOT NULL,
                status TEXT NOT NULL, finished_at TEXT, returned_model TEXT NOT NULL DEFAULT '', elapsed_ms INTEGER, http_status INTEGER,
                request_id TEXT NOT NULL DEFAULT '', error_type TEXT NOT NULL DEFAULT '',
                usage_state TEXT NOT NULL DEFAULT 'missing', input_tokens INTEGER, output_tokens INTEGER,
                total_tokens INTEGER, cached_tokens INTEGER, cache_write_tokens INTEGER,
                reasoning_tokens INTEGER, image_tokens INTEGER,
                run_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'other', source_name TEXT NOT NULL DEFAULT '',
                attempt_id TEXT NOT NULL DEFAULT '', action_id,
                stage TEXT NOT NULL DEFAULT '', step TEXT, material_batch_id TEXT NOT NULL DEFAULT '',
                material_version TEXT NOT NULL DEFAULT ''
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS model_calls_day ON model_calls(local_day,call_id)")
            db.execute("CREATE INDEX IF NOT EXISTS model_calls_run ON model_calls(run_id,call_id)")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(model_calls)")}
            if "finished_at" not in columns: db.execute("ALTER TABLE model_calls ADD COLUMN finished_at TEXT")
            if "returned_model" not in columns: db.execute("ALTER TABLE model_calls ADD COLUMN returned_model TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE model_calls SET status='interrupted' WHERE status='running'")

    def start_call(self, *, provider, model, endpoint="", context=None, started_at=None, call_id=""):
        # 外部可按来源给稳定 call_id；重复导入同一轨迹时不再产生第二条账单。
        call_id = call_id if isinstance(call_id, str) and re.fullmatch(r"[0-9a-f]{32}", call_id) else uuid.uuid4().hex
        local_now = datetime.now().astimezone()
        if isinstance(started_at, str) and started_at.strip():
            try:
                parsed = datetime.fromisoformat(started_at.strip().replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None:
                local_now = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone()
        stamp = local_now.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        local_day = local_now.date().isoformat()
        fields = dict(_usage_fields.get())
        if context:
            fields.update({k: v for k, v in context.items() if k in _FIELDS or k == "source"})
        source = fields.pop("source", "other")
        if source not in _ALLOWED_SOURCES:
            source = "other"
        values = {}
        for key in _FIELDS:
            value = fields.get(key, "")
            if value is None: value = ""
            if key in {"step", "action_id"} and value != "":
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _SQLITE_MAX:
                    raise ValueError(f"{key} 必须为 SQLite 范围内的非负整数")
            elif not isinstance(value, str):
                raise ValueError(f"{key} 必须为字符串或 None")
            values[key] = value
        if values["step"] == "":
            values["step"] = None
        cols = ["call_id", "started_at", "local_day", "provider", "model", "endpoint", "status", "source", *_FIELDS]
        with self.store.connect() as db:
            cursor = db.execute(f"INSERT OR IGNORE INTO model_calls ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                                [call_id, stamp, local_day, str(provider), str(model), _safe_endpoint(endpoint), "running", source,
                                 *[values[k] for k in _FIELDS]])
            if not cursor.rowcount:
                return ""
        return call_id

    def finish_call(self, call_id, *, status, elapsed_ms, http_status=None, response=None, request_id="", error_type="") -> None:
        status = "success" if status == "success" and isinstance(http_status, int) and not isinstance(http_status, bool) and 200 <= http_status < 300 else "error"
        values = _extract_usage(response, self._provider_for(call_id))
        state = _usage_state(values)
        elapsed = _number(elapsed_ms)
        http = _number(http_status)
        request_id = request_id or (response.get("id", "") if isinstance(response, dict) else "")
        returned_model = response.get("model", "") if isinstance(response, dict) else ""
        request_id = self._safe_identifier(request_id)
        returned_model = self._safe_identifier(returned_model)
        error_type = str(error_type or "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type): error_type = ""
        finished_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self.store.connect() as db:
            db.execute("""UPDATE model_calls SET status=?,finished_at=?,returned_model=?,elapsed_ms=?,http_status=?,request_id=?,error_type=?,usage_state=?,
                input_tokens=?,output_tokens=?,total_tokens=?,cached_tokens=?,cache_write_tokens=?,reasoning_tokens=?,image_tokens=?
                WHERE call_id=? AND status='running'""",
                (status, finished_at, returned_model, elapsed, http, request_id, error_type, state,
                 *[values[k] for k in _TOKEN_FIELDS], call_id))

    def _provider_for(self, call_id):
        with self.store.connect() as db:
            row = db.execute("SELECT provider FROM model_calls WHERE call_id=? AND status='running'", (call_id,)).fetchone()
        return row["provider"] if row else ""

    @staticmethod
    def _safe_identifier(value):
        if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:/-]*", value):
            return ""
        return value

    def query(self, *, date_from=None, date_to=None, group_by="day", source="all", run_id="", task_id="",
              provider="", model="", page=1, page_size=30) -> dict:
        if group_by not in {"day", "run", "task", "model"}: raise ValueError("group_by 无效")
        if source not in {"all", *_ALLOWED_SOURCES}: raise ValueError("source 无效")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1: raise ValueError("page 无效")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 200: raise ValueError("page_size 无效")
        today = datetime.now().astimezone().date()
        start = _parse_date(date_from, today - timedelta(days=6)); end = _parse_date(date_to, today)
        if start > end: raise ValueError("date_from 不能晚于 date_to")
        where = ["local_day>=?", "local_day<=?"]; params = [start.isoformat(), end.isoformat()]
        for col, val in (("source", source if source != "all" else ""), ("run_id", run_id), ("task_id", task_id), ("provider", provider), ("model", model)):
            if val: where.append(f"{col}=?"); params.append(val)
        clause = " AND ".join(where)
        with self.store.connect() as db:
            db.execute("BEGIN")
            count = db.execute(f"SELECT COUNT(*) FROM model_calls WHERE {clause}", params).fetchone()[0]
            pages = (count + page_size - 1) // page_size
            page = min(page, max(1, pages))
            summary = dict(db.execute(f"SELECT {self._aggregate_sql()} FROM model_calls WHERE {clause}", params).fetchone())
            calls = [dict(r) for r in db.execute(
                f"SELECT * FROM model_calls WHERE {clause} ORDER BY started_at DESC,call_id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page-1)*page_size])]
            if group_by == "day":
                select = "local_day AS key, local_day AS label"
                group_expr = "local_day"
            elif group_by == "run":
                select = "run_id AS key, CASE WHEN run_id='' THEN '非队列调用' ELSE run_id END AS label, run_id"
                group_expr = "run_id"
            elif group_by == "task":
                select = "CASE WHEN task_id='' THEN 'uncorrelated:'||source ELSE task_id END AS key, CASE WHEN task_id='' THEN '无门店用途' ELSE COALESCE(MAX(NULLIF(source_name,'')),task_id) END AS label, task_id,MAX(source_name) AS source_name"
                if run_id: select += ",? AS run_id"
                else: select += ",'' AS run_id"
                group_expr = "CASE WHEN task_id='' THEN source ELSE task_id END,task_id"
            else:
                select = "provider||'/'||model AS key, provider||'/'||model AS label, provider,model"
                group_expr = "provider,model"
            groups_total = db.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM model_calls WHERE {clause} GROUP BY {group_expr})", params).fetchone()[0]
            group_order = "local_day DESC" if group_by == "day" else "MAX(started_at) DESC, key DESC"
            select_params = [run_id] if group_by == "task" and run_id else []
            groups = [dict(r) for r in db.execute(
                f"SELECT {select},{self._aggregate_sql()},MAX(started_at) AS latest FROM model_calls WHERE {clause} GROUP BY {group_expr} ORDER BY {group_order} LIMIT 200", [*select_params,*params])]
            groups = [{k:v for k,v in row.items() if k != "latest"} for row in groups]
            filter_where = ["local_day>=?", "local_day<=?"]
            filter_params = [start.isoformat(), end.isoformat()]
            if source != "all": filter_where.append("source=?"); filter_params.append(source)
            filter_clause = " AND ".join(filter_where)
            runs=[dict(r) for r in db.execute("SELECT DISTINCT m.run_id, COALESCE(r.created_at,'') created_at FROM model_calls m LEFT JOIN runs r ON r.id=m.run_id WHERE m.run_id!='' AND " + filter_clause, filter_params)]
            models=[dict(r) for r in db.execute(f"SELECT DISTINCT provider,model FROM model_calls WHERE {filter_clause} ORDER BY provider,model", filter_params)]
        for run in runs:
            run["label"] = f"{run['created_at'][:16].replace('T',' ')} · {run['run_id'][:8]}" if run["created_at"] else run["run_id"]
        run_labels = {run["run_id"]: run["label"] for run in runs}
        if group_by == "run":
            for group in groups:
                if group["run_id"]:
                    group["label"] = run_labels.get(group["run_id"], group["run_id"])
        return {"summary":summary,"groups":groups,"groups_total":groups_total,"calls":calls,"pagination":{"page":page,"page_size":page_size,"total":count,"pages":pages},
                "filters":{"runs":runs,"models":models},"timezone":str(datetime.now().astimezone().tzinfo)}

    @staticmethod
    def _aggregate_sql():
        bits = ["COUNT(*) AS calls"]
        bits.extend(f"COALESCE(SUM(CASE WHEN status='{status}' THEN 1 ELSE 0 END),0) AS {key}" for status,key in (
            ("success","success_calls"),("error","error_calls"),("running","running_calls"),("interrupted","interrupted_calls")))
        bits.extend(f"COALESCE(SUM(CASE WHEN usage_state='{state}' THEN 1 ELSE 0 END),0) AS {key}" for state,key in (
            ("complete","known_usage_calls"),("partial","partial_usage_calls"),("missing","unknown_usage_calls")))
        bits.extend(f"COALESCE(SUM({key}),0) AS {key}" for key in _TOKEN_FIELDS + ("elapsed_ms",))
        bits.extend(f"COUNT({key}) AS {key}_known_calls" for key in _TOKEN_FIELDS)
        return ",".join(bits)
