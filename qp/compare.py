"""Supplier comparison (trusted). Comparison totals are computed here, never by the AI.

Rules (approved architecture, clarification 2 + currency rule):
  * Only documents in the project currency are compared; others go to
    "Not comparable: different currency" and are never added or converted.
  * Only items in HUMAN-CONFIRMED equivalence groups count towards comparable
    totals. AI-proposed groups appear under "Not compared yet".
  * A charge kind that any supplier quotes (delivery, installation, tax, other)
    is Unknown for a supplier that does not state it. Unknown is never zero.
"""

from decimal import Decimal

from . import money
from .layer2 import CALCULATED, ESTIMATED, QUOTED, UNKNOWN, fig

CHARGE_KINDS = ("delivery", "installation", "other", "discount", "tax")


def compare(docs, groups, project_currency):
    """docs: [{doc_id, supplier, currency, comparable, l2}]  (l2 = layer2.reconcile output)
    groups: [{group_id, label, status: proposed|confirmed|rejected, members: [{doc_id, item_id}],
              confirmed_by?, confirmed_at?}]
    """
    comp = [d for d in docs if d["comparable"]]
    other_currency = [{"doc_id": d["doc_id"], "supplier": d["supplier"], "currency": d["currency"]}
                      for d in docs if not d["comparable"]]
    confirmed = [g for g in groups if g["status"] == "confirmed"]
    proposed = [g for g in groups if g["status"] == "proposed"]
    by_doc = {d["doc_id"]: d for d in comp}

    def line(doc_id, item_id):
        d = by_doc.get(doc_id)
        if not d:
            return None
        return next((x for x in d["l2"]["lines"] if x["id"] == item_id), None)

    rows = []
    in_confirmed = set()
    for g in confirmed:
        cells = {}
        for d in comp:
            members = [m for m in g["members"] if m["doc_id"] == d["doc_id"]]
            if not members:
                cells[d["doc_id"]] = {"status": "not_offered"}
                continue
            figs = [line(d["doc_id"], m["item_id"]) for m in members]
            in_confirmed.update((d["doc_id"], m["item_id"]) for m in members)
            vals = [f["line_total"] for f in figs if f]
            if any(v["value"] is None for v in vals) or not vals:
                cells[d["doc_id"]] = {"status": "unknown", **fig(None, UNKNOWN)}
            else:
                total = sum((Decimal(v["value"]) for v in vals), Decimal(0))
                label = vals[0]["label"] if len(vals) == 1 else CALCULATED
                cells[d["doc_id"]] = {"status": "ok", **fig(total, label), "items": [m["item_id"] for m in members]}
        rows.append({"group_id": g["group_id"], "label": g["label"], "cells": cells,
                     "equivalence": {"status": "confirmed", "by": g.get("confirmed_by"), "at": g.get("confirmed_at")}})

    # charges per kind
    kinds_present = {ch["kind"] for d in comp for ch in d["l2"]["charges"]}
    if any(d["l2"]["totals"]["tax"]["value"] is not None for d in comp):
        kinds_present.add("tax")
    charge_rows = []
    for kind in [k for k in CHARGE_KINDS if k in kinds_present]:
        cells = {}
        for d in comp:
            chs = [ch for ch in d["l2"]["charges"] if ch["kind"] == kind]
            if kind == "tax" and not chs and d["l2"]["totals"]["tax"]["value"] is not None:
                cells[d["doc_id"]] = d["l2"]["totals"]["tax"]
            elif not chs:
                cells[d["doc_id"]] = fig(None, UNKNOWN, reason="not stated in this quote")
            elif any(ch["amount"]["value"] is None for ch in chs):
                cells[d["doc_id"]] = fig(None, UNKNOWN, reason="stated but not verified")
            else:
                total = sum((Decimal(ch["amount"]["value"]) for ch in chs), Decimal(0))
                if kind == "discount":
                    total = -abs(total)
                cells[d["doc_id"]] = fig(total, chs[0]["amount"]["label"] if len(chs) == 1 else CALCULATED)
        charge_rows.append({"kind": kind, "cells": cells})

    # supplier totals: known part + list of unknowns, never a fake complete total
    totals = {}
    for d in comp:
        known, unknown, labels = Decimal(0), [], set()
        for r in rows:
            c = r["cells"][d["doc_id"]]
            if c["status"] == "not_offered":
                unknown.append(f"{r['label']}: not offered")
            elif c["value"] is None:
                unknown.append(f"{r['label']}: unknown")
            else:
                known += Decimal(c["value"])
                labels.add(c["label"])
        for cr in charge_rows:
            c = cr["cells"][d["doc_id"]]
            if c["value"] is None:
                unknown.append(f"{cr['kind']}: unknown")
            else:
                known += Decimal(c["value"])
                labels.add(c["label"])
        totals[d["doc_id"]] = {
            "known_total": str(money.q2(known)),
            "label": CALCULATED + (" (includes estimate)" if ESTIMATED in labels else ""),
            "complete": not unknown and bool(rows),
            "unknown_components": unknown,
        }

    not_compared = []
    for d in comp:
        for x in d["l2"]["lines"]:
            if (d["doc_id"], x["id"]) not in in_confirmed:
                prop = next((g for g in proposed if any(m["doc_id"] == d["doc_id"] and m["item_id"] == x["id"]
                                                        for m in g["members"])), None)
                not_compared.append({"doc_id": d["doc_id"], "item_id": x["id"], "description": x["description"],
                                     "line_total": x["line_total"],
                                     "equivalence": "Proposed by AI, not confirmed" if prop else "No match proposed",
                                     "group_id": prop["group_id"] if prop else None})

    return {"currency": project_currency, "suppliers": [{"doc_id": d["doc_id"], "supplier": d["supplier"]} for d in comp],
            "rows": rows, "charges": charge_rows, "totals": totals, "not_compared_yet": not_compared,
            "not_comparable_currency": other_currency,
            "note": f"All amounts in {project_currency} as quoted. No currency conversion performed."}


LABELS = (QUOTED, CALCULATED, ESTIMATED, UNKNOWN)
