#!/usr/bin/env python3
"""任意の議題で3つのAIに討論させる

GitHub Actions から手動実行する用。PCにAPIキーを置かなくても討論を回せる。

  python3 tools/debate.py "議題" ["補足情報"]

サイトの現況（収支・検証結果・選出方式・直近の結果）は自動で資料に含めるので、
議題だけ渡せば議論が成立する。補足情報は、その議題に固有の数値を足したいときだけ使う。

結果は data/debates/NNNN.json に保存し、index.json と state.json、
さらに data/latest.json の討論欄も更新する（サイトにすぐ反映されるように）。
"""
from __future__ import annotations

import csv, json, re, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auto_debate
from auto_debate import JST, PROVIDERS, call, DEBATES

DATA = ROOT / "data"

SYSTEM = (
    "あなたはナンバーズ3ミニの候補を出すサイトの設計を議論する3つのAIの1つです。"
    "このサイトは、攻略本やネットで『当たる』と言われている手法が本当に当たるのかを"
    "実データで検証する場であり、当てることが目的ではありません。\n\n"
    "姿勢:\n"
    "- 運営者に媚びるな。慰めも励ましも不要。不都合な結論でも言い切ってよい\n"
    "- 現状の実装を擁護する義務はない。変えるべきと思うならそう言え\n"
    "- 『どちらでもよい』『場合による』で終わらせるな。立場を決めて理由を数値で述べよ\n"
    "- ナンバーズが予測不可能であることは前提。的中率は上げられない。"
    "『この方法なら当たる』は書いてはならない\n"
    "- 当落を方法の良し悪しに結びつけるな。的中は確率どおりの事象が起きただけである\n"
    "- 他の参加者の発言には具体的に同意または反論せよ。社交辞令の同意は禁止\n"
    "- 与えられた数値のみを使い、捏造しない\n"
    "- 抽象論は禁止。計算できる形、実装できる形で述べること"
)


def build_context() -> str:
    """サイトの現況を資料にまとめる。議題ごとに書き直さなくて済むようにする"""
    lines = []
    try:
        d = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "（現況データを読み込めませんでした）"

    o = d.get("odds", {})
    sel = d.get("selection", {})
    if d.get("candidates_ten"):
        cand = (f"十の位 {','.join(d['candidates_ten'])} / "
                f"一の位 {','.join(d['candidates_one'])}")
    else:
        cand = ",".join(d.get("candidates", []))
    lines.append(
        f"【現在の設定】\n"
        f"- 対象は第{d.get('target_round')}回。候補は {cand}\n"
        f"- 買い目 {o.get('tickets')}通り（{o.get('cost'):,}円）。ミニ的中率 {o.get('mini_both')}%\n"
        f"- 1口が押さえるのは100通りのうち1通り。ミニ当選金の平均は"
        f"{(o.get('mini_prize') or {}).get('mean', 0):,}円なので、"
        f"1口あたりの期待値は86.37円、期待回収率は43.2%。"
        f"これは口数によらず一定であり、買い方を変えても動かない\n"
        f"- 選出方式: {sel.get('method', '')}\n"
        f"- 傾きの重み TILT_WEIGHT = {sel.get('tilt_weight')}")

    s = d.get("stats", {})
    mr, cl = s.get("mini_rules") or {}, s.get("claims") or {}
    lines.append(
        f"\n【検証の現状】全履歴{s.get('draws', 0):,}回\n"
        f"- 攻略本の鉄則 {mr.get('total', 0)}項目のうち、基準と差があったもの"
        f" {mr.get('significant', 0)}項目\n"
        f"- ネットの攻略サイトの手法 {len(cl.get('items', []))}項目のうち、"
        f"何も考えない場合より当たりやすかったもの {cl.get('useful', 0)}項目\n"
        f"- 桁別ひっぱりの実測: "
        + " / ".join(f"{r['name']}{r['rate']}%" for r in (mr.get("pos_pull") or [])[:3])
        + "（基準10.00%）")

    cmp_ = d.get("comparison") or {}
    if cmp_.get("variants"):
        a, b = cmp_["variants"][0], cmp_["variants"][1]
        diff = cmp_.get("diff") or {}
        lines.append(
            f"\n【重み付けの効果】第{cmp_['span'][0]}回〜第{cmp_['span'][1]}回で比較\n"
            f"- ミニ的中率 {a['mini_rate']}%（重み{a['weight']}） vs "
            f"{b['mini_rate']}%（一様）\n"
            f"- 差 {diff.get('mini_rate')}ポイント、標準誤差 {diff.get('se')}、"
            f"z={diff.get('z')} → {diff.get('verdict')}")

    rec = d.get("record") or {}
    if rec.get("judged"):
        lines.append(
            f"\n【実績】第{rec.get('start_round')}回から{rec['judged']}回判定済み\n"
            f"- 的中{rec.get('hits')}回 / 購入{rec.get('total_cost', 0):,}円 / "
            f"当選{rec.get('total_prize', 0):,}円 / 収支{rec.get('total_profit', 0):,}円"
            f"（回収率{rec.get('total_prize', 0) / max(1, rec.get('total_cost', 1)) * 100:.1f}%、"
            f"理論値43.2%）\n"
            f"- 収支は実際の購入ではなく、買い目を毎回1口ずつ買ったと仮定した計算である")

    # 直近の結果
    try:
        rows = list(csv.DictReader((DATA / "record.csv").open(encoding="utf-8-sig")))
        recent = [r for r in rows if r["ミニ的中"] != ""][-8:]
        if recent:
            lines.append("\n【直近の結果】")
            for r in recent:
                pos = (f"十{r['候補_十の位']}/一{r['候補_一の位']}"
                       if r["候補_十の位"] else r["候補"])
                lines.append(
                    f"  第{r['回号']}回 候補[{pos}] 当選{r['当選番号']}"
                    f"(下2桁{r['下2桁']}) "
                    f"{'的中' if r['ミニ的中'] == '1' else '外れ'} 収支{r['収支']}円")
    except (OSError, KeyError):
        pass

    return "\n".join(lines)


