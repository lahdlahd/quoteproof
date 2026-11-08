# Hand-written stand-in for AI extractor code (Milestone 1 acceptance only).
# Reads the Source Index from the read-only /input folder and prints JSON.
import json

with open("/input/index.json", encoding="utf-8") as f:
    idx = json.load(f)
print(json.dumps({"schema_seen": idx.get("schema"), "cells": len(idx.get("cells", [])),
                  "lines": len(idx.get("lines", []))}))
