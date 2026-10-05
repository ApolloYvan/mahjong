"""G1 验收工具：把真实日志的起手牌和事件序列逐步"强制"喂给 mj.sim.engine，
核对状态机是否认为每一步合法、局末结算是否与日志一致。

    python3 tools/sim_replay_check.py --limit 20          # 冒烟（先跑这个）
    python3 tools/sim_replay_check.py --jobs 8             # 全量，交给用户执行

"强制回放"：不用引擎自己的 legal_responses/自对弈逻辑去决定谁该做什么——
直接按日志顺序把每个事件对应的 apply_* 调用发给引擎，引擎只负责在应用前
检查这个动作在当前状态合不合法。**流局和胜者判定都不接受外部直接告知**：
- 流局：引擎自己摸到 wall_remaining<=21 就会结束（见 mj.sim.engine 模块
  docstring 的实证），本工具只在局末核对 engine.is_draw 是否等于日志的
  round_ended.draw，并统计停摸时的 wall_remaining 分布（应该全部是 21）。
- 赢家：日志里没有单独的"胡"事件，本工具在 round_ended 非流局时调用
  ``engine.apply_hu(日志给的座位)``，这一步会真正跑 mj.rules.evaluate
  验证这个座位当前手牌是否构成自摸——不是"引擎无条件相信日志"。

只有一类不算"引擎错误"、只统计不计入不一致的信号：
- ``could_self_draw_hu``：每次摸牌后引擎都会记一次"这一刻能不能自摸"，
  用于统计"能胡但没胡"的次数（不强制胡是规则允许的，不是 bug）。

detail 字符串比较（第二轮返修后改为真正计入不一致，不再只统计）：
mj.rules.evaluate() 固定生成"动作链xN"文本，真实服务端按"链里是纯杠/纯飘/
杠飘混合"分别用"杠开"/"连杠×N"/"财飘"族/"杠飘链×N"命名（见 mj.sim.engine
模块 docstring 的实证）——``_map_chain_label`` 把引擎文本映射成服务端命名
之后再比较集合，映射后仍不一致才计入不一致数（映射前的原始差异只在
"detail 映射后一致 N 次"那行的对比数字里看得到，不再是"看不懂就放过"）。

响应窗口核对分两段，分别对齐服务端"先问全部人碰、再问下家吃"的两个独立
窗口（见 mj.sim.engine 模块 docstring）：3b 碰窗口（一直都有）+ 吃窗口
（本轮新增，之前只核对了碰窗口，吃窗口完全没比对过）。

服务端代打（``timeout kind=discard`` 配对出来的自动弃牌）仍然按正常弃牌事件
回放，只是单独计数，不算"不一致"。
"""
import argparse
import hashlib
import json
import os
import pickle
import sys
from collections import Counter
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from mining_common import discover_files, mark_auto_discards, merge_rounds  # noqa: E402
from mj.sim.engine import IllegalActionError, RoundEngine  # noqa: E402

CACHE_PATH = os.path.join(ROOT, "tools", ".cache", "sim_replay_check.pkl")
_CHAIN_ONLY_PIAO_TEXT = {1: "财飘", 2: "双财飘", 3: "三财飘"}


def _map_chain_label(engine_detail, chain_has_gang, chain_has_piao):
    """把 mj.rules.evaluate() 固定生成的"动作链xN"文本换成服务端真实按"链里
    是纯杠/纯飘/杠飘混合"三选一的命名，供局末 detail 逐局比对（不再只统计
    不算不一致，见 mj.sim.engine 模块 docstring H1/H2 的样本）。N 从
    engine_detail 本身解析——如果链被 H3 的爆头门槛拦掉，evaluate() 根本
    不会生成这条文本，这里也就不需要额外传 chain_count。"""
    raw = next((d for d in engine_detail if d.startswith("动作链x")), None)
    if raw is None:
        return engine_detail
    n = int(raw[len("动作链x"):])
    if chain_has_gang and chain_has_piao:
        label = "杠飘链×%d" % n
    elif chain_has_gang:
        label = "杠开" if n == 1 else "连杠×%d" % n
    else:
        label = _CHAIN_ONLY_PIAO_TEXT.get(n, "财飘×%d" % n)
    return [label if d == raw else d for d in engine_detail]


