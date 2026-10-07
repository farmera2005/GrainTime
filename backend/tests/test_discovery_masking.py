from graintime.collector.discovery import mask_value


def test_free_text_in_generic_columns_is_masked():
    assert mask_value("tid_Id", "EVERETT FARMS KENWORTH", True).startswith("<masked")
    assert mask_value("tid_tagid", "E28011700000021559F7DAA2", True).startswith("<masked")
    assert mask_value("tid_driver", "J", True).startswith("<masked")          # name-like column


def test_codes_and_descriptions_are_kept():
    assert mask_value("trt_code", "TRUCKIN", True) == "TRUCKIN"
    assert mask_value("prd_description", "Yellow Corn #2", True) == "Yellow Corn #2"
    assert mask_value("tid_status", "1", True) == "1"
    assert mask_value("tid_LoadNumber", "MPS13620", True) == "MPS13620"
    assert mask_value("tid_Id", "EVERETT FARMS", False) == "EVERETT FARMS"   # masking off
    assert mask_value("tid_price", 4.12, True) == 4.12
