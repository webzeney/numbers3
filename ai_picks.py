#!/usr/bin/env python3
"""3つのAI(ChatGPT / Claude / Gemini)に予想させる

- 1回号につき1回だけ呼び、data/ai/<回号>.json にキャッシュする
- キー未設定・API失敗でもサイト生成は止めない(その枠だけ「未取得」になる)
- 予想の中身に意味はない。当たらないことを記録し続けるための機能である
"""
from __future__ import annotations

import json, os, re, ssl, sys, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
AI_DIR = Path(__file__).resolve().parent / "data" / "ai"

PROVIDERS = {
    "gpt":    {"label": "ChatGPT", "key": "OPENAI_API_KEY",
               "model_env": "OPENAI_MODEL", "model": "gpt-5.5"},
    "claude": {"label": "Claude", "key": "ANTHROPIC_API_KEY",
               "model_env": "ANTHROPIC_MODEL", "model": "claude-sonnet-5"},
    "gemini": {"label": "Gemini", "key": "GEMINI_API_KEY",
               "model_env": "GEMINI_MODEL", "model": "gemini-3.1-flash-lite"},
}
GEMINI_FALLBACK = ["gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3.1-pro-preview"]

SYSTEM = ("あなたはナンバーズ3の予想をする。ナンバーズ3が予測不可能であることは前提として、"
          "その上で必ず具体的な数字を出す。当たらなくても構わない。")
TEMPLATE = """直近の当選番号（新しい順）:
{history}

第{round}回のナンバーズ3について、必ずこの形式だけで出力してください。

本命3桁: XXX
候補4数字: A,B,C,D
理由: (1行、50字以内)"""


def ssl_ctx() -> ssl.SSLContext:
    for ca in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"):
        if os.path.exists(ca):
            return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


def post(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx()) as r:
        return json.loads(r.read())


def call_gpt(key: str, model: str, prompt: str) -> str:
    d = post("https://api.openai.com/v1/chat/completions",
             {"model": model,
              "messages": [{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": prompt}]},
             {"Authorization": f"Bearer {key}"})
    return d["choices"][0]["message"]["content"]


def call_claude(key: str, model: str, prompt: str) -> str:
    d = post("https://api.anthropic.com/v1/messages",
             {"model": model, "max_tokens": 300, "system": SYSTEM,
              "messages": [{"role": "user", "content": prompt}]},
             {"x-api-key": key, "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in d.get("content", []))


def call_gemini(key: str, model: str, prompt: str) -> str:
    chain = [model] + [m for m in GEMINI_FALLBACK if m != model]
    last = None
    for m in chain:
        try:
            d = post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                     {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                      "contents": [{"role": "user", "parts": [{"text": prompt}]}]},
                     {"x-goog-api-key": key})
            parts = d.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
            last = RuntimeError(f"{m}: 応答が空")
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last = e
    raise last


CALLERS = {"gpt": call_gpt, "claude": call_claude, "gemini": call_gemini}


def parse(text: str) -> dict:
    """AIの返答から 本命3桁 / 候補4数字 / 理由 を取り出す"""
    out = {"straight": None, "candidates": [], "reason": "", "raw": text.strip()[:600]}
    m = re.search(r"本命3?桁?\s*[:：]\s*(\d{3})", text)
    if m:
        out["straight"] = m.group(1)
    m = re.search(r"候補4?数字?\s*[:：]\s*([0-9\s,、，]+)", text)
    if m:
        ds = re.findall(r"\d", m.group(1))
        seen = []
        for d in ds:
            if d not in seen:
                seen.append(d)
        out["candidates"] = seen[:4]
    m = re.search(r"理由\s*[:：]\s*(.+)", text)
    if m:
        out["reason"] = m.group(1).strip()[:120]
    return out


def ask_all(target_round: int, history: list[str], force: bool = False) -> dict:
    """3AIに予想させる。キャッシュがあればそれを返す"""
    AI_DIR.mkdir(parents=True, exist_ok=True)
    cache = AI_DIR / f"{target_round}.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text(encoding="utf-8"))

    prompt = TEMPLATE.format(history=" / ".join(history[-10:][::-1]), round=target_round)
    picks = {}
    for name, cfg in PROVIDERS.items():
        key = os.environ.get(cfg["key"])
        if not key:
            picks[name] = {"label": cfg["label"], "status": "未設定",
                           "detail": f"{cfg['key']} が設定されていません"}
            continue
        model = os.environ.get(cfg["model_env"], cfg["model"])
        try:
            text = CALLERS[name](key, model, prompt)
            picks[name] = {"label": cfg["label"], "status": "ok", "model": model,
                           **parse(text)}
        except Exception as e:                      # 1社の失敗で全体を止めない
            msg = str(e)
            if isinstance(e, urllib.error.HTTPError):
                msg = f"HTTP {e.code}"
            picks[name] = {"label": cfg["label"], "status": "取得失敗",
                           "detail": msg[:160], "model": model}
            print(f"  {cfg['label']}: 取得失敗 ({msg[:80]})", file=sys.stderr)
    data = {"round": target_round, "asked_at": datetime.now(JST).isoformat(timespec="seconds"),
            "picks": picks}
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load_records(results: dict[int, str]) -> dict:
    """キャッシュ済みの過去予想を実際の当選番号で採点し、AIごとの通算成績を返す

    results: {回号: 当選番号}
    """
    tally = {k: {"label": v["label"], "n": 0, "straight": 0, "covered": 0,
                 "atleast1": 0, "all3": 0, "history": []}
             for k, v in PROVIDERS.items()}
    for f in sorted(AI_DIR.glob("*.json"), key=lambda p: int(p.stem)) if AI_DIR.exists() else []:
        data = json.loads(f.read_text(encoding="utf-8"))
        rnd = data["round"]
        actual = results.get(rnd)
        if not actual:
            continue
        for name, p in data["picks"].items():
            if name not in tally or p.get("status") != "ok":
                continue
            t = tally[name]
            t["n"] += 1
            cov = sum(1 for ch in actual if ch in p.get("candidates", []))
            t["covered"] += cov
            t["atleast1"] += cov >= 1
            t["all3"] += set(actual) <= set(p.get("candidates", []))
            hit = p.get("straight") == actual
            t["straight"] += hit
            t["history"].append({"round": rnd, "result": actual,
                                 "straight": p.get("straight"),
                                 "candidates": p.get("candidates", []),
                                 "covered": cov, "straight_hit": bool(hit)})
    for t in tally.values():
        n = t["n"]
        t["mean_covered"] = round(t["covered"] / n, 3) if n else None
        t["atleast1_rate"] = round(t["atleast1"] / n * 100, 1) if n else None
        t["all3_rate"] = round(t["all3"] / n * 100, 1) if n else None
        t["history"] = t["history"][-30:]
    return tally


if __name__ == "__main__":
    print(json.dumps(ask_all(int(sys.argv[1]), sys.argv[2:] or ["702"]),
                     ensure_ascii=False, indent=1))
