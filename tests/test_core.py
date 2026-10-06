"""budget.core 的验收测试。

只断言期望的分配结果与不变量：窗口归属与边界、加权份额、额度上限、
回收后再分配、抢占的优先级边界、跨窗口结转与异常输入。
"""

import unittest

from budget import (
    BudgetWindow,
    RollingBudget,
    largest_remainder,
    window_bounds,
    window_index,
)


class WindowBoundaryTests(unittest.TestCase):
    """窗口切分：序号、区间与边界归属。"""

    def test_window_bounds_round_trip_and_boundary_ownership(self):
        self.assertEqual(window_index(0, 10), 0)
        self.assertEqual(window_index(9, 10), 0)
        self.assertEqual(window_index(10, 10), 1)
        self.assertEqual(window_index(-1, 10), -1)
        self.assertEqual(window_index(105, 10, epoch=5), 10)
        self.assertEqual(window_bounds(0, 10), (0, 10))
        self.assertEqual(window_bounds(2, 10), (20, 30))
        self.assertEqual(window_bounds(0, 10, epoch=5), (5, 15))
        for index in range(4):
            start, end = window_bounds(index, 10)
            self.assertEqual(window_index(start, 10), index)
            self.assertEqual(window_index(end, 10), index + 1)
            owned = [tick for tick in range(start, end + 1)
                     if window_index(tick, 10) == index]
            self.assertEqual(owned, list(range(start, end)))


class WeightedAllocationTests(unittest.TestCase):
    """按权重分配：份额与权重成比例，余数按小数部分发。"""

    def test_largest_remainder_follows_the_fractional_part(self):
        self.assertEqual(largest_remainder(100, [3, 2, 1]), [50, 33, 17])
        self.assertEqual(largest_remainder(11, [1, 1, 3]), [2, 2, 7])
        self.assertEqual(largest_remainder(7, [1, 1, 1]), [3, 2, 2])
        self.assertEqual(sum(largest_remainder(13, [5, 3, 2])), 13)
        self.assertEqual(largest_remainder(0, [1, 2]), [0, 0])
        window = BudgetWindow(pool=100)
        window.declare("alpha", 0, 3)
        window.declare("beta", 0, 2)
        window.declare("gamma", 0, 1)
        window.allocate()
        self.assertEqual(window.limit_of("alpha"), 50)
        self.assertEqual(window.limit_of("beta"), 33)
        self.assertEqual(window.limit_of("gamma"), 17)
        self.assertEqual(window.total_limit(), 100)


class AllocationLimitTests(unittest.TestCase):
    """分配与超支：同一份预算不能分身，支出不能越过窗口预算。"""

    def test_repeated_allocation_never_hands_out_the_same_budget_twice(self):
        window = BudgetWindow(pool=100)
        for name in ("a", "b", "c"):
            window.declare(name, 1, 1)
        window.allocate()
        self.assertEqual(window.total_limit(), 100)
        self.assertEqual(window.idle(), 0)
        self.assertEqual(window.allocate(), {})
        self.assertEqual(window.total_limit(), 100)
        self.assertTrue(window.spend("a", 20))
        self.assertEqual(window.allocate(), {})
        self.assertEqual(window.total_limit(), 100)
        self.assertEqual(window.total_spent(), 20)

    def test_excess_spend_is_refused_unless_the_pool_has_idle_budget(self):
        window = BudgetWindow(pool=100)
        window.declare("a", 1, 1)
        window.allocate()
        self.assertTrue(window.spend("a", 50))
        self.assertFalse(window.spend("a", 60))
        self.assertEqual(window.limit_of("a"), 100)
        self.assertEqual(window.spent_of("a"), 50)
        self.assertLessEqual(window.total_spent(), window.pool)
        self.assertTrue(window.spend("a", 50))
        self.assertFalse(window.spend("a", 1))
        self.assertEqual(window.spent_of("a"), 100)
        other = BudgetWindow(pool=100)
        other.declare("x", 1, 1)
        other.declare("y", 1, 1)
        other.allocate()
        self.assertEqual(other.limit_of("x"), 50)
        self.assertTrue(other.spend("x", 50))
        self.assertEqual(other.release("y", 20), 20)
        self.assertEqual(other.idle(), 20)
        self.assertTrue(other.spend("x", 20))
        self.assertEqual(other.limit_of("x"), 70)
        self.assertEqual(other.total_limit(), 100)
        self.assertEqual(other.total_spent(), 70)


