"""How many booted sandboxes should wait in a warm pool.

A claim takes one booted pod from the pool at once, and the controller then boots a replacement. While that replacement boots, the
pool is one pod short. If more claims arrive in that time than the pool holds, the extra claims wait for a cold start: about 10 seconds
in the tested build when the node had room, and 60 seconds when a node had to be launched (scenario S04).

So the pool needs to hold the claims that arrive during one boot, plus a small buffer:

    desired = ceil(claims per second x seconds to boot a replacement) + buffer, kept between a floor and a ceiling
"""
import math


def desired_pool_size(claims_in_window: int, window_seconds: float, boot_seconds: float, buffer: int, floor: int, ceiling: int) -> int:
    if window_seconds <= 0 or boot_seconds < 0 or floor < 0 or ceiling < floor or buffer < 0 or claims_in_window < 0:
        raise ValueError("inputs out of range")
    rate = claims_in_window / window_seconds
    return max(floor, min(ceiling, math.ceil(rate * boot_seconds) + buffer))