def run(topic: str, extra: str = "") -> dict:
    context = build_context()
    if extra.strip():
        context += f"\n\n【この議題に固有の補足】\n{extra.strip()}"

    transcript = []
    for name in ("claude", "gpt", "gemini"):
        body = (
            f"議題: {topic}\n\n{context}\n\n"
            + ("これまでの発言:\n" + "\n\n".join(
                f"【{t['label']}】{t['text']}" for t in transcript) + "\n\n"
               if transcript else "あなたが最初の発言者です。\n\n")
            + "500字以内で述べてください。冒頭で自分の立場を1文で明示してから"
              "理由を述べること。最後の行に『次回の論点: 〜』を1行で書いてください。")
        try:
            text = call(name, SYSTEM, body)
            transcript.append({"name": name, "label": PROVIDERS[name]["label"],
                               "text": text[:1800]})
            print(f"--- {PROVIDERS[name]['label']} ---\n{text}\n", flush=True)
        except Exception as e:                      # noqa: BLE001
            print(f"[失敗] {PROVIDERS[name]['label']}: {str(e)[:200]}", file=sys.stderr)

    if not transcript:
        raise SystemExit("3者とも応答が得られませんでした。APIキーを確認してください")

    state = auto_debate.load_state()
    state["count"] += 1
    m = re.search(r"次回の論点[:：]\s*(.+)", transcript[-1]["text"])
    result = {"topic": topic, "transcript": transcript,
              "next_topic": m.group(1).strip()[:80] if m else None,
              "at": datetime.now(JST).isoformat(timespec="seconds"),
              "no": state["count"]}
    DEBATES.mkdir(parents=True, exist_ok=True)
    (DEBATES / f"{state['count']:04d}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    state["last_debate"] = result["at"]
    state["agenda_index"] += 1
    state["next_topic"] = result["next_topic"]
    auto_debate.save_state(state)
    index = auto_debate.build_index()

    # サイトが読む latest.json の討論欄も更新する（次の定期生成を待たない）
    f = DATA / "latest.json"
    if f.exists():
        try:
            latest = json.loads(f.read_text(encoding="utf-8"))
            latest["debates"] = index[:10]
            latest["debate_latest"] = result
            f.write_text(json.dumps(latest, ensure_ascii=False, indent=1),
                         encoding="utf-8")
            print("  data/latest.json の討論欄を更新した")
        except json.JSONDecodeError:
            print("  latest.json が壊れているため更新を見送った", file=sys.stderr)

    print(f"保存: data/debates/{state['count']:04d}.json "
          f"（第{state['count']}回・{len(transcript)}発言）")
    return result


if __name__ == "__main__":
    auto_debate_topic = sys.argv[1] if len(sys.argv) > 1 else ""
    if not auto_debate_topic.strip():
        raise SystemExit("議題を指定してください: python3 tools/debate.py \"議題\"")
    run(auto_debate_topic, sys.argv[2] if len(sys.argv) > 2 else "")
