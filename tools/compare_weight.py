#!/usr/bin/env python3
"""重み付け版と一様版の差分を全履歴で計測する

Claudeの提案で始めたもの。第7093回の方式変更（位ごとに3数字）に合わせて書き直した。
3つのAIの討論でGeminiが出した論点「重み付けがランダム選出より良い成績を生んでいるか、
長期的に検定し続けよ」に対する答えでもある。

結果は data/comparison.json に保存し、サイトで公開する。
選出方式を変えたときだけ再実行すればよい。

  python3 tools/compare_weight.py          # 現行(位ごと3×3)で比較
  python3 tools/compare_weight.py --legacy # 旧方式(4数字1組)で比較
"""
import hashlib, itertools, json, math, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate as G

DIGITS = "0123456789"
COMBOS4 = [tuple(c) for c in itertools.combinations(DIGITS, 4)]
COMBOS3 = [tuple(c) for c in itertools.combinations(DIGITS, 3)]
START = 200


def _draw(combos, ws, u):
    tot = sum(ws)
    acc = 0.0
    for c, x in zip(combos, ws):
        acc += x / tot
        if u < acc:
            return list(c), [x / tot for x in ws]
    return list(combos[-1]), [x / tot for x in ws]


def run_pos(draws, w, ver):
    """位ごとに3数字を選ぶ方式。十の位・一の位それぞれの的中を数える"""
    mini = ten_hit = one_hit = 0
    sel = Counter()
    sets, overlaps, eff = [], [], []
    prev_set = None
    for i in range(START, len(draws)):
        rnd, actual = draws[i]
        prev = draws[i - 1][1]
        h = hashlib.sha256(f"{rnd}:{prev}:{ver}".encode()).hexdigest()
        combo = {}
        e_tot = 1.0
        for j, (key, pos) in enumerate((("ten", 1), ("one", 2))):
            pts = G.tag_points(prev)
            pts[prev[pos]] += 1                      # 桁別ひっぱり
            ws = [math.exp(w * sum(pts[d] for d in c)) for c in COMBOS3]
            u = int(h[j * 16:(j + 1) * 16], 16) / 2 ** 64
            combo[key], probs = _draw(COMBOS3, ws, u)
            e_tot *= 1 / sum(x * x for x in probs)
        eff.append(e_tot)
        ten_hit += actual[1] in combo["ten"]
        one_hit += actual[2] in combo["one"]
        mini += actual[1] in combo["ten"] and actual[2] in combo["one"]
        sel.update(combo["ten"] + combo["one"])
        cur = (tuple(sorted(combo["ten"])), tuple(sorted(combo["one"])))
        sets.append(cur)
        if prev_set:
            # 位ごとに重なりを数えて足す。期待値は 3×3/10 × 2位 = 1.80
            overlaps.append(len(set(cur[0]) & set(prev_set[0]))
                            + len(set(cur[1]) & set(prev_set[1])))
        prev_set = cur
    n = len(sets)
    e = n * 6 / 10                                   # 1回に6枠(3+3)を埋める
    return {
        "weight": w, "n": n,
        "mini_rate": round(mini / n * 100, 2),
        "ten_rate": round(ten_hit / n * 100, 2),
        "one_rate": round(one_hit / n * 100, 2),
        "chi2": round(sum((sel[d] - e) ** 2 / e for d in DIGITS), 1),
        "overlap": round(sum(overlaps) / len(overlaps), 3),
        "unique_sets": len(set(sets)),
        # 1回に2つの枠(十・一)があるので 2n で割ると「1枠あたりの採用率」になる
        "rate_max": round(max(sel.values()) / (2 * n) * 100, 1),
        "rate_min": round(min(sel.values()) / (2 * n) * 100, 1),
        "effective_combos": round(sum(eff) / len(eff), 1),
    }


