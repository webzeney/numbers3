#!/usr/bin/env python3
"""サイトの動きを時間を進めてシミュレートする (開発用)

実際の generate.py の判定関数をそのまま呼ぶので、本番と同じ挙動になる。
未来の当選番号は存在しないため、架空の結果を使う。

  python3 tools/simulate.py                # 通常ケース(翌朝10:15反映)
  python3 tools/simulate.py same-day       # 当日19:45反映のケース
  python3 tools/simulate.py failure        # データ元が1日止まるケース
"""
import hashlib, sys
from datetime import datetime, date, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate as G

JST = G.JST
START = datetime(2026, 9, 30, 20, 0, tzinfo=JST)
END = datetime(2026, 10, 2, 20, 0, tzinfo=JST)
STEP = timedelta(minutes=30)


def fake_number(rnd: int) -> str:
    """架空の当選番号 (再現可能)"""
    h = hashlib.sha256(f"sim-{rnd}".encode()).hexdigest()
    return f"{int(h[:8], 16) % 1000:03d}"


def build_draws():
    """実履歴 + 架空の未来分"""
    draws = G.load_draws()
    last = draws[-1]
    d = last["date"]
    rnd = last["round"]
    for _ in range(6):
        d = G.expected_next_draw_date(d)
        rnd += 1
        draws.append({"round": rnd, "date": d, "number": fake_number(rnd), "fake": True})
    return draws


def publish_at(draw, mode):
    """その回の結果がデータ元に載る時刻"""
    if mode == "same-day":                     # 当日19:45
        return datetime.combine(draw["date"], time(19, 45), tzinfo=JST)
    t = datetime.combine(G.expected_next_draw_date(draw["date"]), time(10, 15), tzinfo=JST)
    if mode == "failure" and draw["round"] == 7082:   # 1日止まる
        t += timedelta(days=1)
    return t


def run(mode="normal"):
    draws = build_draws()
    print(f"■ シミュレーション: {START:%m/%d %H:%M} 〜 {END:%m/%d %H:%M}  "
          f"(データ元の反映: {'当日19:45' if mode == 'same-day' else '翌営業日10:15'}"
          f"{' / 10/1は障害で1日遅延' if mode == 'failure' else ''})\n")
    print("  抽選予定: " + " / ".join(
        f"第{d['round']}回 {d['date']:%m/%d}" for d in draws[-6:-2]))
    print("  ※未来の当選番号は存在しないので架空の値を使う: " + ", ".join(
        f"第{d['round']}回={d['number']}" for d in draws[-6:-3]) + "\n")

    generated_for = 7082      # 9/30の朝に第7082回向けを生成済み、という前提から開始
    prev_state = None
    now = START
    while now <= END:
        available = [d for d in draws if publish_at(d, mode) <= now]
        last = available[-1]
        target = last["round"] + 1

        # 本番と同じ判定を通す
        try:
            G.validate(available, today=now.date())
            G.check_target_not_drawn(available, now=now)
            err = None
        except G.DataError as e:
            err = e

        if err is None:
            state = "予想公開中" if generated_for == target else "★生成する"
        else:
            state = {"awaiting": "結果待ち(青)", "stale": "取り込み遅延(赤)",
                     "broken": "データ破損(赤)"}[err.kind]

        key = (state, target, last["round"])
        if key != prev_state:
            head = f"{now:%m/%d(%a) %H:%M}"
            if state == "★生成する":
                p = G.pick(target, last["number"])
                print(f"  {head}  ▶ 第{last['round']}回({last['number']})を取り込み → "
                      f"第{target}回向けの候補 {','.join(p['combo'])} を生成")
                print(f"  {'':17}   第{last['round']}回の保存版ページに結果と検証を記入 / "
                      f"3AIの予想を採点 / 次回向けにAIへ1回だけ問い合わせ")
                generated_for = target
                state = "予想公開中"
                key = (state, target, last["round"])
            elif state == "予想公開中":
                print(f"  {head}  ○ 予想公開中: 第{target}回向けの候補を表示 "
                      f"(手元の最新は第{last['round']}回)")
            else:
                mark = "…" if err.kind == "awaiting" else "！"
                print(f"  {head}  {mark} {state}: {err}")
            prev_state = key
        now += STEP

    print(f"\n  終了時点: 第{generated_for}回向けの候補を公開中")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "normal")
