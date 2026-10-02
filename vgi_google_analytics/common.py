"""Small pieces every function module shares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from vgi.arguments import SecretLookupEntry
from vgi.cache_control import CacheControl
from vgi_rpc import ArrowSerializableDataclass

from vgi_google_analytics import auth
from vgi_google_analytics.ga_api import Session
from vgi_google_analytics.settings import Settings

if TYPE_CHECKING:
    import pyarrow as pa
    from vgi.protocol import VgiOutputCollector
    from vgi_rpc.rpc import OutputCollector

#: Every function asks the extension to resolve the ``google_analytics`` secret.
REQUIRED_SECRETS = [SecretLookupEntry(secret_type=auth.SECRET_TYPE)]

#: Grace window for serving a stale cached result when a refetch fails.
STALE_IF_ERROR = 300

#: Admin configuration (accounts, properties, streams, custom definitions)
#: changes when a human edits it in the GA4 interface — rarely, and never in
#: a way a few minutes of staleness would mislead.
ADMIN_TTL_SECONDS = 300

#: The dimension/metric catalogue changes only when a custom definition does.
METADATA_TTL_SECONDS = 3_600

#: A scalar subquery for "some property this credential can read". Used in the
#: catalog's runnable examples so that they execute against *any* credential
#: rather than a property id that only exists in one account.
ANY_PROPERTY = (
    "(SELECT property_id FROM ga4.main.account_summaries "
    "WHERE property_id IS NOT NULL ORDER BY property_id LIMIT 1)"
)


def session(params: Any) -> Session:
    """This call's credentials and its catalog's ATTACH settings.

    Settings arrive as the catalog's attach bytes; credentials come from the
    call's secrets, or from Application Default Credentials when the settings
    and the operator both allow it.
    """
    settings = Settings.decode(getattr(params, "attach_opaque_data", None))
    creds = auth.resolve(getattr(params, "secrets", None), allow_adc=settings.may_use_adc)
    return Session(credentials=creds, settings=settings)


def cache_for(ttl: int) -> CacheControl | None:
    """Result-cache metadata for ``ttl`` seconds, or ``None`` for "do not cache"."""
    if ttl <= 0:
        return None
    return CacheControl(ttl=ttl, stale_if_error=STALE_IF_ERROR)


def emit(out: OutputCollector, batch: pa.RecordBatch, ttl: int = 0) -> None:
    """Emit ``batch``, advertising it as cacheable for ``ttl`` seconds when positive.

    The collector the framework passes is VGI's, whose ``emit`` takes
    ``cache_control``; it is typed as vgi-rpc's narrower base class.
    """
    cast("VgiOutputCollector", out).emit(batch, cache_control=cache_for(ttl))


@dataclass(kw_only=True)
class ListState(ArrowSerializableDataclass):
    """Where a paged list scan has got to, persisted between ``process()`` ticks.

    ``targets_json`` is the JSON list of parents (accounts or properties) still
    to walk, head first; ``page_token`` continues the head's listing. Both are
    strings because the scan state must round-trip through Arrow and HTTP.
    """

    targets_json: str = ""
    page_token: str = ""
    started: bool = False
    done: bool = False
