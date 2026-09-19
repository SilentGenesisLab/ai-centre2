#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import os
import re
import statistics
import subprocess
import time
import unicodedata
import uuid
import wave
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import oss2


LOCALES: list[dict[str, str]] = [
    {
        "language": "zh", "locale": "zh-CN", "country": "中国", "name": "普通话",
        "reference": "清晨的阳光穿过窗帘，房间里显得格外安静。今天我们一起测试自然、清晰而稳定的语音表达。",
        "short": "欢迎回来。",
        "medium": "今天的天气很温暖，我们准备了一段自然流畅的语音，希望每一个字都清楚准确。",
        "long": "在快速变化的数字时代，可靠的语音技术不仅要准确读出文字，还要保持自然的节奏、清晰的停顿和稳定的音色。无论内容是一句简短问候，还是一段较长的产品介绍，听众都希望声音连贯、舒适，并且没有多余的杂音或意外内容。今天我们用统一的方法检查不同语言、不同文本长度和不同并发条件下的表现，从而为后续的生产应用提供客观依据。",
    },
    {
        "language": "en", "locale": "en-US", "country": "美国", "name": "英语",
        "reference": "Morning sunlight passes through the curtains, and the room feels calm and quiet. Today we are testing a clear, natural, and reliable speaking voice.",
        "short": "Welcome back.",
        "medium": "The weather is warm today, and this sentence should sound natural, clear, and easy to understand.",
        "long": "Reliable speech technology must do more than pronounce individual words correctly. It should preserve a natural rhythm, place pauses where listeners expect them, and maintain a stable voice from the beginning to the end. This evaluation compares short greetings, medium explanations, and longer passages under several concurrency levels so that production decisions can be based on measured evidence rather than impressions.",
    },
    {
        "language": "es", "locale": "es-ES", "country": "西班牙", "name": "西班牙语",
        "reference": "La luz de la mañana entra por la ventana y la habitación está tranquila. Hoy probamos una voz clara, natural y estable para diferentes situaciones.",
        "short": "Bienvenidos de nuevo.",
        "medium": "Hoy hace buen tiempo y queremos que esta frase suene clara, natural y fácil de comprender.",
        "long": "Una tecnología de voz fiable no solo debe pronunciar correctamente cada palabra. También necesita mantener un ritmo natural, hacer pausas adecuadas y conservar la misma identidad vocal desde el principio hasta el final. Esta evaluación compara mensajes cortos, explicaciones medianas y textos más largos con varios niveles de concurrencia para obtener resultados objetivos antes de utilizar el sistema en producción.",
    },
    {
        "language": "fr", "locale": "fr-FR", "country": "法国", "name": "法语",
        "reference": "La lumière du matin traverse les rideaux et la pièce reste très calme. Aujourd'hui, nous testons une voix claire, naturelle et stable.",
        "short": "Bienvenue à nouveau.",
        "medium": "Il fait doux aujourd'hui et cette phrase doit rester claire, naturelle et facile à comprendre.",
        "long": "Une technologie vocale fiable ne doit pas seulement prononcer chaque mot correctement. Elle doit également conserver un rythme naturel, placer les pauses au bon endroit et maintenir une voix stable du début à la fin. Cette évaluation compare des messages courts, des explications moyennes et des textes plus longs avec plusieurs niveaux de concurrence afin de prendre des décisions de production fondées sur des mesures objectives.",
    },
    {
        "language": "de", "locale": "de-DE", "country": "德国", "name": "德语",
        "reference": "Das Morgenlicht fällt durch die Vorhänge und der Raum ist angenehm ruhig. Heute testen wir eine klare, natürliche und stabile Stimme.",
        "short": "Willkommen zurück.",
        "medium": "Heute ist das Wetter angenehm, und dieser Satz soll klar, natürlich und gut verständlich klingen.",
        "long": "Zuverlässige Sprachtechnologie muss mehr leisten, als einzelne Wörter korrekt auszusprechen. Sie sollte einen natürlichen Rhythmus bewahren, Pausen an den richtigen Stellen setzen und die Stimme vom Anfang bis zum Ende stabil halten. Dieser Test vergleicht kurze Begrüßungen, mittlere Erklärungen und längere Texte bei verschiedenen Parallelitätsstufen, damit Entscheidungen für den Produktionseinsatz auf objektiven Messwerten beruhen.",
    },
    {
        "language": "it", "locale": "it-IT", "country": "意大利", "name": "意大利语",
        "reference": "La luce del mattino attraversa le tende e la stanza è piacevolmente tranquilla. Oggi proviamo una voce chiara, naturale e stabile.",
        "short": "Bentornati.",
        "medium": "Oggi il tempo è piacevole e questa frase deve risultare chiara, naturale e facile da capire.",
        "long": "Una tecnologia vocale affidabile deve fare molto più che pronunciare correttamente le singole parole. Deve mantenere un ritmo naturale, inserire le pause nei punti giusti e conservare una voce stabile dall'inizio alla fine. Questa valutazione confronta saluti brevi, spiegazioni di media lunghezza e testi più lunghi con diversi livelli di concorrenza, così le decisioni di produzione possono basarsi su misure oggettive.",
    },
    {
        "language": "pt", "locale": "pt-BR", "country": "巴西", "name": "葡萄牙语",
        "reference": "A luz da manhã atravessa as cortinas e o ambiente está calmo e silencioso. Hoje testamos uma voz clara, natural e estável.",
        "short": "Bem-vindos de volta.",
        "medium": "O dia está agradável e queremos que esta frase soe clara, natural e fácil de entender.",
        "long": "Uma tecnologia de voz confiável precisa fazer mais do que pronunciar corretamente cada palavra. Ela deve manter um ritmo natural, colocar pausas nos lugares adequados e preservar a mesma identidade vocal do começo ao fim. Esta avaliação compara mensagens curtas, explicações médias e textos mais longos em diferentes níveis de concorrência para apoiar decisões de produção com dados objetivos.",
    },
    {
        "language": "ja", "locale": "ja-JP", "country": "日本", "name": "日语",
        "reference": "朝の光がカーテンを通り、部屋は静かで落ち着いています。今日は、明瞭で自然かつ安定した音声をテストします。",
        "short": "お帰りなさい。",
        "medium": "今日は暖かく、この文章が自然で明瞭に、そして分かりやすく聞こえることを確認します。",
        "long": "信頼できる音声技術には、文字を正しく読むだけでなく、自然なリズムと適切な間を保つことが求められます。短い挨拶でも長い説明でも、最初から最後まで声質が安定し、余計な音や意図しない発話が入らないことが重要です。この評価では、複数の文章の長さと同時実行条件を比較し、本番運用のための客観的な判断材料を集めます。",
    },
    {
        "language": "ko", "locale": "ko-KR", "country": "韩国", "name": "韩语",
        "reference": "아침 햇살이 커튼을 지나고 방 안은 조용하고 편안합니다. 오늘은 명확하고 자연스러우며 안정적인 음성을 시험합니다.",
        "short": "다시 오신 것을 환영합니다.",
        "medium": "오늘은 날씨가 따뜻하며 이 문장이 자연스럽고 명확하게 들리는지 확인합니다.",
        "long": "신뢰할 수 있는 음성 기술은 글자를 정확하게 읽는 것 이상의 품질을 제공해야 합니다. 자연스러운 리듬과 적절한 멈춤을 유지하고 처음부터 끝까지 같은 목소리를 안정적으로 보존해야 합니다. 이번 평가는 짧은 인사, 중간 길이의 설명, 긴 문장을 여러 동시 요청 조건에서 비교하여 실제 운영에 필요한 객관적인 자료를 수집합니다.",
    },
    {
        "language": "ru", "locale": "ru-RU", "country": "俄罗斯", "name": "俄语",
        "reference": "Утренний свет проходит сквозь шторы, и в комнате тихо и спокойно. Сегодня мы проверяем ясный, естественный и стабильный голос.",
        "short": "С возвращением.",
        "medium": "Сегодня тепло, и эта фраза должна звучать ясно, естественно и легко для понимания.",
        "long": "Надёжная речевая технология должна не только правильно произносить отдельные слова. Она должна сохранять естественный ритм, делать паузы в подходящих местах и поддерживать стабильный голос от начала до конца. В этой проверке сравниваются короткие приветствия, объяснения средней длины и более длинные тексты при нескольких уровнях одновременной нагрузки, чтобы решения для эксплуатации основывались на объективных данных.",
    },
    {
        "language": "ar", "locale": "ar-SA", "country": "沙特阿拉伯", "name": "阿拉伯语",
        "reference": "يمر ضوء الصباح عبر الستائر وتبدو الغرفة هادئة ومريحة. اليوم نختبر صوتا واضحا وطبيعيا ومستقرا لمواقف مختلفة.",
        "short": "مرحبا بعودتكم.",
        "medium": "الطقس دافئ اليوم ونريد أن تبدو هذه الجملة واضحة وطبيعية وسهلة الفهم.",
        "long": "يجب أن تقدم تقنية الصوت الموثوقة أكثر من مجرد نطق الكلمات بشكل صحيح. من المهم أن تحافظ على إيقاع طبيعي وأن تضع فترات التوقف في أماكن مناسبة وأن يبقى الصوت مستقرا من البداية إلى النهاية. تقارن هذه الدراسة بين التحيات القصيرة والشروحات المتوسطة والنصوص الطويلة تحت مستويات مختلفة من الطلبات المتزامنة حتى تعتمد قرارات التشغيل على نتائج موضوعية.",
    },
]

