"""統計時系列コレクタのテスト (§6, §9, §13 / Phase 5)。

統計は記事と壊れ方が違う。ここで守りたいのは次の 4 点。
  - 数値は単位と対象期間を伴って構造のまま入ること（本文から拾い直さない）
  - 同じ数値を毎日取り直しても観測が増えないこと（同一性が安定していること）
  - 速報値が改訂されたら、上書きではなく新しい観測として残ること
  - 取得できていないことを「その期間の統計が無い」と取り違えないこと

  python3 tests/test_series.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from future100 import config, normalize  # noqa: E402
from future100.collect import base, json_series  # noqa: E402

WORLD_BANK_SOURCE = {
    "source_id": "src_test_stats",
    "name": "テスト統計",
    "publisher": "Test Statistics Office",
    "tier": "tier1",
    "reliability": "A",
    "kind": "json_series",
    "categories": ["macro"],
    "series_request": {
        "format": "records",
        "endpoint_template": "https://example.org/v2/{code}?date={start_year}:{end_year}",
        "point_url_template": "https://example.org/indicator/{code}?locations={geo}&period={period}",
        "points_path": "1",
        "period": "date",
        "value": "value",
        "label": "indicator.value",
        "key_fields": {"geo": "countryiso3code"},
        "updated_path": "0.lastupdated",
    },
    "series": [{"code": "NE.GDI.FTOT.CD", "geo": "JP", "unit": "USD"}],
}


def _payload(value=7426675490111.76):
    return [
        {"page": 1, "lastupdated": "2026-07-13"},
        [
            {"indicator": {"id": "NE.GDI.FTOT.CD", "value": "Gross fixed capital formation"},
             "countryiso3code": "JPN", "date": "2024", "value": value},
            {"indicator": {"id": "NE.GDI.FTOT.CD", "value": "Gross fixed capital formation"},
             "countryiso3code": "JPN", "date": "2023", "value": 1234.5},
            {"indicator": {"id": "NE.GDI.FTOT.CD", "value": "Gross fixed capital formation"},
             "countryiso3code": "JPN", "date": "2022", "value": None},
        ],
    ]


def _collect(source: dict, payload) -> list[dict]:
    """http_get を差し替えて 1 回分の取得を再現する。"""
    original = base.http_get
    base.http_get = lambda url, **kwargs: json.dumps(payload).encode("utf-8")
    try:
        return list(json_series.collect_series(source))
    finally:
        base.http_get = original


# --- 取り込みの形 ----------------------------------------------------------

def test_numbers_keep_their_unit_and_period():
    """数値は単位と対象期間を伴って構造のまま入る (§9, §35-38)。"""
    documents = _collect(WORLD_BANK_SOURCE, _payload())
    assert len(documents) == 2, "欠測 (value=None) を 0 として保存しない"

    quantity = documents[0]["content"]["quantities"][0]
    assert quantity["value"] == 7426675490111.76
    assert quantity["unit"] == "USD"
    assert quantity["period"] == "2024"
    assert "Gross fixed capital formation" in quantity["label"]
    assert "JPN" in quantity["label"], "系列名に区分を織り込む（国別の値が 1 本に見えてはいけない）"


def test_observation_time_is_not_the_period():
    """対象期間 (2024年) と、その数値を観測できた時点は別物 (§37)。"""
    document = _collect(WORLD_BANK_SOURCE, _payload())[0]
    assert document["published_at"] == "2026-07-13T00:00:00Z", "データセットの公表時点"
    assert document["content"]["quantities"][0]["period"] == "2024", "対象期間は quantities 側"
    assert document["observed_at"] > document["published_at"], "観測は公表より後"


def test_repeated_collection_does_not_create_new_observations():
    """毎日取り直しても、値が変わらない限り観測は増えない。

    取得 URL には期間指定が入るため、URL をそのまま同一性に使うと年が変わるたびに
    同じ数値が新しい観測として湧く。
    """
    first = _collect(WORLD_BANK_SOURCE, _payload())
    second = _collect(WORLD_BANK_SOURCE, _payload())
    assert [d["raw_id"] for d in first] == [d["raw_id"] for d in second]


def test_revision_is_recorded_as_a_new_observation():
    """速報値の改訂は上書きではなく別の観測として残す。

    改訂を既取得として捨てると、「いつ何が上方修正されたか」が消える。
    """
    original = _collect(WORLD_BANK_SOURCE, _payload())[0]
    revised = _collect(WORLD_BANK_SOURCE, _payload(value=7500000000000.0))[0]
    assert original["raw_id"] != revised["raw_id"], "改訂は新しい観測"
    assert original["fetch"]["canonical_url"] == revised["fetch"]["canonical_url"], \
        "同じ系列・同じ期間なので重複判定では同じ対象として繋がる (§38)"


def test_each_period_is_a_distinct_observation():
    documents = _collect(WORLD_BANK_SOURCE, _payload())
    urls = {d["fetch"]["canonical_url"] for d in documents}
    assert len(urls) == len(documents), "期間ごとに別の観測になる"


# --- JSON-stat (Eurostat) ---------------------------------------------------

def _jsonstat_source():
    return {
        "source_id": "src_test_jsonstat",
        "name": "テスト JSON-stat",
        "tier": "tier1", "reliability": "A", "kind": "json_series",
        "series_request": {
            "format": "jsonstat",
            "endpoint_template": "https://example.org/data/{dataset}?{query}",
            "point_url_template": "https://example.org/view/{dataset}?keys={keys}&period={period}",
            "period": "time",
            "updated_path": "updated",
        },
        "series": [{"dataset": "nrg_cb_pem", "query": "geo=EU", "unit": "GWh"}],
    }


def _jsonstat_payload():
    """siec 2 種 × time 3 期。平坦化添字を座標に戻せているかを見るため、あえて疎にする。"""
    return {
        "label": "Net electricity generation",
        "updated": "2026-08-10T23:00:00+0200",
        "id": ["siec", "time"],
        "size": [2, 3],
        "dimension": {
            "siec": {"category": {"index": {"TOTAL": 0, "RA000": 1},
                                  "label": {"TOTAL": "Total", "RA000": "Renewables"}}},
            "time": {"category": {"index": {"2026-04": 0, "2026-05": 1, "2026-06": 2}}},
        },
        # 添字 0=(TOTAL,2026-04), 2=(TOTAL,2026-06), 4=(RA000,2026-05)
        "value": {"0": 100.0, "2": 120.0, "4": 55.0},
    }


def test_jsonstat_index_maps_back_to_period_and_category():
    documents = _collect(_jsonstat_source(), _jsonstat_payload())
    seen = {
        (d["content"]["quantities"][0]["period"], d["content"]["quantities"][0]["value"])
        for d in documents
    }
    assert seen == {("2026-04", 100.0), ("2026-06", 120.0), ("2026-05", 55.0)}

    renewable = next(d for d in documents if d["content"]["quantities"][0]["value"] == 55.0)
    assert "Renewables" in renewable["content"]["title"], "どの区分の数値かを取り違えない"
    assert "RA000" in renewable["fetch"]["canonical_url"]


# --- 取得失敗を 0 件と取り違えない ------------------------------------------

def test_body_level_failure_is_not_treated_as_no_data():
    """HTTP 200 のまま本文で失敗を告げる API がある（BLS の日次上限超過）。

    これを 0 件として扱うと、取得できていない期間が「統計が無かった」として静かに残る。
    """
    source = {
        "source_id": "src_test_quota", "name": "テスト", "tier": "tier1", "reliability": "A",
        "kind": "json_series",
        "series_request": {
            "format": "records",
            "endpoint_template": "https://example.org/{code}",
            "series_path": "Results.series", "points_path": "data",
            "period": ["year", "period"], "value": "value",
            "status_path": "status", "status_ok": ["REQUEST_SUCCEEDED"],
        },
        "series": [{"code": "CES3133400001"}],
    }
    payload = {"status": "REQUEST_NOT_PROCESSED",
               "message": ["daily threshold ... has been reached"], "Results": {}}
    try:
        _collect(source, payload)
    except base.FetchError as exc:
        assert "REQUEST_NOT_PROCESSED" in exc.reason and "threshold" in exc.reason
        return
    raise AssertionError("本文レベルの失敗を取得失敗として報告すべき")


def test_missing_api_key_fails_with_instructions():
    """鍵が要るソースは、偽の値を埋めずに設定漏れとして失敗させる。"""
    import os

    source = {
        "source_id": "src_test_key", "name": "テスト", "tier": "tier1", "reliability": "A",
        "kind": "json_series",
        "series_request": {
            "format": "records",
            "endpoint_template": "https://example.org/data?api_key=${FUTURE100_TEST_MISSING_KEY}",
            "points_path": "data", "period": "period", "value": "value",
        },
        "series": [{}],
    }
    os.environ.pop("FUTURE100_TEST_MISSING_KEY", None)
    try:
        _collect(source, {"data": []})
    except base.FetchError as exc:
        assert "FUTURE100_TEST_MISSING_KEY" in exc.reason
        return
    raise AssertionError("未設定の環境変数は取得失敗として報告すべき")


# --- 後段への受け渡し -------------------------------------------------------

def test_quantities_survive_normalization():
    """正規化で数値が落ちると、市場規模の根拠に統計を使えない (§9)。"""
    document = _collect(WORLD_BANK_SOURCE, _payload())[0]
    from future100 import dedup

    event = normalize.normalize_document(document, WORLD_BANK_SOURCE, dedup.ClusterIndex.for_date("2026-07-13"))
    assert event["quantities"] == document["content"]["quantities"]
    assert event["category"] == "macro", "ソース定義の領域に落ちる"


def test_statistics_do_not_raise_weak_signals():
    """集計値は「出来事が 1 件起きたこと」ではない (§10)。

    「特許出願件数」という系列名にキーワード判定をかけると、年次統計を 1 本
    取り込んだだけで特許シグナルが 12 件立ち上がる（実測でそうなった）。
    """
    from future100 import dedup

    source = dict(WORLD_BANK_SOURCE)
    payload = _payload()
    for record in payload[1]:
        record["indicator"]["value"] = "Patent applications, residents"

    document = _collect(source, payload)[0]
    event = normalize.normalize_document(document, source, dedup.ClusterIndex.for_date("2026-07-13"))
    assert event["signal_types"] == [], "統計からキーワードでシグナルを立てない"
    assert event["category"] == "macro"

    # ソース定義で明示した種別は統計でも有効（判定を止めるのはキーワードだけ）
    source = dict(source, default_signal_types=["government_budget"])
    event = normalize.normalize_document(document, source, dedup.ClusterIndex.for_date("2026-07-13"))
    assert event["signal_types"] == ["government_budget"]


def test_documents_and_events_match_their_schemas():
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError:
        print("    (skipped: jsonschema not installed)")
        return

    registry = Registry()
    for path in (ROOT / "schemas").glob("*.schema.json"):
        registry = registry.with_resource(path.name, Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))))

    from future100 import dedup

    document = _collect(WORLD_BANK_SOURCE, _payload())[0]
    event = normalize.normalize_document(document, WORLD_BANK_SOURCE, dedup.ClusterIndex.for_date("2026-07-13"))
    for payload, name in ((document, "raw_document"), (event, "event")):
        schema = json.loads((ROOT / f"schemas/{name}.schema.json").read_text(encoding="utf-8"))
        errors = list(Draft202012Validator(schema, registry=registry).iter_errors(payload))
        assert not errors, f"{name}: {errors[0].message}"


def test_registered_statistics_sources_are_renderable():
    """登録した統計ソースのテンプレートが、系列定義だけで埋まること。

    変数の綴り違いは取得時まで気づけない。ここで先に落とす。
    """
    sources = [s for s in config.load_sources(enabled_only=False) if s["kind"] == "json_series"]
    assert sources, "統計ソースが 1 つも登録されていない"

    for source in sources:
        spec = source["series_request"]
        assert spec["format"] in json_series._PARSERS, f"{source['source_id']}: 未知の format"
        for entry in source["series"]:
            for key in ("endpoint_template", "point_url_template"):
                template = spec.get(key)
                if not template:
                    continue
                # テンプレートに入るのは 系列定義 ∪ データ点の区分 ∪ {period, keys}
                fields = dict(entry)
                for name in (spec.get("key_fields") or {}):
                    fields.setdefault(name, "K")
                for name in (entry.get("fields") or {}):
                    fields.setdefault(name, "K")
                fields.setdefault("tenor", "K")
                fields.setdefault("period", "2026-01")
                fields.setdefault("keys", "K")
                # 環境変数を要求するソースは、鍵が無いことだけを理由に失敗するのが正しい
                try:
                    json_series._render(template, fields, source)
                except base.FetchError as exc:
                    assert "環境変数" in exc.reason, f"{source['source_id']}.{key}: {exc.reason}"
                    assert source.get("requires_credentials"), \
                        f"{source['source_id']}: 環境変数を要求するなら requires_credentials を立てる"


def test_odata_xml_splits_one_row_into_named_series():
    """1 つの entry が全期間の値を持つ形式（米財務省の利回り曲線）。

    期間ごとに要求を分けると同じ数百 KB を期間の数だけ取り直すことになるので、
    1 回の取得から名前つきで複数系列を切り出せること。
    """
    source = {
        "source_id": "src_test_odata", "name": "テスト", "tier": "tier1", "reliability": "A",
        "kind": "json_series",
        "series_request": {
            "format": "odata_xml",
            "endpoint_template": "https://example.org/xml?year={end_year}",
            "point_url_template": "https://example.org/rates?tenor={keys}&period={period}",
            "period": "NEW_DATE",
        },
        "series": [{"label": "米国債利回り", "unit": "%",
                    "fields": {"10年": "BC_10YEAR", "30年": "BC_30YEAR"}}],
    }
    xml = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
  <entry><content><m:properties xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <d:NEW_DATE>2026-08-19T00:00:00</d:NEW_DATE>
    <d:BC_10YEAR>4.65</d:BC_10YEAR>
    <d:BC_30YEAR>5.19</d:BC_30YEAR>
  </m:properties></content></entry>
</feed>"""

    original = base.http_get
    base.http_get = lambda url, **kwargs: xml.encode("utf-8")
    try:
        documents = list(json_series.collect_series(source))
    finally:
        base.http_get = original

    assert len(documents) == 2, "1 行から 2 系列を切り出す"
    values = {d["content"]["quantities"][0]["label"]: d["content"]["quantities"][0] for d in documents}
    assert values["米国債利回り 10年"]["value"] == 4.65
    assert values["米国債利回り 30年"]["value"] == 5.19
    assert values["米国債利回り 30年"]["period"] == "2026-08-19", "日時は日付まで丸める"
    assert len({d["fetch"]["canonical_url"] for d in documents}) == 2, "系列ごとに別の観測になる"


