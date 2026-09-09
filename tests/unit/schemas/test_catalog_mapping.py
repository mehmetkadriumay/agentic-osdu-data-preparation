from __future__ import annotations

import pytest

from agentic_osdu.schemas.catalog import (
    KindParts,
    SchemaCatalogError,
    parse_kind,
    replace_namespace_placeholders,
    replace_schema_authority,
    schema_relative_path,
    walk_references,
)
from agentic_osdu.schemas.validate import (
    json_pointer,
    normalize_record_surrogates,
    surrogate_id_map,
)


@pytest.mark.parametrize(
    ("kind", "relative"),
    [
        ("osdu:wks:Manifest:1.0.0", "manifest/Manifest.1.0.0.json"),
        ("osdu:wks:master-data--Well:1.2.3", "master-data/Well.1.2.3.json"),
        (
            "osdu:wks:work-product-component--GenericRepresentation:2.0.0",
            "manifest/GenericRepresentation.2.0.0.json",
        ),
        ("osdu:wks:AbstractLegalParentList:1.0.0", "abstract/AbstractLegalParentList.1.0.0.json"),
    ],
)
def test_exact_kind_parsing_and_path_mapping(kind: str, relative: str) -> None:
    assert parse_kind(kind).version == kind.rsplit(":", 1)[1]
    assert schema_relative_path(kind) == relative


@pytest.mark.parametrize(
    "kind",
    [
        "missing-segments",
        "osdu:wks:master-data--Well:1.0",
        "osdu:wks:Unknown:1.0.0",
        "osdu/wks/Manifest/1.0.0",
    ],
)
def test_invalid_or_unmappable_kinds_fail_closed(kind: str) -> None:
    with pytest.raises(SchemaCatalogError, match="SCHEMA_UNAVAILABLE"):
        schema_relative_path(kind)


def test_reference_walking_and_placeholders_are_recursive_and_non_mutating() -> None:
    value = {
        "$id": "{{schema-authority}}:wks:Abstract:1.0.0",
        "properties": {
            "ref": {"$ref": "{{NAMESPACE}}:wks:master-data--Well:1.0.0"},
            "local": {"$ref": "#/$defs/local"},
        },
    }
    replaced = replace_namespace_placeholders(
        replace_schema_authority(value, "tenant"),
        "tenant",
    )
    assert tuple(walk_references(replaced)) == ("tenant:wks:master-data--Well:1.0.0",)
    assert replaced["$id"] == "tenant:wks:Abstract:1.0.0"
    assert value["$id"] == "{{schema-authority}}:wks:Abstract:1.0.0"


def test_surrogate_normalization_and_json_pointer_parity() -> None:
    records = [
        (
            "/Data/WorkProduct",
            {
                "id": "surrogate-key:wp",
                "kind": "tenant:wks:master-data--Well:1.0.0",
            },
        )
    ]
    replacements = surrogate_id_map(records)
    normalized = normalize_record_surrogates(
        {"id": "surrogate-key:wp", "data": {"Parent": "surrogate-key:wp"}},
        replacements,
    )
    assert normalized["id"] == "tenant:master-data--Well:schema-validation-1"
    assert normalized["data"]["Parent"] == "tenant:master-data--Well:schema-validation-1:0"
    assert json_pointer(("a/b", "~key", 0)) == "/a~1b/~0key/0"


def test_kind_parts_and_reference_walk_cover_scalar_and_list_values() -> None:
    assert parse_kind("tenant:wks:Manifest:1.0.0") == KindParts(
        authority="tenant",
        source="wks",
        entity="Manifest",
        version="1.0.0",
    )
    assert tuple(walk_references([1, "value", {"$ref": "#/local"}])) == ()
    assert replace_schema_authority(7, "tenant") == 7
    assert replace_namespace_placeholders(["{{NAMESPACE}}", None], "tenant") == [
        "tenant",
        None,
    ]
