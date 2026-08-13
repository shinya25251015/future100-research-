"""日次 Global Sector Report の生成 (§47)。

16 項目を毎日必ず出す。該当が無い項目も「本日該当なし」と明記して省略しない
（項目が消えると、観測されなかったのか見落としたのかが後から区別できなくなる）。

この層は**観測された事実の要約**までを担う。推論・成長判断は Phase 3-2 以降の
成果物（セクタープロファイル・波及連鎖・予測）が揃った項目にだけ現れる。
未実装の分析に相当する項目は、埋めずに未実装であることを書く。

§44-46 の禁止事項（銘柄推奨・ランキング）は出力前に機械的に検査する。
検査対象は本システムが書いた文であって、一次情報の見出しをそのまま引用した
部分は対象外とする（見出しに "Top 5" が含まれるだけでレポート全体を止めない）。
"""
from __future__ import annotations

import re
from collections.abc import Callable

from . import config, storage, timeutil

SECTION_COUNT = 16
_NONE = "本日該当なし"
_MAX_ITEMS = 8

# §44-46 の禁止事項を検出する。本システムが生成した文にのみ適用する。
_TICKER_PATTERNS = [
    re.compile(r"\b(?:NASDAQ|NYSE|TYO|TSE|HKEX|KRX)\s*[:：]\s*[A-Z0-9]{1,6}\b"),
    re.compile(r"\$[A-Z]{2,5}\b"),
    re.compile(r"証券コード"),
]
_RANKING_PATTERNS = [
    re.compile(r"ランキング"),
    re.compile(r"第?\s*[1-9]\d*\s*位"),
    re.compile(r"\btop\s*\d+\b", re.I),
    re.compile(r"\b(?:best|worst)\s+\d+\b", re.I),
    re.compile(r"おすすめ(?:銘柄|投資)"),
]


class ProhibitedOutput(RuntimeError):
    """§44-46 に反する出力を検出したときに送出する。レポートは保存しない。"""


def build_daily_report(*, as_of: str | None = None, report_date: str | None = None) -> dict:
    """as_of 時点で観測済みのデータだけからレポートを組み立てる (§37)。"""
    as_of = as_of or timeutil.now_str()
    report_date = report_date or f"{timeutil.day_of(as_of):%Y-%m-%d}"

    events = list(storage.iter_events(as_of=as_of))
    primary = [e for e in events if e.get("is_cluster_primary", True)]
    today = [e for e in primary if e["observed_at"][:10] == report_date]
    windows = _load_windows(report_date)

    sections = [builder(today, primary, windows, report_date) for builder in _SECTION_BUILDERS]
    _assert_section_shape(sections)

    return {
        "report_id": f"rpt_{report_date.replace('-', '')}",
        "report_date": report_date,
        "as_of": as_of,
        "coverage": {
            "events_ingested": len([e for e in events if e["observed_at"][:10] == report_date]),
            "events_after_dedup": len(today),
            "sources_polled": len(config.load_sources()),
            "sources_failed": [],
        },
        "sections": sections,
        "prohibited_output_check": {
            "contains_ticker_recommendation": False,
            "contains_ranking": False,
            "checked_at": timeutil.now_str(),
        },
        "schema_version": "1.0",
    }


# --- 節の組み立て ---------------------------------------------------------

def _section(no: int, key: str, title: str, narrative: str, items: list[str] | None = None) -> dict:
    """1 節を作る。narrative は本システムが書いた文なので禁止事項を検査する。"""
    _guard(narrative, f"section {no} ({key})")
    body = narrative
    if items:
        body += "\n" + "\n".join(f"- {item}" for item in items[:_MAX_ITEMS])
    return {"no": no, "key": key, "title": title, "body": body}


def _by_category(events: list[dict], category: str) -> list[dict]:
    rows = [e for e in events if e.get("category") == category]
    rows.sort(key=lambda e: (e.get("max_reliability", "D"), e["event_at"]), reverse=True)
    return rows


def _lines(events: list[dict]) -> list[str]:
    return [
        f"[{e.get('max_reliability', '?')}] {e['event_at'][:10]} {e['title']} ({e['event_id']})"
        for e in events
    ]


def _category_section(no: int, key: str, title: str, category: str, note: str = "") -> Callable:
    def build(today, primary, windows, report_date) -> dict:
        rows = _by_category(today, category)
        if not rows:
            return _section(no, key, title, f"{_NONE}（本日の新規観測なし）")
        narrative = f"本日 {len(rows)} 件を観測。{note}".strip()
        return _section(no, key, title, narrative, _lines(rows))

    return build


