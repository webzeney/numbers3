#!/usr/bin/env python3
"""ナンバーズ3 候補数字ジェネレータ (本番用)

設計の根拠は docs/仕様書.md を参照。要点:
  - 210通り(10C4)から「回号 + 直近確定番号」のSHA256で一様ランダムに1組選ぶ
  - 伝統手法(ひっぱり・裏数字など)は採点に一切関与させず、選出後のタグとしてのみ表示
  - データが古い/壊れている場合は候補生成を停止する(前回値の流用はしない)

使い方:
  python3 generate.py            # 履歴を更新して data/*.json を生成
  python3 generate.py --if-new   # 新しい回が出ていなければ何もしない(cron用)
  python3 generate.py --no-fetch # ダウンロードせずローカルCSVで生成
  python3 generate.py --no-ai    # AIへの問い合わせを省く
  python3 generate.py --verify   # 生成物の整合性を検証するだけ
  python3 generate.py --now 2026-09-30T20:00  # 現在時刻を差し替える(テスト用)

終了コード: 0=生成した / 2=データ異常で停止 / 3=新しい回がないので何もしなかった
"""
from __future__ import annotations

import csv, hashlib, itertools, json, math, os, ssl, sys, urllib.error, urllib.request
from collections import Counter
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import ai_rules
import auto_debate
import hypotheses

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CSV_PATH = DATA / "NUMBERS3_ALL.csv"
ROUNDS = DATA / "rounds"          # 回ごとの保存版ページ用スナップショット
SOURCES_DIR = DATA / "sources"    # 取得元ごとの生データ
TICKET_PRICE = 200                # ミニ1口の価格

# 取得元。いずれか新しいものを採用し、重なる範囲の当選番号が一致するか照合する。
# みずほ銀行(公式)は機械的なアクセスを403で拒否するため、中継サイトを使う。
SOURCES = [
    # 抽選当日19:50頃に公開される(実測)。こちらを主に使う
    {"name": "loto-life", "format": "lotolife",
     "url": "https://loto-life.net/csv/numbers3"},
    # 翌朝10:15頃の更新。照合用かつloto-lifeが落ちたときの予備
    {"name": "mk-mode", "format": "mk",
     "url": "https://www.mk-mode.com/rails/loto/NUMBERS3_ALL.csv"},
]
UA = "numbers3-bot/2.1 (+https://numbers.ota9.site/)"   # 素性を明かす

LOGIC_VERSION = "3.3.0-mini"      # サイト全体のバージョン（表示・記録用）

# 選出方式そのもののバージョン。乱数の種に使うため、方式を変えない限り凍結する。
# ここを変えると同じ回でも候補が変わってしまう（2026-10-02に実際に起きた事故）。
SELECTION_VERSION = "2.1.0-tilted"
TILT_WEIGHT = 0.2         # 宝島本の手法に該当する数字への傾き。0なら完全な一様ランダム
DIGITS = "0123456789"
COMBOS = [tuple(c) for c in itertools.combinations(DIGITS, 4)]   # 210通り
JST = timezone(timedelta(hours=9))
MAX_GAP_DAYS = 5          # 最新抽選日からこれ以上経っていたら停止
DRAW_TIME = (18, 45)      # 抽選時刻 (JST)。この時刻を過ぎた平日は、その回が抽選済みとみなす
AWAIT_HOURS = 18          # 抽選後この時間までは「通常の結果待ち」。超えたら異常な遅延とみなす


NOW_OVERRIDE: datetime | None = None      # テスト用。--now で現在時刻を差し替える


def load_env() -> None:
    """スクリプトと同じ場所の .env を環境変数に読み込む(既存の値は上書きしない)

    cronはシェルの設定を読まないため、APIキーの受け渡しに使う。
    .env は公開ディレクトリに置くことになるので、.htaccess で遮断すること。
    """
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now_jst() -> datetime:
    return NOW_OVERRIDE or datetime.now(JST)


class DataError(RuntimeError):
    """データ鮮度・整合性の異常。候補生成を止めるために使う

    kind:
      awaiting … 抽選直後で結果がまだ出ていない(正常な待ち状態)
      stale    … 想定時間を過ぎても取り込めていない(異常)
      broken   … データそのものが壊れている
    """

    def __init__(self, message: str, kind: str = "broken"):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- データ取得

def ssl_ctx() -> ssl.SSLContext:
    """macOSのpython.org版はルート証明書を持たないため明示する"""
    for ca in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
        if os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


def _parse(text: str, fmt: str) -> list[dict]:
    """取得元ごとのCSVを共通の形に直す"""
    out = []
    for row in list(csv.reader(text.splitlines()))[1:]:
        if not row or not row[0].strip().isdigit():
            continue
        num = row[2].strip().strip('"').lstrip("=").strip('"')
        if fmt == "mk":
            d = datetime.strptime(row[1].strip(), "%Y/%m/%d").date()
        else:
            d = datetime.strptime(row[1].strip(), "%Y-%m-%d").date()
        if not num.isdigit():
            continue
        out.append({"round": int(row[0]), "date": d, "number": num.zfill(3)})
    return sorted(out, key=lambda x: x["round"])


