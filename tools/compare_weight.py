#!/usr/bin/env python3
"""Claudeの提案: 重み付け版(0.2)と一様版の差分を全履歴で計測する

結果は data/comparison.json に保存し、サイトで公開する。
選出方式を変えたときだけ再実行すればよい。
"""
import hashlib, itertools, json, math, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate as G

DIGITS = "0123456789"
COMBOS = [tuple(c) for c in itertools.combinations(DIGITS, 4)]
START = 200


def weights(prev: str, w: float):
    pts = G.tag_points(prev)
    return [math.exp(w * sum(pts[d] for d in c)) for c in COMBOS]


def pick_with(prev: str, rnd: int, w: float, ver: str):
    ws = weights(prev, w)
    tot = sum(ws)
    u = int(hashlib.sha256(f"{rnd}:{prev}:{ver}".encode()).hexdigest()[:16], 16) / 2 ** 64
    acc = 0.0
    for c, x in zip(COMBOS, ws):
        acc += x / tot
        if u < acc:
            return list(c), x / tot, ws, tot
    return list(COMBOS[-1]), ws[-1] / tot, ws, tot


def run(draws, w, ver):
    cov = mini = 0
    sel = Counter()
    sets, overlaps, eff = [], [], []
    prev_set = None
    for i in range(START, len(draws)):
        rnd, actual = draws[i]
        prev = draws[i - 1][1]
        c, p, ws, tot = pick_with(prev, rnd, w, ver)
        probs = [x / tot for x in ws]
        eff.append(1 / sum(x * x for x in probs))        # 有効候補数(ChatGPT提案)
        last2 = actual[1:]
        mini += all(ch in c for ch in last2)
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
        "mini_rate": round(mini / n * 100, 3),
        "mean_covered": round(cov / n, 4),
        "chi2": round(sum((sel[d] - e) ** 2 / e for d in DIGITS), 1),
        "overlap": round(sum(overlaps) / len(overlaps), 3),
        "unique_sets": len(set(sets)),
        "rate_max": round(max(sel.values()) / n * 100, 1),
        "rate_min": round(min(sel.values()) / n * 100, 1),
        "effective_combos": round(sum(eff) / len(eff), 1),
    }


if __name__ == "__main__":
    draws = [(d["round"], d["number"]) for d in G.load_draws()]
    out = {"generated_at": G.now_jst().isoformat(timespec="seconds"),
           "span": [draws[START][0], draws[-1][0]],
           "note": ("Claudeの提案により、重み付け版と一様版を同じ条件で比較した。"
                    "ミニ的中率の理論値は16.0%、平均カバー桁数は1.200、"
                    "連続回のセット重複は1.60、有効候補数は210が一様の値。"),
           "variants": []}
    for w, ver in ((0.2, G.SELECTION_VERSION), (0.0, "uniform-compare")):
        r = run(draws, w, ver)
        r["label"] = "重み0.2（現行）" if w else "重みなし（一様）"
        out["variants"].append(r)
        print(f"{r['label']}: ミニ的中{r['mini_rate']}% / カバー{r['mean_covered']} / "
              f"χ²{r['chi2']} / 重複{r['overlap']} / 有効候補数{r['effective_combos']}")
    (Path(__file__).resolve().parent.parent / "data" / "comparison.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\ndata/comparison.json に保存した")
