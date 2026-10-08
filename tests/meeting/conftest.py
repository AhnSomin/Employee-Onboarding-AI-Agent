"""In-memory stand-ins for gspread, enough for SheetsStore.

Cells are kept as strings, as with RAW value input. Each call is counted so
tests can check that reads and writes stay batched.
"""

from collections import Counter

import pytest
from gspread.exceptions import WorksheetNotFound
from gspread.utils import a1_to_rowcol


class FakeWorksheet:
    def __init__(self, title: str) -> None:
        self.title = title
        self.cells: list[list[str]] = []
        self.calls: Counter[str] = Counter()

    def get_all_values(self):
        self.calls["read"] += 1
        width = max((len(r) for r in self.cells), default=0)
        return [list(r) + [""] * (width - len(r)) for r in self.cells]

    def _write(self, range_name: str, values) -> None:
        row, col = a1_to_rowcol(range_name.split(":")[0])
        for offset, new_row in enumerate(values):
            index = row - 1 + offset
            while len(self.cells) <= index:
                self.cells.append([])
            current = self.cells[index]
            while len(current) < col - 1 + len(new_row):
                current.append("")
            current[col - 1 : col - 1 + len(new_row)] = [str(v) for v in new_row]

    def update(self, values, range_name=None, **_):
        self.calls["write"] += 1
        self._write(range_name or "A1", values)

    def batch_update(self, data, **_):
        self.calls["write"] += 1
        for entry in data:
            self._write(entry["range"], entry["values"])

    def append_rows(self, values, value_input_option=None, **_):
        self.calls["write"] += 1
        self.cells.extend([str(v) for v in row] for row in values)

    def delete_rows(self, start_index, end_index=None):
        self.calls["write"] += 1
        del self.cells[start_index - 1 : end_index or start_index]


class FakeSpreadsheet:
    def __init__(self) -> None:
        self.sheets: dict[str, FakeWorksheet] = {}

    def worksheet(self, title: str) -> FakeWorksheet:
        if title not in self.sheets:
            raise WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title: str, rows: int, cols: int, index=None) -> FakeWorksheet:
        self.sheets[title] = FakeWorksheet(title)
        return self.sheets[title]


@pytest.fixture
def fake_spreadsheet() -> FakeSpreadsheet:
    return FakeSpreadsheet()
