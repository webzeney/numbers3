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


# ===== 当選番号の取得（本日分のみ） =====
def get_latest_numbers(next_round):
    """
    history.jsonの次の回号（next_round）の当選番号のみを取得する。
    必要なのは毎日1件だけ。
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
    }

    next_round_str = str(next_round)
    print(f"第{next_round_str}回の当選番号を取得します...")

    def search_in_html(html, target_round):
        """HTMLから特定回号の当選番号を探す"""
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text()

        # 「第XXXX回」の直後にある3桁数字を探す
        patterns = [
            rf'第\s*{target_round}\s*回[^\d]{{0,30}}?(\d{{3}})(?!\d)',
            rf'{target_round}[^\d]{{0,20}}?(\d{{3}})(?!\d)',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                number = m.group(1)
                if len(number) == 3:
                    return number

        # テーブルから探す
        for row in soup.find_all('tr'):
            cells = row.find_all(['td', 'th'])
            texts = [re.sub(r'[^\d]', '', c.get_text(strip=True)) for c in cells]
            if target_round in texts:
                idx = texts.index(target_round)
                for t in texts[idx+1:idx+4]:
                    if len(t) == 3:
                        return t
        return None

    # 試すサイトリスト（上から順に試す）
    # money-planは予想サイトのため除外
    scrapers = [
        {
            "name": "numbers-renban",
            "url": "https://numbers-renban.tokyo/numbers3/result_all",
            "method": "renban",
        },
        {
            "name": "ts4-net",
            "url": "https://ts4-net.com/suuji3-hyo.html",
            "method": "ts4",
        },
        {
            "name": "mk-mode",
            "url": "https://www.mk-mode.com/rails/loto/numbers3",
            "method": "generic",
        },
        {
            "name": "楽天宝くじ(当月)",
            "url": f"https://takarakuji.rakuten.co.jp/backnumber/numbers3/{datetime.now(JST).strftime('%Y%m')}/",
            "method": "generic",
        },
    ]

    def parse_renban(html, target_round):
        """numbers-renban専用パーサー
        構造：テーブルに「ナンバーズ3」と当選番号のペアが入っている
        回号は別途テキストから探す
        """
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text()

        # テーブルから「ナンバーズ3」行の当選番号を取得
        number = None
        for row in soup.find_all('tr'):
            cells = row.find_all(['td', 'th'])
            texts = [c.get_text(strip=True) for c in cells]
            # 「ナンバーズ3」と3桁数字のペアを探す
            if len(texts) >= 2 and 'ナンバーズ3' in texts[0]:
                candidate = re.sub(r'[^\d]', '', texts[1])
                if len(candidate) == 3:
                    number = candidate
                    break

        if not number:
            return None

        # 回号を確認：テキストから最新の回号を探す
        round_matches = re.findall(r'第\s*(\d{4,5})\s*回', text)
        if round_matches:
            latest_round = round_matches[0]
            print(f"    取得した回号: 第{latest_round}回 / 番号: {number}")
            if latest_round == target_round:
                return number
            else:
                print(f"    ※回号不一致（取得:{latest_round} / 期待:{target_round}）- 本日未掲載の可能性")
                return None

        # 回号が見つからない場合はhistry.jsonの最新回号と照合
        print(f"    回号不明・番号のみ取得: {number}")
        return None

    def parse_ts4(html, target_round):
        """ts4-net専用パーサー"""
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text()
        print(f"    [{target_round}含む: {target_round in text}] [069含む: {'069' in text}]")
        # テーブルの全行を確認
        for row in soup.find_all('tr'):
            cells = row.find_all(['td','th'])
            texts = [re.sub(r'[^\d]', '', c.get_text(strip=True)) for c in cells]
            if target_round in texts:
                idx = texts.index(target_round)
                for t in texts[idx+1:idx+4]:
                    if len(t) == 3:
                        return t
        # テキストパターン
        patterns = [
            rf'第\s*{target_round}\s*回[^\d]{{0,30}}?(\d{{3}})(?!\d)',
            rf'{target_round}[^\d]{{0,20}}?(\d{{3}})(?!\d)',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(1)
        return None

    for scraper in scrapers:
        try:
            print(f"  {scraper['name']} を試みます...")
            r = requests.get(scraper['url'], headers=headers, timeout=15)
            print(f"  ステータス: {r.status_code}")
            if r.status_code != 200:
                continue

            if scraper['method'] == 'renban':
                number = parse_renban(r.text, next_round_str)
            elif scraper['method'] == 'ts4':
                number = parse_ts4(r.text, next_round_str)
            else:
                number = search_in_html(r.text, next_round_str)

            if number:
                print(f"  ✓ {scraper['name']}から取得: 第{next_round_str}回 {number}")
                return [{"round": next_round_str, "number": number}]
            else:
                print(f"  {scraper['name']}: 第{next_round_str}回のデータなし")

        except Exception as e:
            print(f"  {scraper['name']} エラー: {e}")

    print(f"  第{next_round_str}回のデータは取得できませんでした")
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

    # 全数字のポイント計算（index.htmlのテーブル表示用）
    def rank_pt_all(scores, pts):
        sd = sorted(scores.keys(), key=lambda x: -scores[x])
        return {d: pts[i] if i < len(pts) else 0 for i, d in enumerate(sd)}

    # 頻度はanalyze_Aと同じ直近63回で計算（一致させる）
    r63_cd = nums[:63] if len(nums) >= 63 else nums
    freq_all = {d: sum(d in n for n in r63_cd) for d in '0123456789'}
    freq_pt_all = rank_pt_all(freq_all, [4,3,2,1,1,0,0,0,0,0])

    renban_count_all = {d: 0 for d in '0123456789'}
    for i in range(1, len(r100)):
        prev_adj = set()
        for pd in r100[i-1]:
            prev_adj.add(str((int(pd)+1) % 10))
            prev_adj.add(str((int(pd)-1) % 10))
        for d in r100[i]:
            if d in prev_adj:
                renban_count_all[d] += 1
    renban_next_all = set()
    for pd in nums[0]:
        renban_next_all.add(str((int(pd)+1) % 10))
        renban_next_all.add(str((int(pd)-1) % 10))
    rn_sorted = sorted(renban_count_all.keys(), key=lambda x: -renban_count_all[x])
    rn_rank = {rn_sorted[i]: [3,2,1][i] if i < 3 else 0 for i in range(10)}
    renban_pt_all = {d: rn_rank[d] + (1 if d in renban_next_all else 0) for d in '0123456789'}

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
        "freq_pt_all": freq_pt_all,
        "renban_pt_all": renban_pt_all,
    }


# ===== ひっぱり連続統計テーブルを構築 =====
def build_pull_stats(nums):
    """全データからN連続後の3世代ひっぱり発生確率テーブルを構築する"""
    N = len(nums)
    pull1_hist = [bool(set(nums[i-1]) & set(nums[i])) for i in range(1, N)]

    # 各時点での連続数を計算
    streaks = []
    current = 0
    for p in pull1_hist:
        streaks.append(current)
        if p: current += 1
        else: current = 0

    stats = {}
    for n in range(0, 10):
        indices = [i for i, s in enumerate(streaks)
                   if s == n and i >= 3 and i + 1 < N]
        if len(indices) < 10:
            continue
        total = len(indices)
        next_pull = sum(pull1_hist[i] for i in indices if i < len(pull1_hist))
        p1 = sum(1 for i in indices if bool(set(nums[i])   & set(nums[i+1])))
        p2 = sum(1 for i in indices if bool(set(nums[i-1]) & set(nums[i+1])))
        p3 = sum(1 for i in indices if bool(set(nums[i-2]) & set(nums[i+1])))
        stats[n] = {
            'total': total,
            'pull_prob': round(next_pull / total * 100, 1),
            'p1': round(p1 / total * 100, 1),
            'p2': round(p2 / total * 100, 1),
            'p3': round(p3 / total * 100, 1),
        }
    return stats


# ===== 候補数字調査A =====
def analyze_A(history):
    if len(history) < 20:
        return None
    nums = [h['number'] for h in history]
    N = len(nums)

    # --- 現在の連続数を計算 ---
    current_streak = 0
    for i in range(N - 1):
        if bool(set(nums[i+1]) & set(nums[i])): current_streak += 1
        else: break

    # --- ひっぱり統計テーブルを構築 ---
    pull_stats = build_pull_stats(nums)
    stat = pull_stats.get(current_streak, pull_stats.get(0, {'pull_prob':62.0,'p1':62.0,'p2':58.0,'p3':58.0,'total':0}))
    pull_prob = stat['pull_prob']

    # --- 加点重みを決定 ---
    if pull_prob >= 70:
        pull_weight = 5
        pull_judge = '強い加点'
    elif pull_prob >= 60:
        pull_weight = 3
        pull_judge = '通常加点'
    else:
        pull_weight = 0
        pull_judge = '加点なし'

    # --- 最強世代を特定して各数字にポイント付与 ---
    pt_pull = {d: 0 for d in '0123456789'}
    best_gen_name = '-'
    best_gen_prob = 0.0
    if pull_weight > 0 and len(nums) >= 3:
        gen_map = {
            'p1': (stat['p1'], set(nums[0])),   # 1回前
            'p2': (stat['p2'], set(nums[1])),   # 2回前
            'p3': (stat['p3'], set(nums[2])),   # 3回前
        }
        max_prob = max(v[0] for v in gen_map.values())
        best_gen_name = max(gen_map, key=lambda k: gen_map[k][0])
        best_gen_prob = gen_map[best_gen_name][0]
        for gen, (prob, digits) in gen_map.items():
            for d in digits:
                pt = round(pull_weight * (prob / max_prob))
                pt_pull[d] = max(pt_pull[d], pt)

    # --- 頻度（直近63回）---
    r63 = nums[:63] if len(nums) >= 63 else nums
    freq = {d: sum(d in n for n in r63) for d in '0123456789'}

    # --- 勢い（直近20回）---
    g1 = nums[:10]
    g2 = nums[10:20]
    ikioi = {d: sum(d in n for n in g1) - sum(d in n for n in g2) for d in '0123456789'}

    # --- 連番パターン（直近100回）---
    r100 = nums[:100] if len(nums) >= 100 else nums
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

    pt_freq   = rank_pt(freq,  [4, 3, 2, 1, 1, 0, 0, 0, 0, 0])
    pt_ikioi  = {d: (4 if ikioi[d] >= 3 else 3 if ikioi[d] >= 2 else 2 if ikioi[d] >= 1 else 1 if ikioi[d] == 0 else 0) for d in '0123456789'}
    rn_sorted = sorted(renban_count.keys(), key=lambda x: -renban_count[x])
    rn_rank   = {rn_sorted[i]: [3, 2, 1][i] if i < 3 else 0 for i in range(10)}
    pt_renban = {d: rn_rank[d] + (1 if d in renban_next else 0) for d in '0123456789'}

    total = {d: pt_pull[d] + pt_freq[d] + pt_ikioi[d] + pt_renban[d] for d in '0123456789'}
    ranking = sorted(total.keys(), key=lambda x: -total[x])
    candidates = ranking[:4]

    last5 = nums[:5]
    hit_check = {d: sum(d in n for n in last5) for d in candidates}
    in_latest = [d for d in candidates if d in set(nums[0])]

    return {
        "candidates": candidates,
        "scores": {d: total[d] for d in candidates},
        "all_scores": total,
        "details": {
            "ikioi":   {d: ikioi[d]     for d in candidates},
            "freq":    {d: freq[d]       for d in candidates},
            "pull_pt": {d: pt_pull[d]   for d in candidates},
            "renban":  {d: pt_renban[d] for d in candidates},
        },
        "last5_hit": hit_check,
        "in_latest": in_latest,
        "latest_number": nums[0],
        "pull_streak": {
            "current":      current_streak,
            "pull_prob":    pull_prob,
            "pull_judge":   pull_judge,
            "best_gen":     best_gen_name,
            "best_gen_prob": best_gen_prob,
            "p1": stat['p1'],
            "p2": stat['p2'],
            "p3": stat['p3'],
            "gen_digits": {
                "p1": sorted(set(nums[0])) if len(nums) >= 1 else [],
                "p2": sorted(set(nums[1])) if len(nums) >= 2 else [],
                "p3": sorted(set(nums[2])) if len(nums) >= 3 else [],
            }
        },
        "pull_pt_all": pt_pull,  # 全数字のひっぱりポイント（chart_data経由でindex.htmlに渡す）
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

    ps = analysis_a.get('pull_streak', {})
    streak_info = (
        f"{ps.get('current',0)}連続中 / "
        f"次回ひっぱり確率{ps.get('pull_prob',0)}%（{ps.get('pull_judge','-')}）/ "
        f"最強世代:{ps.get('best_gen','-')}({ps.get('best_gen_prob',0)}%) / "
        f"1回前:{ps.get('p1',0)}% 2回前:{ps.get('p2',0)}% 3回前:{ps.get('p3',0)}%"
    )
    gen_digits = ps.get('gen_digits', {})

    prompt = f"""