LOCALE_BY_LANGUAGE = {item["language"]: item for item in LOCALES}
LENGTHS = ("short", "medium", "long")
CROSS_PAIRS = [
    ("en", "zh"), ("es", "zh"), ("fr", "zh"), ("de", "zh"),
    ("it", "zh"), ("pt", "zh"), ("ja", "zh"), ("ko", "zh"),
    ("ru", "zh"), ("ar", "zh"), ("zh", "en"), ("zh", "es"),
    ("zh", "fr"), ("zh", "de"), ("zh", "ja"), ("zh", "ko"),
    ("zh", "ru"), ("zh", "ar"), ("pt", "en"), ("it", "en"),
]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(character for character in normalized if character.isalnum())


def words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def edit_distance(left: list[str] | str, right: list[str] | str) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_value in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_value in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_value != right_value),
            ))
        previous = current
    return previous[-1]


def text_errors(
    expected: str,
    actual: str,
    language: str | None = None,
) -> tuple[float, float | None, bool, bool]:
    expected_chars = normalize_text(expected)
    actual_chars = normalize_text(actual)
    cer = edit_distance(expected_chars, actual_chars) / max(1, len(expected_chars))
    expected_words = words(expected)
    actual_words = words(actual)
    # Chinese, Japanese, and Korean do not have whitespace-delimited words in
    # a form that makes this simple WER calculation meaningful. CER remains
    # the primary cross-language metric for those scripts.
    wer = (
        edit_distance(expected_words, actual_words) / max(1, len(expected_words))
        if language not in {"zh", "ja", "ko"} and len(expected_words) >= 2
        else None
    )
    prefix = bool(actual_chars and expected_chars and not actual_chars.startswith(expected_chars[:1]))
    suffix = bool(actual_chars.startswith(expected_chars) and len(actual_chars) > len(expected_chars))
    return cer, wer, prefix, suffix


