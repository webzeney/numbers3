#!/usr/bin/env python3
"""3つのAIが事前に宣言した選定ルール

各AIが討論の場で自ら定義し、実装上の曖昧さを本人が確認して凍結したもの。
以後このルールだけが数字を決める。AIがその場で考えることはない。

ルール文を変更する場合は、必ず version を上げ、CHANGELOG に理由を残すこと
(「基準をこっそり調整すれば事前コミットも形骸化する」というClaudeの指摘による)。
"""
from __future__ import annotations

import hashlib
from collections import Counter

DIGITS = "0123456789"
POSITIONS = ("百の位", "十の位", "一の位")


# ---------------------------------------------------------------- GPT-v1

GPT_RULE = """ルール名: GPT-v1「検証可能な擬似ランダム基準」
使用データ範囲: 最新回1件のみ（回号・抽選日・当選番号）
計算手順:
 1. 公開済みで回号が最大の回を L とする
 2. S = "GPT-v1|回号|抽選日|当選番号" を作る（回号はゼロ埋めなし、
    抽選日は YYYY-MM-DD、当選番号は3桁ゼロ埋め、UTF-8）
 3. 候補用に SHA-256(S + "|candidates") を計算する
 4. ハッシュ64文字を左から2文字ずつ32個のバイト値にし、各値を mod 10 する
 5. 未採用の数字だけを左から順に採用し、4種類そろった時点で候補とする
 6. 足りなければ suffix を "|candidates|1", "|candidates|2" と増やして続ける
 7. 本命用に SHA-256(S + "|main") を計算する
 8. 左から3バイトを mod 10 し、百・十・一の位とする（重複可）
同点時の処理: 左から順に処理するため同点は発生しない
除外条件: なし"""

GPT_REASON = ("「もっともらしい統計理由」を作る癖を避けるため、あえて予測らしさを捨て、"
              "検証可能な擬似ランダムを選んだ。「読めないものを読んだふりしない」ことを"
              "自分の役割にするため。")
GPT_SELF = ("当たるとは思っていない。的中率を上げるルールではなく、後付け不能性と再現性を"
            "優先した基準である。価値は予測力ではなく、透明な比較対象になる点にある。")


def gpt_v1(draws: list[dict]) -> dict:
    last = draws[-1]
    s = f"GPT-v1|{last['round']}|{last['date']}|{last['number']}"

    def bytes_of(material: str) -> list[int]:
        h = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return [int(h[i:i + 2], 16) for i in range(0, 64, 2)]

    cand: list[str] = []
    suffix = 0
    while len(cand) < 4:
        material = s + "|candidates" + (f"|{suffix}" if suffix else "")
        for b in bytes_of(material):
            d = str(b % 10)
            if d not in cand:
                cand.append(d)
                if len(cand) == 4:
                    break
        suffix += 1
    main = "".join(str(b % 10) for b in bytes_of(s + "|main")[:3])
    return {"candidates": cand, "straight": main, "seed_material": s}


# ---------------------------------------------------------------- Claude-v1

CLAUDE_RULE = """ルール名: Claude-v1「最長未出現基準（ギャンブラーの誤謬の意図的実装）」
使用データ範囲: 全履歴（第1回〜直近回）
計算手順:
 1. 全当選番号を百・十・一の位に分解し、位ごとの時系列を作る
 2. 位ごと・数字ごとに「最後に出現した回号」を求める（未出現は0）
 3. 未出現経過回数 = 直近回号 − 最後に出現した回号
 4. 各位について未出現経過回数が最大の数字を、その位の本命とする
 5. 候補4数字は、位を区別しない全履歴の出現総数が少ない順に4つ
同点時の処理:
 - 手順4が同数なら小さい数字を優先
 - 手順5が同数なら小さい数字を優先、それでも同数なら未出現経過回数が大きい方
補足（本人による最終確認）:
 - 直近回とは回号が最大の回
 - 候補と本命で数字が重複してよい（差し替えない）
 - 「出現」とはその位にその数字が現れたことを指し、位ごとに独立して判定する
除外条件: なし"""

CLAUDE_REASON = ("GPTが「予測らしさを捨てた擬似ランダム」を選んだため、あえて逆方向、"
                 "「人間が最も陥りやすい推論の誤り」を精密に実装する側に回る。"
                 "「長く出ていないから次は出るはず」を厳密な計算式として固定することで、"
                 "なぜその誤りが魅力的に見えるのかを可視化できると考えた。")
CLAUDE_SELF = ("当たるとは思っていない。各回の抽選は独立事象であり、未出現経過回数は"
               "次回の確率に理論上無関係。「当たると錯覚しやすい思考様式」をコードとして"
               "固定し、外れ続ける様子ごと提示するためのものだ。")


def claude_v1(draws: list[dict]) -> dict:
    latest = draws[-1]["round"]
    last_seen = [{d: 0 for d in DIGITS} for _ in range(3)]
    total = Counter()
    for dr in draws:
        for p, ch in enumerate(dr["number"]):
            last_seen[p][ch] = dr["round"]
            total[ch] += 1

    main = ""
    gaps = []
    for p in range(3):
        g = {d: latest - last_seen[p][d] for d in DIGITS}
        gaps.append(g)
        main += min(DIGITS, key=lambda d: (-g[d], int(d)))

    cand = sorted(DIGITS, key=lambda d: (total[d], int(d)))[:4]
    return {"candidates": cand, "straight": main,
            "detail": {"gaps": gaps, "totals": {d: total[d] for d in DIGITS}}}


