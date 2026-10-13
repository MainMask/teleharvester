from modules.types.account_settings import AccountSettings


class JsonSession:
    def __init__(self, *, dict_settings):
        self.account: AccountSettings = AccountSettings.from_dict(dict_settings)
