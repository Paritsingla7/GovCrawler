import datetime

from shared.scoring import DEFAULT_WEIGHTS

from ..tables.settings import AppSetting

_EMPTY_POLICY = {"extraction": {}, "lead_score": {"weights": DEFAULT_WEIGHTS}, "crawler": {}}


class AppSettingsMixin:
    def get_app_setting(self, key: str) -> dict | None:
        with self._Session() as s:
            row = s.get(AppSetting, key)
            if not row:
                return None
            return {"value": row.value, "updated_at": row.updated_at, "updated_by": row.updated_by}

    def set_app_setting(self, key: str, value: dict, updated_by: int | None,
                        expected_updated_at: datetime.datetime | None = None) -> bool:
        """Upsert. If the row already exists and `expected_updated_at` is given but
        doesn't match the current row's updated_at, the write is rejected (a
        concurrent-edit conflict — plan.md §17) and returns False without writing.
        A missing row always accepts the write (first write)."""
        with self._Session() as s:
            row = s.get(AppSetting, key)
            if row:
                if expected_updated_at is not None and row.updated_at != expected_updated_at:
                    return False
                row.value = value
                row.updated_by = updated_by
            else:
                row = AppSetting(key=key, value=value, updated_by=updated_by)
                s.add(row)
            s.commit()
            return True

    def get_crawl_policy(self) -> dict:
        setting = self.get_app_setting("crawl_policy")
        if not setting:
            return _EMPTY_POLICY
        return setting["value"]