def _download(src: dict) -> list[dict] | None:
    """取得元からCSVを取る。前回から変わっていなければ再取得しない(304)"""
    cache = SOURCES_DIR / f"{src['name']}.csv"
    meta_path = SOURCES_DIR / f"{src['name']}.meta.json"
    meta = {}
    if meta_path.exists() and cache.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}

    headers = {"User-Agent": UA}
    if meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]
    if meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]

    req = urllib.request.Request(src["url"], headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl_ctx()) as r:
            body = r.read()
            new_meta = {"last_modified": r.headers.get("Last-Modified"),
                        "etag": r.headers.get("ETag")}
    except urllib.error.HTTPError as e:
        if e.code == 304 and cache.exists():       # 変わっていないので手元のものを使う
            print(f"  {src['name']}: 更新なし(304) → キャッシュを使用")
            return _parse_bytes(cache.read_bytes(), src)
        print(f"  {src['name']}: 取得失敗 ({e})", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"  {src['name']}: 取得失敗 ({e})", file=sys.stderr)
        return None
    draws = _parse_bytes(body, src)
    if draws:
        SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(body)
        meta_path.write_text(json.dumps(new_meta, ensure_ascii=False), encoding="utf-8")
    return draws


def _parse_bytes(body: bytes, src: dict) -> list[dict] | None:
    for enc in ("utf-8", "cp932"):
        try:
            text = body.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        print(f"  {src['name']}: 文字コードを判別できない", file=sys.stderr)
        return None
    draws = _parse(text, src["format"])
    if len(draws) < 1000:
        print(f"  {src['name']}: 件数が少なすぎる({len(draws)}件)", file=sys.stderr)
        return None
    return draws


def cross_check(a: list[dict], b: list[dict], names: tuple[str, str]) -> None:
    """2つの取得元で、重なる範囲の当選番号が一致するかを照合する"""
    mb = {d["round"]: d["number"] for d in b}
    diffs = [(d["round"], d["number"], mb[d["round"]])
             for d in a[-300:] if d["round"] in mb and mb[d["round"]] != d["number"]]
    if diffs:
        r, x, y = diffs[0]
        raise DataError(
            f"取得元で当選番号が食い違っている: 第{r}回 {names[0]}={x} / {names[1]}={y}"
            f"（不一致 {len(diffs)}件）", kind="broken")


def fetch_csv() -> list[dict] | None:
    """全取得元から取り、最も新しいものを採用する。失敗したらNone"""
    got = []
    for src in SOURCES:
        draws = _download(src)
        if draws:
            got.append((src["name"], draws))
            print(f"  {src['name']}: 第{draws[-1]['round']}回 "
                  f"({draws[-1]['date']}) まで")
    if not got:
        return None
    if len(got) >= 2:
        cross_check(got[0][1], got[1][1], (got[0][0], got[1][0]))
    name, best = max(got, key=lambda g: g[1][-1]["round"])
    print(f"  採用: {name}")
    # 共通の形式で保存し直す (以降の処理はこのファイルだけを見る)
    # このファイルは取得元のデータそのものなので、公開も再配布もしない
    # (.gitignore と FTPの除外設定で外している)
    DATA.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["No", "抽選日", "当選数字"])
        for d in best:
            w.writerow([d["round"], d["date"].strftime("%Y/%m/%d"), d["number"]])
    return best


def load_draws() -> list[dict]:
    """[{round, date, number}, ...] を古い順で返す"""
    draws = []
    with CSV_PATH.open(encoding="utf-8") as f:
        for row in list(csv.reader(f))[1:]:
            if row and row[0].strip().isdigit():
                num = row[2].strip().strip('"').lstrip("=").strip('"')
                draws.append({"round": int(row[0]),
                              "date": datetime.strptime(row[1].strip(), "%Y/%m/%d").date(),
                              "number": num.zfill(3)})
    if not draws:
        raise DataError("CSVから1件も読み込めなかった")
    return draws


def validate(draws: list[dict], today: date | None = None) -> None:
    """データ鮮度フェイルクローズ。異常なら DataError を投げて生成を止める"""
    today = today or now_jst().date()
    rounds = [d["round"] for d in draws]
    if len(set(rounds)) != len(rounds):
        raise DataError("回号に重複がある")
    if rounds != sorted(rounds):
        raise DataError("回号が昇順になっていない")
    gaps = [b - a for a, b in zip(rounds, rounds[1:]) if b - a != 1]
    if gaps:
        raise DataError(f"回号に飛びがある: {len(gaps)}箇所")
    if any(len(d["number"]) != 3 or not d["number"].isdigit() for d in draws):
        raise DataError("当選番号の形式が不正")
    last = draws[-1]
    gap_days = (today - last["date"]).days
    if gap_days > MAX_GAP_DAYS:
        raise DataError(
            f"最新データが第{last['round']}回({last['date']})で{gap_days}日前。"
            f"許容{MAX_GAP_DAYS}日を超えたため候補生成を停止", kind="stale")
    if gap_days < 0:
        raise DataError(f"最新抽選日({last['date']})が未来になっている")


def is_draw_day(d: date) -> bool:
    """抽選がある日か

    全履歴を調べた結果、抽選がないのは土日と年末年始(12/31〜1/3)だけで、
    祝日には抽選がある(例: 2026-09-21 敬老の日 = 第7075回)。
    """
    if d.weekday() >= 5:
        return False
    if (d.month, d.day) in {(12, 31), (1, 1), (1, 2), (1, 3)}:
        return False
    return True


def expected_next_draw_date(last_date: date) -> date:
    """最終抽選日の次の抽選日を返す"""
    d = last_date + timedelta(days=1)
    while not is_draw_day(d):
        d += timedelta(days=1)
    return d


