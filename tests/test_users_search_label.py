"""The users search button is just «جستجو»; its prompt lists what can be searched."""
import unittest

from app.bot.handlers.subscription_lookup import SEARCH_PROMPT
from app.services.reply_label_aliases import register_aliases_on_mapping
from app.services.retired_text_defaults import refresh_retired_defaults
from app.services.settings_button_labels import BTN_ADMIN_PEOPLE_SUB
from app.services.users import DEFAULT_SETTINGS

OLD_LABEL = "🔎 جستجو با آیدی تلگرام"
KEY = "btn_adm_users_search"
ACTION = "adm_users_search"


class UsersSearchLabelTests(unittest.TestCase):
    def test_the_default_button_text_is_only_search(self):
        self.assertEqual(DEFAULT_SETTINGS[KEY], "🔎 جستجو")
        editor_label = next(label for key, label, _ in BTN_ADMIN_PEOPLE_SUB if key == KEY)
        self.assertEqual(editor_label, "جستجو")
        self.assertNotIn("آیدی", DEFAULT_SETTINGS[KEY])

    def test_the_prompt_lists_everything_that_can_be_searched(self):
        for part in ("نام", "یوزرنیم", "آیدی عددی", "لینک اشتراک", "@username", "123456789"):
            self.assertIn(part, SEARCH_PROMPT)

    def test_an_untouched_old_default_follows_the_new_text_but_an_edited_one_stays(self):
        self.assertEqual(refresh_retired_defaults({KEY: OLD_LABEL}, DEFAULT_SETTINGS)[KEY], DEFAULT_SETTINGS[KEY])
        self.assertEqual(refresh_retired_defaults({KEY: "🔍 پیدا کن"}, DEFAULT_SETTINGS)[KEY], "🔍 پیدا کن")

    def test_a_keyboard_sent_before_the_rename_still_opens_the_search(self):
        mapping = {DEFAULT_SETTINGS[KEY]: ACTION}
        register_aliases_on_mapping(mapping, {KEY: DEFAULT_SETTINGS[KEY]})
        self.assertEqual(mapping[OLD_LABEL], ACTION)
        self.assertEqual(mapping[DEFAULT_SETTINGS[KEY]], ACTION)


if __name__ == "__main__":
    unittest.main()
