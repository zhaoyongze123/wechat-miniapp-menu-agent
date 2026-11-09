from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from menu_capture.model_usage import UsageLedger, usage_context
from menu_capture.store import TaskStore


def ledger(tmp_path):
    return UsageLedger(TaskStore(tmp_path / "tasks.sqlite3"))


def finish(usage, response, *, status="success", http_status=200):
    usage.finish_call(response["id"], status=status, elapsed_ms=12, http_status=http_status,
                      response=response.get("response"))


def test_openai_compatible_usage_and_sensitive_payload_are_not_stored(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    usage = UsageLedger(store)
    cid = usage.start_call(provider="openai_compatible", model="m", endpoint="https://u:p@example.test/v1/chat?api_key=secret")
    usage.finish_call(cid, status="success", elapsed_ms=12, http_status=200,
                      response={"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14,
                                        "prompt_tokens_details":{"cached_tokens":3},
                                        "completion_tokens_details":{"reasoning_tokens":2},"image_tokens":5},
                               "choices":[{"message":{"content":"DO NOT SAVE THIS"}}]})
    result = usage.query()
    call = result["calls"][0]
    assert (call["input_tokens"], call["output_tokens"], call["total_tokens"]) == (10, 4, 14)
    assert call["cached_tokens"] == 3 and call["reasoning_tokens"] == 2 and call["image_tokens"] == 5
    assert "secret" not in str(call) and "DO NOT SAVE" not in str(call)
    with store.connect() as db:
        stored = dict(db.execute("SELECT * FROM model_calls").fetchone())
    assert stored["endpoint"] == "https://example.test/v1/chat"


def test_anthropic_cache_and_missing_partial_zero_and_invalid_values(tmp_path):
    usage = ledger(tmp_path)
    def record(response):
        cid = usage.start_call(provider="anthropic", model="claude")
        usage.finish_call(cid, status="success", elapsed_ms=1, http_status=200, response=response)
    record({"usage":{"input_tokens":4,"cache_read_input_tokens":2,"cache_creation_input_tokens":3,"output_tokens":5}})
    record({"usage":{"input_tokens":1,"cache_read_input_tokens":-1,"output_tokens":2}})
    record({"usage":{"cache_read_input_tokens":2,"output_tokens":3}})
    record({"content":"no usage"})
    calls=usage.query()["calls"]
    assert calls[3]["usage_state"] == "complete"
    assert (calls[3]["input_tokens"], calls[3]["total_tokens"], calls[3]["cached_tokens"], calls[3]["cache_write_tokens"]) == (9,14,2,3)
    assert calls[2]["usage_state"] == "partial" and calls[2]["input_tokens"] is None
    assert calls[1]["usage_state"] == "partial" and calls[1]["input_tokens"] is None and calls[1]["cached_tokens"] == 2
    assert calls[0]["usage_state"] == "missing"
    assert usage.query()["summary"]["input_tokens"] == 9


def test_openai_true_zero_generic_cache_subitems_and_partial_usage(tmp_path):
    usage=ledger(tmp_path)
    def add(provider, payload):
        cid=usage.start_call(provider=provider,model="m")
        usage.finish_call(cid,status="success",elapsed_ms=1,http_status=200,response={"usage":payload})
    add("openai_compatible",{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0,
                              "prompt_tokens_details":{"cached_tokens":0,"image_tokens":0},
                              "completion_tokens_details":{"reasoning_tokens":0}})
    add("generic",{"input_tokens":10,"output_tokens":5,"total_tokens":15,
                    "input_tokens_details":{"cached_tokens":4,"image_tokens":2},
                    "output_tokens_details":{"reasoning_tokens":3}})
    add("generic",{"output_tokens":7})
    calls=usage.query()["calls"]
    assert calls[2]["usage_state"]=="complete" and calls[2]["input_tokens"]==0 and calls[2]["cached_tokens"]==0
    assert calls[1]["input_tokens"]==10 and calls[1]["total_tokens"]==15 and calls[1]["cached_tokens"]==4
    assert calls[1]["image_tokens"]==2 and calls[1]["reasoning_tokens"]==3
    assert calls[0]["usage_state"]=="partial" and calls[0]["input_tokens"] is None
    query=usage.query(group_by="model")
    assert query["summary"]["input_tokens_known_calls"]==2
    assert query["summary"]["output_tokens_known_calls"]==3
    assert sum(group["input_tokens_known_calls"] for group in query["groups"])==2


