# Hand-written stand-in for AI-generated extractor code. Used by the test suite and
# for the sandbox-side check on the VM before an inference key exists.
# It follows the same contract the model is given: read /input/index.json,
# print one qp.extraction.v1 JSON object, standard library only.
import json
import re
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/input/index.json"
with open(PATH, encoding="utf-8") as f:
    idx = json.load(f)

CHARGE = [(re.compile(r"deliver|transport|shipping|freight|logistic", re.I), "delivery"),
          (re.compile(r"install", re.I), "installation"),
          (re.compile(r"\bvat\b|\btax\b", re.I), "tax"),
          (re.compile(r"discount|rebate", re.I), "discount")]
NUM = re.compile(r"(?:₦|NGN\s?|USD\s?|US\$|\$|N(?=\d))?\d[\d,]*(?:\.\d+)?")


def F(v, ref):
    return None if v in (None, "") else {"value_str": str(v), "cite": ref}


def kind_of(text):
    for rx, k in CHARGE:
        if rx.search(text or ""):
            return k
    return None


out = {"schema": "qp.extraction.v1", "supplier": None, "currency": "UNKNOWN", "line_items": [], "charges": [],
       "stated": {"subtotal": None, "tax": None, "grand_total": None}}
alltext = " ".join((c.get("value") or "") for c in idx.get("cells", [])) + " ".join(
    l["text"] for l in idx.get("lines", []))
if re.search(r"₦|\bNGN\b", alltext):
    out["currency"] = "NGN"
elif re.search(r"\bUSD\b|US\$|\$", alltext):
    out["currency"] = "USD"

if idx.get("cells"):
    rows = {}
    for c in idx["cells"]:
        rows.setdefault((c.get("sheet"), c["row"]), {})[c["col"]] = c
    header = None
    for key in sorted(rows):
        texts = {col: (c.get("value") or "").lower() for col, c in rows[key].items()}
        if any("qty" in t or "quantity" in t for t in texts.values()):
            header = key
            cols = {}
            for col, t in texts.items():
                if "qty" in t or "quantity" in t:
                    cols["qty"] = col
                elif "unit" in t or "rate" in t or ("price" in t and "total" not in t):
                    cols["unit"] = col
                elif "total" in t or "amount" in t:
                    cols["total"] = col
                elif "item" in t or "desc" in t or "product" in t:
                    cols["desc"] = col
            cols.setdefault("desc", min(texts))
            break
    for key in sorted(rows):
        r = rows[key]
        label = r.get(min(r))
        if label and (label.get("value") or "").lower() == "supplier" and len(r) > 1:
            other = r[sorted(r)[1]]
            out["supplier"] = F(other["value"], other["ref"])
    if header:
        n = 0
        for key in sorted(k for k in rows if k[0] == header[0] and k[1] > header[1]):
            r = rows[key]
            d = r.get(cols["desc"])
            desc = (d.get("value") if d else "") or ""
            tot = r.get(cols.get("total"))
            low = desc.lower()
            if "grand total" in low or low.strip() == "total":
                out["stated"]["grand_total"] = F(tot and tot["value"], tot and tot["ref"])
            elif "subtotal" in low or "sub-total" in low:
                out["stated"]["subtotal"] = F(tot and tot["value"], tot and tot["ref"])
            elif kind_of(desc):
                k = kind_of(desc)
                rate = re.search(r"\d+(?:\.\d+)?%", desc)
                out["charges"].append({"id": f"C{len(out['charges']) + 1}", "kind": k, "description": F(desc, d["ref"]),
                                       "amount": F(tot and tot["value"], tot and tot["ref"]),
                                       "rate": F(rate.group(0), d["ref"]) if rate else None})
                if k == "tax" and tot:
                    out["stated"]["tax"] = F(tot["value"], tot["ref"])
            elif d and r.get(cols.get("qty")) and re.fullmatch(r"\d[\d,]*(?:\.\d+)?", (r[cols["qty"]].get("value") or "").strip()):
                n += 1
                q, u = r.get(cols.get("qty")), r.get(cols.get("unit"))
                out["line_items"].append({"id": f"L{n}", "description": F(desc, d["ref"]),
                                          "qty": F(q["value"], q["ref"]),
                                          "unit_price": F(u and u["value"], u and u["ref"]),
                                          "line_total": F(tot and tot["value"], tot and tot["ref"])})
else:
    item = re.compile(r"^(.*?)\s+(\d[\d,]*)\s*x\s*(\S+)\s*=\s*(\S+)\s*$")
    for i, ln in enumerate(idx.get("lines", [])):
        t, ref = ln["text"], ln["ref"]
        if i == 0:
            out["supplier"] = F(t.split(" - ")[0].strip(), ref)
            continue
        m = item.match(t)
        nums = NUM.findall(t)
        if m:
            out["line_items"].append({"id": f"L{len(out['line_items']) + 1}", "description": F(m.group(1), ref),
                                      "qty": F(m.group(2), ref), "unit_price": F(m.group(3), ref),
                                      "line_total": F(m.group(4), ref)})
        elif re.match(r"^(grand\s+)?total\b", t, re.I) and nums:
            out["stated"]["grand_total"] = F(nums[-1], ref)
        elif re.match(r"^sub-?total\b", t, re.I) and nums:
            out["stated"]["subtotal"] = F(nums[-1], ref)
        elif kind_of(t) and nums and re.search(r"\d,\d{3}|\.\d{2}\b", nums[-1]):
            label = NUM.sub("", t).strip(" :-")
            out["charges"].append({"id": f"C{len(out['charges']) + 1}", "kind": kind_of(t),
                                   "description": F(label or t, ref), "amount": F(nums[-1], ref), "rate": None})

print(json.dumps(out, ensure_ascii=False))
