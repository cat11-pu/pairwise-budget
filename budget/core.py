"""budget：滑动窗口预算分配内核（纯标准库，行为完全确定）。

把一段连续的整数刻度切成等宽的窗口，在每个窗口里把一份预算按权重分给若干
申领方，并支持超支借用、回收再分配、优先级抢占与跨窗口结转。对外接口：
window_index() / window_bounds() 负责刻度与窗口的换算，largest_remainder()
把整数预算按权重分完，Claim 是申领方的额度状态，BudgetWindow 是单窗口的
分配与对账，RollingBudget 负责滚动与结转。

约定：

* 刻度与预算一律是整数，由调用方注入；内核不读真实时钟、不起线程、不做 I/O，
  同一串调用必然得到同一个结果；
* 窗口归属左闭右开：刻度 t 属于 [epoch + index * width, epoch + (index + 1) * width)，
  恰好落在边界上的刻度归新窗口；
* 任一窗口里分配出去的额度总量不超过窗口预算，支出总量也不超过窗口预算；
* 额度不够时可从池子里还没分配出去的预算借用（等于补分配），借不到就整体失败；
* 回收只收回还没花掉的额度，回收之后可以再分配；抢占只从优先级更低、还没
  花掉的额度里征收，只搬额度不动已支出；关闭窗口时没花掉的预算按 carry_limit
  上限结转到下一个窗口。

优先级用整数表示，数值越小越重要；权重为 0 的申领方不参与按权重分配。
"""