def _expected_response_seats(events, discard_idx):
    """从紧跟在这次弃牌后面的响应事件里，重建"碰窗口"和"吃窗口"各自实际
    出现过的座位集合，以及这段窗口里有没有人真的声明（一旦有人声明，
    其余待定响应就不会被记录——见 mj.sim.engine 模块 docstring 的实证）。
    只在非抓打圈弃牌时调用（调用方保证），这种情形下碰窗口固定问另外
    3 家——用位置（先消费最多 3 个 pass/timeout 当碰窗口，再消费 1 个
    pass/timeout 当吃窗口）判断窗口边界，不依赖 pass 事件本身没有的
    ``data.window`` 字段。

    之前按"第一个 window='chi' 的 timeout 之前算碰窗口"分界，被本轮新增
    的吃窗口核对（3b 的姊妹检查）证伪：当吃窗口唯一候选人（下家）直接
    ``pass``（不是 timeout）、且碰窗口里也没有任何一次 timeout 带
    ``window='chi'`` 标记时，这次 pass 会被错误分进碰窗口——对碰窗口本身
    的核对没有影响（下家总是也在碰窗口候选里，多算一次同一个座位不改变
    集合），但会让吃窗口的观测座位集合永远是空集，跟引擎一比对就是
    100% 假不一致。改成按固定槽位数（碰 3 个、吃 1 个，声明提前截断）
    重建，同时要跳过穿插在响应事件之间的 ``timeout(kind="discard")``——
    那是"下一位的弃牌被代打"的标记，不是响应；第一版位置法没过滤它，
    把它当成了响应者本人的 pass/timeout，导致碰窗口座位集合混入了弃牌人
    自己、连带吃窗口整体错位一格，被冒烟跑到的
    ``a_06a4ad389b6f_r1_b4_t0`` 第 6 局证伪。不再依赖不可靠的 window
    标记。返回 (peng_seats, peng_claimed, chi_seats, chi_claimed)。"""
    def is_response_slot(ev):
        if ev["type"] == "pass":
            return True
        return ev["type"] == "timeout" and (ev.get("data") or {}).get("kind") == "response"

    def skip_auto_discard_marker(j):
        # 只跳过这一种已知会穿插进来的非响应事件（下一位弃牌被代打的标记，
        # 见 mark_auto_discards），不做更宽的"跳过任何不认识的类型"，避免
        # 真出现窗口提前结束的未知情形时，误吞后面完全不相关的事件。
        while j < len(events) and events[j]["type"] == "timeout" and (events[j].get("data") or {}).get("kind") == "discard":
            j += 1
        return j

    peng_seats, chi_seats = set(), set()
    peng_claimed = chi_claimed = False
    j = skip_auto_discard_marker(discard_idx + 1)
    while len(peng_seats) < 3 and j < len(events) and is_response_slot(events[j]):
        peng_seats.add(events[j].get("seat"))
        j = skip_auto_discard_marker(j + 1)
    if j < len(events) and events[j]["type"] in ("peng", "gang"):
        peng_seats.add(events[j].get("seat"))
        peng_claimed = True
        j = skip_auto_discard_marker(j + 1)
    if not peng_claimed:
        if j < len(events) and is_response_slot(events[j]):
            chi_seats.add(events[j].get("seat"))
        elif j < len(events) and events[j]["type"] == "chi":
            chi_seats.add(events[j].get("seat"))
            chi_claimed = True
    return peng_seats, peng_claimed, chi_seats, chi_claimed