def test_http_failure_no_usage_is_error_and_duplicate_finish_is_idempotent(tmp_path):
    usage=ledger(tmp_path)
    cid=usage.start_call(provider="qwen",model="qwen-vl",context={"source":"connection_test"})
    usage.finish_call(cid,status="success",elapsed_ms=3,http_status=503,response={"usage":{"prompt_tokens":100}})
    usage.finish_call(cid,status="error",elapsed_ms=9,http_status=503,response={"usage":{"prompt_tokens":999}})
    cid2=usage.start_call(provider="qwen",model="qwen-vl")
    usage.finish_call(cid2,status="error",elapsed_ms=2,http_status=None,response=None,error_type="Timeout")
    result=usage.query()
    assert result["summary"]["error_calls"] == 2
    assert result["summary"]["input_tokens"] == 100
    assert result["calls"][1]["http_status"] == 503
    assert result["calls"][0]["usage_state"] == "missing"


def test_context_nested_restore_and_context_isolation(tmp_path):
    usage=ledger(tmp_path)
    with usage_context(run_id="r1",task_id="t1",source="capture"):
        with usage_context(stage="upload", action_id=123):
            cid=usage.start_call(provider="p",model="m")
        # lexical context exited, only outer context applies
        cid2=usage.start_call(provider="p",model="m")
    with usage_context(run_id="r2"):
        cid3=usage.start_call(provider="p",model="m")
    rows={r["call_id"]:r for r in usage.query()["calls"]}
    assert rows[cid]["run_id"]=="r1" and rows[cid]["stage"]=="upload" and rows[cid]["action_id"]==123
    assert rows[cid2]["run_id"]=="r1" and rows[cid2]["stage"]==""
    assert rows[cid3]["run_id"]=="r2" and rows[cid3]["task_id"]==""


def test_query_date_groups_filters_and_pagination(tmp_path):
    store=TaskStore(tmp_path / "tasks.sqlite3")
    usage=UsageLedger(store)
    with store.connect() as db:
        db.execute("INSERT INTO runs(id,mode,status,task_ids,created_at,updated_at) VALUES('r1','batch','running','[]','2026-01-01T00:00:00+00:00','x')")
    ids=[]
    for i in range(3):
        with usage_context(run_id="r1",task_id=f"t{i%2}",source_name=f"Shop {i%2}",source="capture"):
            cid=usage.start_call(provider="p",model="m" if i<2 else "m2")
        ids.append(cid)
        usage.finish_call(cid,status="success",elapsed_ms=10,http_status=200,
                          response={"usage":{"prompt_tokens":1,"completion_tokens":2}})
    today=datetime.now().astimezone().date().isoformat()
    result=usage.query(date_from=today,date_to=today,group_by="day",page=2,page_size=2)
    assert result["summary"]["calls"] == 3 and len(result["calls"]) == 1
    assert result["pagination"] == {"page":2,"page_size":2,"total":3,"pages":2}
    assert result["groups"][0]["key"] == today
    assert result["filters"]["runs"][0]["run_id"] == "r1"
    assert len(result["filters"]["models"]) == 2
    task=usage.query(group_by="task",run_id="r1",task_id="t0")
    assert task["summary"]["calls"] == 2 and task["groups"][0]["label"] == "Shop 0"
    assert task["groups"][0]["task_id"] == "t0" and task["groups"][0]["run_id"] == "r1"
    assert usage.query(source="connection_test")["summary"]["calls"] == 0
    assert usage.query(page=99,page_size=2)["pagination"]["page"] == 2
    with pytest.raises(ValueError): usage.query(group_by="bad")
    with pytest.raises(ValueError): usage.query(source="bad")


