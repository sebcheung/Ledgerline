"""RFC 7807 problem document schema.

Used only for OpenAPI `responses=` documentation so the generated schema
advertises the `application/problem+json` shape; runtime error handlers in
`ledger.api.errors` build plain dicts directly rather than instantiating
this model (avoiding an extra validation pass on the error path).
"""

from pydantic import BaseModel, ConfigDict


class Problem(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
