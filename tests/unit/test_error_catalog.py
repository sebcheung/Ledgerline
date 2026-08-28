"""Static assertions over the domain error catalog -- no DB required.

Ensures every LedgerError subclass is fully specified (so no error can fall
through to a bare 500) and that the SPEC.md §9 type URIs are all accounted
for.
"""

import ledger.core.errors as errors_module
from ledger.core.errors import LedgerError

_SPEC_URIS = {
    "/errors/unbalanced-transaction": 422,
    "/errors/insufficient-funds": 422,
    "/errors/currency-mismatch": 422,
    "/errors/account-not-found": 404,
    # /errors/idempotency-key-reuse and /errors/rate-limited are reserved
    # for Phase 3 and Phase 7 respectively; not yet raised anywhere.
    "/errors/idempotency-conflict": 409,
    "/errors/already-reversed": 409,
}


def _all_ledger_error_subclasses() -> list[type[LedgerError]]:
    return [
        obj
        for obj in vars(errors_module).values()
        if isinstance(obj, type) and issubclass(obj, LedgerError) and obj is not LedgerError
    ]


def test_every_subclass_declares_the_three_classvars() -> None:
    for cls in _all_ledger_error_subclasses():
        assert isinstance(cls.error_type, str)
        assert cls.error_type.startswith("/errors/")
        assert isinstance(cls.title, str)
        assert cls.title
        assert isinstance(cls.status, int)
        assert 400 <= cls.status < 600


def test_error_types_are_unique() -> None:
    types = [cls.error_type for cls in _all_ledger_error_subclasses()]
    assert len(types) == len(set(types))


def test_spec_uris_are_all_represented() -> None:
    represented = {cls.error_type: cls.status for cls in _all_ledger_error_subclasses()}
    for uri, status in _SPEC_URIS.items():
        assert uri in represented, f"no LedgerError subclass declares {uri}"
        assert represented[uri] == status


def test_extra_is_json_safe() -> None:
    import uuid

    exc = errors_module.AccountNotFound("not found", account_id=uuid.uuid4())
    assert isinstance(exc.as_problem_members()["account_id"], str)