def test_start_time_is_utc_and_local_day_and_restart_interrupts(tmp_path):
    store=TaskStore(tmp_path / "tasks.sqlite3")
    usage=UsageLedger(store)
    cid=usage.start_call(provider="p",model="m")
    row=usage.query()["calls"][0]
    assert datetime.fromisoformat(row["started_at"]).utcoffset() == timedelta(0)
    assert row["local_day"] == datetime.now().astimezone().date().isoformat()
    usage=UsageLedger(store)
    call=usage.query()["calls"][0]
    assert call["status"] == "interrupted" and call["usage_state"] == "missing"
    assert usage.query()["summary"]["interrupted_calls"] == 1


def test_sql_query_limits_detail_rows_and_summarizes_task_across_runs(tmp_path):
    store=TaskStore(tmp_path / "tasks.sqlite3")
    usage=UsageLedger(store)
    calls=[]
    original_connect=store.connect
    def traced_connect():
        db=original_connect()
        db.set_trace_callback(calls.append)
        return db
    store.connect=traced_connect
    for run in ("r1","r2"):
        cid=usage.start_call(provider="p",model="m",context={"run_id":run,"task_id":"same","source_name":"Same Shop"})
        usage.finish_call(cid,status="success",elapsed_ms=1,http_status=200,response={"usage":{"prompt_tokens":1,"completion_tokens":1},"id":"req.1","model":"returned-v1"})
    result=usage.query(group_by="task",page=1,page_size=1)
    first_detail=[sql.lower() for sql in calls if "select * from model_calls" in sql.lower()][-1]
    assert result["groups_total"] == 1 and len(result["groups"]) == 1
    assert result["groups"][0]["calls"] == 2 and result["groups"][0]["run_id"] == ""
    assert usage.query(group_by="task",run_id="r1")["groups"][0]["calls"] == 1
    for _ in range(2):
        cid=usage.start_call(provider="p",model="m",context={"source":"connection_test"})
        usage.finish_call(cid,status="error",elapsed_ms=1,http_status=None)
    uncorrelated=usage.query(group_by="task",source="connection_test")
    assert uncorrelated["groups_total"]==1
    assert uncorrelated["groups"][0]["label"]=="无门店用途" and uncorrelated["groups"][0]["calls"]==2
    detail_queries=[sql.lower() for sql in calls if "select * from model_calls" in sql.lower()]
    assert detail_queries and "limit 1 offset 0" in first_detail
    assert result["calls"][0]["finished_at"] and result["calls"][0]["returned_model"] == "returned-v1"
    assert result["calls"][0]["request_id"] == "req.1"


def test_context_none_numeric_bounds_and_response_identifiers_are_sanitized(tmp_path):
    usage=ledger(tmp_path)
    cid=usage.start_call(provider="p",model="m",context={"run_id":None,"action_id":7})
    usage.finish_call(cid,status="success",elapsed_ms=1,http_status=200,
                      response={"id":"req-1","model":"m-latest","usage":{"prompt_tokens":0,"completion_tokens":0}})
    row=usage.query()["calls"][0]
    assert row["run_id"]=="" and row["action_id"]==7
    assert row["request_id"]=="req-1" and row["returned_model"]=="m-latest"
    with pytest.raises(ValueError):
        usage.start_call(provider="p",model="m",context={"step":2**63})



def test_anthropic_empty_usage_does_not_fabricate_zero_cache(tmp_path):
    usage = ledger(tmp_path)
    cid = usage.start_call(provider='anthropic', model='example')
    usage.finish_call(cid, status='success', elapsed_ms=1, http_status=200, response={'usage':{}})
    row = usage.query()['calls'][0]
    assert row['usage_state'] == 'missing'
    assert row['cached_tokens'] is None and row['cache_write_tokens'] is None


def test_openai_output_only_usage_is_preserved_as_partial(tmp_path):
    usage = ledger(tmp_path)
    cid = usage.start_call(provider='openai_compatible', model='example')
    usage.finish_call(cid, status='success', elapsed_ms=1, http_status=200,
                      response={'usage':{'completion_tokens':8,'completion_tokens_details':{'reasoning_tokens':3}}})
    row = usage.query()['calls'][0]
    assert row['usage_state'] == 'partial' and row['output_tokens'] == 8
    assert row['input_tokens'] is None and row['total_tokens'] is None
    assert row['reasoning_tokens'] == 3
