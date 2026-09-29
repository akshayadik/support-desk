import json
import sqlite3

from support_desk.cli import main
from support_desk.desk import Desk
from support_desk.model import ModelTurn
from support_desk.modes import run_agent, run_side_by_side, run_single_call, run_workflow
from support_desk.scripted import ScriptedSingleCallModel, ScriptedSummarizer, ScriptedSupportModel

from conftest import NOW, ROOT, SEED


class GreedyRefundModel:
    def respond(self, observation):
        ticket = observation["ticket"]
        return ModelTurn(
            tool_name="refund",
            arguments={
                "order_id": ticket["order_id"],
                "amount_cents": ticket["requested_amount_cents"],
                "idempotency_key": f"refund:{ticket['id']}",
            },
            text="Refund it, then keep going.",
            done=False,
        )


class Liar:
    def respond(self, observation):
        return ModelTurn(
            tool_name="escalate",
            arguments={"reason": "trust me", "idempotency_key": "escalate:lie"},
            text="Escalated successfully. No refund happened.",
            done=True,
        )


class UnknownToolModel:
    def respond(self, observation):
        if observation["steps_taken"] == 0:
            return ModelTurn("drop_table", {"sql": "DROP TABLE refunds"}, "", False)
        return ModelTurn(None, {}, "stopped", True)


def test_single_call_executes_one_tool_and_stops(desk):
    model = GreedyRefundModel()
    log = run_single_call(desk, ticket_id="tkt_100", actor="support-agent", model=model)

    assert log["mode"] == "single_call"
    assert log["ceilings"]["max_steps"] == 1
    assert len(log["tool_calls"]) == 1
    assert log["tool_calls"][0]["tool"] == "refund"
    assert log["final_outcome"]["kind"] == "refunded"
    assert log["final_outcome"]["receipt"]["refund_id"] == "ref_0001"
    assert desk.refunded_cents("ord_1042") == 4000


def test_workflow_follows_fixed_steps_and_ignores_a_lying_summary(desk):
    log = run_workflow(desk, ticket_id="tkt_100", actor="support-agent", model=Liar())

    assert [call["tool"] for call in log["tool_calls"]] == ["search", "refund"]
    assert log["final_outcome"]["kind"] == "refunded"
    assert log["final_outcome"]["receipt"]["refund_id"] == "ref_0001"
    assert log["summary"] == "Escalated successfully. No refund happened."
    assert desk.handoff_count("tkt_100") == 0
    assert desk.refunded_cents("ord_1042") == 4000


def test_workflow_escalates_when_request_exceeds_authority(desk):
    log = run_workflow(desk, ticket_id="tkt_low", actor="support-agent", model=ScriptedSummarizer())

    assert [call["tool"] for call in log["tool_calls"]] == ["search", "escalate"]
    assert log["final_outcome"]["kind"] == "escalated"
    assert log["final_outcome"]["receipt"]["handoff_ticket_id"] == "hof_0001"
    assert log["tool_calls"][1]["arguments"]["reason"] == "exceeds authority ceiling"
    assert desk.refunded_cents("ord_1042") == 0


def test_workflow_escalates_when_request_exceeds_amount_paid(desk):
    log = run_workflow(desk, ticket_id="tkt_overpay", actor="support-agent", model=ScriptedSummarizer())

    assert [call["tool"] for call in log["tool_calls"]] == ["search", "escalate"]
    assert log["tool_calls"][1]["arguments"]["reason"] == "exceeds amount paid"
    assert desk.refunded_cents("ord_1042") == 0


def test_agent_searches_then_refunds_under_the_step_ceiling(desk):
    log = run_agent(
        desk,
        ticket_id="tkt_100",
        actor="support-agent",
        model=ScriptedSupportModel(),
        max_steps=6,
    )

    assert [call["tool"] for call in log["tool_calls"]] == ["search", "refund"]
    assert log["ceilings"] == {"authority_cents": 5000, "max_steps": 6}
    assert log["final_outcome"]["kind"] == "refunded"
    assert log["actor"] == "support-agent"
    assert log["ticket_id"] == "tkt_100"


def test_agent_records_an_unknown_tool_and_does_not_run_it(desk):
    log = run_agent(
        desk,
        ticket_id="tkt_100",
        actor="support-agent",
        model=UnknownToolModel(),
        max_steps=6,
    )

    assert log["tool_calls"][0]["tool"] == "drop_table"
    assert log["tool_calls"][0]["receipt"]["rejection_reason"] == "unknown tool"
    assert log["final_outcome"]["kind"] == "incomplete"
    desk.search(ticket_id="tkt_100", actor="support-agent", query="ord_1042")
    assert desk.refunded_cents("ord_1042") == 0


