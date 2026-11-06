"""
Vision-language readers for handwritten prescriptions.

Plain OCR reads one glyph at a time, so a doctor's scrawl comes out as noise. A
vision-language model reads the page the way a pharmacist does: it uses the layout,
the "Tab"/"Cap" prefixes, dosing patterns and its knowledge of drug names to decide
what each squiggle most likely says. Both engines here return one normalised line per
medicine ("Tab Torsemide 5mg") plus alternative readings; the dictionary matcher then
keeps whichever reading names a real brand or generic, so the model's guesses are
always checked against the database rather than trusted.

  ClaudeVisionEngine  PRESCRIPTION_OCR_ENGINE=claude. Sends the image to the Anthropic
                      API. Needs ANTHROPIC_API_KEY. PRESCRIPTION_CLAUDE_MODEL and
                      PRESCRIPTION_CLAUDE_EFFORT override the defaults.
  LocalVisionEngine   PRESCRIPTION_OCR_ENGINE=local-vlm. Runs a Qwen3-VL checkpoint on
                      this machine; nothing leaves it. PRESCRIPTION_VLM_MODEL overrides
                      the default Hugging Face id (or points at a local folder).
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import threading

import numpy as np

from app.prescription.ocr import OcrLine, OcrUnavailableError

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_CLAUDE_EFFORT = "high"
DEFAULT_LOCAL_MODEL = "Qwen/Qwen3-VL-2B-Instruct"

# Claude downsizes larger images anyway; the local model's memory grows with pixels.
CLAUDE_MAX_SIDE = 2000
LOCAL_MAX_SIDE = 1280

LEGIBILITY_CONFIDENCE = {"high": 0.9, "medium": 0.65, "low": 0.35}
LOCAL_MAX_CONFIDENCE = 0.45

# Words the line parser recognises as a dosage form (see matcher.FORM_ALIASES).
FORM_WORDS = {
    "tablet": "Tab", "capsule": "Cap", "syrup": "Syp", "suspension": "Susp",
    "injection": "Inj", "drops": "Drops", "cream": "Cream", "ointment": "Oint",
    "gel": "Gel", "lotion": "Lotion", "inhaler": "Inhaler", "powder": "Powder",
    "sachet": "Sachet", "solution": "Sol", "spray": "Spray",
}

PROMPT = """\
You are an experienced Indian pharmacist reading a doctor's prescription from a photo.
Handwriting is often hard to read; use the layout, dosage-form prefixes (Tab, Cap, Syp,
Inj), strengths, dosing patterns (1-0-1, BD, TDS, OD, SOS, x 5 days) and your knowledge
of medicine names sold in India to work out what each medicine line says.

Return every medicine the doctor prescribed, in order. Do not list the hospital or
doctor's name, patient details, diagnoses, investigations, advice such as physiotherapy
or diet, or the signature.

For each medicine:
- written_as: the line exactly as you can read it, including dosing.
- name: your best reading of the medicine name (brand or generic) as the doctor meant it,
  spelled correctly. Leave out the dosage form and strength.
- alternatives: up to 3 other medicine names the handwriting could plausibly be, most
  likely first; an empty list if the name is clear.
- strength: the strength as written, e.g. "650mg", "5mg", "125mg/5ml"; "" if none.
- form: one of tablet, capsule, syrup, suspension, injection, drops, cream, ointment,
  gel, lotion, inhaler, powder, sachet, solution, spray, or "" if not written or implied.
- dosing: how to take it, in plain words, e.g. "1 tablet twice a day for 5 days"; "" if
  none.
- legibility: "high" if you are confident of the name, "medium" if you had to infer
  part of it, "low" if it is mostly a guess.

Never invent a medicine to fill a line you cannot read; use legibility "low" and give
your alternatives instead. If the image is not a prescription (a CV, a bill, a form with
no medicines), set is_prescription to false and return no medicines."""

SCHEMA = {
    "type": "object",
    "properties": {
        "is_prescription": {"type": "boolean"},
        "medicines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "written_as": {"type": "string"},
                    "name": {"type": "string"},
                    "alternatives": {"type": "array", "items": {"type": "string"}},
                    "strength": {"type": "string"},
                    "form": {"type": "string"},
                    "dosing": {"type": "string"},
                    "legibility": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": [
                    "written_as", "name", "alternatives", "strength", "form",
                    "dosing", "legibility",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_prescription", "medicines"],
    "additionalProperties": False,
}


def _encode_jpeg(image: np.ndarray, max_side: int) -> tuple[bytes, "object"]:
    from PIL import Image

    img = Image.fromarray(image)
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue(), img


def _line_text(form: str, name: str, strength: str) -> str:
    prefix = FORM_WORDS.get(form.strip().lower(), "")
    return " ".join(part for part in (prefix, name.strip(), strength.strip()) if part)


def result_to_lines(result: dict) -> list[OcrLine]:
    """Turn the model's JSON into lines the matcher understands."""
    if not result.get("is_prescription", True):
        return []
    lines = []
    for med in result.get("medicines", []):
        name = str(med.get("name", "")).strip()
        if not name:
            continue
        form = str(med.get("form", ""))
        strength = str(med.get("strength", ""))
        alternatives = [
            _line_text(form, str(alt), strength)
            for alt in med.get("alternatives", [])[:3]
            if str(alt).strip() and str(alt).strip().lower() != name.lower()
        ]
        note_parts = []
        if med.get("written_as"):
            note_parts.append(f"Written: {str(med['written_as']).strip()}")
        if med.get("dosing"):
            note_parts.append(f"Dosing: {str(med['dosing']).strip()}")
        lines.append(
            OcrLine(
                text=_line_text(form, name, strength),
                confidence=LEGIBILITY_CONFIDENCE.get(str(med.get("legibility")), 0.5),
                alternatives=alternatives,
                note=" · ".join(note_parts) or None,
            )
        )
    return lines


