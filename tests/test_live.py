"""The live adapter is tested against a fake client. No network, no spend.

What is under test is the translation from a Messages response into a
ModelTurn, and that the harness treats a live seat exactly like a scripted
one: it mints the key, gates the schema, and closes on the receipt.
"""

import json
from types import SimpleNamespace

from support_desk.live import LiveSupportModel, TOOLS, summary_row, summary_table
from support_desk.modes import run_agent


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _tool(name, **arguments):
    return SimpleNamespace(type="tool_use", id="tu_1", name=name, input=arguments)


def _response(blocks, stop_reason):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        model="fake-model",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class FakeClient:
    """Replays scripted responses and keeps every request it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        return self._responses.pop(0)


def test_tool_use_becomes_a_proposal_and_the_request_carries_the_strict_tools():
    client = FakeClient([_response([_text("Looking it up."), _tool("search", query="ord_1042")], "tool_use")])
    model = LiveSupportModel(client=client, model="fake-model", effort="low")

    turn = model.respond({"mode": "agent", "ticket": {}, "receipts": [], "ceilings": {}, "steps_taken": 0})

    assert turn.tool_name == "search"
    assert turn.arguments == {"query": "ord_1042"}
    assert turn.text == "Looking it up."
    assert turn.done is False
    request = client.requests[0]
    assert request["model"] == "fake-model"
    assert request["tools"] == TOOLS
    assert all(tool["strict"] for tool in request["tools"])
    assert request["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert request["output_config"] == {"effort": "low"}
    assert json.loads(request["messages"][0]["content"])["steps_taken"] == 0
    assert model.turns[0]["tool_uses"] == [{"name": "search", "input": {"query": "ord_1042"}}]


def test_text_only_reply_ends_the_run():
    client = FakeClient([_response([_text("Done.")], "end_turn")])
    model = LiveSupportModel(client=client)

    turn = model.respond({"steps_taken": 1})

    assert turn.tool_name is None
    assert turn.done is True
    assert turn.text == "Done."


def test_refusal_ends_the_run_without_a_tool_even_if_a_tool_block_is_present():
    client = FakeClient([_response([_tool("refund", order_id="ord_1042", amount_cents=4000)], "refusal")])
    model = LiveSupportModel(client=client)

    turn = model.respond({"steps_taken": 0})

    assert turn.tool_name is None
    assert turn.done is True
    assert model.turns[0]["stop_reason"] == "refusal"


def test_live_seat_in_the_agent_harness_closes_on_the_receipt_not_the_sentence(desk):
    client = FakeClient(
        [
            _response([_tool("search", query="ord_1042")], "tool_use"),
            _response([_tool("refund", order_id="ord_1042", amount_cents=4000, idempotency_key="mine")], "tool_use"),
            _response([_text("Refunded Ada's order.")], "end_turn"),
        ]
    )
    model = LiveSupportModel(client=client)

    log = run_agent(desk, "tkt_100", "support-agent", model, max_steps=6, run_label="live_01")

    assert log["run_id"] == "run_agent_tkt_100_live_01"
    assert [call["tool"] for call in log["tool_calls"]] == ["search", "refund"]
    refund = log["tool_calls"][1]
    assert refund["arguments"]["idempotency_key"] == "refund:tkt_100"
    assert refund["proposed_idempotency_key"] == "mine"
    assert log["final_outcome"]["kind"] == "refunded"
    assert log["final_outcome"]["receipt"]["refund_id"] == "ref_0001"
    assert log["summary"] == "Refunded Ada's order."
    assert desk.refunded_cents("ord_1042") == 4000
    # The second request saw the search receipt the harness recorded.
    second = json.loads(client.requests[1]["messages"][0]["content"])
    assert second["receipts"][0]["result_set_id"] == "rs_0001"

    row = summary_row(1, log, desk)
    assert row["tools"] == "search > refund"
    assert row["proposed_keys"] == 1
    assert row["evidence"] == "ref_0001"
    table = summary_table([row])
    assert "| 01 | 2 | search > refund | 0 | 0 | 1 | refunded | ref_0001 | 4000 |" in table
    assert "Outcomes: refunded 1." in table


def test_live_seat_saying_done_first_closes_incomplete(desk):
    client = FakeClient([_response([_text("Done.")], "end_turn")])
    model = LiveSupportModel(client=client)

    log = run_agent(desk, "tkt_100", "support-agent", model, max_steps=6, run_label="live_02")

    assert log["tool_calls"] == []
    assert log["final_outcome"]["kind"] == "incomplete"
    assert log["final_outcome"]["receipt"] is None
    assert desk.refunded_cents("ord_1042") == 0
    assert summary_row(2, log, desk)["evidence"] == ""
