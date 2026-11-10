"""FSM States для aiogram. ARCHITECTURE.md §10.3, §10.4."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    waiting_phone = State()
    waiting_api_id = State()
    waiting_api_hash = State()
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
    waiting_template = State()       # только для message
    waiting_target_chat = State()    # только для invite (§10.4)
    waiting_confirm = State()


class ParseGroups(StatesGroup):
    waiting_groups = State()
    waiting_account = State()
    waiting_options = State()
    waiting_msg_limit = State()


# --- Рассылка по группам (§22.9) ---


class NewGroupCampaign(StatesGroup):
    waiting_name = State()
    waiting_template = State()
    waiting_groups = State()
    waiting_accounts = State()
    waiting_distribution = State()
    waiting_intervals = State()
    waiting_interval_value = State()
    waiting_confirm = State()


class EditGroupCampaign(StatesGroup):
    waiting_intervals = State()
    waiting_interval_value = State()
    waiting_groups = State()
    waiting_template_text = State()


class NewGroupTemplate(StatesGroup):
    waiting_name = State()
    waiting_body = State()
    waiting_confirm = State()
