"""Task 6: ask an open LLM nine questions (3 open-ended, 3 few-shot reasoning, 3 one-word reasoning).

Usage:
    python llm_questions.py                                  # default model, auto device
    python llm_questions.py --model Qwen/Qwen2.5-0.5B-Instruct   # smaller model if memory is tight
    python llm_questions.py --device cpu                     # force CPU if MPS misbehaves

Outputs (next to this script): results.json and results.md
"""
import argparse
import json
import re
import time
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

DEFAULT_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"

# ---------------------------------------------------------------------------
# The nine questions
# ---------------------------------------------------------------------------

# Part 1: open-ended (long answers, no examples)
OPEN_ENDED = [
    {
        "name": "open_1_french_revolution",
        "prompt": "Summarize the main causes, key events, and long-term consequences of the French "
                  "Revolution in a few paragraphs.",
    },
    {
        "name": "open_2_apollo_11",
        "prompt": "Describe what happened during the Apollo 11 mission, from launch to the return of "
                  "the astronauts, and explain why it was historically important.",
    },
    {
        "name": "open_3_vaccines",
        "prompt": "Explain how vaccines train the immune system, and discuss why widespread "
                  "vaccination can protect people who are not vaccinated.",
    },
]

# Part 2: reasoning with rules + examples in the prompt (few-shot prompt engineering)
FEW_SHOT = [
    {
        "name": "reasoning_1_word_rule",
        "expected": "Nretnal",
        "prompt": (
            "You are solving a word puzzle.\n"
            "Rules:\n"
            "1. Reverse the letters of the word.\n"
            "2. Then capitalize only the first letter of the reversed word.\n\n"
            "Examples:\n"
            "Word: cat -> Reversed: tac -> Answer: Tac\n"
            "Word: planet -> Reversed: tenalp -> Answer: Tenalp\n"
            "Word: river -> Reversed: revir -> Answer: Revir\n\n"
            "Now solve this one. Show the reversed word, then the answer.\n"
            "Word: lantern"
        ),
    },
    {
        "name": "reasoning_2_family_relations",
        "expected": "Anna is Ben's great-grandparent",
        "prompt": (
            "You are solving a family-relationship puzzle.\n"
            "Rules:\n"
            "1. If A is the parent of B, then B is the child of A.\n"
            "2. If A is the parent of B and B is the parent of C, then A is the grandparent of C.\n"
            "3. If A is the grandparent of B and B is the parent of C, then A is the great-grandparent of C.\n\n"
            "Example:\n"
            "Facts: Maria is the parent of Luis. Luis is the parent of Sofia.\n"
            "Question: What is Maria to Sofia?\n"
            "Reasoning: Maria is the parent of Luis, and Luis is the parent of Sofia (rule 2).\n"
            "Answer: Maria is Sofia's grandparent.\n\n"
            "Now solve this one using the same style.\n"
            "Facts: Anna is the parent of Carl. Carl is the parent of Dana. Dana is the parent of Ben.\n"
            "Question: What is Anna to Ben?"
        ),
    },
    {
        "name": "reasoning_3_discount_math",
        "expected": "$47.60",
        "prompt": (
            "You are solving a shopping-cost problem.\n"
            "Rules:\n"
            "1. Total = price per item x number of items.\n"
            "2. If the total is at least $50, subtract a 15% discount.\n"
            "3. If the total is under $50, there is no discount.\n\n"
            "Examples:\n"
            "Problem: 4 notebooks at $5 each.\n"
            "Work: 4 x 5 = $20. Under $50, so no discount. Answer: $20.00\n\n"
            "Problem: 10 pens at $8 each.\n"
            "Work: 10 x 8 = $80. At least $50, so subtract 15% ($12). Answer: $68.00\n\n"
            "Now solve this one the same way.\n"
            "Problem: 7 mugs at $8 each."
        ),
    },
]