# ---------------------------------------------------------------- Gemini-v1

GEMINI_RULE = """ルール名: Gemini-v1「重み付け周期相関フィルタ」
使用データ範囲: 全履歴（頻度は直近30回、周期は全履歴）
計算手順:
 1. 直近30回の各数字(0-9)の出現頻度をカウントする
 2. スコア = 1 / 出現頻度（頻度0の数字はスコア10）
 3. ラグN=1〜10について、全履歴で「N回前と同じ位に同じ数字」が一致した率を調べ、
    最も高いラグ N* を特定する（同率なら小さいN）。N*回前の当選番号の
    同じ位の数字に、その位のスコアを1.5倍するブーストを掛ける
 4. 各位について、ブースト後のスコアが最大の数字をその位の本命とする
 5. 候補4数字は、ブースト前のスコアが高い順に上位4つ
同点時の処理: 奇数を優先し、それでも同点なら小さい数字を優先
除外条件: なし"""

GEMINI_REASON = ("GPTが「ランダムによる透明性」、Claudeが「心理的バイアスの可視化」を選んだため、"
                 "データの中にある擬似的なパターンを最大限に搾り出す統計的アプローチをとる。"
                 "多くのプレイヤーが「攻略」と考える王道的手法をあえて計算式として固定し、"
                 "それがいかに無力かを実証する役割を担う。")
GEMINI_SELF = ("的中率は期待していない。独立事象に対して周期性や頻度を追うことが理論的な誤りで"
               "あることを、自らそのルールを体現し続けることで証明する。"
               "「分析すればするほど、もっともらしいが外れる」というAIの限界を示したい。")


def gemini_v1(draws: list[dict]) -> dict:
    freq30 = Counter("".join(d["number"] for d in draws[-30:]))
    base = {d: (10.0 if freq30[d] == 0 else 1.0 / freq30[d]) for d in DIGITS}

    best_lag, best_rate = 1, -1.0
    for lag in range(1, 11):
        hit = tot = 0
        for i in range(lag, len(draws)):
            for p in range(3):
                tot += 1
                hit += draws[i]["number"][p] == draws[i - lag]["number"][p]
        rate = hit / tot if tot else 0.0
        if rate > best_rate + 1e-12:
            best_lag, best_rate = lag, rate

    ref = draws[-best_lag]["number"] if len(draws) >= best_lag else None

    def pick(cands, score):            # 同点は奇数優先 → 小さい数字
        return min(cands, key=lambda d: (-score[d], int(d) % 2 == 0, int(d)))

    main = ""
    for p in range(3):
        sc = dict(base)
        if ref:
            sc[ref[p]] = sc[ref[p]] * 1.5
        main += pick(DIGITS, sc)

    cand = sorted(DIGITS, key=lambda d: (-base[d], int(d) % 2 == 0, int(d)))[:4]
    return {"candidates": cand, "straight": main,
            "detail": {"lag": best_lag, "lag_rate": round(best_rate * 100, 3),
                       "boost_from": ref, "freq30": {d: freq30[d] for d in DIGITS}}}


# ---------------------------------------------------------------- 登録

RULES = {
    "gpt": {"label": "ChatGPT", "version": "GPT-v1", "fn": gpt_v1,
            "title": "検証可能な擬似ランダム基準",
            "rule": GPT_RULE, "reason": GPT_REASON, "self": GPT_SELF},
    "claude": {"label": "Claude", "version": "Claude-v1", "fn": claude_v1,
               "title": "最長未出現基準（ギャンブラーの誤謬の意図的実装）",
               "rule": CLAUDE_RULE, "reason": CLAUDE_REASON, "self": CLAUDE_SELF},
    "gemini": {"label": "Gemini", "version": "Gemini-v1", "fn": gemini_v1,
               "title": "重み付け周期相関フィルタ",
               "rule": GEMINI_RULE, "reason": GEMINI_REASON, "self": GEMINI_SELF},
}

CHANGELOG = [
    {"date": "2026-10-02", "version": "v1",
     "note": "3者が討論の場で自ら定義し、実装上の曖昧さを本人が確認して凍結"},
]


def run_all(draws: list[dict]) -> dict:
    """全ルールを実行する。ルール文のハッシュも添えて改変できないようにする"""
    out = {}
    for key, r in RULES.items():
        res = r["fn"](draws)
        out[key] = {
            "label": r["label"], "status": "ok",
            "version": r["version"], "title": r["title"],
            "straight": res["straight"], "candidates": res["candidates"],
            "reason": r["reason"], "self_assessment": r["self"],
            "rule_text": r["rule"],
            "rule_hash": hashlib.sha256(r["rule"].encode("utf-8")).hexdigest(),
            "detail": res.get("detail", {}),
            "basis": {"round": draws[-1]["round"], "number": draws[-1]["number"]},
        }
    return out