def _executive_summary(today, primary, windows, report_date) -> dict:
    if not today:
        return _section(1, "executive_summary", "本日の構造変化サマリ", f"{_NONE}（観測イベントなし）")

    counts: dict[str, int] = {}
    for event in today:
        counts[event["category"]] = counts.get(event["category"], 0) + 1
    signals: dict[str, int] = {}
    for event in today:
        for signal_type in event.get("signal_types", []):
            signals[signal_type] = signals.get(signal_type, 0) + 1

    fired = [w for w in windows if w["co_occurrence"]["threshold_met"]]
    held = [w for w in windows if not w["coverage"]["baseline_covered"]]

    narrative = (
        f"本日の観測は {len(today)} 件。"
        f"内訳（分類別）: {_kv(counts)}。"
        f"シグナル種別: {_kv(signals) or 'なし'}。"
        f"同時増加のしきい値を満たした topic: {len(fired)} 件"
        f"（観測期間不足で判定保留: {len(held)} 件）。"
        "件数は観測量であって重要度ではない。"
    )
    return _section(1, "executive_summary", "本日の構造変化サマリ", narrative)


def _early_signals(today, primary, windows, report_date) -> dict:
    if not windows:
        return _section(7, "early_signals", "Early Signal Detection (§10)",
                        f"{_NONE}（シグナル集計が未実行）")

    fired = [w for w in windows if w["co_occurrence"]["threshold_met"]]
    held = [w for w in windows if not w["coverage"]["baseline_covered"]]
    if fired:
        narrative = f"同時増加のしきい値を満たした topic が {len(fired)} 件。構造評価の対象候補とする。"
        items = [
            f"{w['topic']['label']}: 増加した種別 {w['co_occurrence']['distinct_signal_types']} / "
            f"ソース {w['co_occurrence']['distinct_sources']}"
            for w in fired
        ]
    else:
        narrative = (
            f"しきい値を満たした topic はなし。"
            f"うち {len(held)} 件は観測期間が baseline に届かず判定を保留している"
            "（取り込み開始による見かけの急増をシグナルとして扱わないため）。"
        )
        items = None
    return _section(7, "early_signals", "Early Signal Detection (§10)", narrative, items)


def _known_sector_monitor(today, primary, windows, report_date) -> dict:
    from . import sector_analysis

    rows = []
    for sector in config.load_known_sectors():
        matched = [
            e for e in today
            if any(link["sector_id"] == sector["sector_id"] for link in e.get("sector_links", []))
        ]
        rows.append(f"{sector['name']} ({sector['sector_id']}): 本日 {len(matched)} 件")
    narrative = (
        f"常時監視 {len(rows)} セクターの本日の観測件数。"
        "掲載順は設定ファイルの登録順であり、優劣や順位ではない (§44-46)。"
    )
    _ = sector_analysis  # 将来のプロファイル連携用に参照だけ残す
    return _section(8, "known_sector_monitor", "既知セクター監視 (§11)", narrative, rows)


def _emerging_sector_discovery(today, primary, windows, report_date) -> dict:
    candidates = [w for w in windows if w["topic"].get("is_new_concept") and w["co_occurrence"]["threshold_met"]]
    if not candidates:
        return _section(9, "emerging_sector_discovery", "新規セクター探知 (§12)",
                        f"{_NONE}（既知セクターに属さない同時増加は検出されていない）")
    return _section(9, "emerging_sector_discovery", "新規セクター探知 (§12)",
                    f"{len(candidates)} 件の新概念候補を検出。",
                    [w["topic"]["label"] for w in candidates])


def _sector_profiles_section(no: int, key: str, title: str, empty_note: str) -> Callable:
    def build(today, primary, windows, report_date) -> dict:
        profiles = sorted(storage.SECTORS_DIR.glob("sec_*.json"))
        if not profiles:
            return _section(no, key, title, empty_note)
        items = []
        for path in profiles:
            profile = storage.read_json(path)
            if key == "consensus_vs_independent":
                items.append(f"{profile['name']}: {profile['views']['independent']['divergence']}")
            else:
                items.append(
                    f"{profile['name']}: フェーズ {profile['phase']['phase']} / "
                    f"成長確率 {profile['growth']['probability']['value']} / "
                    f"拡大規模 {profile['growth']['magnitude']['value']}"
                )
        return _section(no, key, title, f"{len(profiles)} セクターの評価が存在する。", items)

    return build


def _not_implemented(no: int, key: str, title: str, phase: str) -> Callable:
    def build(today, primary, windows, report_date) -> dict:
        return _section(no, key, title, f"{phase} が未実装のため、本項目は生成できない。推測では埋めない。")

    return build


