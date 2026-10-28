"""Export the generic API contract without starting workers or connecting to a database."""

from __future__ import annotations

import json

from fastapi import FastAPI

from app.api.integration_api import register_integration_api


def integration_openapi() -> dict:
    app = FastAPI(title="PGClockBot Integration API", version="v1", description="Backend-only technical service API; deployment gates apply.")
    async def unused_database() -> None:
        raise RuntimeError("Schema export cannot execute a request")
    register_integration_api(app, get_db=unused_database, enabled=True)
    return app.openapi()


def main() -> None:
    print(json.dumps(integration_openapi(), ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