def wav_info(content: bytes) -> dict[str, Any]:
    try:
        with wave.open(io.BytesIO(content), "rb") as source:
            frames = source.getnframes()
            rate = source.getframerate()
            channels = source.getnchannels()
            width = source.getsampwidth()
        return {
            "duration_seconds": frames / max(1, rate),
            "sample_rate": rate,
            "channels": channels,
            "sample_width": width,
            "riff_finalized": bool(
                content[:4] == b"RIFF"
                and int.from_bytes(content[4:8], "little") == len(content) - 8
                and int.from_bytes(content[40:44], "little") == len(content) - 44
            ),
        }
    except (EOFError, wave.Error, ZeroDivisionError):
        return {
            "duration_seconds": 0.0, "sample_rate": 0, "channels": 0,
            "sample_width": 0, "riff_finalized": False,
        }


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class TestCase:
    case_id: str
    phase: str
    mode: str
    source_language: str | None
    target_language: str
    length: str
    text: str
    reference_url: str | None = None
    prompt_text: str | None = None
    concurrency: int = 1
    repeats: int = 1


class ResourceSampler:
    UNITS = {
        "voxcpm2": "ai-centre-voxcpm2-gpu1.service",
        "control": "ai-centre-control.service",
        "speaker": "ai-centre-speaker-verifier.service",
        "asr": "ai-centre-asr-gpu0.service",
    }

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self._stop = asyncio.Event()
        self._previous_cpu: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _command(command: list[str]) -> str:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        return completed.stdout.strip()

    def _sample_sync(self) -> dict[str, Any]:
        now = time.monotonic()
        sample: dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat()}
        gpu_output = self._command([
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
            "--format=csv,noheader,nounits",
        ])
        for row in gpu_output.splitlines():
            columns = [column.strip() for column in row.split(",")]
            if len(columns) != 7:
                continue
            index = columns[0]
            sample[f"gpu{index}_name"] = columns[1]
            for suffix, value in zip(
                ("util_percent", "memory_used_mib", "memory_total_mib", "power_w", "temperature_c"),
                columns[2:],
            ):
                try:
                    sample[f"gpu{index}_{suffix}"] = float(value)
                except ValueError:
                    sample[f"gpu{index}_{suffix}"] = None
        for label, unit in self.UNITS.items():
            output = self._command([
                "systemctl", "--user", "show", unit,
                "--property=CPUUsageNSec", "--property=MemoryCurrent",
            ])
            properties = dict(
                line.split("=", 1) for line in output.splitlines() if "=" in line
            )
            try:
                cpu_ns = int(properties.get("CPUUsageNSec", "0"))
            except ValueError:
                cpu_ns = 0
            try:
                memory = int(properties.get("MemoryCurrent", "0"))
            except ValueError:
                memory = 0
            previous = self._previous_cpu.get(label)
            cpu_percent = None
            if previous and now > previous[1] and cpu_ns >= previous[0]:
                cpu_percent = (cpu_ns - previous[0]) / ((now - previous[1]) * 1e9) * 100.0
            self._previous_cpu[label] = (cpu_ns, now)
            sample[f"{label}_cpu_percent"] = cpu_percent
            sample[f"{label}_memory_mib"] = memory / 1024 / 1024 if memory else None
        return sample

    async def run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(await asyncio.to_thread(self._sample_sync))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


