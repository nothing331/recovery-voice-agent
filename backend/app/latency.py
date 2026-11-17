"""Per-call latency accumulator: user->bot response time per turn and per-service TTFB."""

import math

from loguru import logger

from pipecat.observers.service_metrics_observer import ServiceLatencyKind, ServiceMetricsObserver
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver

SERVICES = ("stt", "llm", "tts")


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0 for empty input."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(1, math.ceil(pct / 100 * len(s)))
    return float(s[k - 1])


def classify_service(processor: str) -> str | None:
    name = processor.upper()
    for key in ("STT", "LLM", "TTS"):  # STT first: "STTSERVICE" also contains "TTS"
        if key in name:
            return key.lower()
    return None


def compute_stats(turns_ms: list[float], ttfb_ms: dict[str, list[float]]) -> dict:
    def avg(v):
        return round(sum(v) / len(v)) if v else 0

    return {
        "turns_ms": [round(t) for t in turns_ms],
        "response_ms": {
            "avg": avg(turns_ms),
            "p50": round(percentile(turns_ms, 50)),
            "p95": round(percentile(turns_ms, 95)),
            "max": round(max(turns_ms)) if turns_ms else 0,
            "count": len(turns_ms),
        },
        "services": {
            s: {"avg_ttfb_ms": avg(ttfb_ms.get(s, [])), "count": len(ttfb_ms.get(s, []))} for s in SERVICES
        },
    }


class LatencyCollector:
    def __init__(self, call_id: int | None = None):
        self.call_id = call_id
        self.turns_ms: list[float] = []
        self.ttfb_ms: dict[str, list[float]] = {s: [] for s in SERVICES}
        self.user_bot = UserBotLatencyObserver()
        self.services = ServiceMetricsObserver()

        @self.user_bot.event_handler("on_latency_measured")
        async def _on_turn(observer, latency_secs):
            ms = latency_secs * 1000
            self.turns_ms.append(ms)
            logger.info(f"call {self.call_id}: turn {len(self.turns_ms)} response latency {ms:.0f} ms")

        @self.services.event_handler("on_service_latency")
        async def _on_service(observer, record):
            if record.kind != ServiceLatencyKind.TTFB:
                return
            svc = classify_service(record.processor)
            if svc:
                self.ttfb_ms[svc].append(record.seconds * 1000)

    @property
    def observers(self) -> list:
        return [self.user_bot, self.services]

    def stats(self) -> dict | None:
        if not self.turns_ms and not any(self.ttfb_ms.values()):
            return None
        return compute_stats(self.turns_ms, self.ttfb_ms)

    def summary_line(self) -> str:
        st = self.stats()
        if not st:
            return f"call {self.call_id}: latency: no data"
        r, s = st["response_ms"], st["services"]
        return (
            f"call {self.call_id}: latency turns={r['count']} avg={r['avg']}ms p50={r['p50']}ms "
            f"p95={r['p95']}ms max={r['max']}ms | TTFB stt={s['stt']['avg_ttfb_ms']}ms "
            f"llm={s['llm']['avg_ttfb_ms']}ms tts={s['tts']['avg_ttfb_ms']}ms"
        )
