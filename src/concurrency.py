"""渠道并发限制 + FIFO 排队。

每个渠道一个"在途计数器 (in_flight)"，上限由 Channel.max_concurrent 决定
（0 = 不限）。满了时候调用方可以走 `acquire_from_candidates(...)` 在一组
候选渠道上排队等任一位置空出，超时返回 None。

设计要点：
  - 单进程 async：用 asyncio.Lock + 手写 FIFO waiter 列表
  - 每个渠道一份 `ChannelSlot`，按需懒构造
  - max 值动态从 registry 读（config 热加载可能改）；in_flight 不随 reload 重置
  - acquire/release 必须配对，`try/finally` 保证 release 被调用
  - 非持久化：重启清零（"在途"概念本身重启就没了）
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import channel_state, config


@dataclass
class ChannelSlot:
    """单渠道的并发槽位。"""

    key: str
    max_concurrent: int
    in_flight: int = 0
    # FIFO waiter 队列：每个 waiter 是一个 asyncio.Future。
    # 释放时只唤醒队头；唤醒方设置 future.set_result(None)，等待方被唤醒后
    # 用原子路径 (set_result 前 slot.in_flight += 1) 拿到位置。
    waiters: list[asyncio.Future] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_unlimited(self) -> bool:
        return self.max_concurrent <= 0

    def is_saturated(self) -> bool:
        if self.is_unlimited():
            return False
        return self.in_flight >= self.max_concurrent


# 全局 slot 表。key = channel.key（"api:xxx" 或 "oauth:provider:email"）。
_slots: dict[str, ChannelSlot] = {}
_slots_lock = asyncio.Lock()
_slots_guard = threading.RLock()
# Renamed keys are allowed to drain in place. Moving a live slot would make
# existing requests release the old key while its counter lives under the new
# key, permanently leaking in_flight and waiters.
_retired_keys: set[str] = set()
_retired_limits: dict[str, int] = {}
_deleted_retire_targets: dict[str, str] = {}

# 全局"释放事件"：任一渠道 release 时 set 一次，用来唤醒"跨候选"排队方
# （acquire_from_candidates 会注册到多个 slot 的 waiter 队列里，任一 slot 释放都要醒）。
# 严格 FIFO 通过 waiter 队列保证，_release_event 只是兜底 / 轮询唤醒源。
_release_event: asyncio.Event = asyncio.Event()


def _get_channel_max(ch_key: str) -> int:
    """从 registry 查当前渠道的 max_concurrent。缺失/未知渠道返回 defaultMaxConcurrent。"""
    from .channel import registry  # 延迟 import 避免循环

    cfg = config.get()
    cc_cfg = cfg.get("concurrency") or {}
    default_max = int(cc_cfg.get("defaultMaxConcurrent", 0))
    ch = registry.get_channel(channel_state.resolve(ch_key))
    if ch is None:
        return default_max
    mc = getattr(ch, "max_concurrent", 0)
    try:
        mc = int(mc or 0)
    except Exception:
        mc = 0
    # 0 / 负数 → 用全局默认（仍为 0 则不限）
    return mc if mc > 0 else default_max


def _get_or_create_slot_locked(ch_key: str) -> ChannelSlot:
    slot = _slots.get(ch_key)
    if slot is None:
        max_concurrent = (
            _retired_limits[ch_key]
            if ch_key in _retired_keys and ch_key in _retired_limits
            else _get_channel_max(ch_key)
        )
        slot = ChannelSlot(key=ch_key, max_concurrent=max_concurrent)
        _slots[ch_key] = slot
    elif ch_key not in _retired_keys:
        slot.max_concurrent = _get_channel_max(ch_key)
    return slot


def _enabled() -> bool:
    cfg = config.get()
    cc_cfg = cfg.get("concurrency") or {}
    return bool(cc_cfg.get("enabled", True))


def _is_deleted_generation_locked(ch_key: str) -> bool:
    """Whether a successful delete retired this exact/aliased generation.

    Rename-only generations may continue draining in place.  A delete is
    different: requests that have not acquired capacity yet must not start an
    upstream call with credentials removed from config.
    """
    deleted_target = _deleted_retire_targets.get(ch_key)
    if deleted_target is not None or channel_state.is_deleted(ch_key):
        return True
    resolved = channel_state.resolve(ch_key)
    return (
        channel_state.is_deleted(ch_key)
        or channel_state.is_deleted(resolved)
    )


async def try_acquire(ch_key: str) -> bool:
    """非阻塞尝试占一个位置。成功 in_flight+=1，返回 True；满了返回 False。

    禁用并发限制或渠道不限（max=0）时永远返回 True。
    """
    async with _slots_lock:
        with channel_state.mutation_lock, _slots_guard:
            if _is_deleted_generation_locked(ch_key):
                return False
            if not _enabled():
                return True
            slot = _get_or_create_slot_locked(ch_key)
            if slot.is_unlimited():
                slot.in_flight += 1
                return True
            if slot.in_flight < slot.max_concurrent:
                slot.in_flight += 1
                return True
    return False


def release(ch_key: str) -> None:
    with channel_state.mutation_lock, _slots_guard:
        _release_locked(ch_key)


def _release_locked(ch_key: str) -> None:
    """释放一个位置，唤醒该 slot 的 FIFO 队头（若有）。

    同步函数，可在 try/finally 里直接调用。
    """
    slot = _slots.get(ch_key)
    if slot is None:
        return
    if slot.is_unlimited():
        # 不限制的渠道仅做 in_flight 计数回减，无需唤醒
        if slot.in_flight > 0:
            slot.in_flight -= 1
        _cleanup_retired_slot(ch_key, slot)
        return
    if slot.in_flight > 0:
        slot.in_flight -= 1
    # 唤醒队头 waiter（只唤醒一个；FIFO 语义）
    # 注意：即使没 waiter，也要 set 一次 _release_event，唤醒 acquire_from_candidates
    # 里跨 slot 轮询的等待方。
    woke_waiter = False
    while slot.waiters:
        fut = slot.waiters.pop(0)
        if not fut.done():
            fut.set_result(None)
            woke_waiter = True
            break
        # done 的是被别处取消 / 已超时的，继续找下一个
    try:
        _release_event.set()
    except Exception:
        pass
    if not woke_waiter:
        _cleanup_retired_slot(ch_key, slot)


def _cleanup_retired_slot(ch_key: str, slot: ChannelSlot) -> None:
    with channel_state.mutation_lock, _slots_guard:
        if (
            ch_key in _retired_keys
            and slot.in_flight == 0
            and not slot.waiters
            and _slots.get(ch_key) is slot
        ):
            _slots.pop(ch_key, None)
            if _deleted_retire_targets.pop(ch_key, None) is not None:
                _retired_keys.discard(ch_key)
                _retired_limits.pop(ch_key, None)
                # Keep the deleted target tombstoned until process restart.
                # A request can exist before acquire (or concurrency can be
                # disabled), so an empty slot is not proof that no old
                # generation can publish late side effects.


async def _acquire_or_register_waiter(
    ch_key: str,
) -> tuple[ChannelSlot | None, asyncio.Future | None, bool]:
    """Atomically acquire available capacity or append one waiter."""
    async with _slots_lock:
        with channel_state.mutation_lock, _slots_guard:
            if _is_deleted_generation_locked(ch_key):
                return None, None, False
            slot = _get_or_create_slot_locked(ch_key)
            if slot.is_unlimited() or slot.in_flight < slot.max_concurrent:
                slot.in_flight += 1
                return slot, None, True
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            slot.waiters.append(fut)
            return slot, fut, False


async def acquire_from_candidates(
    candidates: list[tuple[str, object]],  # [(ch_key, resolved_model_or_whatever)]
    timeout_seconds: float,
) -> Optional[tuple[str, object]]:
    """在一组候选渠道上排队等位。

    行为：
      1. 先挨个 try_acquire；命中则返回 (ch_key, payload)。
      2. 全满 → 在**每个**候选 slot 末尾注册一个 waiter future；
         同时 asyncio.wait 各 future + 全局 _release_event + timeout。
      3. 任一 future 就绪（被 release 精确唤醒）→ 对应候选 try_acquire
         抢不到就循环回 step 2（避免被别的 ch_key 抢走）。
      4. 全局 _release_event 唤醒只表示"可能"有位置，轮询所有候选再 try。
      5. 超过 timeout → 返回 None。

    candidates 保持原顺序 → 优先级语义和调度器的排序一致。
    超时返回 None 后上层应给客户端 429。
    """
    if not candidates:
        return None
    # step 1: 快速路径
    for ch_key, payload in candidates:
        if await try_acquire(ch_key):
            return (ch_key, payload)

    deadline = time.monotonic() + max(0.0, timeout_seconds)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None

        # step 2: 为每个 slot 注册 waiter
        futures: list[tuple[ChannelSlot, asyncio.Future]] = []
        for ch_key, payload in candidates:
            slot, fut, acquired = await _acquire_or_register_waiter(ch_key)
            if acquired:
                for previous_slot, previous_fut in futures:
                    _drop_waiter(previous_slot, previous_fut)
                return (ch_key, payload)
            if slot is None:
                # Deleted after scheduler selection but before acquire.
                continue
            assert fut is not None
            futures.append((slot, fut))

        if not futures:
            return None

        # 同时等 _release_event 作为兜底唤醒源（覆盖 slot 被重建等极端情况）
        _release_event.clear()
        global_wake = asyncio.create_task(_release_event.wait())
        wait_futs = [fut for _, fut in futures] + [global_wake]

        cancelled = False
        try:
            done, _pending = await asyncio.wait(
                wait_futs,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # 不管怎么样都要把自己从所有 slot 的 waiter 队列里摘掉，避免泄漏
            for slot, fut in futures:
                _drop_waiter(slot, fut)
            if not global_wake.done():
                global_wake.cancel()
            if cancelled:
                for slot, _ in futures:
                    _cleanup_retired_slot(slot.key, slot)

        if not done:
            # 超时
            for slot, _ in futures:
                _cleanup_retired_slot(slot.key, slot)
            return None

        # step 3: 被唤醒，挨个候选再试一次
        # （优先按原顺序 try_acquire，保证候选优先级语义）
        for ch_key, payload in candidates:
            if await try_acquire(ch_key):
                return (ch_key, payload)
        # 没抢到 → 回 step 2 继续等


def _drop_waiter(slot: ChannelSlot, fut: asyncio.Future) -> None:
    with channel_state.mutation_lock, _slots_guard:
        try:
            slot.waiters.remove(fut)
        except ValueError:
            pass
        if not fut.done():
            try:
                fut.cancel()
            except Exception:
                pass


def _cancel_waiter_threadsafe(fut: asyncio.Future) -> None:
    """Cancel a waiter on its owning loop (delete may run in a TG thread)."""
    if fut.done():
        return
    try:
        loop = fut.get_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(fut.cancel)
        else:
            fut.cancel()
    except Exception:
        try:
            fut.cancel()
        except Exception:
            pass


def is_saturated(ch_key: str) -> bool:
    """同步查询是否饱和（用于 scheduler filter 的快速路径）。"""
    if not _enabled():
        return False
    with channel_state.mutation_lock, _slots_guard:
        slot = _slots.get(ch_key)
        if slot is None:
            return False
        if ch_key not in _retired_keys:
            slot.max_concurrent = _get_channel_max(ch_key)
        return slot.is_saturated()


def snapshot() -> list[dict]:
    """供 TG Bot 展示：[{ch_key, in_flight, max, waiting}]。"""
    out = []
    with channel_state.mutation_lock, _slots_guard:
        for key, slot in _slots.items():
            if key not in _retired_keys:
                slot.max_concurrent = _get_channel_max(key)
            out.append({
                "channel_key": key,
                "in_flight": slot.in_flight,
                "max_concurrent": slot.max_concurrent,
                "waiting": len(slot.waiters),
                "unlimited": slot.is_unlimited(),
            })
    out.sort(key=lambda x: x["channel_key"])
    return out


def totals() -> dict:
    """汇总：{in_flight, waiting, tracked_channels}。"""
    with channel_state.mutation_lock, _slots_guard:
        return {
            "in_flight": sum(s.in_flight for s in _slots.values()),
            "waiting": sum(len(s.waiters) for s in _slots.values()),
            "tracked_channels": len(_slots),
        }


def forget_channel(ch_key: str) -> None:
    """渠道删除 / 改名时清理。必须确保 in_flight=0、waiters 空，否则忽略。"""
    with channel_state.mutation_lock, _slots_guard:
        slot = _slots.get(ch_key)
        if slot is None:
            return
        if slot.in_flight > 0 or slot.waiters:
            return
        _slots.pop(ch_key, None)


def capture_rename_limit(ch_key: str) -> int:
    """Capture the old generation's limit before config publishes its rename."""
    with channel_state.mutation_lock, _slots_guard:
        slot = _slots.get(ch_key)
        return slot.max_concurrent if slot is not None else _get_channel_max(ch_key)


