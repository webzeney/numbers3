#!/usr/bin/env python3
"""3つのAIが人間の指示なしで議論する仕組み

- 抽選結果が出た直後: 3者が結果を見て一言ずつ（その回のページに載る）
- 週1回: 設計についての討論を1周（討論ログに積み上がる）

守っていること:
  - AIの提案は「保留中」として記録するだけで、サイトには自動適用しない
    (依頼主の承認制。公開済みの数字が勝手に変わる事故を防ぐため)
  - 1回の討論は3発言まで。費用とノイズを抑える
  - APIが落ちてもサイト生成は止めない
"""
from __future__ import annotations

import json, os, re, ssl, sys, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEBATES = ROOT / "data" / "debates"
JST = timezone(timedelta(hours=9))
MAX_SPEAKERS = 3

PROVIDERS = {
    "gpt": {"label": "ChatGPT", "key": "OPENAI_API_KEY",
            "model_env": "OPENAI_MODEL", "model": "gpt-5.5"},
    "claude": {"label": "Claude", "key": "ANTHROPIC_API_KEY",
               "model_env": "ANTHROPIC_MODEL", "model": "claude-sonnet-5"},
    "gemini": {"label": "Gemini", "key": "GEMINI_API_KEY",
               "model_env": "GEMINI_MODEL", "model": "gemini-3.1-flash-lite"},
}
GEMINI_FALLBACK = ["gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3.1-pro-preview"]

# 週次討論の既定の論点。前回の発言から次の論点を拾えなかった場合に順に使う
DEFAULT_AGENDA = [
    "候補の出し方について、いま計測できていない弱点はどこか",
    "利用者が誤解しやすい表示はどこか。どう直すべきか",
    "宣言した自分のルールに不満はあるか。変えるとしたら何をどう変えるか",
    "このサイトは何を記録し続けるべきか。足りない記録は何か",
    "予測不能なものを扱うとき、AIが陥りやすい失敗は何か",
]


def ssl_ctx() -> ssl.SSLContext:
    for ca in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
        if os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


def post(url: str, payload: dict, headers: dict, timeout: int = 150) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx()) as r:
        return json.loads(r.read())


def call(name: str, system: str, user: str) -> str:
    cfg = PROVIDERS[name]
    key = os.environ.get(cfg["key"])
    if not key:
        raise RuntimeError(f"{cfg['key']} が未設定")
    model = os.environ.get(cfg["model_env"], cfg["model"])
    if name == "gpt":
        d = post("https://api.openai.com/v1/chat/completions",
                 {"model": model, "messages": [{"role": "system", "content": system},
                                               {"role": "user", "content": user}]},
                 {"Authorization": f"Bearer {key}"})
        return d["choices"][0]["message"]["content"].strip()
    if name == "claude":
        d = post("https://api.anthropic.com/v1/messages",
                 {"model": model, "max_tokens": 12000, "system": system,
                  "messages": [{"role": "user", "content": user}]},
                 {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in d.get("content", [])).strip()
    chain = [model] + [m for m in GEMINI_FALLBACK if m != model]
    last = None
    for m in chain:
        try:
            d = post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                     {"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": user}]}]},
                     {"x-goog-api-key": key})
            parts = d.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            txt = "".join(p.get("text", "") for p in parts).strip()
            if txt:
                return txt
        except Exception as e:                      # noqa: BLE001
            last = e
    raise last or RuntimeError("Geminiから応答が無い")


# ---------------------------------------------------------------- 結果への一言

REFLECT_SYSTEM = (
    "あなたはナンバーズ3ミニの候補を出すサイトに参加している3つのAIの1つです。"
    "抽選結果を見て短く所見を述べます。\n\n"
    "厳守すること:\n"
    "- 候補は210通りからのほぼ一様な抽選で選ばれている。当たっても外れても、"
    "選出方法の良し悪しとは無関係である\n"
    "- 的中を『アルゴリズムが傾向を捉えた』『選定の有効性を示す』のように語ってはならない。"
    "それは確率16%の事象が起きただけであり、因果ではない\n"
    "- 外れを『惜しかった』『あと一歩』と語ってもならない。近さに意味はない\n"
    "- 次回の予想や、出やすい数字についての示唆を書いてはならない\n"
    "- 書くべきは、事実の確認、記録上の位置づけ(通算何回目か等)、"
    "確率的に見てどう解釈すべきかの注意喚起のいずれかである\n"
    "- 特筆すべきことが無ければ『特筆すべき点はない』と書いてよい")


