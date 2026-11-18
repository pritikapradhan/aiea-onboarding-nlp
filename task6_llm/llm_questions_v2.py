"""Task 6, round 2: same nine questions as llm_questions.py, with improved prompts.

What changed compared with round 1 (the questions themselves are unchanged so results can be compared):
  * Open-ended: longer answer limit (600 tokens) so answers are not cut off, and a system
    prompt asking the model to state only facts it is confident about.
  * Few-shot reasoning: examples now show every step of the work (spelling the word out
    letter by letter, naming each relationship, checking the discount threshold explicitly).
  * One-word reasoning: each prompt now has two examples with DIFFERENT answers, so the model
    cannot just copy the answer of a single example.
  * Few-shot answers are now also checked automatically against the expected answer.

Usage:
    python llm_questions_v2.py                      # Qwen2.5-0.5B-Instruct on CPU (fits an 8 GB Mac)
    python llm_questions_v2.py --model Qwen/Qwen2.5-1.5B-Instruct

Outputs (next to this script): results_v2.json and results_v2.md
"""
import argparse
import json
import re
import time
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

DEFAULT_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"

SYSTEM_DEFAULT = "You are a helpful assistant."
SYSTEM_FACTUAL = (
    "You are a careful assistant. Only state facts you are confident about. "
    "If you are not sure about a detail, say you are not sure instead of guessing."
)

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
        "check": lambda a: "nretnal" in a.lower().replace("-", ""),
        "prompt": (
            "You are solving a word puzzle.\n"
            "Rules:\n"
            "1. Spell the word out letter by letter, separated by hyphens.\n"
            "2. Write those letters in reverse order, separated by hyphens.\n"
            "3. Join the reversed letters into a word and capitalize only its first letter.\n\n"
            "Examples:\n"
            "Word: cat\n"
            "Letters: c-a-t\n"
            "Reversed letters: t-a-c\n"
            "Answer: Tac\n\n"
            "Word: planet\n"
            "Letters: p-l-a-n-e-t\n"
            "Reversed letters: t-e-n-a-l-p\n"
            "Answer: Tenalp\n\n"
            "Word: river\n"
            "Letters: r-i-v-e-r\n"
            "Reversed letters: r-e-v-i-r\n"
            "Answer: Revir\n\n"
            "Now solve this one in exactly the same format.\n"
            "Word: lantern"
        ),
    },
    {
        "name": "reasoning_2_family_relations",
        "expected": "Anna is Ben's great-grandparent",
        "check": lambda a: "great-grandparent" in a.lower().replace("great grandparent", "great-grandparent")
        and "great-great" not in a.lower(),
        "prompt": (
            "You are solving a family-relationship puzzle.\n"
            "Rules:\n"
            "1. One link (A is the parent of B): A is the parent of B.\n"
            "2. Two links (A is the parent of B, B is the parent of C): A is the grandparent of C.\n"
            "3. Three links (A is the parent of B, B is the parent of C, C is the parent of D): "
            "A is the great-grandparent of D.\n"
            "First count the number of parent links from the older person down to the younger "
            "person, then use the matching rule.\n\n"
            "Example 1:\n"
            "Facts: Maria is the parent of Luis. Luis is the parent of Sofia.\n"
            "Question: What is Maria to Sofia?\n"
            "Links: Maria -> Luis -> Sofia = 2 links.\n"
            "Answer: Maria is Sofia's grandparent.\n\n"
            "Example 2:\n"
            "Facts: Omar is the parent of Lena. Lena is the parent of Raj. Raj is the parent of Tia.\n"
            "Question: What is Omar to Tia?\n"
            "Links: Omar -> Lena -> Raj -> Tia = 3 links.\n"
            "Answer: Omar is Tia's great-grandparent.\n\n"
            "Now solve this one in exactly the same format.\n"
            "Facts: Anna is the parent of Carl. Carl is the parent of Dana. Dana is the parent of Ben.\n"
            "Question: What is Anna to Ben?"
        ),
    },
    {
        "name": "reasoning_3_discount_math",
        "expected": "$47.60",
        "check": lambda a: "47.6" in a,
        "prompt": (
            "You are solving a shopping-cost problem.\n"
            "Rules:\n"
            "1. Total = price per item x number of items.\n"
            "2. Check: is the total 50 or more? If YES, subtract 15% of the total. "
            "If NO, there is no discount.\n\n"
            "Example 1:\n"
            "Problem: 4 notebooks at $5 each.\n"
            "Total: 4 x 5 = 20.\n"
            "Check: is 20 at least 50? NO, so no discount.\n"
            "Answer: $20.00\n\n"
            "Example 2:\n"
            "Problem: 6 books at $9 each.\n"
            "Total: 6 x 9 = 54.\n"
            "Check: is 54 at least 50? YES, so subtract 15% of 54. 15% of 54 = 8.10. 54 - 8.10 = 45.90.\n"
            "Answer: $45.90\n\n"
            "Now solve this one in exactly the same format.\n"
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
            "Rules: Use only the facts given. If the conclusion must be true, answer True. "
            "If it does not have to be true, answer False.\n\n"
            "Example 1: All cats are animals. All animals need water. Do all cats need water? Answer: True\n"
            "Example 2: All pens are tools. Some tools are heavy. Are all pens heavy? Answer: False\n\n"
            "Facts: All bloops are razzies. All razzies are lazzies.\n"
            "Question: Are all bloops lazzies? Answer True or False.\n"
            + ONE_WORD_INSTRUCTION
        ),
    },
    {
        "name": "oneword_2_affirming_consequent",
        "expected": "no",
        "prompt": (
            "Rules: Only answer Yes if the facts force the conclusion. Otherwise answer No.\n\n"
            "Example 1: If it is a square, it has four sides. This shape is a square. "
            "Does it have four sides? Answer: Yes\n"
            "Example 2: If it is a dog, it has fur. This animal has fur. "
            "Is it necessarily a dog? Answer: No\n\n"
            "Facts: If it rains, the ground gets wet. The ground is wet.\n"
            "Question: Does it necessarily follow that it rained? Answer Yes or No.\n"
            + ONE_WORD_INSTRUCTION
        ),
    },
    {
        "name": "oneword_3_ordering",
        "expected": "false",
        "prompt": (
            "Rules: Use only the facts given. Answer True or False.\n\n"
            "Example 1: Ann is older than Bo. Bo is older than Cy. Is Ann older than Cy? Answer: True\n"
            "Example 2: Ann is older than Bo. Bo is older than Cy. Is Cy older than Ann? Answer: False\n\n"
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
    model = AutoModelForCausalLM.from_pretrained(model_name, dtype=dtype)
    model.to(device)
    model.eval()
    return tok, model


def generate(tok, model, device, system_prompt, user_prompt, max_new_tokens):
    messages = [
        {"role": "system", "content": system_prompt},
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
        [("open-ended", q, 600, SYSTEM_FACTUAL) for q in OPEN_ENDED]
        + [("reasoning (few-shot)", q, 300, SYSTEM_DEFAULT) for q in FEW_SHOT]
        + [("reasoning (one word)", q, 6, SYSTEM_DEFAULT) for q in ONE_WORD]
    )

    results = []
    for kind, q, max_new, system_prompt in jobs:
        print("=" * 80)
        print(f"[{kind}] {q['name']}")
        print("-" * 80)
        t = time.time()
        answer = generate(tok, model, device, system_prompt, q["prompt"], max_new)
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
        elif kind == "reasoning (few-shot)":
            record["correct"] = bool(q["check"](answer))
        results.append(record)
        print(answer)
        if "expected" in q:
            print(f"\n(expected: {q['expected']})")
        if "parsed" in record:
            print(f"(parsed: {record['parsed']!r})")
        if "correct" in record:
            print(f"(correct: {record['correct']})")
        print(f"({secs:.1f}s)\n", flush=True)

    graded = [r for r in results if "correct" in r]
    n_ok = sum(r["correct"] for r in graded)
    print(f"Score on the 6 reasoning questions: {n_ok}/{len(graded)}")

    here = Path(__file__).resolve().parent
    (here / "results_v2.json").write_text(json.dumps(results, indent=2))

    lines = [f"# Task 6 results, round 2\n\nModel: `{args.model}` on `{device}`, greedy decoding.\n",
             f"Score on the 6 reasoning questions: **{n_ok}/{len(graded)}**\n"]
    for r in results:
        lines.append(f"## {r['name']} ({r['kind']})\n")
        lines.append("**Prompt**\n\n```\n" + r["prompt"] + "\n```\n")
        lines.append("**Answer**\n\n```\n" + r["answer"] + "\n```\n")
        if "expected" in r:
            lines.append(f"Expected: `{r['expected']}`")
        if "parsed" in r:
            lines.append(f"Parsed one-word answer: `{r['parsed']}`")
        if "correct" in r:
            lines.append(f"Correct: **{r['correct']}**")
        lines.append(f"Time: {r['seconds']}s\n")
    (here / "results_v2.md").write_text("\n".join(lines))
    print("Saved results_v2.json and results_v2.md")


if __name__ == "__main__":
    main()
