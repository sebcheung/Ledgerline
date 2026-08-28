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
    "/errors/idempotency-conflict": 409,
    "/errors/idempotency-key-reuse": 422,
    # /errors/rate-limited is reserved for Phase 7; not yet raised anywhere.
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


def test_problem_headers_are_str_to_str() -> None:
    for cls in _all_ledger_error_subclasses():
        headers = cls("detail").problem_headers()
        assert isinstance(headers, dict)
        for key, value in headers.items():
            assert isinstance(key, str)
            assert isinstance(value, str)


def test_idempotency_conflict_sets_retry_after() -> None:
    exc = errors_module.DuplicateTransaction("in flight")
    assert exc.problem_headers() == {"Retry-After": "1"}