def test_datetime_periods_are_truncated_to_the_day():
    """入札日 2026-08-19T00:00:00 の 00:00 は「その日」以上の情報を持たない。
    残すと同じ日が別の期間として並ぶ。"""
    assert json_series._period({"d": "2026-08-19T00:00:00"}, "d") == "2026-08-19"
    assert json_series._period({"y": "2026", "p": "M07"}, ["y", "p"]) == "2026-M07", "月次の表記は壊さない"


def test_poll_interval_never_skips_a_daily_source():
    """取得間隔は「少なくともこの頻度で」の意味に取る。

    日次ジョブの起動時刻は数十分ずれる。間隔をそのまま締切にすると、23 時間 40 分で
    起きた日に日次ソースを丸ごと落とす。落とした日は観測履歴に穴が開き、
    baseline の充足がその日数だけ後ろにずれる (§10)。
    """
    daily = {"source_id": "src_daily", "poll_interval_minutes": 1440}
    weekly = {"source_id": "src_weekly", "poll_interval_minutes": 10080}

    now = "2026-08-14T21:40:00Z"
    assert config.is_due(daily, None, now=now), "一度も取っていなければ必ず取る"
    assert config.is_due(daily, "2026-08-13T22:00:00Z", now=now), "23 時間 40 分でも日次ソースは取る"
    assert not config.is_due(daily, "2026-08-14T03:00:00Z", now=now), "同じ日に取り直しはしない"
    assert not config.is_due(weekly, "2026-08-13T22:00:00Z", now=now), "週次ソースは翌日には取り直さない"
    assert config.is_due(weekly, "2026-08-07T22:00:00Z", now=now), "週次ソースは 1 週間後に取る"

    hourly = {"source_id": "src_hourly", "poll_interval_minutes": 360}
    assert config.is_due(hourly, "2026-08-14T21:00:00Z", now=now), "1 日未満の間隔は日次サイクルでは常に満たされる"


def main() -> int:
    failures = 0
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
            print(f"  PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print("all tests passed" if not failures else f"{failures} test(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
