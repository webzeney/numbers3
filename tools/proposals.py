#!/usr/bin/env python3
"""3者の改良提案を全履歴でバックテストする(ウォークフォワード)

各回、その回より前のデータだけを使って候補と本命を出し、実際の当選番号で採点する。
"""
import csv, hashlib, itertools, math, random, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate as G

DIGITS = "0123456789"
COMBOS = [tuple(c) for c in itertools.combinations(DIGITS, 4)]


def load():
    return [(d["round"], d["number"]) for d in G.load_draws()]


# ---------------- 各方式 ----------------

def m_current(hist, rnd):
    """現行: 210通りから、原典該当点で重み付けした抽選"""
    prev = hist[-1]
    pts = G.tag_points(prev)
    w = [math.exp(G.TILT_WEIGHT * sum(pts[d] for d in c)) for c in COMBOS]
    tot = sum(w)
    u = int(hashlib.sha256(f"{rnd}:{prev}:{G.LOGIC_VERSION}".encode()).hexdigest()[:16], 16) / 2**64
    acc = 0.0
    for c, x in zip(COMBOS, w):
        acc += x / tot
        if u < acc:
            return list(c), None
    return list(COMBOS[-1]), None


def m_gpt(hist, rnd):
    """GPT案: 全履歴の実出現数 − 期待値 が小さい順"""
    n = len(hist)
    total = Counter("".join(hist))
    exp = 3 * n / 10
    cand = sorted(DIGITS, key=lambda d: (total[d] - exp, int(d)))[:4]
    main = ""
    for p in range(3):
        c = Counter(h[p] for h in hist)
        e = n / 10
        main += min(DIGITS, key=lambda d: (c[d] - e, int(d)))
    return cand, main


def m_claude(hist, rnd, W=100):
    """Claude案: 直近W回の窓で、位別の不足量(期待値−観測)が大きい順"""
    win = hist[-W:] if len(hist) >= W else hist
    n = len(win)
    e = n / 10
    deficits = []
    for p in range(3):
        c = Counter(h[p] for h in win)
        deficits.append({d: e - c[d] for d in DIGITS})
    main = "".join(min(DIGITS, key=lambda d: (-deficits[p][d], int(d))) for p in range(3))
    best = {d: max(deficits[p][d] for p in range(3)) for d in DIGITS}
    cand = sorted(DIGITS, key=lambda d: (-best[d], int(d)))[:4]
    return cand, main


def m_uniform(hist, rnd):
    """対照: 回号hashで210通りから一様に1組"""
    h = hashlib.sha256(f"uniform:{rnd}:{hist[-1]}".encode()).hexdigest()
    i = int(h[:16], 16) % len(COMBOS)
    main = "".join(str(int(h[32 + k * 2:34 + k * 2], 16) % 10) for k in range(3))
    return list(COMBOS[i]), main


_GPT2_CACHE = {"n": 0, "h": None}


def m_gpt2(hist, rnd, dates=None):
    """GPT案v2: 全履歴テキストのSHA-256を撹拌源にする(計数しない)

    毎回連結し直すと O(n^2) になるため、前回までのハッシュ状態を使い回す。
    結果は「全履歴を連結してハッシュした値」と同一になる。
    """
    if _GPT2_CACHE["h"] is None or _GPT2_CACHE["n"] > len(hist):
        _GPT2_CACHE["h"] = hashlib.sha256(("site-hash-v1|" + "|".join(hist)).encode())
        _GPT2_CACHE["n"] = len(hist)
    else:
        while _GPT2_CACHE["n"] < len(hist):
            _GPT2_CACHE["h"].update(("|" + hist[_GPT2_CACHE["n"]]).encode())
            _GPT2_CACHE["n"] += 1
    base = _GPT2_CACHE["h"].copy()
    cand = []
    suffix = 0
    while len(cand) < 4 and suffix < 20:
        hh = base.copy()
        hh.update(("|candidates" + (f"|{suffix}" if suffix else "")).encode())
        h = hh.hexdigest()
        for i in range(0, 64, 2):
            d = str(int(h[i:i+2], 16) % 10)
            if d not in cand:
                cand.append(d)
                if len(cand) == 4:
                    break
        suffix += 1
    hm_ = base.copy(); hm_.update(b"|main"); hm = hm_.hexdigest()
    main = "".join(str(int(hm[i*2:i*2+2], 16) % 10) for i in range(3))
    return cand, main


def m_claude2(hist, rnd):
    """Claude案v2: 直前当選番号だけを種にした線形変換(電卓で検算できる)"""
    a, b, c = (int(x) for x in hist[-1])
    L = rnd
    g = lambda x, k: (x * k + L) % 10
    main = f"{g(a,3)}{g(b,7)}{g(c,9)}"
    seeds = [(a, 2), (b, 5), (c, 11), ((a + b + c) % 10, 13)]
    primes = [17, 19, 23, 29, 31, 37, 41, 43]
    cand = []
    for (x, k) in seeds:
        d = str(g(x, k))
        if d not in cand:
            cand.append(d)
    for k in primes:                       # 素数を順に試す
        if len(cand) >= 4:
            break
        for (x, _) in seeds:
            d = str(g(x, k))
            if d not in cand:
                cand.append(d)
                if len(cand) == 4:
                    break
    for d in DIGITS:                       # それでも足りなければ小さい順に補う
        if len(cand) >= 4:
            break
        if d not in cand:
            cand.append(d)
    return cand, main


