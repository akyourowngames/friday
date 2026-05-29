import numpy as np
from agent.router import ToolRouter
from agent.embedder import embed

lines = []
router = ToolRouter()
for q in ["show directory entries in tools", "find my recent google drive files", "list files in the tools directory"]:
    qe = embed(q)
    names = [t["name"] for t in router.select_tools(q, qe)]
    hint = router.capability_hint("composio")
    lines.append(f"{q!r} -> {names} | hint_slug={hint.get('args',{}).get('tool_slug','-')}")

from pathlib import Path
Path("_diag2_out.txt").write_text("\n".join(lines), encoding="utf-8")
