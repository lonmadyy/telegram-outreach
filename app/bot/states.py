"""FSM States для aiogram. ARCHITECTURE.md §10.3, §10.4."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_proxy = State()


class NewTemplate(StatesGroup):
    waiting_name = State()
    waiting_body = State()
    waiting_confirm = State()


class NewCampaign(StatesGroup):
    waiting_type = State()
    waiting_txt = State()
    waiting_resend_decision = State()
    waiting_template = State()
    waiting_confirm = State()