def check_target_not_drawn(draws: list[dict], now: datetime | None = None) -> None:
    """予想対象の回がすでに抽選済みでないかを検査する

    データ元の更新が遅れていると、すでに抽選が終わった回を「次回」として
    予想してしまう。現行サイトが4回分ずれていたのはこの検査がなかったため。
    """
    now = now or now_jst()
    last = draws[-1]
    target_date = expected_next_draw_date(last["date"])
    draw_at = datetime.combine(target_date, datetime.min.time(), tzinfo=JST).replace(
        hour=DRAW_TIME[0], minute=DRAW_TIME[1])
    if now >= draw_at:
        elapsed = (now - draw_at).total_seconds() / 3600
        kind = "awaiting" if elapsed < AWAIT_HOURS else "stale"
        raise DataError(
            f"第{last['round'] + 1}回は{target_date} "
            f"{DRAW_TIME[0]}:{DRAW_TIME[1]:02d}に抽選済み。結果の取り込み待ち"
            f"（抽選から{elapsed:.1f}時間経過 / 手元の最新は第{last['round']}回 {last['date']}）",
            kind=kind)


def mini_prize_map() -> dict[int, int]:
    """回号 → ミニの当選金額。取得元の生データから読む(無ければ空)"""
    out = {}
    for name, enc, col in (("loto-life", "cp932", 12), ("mk-mode", "utf-8", 13)):
        f = SOURCES_DIR / f"{name}.csv"
        if not f.exists():
            continue
        try:
            rows = list(csv.reader(f.read_text(encoding=enc).splitlines()))[1:]
        except (UnicodeDecodeError, OSError):
            continue
        for row in rows:
            if row and row[0].strip().isdigit() and len(row) > col:
                v = row[col].strip().replace(",", "")
                if v.isdigit() and int(v) > 0:
                    out.setdefault(int(row[0]), int(v))
        if out:
            break
    return out