class ReclaimTests(unittest.TestCase):
    """回收：只收回没花掉的部分，收回来的额度可以再分配。"""

    def test_release_takes_back_only_the_unspent_capacity(self):
        window = BudgetWindow(pool=100)
        window.declare("a", 1, 1)
        window.declare("b", 1, 1)
        window.allocate()
        self.assertEqual(window.limit_of("a"), 50)
        self.assertTrue(window.spend("a", 20))
        self.assertEqual(window.release("a"), 30)
        self.assertEqual(window.limit_of("a"), 20)
        self.assertEqual(window.spent_of("a"), 20)
        self.assertEqual(window.remaining_of("a"), 0)
        self.assertLessEqual(window.total_spent(), window.total_limit())
        self.assertEqual(window.release("b"), 50)
        self.assertEqual(window.total_limit(), 20)
        window.allocate()
        self.assertEqual(window.limit_of("a"), 60)
        self.assertEqual(window.total_limit(), 100)
        self.assertEqual(window.total_spent(), 20)

    def test_reclaimed_capacity_is_idle_again(self):
        window = BudgetWindow(pool=50)
        window.declare("a", 1, 1)
        window.allocate()
        self.assertEqual(window.limit_of("a"), 50)
        self.assertTrue(window.spend("a", 20))
        window.release("a")
        self.assertEqual(window.idle(), 30)
        self.assertEqual(window.total_spent(), 20)
        self.assertEqual(window.release("a"), 0)


class PreemptionTests(unittest.TestCase):
    """抢占：只从优先级更低的那一侧搬走还没花掉的额度。"""

    def test_preemption_takes_only_from_strictly_lower_priority(self):
        window = BudgetWindow(pool=90)
        window.declare("high", 0, 1)
        window.declare("peer", 0, 1)
        window.declare("low", 5, 1)
        window.allocate()
        self.assertEqual(window.limit_of("low"), 30)
        self.assertEqual(window.preempt("high", 10), 10)
        self.assertEqual(window.limit_of("high"), 40)
        self.assertEqual(window.limit_of("peer"), 30)
        self.assertEqual(window.limit_of("low"), 20)
        self.assertEqual(window.total_limit(), 90)
        self.assertTrue(window.spend("low", 5))
        self.assertTrue(window.spend("peer", 30))
        self.assertEqual(window.preempt("high", 30), 15)
        self.assertEqual(window.limit_of("high"), 55)
        self.assertEqual(window.limit_of("low"), 5)
        self.assertEqual(window.spent_of("low"), 5)
        self.assertEqual(window.total_limit(), 90)


class RollingTests(unittest.TestCase):
    """滚动：窗口内的刻度不滚动，边界刻度开新窗口，未花预算按上限结转。"""

    def test_ticks_inside_a_window_do_not_roll_it(self):
        kernel = RollingBudget(width=10, pool=60, carry_limit=0)
        first = kernel.window_for(3)
        self.assertEqual(first.index, 0)
        self.assertEqual(kernel.closed_count(), 0)
        self.assertIs(kernel.window_for(9), first)
        self.assertEqual(kernel.closed_count(), 0)
        self.assertEqual(kernel.window_for(10).index, 1)
        self.assertEqual(kernel.closed_count(), 1)
        third = kernel.window_for(35)
        self.assertEqual(third.index, 3)
        self.assertEqual(kernel.closed_count(), 3)
        self.assertIs(kernel.window_for(39), third)
        with self.assertRaises(ValueError):
            kernel.window_for(4)

    def test_boundary_tick_opens_the_next_window_and_carries_unspent(self):
        kernel = RollingBudget(width=10, pool=100, carry_limit=50)
        window = kernel.window_for(0)
        window.declare("a", 1, 1)
        window.allocate()
        self.assertTrue(window.spend("a", 40))
        self.assertEqual(kernel.closed_count(), 0)
        self.assertEqual(kernel.window_for(5).index, 0)
        self.assertEqual(kernel.closed_count(), 0)
        self.assertIs(kernel.window_for(9), window)
        nxt = kernel.window_for(10)
        self.assertEqual(nxt.index, 1)
        self.assertEqual(kernel.closed_count(), 1)
        self.assertEqual(nxt.pool, 150)
        self.assertEqual(nxt.names(), [])
        self.assertEqual(window.limit_of("a"), 100)
        self.assertEqual(window.spent_of("a"), 40)
        self.assertEqual(kernel.history()[0], window)
        with self.assertRaises(ValueError):
            kernel.window_for(5)