def m_gemini2(hist, rnd):
    """Gemini案v2: 回号と直前番号のビットシフト+XOR(計数しない)"""
    x, y, z = (int(ch) for ch in hist[-1])
    V = (rnd * 1000 + x * 100 + y * 10 + z) % 65536
    for _ in range(3):
        V = ((V << 3) ^ (V >> 5)) % 65536
    s = str(V)
    main = "".join(str(int(s[i]) % 10) if i < len(s) else "0" for i in range(3))
    cand = []
    pos = 3
    cur = V
    for _ in range(40):                    # 更新は上限40回まで
        t = str(cur)
        while pos < len(t) and len(cand) < 4:
            d = str(int(t[pos]) % 10)
            if d not in cand:
                cand.append(d)
            pos += 1
        if len(cand) >= 4:
            break
        cur = (cur * 7 + 1) % 65536        # 0に落ちても止まらないよう+1
        pos = 0
    for d in DIGITS:
        if len(cand) >= 4:
            break
        if d not in cand:
            cand.append(d)
    return cand, main


METHODS = {
    "現行(重み0.2)": m_current,
    "【取下】Claude案(窓100)": lambda h, r: m_claude(h, r, 100),
    "GPT案v2(履歴ハッシュ)": m_gpt2,
    "Claude案v2(線形変換)": m_claude2,
    "Gemini案v2(ビットシフト)": m_gemini2,
    "一様ランダム(対照)": m_uniform,
}


def run(draws, start=200):
    out = {}
    for name, fn in METHODS.items():
        cov, overlaps, sets, straight = [], [], [], 0
        sel = Counter(); streak = Counter(); best = Counter(); prev_set = None
        for i in range(start, len(draws)):
            rnd, actual = draws[i]
            hist = [n for _, n in draws[:i]]
            cand, main = fn(hist, rnd)
            cov.append(sum(1 for ch in actual if ch in cand))
            sets.append(tuple(sorted(cand)))
            sel.update(cand)
            if main is not None and main == actual:
                straight += 1
            if prev_set:
                overlaps.append(len(set(cand) & prev_set))
            for d in DIGITS:
                if d in cand:
                    streak[d] += 1; best[d] = max(best[d], streak[d])
                else:
                    streak[d] = 0
            prev_set = set(cand)
        n = len(cov)
        e = n * 4 / 10
        out[name] = {
            "n": n,
            "cov": sum(cov) / n,
            "atleast1": sum(1 for c in cov if c >= 1) / n * 100,
            "straight": straight / n * 100 if any(fn(["000"], 1)[1] for _ in [0]) or straight else None,
            "chi2": sum((sel[d] - e) ** 2 / e for d in DIGITS),
            "overlap": sum(overlaps) / len(overlaps),
            "streak": max(best.values()),
            "unique": len(set(sets)),
            "rate_max": max(sel.values()) / n * 100,
            "rate_min": min(sel.values()) / n * 100,
        }
    return out


def w_sensitivity(draws, start=200):
    """Claudeの関心: 窓幅を変えると候補はどれだけ入れ替わるか"""
    diffs = {"50対100": [], "100対200": [], "50対200": []}
    for i in range(start, len(draws)):
        rnd = draws[i][0]
        hist = [n for _, n in draws[:i]]
        c50 = set(m_claude(hist, rnd, 50)[0])
        c100 = set(m_claude(hist, rnd, 100)[0])
        c200 = set(m_claude(hist, rnd, 200)[0])
        diffs["50対100"].append(4 - len(c50 & c100))
        diffs["100対200"].append(4 - len(c100 & c200))
        diffs["50対200"].append(4 - len(c50 & c200))
    return {k: sum(v) / len(v) for k, v in diffs.items()}


if __name__ == "__main__":
    draws = load()
    print(f"対象: 第{draws[200][0]}回〜第{draws[-1][0]}回 ({len(draws)-200}回)\n")
    res = run(draws)
    print(f"{'方式':<22}{'カバー':>8}{'1桁以上':>9}{'本命的中':>9}{'採用χ²':>9}"
          f"{'重複':>7}{'最長連続':>9}{'ユニーク':>9}{'最多採用率':>10}")
    print("-" * 94)
    for k, v in res.items():
        st = f"{v['straight']:.3f}%" if v['straight'] is not None else "—"
        print(f"{k:<22}{v['cov']:>8.3f}{v['atleast1']:>8.1f}%{st:>9}{v['chi2']:>9.0f}"
              f"{v['overlap']:>7.2f}{v['streak']:>9.0f}{v['unique']:>9}{v['rate_max']:>9.1f}%")
    print(f"\n基準: カバー1.200 / 1桁以上78.4% / 本命的中0.100% / χ²は0が均等 / 重複1.60 / 最多採用率40.0%")
    print("\n窓幅を変えたときに候補4数字が何個入れ替わるか(最大4個):")
    for k, v in w_sensitivity(draws).items():
        print(f"  {k}: 平均 {v:.2f}個")
