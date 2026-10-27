"""A live model in the third seat.

One Messages request per turn. The harness still owns the loop, the tool
schemas, the idempotency key, and the close. This adapter only turns the
model's reply into a ModelTurn, the same shape the scripted stand-ins return.

Requires the `anthropic` package and a credential the SDK can find
(ANTHROPIC_API_KEY, or a profile from `ant auth login`).
"""

import json

from support_desk.model import ModelTurn

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"

SYSTEM = (
    "You are the model seat on a customer support desk. The desk is a ledger "
    "and a harness runs the loop. Each turn you are shown one ticket, the "
    "receipts of every tool call made so far on this run, the run's ceilings, "
    "and the number of steps taken.\n\n"
    "Your job is to handle the ticket. A refund is appropriate when the order "
    "exists, the requested amount is no more than the amount paid, and it is "
    "inside the authority ceiling. Otherwise escalate with a reason.\n\n"
    "Call at most one tool per turn. When the receipts show the ticket has "
    "been handled, reply with a one-sentence summary and no tool call. A "
    "receipt is the only evidence that a tool did something; your sentence is not."
)

# The model-facing schemas. They match modes._SCHEMAS: no idempotency_key,
# because the harness mints it.
TOOLS = [
    {
        "name": "search",
        "description": (
            "Look up orders by order id or customer id. Returns a result_set_id "
            "and the matching orders, or no_result when nothing matches."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "refund",
        "description": (
            "Refund an order by the given amount in cents. Returns a refund_id, "
            "or a denial_reason when the desk refuses."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount_cents": {"type": "integer"},
            },
            "required": ["order_id", "amount_cents"],
            "additionalProperties": False,
        },
    },
    {
        "name": "escalate",
        "description": (
            "Hand the ticket to a person with a reason. Returns a "
            "handoff_ticket_id, or a rejection_reason."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]


def describe(model=DEFAULT_MODEL, effort=DEFAULT_EFFORT):
    """What the live seat was given, for the record beside the run files."""
    return {"model": model, "effort": effort, "system": SYSTEM, "tools": TOOLS}


class LiveSupportModel:
    """Calls a live Claude model once per turn and records each raw reply."""

    def __init__(self, client=None, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.turns = []

    def respond(self, observation):
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[
                {"role": "user", "content": json.dumps(observation, indent=2)}
            ],
        )
        text = " ".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        usage = getattr(response, "usage", None)
        self.turns.append(
            {
                "step": observation["steps_taken"],
                "served_by": getattr(response, "model", self.model),
                "stop_reason": response.stop_reason,
                "text": text,
                "tool_uses": [
                    {"name": block.name, "input": dict(block.input)} for block in tool_uses
                ],
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            }
        )
        if response.stop_reason == "refusal" or not tool_uses:
            return ModelTurn(tool_name=None, arguments={}, text=text, done=True)
        first = tool_uses[0]
        return ModelTurn(
            tool_name=first.name, arguments=dict(first.input), text=text, done=False
        )


def summary_row(number, log, desk):
    """One line of the live-run table: what the harness recorded, not what the model said."""
    ticket = desk.ticket(log["ticket_id"])
    calls = log["tool_calls"]
    outcome = log["final_outcome"]
    receipt = outcome["receipt"] or {}
    return {
        "run": number,
        "run_id": log["run_id"],
        "steps": len(calls),
        "tools": " > ".join(call["tool"] for call in calls) or "(none)",
        "rejected": sum(1 for call in calls if call["receipt"].get("rejection_reason")),
        "denied": sum(1 for call in calls if call["receipt"].get("denial_reason")),
        "proposed_keys": sum(1 for call in calls if "proposed_idempotency_key" in call),
        "outcome": outcome["kind"],
        "evidence": receipt.get("refund_id") or receipt.get("handoff_ticket_id") or "",
        "refunded_cents": desk.refunded_cents(ticket["order_id"]),
        "summary": log["summary"] or "",
    }


def summary_table(rows):
    header = (
        "| run | steps | tools | rejected | denied | proposed keys | outcome | evidence | refunded cents | summary |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    )
    lines = []
    for row in rows:
        summary = row["summary"].replace("|", "\\|").replace("\n", " ")
        if len(summary) > 100:
            summary = summary[:97] + "..."
        lines.append(
            f"| {row['run']:02d} | {row['steps']} | {row['tools']} | {row['rejected']} | "
            f"{row['denied']} | {row['proposed_keys']} | {row['outcome']} | {row['evidence']} | "
            f"{row['refunded_cents']} | \"{summary}\" |"
        )
    counts = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    totals = ", ".join(f"{kind} {n}" for kind, n in sorted(counts.items()))
    return header + "\n".join(lines) + f"\n\nRuns: {len(rows)}. Outcomes: {totals}.\n"