def reflect(round_no: int, context: str) -> dict:
    """結果が出た回について、3者が一言ずつ述べる"""
    user = (context + "\n\nこの結果について、あなたの所見を1〜2文（120字以内）で述べてください。"
            "前置きや挨拶は不要。数字の羅列の繰り返しも不要。"
            "的中・外れを選出方法の評価に結びつけないこと。"
            "気づいたことが無ければ『特筆すべき点はない』と書いてください。")
    out = {}
    for name, cfg in PROVIDERS.items():
        try:
            out[name] = {"label": cfg["label"], "text": call(name, REFLECT_SYSTEM, user)[:300]}
        except Exception as e:                      # noqa: BLE001
            print(f"  {cfg['label']}の所見取得に失敗: {str(e)[:80]}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- 週次の討論

DEBATE_SYSTEM = ("あなたはナンバーズ3ミニの候補を出すサイトの設計を議論する3つのAIの1つです。"
                 "ナンバーズが予測不可能であることは前提です。的中率は上げられません。"
                 "その上で、記録の質・表示の誠実さ・検証可能性を良くする議論をしてください。"
                 "抽象論は禁止。計算できる形、実装できる形で述べること。"
                 "他の参加者の発言には具体的に同意または反論すること。")


def weekly_debate(topic: str, context: str) -> dict:
    """1周だけ議論する。発言は3つまで"""
    order = ["claude", "gpt", "gemini"]
    transcript = []
    for name in order[:MAX_SPEAKERS]:
        body = (f"議題: {topic}\n\n{context}\n\n"
                + ("これまでの発言:\n" + "\n\n".join(
                    f"【{t['label']}】{t['text']}" for t in transcript) + "\n\n"
                   if transcript else "あなたが最初の発言者です。\n\n")
                + "400字以内で述べてください。最後の行に『次回の論点: 〜』を1行で書いてください。")
        try:
            text = call(name, DEBATE_SYSTEM, body)
            transcript.append({"name": name, "label": PROVIDERS[name]["label"],
                               "text": text[:1600]})
        except Exception as e:                      # noqa: BLE001
            print(f"  {PROVIDERS[name]['label']}の発言取得に失敗: {str(e)[:80]}", file=sys.stderr)
    next_topic = None
    if transcript:
        m = re.search(r"次回の論点[:：]\s*(.+)", transcript[-1]["text"])
        if m:
            next_topic = m.group(1).strip()[:80]
    return {"topic": topic, "transcript": transcript, "next_topic": next_topic,
            "at": datetime.now(JST).isoformat(timespec="seconds")}


# ---------------------------------------------------------------- 保存と状態

def load_state() -> dict:
    f = DEBATES / "state.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"last_debate": None, "agenda_index": 0, "next_topic": None, "count": 0}


def save_state(state: dict) -> None:
    DEBATES.mkdir(parents=True, exist_ok=True)
    (DEBATES / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def due_for_weekly(state: dict, now: datetime, every_days: int = 7) -> bool:
    last = state.get("last_debate")
    if not last:
        return True
    try:
        return (now - datetime.fromisoformat(last)).days >= every_days
    except ValueError:
        return True


def run_weekly(context: str, now: datetime) -> dict | None:
    state = load_state()
    if not due_for_weekly(state, now):
        return None
    topic = state.get("next_topic") or DEFAULT_AGENDA[state["agenda_index"] % len(DEFAULT_AGENDA)]
    result = weekly_debate(topic, context)
    if not result["transcript"]:
        return None
    DEBATES.mkdir(parents=True, exist_ok=True)
    state["count"] += 1
    result["no"] = state["count"]
    (DEBATES / f"{state['count']:04d}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    state["last_debate"] = now.isoformat(timespec="seconds")
    state["agenda_index"] += 1
    state["next_topic"] = result.get("next_topic")
    save_state(state)
    build_index()
    return result


def build_index() -> list[dict]:
    if not DEBATES.exists():
        return []
    items = []
    for f in sorted(DEBATES.glob("[0-9]*.json"), key=lambda x: -int(x.stem)):
        d = json.loads(f.read_text(encoding="utf-8"))
        items.append({"no": d.get("no"), "at": d.get("at"), "topic": d.get("topic"),
                      "speakers": [t["label"] for t in d.get("transcript", [])],
                      "next_topic": d.get("next_topic")})
    (DEBATES / "index.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return items