def run_legacy(draws, w, ver):
    """旧方式: 4数字を1組だけ選び、両方の位に流用する"""
    cov = mini = 0
    sel = Counter()
    sets, overlaps, eff = [], [], []
    prev_set = None
    for i in range(START, len(draws)):
        rnd, actual = draws[i]
        prev = draws[i - 1][1]
        pts = G.tag_points(prev)
        ws = [math.exp(w * sum(pts[d] for d in c)) for c in COMBOS4]
        u = int(hashlib.sha256(f"{rnd}:{prev}:{ver}".encode()).hexdigest()[:16], 16) / 2 ** 64
        c, probs = _draw(COMBOS4, ws, u)
        eff.append(1 / sum(x * x for x in probs))
        mini += all(ch in c for ch in actual[1:])
        cov += sum(1 for ch in actual if ch in c)
        sel.update(c)
        sets.append(tuple(sorted(c)))
        if prev_set:
            overlaps.append(len(set(c) & prev_set))
        prev_set = set(c)
    n = len(sets)
    e = n * 4 / 10
    return {
        "weight": w, "n": n,
        "mini_rate": round(mini / n * 100, 2),
        "mean_covered": round(cov / n, 4),
        "chi2": round(sum((sel[d] - e) ** 2 / e for d in DIGITS), 1),
        "overlap": round(sum(overlaps) / len(overlaps), 3),
        "unique_sets": len(set(sets)),
        "rate_max": round(max(sel.values()) / n * 100, 1),
        "rate_min": round(min(sel.values()) / n * 100, 1),
        "effective_combos": round(sum(eff) / len(eff), 1),
    }


if __name__ == "__main__":
    legacy = "--legacy" in sys.argv
    draws = [(d["round"], d["number"]) for d in G.load_draws()]
    run = run_legacy if legacy else run_pos
    base_ver = G.SELECTION_VERSION if legacy else G.SELECTION_VERSION_POS
    rows = [
        ("ミニ的中率", "mini_rate", "%", "16.0%" if legacy else "9.0%"),
        *([("平均カバー桁数", "mean_covered", "", "1.200")] if legacy
          else [("十の位が当たった率", "ten_rate", "%", "30.0%"),
                ("一の位が当たった率", "one_rate", "%", "30.0%")]),
        ("採用回数のχ²", "chi2", "", "0"),
        ("連続回の数字の重複", "overlap", "", "1.60" if legacy else "1.80"),
        ("有効候補数", "effective_combos", "", "210" if legacy else "14,400"),
        ("最多採用率", "rate_max", "%", "40.0%" if legacy else "30.0%"),
    ]
    out = {
        "generated_at": G.now_jst().isoformat(timespec="seconds"),
        "span": [draws[START][0], draws[-1][0]],
        "mode": "legacy" if legacy else "pos",
        "uniform_space": 210 if legacy else len(COMBOS3) ** 2,
        "rows": [{"label": l, "key": k, "unit": u, "base": b} for l, k, u, b in rows],
        "note": ("重み付け版と一様版を、同じ乱数の種・同じ全履歴で比較した。"
                 + ("4数字を1組選ぶ旧方式での比較。" if legacy else
                    "十の位・一の位それぞれ3数字を選ぶ現行方式での比較。")),
        "variants": [],
    }
    for w, ver in ((0.2, base_ver), (0.0, "uniform-compare")):
        r = run(draws, w, ver)
        r["label"] = "重み0.2（現行）" if w else "重みなし（一様）"
        out["variants"].append(r)
        print(f"{r['label']}: ミニ的中{r['mini_rate']}% / χ²{r['chi2']} / "
              f"重複{r['overlap']} / 有効候補数{r['effective_combos']:,}")
    a, b = out["variants"]
    diff = a["mini_rate"] - b["mini_rate"]
    se = math.sqrt(0.09 * 0.91 / a["n"]) * 100 * math.sqrt(2) if not legacy \
        else math.sqrt(0.16 * 0.84 / a["n"]) * 100 * math.sqrt(2)
    out["diff"] = {"mini_rate": round(diff, 3), "se": round(se, 3),
                   "z": round(diff / se, 2),
                   "verdict": "差なし" if abs(diff / se) < 1.96 else "差あり"}
    print(f"的中率の差 {diff:+.3f}ポイント (標準誤差{se:.3f}, z={diff / se:+.2f}) "
          f"→ {out['diff']['verdict']}")
    (Path(__file__).resolve().parent.parent / "data" / "comparison.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\ndata/comparison.json に保存した")
