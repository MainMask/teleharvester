import time
from collections import deque

from scraper.datafiles import format_duration

BAR_WIDTH = 15
WINDOW = 20  # the rate (ETA, /мин) is measured over the last this many steps


class Progress:
    """A job's progress for the 📊 button: done/total steps, or a fraction + ETA set
    directly by a job that computes them itself (the scraper, from its thread)."""

    def __init__(self):
        self.started = time.monotonic()  # job start: the "elapsed" shown
        self.total = None
        self.done = 0
        self.fraction = None  # set() by a job that measures progress on its own
        self.eta = None
        self.counting = False  # started without a total: show a counter and a rate only
        self.preparing = False  # the job will start counting once its prep is done
        self.note = None  # an extra line from set(), e.g. the scraper's post count
        self.ok = 0      # items / workers the job did: the summary's "✅ Успешно" (see BaseFunction.progress_ok)
        self.failed = 0  # ...and the ones it didn't: "⚠️ Ошибок"
        self._samples = deque([(self.started, 0)], maxlen=WINDOW + 1)  # (monotonic, done)

    def prepare(self):
        self.preparing = True

    def start(self, total: int | None):
        """total=None: an open-ended job (an unlimited or trigger campaign) — count only."""
        self.total = total
        self.counting = total is None
        self.done = 0
        # the rate excludes any prep before the count; one assignment, not clear()+append():
        # verify calls this from its thread, and render() must never see an empty window
        self._samples = deque([(time.monotonic(), 0)], maxlen=WINDOW + 1)

    def step(self):
        self.done += 1
        self._samples.append((time.monotonic(), self.done))

    def drop(self, n: int):
        """Take out of the total what a worker that stopped early will never do."""
        if self.total is not None and n > 0:
            self.total = max(self.total - n, self.done)

    def update(self, done: int, total: int):
        """Absolute done/total from a job that counts on its own (verify, from its thread)."""
        if total != self.total:
            self.start(total)
        self.done = done
        self._samples.append((time.monotonic(), done))  # deque.append is thread-safe

    def _seconds_per_step(self) -> float | None:
        """The recent rate: up to now, not to the last step, so a stall (a flood wait,
        an account-switch pause) raises the ETA instead of freezing it."""
        t0, d0 = self._samples[0]
        if self.done <= d0:
            return None
        return (time.monotonic() - t0) / (self.done - d0)

    def set(self, fraction: float, eta: float | None, note: str | None = None):
        self.fraction = fraction
        self.eta = eta
        self.note = note

    def render(self, label: str) -> str:
        elapsed = time.monotonic() - self.started
        if self.fraction is not None:
            frac, eta, count = self.fraction, self.eta, self.note
        elif self.counting:
            line = f"Отправлено: {self.done}"
            if seconds := self._seconds_per_step():
                line += f" · ~{60 / seconds:.1f}/мин"
            return f"{label[:100]}\n{line}\n⏱ {format_duration(elapsed)}"
        elif self.total is not None:
            frac = min(self.done / self.total, 1.0) if self.total else 1.0  # 0: nothing to do
            seconds = self._seconds_per_step()
            eta = None if seconds is None else seconds * max(self.total - self.done, 0)
            count = f"{self.done} / {self.total}"
        else:
            state = "Подготовка…" if self.preparing else "Прогресс недоступен для этой задачи"
            return f"{label[:100]}\n{state}\n⏱ {format_duration(elapsed)}"

        filled = round(min(max(frac, 0.0), 1.0) * BAR_WIDTH)
        lines = [label[:100], f"{'█' * filled}{'░' * (BAR_WIDTH - filled)} {frac * 100:.0f}%"]
        if count:
            lines.append(count)
        if frac >= 1:  # all counted, but the job is still running: saving files, final checks
            tail = "Завершение…"
        else:
            tail = f"ETA {format_duration(eta) if eta is not None else 'оценивается'}"
        lines.append(f"⏱ {format_duration(elapsed)} · {tail}")
        return "\n".join(lines)
