import unittest

from pool_sizing import desired_pool_size


class PoolSizingTests(unittest.TestCase):
    def test_an_idle_platform_keeps_the_floor(self):
        self.assertEqual(desired_pool_size(0, 300, 60, buffer=1, floor=2, ceiling=20), 2)

    def test_a_burst_grows_the_pool_to_cover_one_boot(self):
        # 150 claims in 5 minutes is 0.5 a second. A 60 second boot means 30 claims can arrive while one replacement boots.
        self.assertEqual(desired_pool_size(150, 300, 60, buffer=1, floor=2, ceiling=50), 31)

    def test_the_ceiling_caps_the_cost(self):
        self.assertEqual(desired_pool_size(3000, 300, 60, buffer=1, floor=2, ceiling=20), 20)

    def test_a_faster_boot_needs_a_smaller_pool(self):
        slow = desired_pool_size(150, 300, 60, buffer=1, floor=2, ceiling=50)
        fast = desired_pool_size(150, 300, 10, buffer=1, floor=2, ceiling=50)
        self.assertLess(fast, slow)

    def test_the_scenario_s04_burst(self):
        # Three sessions started at the same instant against a pool of two: with a 10 second boot and the 3 claims seen in the last
        # minute the rule asks for 1 (rate 0.05/s x 10 s rounds up to 1) plus the buffer, so the floor of 2 is what protects the burst.
        self.assertEqual(desired_pool_size(3, 60, 10, buffer=1, floor=2, ceiling=20), 2)

    def test_bad_inputs_are_refused(self):
        with self.assertRaises(ValueError):
            desired_pool_size(1, 0, 10, 1, 2, 20)
        with self.assertRaises(ValueError):
            desired_pool_size(1, 60, 10, 1, 5, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