あなたはナンバーズ3の候補数字を分析するAIです。
以下のデータをもとに、第{next_round}回の予想に向けた分析思考を
日本語でわかりやすく説明してください。

【最新当選番号】第{latest_result['round']}回：{latest_result['number']}
【候補数字調査A】
候補数字：{'・'.join(analysis_a['candidates'])}
スコア：{analysis_a['scores']}
勢い：{analysis_a['details']['ikioi']}
ひっぱりポイント：{analysis_a['details']['pull_pt']}
直近5回ヒット：{analysis_a['last5_hit']}
最新回に含まれる候補：{analysis_a['in_latest']}

【ひっぱり分析】
{streak_info}
1回前({latest_result['number']})の数字：{gen_digits.get('p1',[])}
2回前の数字：{gen_digits.get('p2',[])}
3回前の数字：{gen_digits.get('p3',[])}

【候補数字調査B】
総和上位：{analysis_b['combo_sum'][:2]}
推奨ストレート：{analysis_b['straight_orders']}

【出現間隔アラート】
{[f"{d}:{v['level']} 休止{v['current_rest']}回/平均{v['avg_interval']}回" for d,v in alert.items() if v['level'] in ['🔴','🟡']]}

以下の構成で説明してください：
1. 前回の当選番号の振り返り（ひっぱりが発生したか・どの数字が含まれるか）
2. ひっぱり分析（現在何連続中か・次回の発生確率・最も引っ張られやすい世代とその数字）
3. 候補数字の根拠（調査Aのポイント上位の理由）
4. 調査Bからの検証（信頼度の高い組み合わせ・推奨ストレートの根拠）
5. アラートで注目すべき数字
6. 総合的な一言コメント