# Part 3: reasoning where the answer must be ONE word
ONE_WORD_INSTRUCTION = "Answer with exactly one word and nothing else."
ONE_WORD = [
    {
        "name": "oneword_1_syllogism",
        "expected": "true",
        "prompt": (
            "Rules: Use only the facts given. Answer True or False.\n"
            "Example: All cats are animals. All animals need water. Do all cats need water? Answer: True\n\n"
            "Facts: All bloops are razzies. All razzies are lazzies.\n"
            "Question: Are all bloops lazzies? Answer True or False.\n"
            + ONE_WORD_INSTRUCTION
        ),
    },
    {
        "name": "oneword_2_affirming_consequent",
        "expected": "no",
        "prompt": (
            "Rules: Only conclude something if the facts force it. Answer Yes or No.\n"
            "Example: If it is a square, it has four sides. This shape is a square. "
            "Does it have four sides? Answer: Yes\n\n"
            "Facts: If it rains, the ground gets wet. The ground is wet.\n"
            "Question: Does it necessarily follow that it rained? Answer Yes or No.\n"
            + ONE_WORD_INSTRUCTION
        ),
    },
    {
        "name": "oneword_3_ordering",
        "expected": "false",
        "prompt": (
            "Rules: Use only the facts given. Answer True or False.\n"
            "Example: Ann is older than Bo. Bo is older than Cy. Is Ann older than Cy? Answer: True\n\n"
            "Facts: Tom is taller than Sam. Sam is taller than Lee.\n"
            "Question: Is Lee taller than Tom? Answer True or False.\n"
            + ONE_WORD_INSTRUCTION
        ),
    },
]


# ---------------------------------------------------------------------------
# Model loading and generation
# ---------------------------------------------------------------------------

def pick_device(requested):
    if requested != "auto":
        return requested
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def load_model(model_name, device):
    dtype = torch.float16 if device in ("mps", "cuda") else torch.float32
    tok = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=dtype)
    model.to(device)
    model.eval()
    return tok, model


def generate(tok, model, device, user_prompt, max_new_tokens):
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": user_prompt},
    ]
    text = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tok(text, return_tensors="pt").to(device)
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,       # greedy decoding so the run is reproducible
            temperature=None,
            top_p=None,
            top_k=None,
        )
    new_tokens = out[0][inputs["input_ids"].shape[1]:]
    return tok.decode(new_tokens, skip_special_tokens=True).strip()


def first_word(text):
    m = re.search(r"[A-Za-z]+", text)
    return m.group(0).lower() if m else ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "mps", "cuda"])
    args = ap.parse_args()

    device = pick_device(args.device)
    print(f"Loading {args.model} on {device} ...", flush=True)
    t0 = time.time()
    tok, model = load_model(args.model, device)
    print(f"Loaded in {time.time() - t0:.1f}s\n", flush=True)

    jobs = (
        [("open-ended", q, 350) for q in OPEN_ENDED]
        + [("reasoning (few-shot)", q, 250) for q in FEW_SHOT]
        + [("reasoning (one word)", q, 6) for q in ONE_WORD]
    )

    results = []
    for kind, q, max_new in jobs:
        print("=" * 80)
        print(f"[{kind}] {q['name']}")
        print("-" * 80)
        t = time.time()
        answer = generate(tok, model, device, q["prompt"], max_new)
        secs = time.time() - t
        record = {
            "kind": kind,
            "name": q["name"],
            "prompt": q["prompt"],
            "answer": answer,
            "seconds": round(secs, 1),
        }
        if "expected" in q:
            record["expected"] = q["expected"]
        if kind == "reasoning (one word)":
            record["parsed"] = first_word(answer)
            record["correct"] = record["parsed"] == q["expected"]
        results.append(record)
        print(answer)
        if "expected" in q:
            print(f"\n(expected: {q['expected']})")
        if "correct" in record:
            print(f"(parsed: {record['parsed']!r}, correct: {record['correct']})")
        print(f"({secs:.1f}s)\n", flush=True)

    here = Path(__file__).resolve().parent
    (here / "results.json").write_text(json.dumps(results, indent=2))

    lines = [f"# Task 6 results\n\nModel: `{args.model}` on `{device}`, greedy decoding.\n"]
    for r in results:
        lines.append(f"## {r['name']} ({r['kind']})\n")
        lines.append("**Prompt**\n\n```\n" + r["prompt"] + "\n```\n")
        lines.append("**Answer**\n\n```\n" + r["answer"] + "\n```\n")
        if "expected" in r:
            lines.append(f"Expected: `{r['expected']}`")
        if "correct" in r:
            lines.append(f"Parsed one-word answer: `{r['parsed']}` | correct: **{r['correct']}**")
        lines.append(f"Time: {r['seconds']}s\n")
    (here / "results.md").write_text("\n".join(lines))
    print("Saved results.json and results.md")


if __name__ == "__main__":
    main()
