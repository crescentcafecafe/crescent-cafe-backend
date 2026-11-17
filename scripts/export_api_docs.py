"""Generate docs/API.md from the live OpenAPI schema so documentation can never drift from the code.   python -m scripts.export_api_docs"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_doc.db")
os.environ.setdefault("EMBEDDED_WORKER", "false")

from app.main import app  # noqa: E402

AUTH_HINT = {"admin": "Admin", "current_admin": "Admin", "current_customer": "Customer", "current_rider": "Rider", "current_principal": "Any signed-in user"}


def auth_of(path: str, op: dict) -> str:
    if path.startswith("/api/v1/admin") or "/reports" in path:
        return "Admin"
    if any("Bearer" in str(s) or "HTTPBearer" in str(s) for s in op.get("security", [])):
        return "Signed-in (role-checked)"
    return "Public / optional (guest token or Bearer)"


def main() -> None:
    spec = app.openapi()
    groups: dict[str, list[str]] = defaultdict(list)
    for path, ops in sorted(spec["paths"].items()):
        for method, op in ops.items():
            tag = (op.get("tags") or ["other"])[0]
            params = [p["name"] + ("*" if p.get("required") else "") for p in op.get("parameters", []) if p["in"] in ("query", "header")]
            body = ""
            rb = op.get("requestBody", {}).get("content", {})
            if "application/json" in rb:
                ref = rb["application/json"]["schema"].get("$ref", "").split("/")[-1]
                body = f"`{ref}`" if ref else "JSON"
            elif rb:
                body = "multipart/form-data"
            doc = (op.get("description") or op.get("summary") or "").strip().split("\n")[0]
            groups[tag].append(f"| `{method.upper()}` | `{path}` | {auth_of(path, op)} | {body} | {', '.join(params) or '-'} | {doc} |")
    out = ["# API reference (generated)", "",
           "Generated from the OpenAPI schema by `python -m scripts.export_api_docs`. The interactive version is served at `/docs` (Swagger) and `/redoc`; the machine-readable schema at `/openapi.json`.", "",
           "Conventions: all money is **integer paise** (INR). Errors are `{\"error\": {\"code\", \"message\", \"details\"}}`. Pagination uses `limit`/`offset` and returns `{items,total,limit,offset}`. "
           "`*` marks required parameters. Send `Idempotency-Key` on order creation and refunds. Guests use header `X-Guest-Token`.", ""]
    for tag in sorted(groups):
        out += [f"## {tag}", "", "| Method | Path | Auth | Body | Params | Description |", "|---|---|---|---|---|---|", *groups[tag], ""]
    out += ["## Schemas", "", "Request/response models are listed in `/openapi.json` under `components.schemas`.", ""]
    os.makedirs("docs", exist_ok=True)
    with open("docs/API.md", "w") as f:
        f.write("\n".join(out))
    print(f"wrote docs/API.md ({sum(len(v) for v in groups.values())} endpoints)")


if __name__ == "__main__":
    main()