各項目は2〜4文程度で、専門用語は使わずわかりやすく。
Markdown記法（#・**・---）は使わないこと。
"""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ===== 当たり外れチェック・詳細判定 =====
def judge_result(candidates, result_number):
    """
    当選・外れの詳細判定
    - 当選：候補4つから3つの組み合わせが当選番号の3桁と一致
    - 外れ：それ以外（含まれる数字数と詳細を返す）
    """
    if not result_number:
        return None, ""

    from itertools import combinations as _combs
    res_digits = list(result_number)
    res_set = sorted(set(res_digits))

    # セット当選チェック（4候補から3つの組み合わせ）
    for combo in _combs(candidates, 3):
        if sorted(combo) == res_set:
            straight = ''.join(combo) == result_number
            return True, "ストレート当選" if straight else "ボックス当選"

    # 外れの場合：当選番号の中で候補数字に含まれるものだけを抽出
    hit_digits = list(dict.fromkeys([d for d in res_digits if d in candidates]))
    double_hits = [d for d in candidates if res_digits.count(d) == 2]

    if len(hit_digits) == 0:
        return False, "予想数字は1つも含まれていなかった"
    elif double_hits and len(hit_digits) == 1 and hit_digits[0] in double_hits:
        return False, f"当選番号に予想数字「{double_hits[0]}」がダブルで含まれていた【ダブル発生】"
    elif len(hit_digits) == 1:
        return False, f"当選番号に予想数字「{hit_digits[0]}」が含まれていた"
    elif len(hit_digits) == 2:
        return False, f"当選番号に予想数字「{hit_digits[0]}」と「{hit_digits[1]}」が含まれていた"
    else:
        return False, ""


def check_hit(candidates, result_number):
    hit, _ = judge_result(candidates, result_number)
    return hit


# ===== アーカイブインデックス更新 =====
def update_archive_index(archive_data, history):
    index_path = 'data/archive/index.json'
    try:
        with open(index_path, 'r', encoding='utf-8') as f:
            index = json.load(f)
    except:
        index = []

    # 今日のエントリを追加（重複チェック）
    existing_dates = {item['date'] for item in index}
    if archive_data['date'] not in existing_dates:
        index.insert(0, {
            "date":        archive_data['date'],
            "round":       archive_data['latest_round'],   # 当選回号
            "next_round":  archive_data['next_round'],     # 予想した回号
            "candidates":  archive_data['analysis_a']['candidates'],  # 予想数字
            "result_number": None,
            "hit":         None,
            "result_detail": ""
        })

    # 過去エントリの当選番号・判定を更新
    # 判定ロジック：
    # エントリの「next_round」の当選番号が出たら
    # candidates（next_round向けの予想数字）と照合する
    round_map = {h['round']: h['number'] for h in history}
    for item in index:
        # next_roundが未設定の場合は補完
        if 'next_round' not in item:
            item['next_round'] = str(int(item.get('round', '0')) + 1)

        if item.get('result_number') is None:
            # next_round（予想した回）の当選番号が出ているか確認
            next_r = item.get('next_round', '')
            if next_r in round_map:
                result_num = round_map[next_r]
                hit, detail = judge_result(item.get('candidates', []), result_num)
                item['result_number'] = result_num
                item['hit'] = hit
                item['result_detail'] = detail

    os.makedirs('data/archive', exist_ok=True)
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


# ===== メイン処理 =====
def main():
    print(f"処理開始: {today}")

    # まず既存のhistoryを読み込み、次の回号を特定する
    history = load_history()

    # 基準回号の特定
    valid_history = [h for h in history
                     if h['round'].isdigit() and len(h['round']) == 4
                     and h['number'].isdigit() and len(h['number']) == 3]
    valid_history.sort(key=lambda x: -int(x['round']))

    if valid_history:
        base_round = int(valid_history[0]['round'])
    else:
        base_round = 7017

    next_round_to_fetch = base_round + 1
    print(f"既存データ最新: 第{base_round}回 / 取得対象: 第{next_round_to_fetch}回")

    # 本日分（次の1件）のみ取得
    latest = get_latest_numbers(next_round_to_fetch)

    # 履歴の更新
    existing_rounds = {h['round'] for h in valid_history}

    # 取得した本日分データを追加
    new_entries = [e for e in latest if e['round'] not in existing_rounds]

    if new_entries:
        updated = new_entries + valid_history
        updated.sort(key=lambda x: -int(x['round']))
        os.makedirs('data', exist_ok=True)
        with open('data/history.json', 'w', encoding='utf-8') as f:
            json.dump(updated, f, ensure_ascii=False, indent=2)
        history = updated
        print(f"✓ 新規データ追加: 第{new_entries[0]['round']}回 {new_entries[0]['number']} / 累計: {len(history)}件")
    else:
        # valid_historyで上書き（不正データを自動除去）
        os.makedirs('data', exist_ok=True)
        with open('data/history.json', 'w', encoding='utf-8') as f:
            json.dump(valid_history, f, ensure_ascii=False, indent=2)
        history = valid_history
        print(f"新規データなし / 累計: {len(history)}件")

    if len(history) < 20:
        print(f"データ不足（{len(history)}件 / 最低20回必要）")
        return

    # 分析実行
    result_a   = analyze_A(history)
    chart_data = calc_chart_data(history)
    # ひっぱりポイント（全数字）をchart_dataにマージしてindex.htmlで参照できるようにする
    chart_data['pull_pt_all'] = result_a.get('pull_pt_all', {})
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

    ps = result_a.get('pull_streak', {})
    print(f"完了！候補数字: {'・'.join(result_a['candidates'])}")
    print(f"ひっぱり連続: {ps.get('current', 0)}連続中 / 次回ひっぱり確率: {ps.get('pull_prob', 0)}%（{ps.get('pull_judge', '-')}）")


if __name__ == "__main__":
    main()
