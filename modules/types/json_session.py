from modules.types.account_settings import AccountSettings


class JsonSession:
    def __init__(self, *, account_settings=None, dict_settings=None):
        if account_settings is not None:
            self.account: AccountSettings = account_settings

        elif dict_settings is not None:
            self.account: AccountSettings = AccountSettings.from_dict(dict_settings)