MEDICINE_OBJECT_RE = re.compile(r"\{[^{}]*\"name\"[^{}]*\}", flags=re.DOTALL)


def _parse_json(text: str) -> dict:
    """
    Small local models wrap the JSON in code fences, and can loop until the token limit
    cuts the answer off mid-object. Fall back to the medicine objects that are complete.
    """
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            pass
    medicines, seen = [], set()
    for raw in MEDICINE_OBJECT_RE.findall(text):
        try:
            med = json.loads(raw)
        except ValueError:
            continue
        key = (str(med.get("name", "")).lower(), str(med.get("strength", "")).lower())
        if key not in seen:
            seen.add(key)
            medicines.append(med)
    if not medicines:
        raise ValueError("no JSON object in the model output")
    return {"is_prescription": True, "medicines": medicines}


class ClaudeVisionEngine:
    def __init__(self, model: str | None = None, effort: str | None = None):
        try:
            import anthropic
        except ImportError as exc:
            raise OcrUnavailableError(
                "The Claude reader needs the Anthropic SDK. Run: pip install anthropic"
            ) from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self.model = model or os.environ.get("PRESCRIPTION_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
        self.effort = effort or os.environ.get("PRESCRIPTION_CLAUDE_EFFORT", DEFAULT_CLAUDE_EFFORT)
        self.name = f"claude:{self.model}"

    def read(self, image: np.ndarray) -> list[OcrLine]:
        anthropic = self._anthropic
        jpeg, _ = _encode_jpeg(image, CLAUDE_MAX_SIDE)
        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                # On a safety decline the API retries on a fallback model within the call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64.standard_b64encode(jpeg).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": PROMPT},
                    ],
                }],
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise OcrUnavailableError(
                "The Claude reader could not authenticate. Set ANTHROPIC_API_KEY."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise OcrUnavailableError("The Claude reader is rate limited; try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise OcrUnavailableError(f"The Claude reader failed ({exc.status_code}).") from exc
        except anthropic.APIConnectionError as exc:
            raise OcrUnavailableError("Could not reach the Anthropic API.") from exc
        except TypeError as exc:
            # The client raises TypeError when no credentials can be resolved.
            raise OcrUnavailableError(
                "The Claude reader has no credentials. Set ANTHROPIC_API_KEY."
            ) from exc

        if response.stop_reason == "refusal":
            raise OcrUnavailableError("The Claude reader declined to read this image.")
        if response.stop_reason == "max_tokens":
            raise OcrUnavailableError("The Claude reader's answer was cut off; try a single page.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return result_to_lines(json.loads(text))
        except (ValueError, TypeError) as exc:
            raise OcrUnavailableError("The Claude reader returned an unreadable answer.") from exc


class LocalVisionEngine:
    def __init__(self, model_path: str | None = None):
        try:
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor
        except ImportError as exc:
            raise OcrUnavailableError(
                "The local reader needs extra packages. Run: pip install transformers accelerate"
            ) from exc
        model_path = model_path or os.environ.get("PRESCRIPTION_VLM_MODEL", DEFAULT_LOCAL_MODEL)
        self._torch = torch
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if self._device == "cuda" else torch.float32
        self._processor = AutoProcessor.from_pretrained(model_path)
        self._model = AutoModelForImageTextToText.from_pretrained(
            model_path, dtype=dtype
        ).to(self._device)
        self._model.eval()
        self.name = f"local-vlm:{os.path.basename(os.path.normpath(model_path))}"
        self._lock = threading.Lock()

    def read(self, image: np.ndarray) -> list[OcrLine]:
        _, img = _encode_jpeg(image, LOCAL_MAX_SIDE)
        prompt = (
            PROMPT
            + "\n\nAnswer with only a JSON object matching this schema, no other text:\n"
            + json.dumps(SCHEMA)
        )
        messages = [{
            "role": "user",
            "content": [{"type": "image", "image": img}, {"type": "text", "text": prompt}],
        }]
        with self._lock:
            inputs = self._processor.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True,
                return_dict=True, return_tensors="pt",
            ).to(self._device)
            with self._torch.no_grad():
                # The penalty stops small models re-listing the same medicine forever.
                out = self._model.generate(
                    **inputs, max_new_tokens=1536, do_sample=False, repetition_penalty=1.15
                )
            generated = out[:, inputs["input_ids"].shape[1]:]
            text = self._processor.batch_decode(generated, skip_special_tokens=True)[0]
        try:
            lines = result_to_lines(_parse_json(text))
        except (ValueError, TypeError):
            # A small model can produce broken JSON; treat it as "nothing readable".
            return []
        # Small local models invent plausible medicines on illegible lines and rate them
        # legible, so their self-reported legibility is not trusted: every line is shown
        # for review (below LOW_OCR_CONFIDENCE).
        for line in lines:
            line.confidence = min(line.confidence, LOCAL_MAX_CONFIDENCE)
        return lines