def _replay_round(rnd, meta):
    """返回 (mismatches, stats)。mismatches：每项 (事件序号或None, 事件类型,
    座位, 牌, 原因)。"""
    stats = Counter()
    mismatches = []
    hands = rnd.get("start_hands")
    if not hands or len(hands) != 4 or not all(hands):
        stats["skipped_no_hands"] += 1
        return mismatches, stats
    dealer = meta.get("dealer", rnd.get("dealer"))
    if dealer is None:
        stats["skipped_no_dealer"] += 1
        return mismatches, stats

    # 分数从 [0,0,0,0] 起算——meta["scores"] 是"这一局的结果增量"，不是起始
    # 分（曾经的 bug）。用 from_known_hands 而不是手动摆字段：新增引擎字段
    # 时不用记得同步改这里（也曾经因为漏改一处 AttributeError 过一次）。
    engine = RoundEngine.from_known_hands(hands, dealer, rnd["round_no"], scores=[0, 0, 0, 0])
    # 注意（全语料通查确认，不是个别现象）：每一局庄家起手牌都是 14 张，
    # 日志压根不给这张"发牌自带的摸牌"记一条 tile_drawn——第一条事件直接是
    # 庄家的弃牌。wall_remaining 的实证阈值（21，模块 docstring）本来就是
    # 按"drawn_count 不计这张隐藏摸牌"校准出来的，这里绝对不能给
    # drawn_count +1，不然全局 wall_remaining 少算一张、流局判定提前一轮
    # ——这是本轮一度引入又改回来的一个真实 bug，教训写在这里。
    # 只有极少数局（全语料 n=3）庄家直接靠这张隐藏摸牌自摸、事件列表只有
    # 一条 round_ended、连弃牌都没有——这种局需要单独把 drawn_tile 摆出来，
    # 不然 apply_hu 会报"自摸时机不对：drawn_tile=None"；drawn_count 不动。
    if len(events := rnd["events"]) == 1 and events[0].get("type") == "round_ended":
        engine.drawn_tile = engine.hands[dealer][-1]
        engine.could_self_draw_hu = bool(engine.can_self_draw_hu())

    auto_idx = mark_auto_discards(events)
    win_detail_actual = []
    could_hu_not_taken = 0
    for i, ev in enumerate(events):
        kind = ev.get("type")
        seat = ev.get("seat")
        tile = ev.get("tile")
        data = ev.get("data") or {}
        try:
            if kind == "tile_drawn":
                engine.step_draw(forced_tile=tile)
                if engine.could_self_draw_hu:
                    could_hu_not_taken += 1   # 如果这一摸就是最后一摸（胡了），下面局末核对会再减掉这一次
            elif kind == "tile_discarded":
                if i in auto_idx:
                    stats["auto_discard"] += 1
                if tile != "白" and not data.get("catch_play"):
                    # 先在应用之前重建"日志实际观察到的响应集合"，应用之后
                    # 再和引擎自己算出来的 responding_seats 比对（3b）。
                    peng_obs, peng_claimed, chi_obs, chi_claimed = _expected_response_seats(events, i)
                else:
                    peng_obs = chi_obs = None
                    peng_claimed = chi_claimed = False
                engine.apply_discard(seat, tile)
                logged_catch = data.get("catch_play")
                if logged_catch is not None and bool(logged_catch) != engine.catch_play:
                    mismatches.append((i, kind, seat, tile,
                                      "catch_play 不一致：引擎=%s 日志=%s" % (engine.catch_play, logged_catch)))
                stats["catch_play_checked"] += 1
                if peng_obs is not None:
                    if engine.phase == "response" and engine.response_stage == "peng":
                        engine_peng = set(engine.responding_seats)
                    else:
                        engine_peng = set()
                    stats["peng_window_checked"] += 1
                    if not peng_claimed:
                        if engine_peng != peng_obs:
                            mismatches.append((i, kind, seat, tile,
                                              "碰窗口座位集合不一致：引擎=%s 日志=%s" % (
                                                  sorted(engine_peng), sorted(peng_obs))))
                    else:
                        stats["peng_window_short_circuited"] += 1
                        if not peng_obs <= engine_peng:
                            mismatches.append((i, kind, seat, tile,
                                              "碰窗口声明座位不在引擎响应名单里：引擎=%s 日志声明含=%s" % (
                                                  sorted(engine_peng), sorted(peng_obs))))
                if chi_obs is not None and not peng_claimed:
                    # 吃窗口只在碰没被声明时才可能真的打开（服务端一样：碰
                    # 优先，一旦有人碰/杠，吃窗口根本不会出现）。这里不看
                    # engine.phase/responding_seats 的当下状态（此刻引擎可能
                    # 还卡在碰窗口没走完），直接用只读的 _chi_window_seat
                    # 重新算一遍"如果碰没人要，吃窗口该问谁"——这个结果只
                    # 依赖弃牌时刻已经落地的 catch_play/god_discarder_seat，
                    # 碰窗口后续怎么被 pass 掉不影响它。
                    expected_chi_seat = engine._chi_window_seat(seat)
                    engine_chi = {expected_chi_seat} if expected_chi_seat is not None else set()
                    stats["chi_window_checked"] += 1
                    if not chi_claimed:
                        if engine_chi != chi_obs:
                            mismatches.append((i, kind, seat, tile,
                                              "吃窗口座位集合不一致：引擎=%s 日志=%s" % (
                                                  sorted(engine_chi), sorted(chi_obs))))
                    else:
                        stats["chi_window_short_circuited"] += 1
                        if not chi_obs <= engine_chi:
                            mismatches.append((i, kind, seat, tile,
                                              "吃窗口声明座位不在引擎响应名单里：引擎=%s 日志声明含=%s" % (
                                                  sorted(engine_chi), sorted(chi_obs))))
            elif kind == "chi":
                used = list(data.get("tiles") or [])
                if tile in used:
                    used.remove(tile)
                engine.apply_claim(seat, "chi", used, auto_draw=False)
            elif kind == "peng":
                engine.apply_claim(seat, "peng", None, auto_draw=False)
            elif kind == "gang":
                sub = data.get("kind")
                if sub == "ming":
                    engine.apply_claim(seat, "gang", None, auto_draw=False)
                else:
                    engine.apply_gang(seat, tile, sub, auto_draw=False)
            elif kind == "pass":
                engine.apply_pass(seat)
            elif kind == "timeout":
                stats["timeout_event"] += 1
                # timeout(kind=response) 是"这个响应窗口没人声明"的隐式 pass——
                # 之前整个跳过不处理，导致 responding_seats 永远清不空、
                # phase 卡在 response，后面所有事件都被判"时机不对"（找到的
                # 第一版冒烟就是 12309/12549 条不一致全部出在这里）。
                # timeout(kind=discard) 是代打，已经通过 tile_discarded 事件
                # 本身回放（配对见 mark_auto_discards），这里不重复处理。
                if data.get("kind") == "response":
                    engine.apply_pass(seat)
                continue
            elif kind == "round_ended":
                win_detail_actual = data
                if data.get("draw"):
                    stats["draw_rounds"] += 1
                    stats["draw_wall_remaining_%d" % engine.wall_remaining()] += 1
                    if not engine.is_draw:
                        mismatches.append((None, "round_ended", None, None,
                                          "日志说流局，引擎没有自己判定流局（wall_remaining=%d）" %
                                          engine.wall_remaining()))
                else:
                    if engine.could_self_draw_hu:
                        could_hu_not_taken -= 1   # 最后一摸就是胡这一摸，不算"能胡没胡"
                    engine.apply_hu(meta.get("winner"))
                continue
            else:
                stats["unhandled_event_%s" % kind] += 1
                continue
            stats["applied_" + kind] += 1
        except IllegalActionError as exc:
            mismatches.append((i, kind, seat, tile, str(exc)))
            stats["illegal"] += 1
        except (ValueError, KeyError, IndexError) as exc:
            mismatches.append((i, kind, seat, tile, "内部异常 %r" % exc))
            stats["engine_exception"] += 1
    stats["could_hu_not_taken"] += max(0, could_hu_not_taken)

    if win_detail_actual and not win_detail_actual.get("draw"):
        actual_winner = meta.get("winner")
        actual_fan = win_detail_actual.get("fan")
        actual_scores = win_detail_actual.get("scores")
        if engine.winner != actual_winner:
            mismatches.append((None, "round_ended", None, None,
                              "winner 不一致：引擎=%s 日志=%s" % (engine.winner, actual_winner)))
        if actual_fan is not None and engine.fan != actual_fan:
            mismatches.append((None, "round_ended", None, None,
                              "fan 不一致：引擎=%s 日志=%s" % (engine.fan, actual_fan)))
        actual_detail = win_detail_actual.get("detail")
        if isinstance(actual_detail, list) and engine.detail is not None:
            if set(engine.detail) != set(actual_detail):
                stats["detail_text_differs"] += 1   # 映射前的原始差异，仅供诊断参考
            mapped_detail = _map_chain_label(engine.detail, engine.chain_has_gang, engine.chain_has_piao)
            if set(mapped_detail) != set(actual_detail):
                mismatches.append((None, "round_ended", None, None,
                                  "detail 映射后仍不一致：引擎=%s（映射前=%s）日志=%s" % (
                                      mapped_detail, engine.detail, actual_detail)))
            else:
                stats["detail_mapped_matches"] += 1
        if isinstance(actual_scores, list) and len(actual_scores) == 4:
            if list(engine.scores) != list(actual_scores):
                mismatches.append((None, "round_ended", None, None,
                                  "scores 不一致：引擎=%s 日志=%s" % (engine.scores, actual_scores)))
    return mismatches, stats


