import requests
from bs4 import BeautifulSoup
import json
import os
import re
from datetime import datetime, timezone, timedelta
import anthropic
from itertools import combinations
from collections import Counter

JST = timezone(timedelta(hours=9))
today = datetime.now(JST).strftime('%Y-%m-%d')


# ===== 当選番号の取得 =====
def get_latest_numbers():

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
    }

    def parse_rakuten(html):
        """楽天宝くじ専用パーサー"""
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        seen_rounds = set()

        # ナンバーズ3の回号は現実的に4桁（6000〜9999）の範囲に限定
        def is_valid_round(s):
            return s.isdigit() and len(s) == 4 and 6000 <= int(s) <= 9999

        def is_valid_number(s):
            return s.isdigit() and len(s) == 3

        # テーブルから回号と番号を探す（同じ行内のペアのみ採用）
        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')
            for row in rows:
                cells = row.find_all(['td', 'th'])
                texts = [re.sub(r'[^\d]', '', c.get_text(strip=True)) for c in cells]
                round_num = None
                number = None
                for t in texts:
                    if is_valid_round(t) and round_num is None:
                        round_num = t
                    elif is_valid_number(t) and number is None:
                        number = t
                if round_num and number and round_num not in seen_rounds:
                    results.append({"round": round_num, "number": number})
                    seen_rounds.add(round_num)

        # テーブルで見つからない場合はテキストから「第XXXX回」パターンのみ厳密に抽出
        if not results:
            text = soup.get_text()
            pattern = re.findall(r'第\s*(\d{4})\s*回[^\d]{0,15}?(\d{3})(?!\d)', text)
            for round_num, number in pattern:
                if is_valid_round(round_num) and round_num not in seen_rounds:
                    results.append({"round": round_num, "number": number})
                    seen_rounds.add(round_num)
                    if len(results) >= 30:
                        break

        return results

    def parse_renban(html):
        """numbers-renban専用パーサー"""
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        seen_rounds = set()

        # すべてのtr要素を確認
        for row in soup.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) >= 2:
                for i in range(len(cells)-1):
                    t1 = cells[i].get_text(strip=True)
                    t2 = cells[i+1].get_text(strip=True)
                    if t1.isdigit() and len(t1) >= 4 and t2.isdigit() and len(t2) == 3:
                        if t1 not in seen_rounds:
                            results.append({"round": t1, "number": t2})
                            seen_rounds.add(t1)

        # aタグからも探す
        if not results:
            text = soup.get_text()
            nums_pattern = re.findall(r'(\d{4,5})[^\d]{1,10}(\d{3})', text)
            for round_num, number in nums_pattern:
                if round_num not in seen_rounds and len(number) == 3:
                    results.append({"round": round_num, "number": number})
                    seen_rounds.add(round_num)
                    if len(results) >= 30:
                        break

        return results

    all_results = {}

    # ===== 楽天宝くじ：月別ページを複数取得 =====
    print("楽天宝くじ（月別）から取得を試みます...")
    now_jst = datetime.now(JST)
    for month_offset in range(6):  # 直近6ヶ月分
        target = now_jst - timedelta(days=30*month_offset)
        year = target.year
        month = target.month
        url = f"https://takarakuji.rakuten.co.jp/backnumber/numbers3/{year}{month:02d}/"
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                results = parse_rakuten(r.text)
                if results:
                    for item in results:
                        if item['round'] not in all_results:
                            all_results[item['round']] = item['number']
                    print(f"  {year}/{month:02d}: {len(results)}件取得（累計{len(all_results)}件）")
                else:
                    print(f"  {year}/{month:02d}: データなし")
            else:
                print(f"  {year}/{month:02d}: {r.status_code}")
        except Exception as e:
            print(f"  {year}/{month:02d}: エラー {e}")

    if len(all_results) >= 20:
        sorted_results = sorted(
            [{"round": r, "number": n} for r,n in all_results.items()],
            key=lambda x: -int(x['round'])
        )
        print(f"楽天宝くじから合計{len(sorted_results)}件取得成功")
        print(f"最新: 第{sorted_results[0]['round']}回 {sorted_results[0]['number']}")
        return sorted_results

    # ===== numbers-renban =====
    print("numbers-renban から取得を試みます...")
    try:
        r = requests.get(
            "https://numbers-renban.tokyo/numbers3/result_all",
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            results = parse_renban(r.text)
            if results and len(results) >= 10:
                for item in results:
                    if item['round'] not in all_results:
                        all_results[item['round']] = item['number']
                print(f"  numbers-renban: {len(results)}件取得")
    except Exception as e:
        print(f"  numbers-renban エラー: {e}")

    if len(all_results) >= 20:
        sorted_results = sorted(
            [{"round": r, "number": n} for r,n in all_results.items()],
            key=lambda x: -int(x['round'])
        )
        print(f"合計{len(sorted_results)}件取得成功")
        return sorted_results

    # ===== PayPay銀行 =====
    print("PayPay銀行から取得を試みます...")
    try:
        r = requests.get(
            "https://www.paypay-bank.co.jp/lottery/numbers/n3recent.html",
            headers=headers, timeout=15
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            text = soup.get_text()
            pattern = re.findall(r'第?\s*(\d{4,5})\s*回[^\d]{0,20}?(\d{3})', text)
            for round_num, number in pattern:
                if round_num not in all_results:
                    all_results[round_num] = number
            print(f"  PayPay銀行: {len(pattern)}件取得")
    except Exception as e:
        print(f"  PayPay銀行 エラー: {e}")

    if all_results:
        sorted_results = sorted(
            [{"round": r, "number": n} for r,n in all_results.items()],
            key=lambda x: -int(x['round'])
        )
        print(f"合計{len(sorted_results)}件取得")
        return sorted_results

    print("全サイトの取得に失敗しました")
    return []


# ===== 過去データの読み込み =====
def load_history():
    try:
        with open('data/history.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        # 不正データを除外：回号は4桁・番号は3桁のみ許可
        cleaned = []
        for h in data:
            r = h.get('round', '')
            n = h.get('number', '')
            if (r.isdigit() and len(r) == 4 and
                    n.isdigit() and len(n) == 3):
                cleaned.append(h)
        # 新しい順にソート
        cleaned.sort(key=lambda x: -int(x['round']))
        return cleaned
    except:
        return []


# ===== 移動平均 =====
def moving_avg(data, w):
    return [round(sum(data[i-w+1:i+1])/w, 1) if i >= w-1 else None for i in range(len(data))]


# ===== グラフ用チャートデータ生成 =====
def calc_chart_data(history):
    nums = [h['number'] for h in history]
    r100 = nums[:100] if len(nums) >= 100 else nums
    rounds = [h['round'] for h in history[:len(r100)]]

    sums = [int(n[0])+int(n[1])+int(n[2]) for n in r100]
    maxs = [max(int(d) for d in n) for n in r100]
    mins = [min(int(d) for d in n) for n in r100]
    ma5s  = moving_avg(sums, 5)
    ma5mx = moving_avg(maxs, 5)
    ma5mn = moving_avg(mins, 5)

    sc = Counter(sums)
    mc = Counter(maxs)
    nc = Counter(mins)

    pos_data = {}
    for d in '0123456789':
        h_vals = [sum(1 for n in r100[i:i+10] if n[0]==d) for i in range(0, 100, 10)]
        t_vals = [sum(1 for n in r100[i:i+10] if n[1]==d) for i in range(0, 100, 10)]
        u_vals = [sum(1 for n in r100[i:i+10] if n[2]==d) for i in range(0, 100, 10)]
        pos_data[d] = {
            "h": sum(1 for n in r100 if n[0]==d),
            "t": sum(1 for n in r100 if n[1]==d),
            "u": sum(1 for n in r100 if n[2]==d),
            "trend_h": h_vals,
            "trend_t": t_vals,
            "trend_u": u_vals,
        }

    g1 = nums[:10]
    g2 = nums[10:20] if len(nums) >= 20 else nums
    ikioi_all = {d: sum(d in n for n in g1) - sum(d in n for n in g2) for d in '0123456789'}

    pull_total = {d: 0 for d in '0123456789'}
    for gap in [1, 2, 3]:
        for i in range(gap, len(r100)):
            ps = set(r100[i-gap])
            for d in r100[i]:
                if d in ps:
                    pull_total[d] += 1
    pull_total_all = dict(pull_total)

    return {
        "rounds":   list(reversed(rounds)),
        "sums":     list(reversed(sums)),
        "maxs":     list(reversed(maxs)),
        "mins":     list(reversed(mins)),
        "ma5s":     list(reversed(ma5s)),
        "ma5max":   list(reversed(ma5mx)),
        "ma5min":   list(reversed(ma5mn)),
        "sum_dist": [sc.get(i, 0) for i in range(28)],
        "max_dist": [mc.get(i, 0) for i in range(10)],
        "min_dist": [nc.get(i, 0) for i in range(10)],
        "avg_sum":  round(sum(sums)/len(sums), 1),
        "avg_max":  round(sum(maxs)/len(maxs), 1),
        "avg_min":  round(sum(mins)/len(mins), 1),
        "pos_data": pos_data,
        "ikioi_all": ikioi_all,
        "pull_total_all": pull_total_all,
    }


# ===== 候補数字調査A =====
def analyze_A(history):
    if len(history) < 20:
        return None
    nums = [h['number'] for h in history]

    g1 = nums[:10]
    g2 = nums[10:20]
    ikioi = {d: sum(d in n for n in g1) - sum(d in n for n in g2) for d in '0123456789'}

    r63 = nums[:63] if len(nums) >= 63 else nums
    freq = {d: sum(d in n for n in r63) for d in '0123456789'}

    r100 = nums[:100] if len(nums) >= 100 else nums
    pull_total = {d: 0 for d in '0123456789'}
    for gap in [1, 2, 3]:
        for i in range(gap, len(r100)):
            ps = set(r100[i-gap])
            for d in r100[i]:
                if d in ps:
                    pull_total[d] += 1

    pull_score = {}
    for d in '0123456789':
        s = 0
        if len(nums) >= 1 and d in set(nums[0]): s += 3
        if len(nums) >= 2 and d in set(nums[1]): s += 2
        if len(nums) >= 3 and d in set(nums[2]): s += 1
        pull_score[d] = s

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

    def rank_pt(scores, pts):
        sd = sorted(scores.keys(), key=lambda x: -scores[x])
        return {d: pts[i] if i < len(pts) else 0 for i, d in enumerate(sd)}

    pt_pull  = rank_pt(pull_total, [5, 4, 3, 2, 1, 0, 0, 0, 0, 0])
    pt_freq  = rank_pt(freq,       [4, 3, 2, 1, 1, 0, 0, 0, 0, 0])
    pt_ikioi = {d: (4 if ikioi[d] >= 3 else 3 if ikioi[d] >= 2 else 2 if ikioi[d] >= 1 else 1 if ikioi[d] == 0 else 0) for d in '0123456789'}
    pt_ps    = {d: (3 if pull_score[d] >= 3 else 2 if pull_score[d] == 2 else 1 if pull_score[d] == 1 else 0) for d in '0123456789'}
    rn_sorted = sorted(renban_count.keys(), key=lambda x: -renban_count[x])
    rn_rank = {rn_sorted[i]: [3, 2, 1][i] if i < 3 else 0 for i in range(10)}
    pt_renban = {d: rn_rank[d] + (1 if d in renban_next else 0) for d in '0123456789'}

    total = {d: pt_pull[d]+pt_freq[d]+pt_ikioi[d]+pt_ps[d]+pt_renban[d] for d in '0123456789'}
    ranking = sorted(total.keys(), key=lambda x: -total[x])
    candidates = ranking[:4]

    last5 = nums[:5]
    hit_check = {d: sum(d in n for n in last5) for d in candidates}
    in_latest = [d for d in candidates if d in set(nums[0])]

    # ひっぱり連続状況
    all_pull = []
    for i in range(1, len(nums)):
        all_pull.append(bool(set(nums[i-1]) & set(nums[i])))
    all_pull.reverse()

    current_streak = 0
    for p in reversed(all_pull):
        if p:
            current_streak += 1
        else:
            break

    continued = 0
    total_cases = 0
    if current_streak > 0:
        for i in range(current_streak, len(all_pull)):
            if all(all_pull[i-current_streak:i]):
                total_cases += 1
                if all_pull[i]:
                    continued += 1

    pull_continue_prob = round(continued/total_cases*100, 1) if total_cases > 0 else 0

    return {
        "candidates": candidates,
        "scores": {d: total[d] for d in candidates},
        "all_scores": total,
        "details": {
            "ikioi":      {d: ikioi[d]      for d in candidates},
            "freq":       {d: freq[d]        for d in candidates},
            "pull_total": {d: pull_total[d]  for d in candidates},
            "pull_score": {d: pull_score[d]  for d in candidates},
            "renban":     {d: pt_renban[d]   for d in candidates},
        },
        "last5_hit": hit_check,
        "in_latest": in_latest,
        "latest_number": nums[0],
        "pull_streak": {
            "current": current_streak,
            "continue_prob": pull_continue_prob,
            "total_cases": total_cases,
        }
    }


# ===== 候補数字調査B =====
def analyze_B(history, candidates, chart_data):
    nums = [h['number'] for h in history]
    r100 = nums[:100] if len(nums) >= 100 else nums
    cands_int = [int(d) for d in candidates]
    combos = list(combinations(sorted(cands_int), 3))

    sums_list = [int(n[0])+int(n[1])+int(n[2]) for n in r100]
    sum_count = Counter(sums_list)
    avg_sum = sum(sums_list) / len(sums_list)

    combo_sum_eval = []
    for c in combos:
        s = sum(c)
        cnt = sum_count.get(s, 0)
        zone = '中(10-17)' if 10 <= s <= 17 else '低(0-9)' if s <= 9 else '高(18-27)'
        combo_sum_eval.append({"combo": ''.join(map(str, c)), "sum": s, "count": cnt, "zone": zone})
    combo_sum_eval.sort(key=lambda x: -x['count'])

    maxs = [max(int(d) for d in n) for n in r100]
    mins = [min(int(d) for d in n) for n in r100]
    mc = Counter(maxs)
    nc = Counter(mins)

    combo_maxmin_eval = []
    for c in combos:
        mx = max(c)
        mn = min(c)
        mx_rank = sorted(mc.keys(), key=lambda x: -mc[x]).index(mx)+1 if mx in mc else 99
        mn_rank = sorted(nc.keys(), key=lambda x: -nc[x]).index(mn)+1 if mn in nc else 99
        combo_maxmin_eval.append({"combo": ''.join(map(str, c)), "max": mx, "min": mn, "max_rank": mx_rank, "min_rank": mn_rank})
    combo_maxmin_eval.sort(key=lambda x: x['max_rank']+x['min_rank'])

    pos = chart_data['pos_data']
    straight_orders = []
    for c in combos:
        digits = list(map(str, c))
        h = max(digits, key=lambda d: pos[d]["h"])
        remaining = [d for d in digits if d != h]
        t = max(remaining, key=lambda d: pos[d]["t"])
        u = [d for d in remaining if d != t][0]
        reason = f"{h}→百(100回{pos[h]['h']}回) / {t}→十(100回{pos[t]['t']}回) / {u}→一(100回{pos[u]['u']}回)"
        straight_orders.append({"combo": ''.join(map(str, c)), "straight": h+t+u, "reason": reason})

    return {
        "combo_sum": combo_sum_eval,
        "combo_maxmin": combo_maxmin_eval,
        "straight_orders": straight_orders,
        "avg_sum": round(avg_sum, 1),
        "avg_max": round(sum(maxs)/len(maxs), 1),
        "avg_min": round(sum(mins)/len(mins), 1),
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

    pull_streak = analysis_a.get('pull_streak', {})
    streak_info = f"{pull_streak.get('current', 0)}連続中（継続確率{pull_streak.get('continue_prob', 0)}%）"

    prompt = f"""
あなたはナンバーズ3の候補数字を分析するAIです。
以下のデータをもとに、第{next_round}回の予想に向けた分析思考を
**日本語で・わかりやすく** 説明してください。

【最新当選番号】第{latest_result['round']}回：{latest_result['number']}
【候補数字調査A】
候補数字：{'・'.join(analysis_a['candidates'])}
スコア：{analysis_a['scores']}
勢い：{analysis_a['details']['ikioi']}
ひっぱり系合計：{analysis_a['details']['pull_total']}
直近5回ヒット：{analysis_a['last5_hit']}
最新回に含まれる候補：{analysis_a['in_latest']}
ひっぱり連続状況：{streak_info}

【候補数字調査B】
総和上位：{analysis_b['combo_sum'][:2]}
推奨ストレート：{analysis_b['straight_orders']}

【出現間隔アラート】
{[f"{d}:{v['level']} 休止{v['current_rest']}回/平均{v['avg_interval']}回" for d,v in alert.items() if v['level'] in ['🔴','🟡']]}

以下の構成で説明してください：
1. 前回の当選番号の振り返り（ひっぱりの観点）
2. ひっぱり連続状況と次回への影響
3. 今回の候補数字の根拠（調査Aのポイント上位の理由）
4. 調査Bからの検証（組み合わせの信頼度・推奨ストレート順番の根拠）
5. アラートで注目すべき数字
6. 総合的な一言コメント

各項目は2〜4文程度で、専門用語は使わずわかりやすく。
"""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ===== 当たり外れチェック =====
def check_hit(candidates, result_number):
    for d in result_number:
        if d in candidates:
            return True
    return False


# ===== アーカイブインデックス更新 =====
def update_archive_index(archive_data, history):
    index_path = 'data/archive/index.json'
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            index = json.load(f)
    except:
        index = []

    existing_dates = {item['date'] for item in index}
    if archive_data['date'] not in existing_dates:
        index.insert(0, {
            "date": archive_data['date'],
            "round": archive_data['latest_round'],
            "candidates": archive_data['analysis_a']['candidates'],
            "result_number": None,
            "hit": None
        })

    round_map = {h['round']: h['number'] for h in history}
    for item in index:
        if item['result_number'] is None:
            next_round_num = str(int(item['round']) + 1)
            if next_round_num in round_map:
                item['result_number'] = round_map[next_round_num]
                item['hit'] = check_hit(item['candidates'], round_map[next_round_num])

    os.makedirs('data/archive', exist_ok=True)
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


# ===== メイン処理 =====
def main():
    print(f"処理開始: {today}")

    # 当選番号取得
    latest = get_latest_numbers()
    if not latest:
        print("当選番号の取得に失敗しました")
        return

    # 履歴の更新
    history = load_history()
    existing_rounds = {h['round'] for h in history}

    # 基準回号：history.jsonの最大回号（4桁・確認済みデータのみ）
    valid_history = [h for h in history
                     if h['round'].isdigit() and len(h['round']) == 4
                     and h['number'].isdigit() and len(h['number']) == 3]

    if valid_history:
        base_round = max(int(h['round']) for h in valid_history)
    else:
        base_round = 7017  # 2026年7月時点の既知の最新回号

    print(f"基準回号: 第{base_round}回")

    def is_valid_entry(e):
        r, n = e.get('round', ''), e.get('number', '')
        # 必須：4桁の回号、3桁の番号
        if not (r.isdigit() and len(r) == 4 and n.isdigit() and len(n) == 3):
            return False
        # 必須：基準回号の次の回（+1〜+5）のみ受け付ける
        # これにより9800・90600などの誤検出を完全排除
        return base_round + 1 <= int(r) <= base_round + 5

    new_entries = [e for e in latest if is_valid_entry(e) and e['round'] not in existing_rounds]
    print(f"新着候補: {len(latest)}件 → 有効: {len(new_entries)}件（第{base_round+1}〜{base_round+5}回のみ許可）")

    if new_entries:
        # valid_historyのみを使って更新（不正データを排除）
        updated = new_entries + valid_history
        updated.sort(key=lambda x: -int(x['round']))
        os.makedirs('data', exist_ok=True)
        with open('data/history.json', 'w', encoding='utf-8') as f:
            json.dump(updated, f, ensure_ascii=False, indent=2)
        history = updated
        print(f"新規データ追加: {len(new_entries)}件 / 累計: {len(history)}件")
    else:
        # 不正データを排除したvalid_historyで上書き保存（クリーンアップ）
        valid_history.sort(key=lambda x: -int(x['round']))
        os.makedirs('data', exist_ok=True)
        with open('data/history.json', 'w', encoding='utf-8') as f:
            json.dump(valid_history, f, ensure_ascii=False, indent=2)
        history = valid_history
        print(f"新規データなし / 累計: {len(history)}件（不正データ自動除去済み）")

    if len(history) < 20:
        print(f"データ不足（{len(history)}件 / 最低20回必要）")
        return

    # 分析実行
    result_a   = analyze_A(history)
    chart_data = calc_chart_data(history)
    result_b   = analyze_B(history, result_a['candidates'], chart_data)
    alert      = calc_alert(history)

    latest_result = history[0]
    next_round = str(int(latest_result['round']) + 1)

    # AI思考生成
    print("AI思考を生成中...")
    ai_thoughts = generate_ai_thoughts(result_a, result_b, alert, latest_result, next_round)

    # データ保存
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
        "chart_data": chart_data,
        "generated_at": datetime.now(JST).isoformat()
    }

    with open('data/latest.json', 'w', encoding='utf-8') as f:
        json.dump(archive_data, f, ensure_ascii=False, indent=2)

    with open(f'data/archive/{today}.json', 'w', encoding='utf-8') as f:
        json.dump(archive_data, f, ensure_ascii=False, indent=2)

    update_archive_index(archive_data, history)

    print(f"完了！候補数字: {'・'.join(result_a['candidates'])}")
    pull_streak = result_a.get('pull_streak', {})
    print(f"ひっぱり連続: {pull_streak.get('current', 0)}連続中（継続確率{pull_streak.get('continue_prob', 0)}%）")


if __name__ == "__main__":
    main()