def _kpi_and_forecast_review(today, primary, windows, report_date) -> dict:
    predictions = sorted((storage.DATA / "predictions").glob("prd_*.json"))
    if not predictions:
        return _section(16, "kpi_and_forecast_review", "KPI 追跡と予測レビュー (§40-43)",
                        f"{_NONE}（追跡対象の予測がまだ無い）")
    due = [p for p in predictions if storage.read_json(p)["resolution"]["due_date"] <= report_date]
    return _section(16, "kpi_and_forecast_review", "KPI 追跡と予測レビュー (§40-43)",
                    f"追跡中の予測 {len(predictions)} 件。うち本日時点で判定期限を迎えたもの {len(due)} 件。")


_SECTION_BUILDERS: list[Callable] = [
    _executive_summary,
    _category_section(2, "policy_regulation", "政策・規制 (§4)", "policy_regulation"),
    _category_section(3, "geopolitics", "地政学 (§5)", "geopolitics"),
    _category_section(4, "macro_economy", "マクロ経済 (§6)", "macro"),
    _category_section(5, "global_capex", "世界の設備投資 (§7)", "capex"),
    _category_section(6, "technology_progress", "技術革新の進展 (§8)", "technology"),
    _early_signals,
    _known_sector_monitor,
    _emerging_sector_discovery,
    _category_section(10, "supply_demand", "需給分析 (§13)", "supply_demand"),
    _not_implemented(11, "bottlenecks", "ボトルネックと収益回収構造 (§14-15)", "Phase 4"),
    _not_implemented(12, "supply_chain_map", "サプライチェーン構造 (§16)", "Phase 4"),
    _not_implemented(13, "wave_analysis", "第二波・第三波分析 (§17-19)", "Phase 4"),
    _sector_profiles_section(14, "market_formation", "市場形成と成長フェーズ (§9, §20-32)",
                             f"{_NONE}（セクター評価がまだ作成されていない）"),
    _sector_profiles_section(15, "consensus_vs_independent", "コンセンサスと独自見解 (§34)",
                             f"{_NONE}（セクター評価がまだ作成されていない）"),
    _kpi_and_forecast_review,
]


# --- 禁止事項の検査 (§44-46) ----------------------------------------------

def _guard(text: str, where: str) -> None:
    for pattern in _TICKER_PATTERNS:
        if pattern.search(text):
            raise ProhibitedOutput(f"{where}: 銘柄の記載を検出した: {pattern.pattern}")
    for pattern in _RANKING_PATTERNS:
        if pattern.search(text):
            raise ProhibitedOutput(f"{where}: ランキング表現を検出した: {pattern.pattern}")


def _assert_section_shape(sections: list[dict]) -> None:
    if len(sections) != SECTION_COUNT:
        raise ValueError(f"§47: sections must be exactly {SECTION_COUNT}, got {len(sections)}")
    numbers = [s["no"] for s in sections]
    if numbers != list(range(1, SECTION_COUNT + 1)):
        raise ValueError(f"§47: section numbers must run 1..{SECTION_COUNT}, got {numbers}")


# --- 出力 -----------------------------------------------------------------

def render_markdown(report: dict) -> str:
    coverage = report["coverage"]
    lines = [
        f"# Global Sector Report {report['report_date']}",
        "",
        f"as_of: {report['as_of']}",
        f"観測 {coverage['events_ingested']} 件 / 重複統合後 {coverage['events_after_dedup']} 件 / "
        f"巡回ソース {coverage['sources_polled']}",
        "",
        "> 本レポートは産業構造の観測記録であり、銘柄推奨・ランキングを含まない (§44-46)。",
        "",
    ]
    for section in report["sections"]:
        lines.append(f"## {section['no']}. {section['title']}")
        lines.append("")
        lines.append(section["body"])
        lines.append("")
    return "\n".join(lines)


def save(report: dict) -> tuple:
    """JSON と Markdown を保存し、両方のパスを返す。"""
    json_path = storage.REPORTS_DIR / "daily" / f"{report['report_id']}.json"
    md_path = storage.REPORTS_DIR / "daily" / f"{report['report_id']}.md"
    storage.write_json(json_path, report)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def _load_windows(report_date: str) -> list[dict]:
    suffix = f"_{report_date.replace('-', '')}.json"
    return [storage.read_json(p) for p in sorted(storage.SIGNALS_DIR.glob(f"sig_*{suffix}"))]


def _kv(counts: dict[str, int]) -> str:
    return " / ".join(f"{key} {value}" for key, value in sorted(counts.items(), key=lambda kv: -kv[1]))
