#!/usr/bin/env python3
"""登録した仮説と、その事前登録条件

バックテストで偶然見つかった「当たっているように見える現象」を、
後から都合よく解釈できない形で検証するための仕組み。

討論で決まった事後解釈の防止策:
  - 係数を固定し、派生案の追加を禁止する
  - 評価対象は登録後の未来回のみ（過去の当たりは数えない）
  - 最低1000回続け、有利不利を理由に途中で打ち切らない
  - 判定の閾値を事前に固定する（多重比較を考慮して補正後 p<0.01）
  - 提案者本人（Claude）は継続可否の判断から外れる
"""
from __future__ import annotations

import math


def claude_v2_straight(prev_number: str, target_round: int) -> str:
    """Claude案v2・係数(3,7,9) の本命3桁。この式は凍結されており変更できない

    g(x, k) = (x * k + 回号) mod 10
    百の位 = g(前回の百の位, 3) / 十の位 = g(前回の十の位, 7) / 一の位 = g(前回の一の位, 9)
    """
    a, b, c = (int(x) for x in prev_number)
    g = lambda x, k: (x * k + target_round) % 10
    return f"{g(a, 3)}{g(b, 7)}{g(c, 9)}"


REGISTRY = [
    {
        "id": "claude-v2-379-straight",
        "title": "Claude案v2・係数(3,7,9) の本命3桁完全一致率",
        "registered_round": 7084,
        "registered_at": "2026-10-02",
        "claim": "この式が出す3桁が、基準の0.1%を有意に上回る割合で当選番号と完全一致する",
        "origin": ("全履歴6883回のバックテストで15回的中（期待6.88回、p=0.0049）。"
                   "期間を2分割した追試でも前半7回・後半8回と両方で上振れした。"
                   "ただし約30通りの比較を行っており、多重比較による偽陽性の可能性が高い。"),
        "formula": "百=(前回の百の位×3+回号) mod 10 / 十=(×7) / 一=(×9)",
        "conditions": [
            "係数(3,7,9)を固定し、派生案を追加しない",
            "評価は第7084回以降の実データのみ（過去の的中は数えない）",
            "最低1000回継続し、有利不利を理由に途中で打ち切らない",
            "主指標は本命3桁の完全一致率のみ",
            "判定は多重比較補正後の片側p<0.01を閾値とする",
            "提案者のClaudeは継続可否の判断から外れる（判定はGPT・Gemini・依頼主）",
        ],
        "baseline": 0.001,
        "min_rounds": 1000,
        "threshold_p": 0.01,
        "fn": claude_v2_straight,
    },
]


def poisson_upper(k: int, lam: float) -> float:
    """P(X >= k) を返す（ポアソン分布の上側確率）"""
    if lam <= 0:
        return 1.0 if k <= 0 else 0.0
    acc, term = 0.0, math.exp(-lam)
    for i in range(k):
        acc += term
        term *= lam / (i + 1)
    return max(0.0, min(1.0, 1 - acc))


def predict(h: dict, prev_number: str, target_round: int) -> str:
    return h["fn"](prev_number, target_round)


def evaluate(h: dict, records: list[dict]) -> dict:
    """records: [{round, prediction, result}] 登録回以降・結果確定済みのもの"""
    n = len(records)
    hits = sum(1 for r in records if r["prediction"] == r["result"])
    lam = n * h["baseline"]
    p = poisson_upper(hits, lam) if n else None
    if n < h["min_rounds"]:
        verdict = f"検証中（{n} / {h['min_rounds']}回）"
    elif p is not None and p < h["threshold_p"]:
        verdict = "基準を有意に上回った（要再検討）"
    else:
        verdict = "基準と差がない（棄却できず＝偶然だった）"
    return {"n": n, "hits": hits, "expected": round(lam, 2),
            "rate": round(hits / n * 100, 3) if n else None,
            "baseline_rate": h["baseline"] * 100,
            "p_value": round(p, 4) if p is not None else None,
            "verdict": verdict, "progress": round(n / h["min_rounds"] * 100, 1)}
