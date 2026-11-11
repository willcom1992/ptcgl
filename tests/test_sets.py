from ptcgl.data.sets import find_set, load_en_sets, sets_by_official_total


def test_sets_loaded_from_package_data() -> None:
    sets = load_en_sets()
    assert len(sets) > 40
    svi = find_set("svi")
    assert svi is not None and svi.official == 198 and svi.name == "Scarlet & Violet"


def test_unique_total_resolves_to_one_set() -> None:
    assert [s.code for s in sets_by_official_total(193)] == ["PAL"]


def test_colliding_totals_return_newest_first() -> None:
    codes = [s.code for s in sets_by_official_total(182)]
    assert codes[:2] == ["DRI", "PAR"]
    codes86 = {s.code for s in sets_by_official_total(86)}
    assert {"BLK", "WHT", "CRI"} <= codes86


def test_subset_prefix_filters() -> None:
    assert [s.code for s in sets_by_official_total(30, "TG")] == ["SIT", "LOR", "ASR", "BRS"]
    # 接頭辞なしの分母 30 には Trainer Gallery は含まれない（30th Classic Collection のみ）
    assert all(s.number_prefix == "" for s in sets_by_official_total(30))
    assert "BRS" not in {s.code for s in sets_by_official_total(30)}
