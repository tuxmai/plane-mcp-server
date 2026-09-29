"""Workitem `labels[]` enrichment: one uniform shape across every read action.

Plane's REST returns `labels[]` as bare UUIDs from list endpoints and as full
`{id, name, color}` objects from retrieve endpoints. The enrichment helpers in
`plane_mcp.toolkit.paging` normalise both to the object shape so a caller
never sees two wire formats from the same MCP server.

These tests use `SpyClient` (see `_spyclient.py`) to assert:

- list with a populated label map resolves UUIDs into `{id, name}` objects,
- list with a missing label map keeps `name == id` (no id is ever lost),
- retrieve with already-populated names is a pass-through,
- retrieve with empty `name` is backfilled from the map,
- list_workspace (no project_id) does not call `client.labels.list`,
- manage_label still extracts UUIDs from enriched objects (ids_of handles dicts),
- retrieve_by_identifier falls back to the response's `project` field for the
  workspace project, so labels resolve even when the caller omitted project_id.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _paginated(items, **envelope):
    """Build the `envelope()`-compatible response shape used by list endpoints."""
    return SimpleNamespace(
        results=items,
        total_count=envelope.get("total_count", len(items)),
        count=envelope.get("count", len(items)),
        next_cursor=envelope.get("next_cursor", None),
        prev_cursor=envelope.get("prev_cursor", None),
        next_page_results=envelope.get("next_page_results", None),
        prev_page_results=envelope.get("prev_page_results", None),
    )


def _workitem(labels, *, item_id="wi-1", state=None, project=None):
    """Build a minimal WorkItem-like object carrying only the fields under test.

    Uses a plain dict so callers can subscript (`item["labels"]`) and so
    `dump_results` in `paging.envelope` passes it through unchanged --
    SimpleNamespace items would force attribute access in every assertion.
    """
    return {"id": item_id, "labels": labels, "state": state, "project": project}


def _label_object(label_id, name, color="#cccccc"):
    """Mirror Plane's expanded Label object shape -- dict for subscriptable asserts."""
    return {"id": label_id, "name": name, "color": color}


def test_list_resolves_label_uuids_to_names(registered, spy):
    """list with a populated label map hands back {id, name} objects."""
    spy.returns["work_items.list"] = _paginated([_workitem(["l-1", "l-2"])])
    spy.returns["labels.list"] = _paginated(
        [_label_object("l-1", "bug"), _label_object("l-2", "feature")]
    )

    result = registered["workitem"].fn(action="list", project_id="p-1")

    enriched = result["results"][0]["labels"]
    assert enriched == [{"id": "l-1", "name": "bug"}, {"id": "l-2", "name": "feature"}], enriched


def test_list_keeps_name_equals_id_when_label_map_is_empty(registered, spy):
    """A failed labels.list (returns Auto -> empty results) must never lose an id."""
    spy.returns["work_items.list"] = _paginated([_workitem(["l-1", "l-2"])])
    # Default Auto() for labels.list -> falsy results, so the map comes back empty.
    # The behaviour: every UUID becomes {"id": u, "name": u}.
    result = registered["workitem"].fn(action="list", project_id="p-1")

    enriched = result["results"][0]["labels"]
    assert enriched == [{"id": "l-1", "name": "l-1"}, {"id": "l-2", "name": "l-2"}], enriched


def test_list_workspace_skips_label_resolution(registered, spy):
    """Workspace-scope lists have no project_id; enrichment must not call labels.list."""
    spy.returns["work_items.list_workspace"] = _paginated([_workitem(["l-1"])])

    result = registered["workitem"].fn(action="list")

    methods = [c.method for c in spy.recorder.calls]
    assert "labels.list" not in methods, methods
    assert result["results"][0]["labels"] == [{"id": "l-1", "name": "l-1"}]


def test_retrieve_passes_through_already_populated_names(registered, spy):
    """Already-resolved Label objects keep their own names -- no backfill needed."""
    item = _workitem([_label_object("l-1", "bug"), _label_object("l-2", "feature")])
    spy.returns["work_items.retrieve"] = item

    result = registered["workitem"].fn(action="retrieve", project_id="p-1", workitem_id="wi-1")

    assert result["labels"] == [
        {"id": "l-1", "name": "bug", "color": "#cccccc"},
        {"id": "l-2", "name": "feature", "color": "#cccccc"},
    ]
    # labels.list may or may not be called: the simpler implementation always
    # makes the call to populate the map. The contract is that the result is
    # untouched, not that the call is skipped.


