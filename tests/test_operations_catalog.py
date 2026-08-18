from app.services.operations_catalog import list_operation_specs


def test_operation_catalog_lists_supported_operations() -> None:
    specs = list_operation_specs()

    assert {spec["name"] for spec in specs} == {
        "merge",
        "prepare_packet",
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


def test_prepare_packet_catalog_documents_ordering_and_input_count() -> None:
    specs = {spec["name"]: spec for spec in list_operation_specs()}
    packet = specs["prepare_packet"]
    order = next(
        parameter for parameter in packet["parameters"] if parameter["name"] == "order"
    )

    assert packet["input_count_min"] == 2
    assert packet["input_count_max"] is None
    assert order["default"] == "as_provided"
    assert order["allowed_values"] == ["as_provided", "filename", "manifest"]
    parameters = {parameter["name"]: parameter for parameter in packet["parameters"]}
    assert parameters["manifest"]["type"] == "array<object>"
    assert parameters["allow_unlisted"]["default"] is False
