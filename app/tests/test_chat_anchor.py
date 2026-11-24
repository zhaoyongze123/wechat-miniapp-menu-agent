from __future__ import annotations

from menu_capture.chat_anchor import find_store_name_matches


def _row(text: str, x: float, y: float, width: float, height: float = 20) -> dict:
    return {"text": text, "x": x, "y": y, "width": width, "height": height}


def test_matches_four_observed_batch_ocr_failures():
    cases = [
        (
            "ZA0070005＿老胖串店＿济南市＿老胖串店中央厨房",
            [_row("ZAO070005_老胖串店二济南市_老胖串店中央厨房", 444, 604, 332)],
            "store_id+brand",
            624,
        ),
        (
            "ZA0070249＿徐志膜＿济南市＿徐志膜单店",
            [_row("ZA0070249_徐志膜二济南市_徐志膜单店", 500, 604, 276)],
            "store_id+brand",
            624,
        ),
        (
            "ZA0070551＿冒二三烤鸭＿大连市＿红烧尚（大连）餐饮管理有限公司单店",
            [
                _row("ZA0070551_冒二三烤鸭_大连市_红烧尚（大连）餐饮管理有", 376, 556, 398),
                _row("限公司单店", 378, 576, 70, 18),
            ],
            "store_id+brand",
            594,
        ),
        (
            "ZA0070751＿羊额烧鹅＿武汉市＿羊额烧鹅单店",
            [_row("ZA0070751_羊额烧鹅_武汉市二羊额烧鹅单店", 471.9, 601.9, 304.2, 22.7)],
            "store_id+brand",
            624.6,
        ),
    ]
    for source, rows, method, expected_bottom in cases:
        matches = find_store_name_matches(rows, source, width=864, image_height=958)
        assert len(matches) == 1
        assert matches[0]["match_method"] == method
        assert matches[0]["id_evidence"]["expected"] in source.casefold()
        assert matches[0]["helper_evidence"]["matched"] is True
        assert matches[0]["bottom"] == expected_bottom


def test_exact_store_id_survives_brand_ocr_error_and_reports_helper_evidence():
    source = "ZA0070751＿羊额烧鹅＿武汉市＿羊额烧鹅单店"
    matches = find_store_name_matches(
        [_row("ZA0070751_羊額燒鵝_武漢市_羊額燒鵝單店", 470, 400, 300)], source, 864, 958
    )
    assert len(matches) == 1
    assert matches[0]["match_method"] == "store_id_only"
    assert matches[0]["id_evidence"]["normalized_observed"] == "za0070751"
    assert matches[0]["helper_evidence"]["matched"] is False


def test_ocr_digit_confusions_are_corrected_only_inside_fixed_width_id():
    source = "ZA0070005＿老胖串店＿济南市＿单店"
    matches = find_store_name_matches(
        [_row("ZA〇〇7〇〇〇5_老胖串店", 400, 300, 250)], source, 864, 958
    )
    assert len(matches) == 1
    assert len(matches[0]["id_evidence"]["digit_confusions"]) == 5


def test_right_aligned_wrapped_name_rows_combine():
    source = "ABC12345＿右侧烧烤＿城市＿单店"
    matches = find_store_name_matches(
        [
            _row("ABC12345_右侧烧烤_城", 500, 200, 100),
            _row("市_右侧烧烤单店", 540, 220, 60, 18),
        ],
        source,
        864,
        958,
    )
    assert len(matches) == 1
    assert matches[0]["bottom"] == 238
    assert matches[0]["helper_evidence"]["matched"] is True


def test_does_not_match_same_brand_with_wrong_store_code():
    source = "ZA0070005＿老胖串店＿济南市＿单店"
    rows = [
        _row("ZA0070006_老胖串店_济南市_单店", 400, 200, 300),
        _row("ZA0070249_老胖串店_济南市_单店", 400, 300, 300),
    ]
    assert find_store_name_matches(rows, source, 864, 958) == []


def test_name_similarity_fallback_tolerates_partial_code_and_ocr_omissions():
    source = "ZA0070005＿老胖串店＿济南市＿单店"
    matches = find_store_name_matches(
        [_row("ZA007000S 老胖串店 济南单店", 400, 200, 260)], source, 864, 958
    )
    assert len(matches) == 1
    assert matches[0]["match_method"] == "normalized_name_similarity"
    assert matches[0]["helper_evidence"]["similarity"] >= 0.82
    assert matches[0]["helper_evidence"]["brand_similarity"] >= 0.8

    partial_code = find_store_name_matches(
        [_row("ZA007005_老胖串店_济南市_单店", 400, 300, 260)], source, 864, 958
    )
    assert len(partial_code) == 1
    assert partial_code[0]["match_method"] == "normalized_name_similarity"


def test_name_fallback_rejects_complete_other_code_even_with_same_brand():
    source = "ZA0070005＿老胖串店＿济南市＿单店"
    rows = [_row("ZA0070006_老胖串店_济南市_单店", 400, 200, 260)]
    assert find_store_name_matches(rows, source, 864, 958) == []


def test_name_fallback_rejects_clearly_different_store():
    source = "ZA0070005＿老胖串店＿济南市＿单店"
    rows = [_row("ZA007000S_海底捞_上海市_旗舰店", 400, 200, 260)]
    assert find_store_name_matches(rows, source, 864, 958) == []


def test_name_fallback_preserves_duplicate_anchors_for_same_store_name():
    source = "ZA0070005＿老胖串店＿济南市＿单店"
    rows = [
        _row("ZA007000S_老胖串店_济南市_单店", 400, 120, 260),
        _row("ZA007000S_老胖串店_济南市_单店", 450, 600, 260),
    ]
    matches = find_store_name_matches(rows, source, 864, 958)
    assert len(matches) == 2
    assert [match["y"] for match in matches] == [120, 600]


def test_filters_sidebar_draft_and_rows_outside_chat_vertical_region():
    source = "ZA0070005＿老胖串店＿济南市＿单店"
    rows = [
        _row("ZA0070005_老胖串店", 100, 200, 200),  # sidebar
        _row("ZA0070005_老胖串店", 400, 900, 200),  # composer/draft area
    ]
    assert find_store_name_matches(rows, source, 864, 958) == []


def test_returns_duplicate_store_anchors_so_caller_can_select_latest():
    source = "ZA0070005＿老胖串店＿济南市＿单店"
    matches = find_store_name_matches(
        [_row("ZA0070005_老胖串店", 400, 120, 220), _row("ZA0070005_老胖串店", 450, 600, 220)],
        source,
        864,
        958,
    )
    assert [match["bottom"] for match in matches] == [140, 620]
    assert max(matches, key=lambda match: match["bottom"])["y"] == 600


def test_does_not_extend_anchor_across_distant_bubble():
    source = "ZA0070005＿老胖串店＿济南市＿单店"
    matches = find_store_name_matches(
        [
            _row("ZA0070005_老胖串店", 400, 200, 220),
            _row("另一个消息", 400, 240, 100),
        ],
        source,
        864,
        958,
    )
    assert len(matches) == 1
    assert matches[0]["text"] == "ZA0070005_老胖串店"
    assert matches[0]["bottom"] == 220


def test_legacy_unnumbered_source_keeps_normalized_full_label_matching():
    source = "老店＿城市单店"
    matches = find_store_name_matches(
        [_row("老店_城市 单店", 400, 200, 160)], source, 864, 958
    )
    assert len(matches) == 1
    assert matches[0]["match_method"] == "normalized_full_label"
