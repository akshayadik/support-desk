# Support desk

The running system for *The Agentic Architect*. One order desk, three ways of running the same refund ticket.

Week 1 is the happy path only: search, refund, and escalate, with a receipt for each. The same ticket runs as a single call, a fixed workflow, and an agent loop. Each mode gets its own ledger, seeded from `data/seed.json`, so the three runs can be compared without refunding the order three times.

The model is scripted. The harness is not: single call stops after one tool, the workflow chooses the steps itself, and the agent loop asks the model again after every receipt until the model stops or the step ceiling is hit. A real model can replace the classes in `scripted.py` later. The run log's `final_outcome` is always the tool receipt. `summary` is narration and is not proof.

## Run

```bash
cd /Users/akshay/Documents/_Projects/POC/support-desk
PYTHONPATH=src python3 -m support_desk --mode all --now 2026-09-28T00:00:00Z
```

That writes `runs/tkt_100_single_call.json`, `runs/tkt_100_workflow.json`, and `runs/tkt_100_agent.json`, plus a sqlite ledger beside each one.

```bash
PYTHONPATH=src python3 -m pytest
```

## Ticket tkt_100

Ada asks to refund order `ord_1042` ($40.00). The ticket's authority ceiling is $50.00. An unverified customer note says the refund already happened. Nothing in the happy path reads that note.
