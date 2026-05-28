"""FSM States для aiogram. ARCHITECTURE.md §10.3, §10.4."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_proxy = State()
