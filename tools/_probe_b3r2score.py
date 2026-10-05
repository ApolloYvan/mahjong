import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.strategy import _discard_score
from mj.shanten import route_shanten, shanten, pair_shanten
from mj.tiles import to_counts

hand = ['1t', '3t', '5t', '3b', '5t', '5b', '中']
counts = to_counts(hand)
print("shanten(中留)=", shanten(counts, 2), "pair=", pair_shanten(counts),
      "route=", route_shanten(counts, 2))
for tile in ('中', '3b', '1t', '3t', '5t', '5b'):
    left = list(hand)
    left.remove(tile)
    c = to_counts(left)
    print(f"弃{tile}: shanten={shanten(c, 2)} route={route_shanten(c, 2)} "
          f"score={_discard_score(hand, tile, 2, 0, 0, None, None)}")
