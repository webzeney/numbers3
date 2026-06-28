import requests
from bs4 import BeautifulSoup
import json
import os
from datetime import datetime, timezone, timedelta
import anthropic
from itertools import combinations

# 日本時間
JST = timezone(timedelta(hours=9))
today = datetime.now(JST).strftime('%Y-%m-%d')

# ===== 当選番号の取得 =====
def get_latest_numbers():
    try:
        url = "https://numbers-renban.tokyo/numbers3/result_all"
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        
        results = []
        rows = soup.find_all('tr')
        for row in rows[:10]:
            cells = row.find_all('td')
            if len(cells) >= 2:
                round_num = cells[0].get_text(strip=True)
                number = cells[1].get_text(strip=True)
                if round_num.isdigit() and len(number) == 3 and number.isdigit():
                    results.append({"round": round_num, "number": number})
        return results
    except Exception as e:
        print(f"取得エラー: {e}")
        return []

# ===== 過去データの読み込み =====
def load_history():
    try:
        with open('data/history.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []

# ===== 候補数字調査A =====
def analyze_A(history):
    if len(history) < 20:
        return None
    
    nums = [h['number'] for h in history]
    
    # 勢い（11〜20回前 vs 1〜10回前）
    g1 = nums[:10]
    g2 = nums[10:20]
    ikioi = {}
    for d in '0123456789':
        rec  = sum(d in n for n in g1)
        prev = sum(d in n for n in g2)
        ikioi[d] = rec - prev

    # 頻度（直近63回）
    r63 = nums[:63] if len(nums) >= 63 else nums
    freq = {d: sum(d in n for n in r63) for d in '0123456789'}

    # ひっぱり系100回合計
    r100 = nums[:100] if len(nums) >= 100 else nums
    pull_total = {d: 0 for d in '0123456789'}
    for gap in [1, 2, 3]:
        for i in range(gap, len(r100)):
            ps = set(r100[i-gap])
            for d in r100[i]:
                if d in ps:
                    pull_total[d] += 1

    # 直近ひっぱりスコア
    pull_score = {}
    for d in '0123456789':
        s = 0
        if len(nums) >= 1 and d in set(nums[0]): s += 3
        if len(nums) >= 2 and d in set(nums[1]): s += 2
        if len(nums) >= 3 and d in set(nums[2]): s += 1
        pull_score[d] = s

    # 連番出現パターン
    renban_count = {d: 0 for d in '0123456789'}
    for i in range(1, len(r100)):
        prev_adj = set()
        for pd in r100[i-1]:
            prev_adj.add(str((int(pd)+1) % 10))
            prev_adj.add(str((int(pd)-1) % 10))
        for d in r100[i]:
            if d in prev_adj:
                renban_count[d] += 1
    renban_next = set()
    for pd in nums[0]:
        renban_next.add(str((int(pd)+1) % 10))
        renban_next.add(str((int(pd)-1) % 10))

    # ポイント計算
    def rank_pt(scores, pts):
        sorted_d = sorted(scores.keys(), key=lambda x: -scores[x])
        return {d: pts[i] if i < len(pts) else 0 for i, d in enumerate(sorted_d)}

    pt_pull  = rank_pt(pull_total, [5,4,3,2,1,0,0,0,0,0])
    pt_freq  = rank_pt(freq,       [4,3,2,1,1,0,0,0,0,0])
    pt_ikioi = {d: (4 if ikioi[d]>=3 else 3 if ikioi[d]>=2 else 2 if ikioi[d]>=1 else 1 if ikioi[d]==0 else 0) for d in '0123456789'}
    pt_ps    = {d: (3 if pull_score[d]>=3 else 2 if pull_score[d]==2 else 1 if pull_score[d]==1 else 0) for d in '0123456789'}

    rn_sorted = sorted(renban_count.keys(), key=lambda x: -renban_count[x])
    rn_rank   = {rn_sorted[i]: [3,2,1][i] if i < 3 else 0 for i in range(10)}
    pt_renban = {d: rn_rank[d] + (1 if d in renban_next else 0) for d in '0123456789'}

    total = {d: pt_pull[d]+pt_freq[d]+pt_ikioi[d]+pt_ps[d]+pt_renban[d] for d in '0123456789'}
    ranking = sorted(total.keys(), key=lambda x: -total[x])

    candidates = ranking[:4]

    # 直近5回照合
    last5 = nums[:5]
    hit_check = {d: sum(d in n for n in last5) for d in candidates}
    in_latest = [d for d in candidates if d in set(nums[0])]

    return {
        "candidates": candidates,
        "scores": {d: total[d] for d in candidates},
        "details": {
            "ikioi": {d: ikioi[d] for d in candidates},
            "freq": {d: freq[d] for d in candidates},
            "pull_total": {d: pull_total[d] for d in candidates},
            "pull_score": {d: pull_score[d] for d in candidates},
            "renban": {d: pt_renban[d] for d in candidates},
        },
        "last5_hit": hit_check,
        "in_latest": in_latest,
        "latest_number": nums[0],
    }

# ===== 候補数字調査B =====
def analyze_B(history, candidates):
    nums = [h['number'] for h in history]
    r100 = nums[:100] if len(nums) >= 100 else nums

    cands_int = [int(d) for d in candidates]
    combos = list(combinations(sorted(cands_int), 3))

    # 総和分析
    sums = [int(n[0])+int(n[1])+int(n[2]) for n in r100]
    from collections import Counter
    sum_count = Counter(sums)
    avg_sum = sum(sums)/len(sums)

    combo_sum_eval = []
    for c in combos:
        s = sum(c)
        cnt = sum_count.get(s, 0)
        zone = '中(10-17)' if 10<=s<=17 else '低(0-9)' if s<=9 else '高(18-27)'
        combo_sum_eval.append({
            "combo": ''.join(map(str,c)),
            "sum": s,
            "count": cnt,
            "zone": zone
        })
    combo_sum_eval.sort(key=lambda x: -x['count'])

    # 最大・最小分析
    maxs = [max(int(d) for d in n) for n in r100]
    mins = [min(int(d) for d in n) for n in r100]
    avg_max = sum(maxs)/len(maxs)
    avg_min = sum(mins)/len(mins)

    combo_maxmin_eval = []
    for c in combos:
        mx = max(c); mn = min(c)
        from collections import Counter as C
        mc = C(maxs); nc = C(mins)
        mx_rank = sorted(mc.keys(), key=lambda x: -mc[x]).index(mx)+1 if mx in mc else 99
        mn_rank = sorted(nc.keys(), key=lambda x: -nc[x]).index(mn)+1 if mn in nc else 99
        combo_maxmin_eval.append({
            "combo": ''.join(map(str,c)),
            "max": mx, "min": mn,
            "max_rank": mx_rank, "min_rank": mn_rank
        })
    combo_maxmin_eval.sort(key=lambda x: x['max_rank']+x['min_rank'])

    # 桁別分析
    pos = {}
    for d in candidates:
        h = sum(1 for n in r100 if n[0]==d)
        t = sum(1 for n in r100 if n[1]==d)
        u = sum(1 for n in r100 if n[2]==d)
        pos[d] = {"h": h, "t": t, "u": u,
                  "best": "百の位" if h>=t and h>=u else "十の位" if t>=u else "一の位"}

    # 推奨順番
    sorted_by_h = sorted(candidates, key=lambda d: -pos[d]["h"])
    sorted_by_t = sorted(candidates, key=lambda d: -pos[d]["t"])
    sorted_by_u = sorted(candidates, key=lambda d: -pos[d]["u"])
    recommended_order = {
        "百の位": sorted_by_h[0],
        "十の位": sorted_by_t[0],
        "一の位": sorted_by_u[0]
    }

    straight_orders = []
    for c in combos:
        combo_str = ''.join(map(str,c))
        digits = list(map(str,c))
        h = max(digits, key=lambda d: pos[d]["h"])
        remaining = [d for d in digits if d != h]
        t = max(remaining, key=lambda d: pos[d]["t"])
        u = [d for d in remaining if d != t][0]
        straight_orders.append({
            "combo": combo_str,
            "straight": h+t+u,
            "reason": f"{h}→百 / {t}→十 / {u}→一"
        })

    return {
        "combo_sum": combo_sum_eval,
        "combo_maxmin": combo_maxmin_eval,
        "position": pos,
        "straight_orders": straight_orders,
        "avg_sum": round(avg_sum, 1),
        "avg_max": round(avg_max, 1),
        "avg_min": round(avg_min, 1),
    }

# ===== 出現間隔アラート =====
def calc_alert(history):
    nums = [h['number'] for h in history]
    alert = {}
    for d in '0123456789':
        intervals = []
        last = -1
        for i, n in enumerate(nums):
            if d in n:
                if last >= 0:
                    intervals.append(i - last)
                last = i
        avg = sum(intervals)/len(intervals) if intervals else 0
        rest = last  # 最後に出てからの回数（新しい順なので逆）
        # 新しい順なので「現在の休止」= 最新から最後の出現まで
        current_rest = 0
        for n in nums:
            if d in n:
                break
            current_rest += 1
        ratio = current_rest / avg if avg > 0 else 0
        level = "🔴" if ratio >= 1.5 else "🟡" if ratio >= 1.0 else "🟢" if ratio >= 0.5 else "⚪"
        alert[d] = {
            "avg_interval": round(avg, 1),
            "current_rest": current_rest,
            "ratio": round(ratio, 2),
            "level": level
        }
    return alert

# ===== Claude AIの思考生成 =====
def generate_ai_thoughts(analysis_a, analysis_b, alert, latest_result, next_round):
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = f"""
あなたはナンバーズ3の候補数字を分析するAIです。
以下のデータをもとに、第{next_round}回の予想に向けた分析思考を
**日本語で・チャット形式で・わかりやすく** 説明してください。

【最新当選番号】
第{latest_result['round']}回：{latest_result['number']}

【候補数字調査A結果】
候補数字：{'・'.join(analysis_a['candidates'])}
スコア：{analysis_a['scores']}
勢い：{analysis_a['details']['ikioi']}
ひっぱり系合計：{analysis_a['details']['pull_total']}
直近5回ヒット：{analysis_a['last5_hit']}
最新回に含まれる候補：{analysis_a['in_latest']}

【候補数字調査B結果】
総和上位：{analysis_b['combo_sum'][:2]}
推奨ストレート順番：{analysis_b['straight_orders']}

【出現間隔アラート（🔴=要注意）】
{[f"{d}:{v['level']} 休止{v['current_rest']}回/平均{v['avg_interval']}回" for d,v in alert.items() if v['level'] in ['🔴','🟡']]}

以下の構成で説明してください：
1. 前回の当選番号の振り返り（ひっぱりの観点）
2. 今回の候補数字の根拠（調査Aのポイント上位の理由）
3. 調査Bからの検証（組み合わせの信頼度）
4. アラートで注目すべき数字
5. 総合的な一言コメント

各項目は2〜4文程度で、専門用語は使わずわかりやすく。
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

# ===== メイン処理 =====
def main():
    print(f"処理開始: {today}")

    # 最新当選番号を取得
    latest = get_latest_numbers()
    if not latest:
        print("当選番号の取得に失敗しました")
        return

    # 履歴を読み込んで最新を追加
    history = load_history()
    existing_rounds = {h['round'] for h in history}
    new_entries = [e for e in latest if e['round'] not in existing_rounds]

    if new_entries:
        history = new_entries + history
        os.makedirs('data', exist_ok=True)
        with open('data/history.json', 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        print(f"新規データ追加: {len(new_entries)}件")
    else:
        print("新規データなし")

    if len(history) < 20:
        print("データ不足（最低20回必要）")
        return

    # 分析実行
    result_a = analyze_A(history)
    result_b = analyze_B(history, result_a['candidates'])
    alert    = calc_alert(history)

    latest_result = history[0]
    next_round = str(int(latest_result['round']) + 1)

    # AI思考生成
    print("AI思考を生成中...")
    ai_thoughts = generate_ai_thoughts(result_a, result_b, alert, latest_result, next_round)

    # アーカイブ保存
    os.makedirs('data/archive', exist_ok=True)
    archive_data = {
        "date": today,
        "latest_round": latest_result['round'],
        "latest_number": latest_result['number'],
        "next_round": next_round,
        "analysis_a": result_a,
        "analysis_b": result_b,
        "alert": alert,
        "ai_thoughts": ai_thoughts,
        "generated_at": datetime.now(JST).isoformat()
    }

    # 最新データ保存
    with open('data/latest.json', 'w', encoding='utf-8') as f:
        json.dump(archive_data, f, ensure_ascii=False, indent=2)

    # アーカイブ保存
    archive_path = f'data/archive/{today}.json'
    with open(archive_path, 'w', encoding='utf-8') as f:
        json.dump(archive_data, f, ensure_ascii=False, indent=2)

    print(f"完了！候補数字: {'・'.join(result_a['candidates'])}")

if __name__ == "__main__":
    main()