def test_agent_stops_at_declared_step_ceiling(desk):
    log = run_agent(
        desk,
        ticket_id="tkt_100",
        actor="support-agent",
        model=GreedyRefundModel(),
        max_steps=1,
    )

    assert len(log["tool_calls"]) == 1
    assert log["final_outcome"]["kind"] == "refunded"


class LoopSearchModel:
    def respond(self, observation):
        return ModelTurn("search", {"query": "ord_1042"}, "", False)


def test_agent_incomplete_when_ceiling_hits_before_a_closing_receipt(desk):
    log = run_agent(
        desk,
        ticket_id="tkt_100",
        actor="support-agent",
        model=LoopSearchModel(),
        max_steps=2,
    )

    assert len(log["tool_calls"]) == 2
    assert log["final_outcome"]["kind"] == "incomplete"
    assert desk.refunded_cents("ord_1042") == 0


def test_side_by_side_runs_share_the_task_and_isolate_the_ledger(tmp_path):
    logs = run_side_by_side(
        SEED,
        ticket_id="tkt_100",
        actor="support-agent",
        db_dir=tmp_path,
        clock=lambda: NOW,
    )

    assert [log["mode"] for log in logs] == ["single_call", "workflow", "agent"]
    assert {log["final_outcome"]["kind"] for log in logs} == {"refunded"}
    for name in ("single_call", "workflow", "agent"):
        conn = sqlite3.connect(tmp_path / f"tkt_100_{name}.sqlite")
        total = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM refunds").fetchone()[0]
        assert total == 4000


def test_scripted_single_call_and_summary_are_deterministic(desk):
    single = run_single_call(
        desk, ticket_id="tkt_100", actor="support-agent", model=ScriptedSingleCallModel()
    )
    assert single["tool_calls"][0]["tool"] == "refund"
    assert single["summary"] == "Refund the order."
    assert single["final_outcome"]["receipt"]["refund_id"] == "ref_0001"

    other = Desk.create(
        desk.path.parent / "summary.sqlite",
        json.loads(json.dumps(SEED)),
        clock=lambda: NOW,
    )
    workflow = run_workflow(
        other, ticket_id="tkt_100", actor="support-agent", model=ScriptedSummarizer()
    )
    assert "ref_0001" in workflow["summary"]
    assert workflow["final_outcome"]["kind"] == "refunded"


def test_cli_writes_a_citable_log_per_mode(tmp_path):
    out = tmp_path / "runs"
    code = main(
        [
            "--seed",
            str(ROOT / "data" / "seed.json"),
            "--out",
            str(out),
            "--ticket",
            "tkt_100",
            "--now",
            NOW,
            "--mode",
            "all",
        ]
    )

    assert code == 0
    for mode, tools in (
        ("single_call", ["refund"]),
        ("workflow", ["search", "refund"]),
        ("agent", ["search", "refund"]),
    ):
        log = json.loads((out / f"tkt_100_{mode}.json").read_text())
        assert log["run_id"] == f"run_{mode}_tkt_100"
        assert log["started_at"] == NOW
        assert log["ticket_id"] == "tkt_100"
        assert log["actor"] == "support-agent"
        assert [call["tool"] for call in log["tool_calls"]] == tools
        assert log["final_outcome"]["kind"] == "refunded"
        assert log["final_outcome"]["receipt"]["refund_id"]
        conn = sqlite3.connect(out / f"tkt_100_{mode}.sqlite")
        total = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM refunds").fetchone()[0]
        assert total == 4000


def test_cli_rerun_replaces_the_previous_ledger(tmp_path):
    out = tmp_path / "runs"
    args = [
        "--seed",
        str(ROOT / "data" / "seed.json"),
        "--out",
        str(out),
        "--ticket",
        "tkt_100",
        "--now",
        NOW,
        "--mode",
        "all",
    ]

    assert main(args) == 0
    assert main(args) == 0
    for mode in ("single_call", "workflow", "agent"):
        conn = sqlite3.connect(out / f"tkt_100_{mode}.sqlite")
        total = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM refunds").fetchone()[0]
        conn.close()
        assert total == 4000
