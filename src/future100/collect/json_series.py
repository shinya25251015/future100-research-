"""統計時系列コレクタ (§6, §9, §13 / Phase 5)。

記事と統計は別物である。記事は「1 文書 = 1 出来事」だが、統計は「1 系列 = 期間ごとの
数値の列」で、同じ URL の中身が毎月少しずつ増え、ときに過去の値が改訂される。
本文に数字を書き込んで後から拾い直すと単位と期間が失われるため、
1 データ点を 1 RawDocument とし、数値は content.quantities に構造のまま保存する。

守っている規約:
  - 観測の同一性は「系列 + 期間 + 値」で決める。取得 URL には期間指定が入るため、
    URL をそのまま同一性に使うと毎年新しい観測が湧く。
  - 速報値の改訂は別の観測として残す（値まで同一性に含める）。上書きはしない。
  - 対象期間は quantities[].period に置き、event_at には使わない。
    その数値を観測できるようになったのは公表された時点である (§37)。

ソース定義:

  "series_request": {
    "format": "records" | "jsonstat" | "odata_xml",
    "endpoint_template": "https://.../{code}?date={start_year}:{end_year}",
    "point_url_template": "https://.../{code}#{geo}/{period}",  観測の同一性を決める安定 URL
    "points_path": "1",              records: データ点配列へのパス（数字は配列添字）
    "series_path": "Results.series", records: 系列の配列（入れ子のとき）
    "period": "date" | ["year", "period"],
    "value": "value",
    "label": "indicator.value",      無ければ series 定義の label
    "key_fields": {"geo": "countryiso3code"},
    "note_fields": ["obs_status"],
    "updated_path": "0.lastupdated"  データセットの更新時刻（published_at に使う）
  },
  "series": [ {"code": "...", "geo": "...", "unit": "USD", "label": "..."} ]
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from .. import timeutil
from . import base

COLLECTOR = "json_series@1"
_DATETIME_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T")


@dataclass
class Point:
    period: str
    value: float
    label: str
    unit: str
    keys: dict[str, str] = field(default_factory=dict)
    note: str = ""


@base.register("json_series")
def collect_series(source: dict) -> Iterator[dict]:
    """ソース定義の series を 1 つずつ取得する。

    1 系列の失敗で他の系列を捨てない。全滅したときだけソース単位の失敗として送出する。
    """
    spec = source.get("series_request") or {}
    parse = _PARSERS.get(spec.get("format", "records"))
    if parse is None:
        raise base.FetchError(source["source_id"], f"unknown series format: {spec.get('format')!r}")

    observed_at = timeutil.now_str()
    user_agent = base.resolve_user_agent(source)
    produced = 0
    errors: list[str] = []

    for entry in source.get("series") or [{}]:
        try:
            url = _render(spec["endpoint_template"], entry, source)
            payload = base.http_get(url, timeout=source.get("timeout", base.DEFAULT_TIMEOUT), user_agent=user_agent)
            document = ET.fromstring(payload) if spec["format"] == "odata_xml" else json.loads(payload)
            _assert_status(document, spec)
            published_at = _updated_at(document, spec.get("updated_path"))
            for point in parse(document, spec, entry):
                produced += 1
                yield _make_document(source, spec, entry, point, url, published_at, observed_at)
        except base.FetchError as exc:
            errors.append(exc.reason)
        except (json.JSONDecodeError, ET.ParseError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"{entry.get('code', entry.get('dataset', '?'))}: {type(exc).__name__}: {exc}")

    if not produced and errors:
        raise base.FetchError(source["source_id"], "; ".join(errors[:3]))


def _make_document(source: dict, spec: dict, entry: dict, point: Point,
                   request_url: str, published_at: str | None, observed_at: str) -> dict:
    # {keys} は区分をまとめた文字列。データセットごとに次元名が違う統計でも
    # 同じ point_url_template を書けるようにするための逃げ道。
    fields = {**entry, **point.keys, "period": point.period, "keys": "/".join(point.keys.values())}
    point_url = _render(spec.get("point_url_template") or spec["endpoint_template"], fields, source)
    quantity = {
        "label": point.label,
        "value": point.value,
        "unit": point.unit,
        "scale": entry.get("scale", "one"),
        "period": point.period,
    }
    body_lines = [
        f"{point.label}",
        f"期間: {point.period}",
        f"値: {_format_value(point.value)} {point.unit}",
    ]
    if point.keys:
        body_lines.append("区分: " + ", ".join(f"{k}={v}" for k, v in point.keys.items()))
    if point.note:
        body_lines.append(f"注記: {point.note}")
    body_lines.append(f"取得元: {request_url}")

    document = base.make_raw_document(
        source=source,
        url=point_url,
        title=f"{point.label} {point.period}: {_format_value(point.value)} {point.unit}",
        body="\n".join(body_lines),
        published_at=published_at,
        method="json_api",
        collector=COLLECTOR,
        observed_at=observed_at,
        # 改訂を別の観測として残すため、値まで含めて同一性を取る
        identity=f"{point_url}|{point.value}",
        quantities=[quantity],
    )
    # 統計の系列名にセクター名は現れない（「Production in industry, C27」が電力・送配電に
    # 効くことは文面からは読めない）。登録時に宣言したセクターをそのまま持たせる (§11)。
    if entry.get("sectors"):
        document["sector_ids"] = list(entry["sectors"])
    return document


# --- 形式ごとの解釈 --------------------------------------------------------

def _records(document: Any, spec: dict, entry: dict) -> Iterator[Point]:
    """データ点が辞書の配列で返る形式（World Bank, BLS, EIA など）。"""
    containers = [document]
    if spec.get("series_path"):
        series = _dig(document, spec["series_path"])
        containers = series if isinstance(series, list) else []

    for container in containers:
        points = _dig(container, spec.get("points_path")) if spec.get("points_path") else container
        if not isinstance(points, list):
            continue
        for item in points:
            # value / label は系列定義で上書きできる。同じ応答から別の指標を取り出す
            # ため（入札結果の応札倍率と最高利回りなど）に要る。
            value = _number(_dig(item, entry.get("value") or spec.get("value", "value")))
            period = _period(item, spec.get("period", "date"))
            if value is None or not period:
                continue  # 欠測は数値として保存しない（0 と区別できなくなる）
            keys = {name: _text(_dig(item, path)) for name, path in (spec.get("key_fields") or {}).items()}
            label = _text(_dig(item, spec.get("label"))) or entry.get("label", "")
            # 系列名に入れる区分は label_fields で絞れる。識別のためだけに要る区分
            # （入札の CUSIP など）を名前に入れると、1 点しかない系列が銘柄の数だけ並ぶ。
            named = spec.get("label_fields")
            detail = ", ".join(
                value for name, value in keys.items() if value and (named is None or name in named)
            )
            yield Point(
                period=period,
                value=value,
                # 系列名に区分（国など）を織り込む。後段は quantities.label で系列を束ねるため、
                # ここで区別を落とすと 7 か国の値が 1 本の系列に見える。
                label=f"{label}（{detail}）" if detail else label,
                unit=entry.get("unit") or _text(_dig(item, spec.get("unit_path"))) or "",
                keys=keys,
                note=" ".join(filter(None, (_text(_dig(item, p)) for p in spec.get("note_fields") or []))),
            )


def _jsonstat(document: Any, spec: dict, entry: dict) -> Iterator[Point]:
    """JSON-stat 形式（Eurostat）。

    値は「全次元の直積を平坦化した添字」をキーにした疎な辞書で返る。
    添字を次元座標へ戻さないと、どの期間・どの区分の数値なのかが分からない。
    """
    sizes = document.get("size") or []
    dimension_ids = document.get("id") or []
    if not sizes or len(sizes) != len(dimension_ids):
        return

    # 次元 ID → 添字 → コード（例 time: 0 -> "2025-07"）
    labels: dict[str, list[str]] = {}
    captions: dict[str, dict[str, str]] = {}
    for dim_id in dimension_ids:
        category = (document.get("dimension", {}).get(dim_id) or {}).get("category", {})
        index = category.get("index") or {}
        codes = sorted(index, key=index.get) if isinstance(index, dict) else list(index)
        labels[dim_id] = codes
        captions[dim_id] = category.get("label") or {}

    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    dataset_label = _text(document.get("label")) or entry.get("label", "")
    for flat, value in (document.get("value") or {}).items():
        number = _number(value)
        if number is None:
            continue
        remainder = int(flat)
        coordinates: dict[str, str] = {}
        for dim_id, stride in zip(dimension_ids, strides):
            position, remainder = divmod(remainder, stride)
            codes = labels.get(dim_id) or []
            coordinates[dim_id] = codes[position] if position < len(codes) else str(position)

        period = coordinates.pop(spec.get("period", "time"), "")
        if not period:
            continue
        unit = entry.get("unit") or captions.get("unit", {}).get(coordinates.get("unit", ""), "") or coordinates.get("unit", "")
        detail = ", ".join(
            captions.get(dim, {}).get(code, code)
            for dim, code in coordinates.items()
            if dim not in ("freq", "unit")
        )
        yield Point(
            period=period,
            value=number,
            label=f"{dataset_label}（{detail}）" if detail else dataset_label,
            unit=unit,
            keys={k: v for k, v in coordinates.items() if k not in ("freq",)},
        )


_ATOM = "{http://www.w3.org/2005/Atom}"
_ODATA = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"


def _odata_xml(document: Any, spec: dict, entry: dict) -> Iterator[Point]:
    """OData の Atom XML（米財務省の利回り曲線など）。

    1 つの entry が 1 日ぶんの全期間の値を持つ形なので、系列定義の fields で
    「どの列を取り出すか」を名前つきで指定する。1 回の取得で複数系列を作れる
    （期間ごとに要求を分けると、同じ数百 KB を期間の数だけ取り直すことになる）。
    """
    fields = entry.get("fields") or {}
    period_field = spec.get("period", "NEW_DATE")
    for element in document.iter(f"{_ATOM}entry"):
        period = _text(_child_text(element, period_field))[:10]
        if not period:
            continue
        for name, column in fields.items():
            value = _number(_child_text(element, column))
            if value is None:
                continue
            yield Point(
                period=period,
                value=value,
                label=f"{entry.get('label', '')} {name}".strip(),
                unit=entry.get("unit", ""),
                keys={"tenor": name},
            )


def _child_text(element: Any, name: str) -> str:
    node = element.find(f".//{_ODATA}{name}")
    return node.text or "" if node is not None else ""


_PARSERS: dict[str, Callable[[Any, dict, dict], Iterator[Point]]] = {
    "records": _records,
    "jsonstat": _jsonstat,
    "odata_xml": _odata_xml,
}


# --- 補助 ------------------------------------------------------------------

def _render(template: str, fields: dict, source: dict) -> str:
    """テンプレートを埋める。${ENV_VAR} は環境変数、{name} は系列定義と期間の値。"""
    rendered = base.expand_env(
        template, origin=source["source_id"], purpose="この統計 API は API キーを要求します"
    )
    end = timeutil.now().date()
    start = end - timedelta(days=source.get("lookback_days", 365))
    values = {
        "start_year": start.year,
        "end_year": end.year,
        "start_date": f"{start:%Y-%m-%d}",
        "end_date": f"{end:%Y-%m-%d}",
        **fields,
    }
    try:
        return rendered.format(**values)
    except KeyError as exc:
        raise base.FetchError(source["source_id"], f"テンプレートの変数 {exc} が系列定義に無い") from exc


def _assert_status(document: Any, spec: dict) -> None:
    """本文の状態フィールドを検査する。

    HTTP 200 のまま「利用上限に達した」と本文で告げる API がある（BLS は
    キー無しだと日次上限超過を 200 で返す）。0 件と区別しないと、
    取得できていない期間が「その日は統計が無かった」として静かに残る。
    """
    path = spec.get("status_path")
    if not path:
        return
    status = _text(_dig(document, path))
    allowed = spec.get("status_ok") or []
    if allowed and status not in allowed:
        message = _dig(document, spec.get("status_message_path", "message"))
        detail = "; ".join(message) if isinstance(message, list) else _text(message)
        raise base.FetchError("status", f"{status}: {detail}" if detail else status)


def _dig(payload: Any, path: str | None) -> Any:
    """ドット区切りパス。数字は配列の添字として扱う（World Bank は [meta, records] を返す）。"""
    if path is None:
        return None
    current = payload
    for key in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and key.lstrip("-").isdigit():
            index = int(key)
            current = current[index] if -len(current) <= index < len(current) else None
        else:
            return None
    return current


def _period(item: dict, paths: str | list[str]) -> str:
    """対象期間。日時で返す API は日付までに丸める。

    統計の対象期間に時刻の意味は無く（入札日 2026-08-19T00:00:00 の 00:00 は
    「その日」以上の情報を持たない）、残すと同じ日が別の期間として並ぶ。
    """
    parts = [_text(_dig(item, path)) for path in ([paths] if isinstance(paths, str) else paths)]
    period = "-".join(p for p in parts if p)
    return period[:10] if _DATETIME_PREFIX.match(period) else period


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _format_value(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


def _updated_at(document: Any, path: str | None) -> str | None:
    """データセットの更新時刻。取れなければ None（推定日付を書かない, §36）。"""
    text = _text(_dig(document, path)) if path else ""
    if not text:
        return None
    if len(text) == 10 and text[4] == "-":
        return f"{text}T00:00:00Z"
    try:
        return timeutil.fmt(timeutil.parse(text))
    except ValueError:
        return None
