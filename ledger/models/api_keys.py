from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from ledger.models.base import Base, CreatedAtMixin, UUIDPKMixin


class ApiKey(Base, UUIDPKMixin, CreatedAtMixin):
    __tablename__ = "api_keys"

    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