def _require_int(value, label):
    """确认参数是非布尔的整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("%s 必须是整数: %r" % (label, value))
    return value


def window_index(tick, width, epoch=0):
    """刻度所属的窗口序号；恰好落在边界上的刻度归新窗口。"""
    _require_int(tick, "刻度")
    _require_int(width, "窗口宽度")
    _require_int(epoch, "零点")
    if width < 1:
        raise ValueError("窗口宽度必须为正: %r" % (width,))
    return (tick - epoch) // width


def window_bounds(index, width, epoch=0):
    """窗口序号对应的归属区间；与 window_index 互为逆运算。"""
    _require_int(index, "窗口序号")
    _require_int(width, "窗口宽度")
    _require_int(epoch, "零点")
    if width < 1:
        raise ValueError("窗口宽度必须为正: %r" % (width,))
    start = epoch + index * width
    return (start, start + width)


def largest_remainder(total, weights):
    """把整数总量按权重分成整数份额，总和恰好等于总量。

    先按比例取整下发，再把剩下的份额发给小数部分最大的申领方；小数部分
    相同时按登记先后发。权重全为零时无法按权重分配，直接报错。
    """
    _require_int(total, "总量")
    if total < 0:
        raise ValueError("总量不能为负: %r" % (total,))
    normalized = [_require_int(weight, "权重") for weight in weights]
    for weight in normalized:
        if weight < 0:
            raise ValueError("权重不能为负: %r" % (weight,))
    if not normalized:
        return []
    denominator = sum(normalized)
    if denominator <= 0:
        if total == 0:
            return [0] * len(normalized)
        raise ValueError("权重之和必须为正: %r" % (normalized,))
    shares = [total * weight // denominator for weight in normalized]
    leftover = total - sum(shares)
    by_fraction = sorted(
        range(len(normalized)),
        key=lambda i: total * normalized[i] % denominator,
        reverse=True,
    )
    for index in by_fraction[:leftover]:
        shares[index] += 1
    return shares


class Claim:
    """一个申领方在某个窗口里的额度与支出。"""

    __slots__ = ("name", "priority", "weight", "limit", "spent")

    def __init__(self, name, priority, weight):
        self.name = name
        self.priority = priority
        self.weight = weight
        self.limit = 0
        self.spent = 0

    @property
    def remaining(self):
        """额度里还没花掉的部分。"""
        return self.limit - self.spent


class BudgetWindow:
    """一个窗口里的预算分配：按权重分配、支出、回收、抢占与对账。"""

    __slots__ = ("_pool", "_index", "_claims", "_order")

    def __init__(self, pool, index=0):
        _require_int(pool, "窗口预算")
        _require_int(index, "窗口序号")
        if pool < 0:
            raise ValueError("窗口预算不能为负: %r" % (pool,))
        self._pool = pool
        self._index = index
        self._claims = {}
        self._order = []

    # ---- 只读视图 -------------------------------------------------

    @property
    def pool(self):
        return self._pool

    @property
    def index(self):
        return self._index

    def names(self):
        """按登记顺序返回申领方名字。"""
        return list(self._order)

    def claim(self, name):
        """按名字取申领方。"""
        if name not in self._claims:
            raise ValueError("没有这个申领方: %r" % (name,))
        return self._claims[name]

    def priority_of(self, name):
        return self.claim(name).priority

    def weight_of(self, name):
        return self.claim(name).weight

    def limit_of(self, name):
        return self.claim(name).limit

    def spent_of(self, name):
        return self.claim(name).spent

    def remaining_of(self, name):
        return self.claim(name).remaining

    def total_limit(self):
        """已经分配出去的额度总量。"""
        return sum(self._claims[name].limit for name in self._order)

    def total_spent(self):
        """已经花掉的支出总量。"""
        return sum(self._claims[name].spent for name in self._order)

    def idle(self):
        """还没分配出去的预算，可以再分给别人，也可以被超支借用。"""
        return self._pool - self.total_limit()

    # ---- 分配与回收 -----------------------------------------------

    def declare(self, name, priority, weight):
        """登记一个申领方；登记时还不分配额度。"""
        if not isinstance(name, str) or not name:
            raise ValueError("申领方名字必须是非空字符串: %r" % (name,))
        if name in self._claims:
            raise ValueError("申领方已经登记过: %r" % (name,))
        _require_int(priority, "优先级")
        _require_int(weight, "权重")
        if priority < 0:
            raise ValueError("优先级不能为负: %r" % (priority,))
        if weight < 0:
            raise ValueError("权重不能为负: %r" % (weight,))
        claim = Claim(name, priority, weight)
        self._claims[name] = claim
        self._order.append(name)
        return claim

    def allocate(self):
        """把还没分配出去的预算按权重分给所有申领方，返回本次分出的份额。"""
        idle = self.idle()
        if idle <= 0 or not self._order:
            return {}
        claims = [self._claims[name] for name in self._order]
        shares = largest_remainder(idle, [claim.weight for claim in claims])
        given = {}
        for claim, share in zip(claims, shares):
            if share <= 0:
                continue
            claim.limit += share
            given[claim.name] = share
        return given

    def spend(self, name, amount):
        """从某申领方的额度里支出，返回是否扣到。

        额度不够时，如果池子里还有没分配出去的预算，先补分配再支出；
        仍然不够就整体失败，不改动任何额度或支出。
        """
        claim = self.claim(name)
        _require_int(amount, "支出量")
        if amount < 0:
            raise ValueError("支出量不能为负: %r" % (amount,))
        if amount == 0:
            return True
        if amount <= claim.remaining:
            claim.spent += amount
            return True
        shortfall = amount - claim.remaining
        if shortfall <= self.idle():
            claim.limit += shortfall
            claim.spent += amount
            return True
        return False

    def release(self, name, amount=None):
        """把某申领方还没花掉的额度收回池子，返回实际收回的数量。

        amount 缺省表示收回全部未用额度；显式给出时不得超过未用额度。
        """
        claim = self.claim(name)
        if amount is None:
            amount = claim.remaining
        else:
            _require_int(amount, "回收量")
            if amount < 0:
                raise ValueError("回收量不能为负: %r" % (amount,))
            if amount > claim.remaining:
                raise ValueError(
                    "回收量超过未用额度: %r > %r" % (amount, claim.remaining))
        claim.limit -= amount
        return amount

    def preempt(self, name, amount):
        """从优先级更低、还没花掉的额度里征收给 name，返回实际征收的数量。

        只搬额度不动支出：被征收的一方支出不变，只是可用额度减少。
        """
        claim = self.claim(name)
        _require_int(amount, "征收量")
        if amount < 0:
            raise ValueError("征收量不能为负: %r" % (amount,))
        taken = 0
        for other in (self._claims[key] for key in self._order):
            if taken >= amount:
                break
            if other is claim:
                continue
            if other.priority <= claim.priority:
                continue
            want = amount - taken
            if want > other.remaining:
                want = other.remaining
            if want <= 0:
                continue
            other.limit -= want
            taken += want
        claim.limit += taken
        return taken

    def reconcile(self):
        """对账：每个申领方的额度与支出，以及窗口合计。"""
        rows = {}
        for name in self._order:
            claim = self._claims[name]
            rows[name] = {
                "priority": claim.priority, "weight": claim.weight,
                "limit": claim.limit, "spent": claim.spent,
                "remaining": claim.remaining,
            }
        return {
            "index": self._index, "pool": self._pool, "claims": rows,
            "allocated": self.total_limit(), "spent": self.total_spent(),
        }


class RollingBudget:
    """跨窗口滚动：把刻度映射到窗口，关闭旧窗口时按上限结转未花掉的预算。"""

    __slots__ = ("_width", "_pool", "_epoch", "_carry_limit",
                 "_index", "_window", "_closed")

    def __init__(self, width, pool, epoch=0, carry_limit=None):
        _require_int(width, "窗口宽度")
        _require_int(pool, "窗口预算")
        _require_int(epoch, "零点")
        if width < 1:
            raise ValueError("窗口宽度必须为正: %r" % (width,))
        if pool < 0:
            raise ValueError("窗口预算不能为负: %r" % (pool,))
        if carry_limit is None:
            carry_limit = pool // 2
        _require_int(carry_limit, "结转上限")
        if carry_limit < 0:
            raise ValueError("结转上限不能为负: %r" % (carry_limit,))
        self._width = width
        self._pool = pool
        self._epoch = epoch
        self._carry_limit = carry_limit
        self._index = None
        self._window = None
        self._closed = []

    @property
    def pool(self):
        return self._pool

    @property
    def carry_limit(self):
        return self._carry_limit

    def current_index(self):
        """当前窗口序号；还没有被推进过时为 None。"""
        return self._index

    def closed_count(self):
        """已经关闭的窗口个数。"""
        return len(self._closed)

    def history(self):
        """按关闭先后返回已经结束的窗口。"""
        return tuple(self._closed)

    def bounds_of(self, index):
        """窗口序号对应的归属区间。"""
        return window_bounds(index, self._width, self._epoch)

    def window_for(self, tick):
        """返回 tick 所属的窗口，必要时先把内核滚到那个窗口。"""
        target = window_index(tick, self._width, self._epoch)
        if self._index is None:
            self._index = target
            self._window = BudgetWindow(self._pool, target)
            return self._window
        if target < self._index:
            raise ValueError("不能回到已经关闭的窗口: %r < %r" % (target, self._index))
        while self._index < target:
            carry = self._carry_out(self._window)
            self._closed.append(self._window)
            self._index += 1
            self._window = BudgetWindow(self._pool + carry, self._index)
        return self._window

    def _carry_out(self, window):
        """关闭窗口时能带进下一个窗口的额度：没花掉的预算，上限 carry_limit。"""
        unspent = window.pool - window.total_spent()
        if unspent < 0:
            unspent = 0
        return min(unspent, self._carry_limit)
