import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from support_desk.desk import Desk
from support_desk.modes import run_agent, run_side_by_side, run_single_call, run_workflow
from support_desk.scripted import (
    ScriptedDoneFirst,
    ScriptedSingleCallModel,
    ScriptedSummarizer,
    ScriptedSupportModel,
)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="support-desk")
    parser.add_argument("--seed", type=Path, default=Path("data/seed.json"))
    parser.add_argument("--out", type=Path, default=Path("runs"))
    parser.add_argument("--ticket", default="tkt_100")
    parser.add_argument("--actor", default="support-agent")
    parser.add_argument(
        "--mode",
        choices=["single_call", "workflow", "agent", "ghost", "all"],
        default="all",
    )
    parser.add_argument("--now", default=None)
    args = parser.parse_args(argv)

    clock = _clock(args.now)
    seed = json.loads(args.seed.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    if args.mode == "ghost":
        desk = Desk.create(args.out / f"{args.ticket}_ghost.sqlite", seed, clock)
        logs = [
            run_agent(
                desk,
                args.ticket,
                args.actor,
                ScriptedDoneFirst(),
                max_steps=6,
                run_label="ghost",
            )
        ]
        destinations = [args.out / f"{args.ticket}_ghost.json"]
    else:
        logs = _run(args, seed, clock)
        destinations = [
            args.out / f"{log['ticket_id']}_{log['mode']}.json" for log in logs
        ]
    for log, destination in zip(logs, destinations):
        destination.write_text(json.dumps(log, indent=2) + "\n")
        receipt = log["final_outcome"]["receipt"] or {}
        proof = receipt.get("refund_id") or receipt.get("handoff_ticket_id") or receipt.get("rejection_reason")
        print(f"{destination.name}: {log['final_outcome']['kind']} {proof}")
    return 0


def _clock(now):
    if now is not None:
        return lambda: now
    return lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(args, seed, clock):
    if args.mode == "all":
        return run_side_by_side(seed, args.ticket, args.actor, args.out, clock)
    runners = {
        "single_call": lambda desk: run_single_call(
            desk, args.ticket, args.actor, ScriptedSingleCallModel()
        ),
        "workflow": lambda desk: run_workflow(
            desk, args.ticket, args.actor, ScriptedSummarizer()
        ),
        "agent": lambda desk: run_agent(
            desk, args.ticket, args.actor, ScriptedSupportModel(), max_steps=6
        ),
    }
    desk = Desk.create(args.out / f"{args.ticket}_{args.mode}.sqlite", seed, clock)
    return [runners[args.mode](desk)]


if __name__ == "__main__":
    raise SystemExit(main())