class Validator:
    def __init__(self, args: argparse.Namespace, env: dict[str, str]) -> None:
        self.args = args
        self.env = env
        self.results: list[dict[str, Any]] = []
        self.references: dict[str, dict[str, Any]] = {}
        self.output_dir: Path = args.output_dir
        self.audio_dir = self.output_dir / "audio-samples"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.token = os.environ.get("SERVICE_TOKEN") or env.get("SERVICE_TOKEN", "")
        if not self.token:
            raise RuntimeError("SERVICE_TOKEN is not configured")
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(args.timeout, connect=20),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            headers=self.headers,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def transcribe(self, audio: bytes, language: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10)) as client:
                response = await client.post(
                    self.args.asr_url.rstrip("/") + "/asr",
                    data={"language": language, "beam_size": "5"},
                    files={"file": ("generated.wav", audio, "audio/wav")},
                )
            response.raise_for_status()
            body = response.json()
            return "".join(
                str(segment.get("text") or "") for segment in body.get("segments") or []
            ).strip()
        except (httpx.HTTPError, ValueError):
            return ""

    async def synthesize(self, case: TestCase, sample_index: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": case.text,
            "language": case.target_language,
            "voice_profile_id": "default",
            "quality_mode": "standard",
            "clone_mode": "auto",
            "emotion_strategy": "inherit",
            "emotion_enhance": False,
            "prosody": {"speed": 1.0, "volume": 1.0, "pitch": 1.0},
        }
        if case.reference_url:
            payload["reference_audio_url"] = case.reference_url
            payload["prompt_text"] = case.prompt_text or ""
        started = time.perf_counter()
        try:
            response = await self.client.post(
                self.args.base_url.rstrip("/") + "/v2/tts/speech", json=payload,
            )
            latency = time.perf_counter() - started
        except httpx.HTTPError as exc:
            return {
                "case_id": case.case_id, "phase": case.phase, "mode": case.mode,
                "source_language": case.source_language, "target_language": case.target_language,
                "length": case.length, "concurrency": case.concurrency,
                "sample_index": sample_index, "http_status": 0,
                "latency_seconds": time.perf_counter() - started,
                "error": type(exc).__name__, "cer": 1.0, "wer": None,
                "content_exact": False, "quality_passed": False,
            }
        info = wav_info(response.content) if response.status_code == 200 else wav_info(b"")
        transcript = (
            await self.transcribe(response.content, case.target_language)
            if response.status_code == 200 else ""
        )
        cer, wer, asr_prefix, asr_suffix = text_errors(
            case.text,
            transcript,
            case.target_language,
        )
        headers = response.headers

        def header_float(name: str) -> float | None:
            value = headers.get(name)
            if not value or value == "unavailable":
                return None
            try:
                return float(value)
            except ValueError:
                return None

        def header_int(name: str) -> int | None:
            value = headers.get(name)
            try:
                return int(value) if value is not None else None
            except ValueError:
                return None

        quality_header = headers.get("x-tts-quality-passed")
        gate_phonetic_cer = header_float("x-tts-content-phonetic-cer")
        result: dict[str, Any] = {
            "case_id": case.case_id, "phase": case.phase, "mode": case.mode,
            "source_language": case.source_language, "target_language": case.target_language,
            "length": case.length, "concurrency": case.concurrency,
            "sample_index": sample_index, "http_status": response.status_code,
            "latency_seconds": round(latency, 4), "audio_bytes": len(response.content),
            "transcript": transcript, "expected_text": case.text,
            "cer": round(cer, 6), "wer": round(wer, 6) if wer is not None else None,
            "content_exact": cer == 0.0,
            "asr_unexpected_prefix": asr_prefix, "asr_unexpected_suffix": asr_suffix,
            "quality_passed": (
                quality_header == "true" if quality_header is not None else None
            ),
            "quality_attempts": header_int("x-tts-quality-attempts"),
            "selected_attempt": header_int("x-tts-selected-attempt"),
            "speaker_similarity": header_float("x-tts-speaker-similarity"),
            "gate_content_cer": header_float("x-tts-content-cer"),
            "gate_phonetic_cer": gate_phonetic_cer,
            "speed_ratio": header_float("x-tts-speed-ratio"),
            "first_sound_seconds": header_float("x-tts-first-sound-duration"),
            "last_sound_seconds": header_float("x-tts-last-sound-duration"),
            "gate_unexpected_prefix": headers.get("x-tts-unexpected-prefix") == "true",
            "gate_unexpected_suffix": headers.get("x-tts-unexpected-suffix") == "true",
            "gate_edge_available": headers.get("x-tts-unexpected-prefix") is not None,
            "pronunciation_exact": cer == 0.0 or gate_phonetic_cer == 0.0,
            "tail_trimmed": headers.get("x-tts-tail-trimmed") == "true",
            "degraded_reason": headers.get("x-tts-degraded-reason", "missing"),
            "effective_clone_mode": headers.get("x-tts-clone-mode"),
            "clone_fallback": headers.get("x-tts-clone-fallback"),
            "reference_language_detected": headers.get("x-tts-reference-language"),
            "target_language_detected": headers.get("x-tts-target-language"),
            "candidate_count": header_int("x-tts-candidate-count"),
            "candidate_similarities": headers.get("x-tts-candidate-similarities"),
            **info,
        }
        should_save = sample_index == 0 or response.status_code != 200 or cer > 0.1
        if should_save and response.status_code == 200:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", case.case_id)
            (self.audio_dir / f"{safe_name}-{sample_index}.wav").write_bytes(response.content)
        return result

    async def generate_references(self) -> None:
        required = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_ENDPOINT", "OSS_BUCKET")
        if any(not self.env.get(name) for name in required):
            raise RuntimeError("OSS configuration is incomplete")
        bucket = oss2.Bucket(
            oss2.Auth(self.env["OSS_ACCESS_KEY_ID"], self.env["OSS_ACCESS_KEY_SECRET"]),
            self.env["OSS_ENDPOINT"], self.env["OSS_BUCKET"],
        )
        public_base = self.env.get("OSS_PUBLIC_BASE_URL") or (
            f"https://{self.env['OSS_BUCKET']}.{self.env['OSS_ENDPOINT']}"
        )
        for locale in LOCALES:
            case = TestCase(
                case_id=f"reference-{locale['language']}", phase="reference",
                mode="no_reference", source_language=None,
                target_language=locale["language"], length="reference",
                text=locale["reference"],
            )
            result = await self.synthesize(case, 0)
            sample_path = self.audio_dir / f"reference-{locale['language']}-0.wav"
            if result["http_status"] != 200 or not sample_path.exists():
                raise RuntimeError(f"unable to generate {locale['language']} reference")
            content = sample_path.read_bytes()
            object_key = (
                f"ai-centre/validation/tts-multilingual/{self.args.run_id}/"
                f"reference-{locale['locale']}.wav"
            )
            await asyncio.to_thread(
                bucket.put_object, object_key, content,
                headers={"Content-Type": "audio/wav"},
            )
            public_url = public_base.rstrip("/") + "/" + quote(object_key, safe="/")
            self.references[locale["language"]] = {
                "locale": locale["locale"], "country": locale["country"],
                "name": locale["name"], "object_key": object_key,
                "url": public_url, "prompt_text": locale["reference"],
                "duration_seconds": result.get("duration_seconds"),
            }

    def build_matrix(self) -> list[TestCase]:
        cases: list[TestCase] = []
        repetitions = {"short": self.args.matrix_short_repeats,
                       "medium": self.args.matrix_medium_repeats,
                       "long": self.args.matrix_long_repeats}
        for locale in LOCALES:
            for length in LENGTHS:
                cases.append(TestCase(
                    case_id=f"matrix-plain-{locale['language']}-{length}",
                    phase="matrix", mode="no_reference", source_language=None,
                    target_language=locale["language"], length=length,
                    text=locale[length], repeats=repetitions[length],
                ))
                reference = self.references[locale["language"]]
                cases.append(TestCase(
                    case_id=f"matrix-same-{locale['language']}-{length}",
                    phase="matrix", mode="same_language_clone",
                    source_language=locale["language"], target_language=locale["language"],
                    length=length, text=locale[length], reference_url=reference["url"],
                    prompt_text=reference["prompt_text"], repeats=repetitions[length],
                ))
        cross_repetitions = {"short": self.args.cross_short_repeats,
                             "medium": self.args.cross_medium_repeats}
        for source, target in CROSS_PAIRS:
            reference = self.references[source]
            for length in ("short", "medium"):
                target_locale = LOCALE_BY_LANGUAGE[target]
                cases.append(TestCase(
                    case_id=f"matrix-cross-{source}-to-{target}-{length}",
                    phase="matrix", mode="cross_language_clone",
                    source_language=source, target_language=target,
                    length=length, text=target_locale[length],
                    reference_url=reference["url"], prompt_text=reference["prompt_text"],
                    repeats=cross_repetitions[length],
                ))
        for source, target in (("es", "zh"), ("en", "zh"), ("ja", "zh"),
                               ("zh", "en"), ("zh", "es"), ("zh", "ja")):
            reference = self.references[source]
            target_locale = LOCALE_BY_LANGUAGE[target]
            cases.append(TestCase(
                case_id=f"matrix-cross-{source}-to-{target}-long",
                phase="matrix", mode="cross_language_clone",
                source_language=source, target_language=target,
                length="long", text=target_locale["long"],
                reference_url=reference["url"], prompt_text=reference["prompt_text"],
                repeats=self.args.matrix_long_repeats,
            ))
        return cases

    def build_benchmarks(self) -> list[TestCase]:
        scenarios = [
            ("plain", None, "zh", "no_reference"),
            ("same", "zh", "zh", "same_language_clone"),
            ("cross", "es", "zh", "cross_language_clone"),
        ]
        cases: list[TestCase] = []
        for scenario, source, target, mode in scenarios:
            reference = self.references.get(source or "")
            for concurrency in self.args.concurrency:
                cases.append(TestCase(
                    case_id=f"benchmark-{scenario}-c{concurrency}",
                    phase="benchmark", mode=mode, source_language=source,
                    target_language=target, length="medium",
                    text=LOCALE_BY_LANGUAGE[target]["medium"],
                    reference_url=reference["url"] if reference else None,
                    prompt_text=reference["prompt_text"] if reference else None,
                    concurrency=concurrency, repeats=self.args.benchmark_repeats,
                ))
        return cases

    async def run_case(self, case: TestCase) -> None:
        semaphore = asyncio.Semaphore(case.concurrency)

        async def one(index: int) -> dict[str, Any]:
            async with semaphore:
                return await self.synthesize(case, index)

        started = time.perf_counter()
        rows = await asyncio.gather(*(one(index) for index in range(case.repeats)))
        elapsed = time.perf_counter() - started
        for row in rows:
            row["case_wall_seconds"] = round(elapsed, 4)
            row["case_throughput_rps"] = round(case.repeats / max(0.001, elapsed), 4)
        self.results.extend(rows)
        exact = sum(row.get("content_exact") is True for row in rows)
        print(
            f"[{len(self.results):04d}] {case.case_id}: "
            f"HTTP={sum(row.get('http_status') == 200 for row in rows)}/{len(rows)} "
            f"exact={exact}/{len(rows)} wall={elapsed:.2f}s",
            flush=True,
        )


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(row["latency_seconds"]) for row in rows if row.get("latency_seconds") is not None]
    durations = [float(row["duration_seconds"]) for row in rows if row.get("duration_seconds")]
    similarities = [float(row["speaker_similarity"]) for row in rows if row.get("speaker_similarity") is not None]
    cers = [float(row["cer"]) for row in rows if row.get("cer") is not None]
    wers = [float(row["wer"]) for row in rows if row.get("wer") is not None]
    gated = [row for row in rows if row.get("quality_passed") is not None]
    return {
        "requests": len(rows),
        "http_success_rate": sum(row.get("http_status") == 200 for row in rows) / max(1, len(rows)),
        "exact_rate": sum(row.get("content_exact") is True for row in rows) / max(1, len(rows)),
        "pronunciation_exact_rate": sum(
            row.get("pronunciation_exact") is True for row in rows
        ) / max(1, len(rows)),
        "cer_mean": mean(cers), "wer_mean": mean(wers),
        "gate_coverage": len(gated) / max(1, len(rows)),
        "gate_pass_rate": sum(row.get("quality_passed") is True for row in gated) / max(1, len(gated)),
        "p50_latency": percentile(latencies, 0.50), "p95_latency": percentile(latencies, 0.95),
        "p99_latency": percentile(latencies, 0.99), "p50_duration": percentile(durations, 0.50),
        "p99_duration": percentile(durations, 0.99),
        "speaker_mean": mean(similarities), "speaker_min": min(similarities) if similarities else None,
        "extra_speech": sum(
            (
                bool(row.get("gate_unexpected_prefix"))
                or bool(row.get("gate_unexpected_suffix"))
            )
            if row.get("gate_edge_available")
            else (
                bool(row.get("asr_unexpected_prefix"))
                or bool(row.get("asr_unexpected_suffix"))
            )
            for row in rows
        ),
        "tail_trimmed": sum(bool(row.get("tail_trimmed")) for row in rows),
        "retry_rate": sum((row.get("quality_attempts") or 0) > 1 for row in rows) / max(1, len(rows)),
    }