def scan(path):
    total_stats = Counter()
    total_mismatches = []
    rounds_checked = 0
    rounds_seen = 0   # 含跳过局（含 truncated）——skip 占比的正确分母，见 main() 的说明
    try:
        with open(path, encoding="utf-8") as source:
            game = json.load(source)
    except (OSError, ValueError):
        return path, total_stats, total_mismatches, rounds_checked, rounds_seen
    info = {r.get("round_no"): r for r in game.get("rounds") or []}
    for rnd in merge_rounds(game):
        rounds_seen += 1
        meta = info.get(rnd["round_no"])
        if not meta or rnd.get("truncated"):
            total_stats["skipped_truncated"] += 1
            continue
        mismatches, stats = _replay_round(rnd, meta)
        total_stats.update(stats)
        rounds_checked += 1
        for m in mismatches:
            total_mismatches.append((path, rnd["round_no"]) + m)
    return path, total_stats, total_mismatches, rounds_checked, rounds_seen


def _scan_path(path):
    return scan(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个文件（冒烟）")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    files = discover_files(limit=args.limit)

    with open(os.path.abspath(__file__), "rb") as own:
        code_ver = hashlib.md5(own.read()).hexdigest()
    with open(os.path.join(ROOT, "mj", "sim", "engine.py"), "rb") as eng:
        code_ver += hashlib.md5(eng.read()).hexdigest()
    cache = {}
    if not args.no_cache:
        try:
            with open(CACHE_PATH, "rb") as source:
                saved = pickle.load(source)
            if saved.get("ver") == code_ver:
                cache = saved.get("files") or {}
        except (OSError, ValueError, EOFError, pickle.UnpicklingError, AttributeError):
            cache = {}

    def file_key(path):
        st = os.stat(path)
        return (st.st_mtime, st.st_size)

    todo, keys = [], {}
    total_stats = Counter()
    all_mismatches = []
    rounds_total = 0
    rounds_seen_total = 0
    files_from_cache = 0
    for path in files:
        k = file_key(path)
        keys[path] = k
        hit = cache.get(path)
        if hit and hit[0] == k:
            files_from_cache += 1
            total_stats.update(hit[1])
            all_mismatches.extend(hit[2])
            rounds_total += hit[3]
            rounds_seen_total += hit[4]
        else:
            todo.append(path)
    print("缓存命中 %d 个文件，需要重新回放 %d 个" % (files_from_cache, len(todo)))

    fresh = {}
    if todo:
        with Pool(args.jobs) as pool:
            for done, (path, stats, mismatches, rounds_checked, rounds_seen) in enumerate(
                    pool.imap_unordered(_scan_path, todo, chunksize=4), 1):
                fresh[path] = (keys[path], dict(stats), mismatches, rounds_checked, rounds_seen)
                total_stats.update(stats)
                all_mismatches.extend(mismatches)
                rounds_total += rounds_checked
                rounds_seen_total += rounds_seen
                if done % 200 == 0 or done == len(todo):
                    print("  已回放 %d / %d 个文件，累计局数 %d，累计不一致 %d" % (
                        done, len(todo), rounds_total, len(all_mismatches)), flush=True)
    if not args.no_cache:
        merged = {p: cache[p] for p in files if p in cache and p not in fresh}
        merged.update(fresh)
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "wb") as out:
            pickle.dump({"ver": code_ver, "files": merged}, out, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, CACHE_PATH)

    print("\n=== 回放结果 ===")
    print("文件数 %d，局数 %d" % (len(files), rounds_total))
    print("不一致数（局末四项 + catch_play + 碰窗口 + 动作非法）：%d" % len(all_mismatches))
    print("其中服务端代打弃牌 %d 次（照常回放，单独计数，不算不一致）" % total_stats["auto_discard"])
    print("能胡但没胡（不强制胡，正常现象，不算不一致）：%d 次" % total_stats["could_hu_not_taken"])
    print("detail 映射后一致 %d 次（映射规则见 _map_chain_label；映射前原始文本不同 %d 次，"
          "映射后仍不一致的已计入上面的不一致数，不再只统计不算数）" % (
        total_stats["detail_mapped_matches"], total_stats["detail_text_differs"]))
    skipped_no_hands = total_stats["skipped_no_hands"]
    skipped_no_dealer = total_stats["skipped_no_dealer"]
    skipped_truncated = total_stats["skipped_truncated"]
    skipped_total = skipped_no_hands + skipped_no_dealer + skipped_truncated
    print("跳过局：%d（无起手牌 %d / 无 dealer %d / 截断 %d）" % (
        skipped_total, skipped_no_hands, skipped_no_dealer, skipped_truncated))
    # 分母必须是"这个文件里出现过的全部局"（含截断的，rounds_seen_total），
    # 不能只用 rounds_total（那是真正跑完 _replay_round 的局数，截断局从来
    # 不会走到那一步、也就永远不会被算进分母）——之前这里的分母用
    # rounds_total，导致截断类的跳过（真实语料里几乎全部的跳过原因）从分子
    # 分母里同时消失，算出来的占比恒等于 0（真实占比是 290/26086≈1.1%）。
    if rounds_seen_total:
        print("跳过占比：%.3f%%（验收要求 <=0.1%%，且需确认属日志缺陷；分母=全部遇到的局数 %d，含截断）" % (
            100.0 * skipped_total / rounds_seen_total, rounds_seen_total))

    print("\n=== 流局停摸时的 wall_remaining 分布（应全部是 21） ===")
    draw_dist = {k: v for k, v in total_stats.items() if k.startswith("draw_wall_remaining_")}
    print("流局局数 n=%d，分布=%s" % (total_stats["draw_rounds"], draw_dist))

    print("\n=== 响应窗口核对 ===")
    print("碰窗口核对次数 %d，其中因为有人直接声明而短路 %d 次（只做子集校验）" % (
        total_stats["peng_window_checked"], total_stats["peng_window_short_circuited"]))
    print("吃窗口核对次数 %d，其中因为有人直接声明而短路 %d 次（只做子集校验）" % (
        total_stats["chi_window_checked"], total_stats["chi_window_short_circuited"]))
    print("catch_play 逐弃牌核对次数 %d" % total_stats["catch_play_checked"])

    print("\n=== 事件应用计数（诊断用） ===")
    for key in sorted(k for k in total_stats if k.startswith("applied_") or k.startswith("unhandled_event_")):
        print("  %-28s %d" % (key, total_stats[key]))
    print("  %-28s %d" % ("illegal（动作非法）", total_stats["illegal"]))
    print("  %-28s %d" % ("engine_exception（内部异常）", total_stats["engine_exception"]))

    if all_mismatches:
        print("\n=== 不一致明细（最多列 30 条，其余看 --limit 缩小范围复查） ===")
        for row in all_mismatches[:30]:
            print(" ", row)
    else:
        print("\n不一致数 = 0。")


if __name__ == "__main__":
    main()