def mini_prize_stats() -> dict:
    """取得元の生データからミニの当選金額を集計する(無ければNone)"""
    f = SOURCES_DIR / "loto-life.csv"
    if not f.exists():
        return {}
    vals = []
    try:
        for row in list(csv.reader(f.read_text(encoding="cp932").splitlines()))[1:]:
            if row and row[0].strip().isdigit() and int(row[0]) >= 6000 and len(row) > 12:
                v = row[12].strip()
                if v.isdigit() and int(v) > 0:
                    vals.append(int(v))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not vals:
        return {}
    vals.sort()
    mean = sum(vals) / len(vals)
    return {"n": len(vals), "mean": round(mean), "median": vals[len(vals) // 2],
            "min": vals[0], "max": vals[-1],
            "expected_value": round(mean * 0.01)}      # 1口200円あたりの期待値


# ---------------------------------------------------------------- 候補の選出

def tag_points(prev_number: str) -> dict:
    """前回の当選番号から見た、各数字の原典ポイント(0〜4)

    宝島本の4手法に該当するかを数える。いずれも「前回番号との関係」で決まるため、
    特定の数字が固定的に有利になることはない(前回番号が毎回変わるため)。
    """
    a, b, c = (int(x) for x in prev_number)
    pull = set(prev_number)
    slide = {str((int(p) + s) % 10) for p in prev_number for s in (1, -1)}
    ura = {str((int(p) + 5) % 10) for p in prev_number}
    awase = {str((a + b) % 10), str((b + c) % 10), str((a + c) % 10)}
    return {d: int(d in pull) + int(d in slide) + int(d in ura) + int(d in awase)
            for d in DIGITS}


def pick(target_round: int, prev_number: str) -> dict:
    """210組から1組を選ぶ

    完全な一様ではなく、宝島本の手法に該当する数字を多く含む組を
    exp(TILT_WEIGHT × 該当点)の比率でわずかに選ばれやすくする。
    全履歴3000回の検証で、この傾きを入れても数字ごとの採用率に偏りは出ない
    (採用χ²=4.8 / 一様のχ²=4.0、いずれも5%限界16.92を大きく下回る)。

    乱数の種は「回号 + 直近確定番号」。実行環境や時刻には依存しない。
    """
    pts = tag_points(prev_number)
    weights = [math.exp(TILT_WEIGHT * sum(pts[d] for d in c)) for c in COMBOS]
    total = sum(weights)
    material = f"{target_round}:{prev_number}:{SELECTION_VERSION}"
    h = hashlib.sha256(material.encode()).hexdigest()
    u = int(h[:16], 16) / 2 ** 64            # [0,1)の一様乱数
    acc = 0.0
    chosen, prob = COMBOS[-1], weights[-1] / total
    for combo, wt in zip(COMBOS, weights):
        acc += wt / total
        if u < acc:
            chosen, prob = combo, wt / total
            break
    uniform = 1 / len(COMBOS)
    probs = [x / total for x in weights]
    effective = 1 / sum(x * x for x in probs)   # 有効候補数(1/Σp²)。一様なら210
    return {
        "combo": list(chosen), "seed_material": material, "hash": h,
        "effective_combos": round(effective, 1),
        "tilt_weight": TILT_WEIGHT,
        "tag_points": pts,
        "probability": round(prob, 6),
        "probability_uniform": round(uniform, 6),
        "ratio_to_uniform": round(prob / uniform, 2),
        "spread": {"max": round(max(weights) / total / uniform, 2),
                   "min": round(min(weights) / total / uniform, 2)},
    }


# ---------------------------------------------------------------- 後付けタグ

# 各タグの「実測的中率」は全履歴7080回の検定値。基準27.10%と比べて差がないことを
# そのまま表示する。読み物として出すが、当たりやすさを主張しないための数値。
TAG_STATS = {
    "ひっぱり": ("前回の当選番号に含まれていた数字", 27.26),
    "スライド": ("前回の当選番号の±1の数字", 27.12),
    "裏数字": ("前回の当選番号に5を足した数字", 27.36),
    "合わせ数字": ("前回の桁同士を足した下1桁", 26.94),
}


def tags_for(digit: str, hist: list[str], combo: list[str]) -> list[dict]:
    """選ばれた数字に後付けの理由タグを付ける。選出には一切影響しない"""
    prev = hist[-1]
    out = []
    if digit in prev:
        out.append("ひっぱり")
    if any(digit == str((int(p) + s) % 10) for p in prev for s in (1, -1)):
        out.append("スライド")
    if any(digit == str((int(p) + 5) % 10) for p in prev):
        out.append("裏数字")
    a, b, c = (int(x) for x in prev)
    if int(digit) in {(a + b) % 10, (b + c) % 10, (a + c) % 10}:
        out.append("合わせ数字")
    res = [{"name": t, "desc": TAG_STATS[t][0], "hit_rate": TAG_STATS[t][1]}
           for t in out]
    # 統計的な状況も添える(タグではなく事実の提示)
    gap = next((i for i, n in enumerate(reversed(hist)) if digit in n), len(hist))
    res.append({"name": f"{gap}回ご無沙汰" if gap else "前回登場",
                "desc": f"直近{gap}回このデジットは出ていない" if gap
                        else "前回の当選番号に含まれていた",
                "hit_rate": None})
    return res


# ---------------------------------------------------------------- 統計

def chi2(counts: Counter, n: int) -> float:
    exp = n / 10
    return sum((counts[d] - exp) ** 2 / exp for d in DIGITS)


def rule_tests(numbers: list[str]) -> list[dict]:
    """原典ルールの検定。基準は『どの数字も3桁中に出る確率』1-0.9^3"""
    base = 1 - 0.9 ** 3
    N = len(numbers)

    def z(hit, tot):
        p = hit / tot
        return (p - base) / math.sqrt(base * (1 - base) / tot), p

    defs = {
        "ひっぱり": lambda d: [d],
        "スライド": lambda d: [str((int(d) + 1) % 10), str((int(d) - 1) % 10)],
        "裏数字": lambda d: [str((int(d) + 5) % 10)],
    }
    out = []
    for name, gen in defs.items():
        hit = tot = 0
        for i in range(N - 1):
            for d in set(numbers[i]):
                for cand in gen(d):
                    tot += 1
                    hit += cand in numbers[i + 1]
        zz, p = z(hit, tot)
        out.append({"name": name, "rate": round(p * 100, 2), "base": round(base * 100, 2),
                    "z": round(zz, 2), "n": tot,
                    "verdict": "有意差なし" if abs(zz) < 1.96 else "有意"})
    # 合わせ数字
    hit = tot = 0
    for i in range(N - 1):
        a, b, c = (int(x) for x in numbers[i])
        for s in {(a + b) % 10, (b + c) % 10, (a + c) % 10}:
            tot += 1
            hit += str(s) in numbers[i + 1]
    zz, p = z(hit, tot)
    out.append({"name": "合わせ数字", "rate": round(p * 100, 2),
                "base": round(base * 100, 2), "z": round(zz, 2), "n": tot,
                "verdict": "有意差なし" if abs(zz) < 1.96 else "有意"})
    return out


def build_stats(draws: list[dict]) -> dict:
    numbers = [d["number"] for d in draws]
    N = len(numbers)
    allc = Counter("".join(numbers))
    pos = [Counter(n[p] for n in numbers) for p in range(3)]
    hi = Counter("".join(d["number"] for d in draws if d["date"].month in (6, 7, 8, 9)))
    lo = Counter("".join(d["number"] for d in draws if d["date"].month in (12, 1, 2, 3)))
    tot = sum(hi.values()) + sum(lo.values())
    homo = sum((grp[d] - (hi[d] + lo[d]) * gn / tot) ** 2 / ((hi[d] + lo[d]) * gn / tot)
               for d in DIGITS for grp, gn in ((hi, sum(hi.values())), (lo, sum(lo.values()))))
    last2 = [n[1:] for n in numbers]
    c2 = Counter(last2)
    exp2 = N / 100
    chi2_last2 = sum((c2[f"{i:02d}"] - exp2) ** 2 / exp2 for i in range(100))
    return {
        "draws": N,
        "mini": {
            "chi2_last2": round(chi2_last2, 1), "chi2_last2_limit": 123.2,
            "top": [{"n": k, "c": v} for k, v in c2.most_common(3)],
            "bottom": [{"n": k, "c": v} for k, v in c2.most_common()[-3:]],
            "expected": round(exp2, 1),
        },
        "span": [str(draws[0]["date"]), str(draws[-1]["date"])],
        "digit_counts": {d: allc[d] for d in DIGITS},
        "digit_expected": round(N * 3 / 10, 1),
        "chi2_all": round(chi2(allc, N * 3), 2),
        "chi2_pos": [round(chi2(c, N), 2) for c in pos],
        "chi2_limit_5": 16.92,
        "chi2_limit_1": 21.67,
        "pos_counts": [{d: c[d] for d in DIGITS} for c in pos],
        "rules": rule_tests(numbers),
        "season": {"humid_draws": sum(hi.values()) // 3, "dry_draws": sum(lo.values()) // 3,
                   "homogeneity_chi2": round(homo, 2)},
        "double_rate": round(sum(1 for n in numbers if len(set(n)) == 2) / N * 100, 2),
        "triple_rate": round(sum(1 for n in numbers if len(set(n)) == 1) / N * 100, 2),
        "sum_mean": round(sum(sum(int(c) for c in n) for n in numbers) / N, 3),
    }


# ---------------------------------------------------------------- 結果の判定

def judge(combo: list[str], number: str) -> dict:
    """候補と当選番号を突き合わせる(ミニ=下2桁が判定の主役)"""
    counts = Counter(number)
    last2 = number[1:]                       # 十の位・一の位
    in_set = [ch in combo for ch in last2]
    mini_hit = all(in_set)                   # 16通りの買い目に入っていたか
    covered = [ch for ch in number if ch in combo]
    uniq = sorted(set(covered))
    triple = [d for d, c in counts.items() if c == 3]
    double = [d for d, c in counts.items() if c == 2]

    if mini_hit:
        label = f"下2桁「{last2}」は候補16通りの中にあった（ミニ的中）"
    elif any(in_set):
        got = last2[0] if in_set[0] else last2[1]
        miss = last2[1] if in_set[0] else last2[0]
        pos = "十の位" if in_set[0] else "一の位"
        label = (f"下2桁のうち{pos}の「{got}」は候補にあったが、"
                 f"もう一方の「{miss}」が候補外だった")
    else:
        label = f"下2桁「{last2}」はどちらも候補外だった"
    if triple:
        label += "【トリプル発生】"
    elif double:
        label += "【ダブル発生】"

    tickets = sorted({a + b for a in combo for b in combo})
    return {
        "mini_hit": mini_hit,
        "last2": last2,
        "tickets": len(tickets),
        "cost": len(tickets) * TICKET_PRICE,
        "covered": len(covered), "digits": uniq,
        "all3": set(number) <= set(combo),
        "label": label,
        "detail": {
            "sum": sum(int(c) for c in number),
            "pattern": "トリプル" if triple else "ダブル" if double else "3桁すべて異なる",
            "missed": sorted(set(number) - set(combo)),
            "mini_tickets": [a + b for a in sorted(combo) for b in sorted(combo)],
        },
    }


# ---------------------------------------------------------------- 保存版ページ

SNAPSHOT_KEYS = ("logic_version", "generated_at", "target_round", "candidates",
                 "basis", "selection", "odds", "tags", "ai", "hypotheses",
                 "ai_comments")


def save_snapshot(latest: dict) -> None:
    """その回の予想ページを丸ごと保存する(後から結果を書き足す)"""
    ROUNDS.mkdir(parents=True, exist_ok=True)
    f = ROUNDS / f"{latest['target_round']}.json"
    snap = {k: latest[k] for k in SNAPSHOT_KEYS if k in latest}
    # 仮説は「予測だけ」を焼き込む(集計はその時点のものなので保存しない)
    if "hypotheses" in snap:
        snap["hypotheses"] = [{"id": h["id"], "prediction": h["prediction"],
                               "formula": h["formula"]} for h in snap["hypotheses"]]
    if f.exists():                      # 既存の結果欄は保持する
        old = json.loads(f.read_text(encoding="utf-8"))
        for k in ("result", "outcome"):
            if k in old:
                snap[k] = old[k]
    f.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")


def fill_results(draws: list[dict]) -> None:
    """抽選が終わった回のスナップショットに、実際の結果と検証を書き足す"""
    if not ROUNDS.exists():
        return
    results = {d["round"]: d for d in draws}
    prizes = mini_prize_map()
    for f in ROUNDS.glob("*.json"):
        if f.name == "index.json":
            continue
        snap = json.loads(f.read_text(encoding="utf-8"))
        simulated = bool((snap.get("result") or {}).get("SIMULATED"))
        if snap.get("result") and not simulated:
            # 既存の回でも、所見が未取得なら後から付ける
            if "ai_comments" not in snap and "--no-ai" not in sys.argv and snap.get("candidates"):
                oc0 = snap.get("outcome") or {}
                num = snap["result"]["number"]
                ctx = (f"第{snap['target_round']}回の結果が出ました。\n"
                       f"- サイトの候補4数字: {','.join(snap['candidates'])}"
                       f"（これで作る買い目は16通り、3,200円）\n"
                       f"- 当選番号: {num}（ミニの対象は下2桁の {num[1:]}）\n"
                       f"- 判定: {'ミニ的中' if oc0.get('mini_hit') else '外れ'}")
                got = auto_debate.reflect(snap["target_round"], ctx)
                if got:
                    snap["ai_comments"] = got
                    f.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
                    print(f"  第{snap['target_round']}回への所見を追加")
            # 金額や収支が未記入なら補う
            oc = snap.get("outcome") or {}
            if "profit" not in oc and snap.get("candidates"):
                oc = judge(snap["candidates"], snap["result"]["number"])
                prize = prizes.get(snap["target_round"])
                if prize:
                    snap["result"]["mini_prize"] = prize
                oc["prize"] = prize if (prize and oc["mini_hit"]) else 0
                oc["profit"] = (oc["prize"] or 0) - oc["cost"]
                snap["outcome"] = oc
                f.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
            continue
        if snap["target_round"] not in results:
            continue
        d = results[snap["target_round"]]
        snap["result"] = {"number": d["number"], "date": str(d["date"])}
        snap["outcome"] = judge(snap["candidates"], d["number"])
        if "--no-ai" not in sys.argv:
            oc0 = snap["outcome"]
            ctx = (f"第{snap['target_round']}回の結果が出ました。\n"
                   f"- サイトの候補4数字: {','.join(snap['candidates'])}"
                   f"（これで作る買い目は16通り、3,200円）\n"
                   f"- 当選番号: {d['number']}（ミニの対象は下2桁の {d['number'][1:]}）\n"
                   f"- 判定: {'ミニ的中' if oc0['mini_hit'] else '外れ'}\n"
                   f"- 3つのAIが宣言ルールで出した本命(下2桁): "
                   + " / ".join(f"{p0['label']} {p0.get('mini', '—')}"
                                for p0 in (snap.get("ai", {}).get("picks", {}) or {}).values()))
            got = auto_debate.reflect(snap["target_round"], ctx)
            if got:
                snap["ai_comments"] = got
                print(f"  第{snap['target_round']}回への所見: "
                      + " / ".join(v["label"] for v in got.values()))
        prize = prizes.get(snap["target_round"])
        if prize:
            snap["result"]["mini_prize"] = prize
        oc = snap["outcome"]
        oc["prize"] = prize if (prize and oc["mini_hit"]) else 0
        oc["profit"] = (oc["prize"] or 0) - oc["cost"]
        # AIの予想も同じ当選番号で採点する
        for name, pick in (snap.get("ai", {}).get("picks", {}) or {}).items():
            if pick.get("status") == "ok":
                cov = [ch for ch in d["number"] if ch in pick.get("candidates", [])]
                pick["outcome"] = {"covered": len(cov),
                                   "straight_hit": pick.get("straight") == d["number"]}
        f.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")


def build_rounds_index() -> list[dict]:
    """保存版ページの一覧"""
    if not ROUNDS.exists():
        return []
    out = []
    for f in sorted(ROUNDS.glob("*.json"), key=lambda x: -int(x.stem) if x.stem.isdigit() else 0):
        if not f.stem.isdigit():
            continue
        snap = json.loads(f.read_text(encoding="utf-8"))
        out.append({
            "round": snap["target_round"],
            "generated_at": snap["generated_at"][:10],
            "candidates": snap["candidates"],
            "result": (snap.get("result") or {}).get("number"),
            "covered": (snap.get("outcome") or {}).get("covered"),
            "label": (snap.get("outcome") or {}).get("label", "抽選前"),
            "mini_hit": (snap.get("outcome") or {}).get("mini_hit"),
            "cost": (snap.get("outcome") or {}).get("cost"),
            "prize": (snap.get("outcome") or {}).get("prize"),
            "profit": (snap.get("outcome") or {}).get("profit"),
        })
    (ROUNDS / "index.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def tally_ai(draws: list[dict]) -> dict:
    """保存版ページを集計して、AIごとの通算成績を出す"""
    results = {d["round"]: d["number"] for d in draws}
    tally = {k: {"label": v["label"], "n": 0, "straight": 0, "covered": 0,
                 "atleast1": 0, "all3": 0, "history": []}
             for k, v in ai_rules.RULES.items()}
    if not ROUNDS.exists():
        return tally
    for f in sorted(ROUNDS.glob("*.json"), key=lambda x: int(x.stem) if x.stem.isdigit() else 0):
        if not f.stem.isdigit():
            continue
        snap = json.loads(f.read_text(encoding="utf-8"))
        actual = results.get(snap["target_round"])
        if not actual:
            continue
        for name, p in (snap.get("ai", {}).get("picks", {}) or {}).items():
            if name not in tally or p.get("status") != "ok":
                continue
            t = tally[name]
            cov = sum(1 for ch in actual if ch in p.get("candidates", []))
            t["n"] += 1
            t["covered"] += cov
            t["atleast1"] += cov >= 1
            t["all3"] += set(actual) <= set(p.get("candidates", []))
            t["straight"] += p.get("straight") == actual
            t["history"].append({"round": snap["target_round"], "result": actual,
                                 "straight": p.get("straight"),
                                 "candidates": p.get("candidates", []),
                                 "covered": cov,
                                 "straight_hit": p.get("straight") == actual,
                                 "version": p.get("version", "API期")})
    for t in tally.values():
        n = t["n"]
        t["mean_covered"] = round(t["covered"] / n, 3) if n else None
        t["atleast1_rate"] = round(t["atleast1"] / n * 100, 1) if n else None
        t["all3_rate"] = round(t["all3"] / n * 100, 1) if n else None
        t["history"] = t["history"][-30:]
    return tally


def build_hypotheses(draws: list[dict], target_round: int) -> list[dict]:
    """登録済み仮説について、今回の予測と、登録後の実績を集計する"""
    results = {d["round"]: d["number"] for d in draws}
    out = []
    for h in hypotheses.REGISTRY:
        records = []
        if ROUNDS.exists():
            for f in sorted(ROUNDS.glob("*.json"), key=lambda x: int(x.stem) if x.stem.isdigit() else 0):
                if not f.stem.isdigit():
                    continue
                snap = json.loads(f.read_text(encoding="utf-8"))
                rnd = snap["target_round"]
                if rnd < h["registered_round"] or rnd not in results:
                    continue
                for rec in snap.get("hypotheses", []):
                    if rec.get("id") == h["id"]:
                        records.append({"round": rnd, "prediction": rec["prediction"],
                                        "result": results[rnd]})
        out.append({
            "id": h["id"], "title": h["title"],
            "registered_round": h["registered_round"], "registered_at": h["registered_at"],
            "claim": h["claim"], "origin": h["origin"], "formula": h["formula"],
            "conditions": h["conditions"], "min_rounds": h["min_rounds"],
            "threshold_p": h["threshold_p"],
            "prediction": hypotheses.predict(h, draws[-1]["number"], target_round),
            "record": hypotheses.evaluate(h, records),
            "history": records[-30:],
        })
    return out


RECORD_HEADER = (["回号", "生成日時", "候補", "種の材料", "ハッシュ", "選出確率",
                  "一様比", "有効候補数"]
                 + [f"原典pt_{d}" for d in DIGITS]
                 + ["当選番号", "下2桁", "ミニ的中", "購入額", "当選額", "収支"])


def write_record_csv() -> int:
    """各回の候補を第三者が再現・検証できるCSVを書き出す

    ChatGPT提案(selected/hit/payout/cost/ハッシュ)と
    Gemini提案(各数字の重み合計)を1つの表にまとめたもの。
    """
    if not ROUNDS.exists():
        return 0
    rows = []
    for f in sorted(ROUNDS.glob("[0-9]*.json"), key=lambda x: int(x.stem)):
        snap = json.loads(f.read_text(encoding="utf-8"))
        sel = snap.get("selection", {})
        oc = snap.get("outcome") or {}
        res = snap.get("result") or {}
        pts = sel.get("tag_points") or {}
        rows.append([
            snap["target_round"], snap.get("generated_at", ""),
            ",".join(snap.get("candidates", [])),
            sel.get("seed_material", ""), sel.get("hash", ""),
            sel.get("probability", ""), sel.get("ratio_to_uniform", ""),
            sel.get("effective_combos", ""),
            *[pts.get(d, "") for d in DIGITS],
            res.get("number", ""), oc.get("last2", ""),
            "" if not res else ("1" if oc.get("mini_hit") else "0"),
            oc.get("cost", ""), oc.get("prize", ""), oc.get("profit", ""),
        ])
    with (DATA / "record.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(RECORD_HEADER)
        w.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------- 生成

def main() -> None:
    global NOW_OVERRIDE
    load_env()
    args = sys.argv[1:]
    for i, a in enumerate(args):            # --now 2026-09-30T20:00 (テスト用)
        if a == "--now" and i + 1 < len(args):
            NOW_OVERRIDE = datetime.fromisoformat(args[i + 1]).replace(tzinfo=JST)
    DATA.mkdir(parents=True, exist_ok=True)

    if "--verify" in args:
        latest = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
        p = pick(latest["target_round"], latest["basis"]["number"])
        ok = p["combo"] == latest["candidates"]
        print(f"再現性: {'一致' if ok else '不一致'} / 候補 {latest['candidates']}")
        sys.exit(0 if ok else 1)

    print("ナンバーズ3 候補生成")
    if "--no-fetch" not in args:
        if fetch_csv() is None:
            if not CSV_PATH.exists():
                raise DataError("どの取得元からも履歴を取れず、ローカルにも無い")
            print("  すべての取得元が失敗 → ローカルの履歴で続行", file=sys.stderr)
    draws = load_draws()
    validate(draws)
    check_target_not_drawn(draws)
    print(f"  履歴 {len(draws)}回 (第{draws[0]['round']}回〜第{draws[-1]['round']}回)")

    last = draws[-1]
    target_round = last["round"] + 1

    # --if-new: すでにこの回号で生成済みなら何もしない (cronで何度も叩くため)
    if "--if-new" in args:
        cur = DATA / "latest.json"
        if cur.exists():
            try:
                done = json.loads(cur.read_text(encoding="utf-8")).get("target_round")
            except json.JSONDecodeError:
                done = None
            if done == target_round:
                print(f"  第{target_round}回向けは生成済み。何もしない")
                sys.exit(3)
    picked = pick(target_round, last["number"])

    # 公開済みの回は候補を作り直さない。最初に出した値を必ず維持する
    existing = ROUNDS / f"{target_round}.json"
    if existing.exists():
        try:
            prev_snap = json.loads(existing.read_text(encoding="utf-8"))
            if prev_snap.get("candidates"):
                if prev_snap["candidates"] != picked["combo"]:
                    print(f"  公開済みの候補を維持: {','.join(prev_snap['candidates'])}"
                          f" (再計算値 {','.join(picked['combo'])} は破棄)", file=sys.stderr)
                picked["combo"] = prev_snap["candidates"]
                if prev_snap.get("selection"):
                    picked["seed_material"] = prev_snap["selection"].get(
                        "seed_material", picked["seed_material"])
                    picked["hash"] = prev_snap["selection"].get("hash", picked["hash"])
        except json.JSONDecodeError:
            pass

    hist = [d["number"] for d in draws]

    latest = {
        "logic_version": LOGIC_VERSION,
        "generated_at": now_jst().isoformat(timespec="seconds"),
        "target_round": target_round,
        "candidates": picked["combo"],
        "basis": {"round": last["round"], "date": str(last["date"]),
                  "number": last["number"]},
        "selection": {
            "method": f"210通り(10C4)から抽選。宝島本の手法に該当する数字を多く含む組を"
                      f"exp({TILT_WEIGHT}×該当点)の比率でやや選ばれやすくしている",
            "seed_material": picked["seed_material"],
            "hash": picked["hash"],
            "combos": len(COMBOS),
            "tilt_weight": picked["tilt_weight"],
            "probability": picked["probability"],
            "probability_uniform": picked["probability_uniform"],
            "ratio_to_uniform": picked["ratio_to_uniform"],
            "spread": picked["spread"],
            "tag_points": picked["tag_points"],
            "effective_combos": picked["effective_combos"],
        },
        "odds": {
            # ミニ（下2桁一致）を基準にした確率。候補4数字で下2桁を組むと 4×4=16通り
            "mini_both": 16.0,        # 下2桁の両方が候補に含まれる
            "mini_either": 64.0,      # 下2桁のどちらかが候補に含まれる
            "mini_none": 36.0,        # どちらも含まれない
            "tickets": 16,            # 16通りを全部買う場合の口数
            "cost": 16 * 200,
            "mini_odds": 1.0,         # ミニ1口の的中確率(%)
            "mini_prize": mini_prize_stats(),
            # 参考: 3桁(ストレート/ボックス)の確率
            "ref_atleast1": 78.4, "ref_all3": 6.4, "ref_expected_covered": 1.2,
        },
        "tags": {d: tags_for(d, hist, picked["combo"]) for d in picked["combo"]},
        "stats": build_stats(draws),
    }
    latest["stale"] = None
    latest["hypotheses"] = build_hypotheses(draws, target_round)

    # 週1回、3者が設計について議論する（提案は保留扱いで自動適用しない）
    if "--no-ai" not in args:
        try:
            rec = latest.get("record", {})
            ctx = (f"現在の対象は第{target_round}回。候補は {','.join(picked['combo'])}。\n"
                   f"選出方式: 210通りから抽選し、宝島本の手法に該当する数字を多く含む組を"
                   f"exp(0.2×該当点)でやや優遇。種は「回号+直近当選番号」のSHA-256。\n"
                   f"全履歴{len(draws)}回の検定では原典4手法いずれも基準27.10%と有意差なし。\n"
                   f"記録: {rec.get('judged', 0)}回分が確定済み、的中{rec.get('hits', 0)}回、"
                   f"通算収支{rec.get('total_profit', 0)}円、回収率は理論43%。\n"
                   f"重要な制約: 提案はサイトに自動適用されない。依頼主が承認して初めて反映される。")
            d = auto_debate.run_weekly(ctx, now_jst())
            if d:
                print(f"  週次討論を実施: 第{d['no']}回「{d['topic']}」"
                      f"（{len(d['transcript'])}発言）")
        except Exception as e:                      # noqa: BLE001
            print(f"  週次討論に失敗: {str(e)[:100]}", file=sys.stderr)
    latest["debates"] = auto_debate.build_index()[:10]
    _d = sorted(auto_debate.DEBATES.glob("[0-9]*.json"),
                key=lambda x: -int(x.stem)) if auto_debate.DEBATES.exists() else []
    latest["debate_latest"] = json.loads(_d[0].read_text(encoding="utf-8")) if _d else None

    # 3つのAIの予想: 各AIが事前に宣言したルールを実行する(その場で考えない)
    if "--no-ai" not in args:
        hist = [{**d, "date": str(d["date"])} for d in draws]
        latest["ai"] = {
            "mode": "pre-committed-rules",
            "decided_at": now_jst().isoformat(timespec="seconds"),
            "picks": ai_rules.run_all(hist, mini=True),
            "changelog": ai_rules.CHANGELOG,
        }
        latest["ai_records"] = tally_ai(draws)
        print("  AI予想(宣言ルールの実行): " + " / ".join(
            f"{v['label']} {v['straight']}" for v in latest["ai"]["picks"].values()))

    (DATA / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")

    # 保存版ページ: 今回分を保存し、抽選済みの回に結果を書き足す
    save_snapshot(latest)
    fill_results(draws)
    rounds_index = build_rounds_index()
    judged = [r for r in rounds_index if r.get("profit") is not None]
    latest["record"] = {
        "count": len(rounds_index),
        "start_round": min((r["round"] for r in rounds_index), default=None),
        "judged": len(judged),
        "total_cost": sum(r["cost"] or 0 for r in judged),
        "total_prize": sum(r["prize"] or 0 for r in judged),
        "total_profit": sum(r["profit"] or 0 for r in judged),
        "hits": sum(1 for r in judged if r.get("mini_hit")),
    }
    (DATA / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"  第{target_round}回の候補: {','.join(picked['combo'])}")
    print(f"  hash: {picked['hash'][:32]}…")
    print(f"  選ばれやすさ: 一様の{picked['ratio_to_uniform']}倍 "
          f"(この回の範囲 {picked['spread']['min']}〜{picked['spread']['max']}倍)")
    for h in latest.get("hypotheses", []):
        r = h["record"]
        print(f"  仮説[{h['id']}] 今回の予測 {h['prediction']} / "
              f"{r['verdict']} 的中{r['hits']}回(期待{r['expected']})")
    n_rec = write_record_csv()
    cmp_f = DATA / "comparison.json"
    if cmp_f.exists():
        try:
            latest["comparison"] = json.loads(cmp_f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    (DATA / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  再現検証用CSV {n_rec}行 (data/record.csv)")
    print(f"  保存版ページ {len(rounds_index)}件 (data/rounds/)")
    print("  data/latest.json を書き出した")


if __name__ == "__main__":
    try:
        main()
    except DataError as e:
        print(f"\n停止: {e}", file=sys.stderr)
        # 候補は更新しないが、サイト側に「更新待ち」であることを伝える
        cur = DATA / "latest.json"
        if cur.exists():
            try:
                d = json.loads(cur.read_text(encoding="utf-8"))
                d["stale"] = {"kind": getattr(e, "kind", "broken"), "reason": str(e),
                              "checked_at": now_jst().isoformat(timespec="seconds")}
                cur.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
                print("  latest.json に更新待ちの状態を記録した", file=sys.stderr)
            except json.JSONDecodeError:
                pass
        sys.exit(2)