def retire_channel(ch_key: str, *, frozen_max: int | None = None,
                   deleted_target: str | None = None) -> None:
    """Let one removed/renamed generation drain without leaking its slot."""
    with channel_state.mutation_lock, _slots_guard:
        _retired_keys.add(ch_key)
        if deleted_target is not None:
            channel_state.retire_deleted(deleted_target)
            _deleted_retire_targets[ch_key] = deleted_target
        slot = _slots.get(ch_key)
        if frozen_max is None:
            frozen_max = slot.max_concurrent if slot is not None else _get_channel_max(ch_key)
        _retired_limits[ch_key] = int(frozen_max)
        if slot is not None:
            slot.max_concurrent = int(frozen_max)
            if deleted_target is not None:
                # A rename may drain queued waiters; a delete may not. Wake
                # every pre-acquire waiter so it rechecks the generation and
                # fails or selects another candidate without using old creds.
                for fut in slot.waiters:
                    _cancel_waiter_threadsafe(fut)
                slot.waiters.clear()
                try:
                    _release_event.set()
                except Exception:
                    pass
            _cleanup_retired_slot(ch_key, slot)
        elif deleted_target is not None:
            # There is no tracked slot, but a request may still be between
            # registry selection and acquire, or concurrency may be disabled.
            # Drop slot bookkeeping while retaining the deleted generation's
            # process-lifetime tombstone.
            _deleted_retire_targets.pop(ch_key, None)
            _retired_keys.discard(ch_key)
            _retired_limits.pop(ch_key, None)


def rename_channel(old_key: str, new_key: str, *, frozen_max: int | None = None) -> None:
    """Retire the old slot in place; new requests get a separate new slot.

    Existing requests and waiters still release/wake by ``old_key``. The old
    slot is removed by the final release after it drains. Destination state is
    never overwritten.
    """
    if old_key == new_key:
        return
    retire_channel(old_key, frozen_max=frozen_max)