class LifecycleAndInputTests(unittest.TestCase):
    """登记、对账与异常输入。"""

    def test_lifecycle_reconciliation_and_invalid_inputs(self):
        window = BudgetWindow(pool=90, index=7)
        window.declare("a", 0, 1)
        window.declare("b", 5, 2)
        window.declare("c", 5, 0)
        self.assertEqual(window.names(), ["a", "b", "c"])
        self.assertEqual(window.allocate(), {"a": 30, "b": 60})
        self.assertEqual(window.limit_of("c"), 0)
        self.assertEqual(window.total_limit(), 90)
        self.assertTrue(window.spend("a", 30))
        self.assertTrue(window.spend("b", 60))
        self.assertFalse(window.spend("a", 1))
        report = window.reconcile()
        self.assertEqual(report["index"], 7)
        self.assertEqual(report["pool"], 90)
        self.assertEqual(report["allocated"], 90)
        self.assertEqual(report["spent"], 90)
        self.assertEqual(report["claims"]["a"], {
            "priority": 0, "weight": 1, "limit": 30, "spent": 30, "remaining": 0})
        self.assertEqual(report["claims"]["c"], {
            "priority": 5, "weight": 0, "limit": 0, "spent": 0, "remaining": 0})
        self.assertEqual(window.priority_of("b"), 5)
        self.assertEqual(window.weight_of("b"), 2)
        with self.assertRaises(ValueError):
            window_index(1, 0)
        with self.assertRaises(ValueError):
            window_bounds(1, -2)
        with self.assertRaises(TypeError):
            window_index(True, 10)
        with self.assertRaises(TypeError):
            window_bounds(1, 10.0)
        with self.assertRaises(ValueError):
            largest_remainder(-1, [1])
        with self.assertRaises(ValueError):
            largest_remainder(5, [-1])
        with self.assertRaises(ValueError):
            largest_remainder(5, [0, 0])
        self.assertEqual(largest_remainder(5, []), [])
        with self.assertRaises(ValueError):
            BudgetWindow(pool=-1)
        with self.assertRaises(TypeError):
            BudgetWindow(pool=1.5)
        with self.assertRaises(ValueError):
            RollingBudget(width=0, pool=10)
        with self.assertRaises(ValueError):
            RollingBudget(width=10, pool=-1)
        with self.assertRaises(ValueError):
            RollingBudget(width=10, pool=10, carry_limit=-1)
        with self.assertRaises(ValueError):
            window.declare("a", 0, 1)
        with self.assertRaises(ValueError):
            window.declare("", 0, 1)
        with self.assertRaises(ValueError):
            window.declare("d", -1, 1)
        with self.assertRaises(ValueError):
            window.declare("d", 0, -1)
        with self.assertRaises(ValueError):
            window.spend("missing", 1)
        with self.assertRaises(ValueError):
            window.spend("a", -1)
        with self.assertRaises(TypeError):
            window.spend("a", True)
        with self.assertRaises(ValueError):
            window.release("a", -1)
        with self.assertRaises(ValueError):
            window.release("a", 1)
        with self.assertRaises(ValueError):
            window.preempt("missing", 1)
        with self.assertRaises(TypeError):
            window.preempt("a", 1.5)
        with self.assertRaises(ValueError):
            window.claim("missing")


if __name__ == "__main__":
    unittest.main()
