import json
from dataclasses import asdict
from pathlib import Path

from support_desk.desk import Desk
from support_desk.scripted import ScriptedSingleCallModel, ScriptedSummarizer, ScriptedSupportModel

_SCHEMAS = {
    "search": {"query": str},
    "refund": {
        "order_id": str,
        "amount_cents": int,
    },
    "escalate": {
        "reason": str,
    },
}

# Tools whose writes are deduplicated by a key. The harness mints the key from
# the ticket; the model never does. A key the model proposes is recorded and
# ignored, so a live model cannot defeat Reversible by naming a fresh attempt.
_KEYED_TOOLS = ("refund", "escalate")


def _mint_key(tool_name, ticket_id):
    return f"{tool_name}:{ticket_id}"


def run_single_call(desk, ticket_id, actor, model):
    ticket = desk.ticket(ticket_id)
    log = _blank_log(desk, "single_call", ticket_id, actor, ticket, max_steps=1)
    turn = model.respond(_observation(log, ticket))
    if turn.tool_name is not None:
        log["tool_calls"].append(_execute(desk, actor, ticket_id, turn.tool_name, turn.arguments))
    return _finish(log, turn.text)


def run_workflow(desk, ticket_id, actor, model):
    ticket = desk.ticket(ticket_id)
    log = _blank_log(desk, "workflow", ticket_id, actor, ticket, max_steps=2)
    search_call = _execute(
        desk, actor, ticket_id, "search", {"query": ticket["order_id"]}
    )
    log["tool_calls"].append(search_call)
    reason = _workflow_block_reason(ticket, search_call["receipt"])
    if reason is None:
        _execute_and_record(
            log,
            desk,
            actor,
            ticket_id,
            "refund",
            {
                "order_id": ticket["order_id"],
                "amount_cents": ticket["requested_amount_cents"],
            },
        )
    else:
        _execute_and_record(
            log,
            desk,
            actor,
            ticket_id,
            "escalate",
            {"reason": reason},
        )
    summary = model.respond(_observation(log, ticket)).text
    return _finish(log, summary)


def run_agent(desk, ticket_id, actor, model, max_steps, run_label=None):
    ticket = desk.ticket(ticket_id)
    log = _blank_log(
        desk, "agent", ticket_id, actor, ticket, max_steps=max_steps, run_label=run_label
    )
    summary = ""
    while len(log["tool_calls"]) < max_steps:
        turn = model.respond(_observation(log, ticket))
        if turn.tool_name is None:
            summary = turn.text
            break
        log["tool_calls"].append(
            _execute(desk, actor, ticket_id, turn.tool_name, turn.arguments)
        )
        if turn.done:
            summary = turn.text
            break
    return _finish(log, summary)


def run_side_by_side(seed, ticket_id, actor, db_dir, clock):
    runners = {
        "single_call": lambda desk: run_single_call(
            desk, ticket_id, actor, ScriptedSingleCallModel()
        ),
        "workflow": lambda desk: run_workflow(
            desk, ticket_id, actor, ScriptedSummarizer()
        ),
        "agent": lambda desk: run_agent(
            desk, ticket_id, actor, ScriptedSupportModel(), max_steps=6
        ),
    }
    logs = []
    for mode, runner in runners.items():
        desk = Desk.create(
            Path(db_dir) / f"{ticket_id}_{mode}.sqlite",
            json.loads(json.dumps(seed)),
            clock,
        )
        logs.append(runner(desk))
    return logs


def _workflow_block_reason(ticket, receipt):
    if receipt.get("rejection_reason") or receipt.get("no_result") or not receipt.get("hits"):
        return "order not found"
    paid = receipt["hits"][0]["amount_cents"]
    if ticket["requested_amount_cents"] > ticket["authority_ceiling_cents"]:
        return "exceeds authority ceiling"
    if ticket["requested_amount_cents"] > paid:
        return "exceeds amount paid"
    return None


def _execute_and_record(log, desk, actor, ticket_id, tool_name, arguments):
    log["tool_calls"].append(_execute(desk, actor, ticket_id, tool_name, arguments))


def _execute(desk, actor, ticket_id, tool_name, arguments):
    if tool_name not in _SCHEMAS:
        return {
            "tool": tool_name,
            "arguments": dict(arguments),
            "receipt": {"rejection_reason": "unknown tool", "accepted": False},
        }
    proposed_key = None
    if tool_name in _KEYED_TOOLS:
        proposed_key = arguments.get("idempotency_key")
        arguments = {k: v for k, v in arguments.items() if k != "idempotency_key"}
    for field, expected in _SCHEMAS[tool_name].items():
        if field not in arguments or not isinstance(arguments[field], expected):
            return {
                "tool": tool_name,
                "arguments": dict(arguments),
                "receipt": {
                    "rejection_reason": f"schema: {field}",
                    "accepted": False,
                },
            }
    if tool_name in _KEYED_TOOLS:
        arguments = {**arguments, "idempotency_key": _mint_key(tool_name, ticket_id)}
    if tool_name == "search":
        receipt = desk.search(ticket_id=ticket_id, actor=actor, query=arguments["query"])
    elif tool_name == "refund":
        receipt = desk.refund(
            ticket_id=ticket_id,
            actor=actor,
            order_id=arguments["order_id"],
            amount_cents=arguments["amount_cents"],
            idempotency_key=arguments["idempotency_key"],
        )
    else:
        receipt = desk.escalate(
            ticket_id=ticket_id,
            actor=actor,
            reason=arguments["reason"],
            idempotency_key=arguments["idempotency_key"],
        )
    call = {"tool": tool_name, "arguments": dict(arguments), "receipt": asdict(receipt)}
    if proposed_key is not None and proposed_key != arguments["idempotency_key"]:
        call["proposed_idempotency_key"] = proposed_key
    return call


def _blank_log(desk, mode, ticket_id, actor, ticket, max_steps, run_label=None):
    # The run id names the run, not the model. The operator may label a run so
    # that two runs of the same mode on the same ticket keep distinct ids.
    authority = None if ticket is None else ticket["authority_ceiling_cents"]
    run_id = f"run_{mode}_{ticket_id}"
    if run_label:
        run_id = f"{run_id}_{run_label}"
    return {
        "run_id": run_id,
        "mode": mode,
        "ticket_id": ticket_id,
        "actor": actor,
        "started_at": desk.clock(),
        "ceilings": {"authority_cents": authority, "max_steps": max_steps},
        "tool_calls": [],
        "final_outcome": None,
        "summary": None,
    }


def _observation(log, ticket):
    return {
        "mode": log["mode"],
        "ticket": ticket,
        "receipts": [call["receipt"] for call in log["tool_calls"]],
        "ceilings": log["ceilings"],
        "steps_taken": len(log["tool_calls"]),
    }


def _finish(log, summary):
    closing = None
    kind = None
    for call in log["tool_calls"]:
        receipt = call["receipt"]
        if receipt.get("refund_id"):
            closing = call
            kind = "refunded"
        elif receipt.get("handoff_ticket_id"):
            closing = call
            kind = "escalated"
    if closing is None:
        last = log["tool_calls"][-1] if log["tool_calls"] else None
        log["final_outcome"] = {
            "kind": "incomplete",
            "tool": None if last is None else last["tool"],
            "receipt": None if last is None else last["receipt"],
        }
    else:
        log["final_outcome"] = {
            "kind": kind,
            "tool": closing["tool"],
            "receipt": closing["receipt"],
        }
    log["summary"] = summary
    return log