def resource_summary(samples: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    columns = sorted({key for row in samples for key in row if key != "timestamp"})
    summary: dict[str, dict[str, float | None]] = {}
    for column in columns:
        values = [float(row[column]) for row in samples if isinstance(row.get(column), int | float)]
        if values:
            summary[column] = {
                "mean": mean(values), "p95": percentile(values, 0.95), "max": max(values),
            }
    return summary


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_report(
    run_id: str,
    results: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    references: dict[str, dict[str, Any]],
    started_at: str,
    finished_at: str,
) -> tuple[str, dict[str, Any]]:
    measured = [row for row in results if row.get("phase") != "reference"]
    overall = aggregate(measured)
    by_language: dict[str, dict[str, Any]] = {}
    for language in LOCALE_BY_LANGUAGE:
        by_language[language] = {}
        for mode in ("no_reference", "same_language_clone", "cross_language_clone"):
            rows = [row for row in measured if row["target_language"] == language and row["mode"] == mode]
            if rows:
                by_language[language][mode] = aggregate(rows)
    cross_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    benchmark_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in measured:
        if row["mode"] == "cross_language_clone":
            cross_groups[(str(row["source_language"]), str(row["target_language"]))].append(row)
        if row["phase"] == "benchmark":
            benchmark_groups[row["case_id"]].append(row)
    resources_agg = resource_summary(resources)
    benchmark_repetitions = min(
        (len(rows) for rows in benchmark_groups.values()),
        default=0,
    )
    degradation = Counter(
        row.get("degraded_reason") or "missing" for row in measured
        if row.get("degraded_reason") not in (None, "none")
    )
    failures = [
        row for row in measured
        if row.get("http_status") != 200
        or (
            float(row.get("cer") or 0) > 0.10
            and row.get("pronunciation_exact") is not True
        )
        or row.get("quality_passed") is False
        or row.get("gate_unexpected_prefix") or row.get("gate_unexpected_suffix")
    ]
    structured = {
        "run_id": run_id, "started_at": started_at, "finished_at": finished_at,
        "locale_count": len(LOCALES), "reference_count": len(references),
        "overall": overall, "by_language": by_language,
        "cross_pairs": {f"{source}->{target}": aggregate(rows) for (source, target), rows in cross_groups.items()},
        "benchmarks": {key: aggregate(rows) for key, rows in benchmark_groups.items()},
        "resources": resources_agg, "degradation_reasons": degradation,
        "failure_count": len(failures), "failures": failures[:100],
    }

    lines = [
        "# RTX 5090 VoxCPM2 多语言、克隆与并发系统测试报告",
        "",
        f"- 测试批次：`{run_id}`",
        f"- 开始时间（UTC）：`{started_at}`",
        f"- 完成时间（UTC）：`{finished_at}`",
        f"- 覆盖：{len(LOCALES)} 个国家/语言、无参考/同语言克隆/跨语言克隆、短中长文本、并发 1/2/4。",
        "- 正确率由部署中的 Faster-Whisper 对输出重新识别后计算；克隆相似度来自生产 ERes2NetV2 门禁。",
        "- 逐字一致率保留ASR原始正字法；发音/正字法等价一致率额外接受日语汉字/假名、中文简繁等同音转写。",
        "",
        "## 一、执行摘要",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| 有效请求数 | {overall['requests']} |",
        f"| HTTP成功率 | {overall['http_success_rate']:.2%} |",
        f"| ASR逐字完全一致率 | {overall['exact_rate']:.2%} |",
        f"| ASR发音/正字法等价一致率 | {overall['pronunciation_exact_rate']:.2%} |",
        f"| 平均CER | {overall['cer_mean']:.4f} |",
        f"| 同步质量门禁覆盖率 | {overall['gate_coverage']:.2%} |",
        f"| 质量门禁通过率 | {overall['gate_pass_rate']:.2%} |",
        f"| 检出的额外前后发声 | {overall['extra_speech']} |",
        f"| 触发安全裁尾 | {overall['tail_trimmed']} |",
        f"| 触发额外质量候选 | {overall['retry_rate']:.2%} |",
        f"| 全体请求P50/P95/P99 | {fmt(overall['p50_latency'])} / {fmt(overall['p95_latency'])} / {fmt(overall['p99_latency'])} 秒 |",
        "",
        "## 二、各语言结果",
        "",
        "| 国家/语言 | 无参考：准确率 / P95 | 同语言克隆：准确率 / P95 / 说话人相似度均值 | 作为跨语言目标：准确率 / 说话人相似度均值 |",
        "| --- | --- | --- | --- |",
    ]
    for locale in LOCALES:
        data = by_language.get(locale["language"], {})
        plain = data.get("no_reference")
        same = data.get("same_language_clone")
        cross = data.get("cross_language_clone")
        def cell(item: dict[str, Any] | None, speaker: bool = False) -> str:
            if not item:
                return "—"
            value = f"{item['exact_rate']:.1%} / {fmt(item['p95_latency'])}s"
            if speaker:
                value += f" / {fmt(item['speaker_mean'], 3)}"
            return value
        lines.append(
            f"| {locale['country']}·{locale['name']}（{locale['locale']}） | "
            f"{cell(plain)} | {cell(same, True)} | {cell(cross, True)} |"
        )

    lines.extend([
        "",
        "## 三、跨语言克隆",
        "",
        "| 语言方向 | 请求数 | 完全一致率 | 平均CER | 门禁通过率 | ERes2NetV2均值 / 最低 | P95 | 额外发声 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for (source, target), rows in sorted(cross_groups.items()):
        item = aggregate(rows)
        lines.append(
            f"| {source} → {target} | {item['requests']} | {item['exact_rate']:.1%} | "
            f"{item['cer_mean']:.4f} | {item['gate_pass_rate']:.1%} | "
            f"{fmt(item['speaker_mean'], 3)} / {fmt(item['speaker_min'], 3)} | "
            f"{fmt(item['p95_latency'])}s | {item['extra_speech']} |"
        )

    lines.extend([
        "",
        "## 四、并发与吞吐",
        "",
        f"专门性能组每个场景、每个并发度均执行{benchmark_repetitions}次，因此本节P95比覆盖矩阵中的少量重复更有统计意义。",
        "",
        "| 场景 | 并发 | 请求数 | 准确率 | 门禁通过率 | P50 / P95 / P99 | 吞吐（请求/秒） | ERes2NetV2均值 |",
        "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |",
    ])
    for case_id, rows in sorted(benchmark_groups.items()):
        item = aggregate(rows)
        throughput = float(rows[0].get("case_throughput_rps") or 0)
        lines.append(
            f"| {case_id} | {rows[0]['concurrency']} | {item['requests']} | "
            f"{item['exact_rate']:.1%} | {item['gate_pass_rate']:.1%} | "
            f"{fmt(item['p50_latency'])} / {fmt(item['p95_latency'])} / {fmt(item['p99_latency'])}s | "
            f"{throughput:.3f} | {fmt(item['speaker_mean'], 3)} |"
        )

    lines.extend([
        "",
        "## 五、资源占用",
        "",
        "| 资源 | 平均 | P95 | 最大 |",
        "| --- | ---: | ---: | ---: |",
    ])
    resource_labels = {
        "gpu1_util_percent": "RTX 5090 GPU利用率（%）",
        "gpu1_memory_used_mib": "RTX 5090显存（MiB）",
        "gpu1_power_w": "RTX 5090功耗（W）",
        "gpu1_temperature_c": "RTX 5090温度（℃）",
        "voxcpm2_cpu_percent": "VoxCPM2 cgroup CPU（%）",
        "voxcpm2_memory_mib": "VoxCPM2 cgroup内存（MiB）",
        "control_cpu_percent": "控制面 cgroup CPU（%）",
        "control_memory_mib": "控制面 cgroup内存（MiB）",
        "speaker_cpu_percent": "ERes2NetV2 cgroup CPU（%）",
        "speaker_memory_mib": "ERes2NetV2 cgroup内存（MiB）",
    }
    for key, label in resource_labels.items():
        item = resources_agg.get(key)
        if item:
            lines.append(
                f"| {label} | {fmt(item['mean'])} | {fmt(item['p95'])} | {fmt(item['max'])} |"
            )

    lines.extend([
        "",
        "## 六、质量门禁与异常",
        "",
        f"- 门禁未通过或CER超过10%的记录：{len(failures)} 条。",
        f"- 安全裁尾：{overall['tail_trimmed']} 次。",
        f"- 额外前后发声：{overall['extra_speech']} 次。",
        f"- 重试率：{overall['retry_rate']:.2%}。",
        f"- 降级原因统计：`{dict(degradation) if degradation else {'none': 0}}`。",
        "- 每条请求的门禁CER、拼音CER、首尾音时长、说话人相似度、实际模式和回退原因均保存在原始CSV/JSON中。",
        "",
        "## 七、方法与边界",
        "",
        "- 参考音频由同一套生产VoxCPM2先生成，再上传到生产OSS，避免依赖来源不明的外部样本。",
        "- 同语言克隆提供准确参考文本；跨语言由控制面自动识别语言并路由到隔离Reference/Controllable模式。",
        "- CER/WER来自ASR，因此会包含ASR自身误差；报告同时保留控制面门禁CER用于交叉核对。",
        f"- 矩阵覆盖组主要用于发现语言兼容性问题；并发性能组每格{benchmark_repetitions}次，专用于P95/P99和吞吐统计。",
        "- 资源数据按1秒采样，CPU为systemd cgroup增量，包含子进程；显存和GPU数据来自nvidia-smi。",
        "- 本次测试不会改生产配置，不会真实执行GPU启停，也不会在报告中保存Token或OSS密钥。",
        "",
        "## 八、产物",
        "",
        "- `REPORT.zh-CN.md`：本报告。",
        "- `summary.json`：聚合结果及失败样本。",
        "- `results.csv` / `results.json`：逐请求结果。",
        "- `resources.csv` / `resources.json`：逐秒资源采样。",
        "- `references.json`：参考素材元数据和OSS对象键（不包含密钥）。",
        "- `audio-samples/`：每个测试用例首个输出及失败样本。",
        "",
    ])
    return "\n".join(lines), structured


async def async_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://aicentre2.sligenai.cn:8443")
    parser.add_argument("--asr-url", default="http://127.0.0.1:9001")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output-root", type=Path, default=Path("runtime/validation"))
    parser.add_argument("--run-id", default=datetime.now().strftime("tts-multilingual-%Y%m%d-%H%M%S"))
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--matrix-short-repeats", type=int, default=3)
    parser.add_argument("--matrix-medium-repeats", type=int, default=2)
    parser.add_argument("--matrix-long-repeats", type=int, default=1)
    parser.add_argument("--cross-short-repeats", type=int, default=3)
    parser.add_argument("--cross-medium-repeats", type=int, default=2)
    parser.add_argument("--benchmark-repeats", type=int, default=20)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4])
    args = parser.parse_args()
    args.output_dir = args.output_root / args.run_id
    args.output_dir.mkdir(parents=True, exist_ok=True)
    env = load_env(args.env_file)
    started_at = datetime.now(timezone.utc).isoformat()
    validator = Validator(args, env)
    sampler = ResourceSampler()
    sampler_task = asyncio.create_task(sampler.run())
    try:
        await validator.generate_references()
        for case in validator.build_matrix():
            await validator.run_case(case)
        for case in validator.build_benchmarks():
            await validator.run_case(case)
    finally:
        sampler.stop()
        await sampler_task
        await validator.close()
    finished_at = datetime.now(timezone.utc).isoformat()
    references_for_report = {
        language: {key: value for key, value in reference.items() if key != "url"}
        for language, reference in validator.references.items()
    }
    report, summary = build_report(
        args.run_id, validator.results, sampler.samples,
        references_for_report, started_at, finished_at,
    )
    (args.output_dir / "REPORT.zh-CN.md").write_text(report, encoding="utf-8")
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (args.output_dir / "results.json").write_text(
        json.dumps(validator.results, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (args.output_dir / "resources.json").write_text(
        json.dumps(sampler.samples, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (args.output_dir / "references.json").write_text(
        json.dumps(references_for_report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    write_csv(args.output_dir / "results.csv", validator.results)
    write_csv(args.output_dir / "resources.csv", sampler.samples)
    print(f"REPORT_DIR={args.output_dir}")
    print(json.dumps(summary["overall"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(async_main())
