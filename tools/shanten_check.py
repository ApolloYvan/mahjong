"""财神向听健全性检查：手牌含白板时，向听必须反映百搭价值。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.shanten import shanten, ukeire
from mj.tiles import to_counts

cases = [
    # (13张手牌, 描述, 期望最大向听)
    (["4w", "5w", "白", "1b", "2b", "3b", "7t", "8t", "9t", "1w", "1w", "2t", "3t"], "财神补顺头(白当3w)", 0),
    (["4w", "5w", "白", "1b", "2b", "3b", "7t", "8t", "9t", "1w", "1w", "2t", "3t"], "白当6w也是0向听", 0),
    (["1w", "5w", "白", "1b", "2b", "3b", "7t", "8t", "9t", "2w", "3w", "2t", "3t"], "白补单张成搭", 1),
    (["1w", "9w", "白", "1b", "9b", "白", "1t", "9t", "白", "东", "东", "南", "南"], "白当将/搭子(孤立幺九多)", 3),
    (["1w", "2w", "3w", "4w", "5w", "6w", "7w", "8w", "9w", "1t", "1t", "1t", "白"], "白当将=听牌", 0),
]

for hand, name, expect in cases:
    counts = to_counts(hand)
    got = shanten(counts)
    waits = len(ukeire(counts))
    status = "OK " if got <= expect else "BAD"
    print(f"{status} {name}: shanten={got} (期望<={expect}) ukeire={waits}")
