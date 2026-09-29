"""Pagination envelopes and PQL failure handling for Plane list endpoints.

Several resources list work items -- workitem, cycle and module -- and all of
them must return the same envelope shape and handle an invalid PQL filter the
same way. Keeping that here is what makes them consistent.

This is Plane's pagination contract, not one tool surface's: any surface talking
to these endpoints needs the same envelope and the same 400-on-bad-PQL answer.
"""

from __future__ import annotations

from typing import Any

from fastmcp.utilities.logging import get_logger
from plane.errors.errors import HttpError
from plane.models.query_params import WorkItemQueryParams

from plane_mcp.pql_reference import PQL_FIELD_DESCRIPTION, PQL_FULL_REFERENCE
from plane_mcp.toolkit.runtime import opt

logger = get_logger(__name__)


PER_PAGE_CAP = 100
DEFAULT_PER_PAGE = 25


def resolve_per_page(per_page: int) -> int:
    """Apply the server-side per_page default and cap."""
    return min(per_page or DEFAULT_PER_PAGE, PER_PAGE_CAP)


def sparse_dump(item: Any, fields: str | None) -> Any:
    """model_dump(include=...) when `fields` names a fieldset, else the model itself."""
    if not fields:
        return item
    requested = {name.strip() for name in fields.split(",")} - {""}
    return item.model_dump(include=requested)


def dump_results(items: Any, fields: str | None) -> list[Any]:
    """Serialise a page, honouring `fields` as a sparse fieldset.

    Written as a loop rather than a nested ternary: the previous one-liner read
    as though `hasattr` guarded both branches, when it guarded only the second,
    so a page of plain dicts raised AttributeError as soon as `fields` was set.
    """
    requested = {name.strip() for name in fields.split(",")} - {""} if fields else None
    dumped: list[Any] = []
    for item in items or []:
        if not hasattr(item, "model_dump"):
            dumped.append(item)
        elif requested:
            dumped.append(item.model_dump(include=requested))
        else:
            dumped.append(item.model_dump())
    return dumped


ENVELOPE_FIELDS = (
    "total_count",
    "count",
    "next_cursor",
    "prev_cursor",
    "next_page_results",
    "prev_page_results",
)


def envelope(response: Any, fields: str | None = None) -> dict[str, Any]:
    """Keep the full pagination envelope so paging stays discoverable.

    An action that takes a `cursor` must return one. Handing back only
    `response.results` lets a caller page in but never page on, and makes a
    truncated first page look like the whole set.

    A few Plane responses carry `results` and nothing else. Those are refused
    rather than defaulted to None: a null `next_cursor` reads as "last page",
    which is the same lie by another route.
    """
    absent = [name for name in ENVELOPE_FIELDS if not hasattr(response, name)]
    if absent:
        raise TypeError(
            f"{type(response).__name__} has no {', '.join(absent)}, so it cannot be enveloped: "
            "this endpoint does not paginate. Return response.results directly, leave cursor and "
            "per_page out of the action's optional params, and record it in NOT_PAGINATED in "
            "tests/tools/test_pagination.py."
        )
    return {
        "results": dump_results(response.results, fields),
        **{name: getattr(response, name) for name in ENVELOPE_FIELDS},
    }


def _resolve_label_names(client: Any, workspace_slug: str, project_id: str) -> dict[str, str]:
    """Build a {label_id: label_name} map for one project's labels.

    Plane's REST API returns `labels[]` on a workitem as bare UUIDs from list
    endpoints and as expanded Label objects from retrieve endpoints. Callers
    overwhelmingly want the human-readable name, so we resolve once per workitem
    call. Empty on no project_id -- workspace-scope lists keep the UUIDs with
    `name == id`, the cheap fall-back.

    Failures (transient Plane 5xx, network blip) are swallowed: an empty map
    means the response keeps the UUID with `name == id`, which is the
    pre-fix shape and strictly worse than nothing -- never worse than the old
    wire shape, and never lossy on `id`.
    """
    if not project_id:
        return {}
    try:
        resp = client.labels.list(workspace_slug=workspace_slug, project_id=project_id, params=None)
    except Exception:  # noqa: BLE001 - enrichment is best-effort; the call's primary result stands either way
        logger.warning("label enrichment skipped: client.labels.list failed")
        return {}

    def _id_name(label: Any) -> tuple[str | None, str | None]:
        if isinstance(label, dict):
            return label.get("id"), label.get("name")
        return getattr(label, "id", None), getattr(label, "name", None)

    return {
        label_id: label_name
        for label in (getattr(resp, "results", None) or [])
        for label_id, label_name in (_id_name(label),)
        if label_id and label_name
    }