def test_retrieve_backfills_empty_name_from_label_map(registered, spy):
    """An expanded object whose `name` is missing gets filled from the label map."""
    broken = {"id": "l-1", "name": "", "color": "#cccccc"}
    spy.returns["work_items.retrieve"] = _workitem([broken])
    spy.returns["labels.list"] = _paginated([_label_object("l-1", "bug")])

    result = registered["workitem"].fn(action="retrieve", project_id="p-1", workitem_id="wi-1")

    assert result["labels"] == [{"id": "l-1", "name": "bug", "color": "#cccccc"}]


def test_retrieve_by_identifier_resolves_project_from_response(registered, spy):
    """When project_id is omitted, retrieve_by_identifier pulls it from the item's `project`."""
    item = _workitem(["l-9"], project="p-from-response")
    spy.returns["work_items.retrieve_by_identifier"] = item
    spy.returns["labels.list"] = _paginated([_label_object("l-9", "picked-up")])

    result = registered["workitem"].fn(action="retrieve_by_identifier", workitem_identifier="ENG-1")

    assert result["labels"] == [{"id": "l-9", "name": "picked-up"}]
    # The labels.list call used the project_id resolved from the response, not "".
    labels_call = next(c for c in spy.recorder.calls if c.method == "labels.list")
    assert labels_call.kwargs["project_id"] == "p-from-response"


def test_manage_label_still_extracts_uuids_from_enriched_objects(registered, spy):
    """manage_label's read-modify-write must keep working when `labels` is already enriched."""
    enriched = SimpleNamespace(
        id="wi-1",
        labels=[{"id": "l-existing", "name": "bug"}],
    )
    spy.returns["work_items.retrieve"] = enriched

    registered["workitem"].fn(
        action="manage_label",
        project_id="p-1",
        workitem_id="wi-1",
        add_label_id='["l-new"]',
    )

    update = next(c for c in spy.recorder.calls if c.method == "work_items.update")
    written = update.kwargs["data"].labels
    assert written == ["l-existing", "l-new"], written


def test_list_archived_enriches_labels_just_like_list(registered, spy):
    """list_archived takes the same shape contract as list; verify it is wired in."""
    spy.returns["work_items.list_archived"] = _paginated([_workitem(["l-7"])])
    spy.returns["labels.list"] = _paginated([_label_object("l-7", "triage")])

    result = registered["workitem"].fn(action="list_archived", project_id="p-1")

    assert result["results"][0]["labels"] == [{"id": "l-7", "name": "triage"}]


def test_cycle_list_workitems_enriches_labels(registered, spy):
    """cycle.list_workitems routes through workitem_page; labels must be enriched there too."""
    spy.returns["cycles.list_work_items"] = _paginated([_workitem(["l-c"])])
    spy.returns["labels.list"] = _paginated([_label_object("l-c", "cycle-label")])

    result = registered["cycle"].fn(action="list_workitems", project_id="p-1", cycle_id="c-1")

    assert result["results"][0]["labels"] == [{"id": "l-c", "name": "cycle-label"}]


def test_module_list_workitems_enriches_labels(registered, spy):
    """module.list_workitems routes through workitem_page; labels must be enriched there too."""
    spy.returns["modules.list_work_items"] = _paginated([_workitem(["l-m"])])
    spy.returns["labels.list"] = _paginated([_label_object("l-m", "module-label")])

    result = registered["module"].fn(action="list_workitems", project_id="p-1", module_id="m-1")

    assert result["results"][0]["labels"] == [{"id": "l-m", "name": "module-label"}]


@pytest.mark.parametrize(
    ("labels_field", "expected"),
    [
        # None and missing: left alone, no AttributeError, no mutation.
        (None, None),
        # Empty list: stays empty.
        ([], []),
    ],
    ids=["none", "empty"],
)
def test_enrichment_handles_missing_or_empty_labels(registered, spy, labels_field, expected):
    """Items with no labels at all must not be touched -- enrichment is opt-in per row."""
    spy.returns["work_items.list"] = _paginated([_workitem(labels_field)])

    result = registered["workitem"].fn(action="list", project_id="p-1")

    assert result["results"][0]["labels"] == expected