# Copyright (c) 2026 Voices of History — HOI4 AI Mod contributors
# SPDX-License-Identifier: MIT
# Adapted from middleware/strategist.py, revision
# 3226478a81edfc376d63502fa9708c48d309b447. Full license: ./LICENSE.
# EYES modifications: existing JSON/SQLite state instead of a second database;
# UUID campaign identities; persistent cursors; explicit AI/freshness checks;
# arbitrary selected countries; pending-work lock; no model or game dispatch.
"""Voices strategic-review scheduling, adapted to EYES's existing state store."""
from copy import deepcopy
import re


def normalize_ledger(value):
    """EYES compatibility boundary for optional old/new state snapshots."""
    result = {}
    if not isinstance(value, dict):
        return result
    for gid, ledger in value.items():
        if not isinstance(gid, str) or not re.fullmatch(r"[a-f0-9]{32}", gid) or not isinstance(ledger, dict):
            continue
        baselines = {tag: day for tag, day in ledger.get("baselines", {}).items()
                     if isinstance(tag, str) and re.fullmatch(r"[A-Z][A-Z0-9]{2}", tag)
                     and type(day) is int and day >= 0} if isinstance(ledger.get("baselines"), dict) else {}
        reviews = []
        for row in ledger.get("reviews", []) if isinstance(ledger.get("reviews"), list) else []:
            if (not isinstance(row, dict) or not isinstance(row.get("actor"), str) or row.get("actor") not in baselines
                    or type(row.get("day")) is not int or row["day"] < 0
                    or not isinstance(row.get("signature"), str)
                    or row.get("status") not in {"pending", "completed", "cancelled", "failed", "legacy_migrated"}):
                continue
            reviews.append({"actor": row["actor"], "day": row["day"],
                "signature": row["signature"][:80], "reason": str(row.get("reason", ""))[:600],
                "summary": str(row.get("summary", ""))[:4000], "status": row["status"]})
        previous = ledger.get("previous")
        previous = deepcopy(previous) if (isinstance(previous, list) and len(previous) == 2
                    and type(previous[0]) is int and previous[0] >= 0
                    and (previous[1] is None or isinstance(previous[1], str))) else None
        result[gid] = {"baselines": baselines, "reviews": reviews, "previous": previous}
    return result


class Strategist:
    """Port of Strategist.due/finish/history; callers retain all decision authority."""
    def __init__(self, ledger):
        self.ledger = ledger

    def due(self, state, world, countries, *, initial_review=False):
        """One candidate per new, contiguous daily tick. No bursts on startup/reload.

        EYES's old single-country API may explicitly request an initial policy
        review. The batch scheduler defaults to the upstream startup delay.
        """
        gid, day, player = state["gid"], state["day"], state.get("tag")
        ledger = self.ledger.setdefault(gid, {"baselines": {}, "reviews": [], "previous": None})
        current, previous = [day, player], ledger.get("previous")
        ledger["previous"] = current
        for actor in countries:
            ledger["baselines"].setdefault(actor, day)
        if previous is None and not initial_review:
            return None
        if previous is not None and previous[1] != player:
            for actor in countries:
                ledger["baselines"][actor] = day
            return None
        if previous is not None and (day <= previous[0] or day > previous[0] + 3):
            # Loading a save does not create a catch-up review backlog.
            if day != previous[0]:
                for actor in countries:
                    ledger["baselines"][actor] = day
            return None
        # EYES: unknown outcome is not retried automatically after restart.
        if any(row["status"] == "pending" for row in ledger["reviews"]):
            return None
        candidates = []
        for actor in countries:
            nation = world.get(actor, {})
            if (actor == player or nation.get("ai_controlled") is not True
                    or nation.get("capitulated") is True or nation.get("day") != day
                    or nation.get("verified") is not True):
                continue
            start = ledger["baselines"][actor]
            prior = [r for r in ledger["reviews"] if r["actor"] == actor]
            last = max([start, *(r["day"] for r in prior)])
            if day - last >= 7 or (initial_review and not prior):
                reason, signature = "Weekly strategic review", "week:" + str(day // 7)
            else:
                continue
            exists = any(r["actor"] == actor and r["signature"] == signature for r in prior)
            if exists:
                continue
            candidates.append((last, actor, reason, signature))
        if not candidates:
            return None
        _, actor, reason, signature = sorted(candidates)[0]
        ledger["reviews"].append({"actor": actor, "day": day, "reason": reason,
                                  "signature": signature, "summary": "", "status": "pending"})
        return {"gid": gid, "day": day, "actor": actor, "reason": reason, "signature": signature}

    def finish(self, review, summary, status="completed"):
        if status not in {"completed", "cancelled", "failed"}:
            raise ValueError("Invalid strategic review result")
        if not isinstance(review, dict) or not {"gid", "actor", "signature", "day"} <= set(review):
            return False
        for row in self.ledger.get(review["gid"], {}).get("reviews", []):
            if (row["actor"] == review["actor"] and row["signature"] == review["signature"]
                    and row["day"] == review["day"] and row["status"] == "pending"):
                row.update(summary=str(summary)[:4000], status=status)
                return True
        return False

    def history(self, gid, actor, day, limit=3):
        # Same temporal and bounded-history contract as the upstream SQL query.
        rows = [r for r in self.ledger.get(gid, {}).get("reviews", [])
                if r["actor"] == actor and r["day"] <= day and r["summary"]]
        rows = sorted(rows, key=lambda r: r["day"], reverse=True)[:max(0, min(3, limit))]
        return [{"day": r["day"], "reason": r["reason"], "summary": r["summary"][:900],
                 "status": r["status"]} for r in reversed(rows)]
