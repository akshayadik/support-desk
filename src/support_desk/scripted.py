from support_desk.model import ModelTurn


class ScriptedSingleCallModel:
    """One refund call. The text is a narration; the receipt is the proof."""

    def respond(self, observation):
        ticket = observation["ticket"]
        return ModelTurn(
            tool_name="refund",
            arguments={
                "order_id": ticket["order_id"],
                "amount_cents": ticket["requested_amount_cents"],
                "idempotency_key": f"refund:{ticket['id']}",
            },
            text="Refund the order.",
            done=False,
        )


class ScriptedSummarizer:
    """Quotes a receipt that already exists. Does not choose a tool."""

    def respond(self, observation):
        receipts = observation["receipts"]
        last = receipts[-1] if receipts else {}
        if last.get("refund_id"):
            text = f"Summary only. Refund receipt {last['refund_id']}."
        elif last.get("handoff_ticket_id"):
            text = f"Summary only. Handoff receipt {last['handoff_ticket_id']}."
        else:
            text = "Summary only. No closing receipt."
        return ModelTurn(tool_name=None, arguments={}, text=text, done=True)


class ScriptedSupportModel:
    """Search, then refund, then stop. Stands in for a tool-calling model."""

    def respond(self, observation):
        receipts = observation["receipts"]
        if any(receipt.get("refund_id") for receipt in receipts):
            return ModelTurn(tool_name=None, arguments={}, text="Done.", done=True)
        if not any(receipt.get("result_set_id") for receipt in receipts):
            return ModelTurn(
                tool_name="search",
                arguments={"query": observation["ticket"]["order_id"]},
                text="",
                done=False,
            )
        ticket = observation["ticket"]
        return ModelTurn(
            tool_name="refund",
            arguments={
                "order_id": ticket["order_id"],
                "amount_cents": ticket["requested_amount_cents"],
                "idempotency_key": f"refund:{ticket['id']}",
            },
            text="",
            done=False,
        )
