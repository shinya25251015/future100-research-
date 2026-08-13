"""仕様書の規律をコードで強制する不変条件 (§17-19, §35-39, §44-46)。

JSON Schema は「形」を守るが、仕様書が本当に守らせたいのは次のような規律である。
これらは外部ライブラリなしで常に検査できるようにしておく。

  - 推論には根拠を必ず添える (§17-19)
  - 未来情報を過去の分析に混入させない (§37)
  - シナリオは 3 本すべて、撤回条件つきで持つ (§39)
  - スコアは内部指標であり、ランキングを出力しない (§44-46)
"""
from __future__ import annotations

from . import timeutil

_REPORT_SECTION_COUNT = 16


class Violation(Exception):
    pass


def check_event(event: dict, *, as_of: str | None = None) -> list[str]:
    problems: list[str] = []

    for claim in event.get("claims", []):
        if claim["type"] == "inferred" and not claim.get("basis"):
            problems.append(f"§17-19: inferred claim without basis: {claim.get('claim_id')}")

    if not event.get("sources"):
        problems.append("§35-38: event has no source evidence")

    if as_of and not timeutil.is_visible(event["observed_at"], as_of):
        problems.append(f"§37: observed_at {event['observed_at']} is after as_of {as_of}")

    # event_at > observed_at（施行日の事前告知など）は正当なので違反としない。
    # 禁じているのは「観測前の情報を分析に使うこと」であり、未来に効力を持つ告知ではない。

    return problems


def check_sector(profile: dict) -> list[str]:
    problems: list[str] = []

    scenarios = profile.get("scenarios", [])
    kinds = {s["scenario"] for s in scenarios}
    if kinds != {"bear", "base", "bull"}:
        problems.append(f"§9/§39: scenarios must be exactly bear/base/bull, got {sorted(kinds)}")
    for scenario in scenarios:
        if not scenario.get("falsifier"):
            problems.append(f"§39: scenario {scenario.get('scenario')} has no falsifier")

    growth = profile.get("growth", {})
    if "probability" not in growth or "magnitude" not in growth:
        problems.append("§21: growth probability and magnitude must be evaluated separately")

    score = profile.get("future_sector_score")
    if score is not None and score.get("internal_only") is not True:
        problems.append("§44-46: future_sector_score must be marked internal_only")

    views = profile.get("views", {})
    if "consensus" not in views or "independent" not in views:
        problems.append("§34: consensus view and independent view must both be present and separated")

    evidence = profile.get("evidence_summary", {})
    as_of = profile.get("as_of")
    newest = evidence.get("newest_evidence_at")
    if as_of and newest and not timeutil.is_visible(newest, as_of):
        problems.append(f"§37: evidence observed at {newest} is newer than as_of {as_of}")

    problems.extend(check_market_size(profile))
    return problems


def check_market_size(profile: dict) -> list[str]:
    """市場規模の数値に対する規律 (§9, §35-38)。

    Phase 5 の本体は一次統計の取得だが、数値を受け入れる側の規律は先に固定できる。
    ここで防ぐのは「出所のない市場規模」と「シナリオ間で辻褄の合わない予測」の 2 つ。
    未推計を 0 のまま置くのは正しい状態で、違反ではない。
    """
    problems: list[str] = []
    market_size = profile.get("market_size", {})

    # 出所のない数値を残さない。0（未推計）は根拠を要求しない。
    for key in ("tam", "sam", "som"):
        amount = market_size.get(key, {}).get("amount", 0)
        if amount and not market_size.get("evidence"):
            problems.append(f"§35-38: market_size.{key} に数値があるのに根拠が無い")

    # 同じ年で bear <= base <= bull になっているか。順序が壊れた予測は読めない。
    by_year: dict[int, dict[str, float]] = {}
    for scenario in profile.get("scenarios", []):
        for projection in scenario.get("projections", []):
            amount = projection.get("market_size", {}).get("amount")
            if amount:
                by_year.setdefault(projection["year"], {})[scenario["scenario"]] = amount

    for year, amounts in sorted(by_year.items()):
        bear, base, bull = amounts.get("bear"), amounts.get("base"), amounts.get("bull")
        if bear is not None and base is not None and bear > base:
            problems.append(f"§9: {year} 年の bear ({bear}) が base ({base}) を上回っている")
        if base is not None and bull is not None and base > bull:
            problems.append(f"§9: {year} 年の base ({base}) が bull ({bull}) を上回っている")

    return problems