def _enrich_labels(items: Any, label_map: dict[str, str]) -> None:
    """Normalise each item's `labels` field to a list of `{id, name, color}` objects.

    Plane returns two shapes depending on the endpoint:

    - list / list_archived / search: `["<uuid>", ...]`
    - retrieve / retrieve_by_identifier: `[{id, name, color, ...}, ...]`

    Callers cannot rely on one type, so we normalise in place:

    - bare UUID strings become `{"id": u, "name": label_map.get(u, u)}`
    - objects with empty `name` are backfilled from `label_map`
    - objects with a non-empty `name` are left alone
    - `None` / missing / non-list values are left alone

    Items may be Pydantic models (the production case) or plain dicts (the
    envelope-output case after `dump_results`). Both attribute and item access
    are tried on each label entry.
    """
    for item in items or []:
        if isinstance(item, dict):
            labels = item.get("labels")
            target = item
        else:
            labels = getattr(item, "labels", None)
            target = item
        if not isinstance(labels, list):
            continue
        enriched: list[Any] = []
        for entry in labels:
            if isinstance(entry, str):
                enriched.append({"id": entry, "name": label_map.get(entry, entry)})
                continue
            if isinstance(entry, dict):
                obj_id = entry.get("id")
                obj_name = entry.get("name")
            else:
                obj_id = getattr(entry, "id", None)
                obj_name = getattr(entry, "name", None)
            if obj_id and not obj_name:
                backfilled = label_map.get(obj_id, obj_id)
                if isinstance(entry, dict):
                    entry["name"] = backfilled
                else:
                    entry.name = backfilled
                enriched.append(entry)
                continue
            enriched.append(entry)
        if isinstance(target, dict):
            target["labels"] = enriched
        else:
            target.labels = enriched


def workitem_page(
    tool: str,
    action: str,
    method: Any,
    *,
    pql: str = "",
    order_by: str = "",
    cursor: str = "",
    per_page: int = 0,
    expand: str = "",
    fields: str = "",
    **target: Any,
) -> dict[str, Any]:
    """One page of the work items belonging to a parent record."""
    # `target` carries the SDK kwargs the parent tool already collected
    # (workspace_slug, project_id, cycle_id / module_id / etc.). The same
    # bag carries `client`, which we need for label enrichment but the SDK
    # method itself does not. Pull just that one out.
    client = target.pop("client", None)
    workspace_slug = target.get("workspace_slug", "")
    project_id = target.get("project_id", "")
    try:
        response = method(
            **target,
            params=WorkItemQueryParams(
                pql=opt(pql),
                order_by=opt(order_by),
                per_page=resolve_per_page(per_page),
                cursor=opt(cursor),
                expand=opt(expand),
                fields=opt(fields),
            ),
        )
    except HttpError as exc:
        failure = pql_failure(tool, action, pql, exc)
        if failure is None:
            raise
        return failure
    _enrich_labels(response.results, _resolve_label_names(client, workspace_slug, project_id))
    return envelope(response, opt(fields))


def pql_failure(tool: str, action: str, pql: str, exc: HttpError) -> dict[str, Any] | None:
    """Turn an invalid-PQL 400 into a correctable answer instead of an exception."""
    if not (pql and exc.status_code == 400 and isinstance(exc.response, dict)):
        return None
    detail = _filter_complaint(exc.response)
    if detail is None:
        return None
    logger.warning("%s %s: invalid PQL %r -> %s", tool, action, pql, exc.response)
    return {
        "error": detail,
        "failed_pql": pql,
        "pql_reference": PQL_FIELD_DESCRIPTION,
        "hint": f"The PQL above failed. Fix it using the brief reference, or call "
                f"get_pql_reference(detail='full') for the full syntax, then retry {tool} {action}.",
    }


def _filter_complaint(body: dict[str, Any]) -> Any | None:
    """What a 400 said about the filter, or None when it was about something else."""
    if "pql" in body:
        return body["pql"]
    if not any(word in str(body).lower() for word in ("pql", "filter")):
        return None
    return body.get("message") or body.get("error") or body.get("detail") or body
