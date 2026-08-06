from app.services.operations_catalog import list_operation_specs


def test_operation_catalog_lists_supported_operations() -> None:
    specs = list_operation_specs()

    assert {spec["name"] for spec in specs} == {
        "merge",
        "split",
        "ocr",
        "compress",
        "extract_text",
    }


def test_operation_catalog_returns_a_copy() -> None:
    specs = list_operation_specs()
    specs[0]["parameters"].append({"name": "mutated"})

    fresh_specs = list_operation_specs()

    assert {"name": "mutated"} not in fresh_specs[0]["parameters"]


def test_compress_catalog_documents_allowed_presets() -> None:
    specs = {spec["name"]: spec for spec in list_operation_specs()}
    preset = next(
        parameter
        for parameter in specs["compress"]["parameters"]
        if parameter["name"] == "preset"
    )

    assert preset["default"] == "ebook"
    assert preset["allowed_values"] == ["screen", "ebook", "print"]