def check_signal_window(window: dict) -> list[str]:
    problems: list[str] = []
    period = window.get("window", {})
    as_of_day = window.get("as_of", "")[:10]

    if period.get("end", "") > as_of_day:
        problems.append(f"§37: window end {period.get('end')} extends past as_of {as_of_day}")
    if period.get("baseline_end") and period["baseline_end"] >= period.get("start", ""):
        problems.append("§10: baseline period must precede the window period")

    coverage = window.get("coverage", {})
    co_occurrence = window.get("co_occurrence", {})
    if not coverage.get("baseline_covered", False) and co_occurrence.get("threshold_met"):
        problems.append(
            "§10: threshold_met set while the baseline period is not covered by observation; "
            "a surge that starts when collection starts is not a signal"
        )
    if window.get("promotion", {}).get("promoted") and not co_occurrence.get("threshold_met"):
        problems.append("§12: promoted without meeting the co-occurrence threshold")

    for entry in window.get("counts", []):
        if entry.get("baseline_count", 0) == 0 and "delta_ratio" in entry:
            problems.append(f"§10: delta_ratio defined against a zero baseline ({entry.get('signal_type')})")

    return problems


def check_wave(chain: dict) -> list[str]:
    """波及連鎖の規律 (§17-19)。"""
    problems: list[str] = []
    ranks = {"low": 0, "medium": 1, "high": 2}
    strongest_by_wave: dict[int, int] = {}

    for index, link in enumerate(chain.get("links", [])):
        where = f"links[{index}] (wave {link.get('wave')})"
        if link.get("claim_type") == "inferred":
            if not link.get("evidence"):
                problems.append(f"§17-19: {where}: 推論した因果に根拠が無い")
            if not link.get("falsifier"):
                problems.append(f"§39: {where}: 推論した因果に撤回条件が無い")
        if not link.get("mechanism"):
            problems.append(f"§17-19: {where}: 波及の機序が書かれていない")

        wave = link.get("wave", 1)
        rank = ranks.get(link.get("causal_confidence", "low"), 0)
        strongest_by_wave[wave] = max(strongest_by_wave.get(wave, rank), rank)

    # 推論を重ねるほど確からしくなることはない。第 n 波が第 n-1 波より強い主張は弾く。
    for wave in sorted(strongest_by_wave):
        previous = strongest_by_wave.get(wave - 1)
        if previous is not None and strongest_by_wave[wave] > previous:
            problems.append(
                f"§19: wave {wave} の causal_confidence が wave {wave - 1} を上回っている。"
                "推論を重ねるほど確信度が上がることはない"
            )
    return problems


def check_supply_chain(chain_map: dict) -> list[str]:
    """サプライチェーンとボトルネックの規律 (§13-16)。"""
    problems: list[str] = []
    node_ids = {node["node_id"] for node in chain_map.get("nodes", [])}

    for edge in chain_map.get("edges", []):
        for side in ("from", "to"):
            if edge.get(side) not in node_ids:
                problems.append(f"§16: edge の {side} が存在しないノードを指している: {edge.get(side)}")

    for node in chain_map.get("nodes", []):
        bottleneck = node.get("bottleneck")
        if not bottleneck or not bottleneck.get("is_bottleneck"):
            continue
        if not bottleneck.get("trigger_condition"):
            problems.append(f"§14: {node['node_id']}: どの程度の需要増で不足するかが書かれていない")
        if not bottleneck.get("monetizer_type"):
            problems.append(f"§15: {node['node_id']}: 不足から利益を回収する主体の類型が書かれていない")
    return problems


def check_prediction(prediction: dict) -> list[str]:
    problems: list[str] = []
    if not prediction.get("falsifier"):
        problems.append("§39: prediction has no falsifier")
    resolution = prediction.get("resolution", {})
    for field in ("due_date", "metric", "criterion"):
        if not resolution.get(field):
            problems.append(f"§41: prediction resolution missing {field}; it would be unverifiable")
    return problems


def check_daily_report(report: dict) -> list[str]:
    problems: list[str] = []
    sections = report.get("sections", [])
    if len(sections) != _REPORT_SECTION_COUNT:
        problems.append(f"§47: daily report must have exactly {_REPORT_SECTION_COUNT} sections, got {len(sections)}")

    keys = [s.get("key") for s in sections]
    if len(set(keys)) != len(keys):
        problems.append("§47: duplicate section keys in daily report")
    for section in sections:
        if not section.get("body", "").strip():
            problems.append(f"§47: section {section.get('key')} is empty; state '該当なし' explicitly")

    check = report.get("prohibited_output_check", {})
    if check.get("contains_ticker_recommendation") is not False:
        problems.append("§44-46: report must assert it contains no ticker recommendation")
    if check.get("contains_ranking") is not False:
        problems.append("§44-46: report must assert it contains no ranking")

    return problems


CHECKS = {
    "event": check_event,
    "signal": check_signal_window,
    "sector": check_sector,
    "wave": check_wave,
    "supply_chain": check_supply_chain,
    "prediction": check_prediction,
    "daily_report": check_daily_report,
}
