from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import torch
    from managed_backends import speaker_verify_api
except ModuleNotFoundError:
    torch = None
    speaker_verify_api = None


@unittest.skipIf(speaker_verify_api is None, "speaker verifier dependencies unavailable")
class SpeakerVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        speaker_verify_api._reference_embeddings.clear()

    def test_reference_embedding_is_reused_by_audio_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_reference = root / "reference-a.wav"
            second_reference = root / "reference-b.wav"
            first_candidate = root / "candidate-a.wav"
            second_candidate = root / "candidate-b.wav"
            first_reference.write_bytes(b"same-reference")
            second_reference.write_bytes(b"same-reference")
            first_candidate.write_bytes(b"candidate-one")
            second_candidate.write_bytes(b"candidate-two")
            embeddings = {
                str(first_reference): torch.tensor([1.0, 0.0]),
                str(second_reference): torch.tensor([1.0, 0.0]),
                str(first_candidate): torch.tensor([1.0, 0.0]),
                str(second_candidate): torch.tensor([0.0, 1.0]),
            }
            calls: list[Path] = []

            def embedding(path: Path):
                calls.append(path)
                return embeddings[str(path)]

            with patch.object(speaker_verify_api, "_embedding", side_effect=embedding):
                first = speaker_verify_api._similarity(
                    first_reference,
                    first_candidate,
                )
                second = speaker_verify_api._similarity(
                    second_reference,
                    second_candidate,
                )

        self.assertAlmostEqual(first, 1.0)
        self.assertAlmostEqual(second, 0.0)
        self.assertEqual(calls.count(first_reference), 1)
        self.assertNotIn(second_reference, calls)
        self.assertEqual(len(speaker_verify_api._reference_embeddings), 1)


if __name__ == "__main__":
    unittest.main()
