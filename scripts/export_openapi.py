"""Write the OpenAPI spec to docs/openapi.json (reviewable without running the server).

python scripts/export_openapi.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402


def main() -> None:
    out = ROOT / "docs" / "openapi.json"
    out.write_text(json.dumps(app.openapi(), indent=2), encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)} ({len(app.openapi()['paths'])} paths)")


if __name__ == "__main__":
    main()
